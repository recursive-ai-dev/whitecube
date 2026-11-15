/* SPDX-License-Identifier: MIT
 *
 * uri.c - org.freedesktop.portal.OpenURI (version 3, minimal).
 *
 * Only http:// and https:// are allowed.  Everything else (file://, javascript:,
 * data:, custom handlers, ...) is rejected with
 * org.freedesktop.portal.Error.NotAllowed.  The browser is started with
 * execve() and a two element argv - no shell, no string interpolation.
 */
#include "whitecube_portal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define URI_MAX_LEN 2048

static const char *const browser_candidates[] = {
    "whitecube-browser", "firefox", "firefox-esr", "chromium", "chromium-browser",
    "google-chrome", "qutebrowser", "epiphany", "falkon", "luakit", "surf",
    "netsurf-gtk", NULL
};

static bool resolve_browser(wc_portal *p)
{
    const char *env;
    size_t i;

    if (p->browser[0])
        return true;
    if (p->cfg.browser_cmd && *p->cfg.browser_cmd) {
        if (wc_find_exec(p->cfg.browser_cmd, p->browser, sizeof p->browser))
            return true;
        wc_err("configured browser '%s' is not executable", p->cfg.browser_cmd);
        return false;
    }
    env = getenv("WC_PORTAL_BROWSER");
    if (env && *env && wc_find_exec(env, p->browser, sizeof p->browser))
        return true;
    for (i = 0; browser_candidates[i]; i++)
        if (wc_find_exec(browser_candidates[i], p->browser, sizeof p->browser))
            return true;
    return false;
}

/* Strict scheme allowlist + structural sanity checks. */
static bool uri_allowed(const char *uri)
{
    size_t n, i;
    const char *authority;

    if (!uri || !*uri)
        return false;
    n = strlen(uri);
    if (n > URI_MAX_LEN)
        return false;
    for (i = 0; i < n; i++) {
        unsigned char c = (unsigned char)uri[i];
        if (c <= 0x20 || c == 0x7f || c == '\\' || c == '"' || c == '<' ||
            c == '>' || c == '`')
            return false;
    }
    if (!strncmp(uri, "https://", 8))
        authority = uri + 8;
    else if (!strncmp(uri, "http://", 7))
        authority = uri + 7;
    else
        return false;

    /* authority must be non-empty and must not carry userinfo */
    {
        size_t alen = strcspn(authority, "/?#");
        if (alen == 0)
            return false;
        if (memchr(authority, '@', alen))
            return false;
        if (authority[0] == '/' || authority[0] == '?')
            return false;
    }
    return true;
}

static void handle_open_uri(wc_portal *p, const wc_msg *m)
{
    wc_reader r;
    wc_array_iter it;
    const char *parent, *uri;
    wc_request *req;
    wc_writer w;
    char *argv[3];
    pid_t pid;

    if (!m->signature || strcmp(m->signature, "ssa{sv}") != 0) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "OpenURI expects (ssa{sv})");
        return;
    }
    wc_r_init(&r, m->body, m->body_len, m->big_endian);
    parent = wc_r_string(&r);
    uri = wc_r_string(&r);
    if (!wc_r_ok(&r) || !parent || !uri) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "malformed arguments");
        return;
    }
    it = wc_r_array_begin(&r, 8);
    while (wc_r_array_more(&r, it)) {
        const char *key;
        wc_value v;
        wc_r_struct_begin(&r);
        key = wc_r_string(&r);
        if (!key || !wc_value_read(&r, &v))
            break;
        wc_r_struct_end(&r);
    }
    wc_r_array_end(&r, it);
    if (!wc_r_ok(&r)) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "malformed options");
        return;
    }

    if (!uri_allowed(uri)) {
        wc_warn("OpenURI rejected (scheme allowlist: http, https)");
        wc_bus_error(&p->bus, m, WC_ERR_NOT_ALLOWED,
                     "only http:// and https:// URIs may be opened");
        return;
    }
    if (!resolve_browser(p)) {
        wc_bus_error(&p->bus, m, WC_ERR_FAILED,
                     "no browser configured (use --browser-cmd)");
        return;
    }
    if (p->active >= p->cfg.max_requests) {
        wc_bus_error(&p->bus, m, WC_ERR_LIMITS, "too many concurrent requests");
        return;
    }
    req = wc_req_new(p, m, NULL, WC_REQ_URI);
    if (!req) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARG, "cannot create request");
        return;
    }

    argv[0] = p->browser;
    argv[1] = (char *)uri;
    argv[2] = NULL;
    pid = wc_spawn(argv);
    if (pid < 0) {
        wc_req_free(p, req);
        wc_bus_error(&p->bus, m, WC_ERR_FAILED, "cannot launch browser");
        return;
    }
    wc_info("opened URI with %s (pid %ld)", p->browser, (long)pid);

    wc_w_init(&w);
    wc_w_string(&w, req->path);
    wc_bus_reply(&p->bus, m, "o", &w);
    wc_w_free(&w);

    /* The browser is long lived: respond now and do not hold a request slot.
     * Its exit status is reaped generically by wc_req_reap(). */
    wc_req_respond(p, req, 0, NULL, 0);
    wc_req_free(p, req);
}

void wc_uri_handle(wc_portal *p, const wc_msg *m)
{
    const char *member = m->member ? m->member : "";

    if (!strcmp(member, "OpenURI")) {
        handle_open_uri(p, m);
        return;
    }
    if (!strcmp(member, "OpenFile") || !strcmp(member, "OpenDirectory")) {
        wc_bus_error(&p->bus, m, WC_ERR_NOT_ALLOWED,
                     "%s is not implemented by whitecube-portal", member);
        return;
    }
    wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_METH,
                 "org.freedesktop.portal.OpenURI has no method '%s'", member);
}