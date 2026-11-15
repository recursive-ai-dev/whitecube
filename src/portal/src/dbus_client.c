/* SPDX-License-Identifier: MIT
 *
 * dbus_client.c - session bus transport: unix socket, SASL EXTERNAL, message
 * framing, name claiming, replies and signals.  No libdbus, no sd-bus.
 *
 * Only what a portal service needs is implemented:
 *   - unix:path= / unix:abstract= addresses (multiple, ';'-separated)
 *   - AUTH EXTERNAL (with ANONYMOUS fallback for permissive buses)
 *   - Hello / RequestName / AddMatch
 *   - method calls in, method returns + errors + unicast signals out
 * File descriptor passing is deliberately NOT negotiated.
 */
#include "whitecube_portal.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define AUTH_TIMEOUT_MS 5000
#define CALL_TIMEOUT_MS 10000
#define OUT_QUEUE_MAX   (4u * 1024u * 1024u)
#define READ_CHUNK      (16u * 1024u)

/* ------------------------------------------------------------------ */
/* low level helpers                                                   */
/* ------------------------------------------------------------------ */

static uint32_t rd32(const uint8_t *p, bool big)
{
    if (big)
        return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
               ((uint32_t)p[2] << 8) | (uint32_t)p[3];
    return ((uint32_t)p[3] << 24) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[1] << 8) | (uint32_t)p[0];
}

static size_t align8(size_t n)
{
    return (n + 7u) & ~(size_t)7u;
}

static bool wait_fd(int fd, short events, int timeout_ms)
{
    struct pollfd pfd;
    int rc;

    pfd.fd = fd;
    pfd.events = events;
    pfd.revents = 0;
    do {
        rc = poll(&pfd, 1, timeout_ms);
    } while (rc < 0 && errno == EINTR);
    return rc > 0 && (pfd.revents & (events | POLLERR | POLLHUP)) != 0;
}

static bool send_all(int fd, const void *buf, size_t len, int timeout_ms)
{
    const uint8_t *p = (const uint8_t *)buf;
    size_t off = 0;

    while (off < len) {
        ssize_t n = send(fd, p + off, len - off, MSG_NOSIGNAL);
        if (n > 0) {
            off += (size_t)n;
            continue;
        }
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (!wait_fd(fd, POLLOUT, timeout_ms))
                return false;
            continue;
        }
        return false;
    }
    return true;
}

/* Auth phase only: read one CRLF terminated line, one byte at a time so we
 * never consume bytes belonging to the binary protocol after BEGIN. */
static bool auth_read_line(int fd, char *buf, size_t cap, int timeout_ms)
{
    size_t n = 0;
    int64_t deadline = wc_now_ms() + timeout_ms;

    while (n + 1 < cap) {
        char c;
        ssize_t rc = recv(fd, &c, 1, 0);
        if (rc == 1) {
            if (c == '\n') {
                if (n && buf[n - 1] == '\r')
                    n--;
                buf[n] = 0;
                return true;
            }
            buf[n++] = c;
            continue;
        }
        if (rc == 0)
            return false;
        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            int64_t left = deadline - wc_now_ms();
            if (left <= 0 || !wait_fd(fd, POLLIN, (int)left))
                return false;
            continue;
        }
        return false;
    }
    return false;
}

/* ------------------------------------------------------------------ */
/* address parsing + connect                                           */
/* ------------------------------------------------------------------ */

static int unix_open(const char *entry)
{
    struct sockaddr_un sa;
    socklen_t salen;
    const char *p;
    char path[sizeof(sa.sun_path)];
    bool abstract = false;
    int fd;

    if (strncmp(entry, "unix:", 5) != 0)
        return -1;

    memset(&sa, 0, sizeof sa);
    sa.sun_family = AF_UNIX;
    path[0] = 0;

    for (p = entry + 5; *p; ) {
        const char *eq = strchr(p, '=');
        const char *end = strchr(p, ',');
        size_t klen, vlen;
        if (!end)
            end = p + strlen(p);
        if (!eq || eq > end)
            return -1;
        klen = (size_t)(eq - p);
        vlen = (size_t)(end - eq - 1);
        if (klen == 4 && !strncmp(p, "path", 4)) {
            if (vlen == 0 || vlen >= sizeof path)
                return -1;
            memcpy(path, eq + 1, vlen);
            path[vlen] = 0;
            abstract = false;
        } else if (klen == 8 && !strncmp(p, "abstract", 8)) {
            if (vlen == 0 || vlen >= sizeof path)
                return -1;
            memcpy(path, eq + 1, vlen);
            path[vlen] = 0;
            abstract = true;
        }
        if (!*end)
            break;
        p = end + 1;
    }
    if (!path[0])
        return -1;

    if (abstract) {
        sa.sun_path[0] = 0;
        memcpy(sa.sun_path + 1, path, strlen(path));
        salen = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 +
                            strlen(path));
    } else {
        if (strlen(path) >= sizeof sa.sun_path)
            return -1;
        memcpy(sa.sun_path, path, strlen(path) + 1);
        salen = (socklen_t)sizeof sa;
    }

    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0)
        return -1;

    if (connect(fd, (struct sockaddr *)&sa, salen) < 0) {
        int err = errno, soerr = 0;
        socklen_t sl = sizeof soerr;
        if (err != EINPROGRESS && err != EAGAIN) {
            close(fd);
            return -1;
        }
        if (!wait_fd(fd, POLLOUT, AUTH_TIMEOUT_MS) ||
            getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl) < 0 ||
            soerr != 0) {
            close(fd);
            return -1;
        }
    }
    wc_dbg("connected to %s", entry);
    return fd;
}

static void hex_encode(const char *in, char *out, size_t cap)
{
    static const char hex[] = "0123456789abcdef";
    size_t i, n = 0;

    for (i = 0; in[i] && n + 2 < cap; i++) {
        out[n++] = hex[(unsigned char)in[i] >> 4];
        out[n++] = hex[(unsigned char)in[i] & 0xf];
    }
    out[n] = 0;
}

static const char ANON_AUTH[] = "AUTH ANONYMOUS 776869746563756265\r\n";

static bool authenticate(int fd)
{
    char line[512];
    char uid[32];
    char hexuid[2 * sizeof uid + 1];
    char nul = 0;

    if (!send_all(fd, &nul, 1, AUTH_TIMEOUT_MS))
        return false;

    if (!wc_fmt(uid, sizeof uid, "%u", (unsigned)getuid()))
        return false;
    hex_encode(uid, hexuid, sizeof hexuid);

    if (!wc_fmt(line, sizeof line, "AUTH EXTERNAL %s\r\n", hexuid))
        return false;
    if (!send_all(fd, line, strlen(line), AUTH_TIMEOUT_MS))
        return false;
    if (!auth_read_line(fd, line, sizeof line, AUTH_TIMEOUT_MS))
        return false;

    if (strncmp(line, "REJECTED", 8) == 0) {
        /* Some minimal buses (lab setups) only offer ANONYMOUS. */
        wc_dbg("EXTERNAL rejected (%s), trying ANONYMOUS", line);
        if (!send_all(fd, ANON_AUTH, sizeof ANON_AUTH - 1, AUTH_TIMEOUT_MS))
            return false;
        if (!auth_read_line(fd, line, sizeof line, AUTH_TIMEOUT_MS))
            return false;
    }
    if (strncmp(line, "OK", 2) != 0) {
        wc_err("bus authentication failed: %s", line);
        return false;
    }
    if (!send_all(fd, "BEGIN\r\n", 7, AUTH_TIMEOUT_MS))
        return false;
    return true;
}

bool wc_bus_connect(wc_bus *b, const char *address)
{
    char buf[2048];
    char fallback[PATH_MAX];
    const char *list = address;
    char *save = NULL, *tok;
    int fd = -1;

    memset(b, 0, sizeof *b);
    b->fd = -1;
    b->next_serial = 1;

    if (!list || !*list)
        list = getenv("DBUS_SESSION_BUS_ADDRESS");
    if ((!list || !*list)) {
        char rt[PATH_MAX];
        if (wc_runtime_dir(rt, sizeof rt) &&
            wc_fmt(fallback, sizeof fallback, "unix:path=%s/bus", rt)) {
            wc_dbg("DBUS_SESSION_BUS_ADDRESS unset, trying %s", fallback);
            list = fallback;
        }
    }
    if (!list || !*list) {
        wc_err("no session bus address (set DBUS_SESSION_BUS_ADDRESS)");
        return false;
    }
    if (!wc_fmt(buf, sizeof buf, "%s", list))
        return false;

    for (tok = strtok_r(buf, ";", &save); tok; tok = strtok_r(NULL, ";", &save)) {
        fd = unix_open(tok);
        if (fd >= 0)
            break;
    }
    if (fd < 0) {
        wc_err("cannot connect to session bus: %s", list);
        return false;
    }
    if (!authenticate(fd)) {
        close(fd);
        return false;
    }
    b->fd = fd;
    return true;
}

void wc_bus_close(wc_bus *b)
{
    if (b->fd >= 0)
        close(b->fd);
    b->fd = -1;
    free(b->in);
    free(b->out);
    b->in = b->out = NULL;
    b->in_len = b->in_cap = b->out_len = b->out_cap = b->out_sent = 0;
    b->dead = true;
}

/* ------------------------------------------------------------------ */
/* output queue                                                        */
/* ------------------------------------------------------------------ */

static bool out_append(wc_bus *b, const uint8_t *data, size_t len)
{
    size_t need;

    if (b->dead)
        return false;
    /* compact first */
    if (b->out_sent) {
        memmove(b->out, b->out + b->out_sent, b->out_len - b->out_sent);
        b->out_len -= b->out_sent;
        b->out_sent = 0;
    }
    if (len > OUT_QUEUE_MAX || b->out_len > OUT_QUEUE_MAX - len) {
        wc_err("output queue overflow, dropping bus connection");
        b->dead = true;
        return false;
    }
    need = b->out_len + len;
    if (need > b->out_cap) {
        size_t cap = b->out_cap ? b->out_cap : 4096;
        uint8_t *p;
        while (cap < need)
            cap *= 2;
        p = (uint8_t *)realloc(b->out, cap);
        if (!p) {
            b->dead = true;
            return false;
        }
        b->out = p;
        b->out_cap = cap;
    }
    memcpy(b->out + b->out_len, data, len);
    b->out_len += len;
    return true;
}

bool wc_bus_flush(wc_bus *b)
{
    while (b->out_sent < b->out_len) {
        ssize_t n = send(b->fd, b->out + b->out_sent,
                         b->out_len - b->out_sent, MSG_NOSIGNAL);
        if (n > 0) {
            b->out_sent += (size_t)n;
            continue;
        }
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            return true;
        wc_err("bus write failed: %s", strerror(errno));
        b->dead = true;
        return false;
    }
    b->out_len = b->out_sent = 0;
    return true;
}

/* ------------------------------------------------------------------ */
/* message construction                                                */
/* ------------------------------------------------------------------ */

static void add_field(wc_writer *w, uint8_t code, const char *sig,
                      const char *sval, uint32_t uval)
{
    wc_w_struct_begin(w);
    wc_w_byte(w, code);
    wc_w_variant(w, sig);
    if (sig[0] == 'u')
        wc_w_u32(w, uval);
    else if (sig[0] == 'g')
        wc_w_signature(w, sval);
    else
        wc_w_string(w, sval);
}

uint32_t wc_bus_send(wc_bus *b, const wc_hdr *h, const uint8_t *body, size_t len)
{
    wc_writer w;
    wc_array_mark fields;
    uint32_t serial;

    if (b->dead)
        return 0;
    if (len > WC_MAX_MESSAGE)
        return 0;

    serial = b->next_serial++;
    if (!b->next_serial)
        b->next_serial = 1;

    wc_w_init(&w);
    wc_w_byte(&w, 'l');
    wc_w_byte(&w, h->type);
    wc_w_byte(&w, h->flags);
    wc_w_byte(&w, 1);
    wc_w_u32(&w, (uint32_t)len);
    wc_w_u32(&w, serial);

    fields = wc_w_array_begin(&w, 8);
    if (h->path)
        add_field(&w, 1, "o", h->path, 0);
    if (h->interface)
        add_field(&w, 2, "s", h->interface, 0);
    if (h->member)
        add_field(&w, 3, "s", h->member, 0);
    if (h->error_name)
        add_field(&w, 4, "s", h->error_name, 0);
    if (h->reply_serial)
        add_field(&w, 5, "u", NULL, h->reply_serial);
    if (h->destination)
        add_field(&w, 6, "s", h->destination, 0);
    if (h->signature && *h->signature)
        add_field(&w, 8, "g", h->signature, 0);
    wc_w_array_end(&w, fields);
    wc_w_pad(&w, 8);

    if (!wc_w_ok(&w)) {
        wc_w_free(&w);
        return 0;
    }
    if (!out_append(b, w.buf, w.len)) {
        wc_w_free(&w);
        return 0;
    }
    if (len && (!body || !out_append(b, body, len))) {
        wc_w_free(&w);
        return 0;
    }
    wc_w_free(&w);
    return serial;
}

void wc_bus_reply(wc_bus *b, const wc_msg *call, const char *sig,
                  const wc_writer *body)
{
    wc_hdr h;

    if (call->flags & 1)      /* NO_REPLY_EXPECTED */
        return;
    memset(&h, 0, sizeof h);
    h.type = WC_MSG_METHOD_RETURN;
    h.reply_serial = call->serial;
    h.destination = call->sender;
    h.signature = sig;
    wc_bus_send(b, &h, body ? body->buf : NULL, body ? body->len : 0);
}

void wc_bus_reply_empty(wc_bus *b, const wc_msg *call)
{
    wc_bus_reply(b, call, NULL, NULL);
}

void wc_bus_error(wc_bus *b, const wc_msg *call, const char *name,
                  const char *fmt, ...)
{
    char text[512];
    va_list ap;
    wc_writer w;
    wc_hdr h;

    va_start(ap, fmt);
    vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);

    if (call->flags & 1)
        return;

    wc_w_init(&w);
    wc_w_string(&w, text);
    memset(&h, 0, sizeof h);
    h.type = WC_MSG_ERROR;
    h.reply_serial = call->serial;
    h.destination = call->sender;
    h.error_name = name;
    h.signature = "s";
    wc_bus_send(b, &h, w.buf, w.len);
    wc_w_free(&w);
    wc_dbg("error to %s: %s: %s", call->sender ? call->sender : "?", name, text);
}

void wc_bus_signal(wc_bus *b, const char *path, const char *iface,
                   const char *member, const char *sig, const wc_writer *body,
                   const char *destination)
{
    wc_hdr h;

    memset(&h, 0, sizeof h);
    h.type = WC_MSG_SIGNAL;
    h.flags = 1;              /* NO_REPLY_EXPECTED */
    h.path = path;
    h.interface = iface;
    h.member = member;
    h.destination = destination;   /* unicast: never leak selections */
    h.signature = sig;
    wc_bus_send(b, &h, body ? body->buf : NULL, body ? body->len : 0);
}

/* ------------------------------------------------------------------ */
/* input                                                               */
/* ------------------------------------------------------------------ */

int wc_bus_recv(wc_bus *b)
{
    uint8_t chunk[READ_CHUNK];
    ssize_t n;
    bool got = false;

    if (b->dead)
        return -1;
    for (;;) {
        n = recv(b->fd, chunk, sizeof chunk, 0);
        if (n > 0) {
            size_t need = b->in_len + (size_t)n;
            if (need > WC_MAX_MESSAGE * 4) {
                wc_err("input buffer overflow");
                b->dead = true;
                return -1;
            }
            if (need > b->in_cap) {
                size_t cap = b->in_cap ? b->in_cap : 8192;
                uint8_t *p;
                while (cap < need)
                    cap *= 2;
                p = (uint8_t *)realloc(b->in, cap);
                if (!p) {
                    b->dead = true;
                    return -1;
                }
                b->in = p;
                b->in_cap = cap;
            }
            memcpy(b->in + b->in_len, chunk, (size_t)n);
            b->in_len += (size_t)n;
            got = true;
            if ((size_t)n < sizeof chunk)
                break;
            continue;
        }
        if (n == 0) {
            wc_info("session bus closed the connection");
            b->dead = true;
            return got ? 1 : -1;
        }
        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK)
            break;
        wc_err("bus read failed: %s", strerror(errno));
        b->dead = true;
        return -1;
    }
    return got ? 1 : 0;
}

void wc_msg_clear(wc_msg *m)
{
    free(m->raw);
    memset(m, 0, sizeof *m);
}

static bool parse_header(wc_msg *m)
{
    wc_reader r;
    wc_array_iter it;
    uint32_t fields_len = rd32(m->raw + 12, m->big_endian);
    size_t header_len = align8(16u + (size_t)fields_len);

    if (header_len > m->raw_len)
        return false;

    wc_r_init(&r, m->raw, header_len, m->big_endian);
    r.pos = 12;
    it = wc_r_array_begin(&r, 8);
    while (wc_r_array_more(&r, it)) {
        uint8_t code;
        const char *sig;

        wc_r_struct_begin(&r);
        code = wc_r_byte(&r);
        sig = wc_r_variant(&r);
        if (!sig)
            return false;
        switch (code) {
        case 1:
            if (strcmp(sig, "o")) return false;
            m->path = wc_r_string(&r);
            break;
        case 2:
            if (strcmp(sig, "s")) return false;
            m->interface = wc_r_string(&r);
            break;
        case 3:
            if (strcmp(sig, "s")) return false;
            m->member = wc_r_string(&r);
            break;
        case 4:
            if (strcmp(sig, "s")) return false;
            m->error_name = wc_r_string(&r);
            break;
        case 5:
            if (strcmp(sig, "u")) return false;
            m->reply_serial = wc_r_u32(&r);
            break;
        case 6:
            if (strcmp(sig, "s")) return false;
            m->destination = wc_r_string(&r);
            break;
        case 7:
            if (strcmp(sig, "s")) return false;
            m->sender = wc_r_string(&r);
            break;
        case 8:
            if (strcmp(sig, "g")) return false;
            m->signature = wc_r_signature(&r);
            break;
        case 9:
            if (strcmp(sig, "u")) return false;
            if (wc_r_u32(&r) != 0)      /* fd passing never negotiated */
                return false;
            break;
        default:
            if (!wc_r_skip(&r, sig))
                return false;
            break;
        }
        wc_r_struct_end(&r);
        if (!wc_r_ok(&r))
            return false;
    }
    wc_r_array_end(&r, it);
    if (!wc_r_ok(&r))
        return false;

    m->body = m->raw + header_len;
    return true;
}

int wc_bus_next(wc_bus *b, wc_msg *out)
{
    uint32_t body_len, fields_len, serial;
    size_t header_len, total;
    uint8_t *raw;
    wc_msg m;

    memset(out, 0, sizeof *out);
    if (b->in_len < 16)
        return 0;

    if (b->in[0] != 'l' && b->in[0] != 'B') {
        wc_err("bad endianness byte from bus");
        return -1;
    }
    if (b->in[3] != 1) {
        wc_err("unsupported protocol version %u", b->in[3]);
        return -1;
    }
    body_len = rd32(b->in + 4, b->in[0] == 'B');
    serial = rd32(b->in + 8, b->in[0] == 'B');
    fields_len = rd32(b->in + 12, b->in[0] == 'B');
    if (body_len > WC_MAX_MESSAGE || fields_len > WC_MAX_MESSAGE) {
        wc_err("oversized message rejected (body=%u fields=%u)",
               body_len, fields_len);
        return -1;
    }
    header_len = align8(16u + (size_t)fields_len);
    total = header_len + body_len;
    if (total > WC_MAX_MESSAGE)
        return -1;
    if (b->in_len < total)
        return 0;

    raw = (uint8_t *)malloc(total ? total : 1);
    if (!raw)
        return -1;
    memcpy(raw, b->in, total);

    memset(&m, 0, sizeof m);
    m.raw = raw;
    m.raw_len = total;
    m.big_endian = (raw[0] == 'B');
    m.type = raw[1];
    m.flags = raw[2];
    m.serial = serial;
    m.body_len = body_len;

    if (!parse_header(&m) || m.type < 1 || m.type > 4) {
        wc_err("malformed message header dropped");
        free(raw);
        /* consume it so we do not loop forever */
        memmove(b->in, b->in + total, b->in_len - total);
        b->in_len -= total;
        return -1;
    }

    memmove(b->in, b->in + total, b->in_len - total);
    b->in_len -= total;
    *out = m;
    return 1;
}

/* ------------------------------------------------------------------ */
/* synchronous calls (startup only)                                    */
/* ------------------------------------------------------------------ */

static bool call_sync(wc_bus *b, const wc_hdr *h, const uint8_t *body,
                      size_t len, wc_msg *reply)
{
    uint32_t serial;
    int64_t deadline = wc_now_ms() + CALL_TIMEOUT_MS;

    serial = wc_bus_send(b, h, body, len);
    if (!serial)
        return false;

    /* blocking flush */
    while (b->out_sent < b->out_len) {
        if (!send_all(b->fd, b->out + b->out_sent, b->out_len - b->out_sent,
                      CALL_TIMEOUT_MS)) {
            b->dead = true;
            return false;
        }
        b->out_sent = b->out_len;
    }
    b->out_len = b->out_sent = 0;

    for (;;) {
        int rc = wc_bus_next(b, reply);
        if (rc == 1) {
            if ((reply->type == WC_MSG_METHOD_RETURN ||
                 reply->type == WC_MSG_ERROR) &&
                reply->reply_serial == serial)
                return true;
            wc_msg_clear(reply);
            continue;
        }
        if (rc < 0)
            return false;
        if (wc_now_ms() > deadline) {
            wc_err("timeout waiting for %s.%s", h->interface ? h->interface : "",
                   h->member ? h->member : "");
            return false;
        }
        if (!wait_fd(b->fd, POLLIN, 200))
            continue;
        if (wc_bus_recv(b) < 0)
            return false;
    }
}

bool wc_bus_hello(wc_bus *b)
{
    wc_hdr h;
    wc_msg reply;
    wc_reader r;
    const char *name;

    memset(&h, 0, sizeof h);
    h.type = WC_MSG_METHOD_CALL;
    h.path = "/org/freedesktop/DBus";
    h.interface = "org.freedesktop.DBus";
    h.member = "Hello";
    h.destination = "org.freedesktop.DBus";

    if (!call_sync(b, &h, NULL, 0, &reply))
        return false;
    if (reply.type != WC_MSG_METHOD_RETURN) {
        wc_err("Hello failed: %s", reply.error_name ? reply.error_name : "?");
        wc_msg_clear(&reply);
        return false;
    }
    wc_r_init(&r, reply.body, reply.body_len, reply.big_endian);
    name = wc_r_string(&r);
    if (!name || !wc_fmt(b->unique_name, sizeof b->unique_name, "%s", name)) {
        wc_msg_clear(&reply);
        return false;
    }
    wc_msg_clear(&reply);
    wc_dbg("unique name %s", b->unique_name);
    return true;
}

bool wc_bus_request_name(wc_bus *b, const char *name, bool replace)
{
    wc_hdr h;
    wc_msg reply;
    wc_writer w;
    wc_reader r;
    uint32_t rc;
    bool ok = false;

    wc_w_init(&w);
    wc_w_string(&w, name);
    /* DO_NOT_QUEUE(4) | REPLACE_EXISTING(2) */
    wc_w_u32(&w, replace ? 6u : 4u);

    memset(&h, 0, sizeof h);
    h.type = WC_MSG_METHOD_CALL;
    h.path = "/org/freedesktop/DBus";
    h.interface = "org.freedesktop.DBus";
    h.member = "RequestName";
    h.destination = "org.freedesktop.DBus";
    h.signature = "su";

    if (!call_sync(b, &h, w.buf, w.len, &reply)) {
        wc_w_free(&w);
        return false;
    }
    wc_w_free(&w);
    if (reply.type == WC_MSG_METHOD_RETURN) {
        wc_r_init(&r, reply.body, reply.body_len, reply.big_endian);
        rc = wc_r_u32(&r);
        ok = wc_r_ok(&r) && rc == 1;   /* PRIMARY_OWNER */
        if (!ok)
            wc_err("RequestName(%s) returned %u (already owned?)", name, rc);
    } else {
        wc_err("RequestName(%s) failed: %s", name,
               reply.error_name ? reply.error_name : "?");
    }
    wc_msg_clear(&reply);
    return ok;
}

bool wc_bus_add_match(wc_bus *b, const char *rule)
{
    wc_hdr h;
    wc_msg reply;
    wc_writer w;
    bool ok;

    wc_w_init(&w);
    wc_w_string(&w, rule);
    memset(&h, 0, sizeof h);
    h.type = WC_MSG_METHOD_CALL;
    h.path = "/org/freedesktop/DBus";
    h.interface = "org.freedesktop.DBus";
    h.member = "AddMatch";
    h.destination = "org.freedesktop.DBus";
    h.signature = "s";
    ok = call_sync(b, &h, w.buf, w.len, &reply);
    wc_w_free(&w);
    if (ok) {
        ok = reply.type == WC_MSG_METHOD_RETURN;
        wc_msg_clear(&reply);
    }
    return ok;
}