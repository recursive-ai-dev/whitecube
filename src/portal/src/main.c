/* SPDX-License-Identifier: MIT
 *
 * main.c - whitecube-portal entry point: hardening, event loop, dispatch.
 *
 * Single process, single thread, no callbacks from signal handlers: SIGCHLD /
 * SIGTERM / SIGINT / SIGHUP are blocked and consumed through a signalfd in the
 * same poll() loop as the bus socket, so child reaping can never race with
 * request bookkeeping.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "whitecube_portal.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/signalfd.h>
#include <sys/stat.h>
#include <unistd.h>

#define WC_VERSION "1.0.0"

static wc_portal g_portal;

/* ------------------------------------------------------------------ */
/* usage / options                                                     */
/* ------------------------------------------------------------------ */

static void usage(FILE *out, const char *argv0)
{
    fprintf(out,
"whitecube-portal %s - org.freedesktop.portal.Desktop for whitecube-bus\n"
"\n"
"Usage: %s [options]\n"
"\n"
"  --picker-cmd PATH     file picker executable (default: wc-pick)\n"
"  --browser-cmd PATH    browser for OpenURI (default: $WC_PORTAL_BROWSER,\n"
"                        then firefox/chromium/... from $PATH)\n"
"  --light               report prefer-light (color-scheme 2)\n"
"  --dark                report prefer-dark (color-scheme 1, default)\n"
"  --color-scheme N      0 = no preference, 1 = dark, 2 = light\n"
"  --accent R,G,B        accent colour as three doubles (default 0.4,0.7,0.9)\n"
"  --contrast N          org.freedesktop.appearance contrast (default 0)\n"
"  --max-requests N      concurrent picker cap (default 8, max 64)\n"
"  --timeout SECS        picker lifetime before it is killed (default 300)\n"
"  --bus-address ADDR    override $DBUS_SESSION_BUS_ADDRESS\n"
"  --replace             replace an existing org.freedesktop.portal.Desktop\n"
"  --cancel-on-disconnect  cancel pickers when the calling client vanishes\n"
"  -v, --verbose         debug logging\n"
"  -q, --quiet           warnings and errors only\n"
"  --version             print version and exit\n"
"  -h, --help            this text\n", WC_VERSION, argv0);
}

static bool parse_uint(const char *s, unsigned *out, unsigned lo, unsigned hi)
{
    char *end = NULL;
    unsigned long v;

    if (!s || !*s)
        return false;
    errno = 0;
    v = strtoul(s, &end, 10);
    if (errno || !end || *end || v < lo || v > hi)
        return false;
    *out = (unsigned)v;
    return true;
}

static bool parse_args(wc_portal *p, int argc, char **argv)
{
    int i;

    p->cfg.color_scheme = 1;
    p->cfg.accent[0] = 0.4;
    p->cfg.accent[1] = 0.7;
    p->cfg.accent[2] = 0.9;
    p->cfg.contrast = 0;
    p->cfg.max_requests = WC_DEFAULT_REQUESTS;
    p->cfg.timeout_s = WC_DEFAULT_TIMEOUT_S;

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *next = (i + 1 < argc) ? argv[i + 1] : NULL;
        unsigned u;

        if (!strcmp(a, "--picker-cmd") && next) {
            p->cfg.picker_cmd = next; i++;
        } else if (!strcmp(a, "--browser-cmd") && next) {
            p->cfg.browser_cmd = next; i++;
        } else if (!strcmp(a, "--bus-address") && next) {
            p->cfg.bus_address = next; i++;
        } else if (!strcmp(a, "--light")) {
            p->cfg.color_scheme = 2; p->cfg.color_scheme_set = true;
        } else if (!strcmp(a, "--dark")) {
            p->cfg.color_scheme = 1; p->cfg.color_scheme_set = true;
        } else if (!strcmp(a, "--color-scheme") && next &&
                   parse_uint(next, &u, 0, 2)) {
            p->cfg.color_scheme = u; p->cfg.color_scheme_set = true; i++;
        } else if (!strcmp(a, "--contrast") && next &&
                   parse_uint(next, &u, 0, 1)) {
            p->cfg.contrast = u; p->cfg.contrast_set = true; i++;
        } else if (!strcmp(a, "--accent") && next) {
            double r, g, b;
            if (sscanf(next, "%lf,%lf,%lf", &r, &g, &b) != 3 ||
                r < 0 || r > 1 || g < 0 || g > 1 || b < 0 || b > 1) {
                fprintf(stderr, "invalid --accent (want R,G,B in 0..1)\n");
                return false;
            }
            p->cfg.accent[0] = r;
            p->cfg.accent[1] = g;
            p->cfg.accent[2] = b;
            p->cfg.accent_set = true;
            i++;
        } else if (!strcmp(a, "--max-requests") && next &&
                   parse_uint(next, &u, 1, WC_MAX_REQUESTS_HARD)) {
            p->cfg.max_requests = u; i++;
        } else if (!strcmp(a, "--timeout") && next &&
                   parse_uint(next, &u, 1, 86400)) {
            p->cfg.timeout_s = u; i++;
        } else if (!strcmp(a, "--replace")) {
            p->cfg.replace = true;
        } else if (!strcmp(a, "--cancel-on-disconnect")) {
            p->cfg.cancel_on_disconnect = true;
        } else if (!strcmp(a, "-v") || !strcmp(a, "--verbose")) {
            wc_log_level = WC_LOG_DEBUG;
        } else if (!strcmp(a, "-q") || !strcmp(a, "--quiet")) {
            wc_log_level = WC_LOG_WARN;
        } else if (!strcmp(a, "--version")) {
            printf("whitecube-portal %s\n", WC_VERSION);
            exit(0);
        } else if (!strcmp(a, "-h") || !strcmp(a, "--help")) {
            usage(stdout, argv[0]);
            exit(0);
        } else {
            fprintf(stderr, "unknown or incomplete option: %s\n", a);
            usage(stderr, argv[0]);
            return false;
        }
    }
    return true;
}

/* ------------------------------------------------------------------ */
/* hardening + environment                                             */
/* ------------------------------------------------------------------ */

static void harden(void)
{
    struct rlimit rl;

    (void)umask(077);
    rl.rlim_cur = 0;
    rl.rlim_max = 0;
    (void)setrlimit(RLIMIT_CORE, &rl);
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0)
        wc_warn("PR_SET_DUMPABLE failed: %s", strerror(errno));
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0)
        wc_warn("PR_SET_NO_NEW_PRIVS failed: %s", strerror(errno));
    wc_close_inherited_fds();
}

static void resolve_home(wc_portal *p)
{
    const char *home = getenv("HOME");
    struct passwd *pw;

    if (home && wc_is_absolute_path(home) &&
        wc_fmt(p->home, sizeof p->home, "%s", home))
        return;
    pw = getpwuid(getuid());
    if (pw && pw->pw_dir && wc_is_absolute_path(pw->pw_dir) &&
        wc_fmt(p->home, sizeof p->home, "%s", pw->pw_dir))
        return;
    wc_warn("no usable $HOME, falling back to /");
    wc_fmt(p->home, sizeof p->home, "/");
}

static void resolve_picker(wc_portal *p, const char *exedir)
{
    const char *env;
    char candidate[PATH_MAX];
    const char *names[] = { WC_DEFAULT_PICKER, NULL, NULL, "wc-pick" };
    char devpath[PATH_MAX], devpath2[PATH_MAX];

    if (p->cfg.picker_cmd) {
        if (wc_find_exec(p->cfg.picker_cmd, p->picker, sizeof p->picker))
            return;
        wc_err("--picker-cmd '%s' is not an executable file",
               p->cfg.picker_cmd);
        return;
    }
    env = getenv("WC_PORTAL_PICKER");
    if (env && *env && wc_find_exec(env, p->picker, sizeof p->picker))
        return;
    if (exedir[0]) {
        if (wc_fmt(devpath, sizeof devpath, "%s/scripts/wc-pick", exedir) &&
            wc_find_exec(devpath, p->picker, sizeof p->picker))
            return;
        if (wc_fmt(devpath2, sizeof devpath2, "%s/wc-pick", exedir) &&
            wc_find_exec(devpath2, p->picker, sizeof p->picker))
            return;
    }
    for (size_t i = 0; names[i]; i++) {
        if (!strcmp(names[i], WC_DEFAULT_PICKER)) {
            if (wc_find_exec(names[i], p->picker, sizeof p->picker))
                return;
            continue;
        }
        if (wc_find_exec(names[i], p->picker, sizeof p->picker))
            return;
    }
    (void)candidate;
    wc_warn("no picker found; Settings still work, OpenFile will fail "
            "(install scripts/wc-pick or pass --picker-cmd)");
}

static void resolve_machine_id(wc_portal *p)
{
    char id[64];
    size_t i;

    if ((wc_read_file_line("/etc/machine-id", id, sizeof id) ||
         wc_read_file_line("/var/lib/dbus/machine-id", id, sizeof id)) &&
        strlen(id) == 32) {
        for (i = 0; i < 32; i++)
            if (!((id[i] >= '0' && id[i] <= '9') ||
                  (id[i] >= 'a' && id[i] <= 'f')))
                break;
        if (i == 32 && wc_fmt(p->machine_id, sizeof p->machine_id, "%s", id))
            return;
    }
    wc_fmt(p->machine_id, sizeof p->machine_id,
           "00000000000000000000000000000000");
}

/* ------------------------------------------------------------------ */
/* dispatch                                                            */
/* ------------------------------------------------------------------ */

static const char *infer_interface(const char *member)
{
    if (!strcmp(member, "Introspect"))
        return "org.freedesktop.DBus.Introspectable";
    if (!strcmp(member, "Ping") || !strcmp(member, "GetMachineId"))
        return "org.freedesktop.DBus.Peer";
    if (!strcmp(member, "Get") || !strcmp(member, "GetAll") ||
        !strcmp(member, "Set"))
        return "org.freedesktop.DBus.Properties";
    if (!strcmp(member, "Read") || !strcmp(member, "ReadAll"))
        return "org.freedesktop.portal.Settings";
    if (!strcmp(member, "OpenFile") || !strcmp(member, "SaveFile"))
        return "org.freedesktop.portal.FileChooser";
    if (!strcmp(member, "OpenURI"))
        return "org.freedesktop.portal.OpenURI";
    return NULL;
}

static void dispatch_desktop(wc_portal *p, const wc_msg *m)
{
    const char *iface = m->interface;
    const char *member = m->member ? m->member : "";
    wc_writer w;

    if (!*member) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "missing member");
        return;
    }
    if (!iface || !*iface)
        iface = infer_interface(member);
    if (!iface) {
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_METH,
                     "unknown method '%s'", member);
        return;
    }

    if (!strcmp(iface, "org.freedesktop.DBus.Peer")) {
        if (!strcmp(member, "Ping")) {
            wc_bus_reply_empty(&p->bus, m);
        } else if (!strcmp(member, "GetMachineId")) {
            wc_w_init(&w);
            wc_w_string(&w, p->machine_id);
            wc_bus_reply(&p->bus, m, "s", &w);
            wc_w_free(&w);
        } else {
            wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_METH, "no method %s",
                         member);
        }
        return;
    }
    if (!strcmp(iface, "org.freedesktop.DBus.Introspectable")) {
        if (!strcmp(member, "Introspect"))
            wc_introspect_handle(p, m);
        else
            wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_METH, "no method %s",
                         member);
        return;
    }
    if (!strcmp(iface, "org.freedesktop.DBus.Properties")) {
        wc_properties_handle(p, m);
        return;
    }
    if (!strcmp(iface, "org.freedesktop.portal.Settings")) {
        wc_settings_handle(p, m);
        return;
    }
    if (!strcmp(iface, "org.freedesktop.portal.FileChooser")) {
        wc_filechooser_handle(p, m);
        return;
    }
    if (!strcmp(iface, "org.freedesktop.portal.OpenURI")) {
        wc_uri_handle(p, m);
        return;
    }
    if (wc_iface_denied(iface)) {
        wc_warn("denied portal interface %s.%s", iface, member);
        wc_bus_error(&p->bus, m, WC_ERR_NOT_ALLOWED,
                     "%s is not available in whitecube", iface);
        return;
    }
    wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_IFACE, "unknown interface %s",
                 iface);
}

static void dispatch(wc_portal *p, const wc_msg *m)
{
    if (m->type == WC_MSG_SIGNAL) {
        if (p->cfg.cancel_on_disconnect && m->interface && m->member &&
            !strcmp(m->interface, "org.freedesktop.DBus") &&
            !strcmp(m->member, "NameOwnerChanged") && m->signature &&
            !strcmp(m->signature, "sss")) {
            wc_reader r;
            const char *name, *old_owner, *new_owner;
            wc_r_init(&r, m->body, m->body_len, m->big_endian);
            name = wc_r_string(&r);
            old_owner = wc_r_string(&r);
            new_owner = wc_r_string(&r);
            if (wc_r_ok(&r) && name && old_owner && new_owner && !*new_owner)
                wc_req_sender_vanished(p, old_owner);
        }
        return;
    }
    if (m->type != WC_MSG_METHOD_CALL)
        return;                       /* replies to our own calls: ignored */

    if (!m->path) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "missing object path");
        return;
    }
    wc_dbg("call %s.%s from %s", m->interface ? m->interface : "(null)",
           m->member ? m->member : "(null)", m->sender ? m->sender : "?");

    if (!strcmp(m->path, WC_PORTAL_PATH)) {
        dispatch_desktop(p, m);
        return;
    }
    if (!strncmp(m->path, WC_REQUEST_PREFIX, sizeof WC_REQUEST_PREFIX - 1)) {
        const char *iface = m->interface;
        const char *member = m->member ? m->member : "";
        if ((!iface || !strcmp(iface, "org.freedesktop.portal.Request")) &&
            !strcmp(member, "Close")) {
            wc_request_handle_close(p, m);
            return;
        }
        if ((!iface || !strcmp(iface, "org.freedesktop.DBus.Introspectable")) &&
            !strcmp(member, "Introspect")) {
            wc_introspect_handle(p, m);
            return;
        }
        if ((!iface || !strcmp(iface, "org.freedesktop.DBus.Peer")) &&
            !strcmp(member, "Ping")) {
            wc_bus_reply_empty(&p->bus, m);
            return;
        }
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_METH,
                     "requests only support Close (got %s)", member);
        return;
    }
    wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_OBJ, "no such object: %s", m->path);
}

/* ------------------------------------------------------------------ */
/* signals + main loop                                                 */
/* ------------------------------------------------------------------ */

static int open_signalfd(void)
{
    sigset_t mask;
    int fd;

    sigemptyset(&mask);
    sigaddset(&mask, SIGCHLD);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGHUP);
    if (sigprocmask(SIG_BLOCK, &mask, NULL) != 0)
        return -1;
    (void)signal(SIGPIPE, SIG_IGN);
    fd = signalfd(-1, &mask, SFD_CLOEXEC | SFD_NONBLOCK);
    return fd;
}

static void handle_signals(wc_portal *p, int fd)
{
    struct signalfd_siginfo si;
    ssize_t n;

    for (;;) {
        n = read(fd, &si, sizeof si);
        if (n != (ssize_t)sizeof si)
            break;
        switch (si.ssi_signo) {
        case SIGCHLD:
            wc_req_reap(p);
            break;
        case SIGTERM:
        case SIGINT:
            wc_info("signal %u, shutting down", (unsigned)si.ssi_signo);
            p->running = false;
            break;
        case SIGHUP:
            wc_dbg("SIGHUP: reloading Whitecube appearance settings");
            wc_settings_sync(p, true);
            break;
        default:
            break;
        }
    }
}

static void run_loop(wc_portal *p, int sigfd)
{
    while (p->running && !p->bus.dead) {
        struct pollfd fds[2];
        int timeout = -1;
        int64_t now = wc_now_ms();
        int64_t next = wc_req_next_deadline(p);
        int rc;

        wc_req_expire(p, now);
        if (next > 0) {
            int64_t delta = next - wc_now_ms();
            if (delta < 0)
                delta = 0;
            if (delta > 60000)
                delta = 60000;
            timeout = (int)delta;
        }

        fds[0].fd = p->bus.fd;
        fds[0].events = POLLIN | (p->bus.out_sent < p->bus.out_len ? POLLOUT : 0);
        fds[0].revents = 0;
        fds[1].fd = sigfd;
        fds[1].events = POLLIN;
        fds[1].revents = 0;

        rc = poll(fds, 2, timeout);
        if (rc < 0) {
            if (errno == EINTR)
                continue;
            wc_err("poll: %s", strerror(errno));
            break;
        }
        if (fds[1].revents & POLLIN)
            handle_signals(p, sigfd);

        if (fds[0].revents & POLLOUT)
            (void)wc_bus_flush(&p->bus);

        if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
            if (wc_bus_recv(&p->bus) >= 0) {
                wc_msg m;
                int n;
                while ((n = wc_bus_next(&p->bus, &m)) > 0) {
                    dispatch(p, &m);
                    wc_msg_clear(&m);
                }
            }
        }
        (void)wc_bus_flush(&p->bus);
    }
}

int main(int argc, char **argv)
{
    wc_portal *p = &g_portal;
    char exe[PATH_MAX];
    char exedir[PATH_MAX];
    ssize_t n;
    int sigfd;
    int status = 0;

    exedir[0] = 0;
    if (!parse_args(p, argc, argv))
        return 2;

    /* Resolve our own location before PR_SET_DUMPABLE hides /proc entries. */
    n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n > 0) {
        char *slash;
        exe[n] = 0;
        slash = strrchr(exe, '/');
        if (slash) {
            *slash = 0;
            if (!wc_fmt(exedir, sizeof exedir, "%s", exe))
                exedir[0] = 0;
        }
    }

    harden();
    sigfd = open_signalfd();
    if (sigfd < 0) {
        wc_err("signalfd: %s", strerror(errno));
        return 1;
    }
    if (getuid() == 0)
        wc_warn("running as root: pickers will inherit root privileges");

    resolve_home(p);
    if (!wc_runtime_dir(p->runtime_dir, sizeof p->runtime_dir)) {
        wc_err("no usable runtime directory");
        return 1;
    }
    resolve_picker(p, exedir);
    resolve_machine_id(p);
    wc_settings_sync(p, false);

    if (!wc_bus_connect(&p->bus, p->cfg.bus_address))
        return 1;
    if (!wc_bus_hello(&p->bus)) {
        wc_err("Hello() failed");
        wc_bus_close(&p->bus);
        return 1;
    }
    if (!wc_bus_request_name(&p->bus, WC_PORTAL_NAME, p->cfg.replace)) {
        wc_err("could not claim %s - is another portal running?",
               WC_PORTAL_NAME);
        wc_bus_close(&p->bus);
        return 1;
    }
    if (p->cfg.cancel_on_disconnect &&
        !wc_bus_add_match(&p->bus,
                          "type='signal',sender='org.freedesktop.DBus',"
                          "interface='org.freedesktop.DBus',"
                          "member='NameOwnerChanged'"))
        wc_warn("AddMatch failed; disconnect cleanup disabled");

    wc_info("ready on %s (%s), color-scheme=%u, picker=%s, max-requests=%u",
            WC_PORTAL_NAME, p->bus.unique_name, (unsigned)p->cfg.color_scheme,
            p->picker[0] ? p->picker : "(none)", p->cfg.max_requests);

    p->running = true;
    run_loop(p, sigfd);

    wc_info("shutting down");
    wc_req_shutdown(p);
    (void)wc_bus_flush(&p->bus);
    wc_bus_close(&p->bus);
    close(sigfd);
    return status;
}