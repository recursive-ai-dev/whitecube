/* SPDX-License-Identifier: MIT
 *
 * request.c - org.freedesktop.portal.Request object lifecycle.
 *
 * One request == one object path == at most one picker child == exactly one
 * Response signal.  Requests are bounded, time limited, owned by a single
 * sender, and always cleaned up (temp file unlinked, child reaped).
 */
#include "whitecube_portal.h"

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

wc_request *wc_req_new(wc_portal *p, const wc_msg *call, const char *token,
                       wc_req_kind kind)
{
    char sender_elem[WC_SENDER_MAX];
    char tok[WC_TOKEN_MAX];
    char path[sizeof p->reqs[0].path];
    size_t i;
    wc_request *r = NULL;

    if (!call->sender || !*call->sender)
        return NULL;
    if (!wc_sender_element(call->sender, sender_elem, sizeof sender_elem))
        return NULL;
    wc_sanitize_token(token, call->serial, tok, sizeof tok);
    if (!wc_fmt(path, sizeof path, "%s%s/%s", WC_REQUEST_PREFIX, sender_elem, tok))
        return NULL;
    if (wc_req_by_path(p, path))
        return NULL;                       /* handle_token already in use */

    for (i = 0; i < WC_MAX_REQUESTS_HARD; i++) {
        if (!p->reqs[i].used) {
            r = &p->reqs[i];
            break;
        }
    }
    if (!r)
        return NULL;

    memset(r, 0, sizeof *r);
    r->used = true;
    r->kind = kind;
    r->pid = 0;
    if (!wc_fmt(r->path, sizeof r->path, "%s", path) ||
        !wc_fmt(r->sender, sizeof r->sender, "%s", call->sender) ||
        !wc_fmt(r->token, sizeof r->token, "%s", tok)) {
        r->used = false;
        return NULL;
    }
    r->deadline_ms = wc_now_ms() + (int64_t)p->cfg.timeout_s * 1000;
    p->active++;
    wc_dbg("request %s (kind=%d) for %s", r->path, (int)kind, r->sender);
    return r;
}

wc_request *wc_req_by_path(wc_portal *p, const char *path)
{
    size_t i;

    if (!path)
        return NULL;
    for (i = 0; i < WC_MAX_REQUESTS_HARD; i++)
        if (p->reqs[i].used && !strcmp(p->reqs[i].path, path))
            return &p->reqs[i];
    return NULL;
}

wc_request *wc_req_by_pid(wc_portal *p, pid_t pid)
{
    size_t i;

    if (pid <= 0)
        return NULL;
    for (i = 0; i < WC_MAX_REQUESTS_HARD; i++)
        if (p->reqs[i].used && p->reqs[i].pid == pid)
            return &p->reqs[i];
    return NULL;
}

static void unlink_chooser(wc_request *r)
{
    if (r->chooser[0]) {
        if (unlink(r->chooser) != 0 && errno != ENOENT)
            wc_warn("cannot unlink %s: %s", r->chooser, strerror(errno));
        r->chooser[0] = 0;
    }
}

void wc_req_free(wc_portal *p, wc_request *r)
{
    if (!r->used)
        return;
    unlink_chooser(r);
    if (r->pid > 0) {
        /* Last resort: never leave an orphan picker behind. */
        (void)kill(r->pid, SIGTERM);
        r->pid = 0;
    }
    wc_dbg("free request %s", r->path);
    memset(r, 0, sizeof *r);
    if (p->active)
        p->active--;
}

void wc_req_respond(wc_portal *p, wc_request *r, uint32_t code,
                    const char *const *uris, size_t n_uris)
{
    wc_writer w;
    wc_array_mark dict, arr;
    size_t i;

    if (!r->used || r->responded)
        return;

    wc_w_init(&w);
    wc_w_u32(&w, code);
    dict = wc_w_dict_begin(&w);
    if (n_uris && uris) {
        wc_w_dict_entry(&w, "uris", "as");
        arr = wc_w_array_begin(&w, 4);
        for (i = 0; i < n_uris; i++)
            wc_w_string(&w, uris[i]);
        wc_w_array_end(&w, arr);
    }
    wc_w_dict_end(&w, dict);

    if (wc_w_ok(&w)) {
        wc_bus_signal(&p->bus, r->path, "org.freedesktop.portal.Request",
                      "Response", "ua{sv}", &w, r->sender);
        r->responded = true;
        wc_info("Response(%u) on %s [%zu uri(s)]", (unsigned)code, r->path,
                n_uris);
    } else {
        wc_err("cannot marshal Response for %s", r->path);
        r->responded = true;
    }
    wc_w_free(&w);
}

void wc_req_cancel(wc_portal *p, wc_request *r, uint32_t code)
{
    if (!r->used)
        return;
    if (r->pid > 0 && !r->closing) {
        wc_dbg("SIGTERM picker pid %ld", (long)r->pid);
        (void)kill(r->pid, SIGTERM);
        r->closing = true;
        r->deadline_ms = wc_now_ms() + WC_KILL_GRACE_MS;
    }
    wc_req_respond(p, r, code, NULL, 0);
    if (r->pid <= 0)
        wc_req_free(p, r);
}

void wc_req_reap(wc_portal *p)
{
    int status;
    pid_t pid;

    for (;;) {
        pid = waitpid(-1, &status, WNOHANG);
        if (pid > 0) {
            wc_request *r = wc_req_by_pid(p, pid);
            if (!r)
                continue;              /* detached child (browser) */
            r->pid = 0;
            if (!r->responded)
                wc_filechooser_child_done(p, r, status);
            else
                wc_req_free(p, r);
            continue;
        }
        if (pid < 0 && errno == EINTR)
            continue;
        break;                          /* 0 or ECHILD */
    }
}

void wc_req_expire(wc_portal *p, int64_t now)
{
    size_t i;

    for (i = 0; i < WC_MAX_REQUESTS_HARD; i++) {
        wc_request *r = &p->reqs[i];
        if (!r->used || r->deadline_ms <= 0 || now < r->deadline_ms)
            continue;
        if (!r->responded) {
            wc_warn("request %s timed out", r->path);
            wc_req_cancel(p, r, 2);
        } else if (r->pid > 0) {
            wc_warn("picker pid %ld ignored SIGTERM, sending SIGKILL",
                    (long)r->pid);
            (void)kill(r->pid, SIGKILL);
            r->pid = 0;
            wc_req_free(p, r);
        } else {
            wc_req_free(p, r);
        }
    }
}

void wc_req_sender_vanished(wc_portal *p, const char *name)
{
    size_t i;

    if (!name || !*name)
        return;
    for (i = 0; i < WC_MAX_REQUESTS_HARD; i++) {
        wc_request *r = &p->reqs[i];
        if (r->used && !strcmp(r->sender, name)) {
            wc_info("caller %s disconnected, cancelling %s", name, r->path);
            wc_req_cancel(p, r, 2);
        }
    }
}

int64_t wc_req_next_deadline(wc_portal *p)
{
    int64_t best = 0;
    size_t i;

    for (i = 0; i < WC_MAX_REQUESTS_HARD; i++) {
        wc_request *r = &p->reqs[i];
        if (!r->used || r->deadline_ms <= 0)
            continue;
        if (!best || r->deadline_ms < best)
            best = r->deadline_ms;
    }
    return best;
}

void wc_req_shutdown(wc_portal *p)
{
    size_t i;

    for (i = 0; i < WC_MAX_REQUESTS_HARD; i++) {
        wc_request *r = &p->reqs[i];
        if (!r->used)
            continue;
        if (r->pid > 0)
            (void)kill(r->pid, SIGTERM);
        wc_req_free(p, r);
    }
    wc_req_reap(p);
}

/* ------------------------------------------------------------------ */
/* org.freedesktop.portal.Request.Close                                */
/* ------------------------------------------------------------------ */

void wc_request_handle_close(wc_portal *p, const wc_msg *m)
{
    wc_request *r = wc_req_by_path(p, m->path);

    if (!r) {
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_OBJ, "no such request: %s",
                     m->path);
        return;
    }
    /* Only the client that created the request may close it. */
    if (!m->sender || strcmp(m->sender, r->sender) != 0) {
        wc_bus_error(&p->bus, m, WC_ERR_NOT_ALLOWED,
                     "request belongs to another client");
        return;
    }
    wc_bus_reply_empty(&p->bus, m);
    if (r->responded)
        return;                        /* Close is idempotent */
    wc_req_cancel(p, r, 2);            /* 2 = ended by other means (Close) */
}