/* whitecube-bus: startup, hardening, socket setup, CLI. */
#include "whitecube_bus.h"
#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <unistd.h>

static volatile sig_atomic_t g_stop = 0;
static void on_signal(int sig) { (void)sig; g_stop = 1; }

static const char *DEFAULT_ALLOW[] = {
    "org.whitecube.*",
    "ca.desrt.dconf",
    "org.a11y.Bus",
    "org.a11y.atspi.*",
    "org.freedesktop.Notifications",
    "org.freedesktop.portal.Desktop",
    "org.freedesktop.ScreenSaver",
    "org.freedesktop.FileManager1",
    "org.freedesktop.ReserveDevice1.*",
    "org.gtk.Notifications",
    "org.gtk.vfs.*",
    "org.kde.StatusNotifierWatcher",
    "org.kde.StatusNotifierItem-*",
    "org.mpris.MediaPlayer2.*",
};

static void notify_systemd_ready(void)
{
    const char *ns = getenv("NOTIFY_SOCKET");
    if (!ns || !*ns || strlen(ns) >= sizeof(((struct sockaddr_un *)0)->sun_path)) return;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return;
    struct sockaddr_un sa;
    memset(&sa, 0, sizeof sa);
    sa.sun_family = AF_UNIX;
    if (ns[0] == '@') {
        size_t n = strlen(ns);
        memcpy(sa.sun_path, ns, n);
        sa.sun_path[0] = 0;
        (void)sendto(fd, "READY=1\n", 8, MSG_NOSIGNAL, (struct sockaddr *)&sa,
                     (socklen_t)(offsetof(struct sockaddr_un, sun_path) + n));
    } else if (ns[0] == '/') {
        size_t n = strlen(ns);
        memcpy(sa.sun_path, ns, n);
        (void)sendto(fd, "READY=1\n", 8, MSG_NOSIGNAL, (struct sockaddr *)&sa,
                     (socklen_t)(offsetof(struct sockaddr_un, sun_path) + n + 1));
    }
    close(fd);
}

static void usage(FILE *out)
{
    fprintf(out,
        "whitecube-bus " WC_VERSION " - hardened D-Bus session broker\n"
        "usage: whitecube-bus [options]\n"
        "  --socket PATH              listen path (default $XDG_RUNTIME_DIR/bus)\n"
        "  --allow PATTERN            permit RequestName for PATTERN (repeatable; 'foo.*' prefix)\n"
        "  --allow-file FILE          read patterns from FILE, one per line\n"
        "  --allow-activation PATTERN permit StartServiceByName for PATTERN (repeatable; 'foo.*' prefix)\n"
        "  --allow-activation-file FILE read activation patterns from FILE, one per line\n"
        "  --activation-dir DIR       read .service files from DIR (standard D-Bus activation)\n"
        "  --allow-monitor            permit BecomeMonitor eavesdropping (off by default; testing only)\n"
        "  --audit                    one-line routing log to stderr\n"
        "  --seccomp[=kill]           apply syscall filter after bind (default: errno mode)\n"
        "  --allow-insecure-dir       permit a group/world accessible parent dir (testing only)\n"
        "  --help | --version\n");
}

static void close_inherited(void)
{
#ifdef SYS_close_range
    if (syscall(SYS_close_range, 3, ~0U, 0) == 0) return;
#endif
    for (int fd = 3; fd < 4096; fd++) close(fd);
}

static int check_parent(const char *path, bool strict)
{
    char buf[4096];
    snprintf(buf, sizeof buf, "%s", path);
    char *slash = strrchr(buf, '/');
    if (!slash) { fprintf(stderr, "socket path must be absolute\n"); return -1; }
    if (slash == buf) slash[1] = 0; else *slash = 0;

    struct stat st;
    if (stat(buf, &st) != 0 || !S_ISDIR(st.st_mode)) {
        fprintf(stderr, "parent directory '%s' is not a directory\n", buf);
        return -1;
    }
    if (st.st_uid != getuid()) {
        fprintf(stderr, "parent directory '%s' is not owned by uid %lu\n",
                buf, (unsigned long)getuid());
        return -1;
    }
    if (strict) {
        if ((st.st_mode & 0777) != 0700) {
            fprintf(stderr, "parent directory '%s' must be mode 0700 (is %04o)\n",
                    buf, (unsigned)(st.st_mode & 0777));
            return -1;
        }
    } else if (st.st_mode & 0077) {
        fprintf(stderr, "parent directory '%s' is group/world accessible; "
                        "use --allow-insecure-dir to override\n", buf);
        return -1;
    }
    return 0;
}

static int listen_unix(const char *path)
{
    struct sockaddr_un sa;
    memset(&sa, 0, sizeof sa);
    if (strlen(path) >= sizeof sa.sun_path) {
        fprintf(stderr, "socket path too long\n");
        return -1;
    }
    sa.sun_family = AF_UNIX;
    strcpy(sa.sun_path, path);

    struct stat st;
    if (stat(path, &st) == 0) {
        if (!S_ISSOCK(st.st_mode)) { fprintf(stderr, "'%s' exists and is not a socket\n", path); return -1; }
        int probe = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (probe >= 0) {
            int alive = (connect(probe, (struct sockaddr *)&sa, sizeof sa) == 0);
            close(probe);
            if (alive) { fprintf(stderr, "a bus is already listening on '%s'\n", path); return -1; }
        }
        if (unlink(path) != 0) { perror("unlink stale socket"); return -1; }
    }

    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) { perror("socket"); return -1; }
    mode_t old = umask(0077);
    int rc = bind(fd, (struct sockaddr *)&sa, sizeof sa);
    umask(old);
    if (rc != 0) { perror("bind"); close(fd); return -1; }
    if (chmod(path, 0600) != 0) { perror("chmod"); close(fd); unlink(path); return -1; }
    if (listen(fd, 128) != 0) { perror("listen"); close(fd); unlink(path); return -1; }
    return fd;
}

static int load_allow_file(Options *o, const char *file)
{
    FILE *f = fopen(file, "re");
    if (!f) { perror(file); return -1; }
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, f)) > 0) {
        while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r' || line[n - 1] == ' '))
            line[--n] = 0;
        if (!n || line[0] == '#') continue;
        o->allow = realloc(o->allow, (o->n_allow + 1) * sizeof(char *));
        if (!o->allow) { free(line); fclose(f); return -1; }
        o->allow[o->n_allow++] = strdup(line);
        if (!o->allow[o->n_allow - 1]) { free(line); fclose(f); return -1; }
    }
    free(line);
    fclose(f);
    return 0;
}

static int load_allow_activation_file(Options *o, const char *file)
{
    FILE *f = fopen(file, "re");
    if (!f) { perror(file); return -1; }
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, f)) > 0) {
        while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r' || line[n - 1] == ' '))
            line[--n] = 0;
        if (!n || line[0] == '#') continue;
        o->allow_activation = realloc(o->allow_activation, (o->n_allow_activation + 1) * sizeof(char *));
        if (!o->allow_activation) { free(line); fclose(f); return -1; }
        o->allow_activation[o->n_allow_activation++] = strdup(line);
        if (!o->allow_activation[o->n_allow_activation - 1]) { free(line); fclose(f); return -1; }
    }
    free(line);
    fclose(f);
    return 0;
}

static char *trim(char *s)
{
    while (*s == ' ' || *s == '\t') s++;
    char *end = s + strlen(s) - 1;
    while (end > s && (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n')) *end-- = 0;
    return s;
}

static void parse_service_file(Options *o, const char *path)
{
    FILE *f = fopen(path, "re");
    if (!f) return;

    Service svc = {0};
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    bool in_dbus_section = false;

    while ((n = getline(&line, &cap, f)) > 0) {
        char *s = trim(line);
        if (!*s || *s == '#') continue;

        if (s[0] == '[') {
            in_dbus_section = (strncmp(s, "[D-BUS Service]", 15) == 0);
            continue;
        }
        if (!in_dbus_section) continue;

        char *eq = strchr(s, '=');
        if (!eq) continue;
        *eq = 0;
        char *key = trim(s);
        char *val = trim(eq + 1);

        if (strcmp(key, "Name") == 0) {
            svc.name = strdup(val);
        } else if (strcmp(key, "Exec") == 0) {
            svc.exec = strdup(val);
        } else if (strcmp(key, "User") == 0) {
            svc.user = strdup(val);
        } else if (strcmp(key, "SystemdSlice") == 0) {
            svc.systemd_slice = strdup(val);
        } else if (strcmp(key, "SystemdService") == 0) {
            svc.systemd_service = (strcmp(val, "true") == 0);
        }
    }
    free(line);
    fclose(f);

    if (!svc.name || !svc.exec) {
        free(svc.name); free(svc.exec); free(svc.user); free(svc.systemd_slice);
        return;
    }

    /* Parse Exec into argv */
    char *exec_copy = strdup(svc.exec);
    char *token = strtok(exec_copy, " \t");
    while (token) {
        if (svc.argv == NULL || svc.argc % 8 == 0) {
            int ncap = (svc.argc + 8) & ~7;
            char **nv = realloc(svc.argv, ncap * sizeof(char *));
            if (!nv) break;
            svc.argv = nv;
        }
        svc.argv[svc.argc++] = strdup(token);
        token = strtok(NULL, " \t");
    }
    free(exec_copy);

    if (svc.argc == 0) {
        free(svc.name); free(svc.exec); free(svc.user); free(svc.systemd_slice);
        for (int i = 0; i < svc.argc; i++) free(svc.argv[i]);
        free(svc.argv);
        return;
    }

    if (o->n_services >= o->cap_services) {
        size_t ncap = o->cap_services ? o->cap_services * 2 : 16;
        Service *ns = realloc(o->services, ncap * sizeof(Service));
        if (!ns) {
            free(svc.name); free(svc.exec); free(svc.user); free(svc.systemd_slice);
            for (int i = 0; i < svc.argc; i++) free(svc.argv[i]);
            free(svc.argv);
            return;
        }
        o->services = ns;
        o->cap_services = ncap;
    }
    o->services[o->n_services++] = svc;
}

static void load_activation_dir(Options *o)
{
    if (!o->activation_dir) return;
    DIR *d = opendir(o->activation_dir);
    if (!d) return;

    struct dirent *ent;
    while ((ent = readdir(d))) {
        if (ent->d_name[0] == '.') continue;
        size_t len = strlen(ent->d_name);
        if (len < 8 || strcmp(ent->d_name + len - 8, ".service") != 0) continue;

        char path[4096];
        snprintf(path, sizeof path, "%s/%s", o->activation_dir, ent->d_name);
        parse_service_file(o, path);
    }
    closedir(d);
}

static void free_allow(Options *o)
{
    for (size_t i = 0; i < o->n_allow; i++) free(o->allow[i]);
    free(o->allow);
    o->allow = NULL;
    o->n_allow = 0;
}

static void free_allow_activation(Options *o)
{
    for (size_t i = 0; i < o->n_allow_activation; i++) free(o->allow_activation[i]);
    free(o->allow_activation);
    o->allow_activation = NULL;
    o->n_allow_activation = 0;
}

static void free_services(Options *o)
{
    for (size_t i = 0; i < o->n_services; i++) {
        free(o->services[i].name);
        free(o->services[i].exec);
        for (int j = 0; j < o->services[i].argc; j++) free(o->services[i].argv[j]);
        free(o->services[i].argv);
        free(o->services[i].user);
        free(o->services[i].systemd_slice);
    }
    free(o->services);
    o->services = NULL;
    o->n_services = 0;
    o->cap_services = 0;
}

int main(int argc, char **argv)
{
    Options o;
    memset(&o, 0, sizeof o);
    const char *sock = NULL;
    bool explicit_sock = false;

    close_inherited();
    prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
    prctl(PR_SET_DUMPABLE, 0, 0, 0, 0);

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--socket") && i + 1 < argc) { sock = argv[++i]; explicit_sock = true; }
        else if (!strcmp(a, "--audit")) o.audit = true;
        else if (!strcmp(a, "--allow-monitor")) o.allow_monitor = true;
        else if (!strcmp(a, "--seccomp")) o.seccomp = 1;
        else if (!strcmp(a, "--seccomp=kill")) o.seccomp = 2;
        else if (!strcmp(a, "--allow-insecure-dir")) o.allow_insecure_dir = true;
        else if (!strcmp(a, "--allow") && i + 1 < argc) {
            o.allow = realloc(o.allow, (o.n_allow + 1) * sizeof(char *));
            if (!o.allow) { free_allow(&o); return 1; }
            o.allow[o.n_allow++] = strdup(argv[++i]);
            if (!o.allow[o.n_allow - 1]) { free_allow(&o); return 1; }
        }
        else if (!strcmp(a, "--allow-file") && i + 1 < argc) { if (load_allow_file(&o, argv[++i])) { free_allow(&o); return 1; } }
        else if (!strcmp(a, "--allow-activation") && i + 1 < argc) {
            o.allow_activation = realloc(o.allow_activation, (o.n_allow_activation + 1) * sizeof(char *));
            if (!o.allow_activation) { free_allow(&o); free_allow_activation(&o); return 1; }
            o.allow_activation[o.n_allow_activation++] = strdup(argv[++i]);
            if (!o.allow_activation[o.n_allow_activation - 1]) { free_allow(&o); free_allow_activation(&o); return 1; }
        }
        else if (!strcmp(a, "--allow-activation-file") && i + 1 < argc) { if (load_allow_activation_file(&o, argv[++i])) { free_allow(&o); free_allow_activation(&o); return 1; } }
        else if (!strcmp(a, "--activation-dir") && i + 1 < argc) { o.activation_dir = argv[++i]; }
        else if (!strcmp(a, "--version")) { free_allow(&o); free_allow_activation(&o); free_services(&o); puts("whitecube-bus " WC_VERSION); return 0; }
        else if (!strcmp(a, "--help")) { free_allow(&o); free_allow_activation(&o); free_services(&o); usage(stdout); return 0; }
        else { free_allow(&o); free_allow_activation(&o); free_services(&o); usage(stderr); return 2; }
    }

    char defpath[4096];
    if (!sock) {
        const char *xrd = getenv("XDG_RUNTIME_DIR");
        if (!xrd || !*xrd) { fprintf(stderr, "XDG_RUNTIME_DIR is not set\n"); free_allow(&o); return 1; }
        snprintf(defpath, sizeof defpath, "%s/bus", xrd);
        sock = defpath;
    }
    if (check_parent(sock, !explicit_sock || !o.allow_insecure_dir) != 0) { free_allow(&o); return 1; }

    for (size_t i = 0; i < sizeof DEFAULT_ALLOW / sizeof DEFAULT_ALLOW[0]; i++) {
        o.allow = realloc(o.allow, (o.n_allow + 1) * sizeof(char *));
        if (!o.allow) { free_allow(&o); return 1; }
        o.allow[o.n_allow++] = strdup(DEFAULT_ALLOW[i]);
        if (!o.allow[o.n_allow - 1]) { free_allow(&o); return 1; }
    }
    if (access("/etc/whitecube/bus-allow.conf", R_OK) == 0) {
        if (load_allow_file(&o, "/etc/whitecube/bus-allow.conf")) { free_allow(&o); return 1; }
    }

    /* Load activation configuration */
    if (access("/etc/whitecube/bus-activation.conf", R_OK) == 0) {
        if (load_allow_activation_file(&o, "/etc/whitecube/bus-activation.conf")) { free_allow(&o); free_allow_activation(&o); return 1; }
    }
    load_activation_dir(&o);

    int lfd = listen_unix(sock);
    if (lfd < 0) { free_allow(&o); return 1; }
    o.socket_path = sock;

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);
    sa.sa_handler = SIG_IGN;
    sigaction(SIGPIPE, &sa, NULL);

    Bus *b = bus_new(&o, lfd);
    if (!b) { fprintf(stderr, "bus_new failed\n"); free_allow(&o); close(lfd); unlink(sock); return 1; }

    /* Emit before the filter is installed so stdio buffers are already allocated. */
    fprintf(stderr, "whitecube-bus " WC_VERSION " listening on %s (uid=%lu pid=%ld)\n",
            sock, (unsigned long)getuid(), (long)getpid());
    fflush(stderr);
    notify_systemd_ready();

    if (o.seccomp && wc_seccomp_apply(o.seccomp) != 0) {
        fprintf(stderr, "seccomp filter could not be installed\n");
        bus_destroy(b);
        free_allow(&o);
        close(lfd);
        unlink(sock);
        return 1;
    }

    int rc = bus_loop(b, &g_stop);
    bus_destroy(b);
    free_allow(&o);
    free_allow_activation(&o);
    free_services(&o);
    close(lfd);
    unlink(sock);
    fprintf(stderr, "whitecube-bus stopped\n");
    return rc == 0 ? 0 : 1;
}
