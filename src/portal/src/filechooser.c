/* SPDX-License-Identifier: MIT
 *
 * filechooser.c - org.freedesktop.portal.FileChooser (version 3).
 *
 * OpenFile / SaveFile return a Request object path immediately and run the
 * picker asynchronously.  Every client supplied string reaches the picker
 * through an execve() argv element, never through a shell.
 *
 * Picker ABI (see scripts/wc-pick):
 *   <picker> --mode open|save --chooser-file PATH --dir DIR --title TITLE
 *            [--multiple] [--directory] [--name NAME] [--filter L|g;g]...
 * The picker writes newline delimited absolute paths to --chooser-file and
 * exits 0 on selection, non-zero on cancel.
 */
#include "whitecube_portal.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

typedef struct {
    char   token[WC_TOKEN_MAX];
    char   title[WC_TITLE_MAX];
    char   folder[PATH_MAX];
    char   name[256];
    bool   multiple;
    bool   directory;
    bool   have_token;
    char   filters[WC_MAX_FILTERS][WC_FILTER_MAX];
    size_t n_filters;
} fc_opts;

/* ------------------------------------------------------------------ */
/* option parsing                                                      */
/* ------------------------------------------------------------------ */

static void resolve_folder(wc_portal *p, const char *requested, char *out,
                           size_t cap)
{
    char resolved[PATH_MAX];
    struct stat st;

    out[0] = 0;
    if (requested && requested[0]) {
        if (!wc_is_absolute_path(requested) || !wc_text_is_safe(requested)) {
            wc_warn("rejecting unsafe current_folder, falling back to $HOME");
        } else if (!realpath(requested, resolved)) {
            wc_info("current_folder '%s' unresolvable (%s), using $HOME",
                    requested, strerror(errno));
        } else if (stat(resolved, &st) != 0 || !S_ISDIR(st.st_mode) ||
                   access(resolved, R_OK | X_OK) != 0) {
            wc_info("current_folder '%s' is not a readable directory, "
                    "using $HOME", resolved);
        } else {
            if (wc_fmt(out, cap, "%s", resolved))
                return;
        }
    }
    if (!wc_fmt(out, cap, "%s", p->home))
        wc_fmt(out, cap, "/");
}

static void sanitize_name(const char *in, char *out, size_t cap)
{
    char tmp[256];
    const char *base;

    out[0] = 0;
    if (!in)
        return;
    wc_sanitize_text(tmp, sizeof tmp, in, sizeof tmp - 1);
    base = strrchr(tmp, '/');
    base = base ? base + 1 : tmp;
    if (!*base || !strcmp(base, ".") || !strcmp(base, ".."))
        return;
    if (!wc_fmt(out, cap, "%s", base))
        out[0] = 0;
}

/* filters: a(sa(us)) - label + list of (type, pattern); type 0 = glob,
 * type 1 = mime type.  Globs are forwarded to the picker, mime types are
 * ignored (a terminal picker has no mime database). */
static void parse_filters(wc_reader *r, fc_opts *o)
{
    wc_array_iter outer, inner;
    const char *sig = wc_r_variant(r);

    if (!sig || strcmp(sig, "a(sa(us))")) {
        (void)wc_r_skip(r, sig ? sig : "v");
        return;
    }
    outer = wc_r_array_begin(r, 8);
    while (wc_r_array_more(r, outer)) {
        char label[128];
        char pats[WC_FILTER_MAX];
        size_t used = 0;
        const char *name;

        pats[0] = 0;
        wc_r_struct_begin(r);
        name = wc_r_string(r);
        wc_sanitize_text(label, sizeof label, name ? name : "", 96);
        inner = wc_r_array_begin(r, 8);
        while (wc_r_array_more(r, inner)) {
            uint32_t type;
            const char *pat;
            char clean[128];

            wc_r_struct_begin(r);
            type = wc_r_u32(r);
            pat = wc_r_string(r);
            wc_r_struct_end(r);
            if (!pat || type != 0)
                continue;
            wc_sanitize_text(clean, sizeof clean, pat, sizeof clean - 1);
            if (!clean[0])
                continue;
            /* '|' and ';' are our own separators: drop them from patterns. */
            for (char *c = clean; *c; c++)
                if (*c == '|' || *c == ';')
                    *c = ',';
            if (used && used + 1 < sizeof pats)
                pats[used++] = ';';
            size_t n = strlen(clean);
            if (used + n < sizeof pats) {
                memcpy(pats + used, clean, n);
                used += n;
            }
            pats[used] = 0;
        }
        wc_r_array_end(r, inner);
        wc_r_struct_end(r);
        if (o->n_filters < WC_MAX_FILTERS && pats[0]) {
            if (!wc_fmt(o->filters[o->n_filters], WC_FILTER_MAX, "%s|%s",
                        label[0] ? label : "Files", pats))
                continue;
            o->n_filters++;
        }
    }
    wc_r_array_end(r, outer);
}

static bool parse_options(wc_portal *p, wc_reader *r, fc_opts *o, bool save)
{
    wc_array_iter it;
    char folder_req[PATH_MAX];

    folder_req[0] = 0;
    it = wc_r_array_begin(r, 8);
    while (wc_r_array_more(r, it)) {
        const char *key, *sig;
        wc_value v;
        size_t rewind;

        wc_r_struct_begin(r);
        key = wc_r_string(r);
        if (!key)
            break;
        rewind = r->pos;
        sig = wc_r_variant(r);
        if (!sig)
            break;
        r->pos = rewind;

        if (!strcmp(key, "filters") && !strcmp(sig, "a(sa(us))")) {
            parse_filters(r, o);
        } else if (!wc_value_read(r, &v)) {
            break;
        } else if (!strcmp(key, "handle_token") && !strcmp(v.sig, "s")) {
            wc_sanitize_token(v.str, 0, o->token, sizeof o->token);
            o->have_token = true;
        } else if (!strcmp(key, "multiple") && !strcmp(v.sig, "b")) {
            o->multiple = v.b;
        } else if (!strcmp(key, "directory") && !strcmp(v.sig, "b")) {
            o->directory = v.b;
        } else if (!strcmp(key, "current_folder") && !strcmp(v.sig, "ay")) {
            size_t n = v.bytes.len;
            if (n && v.bytes.data[n - 1] == 0)
                n--;                       /* protocol: NUL terminated */
            if (n >= sizeof folder_req)
                n = sizeof folder_req - 1;
            memcpy(folder_req, v.bytes.data, n);
            folder_req[n] = 0;
            if (memchr(folder_req, 0, n))
                folder_req[0] = 0;         /* embedded NUL: reject */
        } else if (save && !strcmp(key, "current_name") && !strcmp(v.sig, "s")) {
            sanitize_name(v.str, o->name, sizeof o->name);
        }
        wc_r_struct_end(r);
    }
    wc_r_array_end(r, it);
    if (!wc_r_ok(r))
        return false;
    resolve_folder(p, folder_req, o->folder, sizeof o->folder);
    return true;
}

/* ------------------------------------------------------------------ */
/* selection file                                                      */
/* ------------------------------------------------------------------ */

static void drop_chooser(wc_request *r)
{
    if (r->chooser[0]) {
        if (unlink(r->chooser) != 0 && errno != ENOENT)
            wc_warn("cannot unlink %s: %s", r->chooser, strerror(errno));
        r->chooser[0] = 0;
    }
}

static char *read_selection(wc_request *r, size_t *out_len)
{
    struct stat st;
    char *buf;
    int fd;
    size_t got = 0;

    *out_len = 0;
    if (!r->chooser[0])
        return NULL;
    fd = open(r->chooser, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) {
        if (errno != ENOENT)
            wc_warn("cannot open selection file: %s", strerror(errno));
        return NULL;
    }
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) ||
        st.st_uid != getuid() || st.st_nlink != 1 ||
        st.st_size > WC_CHOOSER_MAX_BYTES) {
        wc_warn("selection file failed integrity checks, ignoring");
        close(fd);
        return NULL;
    }
    buf = (char *)malloc((size_t)st.st_size + 1);
    if (!buf) {
        close(fd);
        return NULL;
    }
    while (got < (size_t)st.st_size) {
        ssize_t n = read(fd, buf + got, (size_t)st.st_size - got);
        if (n > 0) {
            got += (size_t)n;
            continue;
        }
        if (n < 0 && errno == EINTR)
            continue;
        break;
    }
    close(fd);
    buf[got] = 0;
    *out_len = got;
    return buf;
}

/* Resolve + type check one selected path. */
static bool validate_path(wc_request *r, const char *path, char *out,
                          size_t cap)
{
    char resolved[PATH_MAX];
    struct stat st;

    if (!wc_is_absolute_path(path) || !wc_text_is_safe(path)) {
        wc_warn("picker returned an unusable path, skipping");
        return false;
    }
    if (!realpath(path, resolved)) {
        if (r->kind != WC_REQ_SAVE)
            return false;
        /* Save targets may not exist yet: resolve the parent directory. */
        {
            char parent[PATH_MAX];
            char base[256];
            const char *slash = strrchr(path, '/');
            size_t blen;

            if (!slash)
                return false;
            blen = strlen(slash + 1);
            if (!blen || blen >= sizeof base)
                return false;
            memcpy(base, slash + 1, blen + 1);
            if (!strcmp(base, ".") || !strcmp(base, ".."))
                return false;
            if (slash == path) {
                if (!wc_fmt(parent, sizeof parent, "/"))
                    return false;
            } else {
                size_t plen = (size_t)(slash - path);
                if (plen >= sizeof parent)
                    return false;
                memcpy(parent, path, plen);
                parent[plen] = 0;
            }
            if (!realpath(parent, resolved))
                return false;
            {
                char joined[PATH_MAX];
                if (!wc_fmt(joined, sizeof joined, "%s%s%s", resolved,
                            resolved[strlen(resolved) - 1] == '/' ? "" : "/",
                            base) ||
                    !wc_fmt(resolved, sizeof resolved, "%s", joined))
                    return false;
            }
        }
        if (stat(resolved, &st) == 0 && !S_ISREG(st.st_mode))
            return false;
    } else {
        if (stat(resolved, &st) != 0)
            return false;
        if (r->kind == WC_REQ_SAVE) {
            if (!S_ISREG(st.st_mode))
                return false;
        } else if (r->directory) {
            if (!S_ISDIR(st.st_mode))
                return false;
        } else if (!S_ISREG(st.st_mode)) {
            return false;
        }
    }
    return wc_fmt(out, cap, "%s", resolved);
}

void wc_filechooser_child_done(wc_portal *p, wc_request *r, int status)
{
    char *buf = NULL;
    size_t len = 0;
    char *lines[WC_MAX_URIS];
    const char *uris[WC_MAX_URIS];
    size_t n_lines = 0, n_uris = 0, i;
    int code = 1;                       /* default: user cancelled */
    char path[PATH_MAX];
    char uri[WC_URI_MAX];

    if (WIFEXITED(status))
        wc_dbg("picker exited with %d", WEXITSTATUS(status));
    else if (WIFSIGNALED(status))
        wc_dbg("picker killed by signal %d", WTERMSIG(status));

    if (WIFEXITED(status) && WEXITSTATUS(status) == 0)
        buf = read_selection(r, &len);
    drop_chooser(r);                    /* unlink immediately after reading */

    if (buf && len) {
        char *save = NULL, *tok;
        for (tok = strtok_r(buf, "\n", &save); tok;
             tok = strtok_r(NULL, "\n", &save)) {
            if (n_lines >= WC_MAX_URIS)
                break;
            if (*tok)
                lines[n_lines++] = tok;
        }
        for (i = 0; i < n_lines && n_uris < WC_MAX_URIS; i++) {
            if (!validate_path(r, lines[i], path, sizeof path))
                continue;
            if (!wc_uri_encode(path, uri, sizeof uri))
                continue;
            uris[n_uris] = strdup(uri);
            if (!uris[n_uris])
                break;
            n_uris++;
            if (!r->multiple)
                break;
        }
        if (n_uris)
            code = 0;
    }
    free(buf);

    wc_req_respond(p, r, (uint32_t)code, n_uris ? uris : NULL, n_uris);
    for (i = 0; i < n_uris; i++)
        free((void *)uris[i]);
    wc_req_free(p, r);
}

/* ------------------------------------------------------------------ */
/* OpenFile / SaveFile                                                 */
/* ------------------------------------------------------------------ */

static void spawn_picker(wc_portal *p, wc_request *r, const fc_opts *o)
{
    char chooser[PATH_MAX];
    char *argv[16 + 2 * WC_MAX_FILTERS];
    size_t argc = 0;
    int fd;
    pid_t pid;

    fd = wc_mkstemp_runtime(p->runtime_dir, chooser, sizeof chooser);
    if (fd < 0) {
        wc_req_respond(p, r, 2, NULL, 0);
        wc_req_free(p, r);
        return;
    }
    close(fd);
    if (!wc_fmt(r->chooser, sizeof r->chooser, "%s", chooser)) {
        unlink(chooser);
        wc_req_respond(p, r, 2, NULL, 0);
        wc_req_free(p, r);
        return;
    }

    argv[argc++] = p->picker;
    argv[argc++] = "--mode";
    argv[argc++] = (r->kind == WC_REQ_SAVE) ? "save" : "open";
    argv[argc++] = "--chooser-file";
    argv[argc++] = r->chooser;
    argv[argc++] = "--dir";
    argv[argc++] = (char *)o->folder;
    argv[argc++] = "--title";
    argv[argc++] = (char *)o->title;
    if (o->multiple)
        argv[argc++] = "--multiple";
    if (o->directory)
        argv[argc++] = "--directory";
    if (o->name[0]) {
        argv[argc++] = "--name";
        argv[argc++] = (char *)o->name;
    }
    for (size_t i = 0; i < o->n_filters; i++) {
        argv[argc++] = "--filter";
        argv[argc++] = (char *)o->filters[i];
    }
    argv[argc] = NULL;

    pid = wc_spawn(argv);
    if (pid < 0) {
        wc_err("cannot spawn picker %s", p->picker);
        wc_req_respond(p, r, 2, NULL, 0);
        wc_req_free(p, r);
        return;
    }
    r->pid = pid;
    wc_info("picker pid %ld: %s (%s)%s%s", (long)pid, o->folder,
            r->kind == WC_REQ_SAVE ? "save" : "open",
            o->multiple ? " multi" : "", o->directory ? " dir" : "");
}

static void handle_open_save(wc_portal *p, const wc_msg *m, bool save)
{
    fc_opts o;
    wc_reader r;
    const char *parent, *title;
    wc_writer w;
    wc_request *req;
    char clean_title[WC_TITLE_MAX];

    memset(&o, 0, sizeof o);
    if (!m->signature || strcmp(m->signature, "ssa{sv}") != 0) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS,
                     "%s expects (ssa{sv})", save ? "SaveFile" : "OpenFile");
        return;
    }
    wc_r_init(&r, m->body, m->body_len, m->big_endian);
    parent = wc_r_string(&r);
    title = wc_r_string(&r);
    if (!wc_r_ok(&r) || !parent || !title) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "malformed arguments");
        return;
    }
    wc_sanitize_text(clean_title, sizeof clean_title, title, WC_TITLE_MAX - 1);
    if (!clean_title[0])
        wc_fmt(clean_title, sizeof clean_title, "%s",
               save ? "Save File" : "Select File");
    if (!wc_fmt(o.title, sizeof o.title, "%s", clean_title)) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "title too long");
        return;
    }
    if (!parse_options(p, &r, &o, save)) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "malformed options");
        return;
    }
    if (parent[0])
        wc_dbg("parent_window '%s' (ignored: whitecube floats the picker)",
               parent);

    if (!p->picker[0]) {
        wc_bus_error(&p->bus, m, WC_ERR_FAILED,
                     "no file picker configured (use --picker-cmd)");
        return;
    }
    if (p->active >= p->cfg.max_requests ||
        p->active >= WC_MAX_REQUESTS_HARD) {
        wc_bus_error(&p->bus, m, WC_ERR_LIMITS,
                     "too many concurrent portal requests (%u)", p->active);
        return;
    }
    req = wc_req_new(p, m, o.have_token ? o.token : NULL,
                     save ? WC_REQ_SAVE : WC_REQ_OPEN);
    if (!req) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARG,
                     "handle_token already in use or invalid sender");
        return;
    }
    req->multiple = o.multiple;
    req->directory = o.directory;

    /* Return the handle first: the client must never block on the picker. */
    wc_w_init(&w);
    wc_w_string(&w, req->path);
    wc_bus_reply(&p->bus, m, "o", &w);
    wc_w_free(&w);

    spawn_picker(p, req, &o);
}

void wc_filechooser_handle(wc_portal *p, const wc_msg *m)
{
    const char *member = m->member ? m->member : "";

    if (!strcmp(member, "OpenFile"))
        handle_open_save(p, m, false);
    else if (!strcmp(member, "SaveFile"))
        handle_open_save(p, m, true);
    else
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_METH,
                     "org.freedesktop.portal.FileChooser has no method '%s'",
                     member);
}