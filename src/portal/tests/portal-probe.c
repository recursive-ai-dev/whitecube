/* SPDX-License-Identifier: MIT
 *
 * portal-probe.c - reference portal client used by the test suite.
 *
 * It behaves the way GTK/libportal do: connect, subscribe to
 * org.freedesktop.portal.Request.Response *before* calling OpenFile, keep the
 * connection open, print the handle, then print the Response.  `gdbus call`
 * cannot do this because it disconnects as soon as the method returns.
 *
 * Built from the same zero-dependency codec as the daemon, so it doubles as a
 * round-trip test of the wire implementation in the other direction.
 */
#include "whitecube_portal.h"

#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static wc_bus bus;

static void die(const char *what)
{
    fprintf(stderr, "probe: %s\n", what);
    exit(3);
}

static bool pump(int timeout_ms)
{
    struct pollfd pfd;
    int rc;

    if (!wc_bus_flush(&bus))
        return false;
    pfd.fd = bus.fd;
    pfd.events = POLLIN;
    pfd.revents = 0;
    rc = poll(&pfd, 1, timeout_ms);
    if (rc < 0)
        return errno == EINTR;
    if (rc == 0)
        return true;
    if (wc_bus_recv(&bus) < 0)
        return false;
    return true;
}

static void print_response(const wc_msg *m)
{
    wc_reader r;
    wc_array_iter it;
    uint32_t code;

    wc_r_init(&r, m->body, m->body_len, m->big_endian);
    code = wc_r_u32(&r);
    printf("RESPONSE %u\n", (unsigned)code);
    it = wc_r_array_begin(&r, 8);
    while (wc_r_array_more(&r, it)) {
        const char *key;
        wc_value v;
        wc_r_struct_begin(&r);
        key = wc_r_string(&r);
        if (!key || !wc_value_read(&r, &v))
            break;
        if (!strcmp(key, "uris") && !strcmp(v.sig, "as")) {
            size_t i;
            for (i = 0; i < v.strs.count; i++)
                printf("URI %s\n", v.strs.item[i]);
        } else {
            printf("KEY %s %s\n", key, v.sig);
        }
        wc_r_struct_end(&r);
    }
    wc_r_array_end(&r, it);
    fflush(stdout);
}

int main(int argc, char **argv)
{
    const char *method = "OpenFile";
    const char *token = NULL, *title = "probe", *dir = NULL, *name = NULL;
    const char *uri = NULL;
    bool multiple = false, directory = false, exit_after_handle = false;
    bool repeat_call = false, want_response = true;
    int wait_ms = 10000, close_after_ms = -1;
    char handle[512];
    wc_writer w;
    wc_array_mark opts;
    wc_hdr h;
    wc_msg m;
    uint32_t serial;
    int64_t start, close_at = 0;
    bool got_handle = false, closed = false;
    int i, rc, exit_code = 1;

    wc_log_level = WC_LOG_ERROR;

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *n = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(a, "--method") && n) { method = n; i++; }
        else if (!strcmp(a, "--token") && n) { token = n; i++; }
        else if (!strcmp(a, "--title") && n) { title = n; i++; }
        else if (!strcmp(a, "--dir") && n) { dir = n; i++; }
        else if (!strcmp(a, "--name") && n) { name = n; i++; }
        else if (!strcmp(a, "--uri") && n) { uri = n; i++; }
        else if (!strcmp(a, "--multiple")) multiple = true;
        else if (!strcmp(a, "--directory")) directory = true;
        else if (!strcmp(a, "--exit-after-handle")) exit_after_handle = true;
        else if (!strcmp(a, "--repeat-call")) repeat_call = true;
        else if (!strcmp(a, "--no-response")) want_response = false;
        else if (!strcmp(a, "--wait-ms") && n) { wait_ms = atoi(n); i++; }
        else if (!strcmp(a, "--close-after-ms") && n) { close_after_ms = atoi(n); i++; }
        else { fprintf(stderr, "probe: bad arg %s\n", a); return 3; }
    }

    handle[0] = 0;
    if (!wc_bus_connect(&bus, NULL))
        die("connect");
    if (!wc_bus_hello(&bus))
        die("hello");
    if (!wc_bus_add_match(&bus, "type='signal',"
                                "interface='org.freedesktop.portal.Request',"
                                "member='Response'"))
        die("addmatch");

    /* ---- build the method body ---- */
    wc_w_init(&w);
    wc_w_string(&w, "");                       /* parent_window */
    wc_w_string(&w, !strcmp(method, "OpenURI") ? (uri ? uri : "") : title);
    opts = wc_w_dict_begin(&w);
    if (token) {
        wc_w_dict_entry(&w, "handle_token", "s");
        wc_w_string(&w, token);
    }
    if (multiple) {
        wc_w_dict_entry(&w, "multiple", "b");
        wc_w_bool(&w, true);
    }
    if (directory) {
        wc_w_dict_entry(&w, "directory", "b");
        wc_w_bool(&w, true);
    }
    if (dir) {
        size_t len = strlen(dir) + 1;
        wc_array_mark ay;
        size_t k;
        wc_w_dict_entry(&w, "current_folder", "ay");
        ay = wc_w_array_begin(&w, 1);
        for (k = 0; k < len; k++)
            wc_w_byte(&w, (uint8_t)dir[k]);
        wc_w_array_end(&w, ay);
    }
    if (name) {
        wc_w_dict_entry(&w, "current_name", "s");
        wc_w_string(&w, name);
    }
    wc_w_dict_end(&w, opts);
    if (!wc_w_ok(&w))
        die("marshal");

    memset(&h, 0, sizeof h);
    h.type = WC_MSG_METHOD_CALL;
    h.path = WC_PORTAL_PATH;
    h.interface = !strcmp(method, "OpenURI") ? "org.freedesktop.portal.OpenURI"
                                             : "org.freedesktop.portal.FileChooser";
    h.member = method;
    h.destination = WC_PORTAL_NAME;
    h.signature = "ssa{sv}";
    serial = wc_bus_send(&bus, &h, w.buf, w.len);
    wc_w_free(&w);
    if (!serial)
        die("send");

    start = wc_now_ms();
    if (close_after_ms >= 0)
        close_at = start + close_after_ms;

    for (;;) {
        int64_t now = wc_now_ms();
        int slice = 200;

        if (now - start > wait_ms) {
            printf("TIMEOUT\n");
            fflush(stdout);
            exit_code = 1;
            break;
        }
        if (!pump(slice))
            die("pump");

        while ((rc = wc_bus_next(&bus, &m)) > 0) {
            if ((m.type == WC_MSG_METHOD_RETURN || m.type == WC_MSG_ERROR) &&
                m.reply_serial == serial) {
                if (m.type == WC_MSG_ERROR) {
                    wc_reader r;
                    const char *text;
                    wc_r_init(&r, m.body, m.body_len, m.big_endian);
                    text = wc_r_string(&r);
                    printf("ERROR %s %s\n", m.error_name ? m.error_name : "?",
                           text ? text : "");
                    fflush(stdout);
                    wc_msg_clear(&m);
                    exit_code = 2;
                    goto done;
                }
                {
                    wc_reader r;
                    const char *path;
                    wc_r_init(&r, m.body, m.body_len, m.big_endian);
                    path = wc_r_string(&r);
                    if (!path)
                        die("bad handle");
                    snprintf(handle, sizeof handle, "%s", path);
                    printf("HANDLE %s\n", handle);
                    fflush(stdout);
                    got_handle = true;
                }
                if (repeat_call) {
                    /* same sender + same token must be rejected */
                    wc_writer w2;
                    wc_array_mark o2;
                    wc_w_init(&w2);
                    wc_w_string(&w2, "");
                    wc_w_string(&w2, title);
                    o2 = wc_w_dict_begin(&w2);
                    wc_w_dict_entry(&w2, "handle_token", "s");
                    wc_w_string(&w2, token ? token : "dup");
                    wc_w_dict_end(&w2, o2);
                    serial = wc_bus_send(&bus, &h, w2.buf, w2.len);
                    wc_w_free(&w2);
                    repeat_call = false;
                } else if (exit_after_handle) {
                    exit_code = 0;
                    wc_msg_clear(&m);
                    goto done;
                } else if (!want_response) {
                    exit_code = 0;
                    wc_msg_clear(&m);
                    goto done;
                }
            } else if (m.type == WC_MSG_SIGNAL && m.member &&
                       !strcmp(m.member, "Response") && got_handle &&
                       m.path && !strcmp(m.path, handle)) {
                print_response(&m);
                wc_msg_clear(&m);
                exit_code = 0;
                goto done;
            }
            wc_msg_clear(&m);
        }
        if (rc < 0)
            die("parse");

        if (close_at && !closed && wc_now_ms() >= close_at) {
            wc_hdr ch;
            memset(&ch, 0, sizeof ch);
            ch.type = WC_MSG_METHOD_CALL;
            ch.path = handle;
            ch.interface = "org.freedesktop.portal.Request";
            ch.member = "Close";
            ch.destination = WC_PORTAL_NAME;
            (void)wc_bus_send(&bus, &ch, NULL, 0);
            closed = true;
            printf("CLOSE_SENT\n");
            fflush(stdout);
        }
    }
done:
    (void)wc_bus_flush(&bus);
    wc_bus_close(&bus);
    return exit_code;
}