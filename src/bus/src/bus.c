/* whitecube-bus: connections, SASL, routing, name registry, driver. */
#include "whitecube_bus.h"
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <unistd.h>

#define ST_NUL 0
#define ST_LINE 1
#define ST_ACTIVE 2

#define T_CALL 1
#define T_RET 2
#define T_ERR 3
#define T_SIG 4

#define DRIVER "org.freedesktop.DBus"
#define DPATH  "/org/freedesktop/DBus"

typedef struct OutMsg {
    struct OutMsg *next;
    uint8_t *p; size_t len, off;
    int fds[WC_MAX_FDS_MSG]; int nfds;
    bool fds_sent;
} OutMsg;

typedef struct {
    unsigned idx;
    bool is_path;
    char *val;
} ArgRule;

typedef struct {
    char *raw; int refs;
    char *type, *sender, *dest, *iface, *member, *path, *path_ns, *arg0ns;
    ArgRule args[WC_MAX_RULE_ARGS];
    int n_args;
} Match;

typedef struct Conn {
    Bus *bus;
    int fd;
    uint32_t id;
    char uniq[24];
    uid_t uid; pid_t pid;
    int state;
    bool authed, await_data, fd_ok, registered, closing, monitor, monitor_filtered;
    int auth_fails;
    Buf in;
    OutMsg *out_head, *out_tail;
    size_t out_bytes;
    int infds[WC_MAX_FDS_CONN]; int n_infds;
    Match **match; int n_match, cap_match;
    struct Conn *next;
} Conn;

typedef struct { char *name; Conn *owner; uint32_t flags; } Name;

struct Bus {
    Options opt;
    int lfd, epfd;
    uid_t uid; pid_t pid;
    Conn *conns; int n_conns;
    Name names[WC_MAX_NAMES]; int n_names;
    uint32_t next_id, next_serial;
    char guid[33], machine[33];
};

/* ---------------- forward declarations ---------------- */
static void conn_shutdown(Conn *c);
static void conn_flush(Conn *c);
static bool conn_queue(Conn *c, const uint8_t *p, size_t len, const int *fds, int nfds);
static void drv_error(Bus *b, Conn *c, const Msg *call, const char *name, const char *text);
static void drv_reply(Bus *b, Conn *c, const Msg *call, const char *sig, Buf *body);
static void drv_signal(Bus *b, const char *member, const char *sig, Buf *body, Conn *dest);
static void deliver(Bus *b, Conn *from, const char *sender, Msg *m, const uint8_t *raw,
                    int *fds, int nfds);
static Conn *resolve(Bus *b, const char *name);
static void name_owner_changed(Bus *b, const char *name, const char *old, const char *neu);

/* ---------------- audit ---------------- */
static const char *tname(uint8_t t)
{
    switch (t) { case T_CALL: return "call"; case T_RET: return "return";
                 case T_ERR: return "error"; case T_SIG: return "signal"; default: return "?"; }
}

static void audit_route(Bus *b, Conn *c, const Msg *m, int nfds)
{
    if (!b->opt.audit) return;
    fprintf(stderr, "[%lu:%ld %s -> %s] %s %s.%s (fds=%d)\n",
            (unsigned long)c->uid, (long)c->pid,
            c->registered ? c->uniq : "-", m->dest ? m->dest : "*",
            tname(m->type), m->iface ? m->iface : "-",
            m->member ? m->member : "-", nfds);
}

static void audit_note(Bus *b, const char *fmt, ...)
{
    if (!b->opt.audit) return;
    va_list ap; va_start(ap, fmt);
    fputs("[audit] ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

/* ---------------- connection lifecycle ---------------- */
static void conn_arm(Conn *c)
{
    if (c->closing) return;
    struct epoll_event ev;
    memset(&ev, 0, sizeof ev);
    ev.events = EPOLLIN | (c->out_head ? EPOLLOUT : 0);
    ev.data.ptr = c;
    epoll_ctl(c->bus->epfd, EPOLL_CTL_MOD, c->fd, &ev);
}

static void conn_shutdown(Conn *c)
{
    if (c->closing) return;
    c->closing = true;
    epoll_ctl(c->bus->epfd, EPOLL_CTL_DEL, c->fd, NULL);
}

static void outmsg_free(OutMsg *o)
{
    for (int i = 0; i < o->nfds; i++) if (o->fds[i] >= 0) close(o->fds[i]);
    free(o->p);
    free(o);
}

static void match_free(Match *r)
{
    if (!r) return;
    free(r->raw); free(r->type); free(r->sender); free(r->dest);
    free(r->iface); free(r->member); free(r->path); free(r->path_ns);
    free(r->arg0ns);
    for (int i = 0; i < r->n_args; i++) free(r->args[i].val);
    free(r);
}

static void conn_free(Bus *b, Conn *c)
{
    /* release every owned well-known name */
    for (int i = 0; i < b->n_names; i++) {
        if (b->names[i].owner == c) {
            name_owner_changed(b, b->names[i].name, c->uniq, "");
            free(b->names[i].name);
            b->names[i] = b->names[--b->n_names];
            i--;
        }
    }
    if (c->registered) name_owner_changed(b, c->uniq, c->uniq, "");

    OutMsg *o = c->out_head;
    while (o) { OutMsg *n = o->next; outmsg_free(o); o = n; }
    for (int i = 0; i < c->n_infds; i++) close(c->infds[i]);
    for (int i = 0; i < c->n_match; i++) match_free(c->match[i]);
    free(c->match);
    buf_free(&c->in);
    if (c->fd >= 0) close(c->fd);
    free(c);
}

static void bus_reap(Bus *b)
{
    Conn **pp = &b->conns;
    while (*pp) {
        Conn *c = *pp;
        if (c->closing) { *pp = c->next; b->n_conns--; conn_free(b, c); }
        else pp = &c->next;
    }
}

/* ---------------- output ---------------- */
static bool conn_queue(Conn *c, const uint8_t *p, size_t len, const int *fds, int nfds)
{
    if (c->closing) return false;
    if (c->out_bytes + len > WC_OUT_CAP) {
        audit_note(c->bus, "slow consumer %s disconnected (queue %zu B)", c->uniq, c->out_bytes);
        conn_shutdown(c);
        return false;
    }
    OutMsg *o = calloc(1, sizeof *o);
    if (!o) return false;
    o->p = malloc(len);
    if (!o->p) { free(o); return false; }
    memcpy(o->p, p, len);
    o->len = len;
    for (int i = 0; i < nfds; i++) {
        int nd = fcntl(fds[i], F_DUPFD_CLOEXEC, 3);
        if (nd < 0) { outmsg_free(o); return false; }
        o->fds[o->nfds++] = nd;
    }
    if (c->out_tail) c->out_tail->next = o; else c->out_head = o;
    c->out_tail = o;
    c->out_bytes += len;
    conn_flush(c);
    return true;
}

static void conn_flush(Conn *c)
{
    union { char buf[CMSG_SPACE(sizeof(int) * WC_MAX_FDS_MSG)]; struct cmsghdr align; } ctl;

    while (c->out_head && !c->closing) {
        OutMsg *o = c->out_head;
        struct iovec iov;
        struct msghdr mh;
        memset(&mh, 0, sizeof mh);
        iov.iov_base = o->p + o->off;
        iov.iov_len = o->len - o->off;
        mh.msg_iov = &iov;
        mh.msg_iovlen = 1;
        if (!o->fds_sent && o->nfds > 0) {
            memset(ctl.buf, 0, sizeof ctl.buf);
            mh.msg_control = ctl.buf;
            mh.msg_controllen = CMSG_SPACE(sizeof(int) * (size_t)o->nfds);
            struct cmsghdr *cm = CMSG_FIRSTHDR(&mh);
            cm->cmsg_level = SOL_SOCKET;
            cm->cmsg_type = SCM_RIGHTS;
            cm->cmsg_len = CMSG_LEN(sizeof(int) * (size_t)o->nfds);
            memcpy(CMSG_DATA(cm), o->fds, sizeof(int) * (size_t)o->nfds);
        }
        ssize_t n = sendmsg(c->fd, &mh, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) break;
            conn_shutdown(c);
            break;
        }
        if (n == 0) { conn_shutdown(c); break; }
        if (!o->fds_sent && o->nfds > 0) {
            o->fds_sent = true;
            for (int i = 0; i < o->nfds; i++) { close(o->fds[i]); o->fds[i] = -1; }
        }
        o->off += (size_t)n;
        c->out_bytes -= (size_t)n;
        if (o->off == o->len) {
            c->out_head = o->next;
            if (!c->out_head) c->out_tail = NULL;
            outmsg_free(o);
        }
    }
    conn_arm(c);
}

static bool send_text(Conn *c, const char *s)
{
    if (!conn_queue(c, (const uint8_t *)s, strlen(s), NULL, 0)) { conn_shutdown(c); return false; }
    return !c->closing;
}

/* ---------------- SASL EXTERNAL ---------------- */
static bool auth_external(Bus *b, Conn *c, const char *hex, bool allow_empty)
{
    char dec[32], want[32];
    size_t n = strlen(hex);
    if (n == 0) {
        if (!allow_empty) { c->await_data = true; return send_text(c, "DATA\r\n"); }
        c->authed = true;
        char ok[64];
        snprintf(ok, sizeof ok, "OK %s\r\n", b->guid);
        return send_text(c, ok);
    }
    if ((n & 1) || n / 2 >= sizeof dec) goto reject;
    for (size_t i = 0; i < n / 2; i++) {
        int hi, lo;
        char a = hex[2 * i], d = hex[2 * i + 1];
        if (a >= '0' && a <= '9') hi = a - '0';
        else if (a >= 'a' && a <= 'f') hi = a - 'a' + 10;
        else if (a >= 'A' && a <= 'F') hi = a - 'A' + 10;
        else goto reject;
        if (d >= '0' && d <= '9') lo = d - '0';
        else if (d >= 'a' && d <= 'f') lo = d - 'a' + 10;
        else if (d >= 'A' && d <= 'F') lo = d - 'A' + 10;
        else goto reject;
        dec[i] = (char)((hi << 4) | lo);
    }
    dec[n / 2] = 0;
    for (size_t i = 0; dec[i]; i++) if (dec[i] < '0' || dec[i] > '9') goto reject;
    snprintf(want, sizeof want, "%lu", (unsigned long)c->uid);
    if (strcmp(dec, want) != 0) goto reject;

    c->authed = true;
    char ok[64];
    snprintf(ok, sizeof ok, "OK %s\r\n", b->guid);
    return send_text(c, ok);

reject:
    audit_note(b, "SASL EXTERNAL rejected for pid %ld", (long)c->pid);
    if (++c->auth_fails > WC_AUTH_MAX_FAILS) { conn_shutdown(c); return false; }
    return send_text(c, "REJECTED EXTERNAL\r\n");
}

static bool auth_command(Bus *b, Conn *c, char *line)
{
    if (strncmp(line, "AUTH", 4) == 0 && (line[4] == 0 || line[4] == ' ')) {
        char *mech = line + 4;
        while (*mech == ' ') mech++;
        char *arg = strchr(mech, ' ');
        if (arg) { *arg++ = 0; while (*arg == ' ') arg++; }
        if (strcmp(mech, "EXTERNAL") == 0) return auth_external(b, c, arg ? arg : "", false);
        audit_note(b, "rejected SASL mechanism '%s' from pid %ld", mech, (long)c->pid);
        if (++c->auth_fails > WC_AUTH_MAX_FAILS) { conn_shutdown(c); return false; }
        return send_text(c, "REJECTED EXTERNAL\r\n");
    }
    if (strncmp(line, "DATA", 4) == 0 && (line[4] == 0 || line[4] == ' ')) {
        char *arg = line + 4;
        while (*arg == ' ') arg++;
        if (!c->await_data) return send_text(c, "ERROR\r\n");
        c->await_data = false;
        return auth_external(b, c, arg, true);
    }
    if (strcmp(line, "NEGOTIATE_UNIX_FD") == 0) {
        if (!c->authed) return send_text(c, "ERROR\r\n");
        c->fd_ok = true;
        return send_text(c, "AGREE_UNIX_FD\r\n");
    }
    if (strcmp(line, "BEGIN") == 0) {
        if (!c->authed) return send_text(c, "ERROR\r\n");
        c->state = ST_ACTIVE;
        return true;
    }
    if (strcmp(line, "CANCEL") == 0) { c->authed = false; return send_text(c, "REJECTED EXTERNAL\r\n"); }
    if (strcmp(line, "ERROR") == 0) return send_text(c, "REJECTED EXTERNAL\r\n");
    return send_text(c, "ERROR\r\n");
}

static void consume(Conn *c, size_t n)
{
    if (n >= c->in.len) { c->in.len = 0; return; }
    memmove(c->in.p, c->in.p + n, c->in.len - n);
    c->in.len -= n;
}

static void auth_process(Bus *b, Conn *c)
{
    for (;;) {
        if (c->state == ST_NUL) {
            if (c->in.len == 0) return;
            if (c->in.p[0] != 0) {
                audit_note(b, "missing SASL NUL byte from pid %ld", (long)c->pid);
                conn_shutdown(c);
                return;
            }
            consume(c, 1);
            c->state = ST_LINE;
        }
        uint8_t *nl = memmem(c->in.p, c->in.len, "\r\n", 2);
        if (!nl) {
            if (c->in.len > WC_AUTH_LINE_MAX) conn_shutdown(c);
            return;
        }
        size_t llen = (size_t)(nl - c->in.p);
        char line[WC_AUTH_LINE_MAX + 1];
        memcpy(line, c->in.p, llen);
        line[llen] = 0;
        consume(c, llen + 2);
        if (!auth_command(b, c, line)) return;
        if (c->state == ST_ACTIVE || c->closing) return;
    }
}

/* ---------------- names ---------------- */
static int name_idx(Bus *b, const char *name)
{
    for (int i = 0; i < b->n_names; i++)
        if (strcmp(b->names[i].name, name) == 0) return i;
    return -1;
}

static Conn *resolve(Bus *b, const char *name)
{
    if (!name) return NULL;
    if (name[0] == ':') {
        for (Conn *c = b->conns; c; c = c->next)
            if (c->registered && !c->closing && strcmp(c->uniq, name) == 0) return c;
        return NULL;
    }
    int i = name_idx(b, name);
    if (i < 0 || b->names[i].owner->closing) return NULL;
    return b->names[i].owner;
}

static void name_owner_changed(Bus *b, const char *name, const char *old, const char *neu)
{
    Buf body; memset(&body, 0, sizeof body);
    bld_s(&body, name); bld_s(&body, old); bld_s(&body, neu);
    drv_signal(b, "NameOwnerChanged", "sss", &body, NULL);
}

static void name_signal(Bus *b, Conn *c, const char *member, const char *name)
{
    Buf body; memset(&body, 0, sizeof body);
    bld_s(&body, name);
    drv_signal(b, member, "s", &body, c);
}

/* Privacy invariant: no process may ever own a name that offers a way to
 * view, capture or remote-control the screen.  This list is checked before
 * the allowlist, so no --allow pattern can override it. */
static const char *const HARD_DENY[] = {
    "org.freedesktop.impl.portal.",        /* all portal backends */
    "org.freedesktop.portal.ScreenCast",
    "org.freedesktop.portal.Screenshot",
    "org.freedesktop.portal.RemoteDesktop",
    "org.freedesktop.portal.InputCapture",
    "org.gnome.Shell.Screenshot",
    "org.gnome.Shell.Screencast",
    "org.gnome.Mutter.ScreenCast",
    "org.gnome.Mutter.RemoteDesktop",
    "org.kde.KWin",
    "org.kde.Spectacle",
    "org.kde.krfb",
    "org.freedesktop.ScreenCast",
    "org.freedesktop.Screenshot",
};

static bool hard_denied(const char *name)
{
    for (size_t i = 0; i < sizeof HARD_DENY / sizeof *HARD_DENY; i++)
        if (strncmp(name, HARD_DENY[i], strlen(HARD_DENY[i])) == 0) return true;
    return false;
}

static bool allowlisted(Bus *b, const char *name)
{
    if (hard_denied(name)) return false;
    for (size_t i = 0; i < b->opt.n_allow; i++) {
        const char *pat = b->opt.allow[i];
        size_t n = strlen(pat);
        if (n >= 2 && pat[n - 1] == '*' && (pat[n - 2] == '.' || pat[n - 2] == '-')) {
            if (strncmp(name, pat, n - 1) == 0 && name[n - 1] != 0) return true;
        } else if (strcmp(pat, name) == 0) {
            return true;
        }
    }
    return false;
}

static bool activation_allowed(Bus *b, const char *name)
{
    if (hard_denied(name)) return false;
    for (size_t i = 0; i < b->opt.n_allow_activation; i++) {
        const char *pat = b->opt.allow_activation[i];
        size_t n = strlen(pat);
        if (n >= 2 && pat[n - 1] == '*' && (pat[n - 2] == '.' || pat[n - 2] == '-')) {
            if (strncmp(name, pat, n - 1) == 0 && name[n - 1] != 0) return true;
        } else if (strcmp(pat, name) == 0) {
            return true;
        }
    }
    return false;
}

static Service *find_service(Bus *b, const char *name)
{
    for (size_t i = 0; i < b->opt.n_services; i++) {
        if (strcmp(b->opt.services[i].name, name) == 0)
            return &b->opt.services[i];
    }
    return NULL;
}

static bool activate_service(Bus *b, Service *svc)
{
    pid_t pid = fork();
    if (pid < 0) {
        audit_note(b, "fork failed for service '%s': %s", svc->name, strerror(errno));
        return false;
    }
    if (pid == 0) {
        /* Child process */
        if (svc->user) {
            /* In a real implementation, we'd switch user here.
             * For now, we just exec directly. */
        }
        execvp(svc->argv[0], svc->argv);
        _exit(127);
    }

    audit_note(b, "activated service '%s' (pid=%ld)", svc->name, (long)pid);
    return true;
}

/* ---------------- match rules ---------------- */
static bool path_prefix(const char *ns, const char *path)
{
    if (!ns || !path) return false;
    size_t n = strlen(ns);
    if (n == 1 && ns[0] == '/') return true;
    if (strncmp(path, ns, n) != 0) return false;
    return path[n] == 0 || path[n] == '/';
}

static bool arg0path_match(const char *a, const char *b)
{
    if (!a || !b) return false;
    if (path_prefix(a, b)) return true;
    return path_prefix(b, a);
}

static bool valid_arg0ns(const char *s)
{
    if (!s || !*s || strlen(s) > 255) return false;
    bool seg = false;
    for (size_t i = 0; s[i]; i++) {
        unsigned char ch = (unsigned char)s[i];
        if (ch == '.') {
            if (!seg) return false;
            seg = false;
        } else if ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
                   (ch >= '0' && ch <= '9') || ch == '_' || ch == '-') {
            if (!seg && ch >= '0' && ch <= '9') return false;
            seg = true;
        } else {
            return false;
        }
    }
    return seg;
}

static bool parse_arg_key(const char *key, unsigned *idx, bool *is_path)
{
    if (strncmp(key, "arg", 3) != 0) return false;
    const char *p = key + 3;
    if (*p < '0' || *p > '9') return false;
    unsigned v = 0;
    while (*p >= '0' && *p <= '9') {
        v = v * 10 + (unsigned)(*p - '0');
        if (v > 63) return false;
        p++;
    }
    if (*p == 0) { *idx = v; *is_path = false; return true; }
    if (!strcmp(p, "path")) { *idx = v; *is_path = true; return true; }
    return false;
}

static Match *match_parse(const char *rule, const char **err)
{
    Match *r = calloc(1, sizeof *r);
    if (!r) { *err = "out of memory"; return NULL; }
    r->refs = 1;
    r->raw = strdup(rule);
    if (!r->raw) { free(r); *err = "out of memory"; return NULL; }

    const char *p = rule;
    while (*p) {
        while (*p == ' ' || *p == '\t' || *p == ',') p++;
        if (!*p) break;
        const char *k = p;
        while (*p && *p != '=') p++;
        if (*p != '=') { *err = "expected key='value'"; goto bad; }
        size_t klen = (size_t)(p - k);
        while (klen > 0 && (k[klen - 1] == ' ' || k[klen - 1] == '\t')) klen--;
        p++;
        while (*p == ' ' || *p == '\t') p++;
        if (*p != '\'') { *err = "value must be single quoted"; goto bad; }
        p++;
        Buf v; memset(&v, 0, sizeof v);
        while (*p && *p != '\'') {
            char ch = *p;
            if (ch == '\\' && p[1]) { p++; ch = *p; }
            buf_put(&v, &ch, 1);
            p++;
        }
        if (*p != '\'') { buf_free(&v); *err = "unterminated value"; goto bad; }
        p++;
        while (*p == ' ' || *p == '\t') p++;
        if (*p && *p != ',') { buf_free(&v); *err = "expected comma between fields"; goto bad; }
        buf_put(&v, "", 1);
        if (v.err) { buf_free(&v); *err = "out of memory"; goto bad; }
        char *val = (char *)v.p;   /* ownership transferred */

        char key[32];
        if (!klen || klen >= sizeof key) { free(val); *err = "unknown key"; goto bad; }
        memcpy(key, k, klen); key[klen] = 0;

        char **slot = NULL;
        unsigned aidx = 0;
        bool is_path = false;
        if (!strcmp(key, "type")) slot = &r->type;
        else if (!strcmp(key, "sender")) slot = &r->sender;
        else if (!strcmp(key, "destination")) slot = &r->dest;
        else if (!strcmp(key, "interface")) slot = &r->iface;
        else if (!strcmp(key, "member")) slot = &r->member;
        else if (!strcmp(key, "path")) slot = &r->path;
        else if (!strcmp(key, "path_namespace")) slot = &r->path_ns;
        else if (!strcmp(key, "arg0namespace")) slot = &r->arg0ns;
        else if (parse_arg_key(key, &aidx, &is_path)) {
            for (int i = 0; i < r->n_args; i++) {
                if (r->args[i].idx == aidx && r->args[i].is_path == is_path) {
                    free(val); *err = "duplicate key"; goto bad;
                }
            }
            if (r->n_args >= WC_MAX_RULE_ARGS) {
                free(val); *err = "too many arg keys"; goto bad;
            }
            r->args[r->n_args].idx = aidx;
            r->args[r->n_args].is_path = is_path;
            r->args[r->n_args].val = val;
            r->n_args++;
            continue;
        } else if (!strcmp(key, "eavesdrop")) {
            bool on = !strcmp(val, "true");
            free(val);
            if (on) { *err = "eavesdropping is not permitted"; goto bad; }
            continue;
        } else { free(val); *err = "unsupported match key"; goto bad; }

        if (*slot) { free(val); *err = "duplicate key"; goto bad; }
        *slot = val;
    }

    if (r->type && strcmp(r->type, "signal") && strcmp(r->type, "method_call") &&
        strcmp(r->type, "method_return") && strcmp(r->type, "error")) {
        *err = "invalid type"; goto bad;
    }
    if (r->sender && !wc_valid_unique(r->sender) && !wc_valid_wellknown(r->sender) &&
        strcmp(r->sender, DRIVER) != 0) { *err = "invalid sender"; goto bad; }
    if (r->dest && !wc_valid_unique(r->dest) && !wc_valid_wellknown(r->dest) &&
        strcmp(r->dest, DRIVER) != 0) { *err = "invalid destination"; goto bad; }
    if (r->iface && !wc_valid_interface(r->iface)) { *err = "invalid interface"; goto bad; }
    if (r->member && !wc_valid_member(r->member)) { *err = "invalid member"; goto bad; }
    if (r->path && !wc_valid_path_n(r->path, strlen(r->path))) { *err = "invalid path"; goto bad; }
    if (r->path_ns && !wc_valid_path_n(r->path_ns, strlen(r->path_ns))) { *err = "invalid path_namespace"; goto bad; }
    if (r->path && r->path_ns) { *err = "path and path_namespace are exclusive"; goto bad; }
    if (r->arg0ns && !valid_arg0ns(r->arg0ns)) { *err = "invalid arg0namespace"; goto bad; }
    return r;

bad:
    match_free(r);
    return NULL;
}

static const char *spec_tname(uint8_t t)
{
    switch (t) {
    case T_CALL: return "method_call";
    case T_RET:  return "method_return";
    case T_ERR:  return "error";
    case T_SIG:  return "signal";
    default:     return "";
    }
}

static bool match_hit(Bus *b, Match *r, Conn *from, const char *sender,
                      const Msg *m, const uint8_t *raw)
{
    (void)from;
    if (r->type && strcmp(r->type, spec_tname(m->type)) != 0) return false;
    if (r->sender) {
        if (r->sender[0] == ':' || strcmp(r->sender, DRIVER) == 0) {
            if (strcmp(sender, r->sender) != 0) return false;
        } else {
            Conn *o = resolve(b, r->sender);
            if (!o || strcmp(o->uniq, sender) != 0) return false;
        }
    }
    if (r->dest) {
        if (!m->dest || !*m->dest) return false;
        if (strcmp(m->dest, r->dest) != 0) {
            Conn *d = resolve(b, r->dest);
            if (!d || strcmp(d->uniq, m->dest) != 0) return false;
        }
    }
    if (r->iface && (!m->iface || strcmp(m->iface, r->iface))) return false;
    if (r->member && (!m->member || strcmp(m->member, r->member))) return false;
    if (r->path && (!m->path || strcmp(m->path, r->path))) return false;
    if (r->path_ns && !path_prefix(r->path_ns, m->path)) return false;
    if (r->arg0ns) {
        const char *a = NULL; char tc = 0;
        if (!wire_arg_nth_string(raw, m, 0, &a, &tc) || tc != 's') return false;
        size_t n = strlen(r->arg0ns);
        if (strcmp(a, r->arg0ns) != 0 && (strncmp(a, r->arg0ns, n) != 0 || a[n] != '.'))
            return false;
    }
    for (int i = 0; i < r->n_args; i++) {
        const char *a = NULL; char tc = 0;
        if (!wire_arg_nth_string(raw, m, r->args[i].idx, &a, &tc)) return false;
        if (r->args[i].is_path) {
            if (!arg0path_match(r->args[i].val, a)) return false;
        } else {
            if (tc != 's' || strcmp(a, r->args[i].val) != 0) return false;
        }
    }
    return true;
}

static bool conn_add_match(Conn *c, Match *r)
{
    if (c->n_match >= WC_MAX_MATCH) return false;
    if (c->n_match >= c->cap_match) {
        int ncap = c->cap_match ? c->cap_match * 2 : 16;
        if (ncap > WC_MAX_MATCH) ncap = WC_MAX_MATCH;
        Match **np = realloc(c->match, (size_t)ncap * sizeof(Match *));
        if (!np) return false;
        c->match = np;
        c->cap_match = ncap;
    }
    c->match[c->n_match++] = r;
    return true;
}

/* ---------------- delivery ---------------- */
static void deliver(Bus *b, Conn *from, const char *sender, Msg *m, const uint8_t *raw,
                    int *fds, int nfds)
{
    Buf out; memset(&out, 0, sizeof out);
    if (!wire_rebuild(raw, m, sender, &out)) {
        audit_note(b, "re-marshal failed (oom)");
        buf_free(&out);
        return;
    }
    if (m->dest && *m->dest) {
        Conn *d = resolve(b, m->dest);
        if (d && !d->closing) {
            if (nfds > 0 && !d->fd_ok) {
                if (from && m->type == T_CALL)
                    drv_error(b, from, m, "org.freedesktop.DBus.Error.InvalidArgs",
                              "recipient did not negotiate unix fd passing");
            } else {
                conn_queue(d, out.p, out.len, fds, nfds);
            }
        } else if (m->type == T_CALL && !(m->flags & 0x01) && from) {
            drv_error(b, from, m, "org.freedesktop.DBus.Error.ServiceUnknown",
                      "name is not owned; service activation is disabled in whitecube-bus");
        }
        for (Conn *c = b->conns; c; c = c->next) {
            if (!c->monitor || c->closing || c == from) continue;
            bool hit = !c->monitor_filtered;
            for (int i = 0; i < c->n_match && !hit; i++)
                hit = match_hit(b, c->match[i], from, sender, m, raw);
            if (hit) conn_queue(c, out.p, out.len, fds, nfds);
        }
    } else {
        for (Conn *c = b->conns; c; c = c->next) {
            if (!c->registered || c->closing) continue;
            bool hit = c->monitor && !c->monitor_filtered;
            for (int i = 0; i < c->n_match && !hit; i++)
                hit = match_hit(b, c->match[i], from, sender, m, raw);
            if (hit) conn_queue(c, out.p, out.len, fds, nfds);
        }
    }
    buf_free(&out);
}

static void route(Bus *b, Conn *c, Msg *m, const uint8_t *raw, int *fds, int nfds)
{
    audit_route(b, c, m, nfds);

    if (c->monitor) {
        drv_error(b, c, m, "org.freedesktop.DBus.Error.AccessDenied",
                  "monitor connections may not send messages");
        return;
    }
    bool to_driver = m->dest && strcmp(m->dest, DRIVER) == 0;
    if (!c->registered) {
        if (!(m->type == T_CALL && to_driver && m->member && !strcmp(m->member, "Hello"))) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.AccessDenied",
                      "connection must call Hello() before sending messages");
            return;
        }
    }
    if (to_driver) {
        if (!m->path || strcmp(m->path, DPATH) != 0) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.UnknownObject", "no such object");
            return;
        }
        extern void whitecube_driver(Bus *, Conn *, Msg *, const uint8_t *);
        whitecube_driver(b, c, m, raw);
        return;
    }
    if (!m->dest) {
        if (m->type == T_CALL)
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "message has no destination");
        else if (m->type == T_SIG)
            deliver(b, c, c->uniq, m, raw, fds, nfds);
        return;
    }
    deliver(b, c, c->uniq, m, raw, fds, nfds);
}

/* ---------------- driver replies ---------------- */
static void drv_reply(Bus *b, Conn *c, const Msg *call, const char *sig, Buf *body)
{
    if (call->flags & 0x01) { if (body) buf_free(body); return; }
    Build out;
    bld_init(&out, T_RET, 0, b->next_serial++);
    bld_field_u(&out, 5, call->serial);
    bld_field_s(&out, 6, c->uniq);
    bld_field_s(&out, 7, DRIVER);
    if (sig && *sig) bld_field_g(&out, 8, sig);
    if (body && body->len) { buf_put(&out.body, body->p, body->len); out.body.err |= body->err; }
    size_t n = 0;
    uint8_t *p = bld_finish(&out, &n);
    if (p) { conn_queue(c, p, n, NULL, 0); free(p); }
    if (body) buf_free(body);
}

static void drv_error(Bus *b, Conn *c, const Msg *call, const char *name, const char *text)
{
    Build out;
    bld_init(&out, T_ERR, 0, b->next_serial++);
    bld_field_s(&out, 4, name);
    bld_field_u(&out, 5, call->serial);
    bld_field_s(&out, 6, c->uniq);
    bld_field_s(&out, 7, DRIVER);
    bld_field_g(&out, 8, "s");
    bld_s(&out.body, text);
    size_t n = 0;
    uint8_t *p = bld_finish(&out, &n);
    if (p) { conn_queue(c, p, n, NULL, 0); free(p); }
    audit_note(b, "error to %s: %s (%s)", c->registered ? c->uniq : "-", name, text);
}

static void drv_signal(Bus *b, const char *member, const char *sig, Buf *body, Conn *dest)
{
    Build out;
    bld_init(&out, T_SIG, 0, b->next_serial++);
    bld_field_s(&out, 1, DPATH);
    bld_field_s(&out, 2, DRIVER);
    bld_field_s(&out, 3, member);
    bld_field_s(&out, 7, DRIVER);
    if (dest) bld_field_s(&out, 6, dest->uniq);
    if (sig && *sig) bld_field_g(&out, 8, sig);
    if (body && body->len) { buf_put(&out.body, body->p, body->len); out.body.err |= body->err; }
    size_t n = 0;
    uint8_t *p = bld_finish(&out, &n);
    if (body) buf_free(body);
    if (!p) return;
    Msg m; size_t need = 0;
    if (wire_parse(p, n, &m, &need) == WIRE_OK)
        deliver(b, NULL, DRIVER, &m, p, NULL, 0);
    free(p);
}

/* ---------------- driver methods ---------------- */
static const char INTROSPECT_XML[] =
"<!DOCTYPE node PUBLIC \"-//freedesktop//DTD D-BUS Object Introspection 1.0//EN\"\n"
" \"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd\">\n"
"<node>\n"
" <interface name=\"org.freedesktop.DBus\">\n"
"  <method name=\"Hello\"><arg direction=\"out\" type=\"s\"/></method>\n"
"  <method name=\"RequestName\"><arg direction=\"in\" type=\"s\"/><arg direction=\"in\" type=\"u\"/><arg direction=\"out\" type=\"u\"/></method>\n"
"  <method name=\"ReleaseName\"><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"u\"/></method>\n"
"  <method name=\"GetNameOwner\"><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"s\"/></method>\n"
"  <method name=\"NameHasOwner\"><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"b\"/></method>\n"
"  <method name=\"ListNames\"><arg direction=\"out\" type=\"as\"/></method>\n"
"  <method name=\"ListActivatableNames\"><arg direction=\"out\" type=\"as\"/></method>\n"
"  <method name=\"ListQueuedOwners\"><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"as\"/></method>\n"
"  <method name=\"GetConnectionUnixUser\"><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"u\"/></method>\n"
"  <method name=\"GetConnectionUnixProcessID\"><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"u\"/></method>\n"
"  <method name=\"GetConnectionCredentials\"><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"a{sv}\"/></method>\n"
"  <method name=\"AddMatch\"><arg direction=\"in\" type=\"s\"/></method>\n"
"  <method name=\"RemoveMatch\"><arg direction=\"in\" type=\"s\"/></method>\n"
"  <method name=\"UpdateActivationEnvironment\"><arg direction=\"in\" type=\"a{ss}\"/></method>\n"
"  <method name=\"StartServiceByName\"><arg direction=\"in\" type=\"s\"/><arg direction=\"in\" type=\"u\"/><arg direction=\"out\" type=\"u\"/></method>\n"
"  <method name=\"GetId\"><arg direction=\"out\" type=\"s\"/></method>\n"
"  <property name=\"Features\" type=\"as\" access=\"read\"/>\n"
"  <property name=\"Interfaces\" type=\"as\" access=\"read\"/>\n"
"  <signal name=\"NameOwnerChanged\"><arg type=\"s\"/><arg type=\"s\"/><arg type=\"s\"/></signal>\n"
"  <signal name=\"NameAcquired\"><arg type=\"s\"/></signal>\n"
"  <signal name=\"NameLost\"><arg type=\"s\"/></signal>\n"
" </interface>\n"
" <interface name=\"org.freedesktop.DBus.Properties\">\n"
"  <method name=\"Get\"><arg direction=\"in\" type=\"s\"/><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"v\"/></method>\n"
"  <method name=\"GetAll\"><arg direction=\"in\" type=\"s\"/><arg direction=\"out\" type=\"a{sv}\"/></method>\n"
"  <method name=\"Set\"><arg direction=\"in\" type=\"s\"/><arg direction=\"in\" type=\"s\"/><arg direction=\"in\" type=\"v\"/></method>\n"
" </interface>\n"
" <interface name=\"org.freedesktop.DBus.Peer\">\n"
"  <method name=\"Ping\"/>\n"
"  <method name=\"GetMachineId\"><arg direction=\"out\" type=\"s\"/></method>\n"
" </interface>\n"
" <interface name=\"org.freedesktop.DBus.Introspectable\">\n"
"  <method name=\"Introspect\"><arg direction=\"out\" type=\"s\"/></method>\n"
" </interface>\n"
" <interface name=\"org.freedesktop.DBus.Monitoring\">\n"
"  <method name=\"BecomeMonitor\"><arg direction=\"in\" type=\"as\"/><arg direction=\"in\" type=\"u\"/></method>\n"
" </interface>\n"
"</node>\n";

static Conn *target_conn(Bus *b, const char *name, bool *is_driver)
{
    *is_driver = false;
    if (!name) return NULL;
    if (strcmp(name, DRIVER) == 0) { *is_driver = true; return NULL; }
    return resolve(b, name);
}

static void bld_var_as(Buf *body, char *const *v, size_t n)
{
    uint8_t vsig[4] = { 2, 'a', 's', 0 };
    buf_put(body, vsig, 4);
    bld_as(body, v, n);
}

static bool valid_drv_prop_iface(const char *p)
{
    return !*p || !strcmp(p, DRIVER) || !strcmp(p, DRIVER ".Monitoring") ||
           !strcmp(p, DRIVER ".Peer") || !strcmp(p, DRIVER ".Introspectable") ||
           !strcmp(p, DRIVER ".Properties");
}

void whitecube_driver(Bus *b, Conn *c, Msg *m, const uint8_t *raw)
{
    const char *ifc = m->iface;
    const char *mem = m->member;
    Buf body;
    memset(&body, 0, sizeof body);

    if (m->type != T_CALL) return;

    bool peer = !ifc || !strcmp(ifc, DRIVER) || !strcmp(ifc, DRIVER ".Peer");
    bool intr = !ifc || !strcmp(ifc, DRIVER) || !strcmp(ifc, DRIVER ".Introspectable");
    bool main_ = !ifc || !strcmp(ifc, DRIVER);

    if (peer && !strcmp(mem, "Ping")) { drv_reply(b, c, m, NULL, NULL); return; }
    if (peer && !strcmp(mem, "GetMachineId")) {
        bld_s(&body, b->machine); drv_reply(b, c, m, "s", &body); return;
    }
    if (intr && !strcmp(mem, "Introspect")) {
        bld_s(&body, INTROSPECT_XML); drv_reply(b, c, m, "s", &body); return;
    }
    if (ifc && !strcmp(ifc, DRIVER ".Properties")) {
        char *ifaces[1] = { (char *)(DRIVER ".Monitoring") };
        if (!strcmp(mem, "Get")) {
            const char *pifc = NULL, *prop = NULL; size_t off = 0;
            if (!m->sig || strcmp(m->sig, "ss") ||
                !wire_arg_s(raw, m, &off, &pifc) || !wire_arg_s(raw, m, &off, &prop)) {
                drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (ss)");
                return;
            }
            if (!valid_drv_prop_iface(pifc)) {
                drv_error(b, c, m, "org.freedesktop.DBus.Error.UnknownInterface", "unknown interface");
                return;
            }
            if ((!*pifc || !strcmp(pifc, DRIVER)) && !strcmp(prop, "Features")) {
                bld_var_as(&body, NULL, 0);
                drv_reply(b, c, m, "v", &body);
                return;
            }
            if ((!*pifc || !strcmp(pifc, DRIVER)) && !strcmp(prop, "Interfaces")) {
                bld_var_as(&body, ifaces, 1);
                drv_reply(b, c, m, "v", &body);
                return;
            }
            drv_error(b, c, m, "org.freedesktop.DBus.Error.UnknownProperty", "unknown property");
            return;
        }
        if (!strcmp(mem, "GetAll")) {
            const char *pifc = NULL; size_t off = 0;
            if (!m->sig || strcmp(m->sig, "s") || !wire_arg_s(raw, m, &off, &pifc)) {
                drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (s)");
                return;
            }
            if (!valid_drv_prop_iface(pifc)) {
                drv_error(b, c, m, "org.freedesktop.DBus.Error.UnknownInterface", "unknown interface");
                return;
            }
            buf_pad(&body, 4);
            size_t lenpos = body.len;
            uint8_t z4[4] = { 0, 0, 0, 0 };
            buf_put(&body, z4, 4);
            buf_pad(&body, 8);
            size_t start = body.len;
            if (!*pifc || !strcmp(pifc, DRIVER)) {
                buf_pad(&body, 8);
                bld_s(&body, "Features");
                bld_var_as(&body, NULL, 0);
                buf_pad(&body, 8);
                bld_s(&body, "Interfaces");
                bld_var_as(&body, ifaces, 1);
            }
            if (!body.err) {
                uint32_t sz = (uint32_t)(body.len - start);
                body.p[lenpos]     = (uint8_t)sz;
                body.p[lenpos + 1] = (uint8_t)(sz >> 8);
                body.p[lenpos + 2] = (uint8_t)(sz >> 16);
                body.p[lenpos + 3] = (uint8_t)(sz >> 24);
            }
            drv_reply(b, c, m, "a{sv}", &body);
            return;
        }
        if (!strcmp(mem, "Set")) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.PropertyReadOnly", "properties are read-only");
            return;
        }
        drv_error(b, c, m, "org.freedesktop.DBus.Error.UnknownMethod", "unknown method");
        return;
    }
    if (ifc && !strcmp(ifc, DRIVER ".Monitoring") && !strcmp(mem, "BecomeMonitor")) {
        /* Privacy: a monitor sees every message (paths, URIs, selections).
         * Refuse unless the operator explicitly enabled it. */
        if (!b->opt.allow_monitor) {
            audit_note(b, "DENIED BecomeMonitor by %s (pid %ld)", c->uniq, (long)c->pid);
            drv_error(b, c, m, "org.freedesktop.DBus.Error.AccessDenied",
                      "bus monitoring is disabled");
            return;
        }
        if (!m->sig || strcmp(m->sig, "asu")) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (asu)");
            return;
        }
        size_t off = 0, aend = 0;
        if (!wire_iter_as(raw, m, &off, &aend)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "invalid filter array");
            return;
        }
        Match *parsed[WC_MAX_MATCH];
        int np = 0;
        while (off < aend) {
            const char *rule = NULL;
            if (!wire_arg_s(raw, m, &off, &rule) || strlen(rule) > WC_MAX_MATCH_LEN) {
                for (int i = 0; i < np; i++) match_free(parsed[i]);
                drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "invalid match rule");
                return;
            }
            if (np >= WC_MAX_MATCH) {
                for (int i = 0; i < np; i++) match_free(parsed[i]);
                drv_error(b, c, m, "org.freedesktop.DBus.Error.LimitsExceeded", "too many filter rules");
                return;
            }
            const char *err = NULL;
            parsed[np] = match_parse(rule, &err);
            if (!parsed[np]) {
                for (int i = 0; i < np; i++) match_free(parsed[i]);
                drv_error(b, c, m, "org.freedesktop.DBus.Error.MatchRuleInvalid", err);
                return;
            }
            np++;
        }
        uint32_t mflags = 0;
        if (!wire_arg_u(raw, m, &off, &mflags)) {
            for (int i = 0; i < np; i++) match_free(parsed[i]);
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (asu)");
            return;
        }
        for (int i = 0; i < c->n_match; i++) match_free(c->match[i]);
        c->n_match = 0;
        for (int i = 0; i < np; i++) {
            if (!conn_add_match(c, parsed[i])) {
                for (int j = i; j < np; j++) match_free(parsed[j]);
                drv_error(b, c, m, "org.freedesktop.DBus.Error.OOM", "out of memory");
                return;
            }
        }
        c->monitor = true;
        c->monitor_filtered = (np > 0);
        for (int i = 0; i < b->n_names; i++) {
            if (b->names[i].owner == c) {
                name_owner_changed(b, b->names[i].name, c->uniq, "");
                free(b->names[i].name);
                b->names[i] = b->names[--b->n_names];
                i--;
            }
        }
        audit_note(b, "%s became a monitor (filters=%d flags=%u)", c->uniq, np, mflags);
        drv_reply(b, c, m, NULL, NULL);
        return;
    }
    if (!main_) {
        drv_error(b, c, m, "org.freedesktop.DBus.Error.UnknownInterface", "unknown interface");
        return;
    }

    /* ---- Hello ---- */
    if (!strcmp(mem, "Hello")) {
        if (c->registered) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.Failed", "already registered");
            return;
        }
        c->id = b->next_id++;
        snprintf(c->uniq, sizeof c->uniq, ":1.%u", c->id);
        c->registered = true;
        bld_s(&body, c->uniq);
        drv_reply(b, c, m, "s", &body);
        name_owner_changed(b, c->uniq, "", c->uniq);
        return;
    }
    /* ---- RequestName ---- */
    if (!strcmp(mem, "RequestName")) {
        const char *name = NULL; uint32_t flags = 0; size_t off = 0;
        if (!m->sig || strcmp(m->sig, "su") ||
            !wire_arg_s(raw, m, &off, &name) || !wire_arg_u(raw, m, &off, &flags)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (su)");
            return;
        }
        if (!wc_valid_wellknown(name) || !strcmp(name, DRIVER)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "invalid bus name");
            return;
        }
        if (!allowlisted(b, name)) {
            audit_note(b, "DENIED RequestName '%s' by %s (pid %ld)", name, c->uniq, (long)c->pid);
            drv_error(b, c, m, "org.freedesktop.DBus.Error.AccessDenied",
                      "name is not in the whitecube-bus allowlist");
            return;
        }
        uint32_t rc;
        int idx = name_idx(b, name);
        if (idx >= 0 && b->names[idx].owner == c) {
            b->names[idx].flags = flags;
            rc = 4;
        } else if (idx < 0) {
            if (b->n_names >= WC_MAX_NAMES) {
                drv_error(b, c, m, "org.freedesktop.DBus.Error.LimitsExceeded", "name table full");
                return;
            }
            b->names[b->n_names].name = strdup(name);
            if (!b->names[b->n_names].name) {
                drv_error(b, c, m, "org.freedesktop.DBus.Error.OOM", "out of memory");
                return;
            }
            b->names[b->n_names].owner = c;
            b->names[b->n_names].flags = flags;
            b->n_names++;
            rc = 1;
            name_owner_changed(b, name, "", c->uniq);
            name_signal(b, c, "NameAcquired", name);
        } else if ((flags & 0x2) && (b->names[idx].flags & 0x1)) {
            Conn *old = b->names[idx].owner;
            b->names[idx].owner = c;
            b->names[idx].flags = flags;
            rc = 1;
            name_signal(b, old, "NameLost", name);
            name_signal(b, c, "NameAcquired", name);
            name_owner_changed(b, name, old->uniq, c->uniq);
        } else {
            rc = 3;   /* EXISTS; queueing is deliberately unsupported */
        }
        bld_u(&body, rc);
        drv_reply(b, c, m, "u", &body);
        return;
    }
    /* ---- ReleaseName ---- */
    if (!strcmp(mem, "ReleaseName")) {
        const char *name = NULL; size_t off = 0;
        if (!m->sig || strcmp(m->sig, "s") || !wire_arg_s(raw, m, &off, &name)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (s)");
            return;
        }
        int idx = name_idx(b, name);
        uint32_t rc;
        if (idx < 0) rc = 2;
        else if (b->names[idx].owner != c) rc = 3;
        else {
            rc = 1;
            name_owner_changed(b, name, c->uniq, "");
            name_signal(b, c, "NameLost", name);
            free(b->names[idx].name);
            b->names[idx] = b->names[--b->n_names];
        }
        bld_u(&body, rc);
        drv_reply(b, c, m, "u", &body);
        return;
    }
    /* ---- GetNameOwner / NameHasOwner / ListQueuedOwners ---- */
    if (!strcmp(mem, "GetNameOwner") || !strcmp(mem, "NameHasOwner") ||
        !strcmp(mem, "ListQueuedOwners")) {
        const char *name = NULL; size_t off = 0;
        if (!m->sig || strcmp(m->sig, "s") || !wire_arg_s(raw, m, &off, &name)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (s)");
            return;
        }
        bool drv = false;
        Conn *o = target_conn(b, name, &drv);
        const char *owner = drv ? DRIVER : (o ? o->uniq : NULL);
        if (!strcmp(mem, "NameHasOwner")) {
            bld_u(&body, owner ? 1u : 0u);
            drv_reply(b, c, m, "b", &body);
            return;
        }
        if (!owner) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.NameHasNoOwner", "name has no owner");
            return;
        }
        if (!strcmp(mem, "GetNameOwner")) bld_s(&body, owner);
        else { char *v[1] = { (char *)owner }; bld_as(&body, v, 1); }
        drv_reply(b, c, m, !strcmp(mem, "GetNameOwner") ? "s" : "as", &body);
        return;
    }
    /* ---- ListNames / ListActivatableNames ---- */
    if (!strcmp(mem, "ListNames") || !strcmp(mem, "ListActivatableNames")) {
        if (!strcmp(mem, "ListActivatableNames")) {
            size_t cap = b->opt.n_services;
            char **list = calloc(cap, sizeof(char *));
            if (!list) {
                drv_error(b, c, m, "org.freedesktop.DBus.Error.OOM", "out of memory");
                return;
            }
            size_t n = 0;
            for (size_t i = 0; i < b->opt.n_services && n < cap; i++) {
                if (activation_allowed(b, b->opt.services[i].name)) {
                    list[n++] = b->opt.services[i].name;
                }
            }
            bld_as(&body, list, n);
            free(list);
            drv_reply(b, c, m, "as", &body);
            return;
        }
        size_t cap = (size_t)b->n_names + (size_t)b->n_conns + 1;
        char **list = calloc(cap, sizeof(char *));
        if (!list) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.OOM", "out of memory");
            return;
        }
        size_t n = 0;
        list[n++] = (char *)DRIVER;
        for (Conn *x = b->conns; x; x = x->next)
            if (x->registered && !x->closing && n < cap) list[n++] = x->uniq;
        for (int i = 0; i < b->n_names && n < cap; i++) list[n++] = b->names[i].name;
        bld_as(&body, list, n);
        free(list);
        drv_reply(b, c, m, "as", &body);
        return;
    }
    /* ---- credentials ---- */
    if (!strcmp(mem, "GetConnectionUnixUser") || !strcmp(mem, "GetConnectionUnixProcessID") ||
        !strcmp(mem, "GetConnectionCredentials")) {
        const char *name = NULL; size_t off = 0;
        if (!m->sig || strcmp(m->sig, "s") || !wire_arg_s(raw, m, &off, &name)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (s)");
            return;
        }
        bool drv = false;
        Conn *o = target_conn(b, name, &drv);
        if (!drv && !o) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.NameHasNoOwner", "name has no owner");
            return;
        }
        uint32_t uid = drv ? (uint32_t)b->uid : (uint32_t)o->uid;
        uint32_t pid = drv ? (uint32_t)b->pid : (uint32_t)o->pid;
        if (!strcmp(mem, "GetConnectionCredentials")) {
            bld_creds(&body, uid, pid);
            drv_reply(b, c, m, "a{sv}", &body);
        } else {
            bld_u(&body, !strcmp(mem, "GetConnectionUnixUser") ? uid : pid);
            drv_reply(b, c, m, "u", &body);
        }
        return;
    }
    /* ---- match rules ---- */
    if (!strcmp(mem, "AddMatch") || !strcmp(mem, "RemoveMatch")) {
        const char *rule = NULL; size_t off = 0;
        if (!m->sig || strcmp(m->sig, "s") || !wire_arg_s(raw, m, &off, &rule)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (s)");
            return;
        }
        if (strlen(rule) > WC_MAX_MATCH_LEN) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.LimitsExceeded", "rule too long");
            return;
        }
        if (!strcmp(mem, "AddMatch")) {
            for (int i = 0; i < c->n_match; i++)
                if (!strcmp(c->match[i]->raw, rule)) {
                    c->match[i]->refs++;
                    drv_reply(b, c, m, NULL, NULL);
                    return;
                }
            if (c->n_match >= WC_MAX_MATCH) {
                drv_error(b, c, m, "org.freedesktop.DBus.Error.LimitsExceeded", "too many match rules");
                return;
            }
            const char *err = NULL;
            Match *r = match_parse(rule, &err);
            if (!r) { drv_error(b, c, m, "org.freedesktop.DBus.Error.MatchRuleInvalid", err); return; }
            if (!conn_add_match(c, r)) {
                match_free(r);
                drv_error(b, c, m, "org.freedesktop.DBus.Error.OOM", "out of memory");
                return;
            }
            drv_reply(b, c, m, NULL, NULL);
            return;
        }
        for (int i = 0; i < c->n_match; i++) {
            if (!strcmp(c->match[i]->raw, rule)) {
                if (--c->match[i]->refs <= 0) {
                    match_free(c->match[i]);
                    c->match[i] = c->match[--c->n_match];
                }
                drv_reply(b, c, m, NULL, NULL);
                return;
            }
        }
        drv_error(b, c, m, "org.freedesktop.DBus.Error.MatchRuleNotFound", "no such rule");
        return;
    }
    /* ---- activation env (accepted no-op) ---- */
    if (!strcmp(mem, "UpdateActivationEnvironment")) {
        if (!m->sig || strcmp(m->sig, "a{ss}")) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (a{ss})");
            return;
        }
        audit_note(b, "UpdateActivationEnvironment ignored safely (%s)", c->uniq);
        drv_reply(b, c, m, NULL, NULL);
        return;
    }
    /* ---- service activation ---- */
    if (!strcmp(mem, "StartServiceByName")) {
        const char *name = NULL; uint32_t flags = 0; size_t off = 0;
        if (!m->sig || strcmp(m->sig, "su") ||
            !wire_arg_s(raw, m, &off, &name) || !wire_arg_u(raw, m, &off, &flags)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.InvalidArgs", "expected (su)");
            return;
        }
        if (resolve(b, name)) { bld_u(&body, 2); drv_reply(b, c, m, "u", &body); return; }

        /* Check if activation is allowed for this name */
        if (!activation_allowed(b, name)) {
            audit_note(b, "activation denied for '%s' (%s)", name, c->uniq);
            drv_error(b, c, m, "org.freedesktop.DBus.Error.AccessDenied",
                      "service activation not permitted for this name");
            return;
        }

        /* Find service definition */
        Service *svc = find_service(b, name);
        if (!svc) {
            audit_note(b, "no service definition for '%s' (%s)", name, c->uniq);
            drv_error(b, c, m, "org.freedesktop.DBus.Error.ServiceUnknown",
                      "no service file found");
            return;
        }

        /* Activate the service */
        if (!activate_service(b, svc)) {
            drv_error(b, c, m, "org.freedesktop.DBus.Error.SpawnFailed",
                      "failed to spawn service process");
            return;
        }

        /* Return 1 (service started) - the service will register the name itself */
        bld_u(&body, 1);
        drv_reply(b, c, m, "u", &body);
        return;
    }
    if (!strcmp(mem, "GetId")) { bld_s(&body, b->guid); drv_reply(b, c, m, "s", &body); return; }

    drv_error(b, c, m, "org.freedesktop.DBus.Error.UnknownMethod", "unknown method");
}

/* ---------------- inbound ---------------- */
static void active_process(Bus *b, Conn *c)
{
    for (;;) {
        if (c->in.len == 0) return;
        Msg m; size_t need = 0;
        WireResult r = wire_parse(c->in.p, c->in.len, &m, &need);
        if (r == WIRE_NEED_MORE) return;
        if (r == WIRE_BAD) {
            audit_note(b, "malformed message from %s (pid %ld) - disconnecting",
                       c->registered ? c->uniq : "-", (long)c->pid);
            conn_shutdown(c);
            return;
        }
        uint32_t want = m.has_unix_fds ? m.unix_fds : 0;
        if (want > WC_MAX_FDS_MSG || want > (uint32_t)c->n_infds) {
            audit_note(b, "fd count mismatch from %s - disconnecting", c->uniq);
            conn_shutdown(c);
            return;
        }
        int fds[WC_MAX_FDS_MSG];
        int nfds = 0;
        for (uint32_t i = 0; i < want; i++) fds[nfds++] = c->infds[i];
        c->n_infds -= (int)want;
        if (c->n_infds > 0)
            memmove(c->infds, c->infds + want, (size_t)c->n_infds * sizeof(int));

        route(b, c, &m, c->in.p, fds, nfds);
        for (int i = 0; i < nfds; i++) close(fds[i]);
        consume(c, m.total);
        if (c->closing) return;
    }
}

static void conn_read(Bus *b, Conn *c)
{
    union { char buf[CMSG_SPACE(sizeof(int) * WC_MAX_FDS_MSG)]; struct cmsghdr align; } ctl;

    for (;;) {
        size_t room = WC_MAX_MESSAGE - c->in.len;
        if (!room) { conn_shutdown(c); return; }
        size_t chunk = room < 16384 ? room : 16384;
        if (!buf_reserve(&c->in, chunk)) { conn_shutdown(c); return; }

        struct iovec iov;
        struct msghdr mh;
        memset(&mh, 0, sizeof mh);
        iov.iov_base = c->in.p + c->in.len;
        iov.iov_len = chunk;
        mh.msg_iov = &iov;
        mh.msg_iovlen = 1;
        memset(ctl.buf, 0, sizeof ctl.buf);
        mh.msg_control = ctl.buf;
        mh.msg_controllen = sizeof ctl.buf;

        ssize_t n = recvmsg(c->fd, &mh, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
        int got = 0;
        for (struct cmsghdr *cm = CMSG_FIRSTHDR(&mh); cm; cm = CMSG_NXTHDR(&mh, cm)) {
            if (cm->cmsg_level != SOL_SOCKET || cm->cmsg_type != SCM_RIGHTS) continue;
            size_t cnt = (cm->cmsg_len - CMSG_LEN(0)) / sizeof(int);
            int *arr = (int *)CMSG_DATA(cm);
            for (size_t i = 0; i < cnt; i++) {
                got++;
                if (c->n_infds < WC_MAX_FDS_CONN) c->infds[c->n_infds++] = arr[i];
                else close(arr[i]);
            }
        }
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno != EAGAIN && errno != EWOULDBLOCK) conn_shutdown(c);
            return;
        }
        if (n == 0) { conn_shutdown(c); return; }
        c->in.len += (size_t)n;
        if ((mh.msg_flags & MSG_CTRUNC) || got > WC_MAX_FDS_MSG) {
            audit_note(b, "control message truncated from pid %ld", (long)c->pid);
            conn_shutdown(c);
            return;
        }
        if (c->state != ST_ACTIVE) auth_process(b, c);
        if (!c->closing && c->state == ST_ACTIVE && c->in.len > 0) active_process(b, c);
        if (c->closing) return;
        if ((size_t)n < chunk) return;
    }
}

/* ---------------- accept / loop ---------------- */
static void do_accept(Bus *b)
{
    for (;;) {
        int fd = accept4(b->lfd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (fd < 0) {
            if (errno == EINTR) continue;
            return;
        }
        struct ucred cr;
        socklen_t cl = sizeof cr;
        memset(&cr, 0, sizeof cr);
        if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cr, &cl) != 0 || cr.uid != b->uid) {
            audit_note(b, "refused foreign peer uid=%lu pid=%ld",
                       (unsigned long)cr.uid, (long)cr.pid);
            close(fd);
            continue;
        }
        if (b->n_conns >= WC_MAX_CONNS) {
            audit_note(b, "connection limit reached; refused pid %ld", (long)cr.pid);
            close(fd);
            continue;
        }
        Conn *c = calloc(1, sizeof *c);
        if (!c) { close(fd); continue; }
        c->bus = b; c->fd = fd; c->uid = cr.uid; c->pid = cr.pid;
        c->state = ST_NUL;
        c->next = b->conns;
        b->conns = c;
        b->n_conns++;

        struct epoll_event ev;
        memset(&ev, 0, sizeof ev);
        ev.events = EPOLLIN;
        ev.data.ptr = c;
        if (epoll_ctl(b->epfd, EPOLL_CTL_ADD, fd, &ev) != 0) { conn_shutdown(c); continue; }
        audit_note(b, "accepted pid %ld (%d/%d)", (long)cr.pid, b->n_conns, WC_MAX_CONNS);
    }
}

Bus *bus_new(Options *opt, int lfd)
{
    Bus *b = calloc(1, sizeof *b);
    if (!b) return NULL;
    b->opt = *opt;
    b->lfd = lfd;
    b->uid = getuid();
    b->pid = getpid();
    b->next_id = 1;
    b->next_serial = 1;

    uint8_t rnd[16];
    if (getrandom(rnd, sizeof rnd, 0) != (ssize_t)sizeof rnd) { free(b); return NULL; }
    static const char hexd[] = "0123456789abcdef";
    for (int i = 0; i < 16; i++) {
        b->guid[2 * i] = hexd[rnd[i] >> 4];
        b->guid[2 * i + 1] = hexd[rnd[i] & 15];
    }
    b->guid[32] = 0;
    memcpy(b->machine, b->guid, 33);

    FILE *f = fopen("/etc/machine-id", "re");
    if (f) {
        char line[64];
        if (fgets(line, sizeof line, f)) {
            size_t n = strcspn(line, "\r\n");
            if (n == 32) { memcpy(b->machine, line, 32); b->machine[32] = 0; }
        }
        fclose(f);
    }

    b->epfd = epoll_create1(EPOLL_CLOEXEC);
    if (b->epfd < 0) { free(b); return NULL; }
    struct epoll_event ev;
    memset(&ev, 0, sizeof ev);
    ev.events = EPOLLIN;
    ev.data.ptr = NULL;
    if (epoll_ctl(b->epfd, EPOLL_CTL_ADD, lfd, &ev) != 0) {
        close(b->epfd);
        free(b);
        return NULL;
    }
    return b;
}

int bus_loop(Bus *b, volatile sig_atomic_t *stop)
{
    struct epoll_event evs[32];
    while (!*stop) {
        int n = epoll_wait(b->epfd, evs, 32, 1000);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        for (int i = 0; i < n; i++) {
            if (!evs[i].data.ptr) { do_accept(b); continue; }
            Conn *c = evs[i].data.ptr;
            if (c->closing) continue;
            if (evs[i].events & EPOLLOUT) conn_flush(c);
            if (!c->closing && (evs[i].events & EPOLLIN)) conn_read(b, c);
            if (!c->closing && (evs[i].events & (EPOLLHUP | EPOLLERR))) conn_shutdown(c);
        }
        bus_reap(b);
    }
    return 0;
}

void bus_destroy(Bus *b)
{
    if (!b) return;
    Conn *c = b->conns;
    while (c) { Conn *n = c->next; conn_free(b, c); c = n; }
    for (int i = 0; i < b->n_names; i++) free(b->names[i].name);
    if (b->epfd >= 0) close(b->epfd);
    free(b);
}
