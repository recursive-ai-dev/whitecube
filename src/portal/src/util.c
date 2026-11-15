/* SPDX-License-Identifier: MIT
 *
 * util.c - logging, sanitisation, RFC 8089 URIs, safe process spawning.
 *
 * Nothing in here ever builds a shell command line.  Every child process is
 * started with execve() and an explicit argv array.
 */
#include "whitecube_portal.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

wc_loglevel wc_log_level = WC_LOG_INFO;

void wc_log(wc_loglevel lvl, const char *fmt, ...)
{
    static const char tag[] = { 'E', 'W', 'I', 'D' };
    va_list ap;

    if (lvl > wc_log_level)
        return;
    fprintf(stderr, "whitecube-portal[%ld] %c: ", (long)getpid(),
            tag[lvl <= WC_LOG_DEBUG ? lvl : WC_LOG_DEBUG]);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

int64_t wc_now_ms(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

bool wc_fmt(char *dst, size_t cap, const char *fmt, ...)
{
    va_list ap;
    int n;

    if (!cap)
        return false;
    va_start(ap, fmt);
    n = vsnprintf(dst, cap, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= cap) {
        dst[0] = 0;
        return false;
    }
    return true;
}

bool wc_text_is_safe(const char *s)
{
    const unsigned char *p = (const unsigned char *)s;

    if (!s || !*s)
        return false;
    for (; *p; p++) {
        if (*p < 0x20 || *p == 0x7f)
            return false;
    }
    return true;
}

void wc_sanitize_text(char *dst, size_t cap, const char *src, size_t maxlen)
{
    size_t n = 0;
    const unsigned char *p;

    if (!cap)
        return;
    dst[0] = 0;
    if (!src)
        return;
    if (maxlen && maxlen < cap - 1)
        cap = maxlen + 1;
    for (p = (const unsigned char *)src; *p && n + 1 < cap; p++) {
        if (*p < 0x20 || *p == 0x7f)
            continue;                 /* strip C0 + DEL, incl. newlines */
        dst[n++] = (char)*p;
    }
    dst[n] = 0;
}

void wc_sanitize_token(const char *token, uint32_t serial, char *out, size_t cap)
{
    size_t n = 0;

    if (cap < 2)
        return;
    out[0] = 0;
    if (token) {
        for (; token[n] && n + 1 < cap && n < WC_TOKEN_MAX - 1; n++) {
            char c = token[n];
            bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                      (c >= '0' && c <= '9') || c == '_';
            out[n] = ok ? c : '_';
        }
    }
    out[n] = 0;
    if (!n) {
        if (!wc_fmt(out, cap, "wc%u", (unsigned)serial))
            out[0] = 0;
    }
}

bool wc_sender_element(const char *sender, char *out, size_t cap)
{
    size_t i, n = 0;

    if (!sender || !*sender || !cap)
        return false;
    /* xdg-desktop-portal / GTK / libportal all strip the leading ':' and
     * replace '.' with '_'.  Anything else and the client subscribes to a
     * different object path than we emit on. */
    if (sender[0] == ':')
        sender++;
    for (i = 0; sender[i]; i++) {
        char c = sender[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_';
        if (n + 1 >= cap)
            return false;
        out[n++] = ok ? c : '_';
    }
    out[n] = 0;
    return n > 0;
}

bool wc_is_absolute_path(const char *s)
{
    return s && s[0] == '/';
}

/* ------------------------------------------------------------------ */
/* RFC 8089 file URIs                                                  */
/* ------------------------------------------------------------------ */

static bool uri_unreserved(unsigned char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '-' || c == '.' || c == '_' ||
           c == '~';
}

bool wc_uri_encode(const char *path, char *out, size_t cap)
{
    static const char hex[] = "0123456789ABCDEF";
    const unsigned char *p;
    size_t n = 0;

    if (!wc_is_absolute_path(path) || cap < 16)
        return false;
    if (cap < 8)
        return false;
    memcpy(out, "file://", 7);
    n = 7;
    for (p = (const unsigned char *)path; *p; p++) {
        if (*p == '/') {
            if (n + 1 >= cap)
                return false;
            out[n++] = '/';
        } else if (uri_unreserved(*p)) {
            if (n + 1 >= cap)
                return false;
            out[n++] = (char)*p;
        } else {
            if (n + 3 >= cap)
                return false;
            out[n++] = '%';
            out[n++] = hex[*p >> 4];
            out[n++] = hex[*p & 0xf];
        }
    }
    out[n] = 0;
    return true;
}

/* ------------------------------------------------------------------ */
/* runtime dir + temp selection files                                  */
/* ------------------------------------------------------------------ */

static bool dir_is_private(const char *path)
{
    struct stat st;

    if (lstat(path, &st) != 0)
        return false;
    if (!S_ISDIR(st.st_mode))
        return false;
    if (st.st_uid != getuid())
        return false;
    if ((st.st_mode & 0077) != 0)
        return false;
    return true;
}

bool wc_runtime_dir(char *out, size_t cap)
{
    const char *env = getenv("XDG_RUNTIME_DIR");
    char fallback[PATH_MAX];

    if (env && wc_is_absolute_path(env) && strlen(env) < PATH_MAX - 32) {
        if (dir_is_private(env)) {
            return wc_fmt(out, cap, "%s", env);
        }
        wc_warn("XDG_RUNTIME_DIR '%s' is not a private directory", env);
    }
    if (!wc_fmt(fallback, sizeof fallback, "/tmp/whitecube-portal-%u",
                (unsigned)getuid()))
        return false;
    if (mkdir(fallback, 0700) != 0 && errno != EEXIST) {
        wc_err("cannot create %s: %s", fallback, strerror(errno));
        return false;
    }
    if (!dir_is_private(fallback)) {
        wc_err("%s is not private, refusing to use it", fallback);
        return false;
    }
    return wc_fmt(out, cap, "%s", fallback);
}

int wc_mkstemp_runtime(const char *runtimedir, char *path_out, size_t cap)
{
    int fd;

    if (!wc_fmt(path_out, cap, "%s/wc-portal-XXXXXX", runtimedir))
        return -1;
    fd = mkostemp(path_out, O_CLOEXEC);
    if (fd < 0) {
        wc_err("mkostemp failed: %s", strerror(errno));
        path_out[0] = 0;
        return -1;
    }
    if (fchmod(fd, 0600) != 0)
        wc_warn("cannot chmod selection file: %s", strerror(errno));
    return fd;
}

/* ------------------------------------------------------------------ */
/* process hygiene                                                     */
/* ------------------------------------------------------------------ */

void wc_close_inherited_fds(void)
{
#ifdef SYS_close_range
    if (syscall(SYS_close_range, 3u, ~0u, 0u) == 0)
        return;
#endif
    long max = sysconf(_SC_OPEN_MAX);
    int fd;

    if (max < 0 || max > 65536)
        max = 65536;
    for (fd = 3; fd < (int)max; fd++)
        (void)close(fd);
}

pid_t wc_spawn(char *const argv[])
{
    pid_t pid;

    if (!argv || !argv[0] || !wc_is_absolute_path(argv[0]))
        return -1;

    pid = fork();
    if (pid < 0) {
        wc_err("fork failed: %s", strerror(errno));
        return -1;
    }
    if (pid > 0)
        return pid;

    /* ---- child: nothing here may touch portal state ---- */
    {
        sigset_t empty;
        int fd;

        sigemptyset(&empty);
        (void)sigprocmask(SIG_SETMASK, &empty, NULL);
        (void)signal(SIGPIPE, SIG_DFL);
        (void)signal(SIGCHLD, SIG_DFL);
        (void)signal(SIGTERM, SIG_DFL);
        (void)signal(SIGINT, SIG_DFL);
        (void)signal(SIGHUP, SIG_DFL);
        (void)setsid();
        /* If the portal dies, do not leave a floating picker behind. */
        (void)prctl(PR_SET_PDEATHSIG, SIGTERM);
        if (getppid() == 1)
            _exit(127);
        wc_close_inherited_fds();
        fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
        if (fd >= 0) {
            (void)dup2(fd, STDIN_FILENO);
            if (fd > 2)
                (void)close(fd);
        }
        /* The spawned program is a separate trust domain: restore normal
         * core-dump/proc semantics for it, keep NO_NEW_PRIVS inherited. */
        (void)prctl(PR_SET_DUMPABLE, 1);
        execve(argv[0], argv, environ);
        _exit(127);
    }
}

bool wc_find_exec(const char *name, char *out, size_t cap)
{
    const char *path;
    char candidate[PATH_MAX];
    struct stat st;

    if (!name || !*name)
        return false;

    if (strchr(name, '/')) {
        if (!wc_is_absolute_path(name))
            return false;
        if (stat(name, &st) != 0 || !S_ISREG(st.st_mode))
            return false;
        if (access(name, X_OK) != 0)
            return false;
        return wc_fmt(out, cap, "%s", name);
    }

    path = getenv("PATH");
    if (!path || !*path)
        path = "/usr/bin:/bin";
    while (*path) {
        const char *end = strchr(path, ':');
        size_t len = end ? (size_t)(end - path) : strlen(path);
        if (len && len < PATH_MAX - 64) {
            char dir[PATH_MAX];
            memcpy(dir, path, len);
            dir[len] = 0;
            if (wc_fmt(candidate, sizeof candidate, "%s/%s", dir, name) &&
                stat(candidate, &st) == 0 && S_ISREG(st.st_mode) &&
                access(candidate, X_OK) == 0)
                return wc_fmt(out, cap, "%s", candidate);
        }
        if (!end)
            break;
        path = end + 1;
    }
    return false;
}

bool wc_read_file_line(const char *path, char *out, size_t cap)
{
    FILE *f;
    size_t n;

    out[0] = 0;
    f = fopen(path, "re");
    if (!f)
        return false;
    if (!fgets(out, (int)cap, f)) {
        fclose(f);
        out[0] = 0;
        return false;
    }
    fclose(f);
    n = strlen(out);
    while (n && (out[n - 1] == '\n' || out[n - 1] == '\r'))
        out[--n] = 0;
    return n > 0;
}