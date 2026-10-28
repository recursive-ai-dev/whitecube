// cube-status: whitecube's clickable bar widgets and app menu.
// Two layer-shell TOP surfaces sit inside the compositor's tab bar:
//   left:  [Apps] button, opens the app menu
//   right: [Wi-Fi] button (opens cube-settings --page network) and the clock,
//          which opens Quick settings (volume, brightness, Wi-Fi, lock/power)
// A third TOP surface in the far right corner is the power button: it drops
// down Lock / Suspend / Restart / Power off (the last two need a second click).
// The app menu is a full-output OVERLAY surface: category tabs, search, app
// tiles and quick buttons; a click outside the panel or Escape closes it.
// whitecube.c reserves BAR_APPS_W / BAR_STATUS_W of the tab bar for these.
// SIGUSR1 toggles the app menu (Super, Alt+Space), SIGUSR2 Quick settings.
// Settings reads and changes go through cube-settings-helper, as in cube-settings.
// cairo + pango on SHM. No X11, no dbus.
//   cube-status                     run as a session client
//   cube-status --render DIR        draw bar and menu PNGs without Wayland
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <dirent.h>
#include <locale.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <errno.h>
#include <sys/wait.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/timerfd.h>
#include <sys/signalfd.h>
#include <poll.h>
#include <linux/input-event-codes.h>
#include <cairo.h>
#include <pango/pangocairo.h>
#include <wayland-client.h>
#include <wayland-cursor.h>
#include <xkbcommon/xkbcommon.h>
#include "wlr-layer-shell-client.h"
#include "desktop-state.h"
#include "status-theme.h"
#include "bar-layout.h"
#include "native-preferences.h"
#include "native-input.h"

// Logical widths before ui_scale; must match whitecube.c.
#define APPS_W 104       // BAR_APPS_W
#define STATUS_W 300     // BAR_STATUS_W
#define POWER_W 44       // BAR_POWER_W (power button in the far right corner)
#define STATUS_RIGHT (116 + POWER_W) // workspace cubes occupy the 112 px left of the power button
#define CHAT_W 140       // BAR_CHAT_W (centered Chat button)
#define DESKTOP_CHAT_SH "/usr/bin/desktop-chat"

static struct status_theme theme;
#define C_BAR theme.bar
#define C_PANEL theme.panel
#define C_INK theme.ink
#define C_HOVER theme.hover
#define C_MUTED theme.muted
#define C_SURFACE theme.surface

#define TERMINAL "/usr/bin/whitecube-terminal"
#define SETTINGS "/usr/bin/cube-settings"
#define NET_STATUS "/run/whitecube-net/status.json"
#ifndef HELPER
#define HELPER "/usr/libexec/whitecube/cube-settings-helper"
#endif
#define SUDO "/usr/bin/sudo"
#define SUSPEND_COMMAND "/usr/bin/whitecube-suspend"
#define MAX_PROFILES 5

// ------------------------------------------------------------------ apps
enum cat { CAT_ALL, CAT_INTERNET, CAT_OFFICE, CAT_DEV, CAT_GRAPHICS, CAT_MEDIA, CAT_GAMES,
	CAT_ACCESSORIES, CAT_SYSTEM, CAT_OTHER, CAT_COUNT };
static const char *cat_names[CAT_COUNT] = { "All apps", "Internet", "Office", "Development",
	"Graphics", "Sound & Video", "Games", "Accessories", "System", "Other" };

struct app {
	char id[512], name[192], exec[1024], icon[256], search[2048], path[2048], cwd[1024];
	enum cat cat;
	bool terminal, icon_tried;
	cairo_surface_t *img;
};
#define MAX_APPS 512
static struct app apps[MAX_APPS];
static int napps;

enum { QB_SETTINGS, QB_PRIVACY, QB_WIFI, QB_TERMINAL, QB_POWER, QB_COUNT };
static const char *qb_names[QB_COUNT] = { "Settings", "Privacy", "Wi-Fi", "Terminal", "Power" };

struct rect { double x, y, w, h; };
static bool in(struct rect r, double x, double y) { return x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h; }

// Hover/hit identifiers.
#define HIT_NONE (-1)
#define HIT_TILE 0      // + filtered index
#define HIT_CAT 10000   // + category
#define HIT_QB 20000    // + quick button
#define HIT_SEARCH 30000
#define HIT_PANEL 30001
#define HIT_WIFI 40000
#define HIT_APPS 40001
#define HIT_CLOCK 40002
#define HIT_CHAT 40003
#define HIT_PWR 40004
#define HIT_PWR_PANEL 50299 // inside the dropdown, not on an item
#define HIT_PWR_ITEM 50300  // + power button (PW_*)
#define HIT_CHAT_OPEN 50010
#define HIT_CHAT_SETTINGS 50011
#define HIT_CHAT_QUIT 50012
#define HIT_QS_PANEL 50000
#define HIT_QS_MUTE 50001
#define HIT_QS_VOL 50002
#define HIT_QS_BRIGHT 50003
#define HIT_QS_WIFISET 50004
#define HIT_QS_PRIVACY 50005
#define HIT_QS_PROF 50100  // + profile
#define HIT_QS_POWER 50200 // + power button

enum { DRAG_NONE, DRAG_VOL, DRAG_BRIGHT };
enum { PW_LOCK, PW_SUSPEND, PW_RESTART, PW_OFF, PW_COUNT };
static const char *power_names[PW_COUNT] = { "Lock", "Suspend", "Restart", "Power off" };

struct panel {
	struct wl_surface *surf;
	struct zwlr_layer_surface_v1 *layer;
	int w, h;
	bool configured;
};

struct st {
	struct wl_display *dpy;
	struct wl_compositor *comp;
	struct wl_shm *shm;
	struct wl_seat *seat;
	struct zwlr_layer_shell_v1 *layer_shell;
	struct wl_pointer *pointer;
	struct wl_keyboard *keyboard;
	struct wl_cursor_theme *cursor_theme;
	struct wl_surface *cursor_surf;
	struct xkb_context *xkb;
	struct xkb_keymap *keymap;
	struct xkb_state *xkb_state;
	struct xkb_compose_state *compose;
	int repeat_fd, repeat_rate, repeat_delay;
	uint32_t repeat_key;
	struct panel apps_btn, status, menu, qs;
	struct panel chat_btn, chat;
	bool chat_open;
	struct panel pwr_btn, pwr;
	bool pwr_open;
	struct panel *hover_panel;
	double px, py;
	int hover, focus;
	bool keyboard_focus, privacy_hide_network;
	bool running, menu_open, qs_open, preview;
	double scale;
	int tab_h;
	// status
	bool clock_12h, clock_seconds;
	char clock_text[96], wifi_label[96], wifi_ssid[80];
	int wifi_bars; // -1: no Wi-Fi, 0..4
	char shown[200];
	// menu
	enum cat cat;
	char query[128];
	int filtered[MAX_APPS], nfiltered, sel;
	double scroll, max_scroll, qs_scroll, qs_max_scroll;
	int cols;
	struct rect r_panel, r_search, r_cats[CAT_COUNT], r_grid, r_qb[QB_COUNT];
	bool cat_used[CAT_COUNT];
	// quick settings
	int vol, bright, dragging, armed;
	bool vol_ok, muted, bright_ok;
	time_t armed_at;
	char battery_line[64];
	struct { char id[64], ssid[80]; } profiles[MAX_PROFILES];
	int nprofiles;
	struct rect r_qs, q_date, q_batt, q_vol_label, q_mute, q_vol, q_bright_label, q_bright, q_wifi_label,
		q_prof[MAX_PROFILES], q_prof_name[MAX_PROFILES], q_noprof, q_wifiset, q_privacy, q_power[PW_COUNT];
	double q_sep[3];
	struct rect r_chat, r_chat_open, r_chat_settings, r_chat_quit;
	struct rect r_pwr, r_pwr_item[PW_COUNT];
	struct panel notice;
	char notice_text[512];
	double notice_until;
};

static struct st *g_st;
static double monotonic_s(void) {
	struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
	return now.tv_sec + now.tv_nsec / 1e9;
}
static void status_notice(const char *format, ...) {
	char message[512];
	va_list args; va_start(args, format); vsnprintf(message, sizeof(message), format, args); va_end(args);
	const char *invalid;
	if (!g_utf8_validate(message, -1, &invalid)) message[invalid - message] = 0;
	fprintf(stderr, "cube-status: %s\n", message);
	if (g_st) {
		g_strlcpy(g_st->notice_text, message, sizeof(g_st->notice_text));
		g_st->notice_until = monotonic_s() + 12;
	}
}

static void repeat_stop(struct st *s) {
	s->repeat_key = 0;
	struct itimerspec off = {0};
	if (s->repeat_fd >= 0) timerfd_settime(s->repeat_fd, 0, &off, NULL);
}

static bool is_hot(const struct st *s, int hit) {
	return s->hover == hit || (s->keyboard_focus && s->focus == hit);
}

static void color(cairo_t *cr, const char *hex) {
	unsigned v = (unsigned)strtoul(hex, NULL, 16);
	cairo_set_source_rgb(cr, ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0);
}

static void read_file_first(const char *path, char *out, size_t n) {
	FILE *f = fopen(path, "r");
	if (!f) { out[0] = 0; return; }
	if (!fgets(out, n, f)) out[0] = 0;
	out[strcspn(out, "\r\n")] = 0;
	fclose(f);
}

// --------------------------------------------------------------- spawning
static bool job_start(int kind, const char *const argv[]);
static void launch_tracked(const char *const argv[], const char *cwd, const char *label);

// These fixed desktop commands are argv, including the historical "exec"
// prefix. No shell is needed to launch them or report exec failures.
static void spawn_shell(const char *cmd) {
    char **args = NULL;
    if (!g_shell_parse_argv(cmd, NULL, &args, NULL) || !args || !args[0]) {
        status_notice("Cannot launch: invalid command"); return;
    }
    const char *const *argv = (const char *const *)args;
    if (!strcmp(args[0], "exec")) argv++;
    if (*argv) launch_tracked(argv, NULL, *argv);
    g_strfreev(args);
}

static void launch_settings(const char *page) {
    char path[1024];
    const char *home = getenv("HOME");
    int n = home ? snprintf(path, sizeof(path), "%s/.local/bin/cube-settings", home) : -1;
    if (n < 0 || (size_t)n >= sizeof(path) || access(path, X_OK))
        snprintf(path, sizeof(path), "%s", SETTINGS);
    const char *args[] = {path, page ? "--page" : NULL, page, NULL};
    launch_tracked(args, NULL, "Settings");
}

static void quick_launch(int button) {
    if (button == QB_TERMINAL) spawn_shell("exec " TERMINAL);
    else launch_settings(button == QB_PRIVACY ? "privacy" : button == QB_WIFI ? "network" :
        button == QB_POWER ? "power" : NULL);
}

static char **app_argv(const struct app *a) {
    char **parsed = NULL;
    if (!g_shell_parse_argv(a->exec, NULL, &parsed, NULL)) return NULL;
    GPtrArray *args = g_ptr_array_new_with_free_func(g_free);
    if (a->terminal) {
        g_ptr_array_add(args, g_strdup(TERMINAL));
        g_ptr_array_add(args, g_strdup("--"));
    }
    for (int i = 0; parsed[i]; i++) {
        if (!strcmp(parsed[i], "%i")) {
            if (a->icon[0]) {
                g_ptr_array_add(args, g_strdup("--icon"));
                g_ptr_array_add(args, g_strdup(a->icon));
            }
            continue;
        }
        if (!strcmp(parsed[i], "%f") || !strcmp(parsed[i], "%F") ||
            !strcmp(parsed[i], "%u") || !strcmp(parsed[i], "%U")) continue;
        GString *value = g_string_new(NULL);
        for (const char *c = parsed[i]; *c; c++) {
            if (*c == '%' && c[1]) {
                c++;
                if (*c == '%') g_string_append_c(value, '%');
                else if (*c == 'c') g_string_append(value, a->name);
                else if (*c == 'k') g_string_append(value, a->path);
                else { g_string_free(value, TRUE); value = NULL; break; }
            } else g_string_append_c(value, *c);
        }
        if (!value) { g_strfreev(parsed); g_ptr_array_free(args, TRUE); return NULL; }
        g_ptr_array_add(args, g_string_free(value, FALSE));
    }
    g_strfreev(parsed);
    int first = a->terminal ? 2 : 0;
    if (args->len <= (unsigned)first || !((char *)args->pdata[first])[0]) {
        g_ptr_array_free(args, TRUE); return NULL;
    }
    g_ptr_array_add(args, NULL);
    return (char **)g_ptr_array_free(args, FALSE);
}

static void launch_app(const struct app *a) {
    char **args = app_argv(a);
    if (!args) { status_notice("Cannot launch %s: invalid desktop command", a->name); return; }
    launch_tracked((const char *const *)args, a->cwd, a->name);
    g_strfreev(args);
}

// ------------------------------------------------------------------- jobs
// Helper results, diagnostics and exec handshakes are bounded and nonblocking.
enum { JOB_SOUND, JOB_POWER, JOB_NET, JOB_SET_SOUND, JOB_ACTION, JOB_LAUNCH, JOB_SUSPEND };
struct job {
    bool active, reaped, timed_out; int fd, err, kind, status; pid_t pid;
    double deadline; char buf[16384], ebuf[2048], label[96], operation[32];
    size_t len, elen;
};
#define MAX_JOBS 8
static struct job jobs[MAX_JOBS];
static char pending_volume[16], pending_brightness[16];
static void job_parse(struct st *s, struct job *j);

static struct job *job_slot(void) {
    for (int i = 0; i < MAX_JOBS; i++) if (!jobs[i].active) return &jobs[i];
    status_notice("Desktop is busy; wait for the current changes and try again");
    return NULL;
}

static bool job_start(int kind, const char *const argv[]) {
    struct job *j = job_slot();
    if (!j) return false;
    int out[2], err[2];
    if (pipe2(out, O_CLOEXEC) < 0) { status_notice("Cannot start task: %s", strerror(errno)); return false; }
    if (pipe2(err, O_CLOEXEC) < 0) { close(out[0]); close(out[1]); status_notice("Cannot start task: %s", strerror(errno)); return false; }
    pid_t pid = fork();
    if (pid == 0) {
        setsid();
        sigset_t none; sigemptyset(&none); sigprocmask(SIG_SETMASK, &none, NULL);
        signal(SIGCHLD, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        int null = open("/dev/null", O_RDONLY);
        dup2(null, 0); dup2(out[1], 1); dup2(err[1], 2);
        execv(argv[0], (char *const *)argv);
        dprintf(2, "Cannot run %s: %s\n", argv[0], strerror(errno)); _exit(127);
    }
    close(out[1]); close(err[1]);
    if (pid < 0) { close(out[0]); close(err[0]); status_notice("Cannot start task: %s", strerror(errno)); return false; }
    fcntl(out[0], F_SETFL, O_NONBLOCK); fcntl(err[0], F_SETFL, O_NONBLOCK);
    *j = (struct job){.active = true, .pid = pid, .fd = out[0], .err = err[0], .kind = kind,
        .deadline = monotonic_s() + (kind == JOB_SUSPEND ? 65 : 35)};
    g_strlcpy(j->label, kind == JOB_SUSPEND ? "Suspend" : "Settings change", sizeof(j->label));
    return true;
}

static void launch_tracked(const char *const argv[], const char *cwd, const char *label) {
    struct job *j = job_slot();
    if (!j) return;
    int errors[2];
    if (pipe2(errors, O_CLOEXEC) < 0) { status_notice("Cannot launch %s: %s", label, strerror(errno)); return; }
    pid_t pid = fork();
    if (pid == 0) {
        close(errors[0]); setsid();
        sigset_t none; sigemptyset(&none); sigprocmask(SIG_SETMASK, &none, NULL);
        signal(SIGCHLD, SIG_DFL);
        if (cwd && cwd[0] && chdir(cwd)) {
            dprintf(errors[1], "Application folder: %s", strerror(errno)); _exit(126);
        }
        execvp(argv[0], (char *const *)argv);
        dprintf(errors[1], "%s: %s", argv[0], strerror(errno)); _exit(127);
    }
    close(errors[1]);
    if (pid < 0) { close(errors[0]); status_notice("Cannot launch %s: %s", label, strerror(errno)); return; }
    fcntl(errors[0], F_SETFL, O_NONBLOCK);
    *j = (struct job){.active = true, .pid = pid, .fd = errors[0], .err = -1,
        .kind = JOB_LAUNCH, .deadline = monotonic_s() + 10};
    g_strlcpy(j->label, label, sizeof(j->label));
}

static void helper(int kind, bool root, const char *a, const char *b) {
    char *pending = !strcmp(a, "volume") ? pending_volume : !strcmp(a, "brightness") ? pending_brightness : NULL;
    if (pending && b) {
        for (int i = 0; i < MAX_JOBS; i++) if (jobs[i].active && !strcmp(jobs[i].operation, a)) {
            g_strlcpy(pending, b, 16); return;
        }
    }
    // Reopening panels must not queue duplicate reads behind slow hardware.
    if (!b && kind <= JOB_NET) for (int i = 0; i < MAX_JOBS; i++)
        if (jobs[i].active && jobs[i].kind == kind) return;
    const char *argv[8]; int n = 0;
    if (root) { argv[n++] = SUDO; argv[n++] = "-n"; argv[n++] = "--"; }
    argv[n++] = HELPER; argv[n++] = a;
    if (b) argv[n++] = b;
    argv[n] = NULL;
    if (job_start(kind, argv)) {
        for (int i = 0; i < MAX_JOBS; i++) if (jobs[i].active && !jobs[i].operation[0] && jobs[i].kind == kind) {
            g_strlcpy(jobs[i].operation, a, sizeof(jobs[i].operation));
            g_strlcpy(jobs[i].label, a, sizeof(jobs[i].label)); break;
        }
    }
}

static void job_drain(int *fd, char *buffer, size_t *length, size_t capacity, bool tail) {
    if (*fd < 0) return;
    char chunk[4096]; ssize_t n;
    size_t drained = 0;
    while ((n = read(*fd, chunk, sizeof(chunk))) > 0) {
        size_t take = (size_t)n;
        if (tail && take > capacity - 1 - *length) {
            size_t drop = take - (capacity - 1 - *length);
            if (drop > *length) drop = *length;
            memmove(buffer, buffer + drop, *length - drop); *length -= drop;
        }
        if (take > capacity - 1 - *length) take = capacity - 1 - *length;
        memcpy(buffer + *length, chunk + (tail ? (size_t)n - take : 0), take); *length += take;
        drained += (size_t)n;
        if (drained >= 65536) { buffer[*length] = 0; return; }
    }
    buffer[*length] = 0;
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) { close(*fd); *fd = -1; }
}

static void jobs_pump(struct st *s) {
    for (int i = 0; i < MAX_JOBS; i++) if (jobs[i].active) {
        struct job *j = &jobs[i];
        job_drain(&j->fd, j->buf, &j->len, sizeof(j->buf), false);
        job_drain(&j->err, j->ebuf, &j->elen, sizeof(j->ebuf), true);
        if (monotonic_s() >= j->deadline && !j->timed_out) {
            j->timed_out = true;
            // Descendants may retain pipes after their leader was reaped and
            // leave its group. Both numeric PID and PGID can then be reused.
            if (!j->reaped) { kill(-j->pid, SIGKILL); kill(j->pid, SIGKILL); }
            if (j->fd >= 0) close(j->fd);
            if (j->err >= 0) close(j->err);
            j->fd = j->err = -1;
        }
    }
    int status; pid_t pid;
    while ((pid = waitpid(-1, &status, WNOHANG)) > 0)
        for (int i = 0; i < MAX_JOBS; i++) if (jobs[i].active && jobs[i].pid == pid) {
            jobs[i].reaped = true; jobs[i].status = status; break;
        }
    for (int i = 0; i < MAX_JOBS; i++) {
        struct job *j = &jobs[i];
        if (!j->active || j->fd >= 0 || j->err >= 0 || (j->kind != JOB_LAUNCH && !j->reaped)) continue;
        bool ok = !j->timed_out && (j->kind == JOB_LAUNCH ? !j->len : WIFEXITED(j->status) && WEXITSTATUS(j->status) == 0);
        int kind = j->kind;
        char operation[32]; g_strlcpy(operation, j->operation, sizeof(operation));
        j->active = false;
        if (!ok) {
            char *detail = j->kind == JOB_LAUNCH ? j->buf : j->ebuf;
            size_t length = strlen(detail);
            while (length && (unsigned char)detail[length - 1] <= ' ') detail[--length] = 0;
            char *line = strrchr(detail, '\n');
            if (line) detail = line + 1;
            if (strlen(detail) > 350) {
                detail += strlen(detail) - 350;
                while (((unsigned char)*detail & 0xc0) == 0x80) detail++;
            }
            for (char *p = detail; *p; p++) if ((unsigned char)*p < 32) *p = ' ';
            char *valid = g_utf8_make_valid(detail, -1);
            status_notice("%s failed: %s", j->label, j->timed_out ? "timed out; try again" : *valid ? valid : "operation was rejected");
            g_free(valid);
        } else if (kind <= JOB_NET) job_parse(s, j);
        char *pending = !strcmp(operation, "volume") ? pending_volume : !strcmp(operation, "brightness") ? pending_brightness : NULL;
        if (pending && pending[0]) {
            char value[16]; g_strlcpy(value, pending, sizeof(value)); pending[0] = 0;
            helper(!strcmp(operation, "volume") ? JOB_SET_SOUND : JOB_ACTION, !strcmp(operation, "brightness"), operation, value);
        } else if (kind == JOB_SET_SOUND) helper(JOB_SOUND, false, "sound", NULL);
        else if (!strcmp(operation, "brightness")) helper(JOB_POWER, false, "power", NULL);
    }
}

// ------------------------------------------------------- desktop entries
static bool list_has(const char *list, const char *word) {
	char buf[512]; snprintf(buf, sizeof(buf), "%s", list);
	for (char *save = NULL, *t = strtok_r(buf, ";", &save); t; t = strtok_r(NULL, ";", &save))
		if (!strcasecmp(t, word)) return true;
	return false;
}

static bool in_path(const char *prog) {
	if (prog[0] == '/') return g_file_test(prog, G_FILE_TEST_IS_REGULAR) && access(prog, X_OK) == 0;
	const char *path = getenv("PATH");
	char buf[2048]; snprintf(buf, sizeof(buf), "%s", path && *path ? path : "/usr/bin:/usr/bin:/bin");
	for (char *save = NULL, *d = strtok_r(buf, ":", &save); d; d = strtok_r(NULL, ":", &save)) {
		char full[1024]; snprintf(full, sizeof(full), "%s/%s", d, prog);
		if (g_file_test(full, G_FILE_TEST_IS_REGULAR) && access(full, X_OK) == 0) return true;
	}
	return false;
}

static enum cat categorize(const char *cats) {
	if (list_has(cats, "Game")) return CAT_GAMES;
	if (list_has(cats, "Development")) return CAT_DEV;
	if (list_has(cats, "Network") || list_has(cats, "WebBrowser") || list_has(cats, "Email")) return CAT_INTERNET;
	if (list_has(cats, "Office")) return CAT_OFFICE;
	if (list_has(cats, "Graphics")) return CAT_GRAPHICS;
	if (list_has(cats, "AudioVideo") || list_has(cats, "Audio") || list_has(cats, "Video")) return CAT_MEDIA;
	if (list_has(cats, "Settings") || list_has(cats, "System")) return CAT_SYSTEM;
	if (list_has(cats, "Utility") || list_has(cats, "Accessories")) return CAT_ACCESSORIES;
	return CAT_OTHER;
}

static bool seen_id(const char *id) {
	for (int i = 0; i < napps; i++) if (!strcmp(apps[i].id, id)) return true;
	return false;
}

static void entry_value(GKeyFile *key, const char *name, bool localized, char *out, size_t cap) {
    char *value = localized ? g_key_file_get_locale_string(key, "Desktop Entry", name, NULL, NULL)
                            : g_key_file_get_string(key, "Desktop Entry", name, NULL);
    if (value && g_utf8_validate(value, -1, NULL)) {
        g_strlcpy(out, value, cap);
        const char *invalid;
        if (!g_utf8_validate(out, -1, &invalid)) out[invalid - out] = 0;
    }
    g_free(value);
}

// Hidden/NoDisplay entries still claim their id so they mask lower-priority dirs.
static void load_entry(const char *dir, const char *file) {
    if (napps >= MAX_APPS) return;
    struct app a = {0};
    if (strlen(file) >= sizeof(a.id)) return;
    g_strlcpy(a.id, file, sizeof(a.id));
    for (char *c = a.id; *c; c++) if (*c == '/') *c = '-';
    if (seen_id(a.id)) return;
    if (snprintf(a.path, sizeof(a.path), "%s/%s", dir, file) >= (int)sizeof(a.path)) return;
    struct stat st;
    if (stat(a.path, &st) || !S_ISREG(st.st_mode) || st.st_size > 1048576) return;
    GKeyFile *key = g_key_file_new();
    if (!g_key_file_load_from_file(key, a.path, G_KEY_FILE_NONE, NULL)) { g_key_file_free(key); return; }
    char type[32] = "", cats[512] = "", only[256] = "", notin[256] = "", tryexec[512] = "";
    char generic[192] = "", keywords[512] = "", comment[512] = "";
    entry_value(key, "Name", true, a.name, sizeof(a.name));
    entry_value(key, "GenericName", true, generic, sizeof(generic));
    entry_value(key, "Keywords", true, keywords, sizeof(keywords));
    entry_value(key, "Comment", true, comment, sizeof(comment));
    entry_value(key, "Exec", false, a.exec, sizeof(a.exec));
    entry_value(key, "Icon", false, a.icon, sizeof(a.icon));
    entry_value(key, "Path", false, a.cwd, sizeof(a.cwd));
    entry_value(key, "Type", false, type, sizeof(type));
    entry_value(key, "Categories", false, cats, sizeof(cats));
    entry_value(key, "OnlyShowIn", false, only, sizeof(only));
    entry_value(key, "NotShowIn", false, notin, sizeof(notin));
    entry_value(key, "TryExec", false, tryexec, sizeof(tryexec));
    a.terminal = g_key_file_get_boolean(key, "Desktop Entry", "Terminal", NULL);
    bool hidden = g_key_file_get_boolean(key, "Desktop Entry", "Hidden", NULL) ||
                  g_key_file_get_boolean(key, "Desktop Entry", "NoDisplay", NULL);
    g_key_file_free(key);
    if (hidden || strcmp(type, "Application") || !a.name[0] || !a.exec[0] ||
        (only[0] && !list_has(only, "whitecube")) || list_has(notin, "whitecube") ||
        (tryexec[0] && !in_path(tryexec))) a.name[0] = 0;
    a.cat = categorize(cats);
    snprintf(a.search, sizeof(a.search), "%s %s %s %s %s", a.name, generic, keywords, comment, a.id);
    apps[napps++] = a;
}

static int app_cmp(const void *x, const void *y) {
    int order = g_utf8_collate(((const struct app *)x)->name, ((const struct app *)y)->name);
    return order ? order : strcmp(((const struct app *)x)->id, ((const struct app *)y)->id);
}

static void scan_apps(const char *root, const char *relative, int depth) {
    if (depth > 4 || napps >= MAX_APPS) return;
    char path[2048];
    if (snprintf(path, sizeof(path), "%s/%s", root, relative) >= (int)sizeof(path)) return;
    DIR *directory = opendir(path);
    if (!directory) return;
    struct dirent *entry;
    while ((entry = readdir(directory))) {
        if (entry->d_name[0] == '.') continue;
        char child[1024], full[2048];
        if (snprintf(child, sizeof(child), "%s%s%s", relative, *relative ? "/" : "", entry->d_name) >= (int)sizeof(child)) continue;
        if (snprintf(full, sizeof(full), "%s/%s", root, child) >= (int)sizeof(full)) continue;
        struct stat st;
        if (lstat(full, &st)) continue;
        if (S_ISDIR(st.st_mode)) scan_apps(root, child, depth + 1);
        else if (g_str_has_suffix(entry->d_name, ".desktop")) load_entry(root, child);
    }
    closedir(directory);
}

static void load_apps(void) {
    for (int i = 0; i < napps; i++) if (apps[i].img) cairo_surface_destroy(apps[i].img);
    napps = 0;
    const char *home = getenv("HOME"), *data = getenv("XDG_DATA_HOME");
    char path[2048];
    if (data && data[0] == '/') snprintf(path, sizeof(path), "%s/applications", data);
    else snprintf(path, sizeof(path), "%s/.local/share/applications", home ? home : "");
    if ((data && data[0] == '/') || home) scan_apps(path, "", 0);
    if (home) {
        snprintf(path, sizeof(path), "%s/.local/share/flatpak/exports/share/applications", home);
        scan_apps(path, "", 0);
    }
    const char *data_dirs = getenv("XDG_DATA_DIRS");
    char **dirs = g_strsplit(data_dirs && *data_dirs ? data_dirs : "/usr/local/share:/usr/share", ":", -1);
    for (int i = 0; dirs[i]; i++) if (dirs[i][0] == '/') {
        snprintf(path, sizeof(path), "%s/applications", dirs[i]); scan_apps(path, "", 0);
    }
    g_strfreev(dirs);
    scan_apps("/var/lib/flatpak/exports/share/applications", "", 0);
    scan_apps("/var/lib/snapd/desktop/applications", "", 0);
    int keep = 0;
    for (int i = 0; i < napps; i++) if (apps[i].name[0]) apps[keep++] = apps[i];
    napps = keep;
    qsort(apps, napps, sizeof(*apps), app_cmp);
}

static cairo_surface_t *load_png(const char *path) {
	if (access(path, R_OK)) return NULL;
	cairo_surface_t *s = cairo_image_surface_create_from_png(path);
	if (cairo_surface_status(s) != CAIRO_STATUS_SUCCESS) { cairo_surface_destroy(s); return NULL; }
	return s;
}

// PNG icons only (no SVG loader on this system); others get a drawn cube.
static cairo_surface_t *app_icon(struct app *a) {
	if (a->icon_tried) return a->img;
	a->icon_tried = true;
	if (!a->icon[0]) return NULL;
	if (a->icon[0] == '/') return a->img = load_png(a->icon);
	char name[160]; snprintf(name, sizeof(name), "%s", a->icon);
	size_t n = strlen(name);
	if (n > 4 && (!strcmp(name + n - 4, ".png") || !strcmp(name + n - 4, ".xpm") || !strcmp(name + n - 4, ".svg"))) name[n - 4] = 0;
	const char *home = getenv("HOME");
	const char *bases[] = { "/usr/share/icons/hicolor", "/usr/local/share/icons/hicolor",
		"/var/lib/flatpak/exports/share/icons/hicolor", NULL };
	const char *sizes[] = { "48x48", "64x64", "128x128", "32x32", "256x256", "24x24", "22x22" };
	char path[1024];
	if (home) {
		for (size_t s = 0; s < sizeof(sizes) / sizeof(*sizes); s++) {
			snprintf(path, sizeof(path), "%s/.local/share/icons/hicolor/%s/apps/%s.png", home, sizes[s], name);
			if ((a->img = load_png(path))) return a->img;
		}
	}
	for (int b = 0; bases[b]; b++)
		for (size_t s = 0; s < sizeof(sizes) / sizeof(*sizes); s++) {
			snprintf(path, sizeof(path), "%s/%s/apps/%s.png", bases[b], sizes[s], name);
			if ((a->img = load_png(path))) return a->img;
		}
	snprintf(path, sizeof(path), "/usr/share/pixmaps/%s.png", name);
	return a->img = load_png(path);
}

static bool search_matches(const char *haystack, const char *query) {
    char *normalized = g_utf8_normalize(haystack, -1, G_NORMALIZE_ALL_COMPOSE);
    char *folded = normalized ? g_utf8_casefold(normalized, -1) : NULL;
    g_free(normalized);
    normalized = g_utf8_normalize(query, -1, G_NORMALIZE_ALL_COMPOSE);
    char *needle = normalized ? g_utf8_casefold(normalized, -1) : NULL;
    g_free(normalized);
    if (!folded || !needle) { g_free(folded); g_free(needle); return false; }
    char **words = g_strsplit_set(needle, " \t", -1);
    bool matched = true;
    for (int i = 0; words[i]; i++) if (*words[i] && !strstr(folded, words[i])) { matched = false; break; }
    g_strfreev(words); g_free(folded); g_free(needle);
    return matched;
}

static void filter_apps(struct st *s) {
    s->nfiltered = 0;
    for (int i = 0; i < napps; i++) {
        if (s->query[0]) { if (!search_matches(apps[i].search, s->query)) continue; }
        else if (s->cat != CAT_ALL && apps[i].cat != s->cat) continue;
        s->filtered[s->nfiltered++] = i;
    }
    if (s->sel >= s->nfiltered) s->sel = s->nfiltered ? s->nfiltered - 1 : 0;
    if (s->sel < 0) s->sel = 0;
}

// ------------------------------------------------------------- drawing
static void text(cairo_t *cr, const char *str, double px, bool bold, double x, double y, double w, double h,
		bool center, int lines, const char *hex) {
	PangoLayout *lo = pango_cairo_create_layout(cr);
	PangoFontDescription *fd = pango_font_description_from_string(bold ? "DejaVu Sans Bold" : "DejaVu Sans");
	pango_font_description_set_absolute_size(fd, px * PANGO_SCALE);
	pango_layout_set_font_description(lo, fd);
	pango_font_description_free(fd);
	pango_layout_set_text(lo, str, -1);
	if (w > 0) {
		pango_layout_set_width(lo, (int)(w * PANGO_SCALE));
		pango_layout_set_ellipsize(lo, PANGO_ELLIPSIZE_END);
		pango_layout_set_alignment(lo, center ? PANGO_ALIGN_CENTER : PANGO_ALIGN_LEFT);
		if (lines > 1) { pango_layout_set_wrap(lo, PANGO_WRAP_WORD_CHAR); pango_layout_set_height(lo, -lines); }
	}
	int tw, th;
	pango_layout_get_pixel_size(lo, &tw, &th);
	color(cr, hex);
	cairo_move_to(cr, x, h > 0 ? y + (h - th) / 2 : y);
	pango_cairo_show_layout(cr, lo);
	g_object_unref(lo);
}

// Tab-style box: 2 px ink border around a filled cube (matches whitecube tabs).
static void box(cairo_t *cr, struct rect r, double s, const char *border, const char *fill) {
	color(cr, border);
	cairo_rectangle(cr, r.x, r.y, r.w, r.h);
	if (!fill) { cairo_set_line_width(cr, s); cairo_stroke(cr); return; }
	cairo_fill(cr);
	double b = 2 * s < 1 ? 1 : 2 * s;
	color(cr, fill);
	cairo_rectangle(cr, r.x + b, r.y + b, r.w - 2 * b, r.h - 2 * b);
	cairo_fill(cr);
}

// White app cubes keep dark initials and edges in every desktop theme.
// These colors apply only to fallback app icons, not the surrounding menu.
static void cube_icon(cairo_t *cr, double x, double y, double size, const char *name, double s) {
	const char *ink = "0d0d0d";
	double d = size * 0.24, f = size - d;
	cairo_set_line_width(cr, 1.5 * s);
	cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND);
	cairo_move_to(cr, x, y + d); cairo_line_to(cr, x + d, y); cairo_line_to(cr, x + d + f, y);
	cairo_line_to(cr, x + f, y + d); cairo_close_path(cr);
	color(cr, "f5f5f5"); cairo_fill_preserve(cr); color(cr, ink); cairo_stroke(cr);
	cairo_move_to(cr, x + f, y + d); cairo_line_to(cr, x + f + d, y); cairo_line_to(cr, x + f + d, y + f);
	cairo_line_to(cr, x + f, y + d + f); cairo_close_path(cr);
	color(cr, "d9d9d9"); cairo_fill_preserve(cr); color(cr, ink); cairo_stroke(cr);
	cairo_rectangle(cr, x, y + d, f, f);
	color(cr, "ffffff"); cairo_fill_preserve(cr); color(cr, ink); cairo_stroke(cr);
	char initial[8] = "?";
	if (name[0]) { int n = g_utf8_next_char(name) - name; snprintf(initial, sizeof(initial), "%.*s", n, name); }
	char *up = g_utf8_strup(initial, -1);
	text(cr, up, f * 0.55, true, x, y + d, f, f, true, 1, ink);
	g_free(up);
}

static void draw_icon(cairo_t *cr, struct app *a, double x, double y, double size, double s) {
	cairo_surface_t *img = app_icon(a);
	if (!img) { cube_icon(cr, x, y, size, a->name, s); return; }
	int iw = cairo_image_surface_get_width(img), ih = cairo_image_surface_get_height(img);
	double k = size / (iw > ih ? iw : ih);
	cairo_save(cr);
	cairo_translate(cr, x + (size - iw * k) / 2, y + (size - ih * k) / 2);
	cairo_scale(cr, k, k);
	cairo_set_source_surface(cr, img, 0, 0);
	cairo_pattern_set_filter(cairo_get_source(cr), CAIRO_FILTER_GOOD);
	cairo_paint(cr);
	cairo_restore(cr);
}

static void wifi_glyph(cairo_t *cr, double x, double y, double h, int bars, double s) {
	double bw = 4 * s, gap = 2 * s;
	for (int i = 0; i < 4; i++) {
		double bh = h * (i + 1) / 4;
		cairo_rectangle(cr, x + i * (bw + gap), y + h - bh, bw, bh);
		color(cr, i < bars ? C_INK : C_MUTED);
		cairo_fill(cr);
	}
	if (bars < 0) { // no Wi-Fi: strike through
		cairo_set_line_width(cr, 1.6 * s); color(cr, C_INK);
		cairo_move_to(cr, x - s, y + h + s); cairo_line_to(cr, x + 4 * (bw + gap), y - s); cairo_stroke(cr);
	}
}

static void draw_apps_btn(struct st *s, cairo_t *cr, int w, int h) {
	double k = s->scale;
	color(cr, C_BAR); cairo_rectangle(cr, 0, 0, w, h); cairo_fill(cr);
	struct rect r = { 8 * k, 6 * k, w - 8 * k, h - 12 * k };
	bool hot = s->menu_open || (s->hover_panel == &s->apps_btn && s->hover == HIT_APPS);
	box(cr, r, k, C_INK, s->menu_open ? C_INK : hot ? C_HOVER : C_SURFACE);
	const char *ink = s->menu_open ? C_SURFACE : C_INK;
	double g = 6 * k, gx = r.x + 12 * k, gy = r.y + (r.h - 2 * g - 2 * k) / 2;
	color(cr, ink);
	for (int i = 0; i < 4; i++) cairo_rectangle(cr, gx + (i % 2) * (g + 2 * k), gy + (i / 2) * (g + 2 * k), g, g);
	cairo_fill(cr);
	text(cr, "Apps", 14 * k, true, gx + 2 * g + 10 * k, r.y, 0, r.h, false, 1, ink);
}

static struct rect wifi_rect(struct st *s, int w, int h) {
	double width = 156 * s->scale;
	if (width > w * .52) width = w * .52;
	return (struct rect){ 0, 6 * s->scale, width, h - 12 * s->scale };
}
static struct rect clock_rect(struct st *s, int w, int h) {
	struct rect wr = wifi_rect(s, w, h);
	return (struct rect){ wr.x + wr.w + 6 * s->scale, wr.y, w - wr.w - 6 * s->scale, wr.h };
}

static void draw_status(struct st *s, cairo_t *cr, int w, int h) {
	double k = s->scale;
	color(cr, C_BAR); cairo_rectangle(cr, 0, 0, w, h); cairo_fill(cr);
	struct rect wr = wifi_rect(s, w, h);
	bool hot = s->hover_panel == &s->status && s->hover == HIT_WIFI;
	box(cr, wr, k, C_INK, hot ? C_HOVER : C_SURFACE);
	double gh = 14 * k;
	if (wr.w >= 28 * k) wifi_glyph(cr, wr.x + 8 * k, wr.y + (wr.h - gh) / 2, gh, s->wifi_bars, k);
	if (wr.w > 50 * k) text(cr, s->wifi_label, 13 * k, false, wr.x + 38 * k, wr.y, wr.w - 46 * k, wr.h, false, 1, C_INK);
	struct rect cr2 = clock_rect(s, w, h);
	bool chot = s->hover_panel == &s->status && s->hover == HIT_CLOCK;
	box(cr, cr2, k, C_INK, s->qs_open ? C_INK : chot ? C_HOVER : C_SURFACE);
	char compact_clock[32]; snprintf(compact_clock, sizeof(compact_clock), "%.*s", (int)strcspn(s->clock_text, " "), s->clock_text);
	if (cr2.w > 8 * k) text(cr, cr2.w < 125 * k ? compact_clock : s->clock_text, 13 * k, false,
		cr2.x + 4 * k, cr2.y, cr2.w - 8 * k, cr2.h, true, 1, s->qs_open ? C_SURFACE : C_INK);
}

// --------------------------------------------------------- chat dropdown
static void draw_chat_btn(struct st *s, cairo_t *cr, int w, int h) {
	double k = s->scale;
	struct wc_bar_layout bar = wc_bar_layout(w, k);
	if (!s->preview) {
		cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
		cairo_set_source_rgba(cr, 0, 0, 0, 0); cairo_paint(cr);
		cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
	}
	color(cr, C_BAR); cairo_rectangle(cr, bar.chat_x, 0, bar.chat_width, h); cairo_fill(cr);
	struct rect r = { bar.chat_x + 4 * k, 6 * k, bar.chat_width - 8 * k, h - 12 * k };
	bool hot = s->chat_open || (s->hover_panel == &s->chat_btn && s->hover == HIT_CHAT);
	box(cr, r, k, C_INK, s->chat_open ? C_INK : hot ? C_HOVER : C_SURFACE);
	text(cr, "Chat", 13 * k, true, r.x, r.y, r.w, r.h, true, 1, s->chat_open ? C_SURFACE : C_INK);
}

static void layout_chat(struct st *s, int W) {
	double k = s->scale, pad = 16 * k, pw = 360 * k, top = s->tab_h * k + 4 * k;
	if (pw > W - 16 * k) pw = W - 16 * k;
	struct wc_bar_layout bar = wc_bar_layout(W, k);
	double px = bar.chat_x + bar.chat_width - pw;
	if (px < 8 * k) px = 8 * k;
	double x = px + pad, w = pw - 2 * pad, y = top + pad + 30 * k; // 30: title row
	s->r_chat_open = (struct rect){ x, y, w, 34 * k }; y += 42 * k;
	s->r_chat_settings = (struct rect){ x, y, w, 34 * k }; y += 42 * k;
	s->r_chat_quit = (struct rect){ x, y, w, 34 * k }; y += 34 * k + pad;
	s->r_chat = (struct rect){ px, top, pw, y - top };
}

static void draw_chat(struct st *s, cairo_t *cr, int W, int H) {
	double k = s->scale;
	layout_chat(s, W);
	if (!s->preview) {
		cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
		cairo_set_source_rgba(cr, 0, 0, 0, 0); cairo_paint(cr);
		cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
	}
	cairo_set_source_rgba(cr, 0.05, 0.05, 0.05, 0.18);
	cairo_rectangle(cr, 0, s->tab_h * k, W, H - s->tab_h * k); cairo_fill(cr);
	struct rect p = s->r_chat;
	cairo_set_source_rgba(cr, 0, 0, 0, .32); cairo_rectangle(cr, p.x + 6 * k, p.y + 6 * k, p.w, p.h); cairo_fill(cr);
	box(cr, p, k, C_INK, C_PANEL);
	text(cr, "Desktop Chat", 14 * k, true, p.x + 16 * k, p.y + 14 * k, p.w - 32 * k, 22 * k, false, 1, C_INK);
	bool ho = is_hot(s, HIT_CHAT_OPEN), hs = is_hot(s, HIT_CHAT_SETTINGS), hq = is_hot(s, HIT_CHAT_QUIT);
	box(cr, s->r_chat_open, k, C_INK, ho ? C_HOVER : C_SURFACE);
	text(cr, "Open Chat", 13 * k, true, s->r_chat_open.x, s->r_chat_open.y, s->r_chat_open.w, s->r_chat_open.h, true, 1, C_INK);
	box(cr, s->r_chat_settings, k, C_INK, hs ? C_HOVER : C_SURFACE);
	text(cr, "Chat Settings", 13 * k, true, s->r_chat_settings.x, s->r_chat_settings.y, s->r_chat_settings.w, s->r_chat_settings.h, true, 1, C_INK);
	box(cr, s->r_chat_quit, k, C_INK, hq ? C_HOVER : C_SURFACE);
	text(cr, "Quit Chat", 13 * k, true, s->r_chat_quit.x, s->r_chat_quit.y, s->r_chat_quit.w, s->r_chat_quit.h, true, 1, C_INK);
}

// ------------------------------------------------------- power dropdown
// The IEC power symbol: a ring open at the top with a bar through the gap.
static void power_glyph(cairo_t *cr, double cx, double cy, double r, double s, const char *ink) {
	cairo_set_line_width(cr, 2 * s);
	cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND);
	color(cr, ink);
	cairo_new_sub_path(cr);
	cairo_arc(cr, cx, cy + s, r, -55 * G_PI / 180, 235 * G_PI / 180);
	cairo_stroke(cr);
	cairo_move_to(cr, cx, cy - r - s); cairo_line_to(cr, cx, cy + s);
	cairo_stroke(cr);
}

static void draw_pwr_btn(struct st *s, cairo_t *cr, int w, int h) {
	double k = s->scale;
	color(cr, C_BAR); cairo_rectangle(cr, 0, 0, w, h); cairo_fill(cr);
	struct rect r = { 2 * k, 6 * k, w - 10 * k, h - 12 * k }; // 8 px kept clear at the screen edge
	bool hot = s->pwr_open || (s->hover_panel == &s->pwr_btn && s->hover == HIT_PWR);
	box(cr, r, k, C_INK, s->pwr_open ? C_INK : hot ? C_HOVER : C_SURFACE);
	power_glyph(cr, r.x + r.w / 2, r.y + r.h / 2, 7 * k, k, s->pwr_open ? C_SURFACE : C_INK);
}

static void layout_pwr(struct st *s, int W) {
	double k = s->scale, pad = 16 * k, pw = 230 * k, top = s->tab_h * k + 4 * k;
	double px = W - 8 * k - pw; // right edge lines up with the button
	if (px < 8 * k) px = 8 * k;
	double x = px + pad, w = pw - 2 * pad, y = top + pad + 30 * k; // 30: title row
	for (int i = 0; i < PW_COUNT; i++) { s->r_pwr_item[i] = (struct rect){ x, y, w, 36 * k }; y += 44 * k; }
	y += pad - 8 * k; // the last row has no gap after it
	s->r_pwr = (struct rect){ px, top, pw, y - top };
}

static void draw_pwr(struct st *s, cairo_t *cr, int W, int H) {
	double k = s->scale;
	layout_pwr(s, W);
	if (!s->preview) {
		cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
		cairo_set_source_rgba(cr, 0, 0, 0, 0); cairo_paint(cr);
		cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
	}
	cairo_set_source_rgba(cr, 0.05, 0.05, 0.05, 0.18);
	cairo_rectangle(cr, 0, s->tab_h * k, W, H - s->tab_h * k); cairo_fill(cr);
	struct rect p = s->r_pwr;
	cairo_set_source_rgba(cr, 0, 0, 0, .32); cairo_rectangle(cr, p.x + 6 * k, p.y + 6 * k, p.w, p.h); cairo_fill(cr);
	box(cr, p, k, C_INK, C_PANEL);
	text(cr, "Power", 14 * k, true, p.x + 16 * k, p.y + 14 * k, p.w - 32 * k, 22 * k, false, 1, C_INK);
	static const char *confirm[PW_COUNT] = { "Lock", "Suspend", "Confirm restart?", "Confirm power off?" };
	for (int i = 0; i < PW_COUNT; i++) {
		struct rect r = s->r_pwr_item[i];
		bool armed = s->armed == i, hot = is_hot(s, HIT_PWR_ITEM + i);
		box(cr, r, k, C_INK, armed ? C_INK : hot ? C_HOVER : C_SURFACE);
		text(cr, armed ? confirm[i] : power_names[i], 13 * k, true, r.x, r.y, r.w, r.h, true, 1, armed ? C_SURFACE : C_INK);
	}
}

static int pwr_hit(struct st *s, double x, double y) {
	if (!in(s->r_pwr, x, y)) return HIT_NONE;
	for (int i = 0; i < PW_COUNT; i++) if (in(s->r_pwr_item[i], x, y)) return HIT_PWR_ITEM + i;
	return HIT_PWR_PANEL;
}

static void layout_menu(struct st *s, int W, int H) {
	double k = s->scale, pad = 12 * k;
	double pw = 760 * k, ph = 560 * k;
	if (pw > W - 16 * k) pw = W - 16 * k;
	double top = s->tab_h * k + 4 * k;
	if (ph > H - top - 8 * k) ph = H - top - 8 * k;
	s->r_panel = (struct rect){ 8 * k, top, pw, ph };
	struct rect p = s->r_panel;
	s->r_search = (struct rect){ p.x + pad, p.y + pad, p.w - 2 * pad, 38 * k };
	double qh = 38 * k, qy = p.y + p.h - pad - qh;
	double qw = (p.w - 2 * pad - (QB_COUNT - 1) * 8 * k) / QB_COUNT;
	for (int i = 0; i < QB_COUNT; i++)
		s->r_qb[i] = (struct rect){ p.x + p.w - pad - (QB_COUNT - i) * (qw + 8 * k) + 8 * k, qy, qw, qh };
	double body = s->r_search.y + s->r_search.h + pad, sidew = 172 * k;
	double cy = body;
	for (int c = 0; c < CAT_COUNT; c++) {
		if (!s->cat_used[c]) { s->r_cats[c] = (struct rect){0}; continue; }
		s->r_cats[c] = (struct rect){ p.x + pad, cy, sidew, 34 * k };
		cy += 38 * k;
	}
	s->r_grid = (struct rect){ p.x + pad + sidew + pad, body, p.w - 3 * pad - sidew - 8 * k, qy - 32 * k - body };
	s->cols = (int)(s->r_grid.w / (112 * k)); if (s->cols < 1) s->cols = 1;
	double tile_h = 108 * k;
	int rows = (s->nfiltered + s->cols - 1) / s->cols;
	s->max_scroll = rows * tile_h - s->r_grid.h; if (s->max_scroll < 0) s->max_scroll = 0;
	if (s->scroll > s->max_scroll) s->scroll = s->max_scroll;
	if (s->scroll < 0) s->scroll = 0;
}

static struct rect tile_rect(struct st *s, int i) {
	double tw = s->r_grid.w / s->cols, th = 108 * s->scale;
	return (struct rect){ s->r_grid.x + (i % s->cols) * tw, s->r_grid.y + (i / s->cols) * th - s->scroll, tw, th };
}

static void draw_menu(struct st *s, cairo_t *cr, int W, int H) {
	double k = s->scale;
	layout_menu(s, W, H);
	if (!s->preview) { // the overlay is transparent except for a dim below the tab bar
		cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
		cairo_set_source_rgba(cr, 0, 0, 0, 0); cairo_paint(cr);
		cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
	}
	cairo_set_source_rgba(cr, 0.05, 0.05, 0.05, 0.18);
	cairo_rectangle(cr, 0, s->tab_h * k, W, H - s->tab_h * k); cairo_fill(cr);
	struct rect p = s->r_panel;
	cairo_set_source_rgba(cr, 0, 0, 0, .32); cairo_rectangle(cr, p.x + 6 * k, p.y + 6 * k, p.w, p.h); cairo_fill(cr);
	box(cr, p, k, C_INK, C_PANEL);

	// search
	box(cr, s->r_search, k, C_INK, C_SURFACE);
	char q[160];
	if (s->query[0]) snprintf(q, sizeof(q), "%s▏", s->query);
	text(cr, s->query[0] ? q : "Type to search apps…", 15 * k, false, s->r_search.x + 12 * k, s->r_search.y,
		s->r_search.w - 24 * k, s->r_search.h, false, 1, s->query[0] ? C_INK : C_MUTED);

	// category tabs
	for (int c = 0; c < CAT_COUNT; c++) {
		if (!s->cat_used[c]) continue;
		bool active = s->query[0] ? c == CAT_ALL : c == (int)s->cat;
		bool hot = s->hover == HIT_CAT + c;
		struct rect r = s->r_cats[c];
		box(cr, r, k, C_INK, active ? C_INK : hot ? C_HOVER : C_SURFACE);
		text(cr, cat_names[c], 13.5 * k, active, r.x + 12 * k, r.y, r.w - 20 * k, r.h, false, 1, active ? C_SURFACE : C_INK);
	}

	// app tiles
	cairo_save(cr);
	cairo_rectangle(cr, s->r_grid.x, s->r_grid.y, s->r_grid.w, s->r_grid.h); cairo_clip(cr);
	if (!s->nfiltered) {
		text(cr, s->query[0] ? "No apps match" : "No apps here", 14 * k, false, s->r_grid.x, s->r_grid.y + 20 * k,
			s->r_grid.w, 0, true, 1, C_MUTED);
		if (s->query[0]) text(cr, "Press Escape to clear your search", 12 * k, false,
			s->r_grid.x, s->r_grid.y + 50 * k, s->r_grid.w, 0, true, 1, C_MUTED);
	}
	for (int i = 0; i < s->nfiltered; i++) {
		struct rect t = tile_rect(s, i);
		if (t.y + t.h < s->r_grid.y || t.y > s->r_grid.y + s->r_grid.h) continue;
		struct app *a = &apps[s->filtered[i]];
		struct rect inner = { t.x + 4 * k, t.y + 4 * k, t.w - 8 * k, t.h - 8 * k };
		bool hot = s->hover == HIT_TILE + i, selected = i == s->sel && s->keyboard_focus;
		if (hot || selected) box(cr, inner, k, C_INK, hot ? C_HOVER : C_SURFACE);
		double isz = 44 * k;
		draw_icon(cr, a, inner.x + (inner.w - isz) / 2, inner.y + 8 * k, isz, k);
		text(cr, a->name, 12 * k, false, inner.x + 4 * k, inner.y + 58 * k, inner.w - 8 * k, 0, true, 2, C_INK);
	}
	cairo_restore(cr);
	if (s->max_scroll > 0) { // scroll thumb
		double track = s->r_grid.h, thumb = track * s->r_grid.h / (s->r_grid.h + s->max_scroll);
		double y = s->r_grid.y + (track - thumb) * s->scroll / s->max_scroll;
		color(cr, C_INK); cairo_rectangle(cr, s->r_grid.x + s->r_grid.w + 3 * k, y, 4 * k, thumb); cairo_fill(cr);
	}

	// footer
	char count[48]; snprintf(count, sizeof(count), "%d app%s", s->nfiltered, s->nfiltered == 1 ? "" : "s");
	text(cr, count, 11 * k, false, p.x + 12 * k, s->r_qb[0].y - 26 * k, 100 * k, 22 * k, false, 1, C_MUTED);
	text(cr, "Arrows: select  ·  Enter: open  ·  Tab: category  ·  Ctrl+P: privacy", 11 * k, false,
		p.x + 112 * k, s->r_qb[0].y - 26 * k, p.w - 124 * k, 22 * k, false, 1, C_MUTED);
	for (int i = 0; i < QB_COUNT; i++) {
		bool hot = s->hover == HIT_QB + i;
		box(cr, s->r_qb[i], k, C_INK, hot ? C_HOVER : C_SURFACE);
		text(cr, qb_names[i], 13 * k, true, s->r_qb[i].x, s->r_qb[i].y, s->r_qb[i].w, s->r_qb[i].h, true, 1, C_INK);
	}
}

// --------------------------------------------------------- quick settings
static void layout_qs(struct st *s, int W, int H) {
	double k = s->scale, pad = 16 * k, pw = 360 * k, top = s->tab_h * k + 4 * k;
	if (pw > W - 16 * k) pw = W - 16 * k;
	top -= s->qs_scroll;
	double px = W - STATUS_RIGHT * k - pw;
	if (px < 8 * k) px = 8 * k;
	double x = px + pad, w = pw - 2 * pad, y = top + pad;
	s->q_date = (struct rect){ x, y, w, 26 * k }; y += 26 * k;
	s->q_batt = (struct rect){ x, y, w, s->battery_line[0] ? 20 * k : 0 }; y += s->q_batt.h + 10 * k;
	s->q_sep[0] = y; y += 12 * k;
	s->q_vol_label = (struct rect){ x, y, w, 20 * k }; y += 24 * k;
	s->q_mute = (struct rect){ x, y, 76 * k, 32 * k };
	s->q_vol = (struct rect){ x + 92 * k, y, w - 100 * k, 32 * k }; y += 44 * k;
	if (s->bright_ok) {
		s->q_bright_label = (struct rect){ x, y, w, 20 * k }; y += 24 * k;
		s->q_bright = (struct rect){ x + 8 * k, y, w - 16 * k, 32 * k }; y += 44 * k;
	} else s->q_bright = s->q_bright_label = (struct rect){0};
	s->q_sep[1] = y; y += 12 * k;
	s->q_wifi_label = (struct rect){ x, y, w, 22 * k }; y += 30 * k;
	for (int i = 0; i < MAX_PROFILES; i++) {
		if (i < s->nprofiles) {
			s->q_prof_name[i] = (struct rect){ x, y, w - 116 * k, 32 * k };
			s->q_prof[i] = (struct rect){ x + w - 108 * k, y, 108 * k, 32 * k };
			y += 38 * k;
		} else s->q_prof[i] = s->q_prof_name[i] = (struct rect){0};
	}
	if (!s->nprofiles) { s->q_noprof = (struct rect){ x, y, w, 24 * k }; y += 28 * k; }
	else s->q_noprof = (struct rect){0};
	s->q_wifiset = (struct rect){ x, y + 2 * k, w, 34 * k }; y += 48 * k;
	s->q_privacy = (struct rect){ x, y, w, 38 * k }; y += 50 * k;
	s->q_sep[2] = y; y += 12 * k;
	double bw = (w - 3 * 8 * k) / PW_COUNT;
	for (int i = 0; i < PW_COUNT; i++) s->q_power[i] = (struct rect){ x + i * (bw + 8 * k), y, bw, 40 * k };
	y += 40 * k + pad;
	s->r_qs = (struct rect){ px, top, pw, y - top };
	s->qs_max_scroll = y + s->qs_scroll + 8 * k - H;
	if (s->qs_max_scroll < 0) s->qs_max_scroll = 0;
	if (s->qs_scroll > s->qs_max_scroll) {
		s->qs_scroll = s->qs_max_scroll;
		layout_qs(s, W, H);
	}
}

static void slider(struct st *s, cairo_t *cr, struct rect r, int value) {
	double k = s->scale, th = 10 * k, ty = r.y + (r.h - th) / 2, fill = r.w * value / 100.0;
	box(cr, (struct rect){ r.x, ty, r.w, th }, k * 0.75, C_INK, C_SURFACE);
	color(cr, C_INK); cairo_rectangle(cr, r.x, ty, fill, th); cairo_fill(cr);
	double kn = 22 * k; // the knob is a little cube
	box(cr, (struct rect){ r.x + fill - kn / 2, r.y + (r.h - kn) / 2, kn, kn }, k, C_INK, C_SURFACE);
}

static void label_row(struct st *s, cairo_t *cr, struct rect r, const char *name, const char *value) {
	double k = s->scale;
	text(cr, name, 13.5 * k, true, r.x, r.y, r.w - 70 * k, r.h, false, 1, C_INK);
	if (value) text(cr, value, 13 * k, false, r.x + r.w - 70 * k, r.y, 70 * k, r.h, true, 1, C_MUTED);
}

static void draw_qs(struct st *s, cairo_t *cr, int W, int H) {
	double k = s->scale;
	layout_qs(s, W, H);
	if (!s->preview) {
		cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
		cairo_set_source_rgba(cr, 0, 0, 0, 0); cairo_paint(cr);
		cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
	}
	cairo_set_source_rgba(cr, 0.05, 0.05, 0.05, 0.18);
	cairo_rectangle(cr, 0, s->tab_h * k, W, H - s->tab_h * k); cairo_fill(cr);
	cairo_save(cr);
	cairo_rectangle(cr, 0, s->tab_h * k, W, H - s->tab_h * k); cairo_clip(cr);
	struct rect p = s->r_qs;
	cairo_set_source_rgba(cr, 0, 0, 0, .32); cairo_rectangle(cr, p.x + 6 * k, p.y + 6 * k, p.w, p.h); cairo_fill(cr);
	box(cr, p, k, C_INK, C_PANEL);

	time_t now = time(NULL);
	char date[64], hm[16], v[16];
	strftime(date, sizeof(date), "%A %-d %B", localtime(&now));
	strftime(hm, sizeof(hm), "%H:%M", localtime(&now));
	text(cr, date, 17 * k, true, s->q_date.x, s->q_date.y, s->q_date.w - 64 * k, s->q_date.h, false, 1, C_INK);
	text(cr, hm, 17 * k, false, s->q_date.x + s->q_date.w - 64 * k, s->q_date.y, 64 * k, s->q_date.h, true, 1, C_INK);
	if (s->battery_line[0])
		text(cr, s->battery_line, 13 * k, false, s->q_batt.x, s->q_batt.y, s->q_batt.w, s->q_batt.h, false, 1, C_MUTED);
	for (int i = 0; i < 3; i++) { color(cr, C_MUTED); cairo_rectangle(cr, p.x + 16 * k, s->q_sep[i], p.w - 32 * k, 1 * k); cairo_fill(cr); }

	// volume
	snprintf(v, sizeof(v), "%d%%", s->vol);
	label_row(s, cr, s->q_vol_label, "Volume", s->vol_ok ? v : NULL);
	bool mhot = is_hot(s, HIT_QS_MUTE);
	box(cr, s->q_mute, k, C_INK, s->muted ? C_INK : mhot ? C_HOVER : C_SURFACE);
	text(cr, !s->vol_ok ? "N/A" : s->muted ? "Muted" : "Mute", 12.5 * k, true, s->q_mute.x, s->q_mute.y, s->q_mute.w, s->q_mute.h, true, 1,
		s->muted ? C_SURFACE : C_INK);
	if (s->vol_ok) slider(s, cr, s->q_vol, s->vol);
	else text(cr, "No volume control found", 12.5 * k, false, s->q_vol.x, s->q_vol.y, s->q_vol.w, s->q_vol.h, false, 1, C_MUTED);

	// brightness
	if (s->bright_ok) {
		snprintf(v, sizeof(v), "%d%%", s->bright);
		label_row(s, cr, s->q_bright_label, "Brightness", v);
		slider(s, cr, s->q_bright, s->bright);
	}

	// Wi-Fi
	double gh = 14 * k;
	wifi_glyph(cr, s->q_wifi_label.x, s->q_wifi_label.y + (s->q_wifi_label.h - gh) / 2, gh, s->wifi_bars, k);
	char wl[128]; snprintf(wl, sizeof(wl), "Wi-Fi · %s", s->wifi_label);
	text(cr, wl, 13.5 * k, true, s->q_wifi_label.x + 30 * k, s->q_wifi_label.y, s->q_wifi_label.w - 30 * k, s->q_wifi_label.h, false, 1, C_INK);
	for (int i = 0; i < s->nprofiles && i < MAX_PROFILES; i++) {
		char label[96];
		if (s->privacy_hide_network) snprintf(label, sizeof(label), "Saved network %d", i + 1);
		else snprintf(label, sizeof(label), "%s", s->profiles[i].ssid);
		text(cr, label, 13.5 * k, false, s->q_prof_name[i].x + 4 * k, s->q_prof_name[i].y,
			s->q_prof_name[i].w - 4 * k, s->q_prof_name[i].h, false, 1, C_INK);
		if (s->wifi_ssid[0] && !strcmp(s->wifi_ssid, s->profiles[i].ssid)) {
			box(cr, s->q_prof[i], k, C_INK, C_INK);
			text(cr, "Connected", 12.5 * k, true, s->q_prof[i].x, s->q_prof[i].y, s->q_prof[i].w, s->q_prof[i].h, true, 1, C_SURFACE);
		} else {
			box(cr, s->q_prof[i], k, C_INK, is_hot(s, HIT_QS_PROF + i) ? C_HOVER : C_SURFACE);
			text(cr, "Connect", 12.5 * k, true, s->q_prof[i].x, s->q_prof[i].y, s->q_prof[i].w, s->q_prof[i].h, true, 1, C_INK);
		}
	}
	if (s->q_noprof.h > 0)
		text(cr, "No saved networks yet", 12.5 * k, false, s->q_noprof.x + 4 * k, s->q_noprof.y, s->q_noprof.w, s->q_noprof.h, false, 1, C_MUTED);
	box(cr, s->q_wifiset, k, C_INK, is_hot(s, HIT_QS_WIFISET) ? C_HOVER : C_SURFACE);
	text(cr, "Wi-Fi settings…", 13 * k, true, s->q_wifiset.x, s->q_wifiset.y, s->q_wifiset.w, s->q_wifiset.h, true, 1, C_INK);
	box(cr, s->q_privacy, k, C_INK, is_hot(s, HIT_QS_PRIVACY) ? C_HOVER : C_SURFACE);
	text(cr, s->privacy_hide_network ? "Privacy · network names hidden" : "Privacy settings…", 13 * k, true,
		s->q_privacy.x, s->q_privacy.y, s->q_privacy.w, s->q_privacy.h, true, 1, C_INK);

	// power
	for (int i = 0; i < PW_COUNT; i++) {
		bool armed = s->armed == i, hot = is_hot(s, HIT_QS_POWER + i);
		box(cr, s->q_power[i], k, C_INK, armed ? C_INK : hot ? C_HOVER : C_SURFACE);
		text(cr, armed ? "Confirm?" : power_names[i], 12.5 * k, true, s->q_power[i].x + 2 * k, s->q_power[i].y,
			s->q_power[i].w - 4 * k, s->q_power[i].h, true, 1, armed ? C_SURFACE : C_INK);
	}
	if (s->keyboard_focus && s->focus == HIT_QS_VOL)
		box(cr, (struct rect){s->q_vol.x - 4 * k, s->q_vol.y - 2 * k, s->q_vol.w + 8 * k, s->q_vol.h + 4 * k}, 2 * k, C_INK, NULL);
	if (s->keyboard_focus && s->focus == HIT_QS_BRIGHT)
		box(cr, (struct rect){s->q_bright.x - 4 * k, s->q_bright.y - 2 * k, s->q_bright.w + 8 * k, s->q_bright.h + 4 * k}, 2 * k, C_INK, NULL);
	if (s->qs_max_scroll > 0) {
		double top = s->tab_h * k + 4 * k, height = H - top - 8 * k;
		double thumb = height * height / (height + s->qs_max_scroll);
		color(cr, C_INK);
		cairo_rectangle(cr, p.x + p.w - 5 * k, top + (height - thumb) * s->qs_scroll / s->qs_max_scroll, 3 * k, thumb);
		cairo_fill(cr);
	}
	cairo_restore(cr);
}

static int qs_hit(struct st *s, double x, double y) {
	if (y < s->tab_h * s->scale || !in(s->r_qs, x, y)) return HIT_NONE;
	if (s->vol_ok && in(s->q_mute, x, y)) return HIT_QS_MUTE;
	if (s->vol_ok && in(s->q_vol, x, y)) return HIT_QS_VOL;
	if (s->bright_ok && in(s->q_bright, x, y)) return HIT_QS_BRIGHT;
	if (in(s->q_wifiset, x, y)) return HIT_QS_WIFISET;
	if (in(s->q_privacy, x, y)) return HIT_QS_PRIVACY;
	for (int i = 0; i < s->nprofiles && i < MAX_PROFILES; i++) if (in(s->q_prof[i], x, y)) return HIT_QS_PROF + i;
	for (int i = 0; i < PW_COUNT; i++) if (in(s->q_power[i], x, y)) return HIT_QS_POWER + i;
	return HIT_QS_PANEL;
}

// ------------------------------------------------------------- status data
static void battery(char *out, size_t n) {
	DIR *d = opendir("/sys/class/power_supply");
	if (!d) { out[0] = 0; return; }
	struct dirent *e;
	char name[256] = {0};
	while ((e = readdir(d))) {
		if (!strncmp(e->d_name, "BAT", 3)) {
			snprintf(name, sizeof(name), "%s", e->d_name);
			break;
		}
	}
	closedir(d);
	if (!name[0]) { out[0] = 0; return; }
	char p[320], cap[16] = {0}, st[16] = {0};
	snprintf(p, sizeof(p), "/sys/class/power_supply/%s/capacity", name);
	read_file_first(p, cap, sizeof(cap));
	snprintf(p, sizeof(p), "/sys/class/power_supply/%s/status", name);
	read_file_first(p, st, sizeof(st));
	if (!cap[0]) { out[0] = 0; return; }
	const char *sym = (!strcmp(st, "Charging")) ? "+" :
		(!strcmp(st, "Discharging")) ? "" : "=";
	snprintf(out, n, "BAT %s%%%s", cap, sym);
}

// Minimal readers for cube-net's flat, ASCII-escaped status.json.
static const char *json_value(const char *data, const char *key) {
	char pattern[64]; snprintf(pattern, sizeof(pattern), "\"%s\"", key);
	const char *p = strstr(data, pattern);
	if (!p || !(p = strchr(p + strlen(pattern), ':'))) return NULL;
	do { p++; } while (*p == ' ');
	return p;
}
static bool json_string(const char *data, const char *key, char *out, size_t n) {
	const char *p = json_value(data, key);
	out[0] = 0;
	if (!p || *p != '"') return false;
	size_t o = 0;
	for (p++; *p && *p != '"' && o + 5 < n; p++) {
		if (*p != '\\') { out[o++] = *p; continue; }
		p++;
		if (*p == 'u' && p[1] && p[2] && p[3] && p[4]) {
			char hex[5] = { p[1], p[2], p[3], p[4], 0 };
			gunichar c = (gunichar)strtoul(hex, NULL, 16);
			p += 4;
			if (c >= 0xd800 && c < 0xdc00 && p[1] == '\\' && p[2] == 'u' && p[3] && p[4] && p[5] && p[6]) {
				char lo[5] = { p[3], p[4], p[5], p[6], 0 };
				gunichar l = (gunichar)strtoul(lo, NULL, 16);
				if (l >= 0xdc00 && l < 0xe000) { c = 0x10000 + ((c - 0xd800) << 10) + (l - 0xdc00); p += 6; }
			}
			if (c >= 0x20) o += g_unichar_to_utf8(c, out + o);
		} else if (*p) out[o++] = (*p == 'n' || *p == 't' || *p == 'r') ? ' ' : *p;
		else break;
	}
	out[o] = 0;
	return true;
}

static void wifi_parse(struct st *s, const char *data, time_t now) {
	s->wifi_ssid[0] = 0;
	const char *ep = json_value(data, "updated_epoch");
	if (!data[0] || !ep || now - strtod(ep, NULL) > 20) {
		s->wifi_bars = -1; snprintf(s->wifi_label, sizeof(s->wifi_label), "Wi-Fi off"); return;
	}
	char state[48], ssid[80];
	json_string(data, "association_state", state, sizeof(state));
	json_string(data, "ssid", ssid, sizeof(ssid));
	const char *dbm = json_value(data, "signal_dbm");
	const char *online = json_value(data, "connectivity_ok");
	s->wifi_bars = 0;
	s->wifi_ssid[0] = 0;
	if (!strcmp(state, "COMPLETED")) {
		snprintf(s->wifi_ssid, sizeof(s->wifi_ssid), "%s", ssid);
		double v = dbm && *dbm != 'n' ? strtod(dbm, NULL) : -100;
		s->wifi_bars = v > -55 ? 4 : v > -67 ? 3 : v > -75 ? 2 : 1;
		snprintf(s->wifi_label, sizeof(s->wifi_label), "%s%s", !s->privacy_hide_network && ssid[0] ? ssid : "Connected",
			online && !strncmp(online, "false", 5) ? " (no internet)" : "");
	} else if (!strcmp(state, "waiting-for-dongle")) {
		s->wifi_bars = -1; snprintf(s->wifi_label, sizeof(s->wifi_label), "No adapter");
	} else if (!strcmp(state, "DISCONNECTED") || !strcmp(state, "INACTIVE") || !strcmp(state, "INTERFACE_DISABLED")) {
		snprintf(s->wifi_label, sizeof(s->wifi_label), "Disconnected");
	} else if (state[0] && strcmp(state, "unknown") && strcmp(state, "stale")) {
		snprintf(s->wifi_label, sizeof(s->wifi_label), "Connecting…");
	} else {
		snprintf(s->wifi_label, sizeof(s->wifi_label), "Wi-Fi");
	}
}

static void wifi_state(struct st *s) {
	char data[16385] = "";
	int fd = open(NET_STATUS, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd >= 0) { ssize_t n = read(fd, data, sizeof(data) - 1); if (n > 0) data[n] = 0; close(fd); }
	wifi_parse(s, data, time(NULL));
}

static void status_update(struct st *s) {
	time_t t = time(NULL);
	char clock[32], bat[48] = "";
	strftime(clock, sizeof(clock), s->clock_12h ? (s->clock_seconds ? "%I:%M:%S%p" : "%I:%M%p") : (s->clock_seconds ? "%H:%M:%S" : "%H:%M"), localtime(&t));
	battery(bat, sizeof(bat));
	if (bat[0]) snprintf(s->clock_text, sizeof(s->clock_text), "%s  %s", clock, bat);
	else snprintf(s->clock_text, sizeof(s->clock_text), "%s", clock);
	wifi_state(s);
	if (getenv("WHITECUBE_SETTINGS_REPORT")) {
		GKeyFile *report = g_key_file_new();
		g_key_file_set_boolean(report, "settings", "privacy_hide_network", s->privacy_hide_network);
		g_key_file_set_string(report, "settings", "clock_format", s->clock_12h ? "12h" : "24h");
		g_key_file_set_boolean(report, "settings", "clock_seconds", s->clock_seconds);
		g_key_file_set_string(report, "settings", "clock_text", s->clock_text);
		g_key_file_set_integer(report, "settings", "tab_h", s->tab_h);
		wc_pref_report(report, "status.ini");
	}
}

// Geometry and palette follow the compositor and shared accessibility settings.
static void status_preference(const char *key, const char *value, void *data) {
	struct st *s = data;
	if (!strcmp(key, "tab_h")) s->tab_h = atoi(value);
	else if (!strcmp(key, "bg")) status_theme_color(theme.background, value);
	else if (!strcmp(key, "tabbar")) status_theme_color(theme.bar, value);
	else if (!strcmp(key, "privacy_hide_network")) s->privacy_hide_network = !strcmp(value, "1");
	else if (!strcmp(key, "clock_format")) s->clock_12h = !strcmp(value, "12h");
	else if (!strcmp(key, "clock_seconds")) s->clock_seconds = !strcmp(value, "1");
}
static void read_geometry(struct st *s) {
	s->tab_h = 40;
	s->privacy_hide_network = false;
	s->clock_12h = s->clock_seconds = false;
	status_theme_defaults(&theme);
	const char *home = getenv("HOME"), *xdg = getenv("XDG_STATE_HOME");
	char user[600] = "", acc[600] = "";
	if (home) snprintf(user, sizeof(user), "%s/.config/whitecube/config", home);
	const char *configs[] = { "/etc/whitecube/config", user };
	for (int i = 0; i < 2; i++) {
		FILE *f = configs[i][0] ? fopen(configs[i], "r") : NULL;
		if (!f) continue;
		char line[256];
		while (fgets(line, sizeof(line), f)) {
			line[strcspn(line, "\r\n")] = 0;
			if (!strncmp(line, "bg=", 3)) status_theme_color(theme.background, line + 3);
			else if (!strcmp(line, "privacy_hide_network=1")) s->privacy_hide_network = true;
			else if (!strcmp(line, "privacy_hide_network=0")) s->privacy_hide_network = false;
			else if (!strncmp(line, "clock_format=", 13)) s->clock_12h = !strcmp(line + 13, "12h");
			else if (!strncmp(line, "clock_seconds=", 14)) s->clock_seconds = !strcmp(line + 14, "1") || !strcmp(line + 14, "true");
			else if (!strncmp(line, "tabbar=", 7)) status_theme_color(theme.bar, line + 7);
			else if (!strncmp(line, "tab_h=", 6)) {
				char *end; errno = 0;
				long value = strtol(line + 6, &end, 10);
				if (end == line + 6 || errno) continue;
				while (isspace((unsigned char)*end)) end++;
				if (!*end) s->tab_h = value < 20 ? 20 : value > 80 ? 80 : (int)value;
			}
		}
		fclose(f);
	}
	wc_prefs_visit(status_preference, s);
	if (s->tab_h < 20) s->tab_h = 20;
	if (xdg && xdg[0] == '/') snprintf(acc, sizeof(acc), "%s/whitecube/accessibility.json", xdg);
	else if (home) snprintf(acc, sizeof(acc), "%s/.local/state/whitecube/accessibility.json", home);
	struct wc_accessibility accessibility = {1, 1, false, false};
	if (acc[0]) wc_accessibility_load(acc, &accessibility);
	s->scale = accessibility.ui_scale;
	status_theme_finish(&theme, accessibility.strong);
}

static void qs_load(struct st *s) {
	helper(JOB_SOUND, false, "sound", NULL);
	helper(JOB_POWER, false, "power", NULL);
	helper(JOB_NET, false, "net", NULL);
}

static void job_parse(struct st *s, struct job *j) {
	j->buf[j->len] = 0;
	if (j->kind == JOB_SOUND) s->vol_ok = false;
	if (j->kind == JOB_POWER) { s->bright_ok = false; s->battery_line[0] = 0; }
	if (j->kind == JOB_NET) s->nprofiles = 0;
	char *save = NULL;
	for (char *line = strtok_r(j->buf, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
		char *f[6] = {0}; int n = 0;
		for (char *q = line; q && n < 6; ) { f[n++] = q; q = strchr(q, '\t'); if (q) *q++ = 0; }
		if (n < 3) continue;
		if (j->kind == JOB_SOUND && !strcmp(f[0], "sound")) {
			if (!strcmp(f[1], "volume")) {
				s->vol_ok = f[2][0] >= '0' && f[2][0] <= '9';
				if (s->vol_ok && s->dragging != DRAG_VOL) s->vol = atoi(f[2]);
			} else if (!strcmp(f[1], "muted")) s->muted = !strcmp(f[2], "yes");
		} else if (j->kind == JOB_POWER && !strcmp(f[0], "backlight") && !s->bright_ok) {
			s->bright_ok = true;
			if (s->dragging != DRAG_BRIGHT) s->bright = atoi(f[2]);
		} else if (j->kind == JOB_POWER && n >= 4 && !strcmp(f[0], "battery") && !s->battery_line[0]) {
			snprintf(s->battery_line, sizeof(s->battery_line), "Battery %s%% \u00b7 %s", f[2], f[3]);
		} else if (j->kind == JOB_NET && !strcmp(f[0], "profile") && s->nprofiles < MAX_PROFILES) {
			snprintf(s->profiles[s->nprofiles].id, sizeof(s->profiles[0].id), "%s", f[1]);
			snprintf(s->profiles[s->nprofiles].ssid, sizeof(s->profiles[0].ssid), "%s", f[2]);
			s->nprofiles++;
		}
	}
}

// ---------------------------------------------------------------- buffers
struct buffer { struct wl_buffer *wl; void *data; size_t size; };
static void buffer_release(void *data, struct wl_buffer *wl) {
	struct buffer *b = data;
	wl_buffer_destroy(wl); munmap(b->data, b->size); free(b);
}
static const struct wl_buffer_listener buffer_listener = { buffer_release };

static void paint(struct st *s, struct panel *p, void (*fn)(struct st *, cairo_t *, int, int)) {
	if (!p->surf || !p->configured || p->w <= 0 || p->h <= 0) return;
	int stride = p->w * 4; size_t size = (size_t)stride * p->h;
	int fd = memfd_create("cube-status", MFD_CLOEXEC);
	if (fd < 0 || ftruncate(fd, size) < 0) { if (fd >= 0) close(fd); return; }
	void *px = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (px == MAP_FAILED) { close(fd); return; }
	struct wl_shm_pool *pool = wl_shm_create_pool(s->shm, fd, size);
	struct buffer *b = calloc(1, sizeof(*b));
	b->data = px; b->size = size;
	b->wl = wl_shm_pool_create_buffer(pool, 0, p->w, p->h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_buffer_add_listener(b->wl, &buffer_listener, b);
	wl_shm_pool_destroy(pool);
	close(fd);
	cairo_surface_t *cs = cairo_image_surface_create_for_data(px, CAIRO_FORMAT_ARGB32, p->w, p->h, stride);
	cairo_t *cr = cairo_create(cs);
	fn(s, cr, p->w, p->h);
	cairo_destroy(cr);
	cairo_surface_flush(cs);
	cairo_surface_destroy(cs);
	wl_surface_attach(p->surf, b->wl, 0, 0);
	wl_surface_damage_buffer(p->surf, 0, 0, p->w, p->h);
	wl_surface_commit(p->surf);
}

// ---------------------------------------------------------------- surfaces
static void layer_configure(void *data, struct zwlr_layer_surface_v1 *ls, uint32_t serial, uint32_t w, uint32_t h);
static void layer_closed(void *data, struct zwlr_layer_surface_v1 *ls);
static const struct zwlr_layer_surface_v1_listener layer_listener = { layer_configure, layer_closed };

static void panel_create(struct st *s, struct panel *p, uint32_t layer, const char *ns, uint32_t anchor,
		int w, int h, int mright, uint32_t keyboard) {
	p->surf = wl_compositor_create_surface(s->comp);
	p->layer = zwlr_layer_shell_v1_get_layer_surface(s->layer_shell, p->surf, NULL, layer, ns);
	zwlr_layer_surface_v1_set_size(p->layer, w, h);
	zwlr_layer_surface_v1_set_anchor(p->layer, anchor);
	zwlr_layer_surface_v1_set_exclusive_zone(p->layer, layer == ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY ? -1 : 0);
	zwlr_layer_surface_v1_set_margin(p->layer, 0, mright, 0, 0);
	zwlr_layer_surface_v1_set_keyboard_interactivity(p->layer, keyboard);
	zwlr_layer_surface_v1_add_listener(p->layer, &layer_listener, p);
	p->configured = false;
	wl_surface_commit(p->surf);
}

static void panel_destroy(struct panel *p) {
	if (p->layer) zwlr_layer_surface_v1_destroy(p->layer);
	if (p->surf) wl_surface_destroy(p->surf);
	memset(p, 0, sizeof(*p));
}

static void draw_notice(struct st *s, cairo_t *cr, int w, int h) {
	color(cr, C_PANEL); cairo_paint(cr);
	text(cr, s->notice_text, 14 * s->scale, false, 16 * s->scale, 8 * s->scale,
		w - 32 * s->scale, h - 16 * s->scale, false, 3, C_INK);
}
static void notice_sync(struct st *s) {
	if (!s->dpy) return;
	if (!s->notice_text[0] || monotonic_s() >= s->notice_until) {
		if (s->notice.layer) panel_destroy(&s->notice);
		s->notice_text[0] = 0; return;
	}
	if (!s->notice.layer) {
		panel_create(s, &s->notice, ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, "cube-notice",
			ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP, (int)(480 * s->scale), (int)(92 * s->scale), 0, 0);
		zwlr_layer_surface_v1_set_margin(s->notice.layer, (int)(48 * s->scale), 0, 0, 0);
		struct wl_region *empty = wl_compositor_create_region(s->comp);
		wl_surface_set_input_region(s->notice.surf, empty); wl_region_destroy(empty);
		wl_surface_commit(s->notice.surf);
	}
	paint(s, &s->notice, draw_notice);
}

static void bar_sizes(struct st *s, int *aw, int *sw, int *h, int *mr) {
	*aw = (int)(APPS_W * s->scale); *sw = (int)(STATUS_W * s->scale);
	if (s->chat_btn.w > 0) *sw = wc_bar_layout(s->chat_btn.w, s->scale).status_width;
	*h = (int)(s->tab_h * s->scale); *mr = (int)(STATUS_RIGHT * s->scale);
}

static void bar_create(struct st *s) {
	int aw, sw, h, mr; bar_sizes(s, &aw, &sw, &h, &mr);
	panel_create(s, &s->apps_btn, ZWLR_LAYER_SHELL_V1_LAYER_TOP, "cube-apps",
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT, aw, h, 0, 0);
	panel_create(s, &s->status, ZWLR_LAYER_SHELL_V1_LAYER_TOP, "cube-status",
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, sw, h, mr, 0);
	panel_create(s, &s->chat_btn, ZWLR_LAYER_SHELL_V1_LAYER_TOP, "cube-chat-btn",
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT,
		0, h, 0, 0);
	panel_create(s, &s->pwr_btn, ZWLR_LAYER_SHELL_V1_LAYER_TOP, "cube-power-btn",
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, (int)(POWER_W * s->scale), h, 0, 0);
}

static void bar_resize(struct st *s) {
	int aw, sw, h, mr; bar_sizes(s, &aw, &sw, &h, &mr);
	zwlr_layer_surface_v1_set_size(s->apps_btn.layer, aw, h);
	wl_surface_commit(s->apps_btn.surf);
	zwlr_layer_surface_v1_set_size(s->status.layer, sw, h);
	zwlr_layer_surface_v1_set_margin(s->status.layer, 0, mr, 0, 0);
	wl_surface_commit(s->status.surf);
	zwlr_layer_surface_v1_set_size(s->chat_btn.layer, 0, h);
	wl_surface_commit(s->chat_btn.surf);
	zwlr_layer_surface_v1_set_size(s->pwr_btn.layer, (int)(POWER_W * s->scale), h);
	wl_surface_commit(s->pwr_btn.surf);
}

static void chat_close(struct st *s);
static void qs_close(struct st *s);
static void pwr_close(struct st *s);
static void menu_open(struct st *s) {
	if (s->menu_open) return;
	qs_close(s);
	chat_close(s);
	pwr_close(s);
	load_apps();
	memset(s->cat_used, 0, sizeof(s->cat_used));
	s->cat_used[CAT_ALL] = true;
	for (int i = 0; i < napps; i++) s->cat_used[apps[i].cat] = true;
	if (!s->cat_used[s->cat]) s->cat = CAT_ALL;
	s->query[0] = 0; s->scroll = 0; s->sel = 0; s->hover = HIT_NONE;
	s->keyboard_focus = true; s->focus = HIT_NONE;
	filter_apps(s);
	s->menu_open = true;
	panel_create(s, &s->menu, ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, "cube-menu",
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
		ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, 0, 0, 0,
		ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE);
	paint(s, &s->apps_btn, draw_apps_btn);
}

static void menu_close(struct st *s) {
	if (!s->menu_open) return;
	repeat_stop(s);
	if (s->compose) xkb_compose_state_reset(s->compose);
	if (s->hover_panel == &s->menu) s->hover_panel = NULL;
	panel_destroy(&s->menu);
	s->menu_open = false;
	s->hover = HIT_NONE;
	paint(s, &s->apps_btn, draw_apps_btn);
}

static void qs_open(struct st *s) {
	if (s->qs_open) return;
	menu_close(s);
	chat_close(s);
	pwr_close(s);
	s->qs_open = true; s->hover = HIT_NONE; s->dragging = DRAG_NONE; s->armed = -1;
	s->focus = HIT_QS_PRIVACY; s->keyboard_focus = true; s->qs_scroll = 0;
	qs_load(s);
	panel_create(s, &s->qs, ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, "cube-quick",
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
		ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, 0, 0, 0,
		ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE);
	paint(s, &s->status, draw_status);
}

static void qs_close(struct st *s) {
	if (!s->qs_open) return;
	repeat_stop(s);
	if (s->hover_panel == &s->qs) s->hover_panel = NULL;
	panel_destroy(&s->qs);
	s->qs_open = false; s->hover = HIT_NONE; s->dragging = DRAG_NONE; s->armed = -1;
	paint(s, &s->status, draw_status);
}

static void menu_toggle(struct st *s) { if (s->menu_open) menu_close(s); else menu_open(s); }
static void qs_toggle(struct st *s) { if (s->qs_open) qs_close(s); else qs_open(s); }

static void chat_open(struct st *s) {
	if (s->chat_open) return;
	menu_close(s);
	qs_close(s);
	pwr_close(s);
	s->chat_open = true; s->hover = HIT_NONE;
	s->focus = HIT_CHAT_OPEN; s->keyboard_focus = true;
	panel_create(s, &s->chat, ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, "cube-chat-menu",
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
		ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, 0, 0, 0,
		ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE);
	paint(s, &s->chat_btn, draw_chat_btn);
}

static void chat_close(struct st *s) {
	if (!s->chat_open) return;
	repeat_stop(s);
	if (s->hover_panel == &s->chat) s->hover_panel = NULL;
	panel_destroy(&s->chat);
	s->chat_open = false; s->hover = HIT_NONE;
	paint(s, &s->chat_btn, draw_chat_btn);
}

static void chat_toggle(struct st *s) { if (s->chat_open) chat_close(s); else chat_open(s); }

static void pwr_open(struct st *s) {
	if (s->pwr_open) return;
	menu_close(s);
	qs_close(s);
	chat_close(s);
	s->pwr_open = true; s->hover = HIT_NONE; s->armed = -1;
	s->focus = HIT_PWR_ITEM + PW_LOCK; s->keyboard_focus = true;
	panel_create(s, &s->pwr, ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, "cube-power-menu",
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
		ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, 0, 0, 0,
		ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE);
	paint(s, &s->pwr_btn, draw_pwr_btn);
}

static void pwr_close(struct st *s) {
	if (!s->pwr_open) return;
	repeat_stop(s);
	if (s->hover_panel == &s->pwr) s->hover_panel = NULL;
	panel_destroy(&s->pwr);
	s->pwr_open = false; s->hover = HIT_NONE; s->armed = -1;
	paint(s, &s->pwr_btn, draw_pwr_btn);
}

static void pwr_toggle(struct st *s) { if (s->pwr_open) pwr_close(s); else pwr_open(s); }

static void chat_hit_to_cmd(struct st *s, int h) {
	if (h == HIT_CHAT_OPEN) spawn_shell("exec " DESKTOP_CHAT_SH " --show");
	else if (h == HIT_CHAT_SETTINGS) spawn_shell("exec " DESKTOP_CHAT_SH " --settings");
	else if (h == HIT_CHAT_QUIT) spawn_shell("exec " DESKTOP_CHAT_SH " --quit"); // asks the running chat to exit; other Electron apps are untouched
}

static void repaint(struct st *s, struct panel *p) {
	if (p == &s->notice) paint(s, p, draw_notice);
	else if (p == &s->menu) paint(s, p, draw_menu);
	else if (p == &s->qs) paint(s, p, draw_qs);
	else if (p == &s->chat) paint(s, p, draw_chat);
	else if (p == &s->chat_btn) paint(s, p, draw_chat_btn);
	else if (p == &s->pwr) paint(s, p, draw_pwr);
	else if (p == &s->pwr_btn) paint(s, p, draw_pwr_btn);
	else if (p == &s->apps_btn) paint(s, p, draw_apps_btn);
	else if (p == &s->status) paint(s, p, draw_status);
}

static void layer_configure(void *data, struct zwlr_layer_surface_v1 *ls, uint32_t serial, uint32_t w, uint32_t h) {
	struct panel *p = data;
	zwlr_layer_surface_v1_ack_configure(ls, serial);
	p->w = (int)w; p->h = (int)h; p->configured = true;
	if (p == &g_st->chat_btn) {
		// Only the visible Chat button accepts pointer input; the rest is transparent.
		struct wc_bar_layout bar = wc_bar_layout(p->w, g_st->scale);
		struct wl_region *input = wl_compositor_create_region(g_st->comp);
		wl_region_add(input, bar.chat_x, 0, bar.chat_width, p->h);
		wl_surface_set_input_region(p->surf, input);
		wl_region_destroy(input);
		if (g_st->status.layer && bar.status_width != g_st->status.w) {
			zwlr_layer_surface_v1_set_size(g_st->status.layer, bar.status_width, p->h);
			wl_surface_commit(g_st->status.surf);
		}
	}
	repaint(g_st, p);
}

static void layer_closed(void *data, struct zwlr_layer_surface_v1 *ls) {
	struct panel *p = data;
	if (p == &g_st->notice) { panel_destroy(p); g_st->notice_text[0] = 0; }
	else if (p == &g_st->menu) menu_close(g_st);
	else if (p == &g_st->qs) qs_close(g_st);
	else if (p == &g_st->chat) chat_close(g_st);
	else if (p == &g_st->pwr) pwr_close(g_st);
	else if (p == &g_st->chat_btn) g_st->running = false;
	else g_st->running = false;
}

// ------------------------------------------------------------------- input
static int menu_hit(struct st *s, double x, double y) {
	if (!in(s->r_panel, x, y)) return HIT_NONE;
	if (in(s->r_search, x, y)) return HIT_SEARCH;
	for (int c = 0; c < CAT_COUNT; c++) if (s->cat_used[c] && in(s->r_cats[c], x, y)) return HIT_CAT + c;
	for (int i = 0; i < QB_COUNT; i++) if (in(s->r_qb[i], x, y)) return HIT_QB + i;
	if (in(s->r_grid, x, y))
		for (int i = 0; i < s->nfiltered; i++) if (in(tile_rect(s, i), x, y)) return HIT_TILE + i;
	return HIT_PANEL;
}

static int chat_hit(struct st *s, double x, double y) {
	if (!in(s->r_chat, x, y)) return HIT_NONE;
	if (in(s->r_chat_open, x, y)) return HIT_CHAT_OPEN;
	if (in(s->r_chat_settings, x, y)) return HIT_CHAT_SETTINGS;
	if (in(s->r_chat_quit, x, y)) return HIT_CHAT_QUIT;
	return HIT_CHAT_OPEN - 1;
}

static int hit(struct st *s) {
	if (s->hover_panel == &s->menu) return menu_hit(s, s->px, s->py);
	if (s->hover_panel == &s->qs) return qs_hit(s, s->px, s->py);
	if (s->hover_panel == &s->chat) return chat_hit(s, s->px, s->py);
	if (s->hover_panel == &s->chat_btn) {
		struct wc_bar_layout bar = wc_bar_layout(s->chat_btn.w, s->scale);
		return s->px >= bar.chat_x && s->px < bar.chat_x + bar.chat_width ? HIT_CHAT : HIT_NONE;
	}
	if (s->hover_panel == &s->pwr) return pwr_hit(s, s->px, s->py);
	if (s->hover_panel == &s->pwr_btn) return s->px >= 2 * s->scale ? HIT_PWR : HIT_NONE;
	if (s->hover_panel == &s->apps_btn) return s->px >= 8 * s->scale ? HIT_APPS : HIT_NONE;
	if (s->hover_panel == &s->status)
		return in(wifi_rect(s, s->status.w, s->status.h), s->px, s->py) ? HIT_WIFI :
			in(clock_rect(s, s->status.w, s->status.h), s->px, s->py) ? HIT_CLOCK : HIT_NONE;
	return HIT_NONE;
}

static void update_hover(struct st *s) {
	int h = hit(s);
	if (h == s->hover) return;
	s->hover = h;
	repaint(s, s->hover_panel);
}

static void drag_update(struct st *s) {
	struct rect r = s->dragging == DRAG_VOL ? s->q_vol : s->q_bright;
	int v = (int)((s->px - r.x) * 100 / r.w + 0.5);
	if (v > 100) v = 100;
	if (v < (s->dragging == DRAG_BRIGHT ? 1 : 0)) v = s->dragging == DRAG_BRIGHT ? 1 : 0;
	if (s->dragging == DRAG_VOL) s->vol = v; else s->bright = v;
	paint(s, &s->qs, draw_qs);
}

static void drag_commit(struct st *s) {
	char v[8];
	snprintf(v, sizeof(v), "%d", s->dragging == DRAG_VOL ? s->vol : s->bright);
	if (s->dragging == DRAG_VOL) helper(JOB_SET_SOUND, false, "volume", v);
	else helper(JOB_ACTION, true, "brightness", v);
	s->dragging = DRAG_NONE;
}

// Lock and Suspend act at once. Restart and Power off arm on the first click
// ("Confirm?") and run on a second click within 4 s. Returns true when the
// panel should close. Shared by Quick settings and the power dropdown.
static bool power_activate(struct st *s, int i) {
	if (i == PW_LOCK) { spawn_shell("exec swaylock -f -c ffffff"); return true; }
	if (i == PW_SUSPEND) { const char *args[] = {SUSPEND_COMMAND, NULL}; job_start(JOB_SUSPEND, args); return true; }
	if (s->armed != i) { s->armed = i; s->armed_at = time(NULL); return false; }
	helper(JOB_ACTION, true, "power-action", i == PW_RESTART ? "reboot" : "poweroff");
	return true;
}

static void qs_activate(struct st *s, int h) {
	if (h == HIT_NONE) { qs_close(s); return; } // click outside the panel
	if (h == HIT_QS_MUTE && s->vol_ok) { s->muted = !s->muted; helper(JOB_SET_SOUND, false, "mute", NULL); }
	else if (h == HIT_QS_VOL || h == HIT_QS_BRIGHT) { s->dragging = h == HIT_QS_VOL ? DRAG_VOL : DRAG_BRIGHT; drag_update(s); return; }
	else if (h == HIT_QS_WIFISET) { quick_launch(QB_WIFI); qs_close(s); return; }
	else if (h == HIT_QS_PRIVACY) { quick_launch(QB_PRIVACY); qs_close(s); return; }
	else if (h >= HIT_QS_PROF && h < HIT_QS_PROF + MAX_PROFILES) {
		helper(JOB_ACTION, true, "net-connect", s->profiles[h - HIT_QS_PROF].id);
		snprintf(s->wifi_label, sizeof(s->wifi_label), "Connecting\u2026");
	} else if (h >= HIT_QS_POWER && h < HIT_QS_POWER + PW_COUNT) {
		if (power_activate(s, h - HIT_QS_POWER)) { qs_close(s); return; }
	}
	paint(s, &s->qs, draw_qs);
}

static void activate(struct st *s, int h) {
	if (h == HIT_APPS) { chat_close(s); menu_toggle(s); return; }
	if (h == HIT_CLOCK) { chat_close(s); qs_toggle(s); return; }
	if (h == HIT_WIFI) { menu_close(s); qs_close(s); chat_close(s); pwr_close(s); quick_launch(QB_WIFI); return; }
	if (h == HIT_CHAT) { chat_toggle(s); return; }
	if (h == HIT_PWR) { pwr_toggle(s); return; }
	if (s->pwr_open && s->hover_panel == &s->pwr) {
		if (h == HIT_NONE || h == HIT_PWR_PANEL) { pwr_close(s); return; } // outside the panel, or its padding
		if (power_activate(s, h - HIT_PWR_ITEM)) pwr_close(s);
		else paint(s, &s->pwr, draw_pwr);
		return;
	}
	if (s->chat_open && s->hover_panel == &s->chat) {
		if (h == HIT_NONE || h == HIT_CHAT_OPEN - 1) { chat_close(s); return; }
		chat_hit_to_cmd(s, h);
		chat_close(s);
		return;
	}
	if (s->qs_open && s->hover_panel == &s->qs) { qs_activate(s, h); return; }
	if (!s->menu_open || s->hover_panel != &s->menu) return;
	if (h == HIT_NONE) { menu_close(s); return; } // click outside the panel
	if (h >= HIT_QB && h < HIT_QB + QB_COUNT) { quick_launch(h - HIT_QB); menu_close(s); return; }
	if (h >= HIT_CAT && h < HIT_CAT + CAT_COUNT) {
		s->cat = (enum cat)(h - HIT_CAT); s->query[0] = 0; s->scroll = 0; s->sel = 0;
		filter_apps(s); paint(s, &s->menu, draw_menu); return;
	}
	if (h >= HIT_TILE && h < HIT_TILE + s->nfiltered) { launch_app(&apps[s->filtered[h - HIT_TILE]]); menu_close(s); }
}

static void set_cursor(struct st *s, uint32_t serial) {
	if (!s->cursor_theme) return;
	struct wl_cursor *c = wl_cursor_theme_get_cursor(s->cursor_theme, "left_ptr");
	if (!c) c = wl_cursor_theme_get_cursor(s->cursor_theme, "default");
	if (!c || !c->image_count) return;
	struct wl_cursor_image *img = c->images[0];
	wl_surface_attach(s->cursor_surf, wl_cursor_image_get_buffer(img), 0, 0);
	wl_surface_damage_buffer(s->cursor_surf, 0, 0, img->width, img->height);
	wl_surface_commit(s->cursor_surf);
	wl_pointer_set_cursor(s->pointer, serial, s->cursor_surf, img->hotspot_x, img->hotspot_y);
}

static struct panel *panel_for(struct st *s, struct wl_surface *surf) {
	if (surf && surf == s->menu.surf) return &s->menu;
	if (surf && surf == s->qs.surf) return &s->qs;
	if (surf && surf == s->chat.surf) return &s->chat;
	if (surf && surf == s->chat_btn.surf) return &s->chat_btn;
	if (surf && surf == s->pwr.surf) return &s->pwr;
	if (surf && surf == s->pwr_btn.surf) return &s->pwr_btn;
	if (surf && surf == s->apps_btn.surf) return &s->apps_btn;
	if (surf && surf == s->status.surf) return &s->status;
	return NULL;
}

static void p_enter(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *surf, wl_fixed_t x, wl_fixed_t y) {
	struct st *s = data;
	struct panel *old = s->hover_panel;
	s->hover_panel = panel_for(s, surf);
	s->px = wl_fixed_to_double(x); s->py = wl_fixed_to_double(y);
	set_cursor(s, serial);
	if (old && old != s->hover_panel) { s->hover = HIT_NONE; repaint(s, old); }
	update_hover(s);
}
static void p_leave(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *surf) {
	struct st *s = data;
	struct panel *old = panel_for(s, surf);
	if (old && old == s->hover_panel) { s->hover_panel = NULL; s->hover = HIT_NONE; repaint(s, old); }
}
static void p_motion(void *data, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
	struct st *s = data;
	if (s->keyboard_focus) { s->keyboard_focus = false; repaint(s, s->hover_panel); }
	s->px = wl_fixed_to_double(x); s->py = wl_fixed_to_double(y);
	if (s->dragging && s->hover_panel == &s->qs) { drag_update(s); return; }
	update_hover(s);
}
static void p_button(void *data, struct wl_pointer *p, uint32_t serial, uint32_t time, uint32_t button, uint32_t state) {
	struct st *s = data;
	if (state == WL_POINTER_BUTTON_STATE_RELEASED && s->dragging) { drag_commit(s); paint(s, &s->qs, draw_qs); return; }
	if (state != WL_POINTER_BUTTON_STATE_PRESSED || button != BTN_LEFT || !s->hover_panel) return;
	repeat_stop(s);
	if (s->compose) xkb_compose_state_reset(s->compose);
	s->keyboard_focus = false;
	activate(s, hit(s));
}
static void p_axis(void *data, struct wl_pointer *p, uint32_t time, uint32_t axis, wl_fixed_t value) {
	struct st *s = data;
	if (axis != WL_POINTER_AXIS_VERTICAL_SCROLL) return;
	if (s->hover_panel == &s->qs) {
		s->qs_scroll += wl_fixed_to_double(value) * 4;
		if (s->qs_scroll < 0) s->qs_scroll = 0;
		if (s->qs_scroll > s->qs_max_scroll) s->qs_scroll = s->qs_max_scroll;
		layout_qs(s, s->qs.w, s->qs.h);
		s->hover = hit(s);
		paint(s, &s->qs, draw_qs);
		return;
	}
	if (s->hover_panel != &s->menu) return;
	s->scroll += wl_fixed_to_double(value) * 4;
	if (s->scroll < 0) s->scroll = 0;
	if (s->scroll > s->max_scroll) s->scroll = s->max_scroll;
	s->hover = hit(s);
	paint(s, &s->menu, draw_menu);
}
static const struct wl_pointer_listener pointer_listener = {
	.enter = p_enter, .leave = p_leave, .motion = p_motion, .button = p_button, .axis = p_axis,
};

static void k_keymap(void *data, struct wl_keyboard *k, uint32_t format, int32_t fd, uint32_t size) {
	struct st *s = data;
	if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1) { close(fd); return; }
	char *map = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
	close(fd);
	if (map == MAP_FAILED) return;
	struct xkb_keymap *km = xkb_keymap_new_from_string(s->xkb, map, XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
	munmap(map, size);
	if (!km) return;
	repeat_stop(s);
	if (s->compose) xkb_compose_state_reset(s->compose);
	if (s->xkb_state) xkb_state_unref(s->xkb_state);
	if (s->keymap) xkb_keymap_unref(s->keymap);
	s->keymap = km; s->xkb_state = xkb_state_new(km);
}
static void k_enter(void *data, struct wl_keyboard *k, uint32_t serial, struct wl_surface *surf, struct wl_array *keys) {}
static void k_leave(void *data, struct wl_keyboard *k, uint32_t serial, struct wl_surface *surf) {
	struct st *s = data; repeat_stop(s);
	if (s->compose) xkb_compose_state_reset(s->compose);
}

static void ensure_visible(struct st *s) {
	if (!s->nfiltered) return;
	struct rect t = tile_rect(s, s->sel);
	if (t.y < s->r_grid.y) s->scroll -= s->r_grid.y - t.y;
	else if (t.y + t.h > s->r_grid.y + s->r_grid.h) s->scroll += t.y + t.h - (s->r_grid.y + s->r_grid.h);
}

struct focus_target { int hit; struct rect rect; };
static int panel_targets(struct st *s, struct focus_target *targets) {
	int n = 0;
#define TARGET(hit_value, rectangle) targets[n++] = (struct focus_target){ hit_value, rectangle }
	if (s->chat_open) {
		TARGET(HIT_CHAT_OPEN, s->r_chat_open);
		TARGET(HIT_CHAT_SETTINGS, s->r_chat_settings);
		TARGET(HIT_CHAT_QUIT, s->r_chat_quit);
	} else if (s->pwr_open) {
		for (int i = 0; i < PW_COUNT; i++) TARGET(HIT_PWR_ITEM + i, s->r_pwr_item[i]);
	} else if (s->qs_open) {
		if (s->vol_ok) { TARGET(HIT_QS_MUTE, s->q_mute); TARGET(HIT_QS_VOL, s->q_vol); }
		if (s->bright_ok) TARGET(HIT_QS_BRIGHT, s->q_bright);
		for (int i = 0; i < s->nprofiles && i < MAX_PROFILES; i++)
			if (!s->wifi_ssid[0] || strcmp(s->wifi_ssid, s->profiles[i].ssid))
				TARGET(HIT_QS_PROF + i, s->q_prof[i]);
		TARGET(HIT_QS_WIFISET, s->q_wifiset);
		TARGET(HIT_QS_PRIVACY, s->q_privacy);
		for (int i = 0; i < PW_COUNT; i++) TARGET(HIT_QS_POWER + i, s->q_power[i]);
	}
#undef TARGET
	return n;
}

static void panel_key(struct st *s, xkb_keysym_t sym) {
	if (sym == XKB_KEY_Escape) { chat_close(s); pwr_close(s); qs_close(s); return; }
	struct focus_target targets[16];
	int count = panel_targets(s, targets), index = -1;
	for (int i = 0; i < count; i++) if (targets[i].hit == s->focus) index = i;
	if (!count) return;
	s->keyboard_focus = true; s->hover = HIT_NONE;
	if (index < 0) { index = 0; s->focus = targets[0].hit; }
	bool slider_focus = s->qs_open && (s->focus == HIT_QS_VOL || s->focus == HIT_QS_BRIGHT);
	if (slider_focus && (sym == XKB_KEY_Left || sym == XKB_KEY_Right || sym == XKB_KEY_Home || sym == XKB_KEY_End)) {
		int *value = s->focus == HIT_QS_VOL ? &s->vol : &s->bright;
		int minimum = s->focus == HIT_QS_VOL ? 0 : 1;
		*value = sym == XKB_KEY_Home ? minimum : sym == XKB_KEY_End ? 100 : *value + (sym == XKB_KEY_Left ? -5 : 5);
		if (*value < minimum) *value = minimum;
		if (*value > 100) *value = 100;
		s->dragging = s->focus == HIT_QS_VOL ? DRAG_VOL : DRAG_BRIGHT;
		drag_commit(s);
	} else if (sym == XKB_KEY_Tab || sym == XKB_KEY_ISO_Left_Tab || sym == XKB_KEY_Up ||
			sym == XKB_KEY_Down || sym == XKB_KEY_Left || sym == XKB_KEY_Right || sym == XKB_KEY_Home || sym == XKB_KEY_End) {
		bool backwards = sym == XKB_KEY_ISO_Left_Tab || sym == XKB_KEY_Up || sym == XKB_KEY_Left;
		if (sym == XKB_KEY_Tab && s->xkb_state && xkb_state_mod_name_is_active(s->xkb_state, XKB_MOD_NAME_SHIFT, XKB_STATE_MODS_EFFECTIVE) > 0)
			backwards = true;
		index = sym == XKB_KEY_Home ? 0 : sym == XKB_KEY_End ? count - 1 : (index + count + (backwards ? -1 : 1)) % count;
		s->focus = targets[index].hit;
	} else if (sym == XKB_KEY_Return || sym == XKB_KEY_KP_Enter || sym == XKB_KEY_space) {
		if (s->chat_open) { chat_hit_to_cmd(s, s->focus); chat_close(s); return; }
		if (s->pwr_open) {
			if (power_activate(s, s->focus - HIT_PWR_ITEM)) { pwr_close(s); return; }
		} else if (!slider_focus) { qs_activate(s, s->focus); if (!s->qs_open) return; }
	} else return;
	if (s->qs_open) {
		struct rect r = targets[index].rect;
		double top = s->tab_h * s->scale + 8 * s->scale, bottom = s->qs.h - 8 * s->scale;
		if (r.y < top) s->qs_scroll -= top - r.y;
		else if (r.y + r.h > bottom) s->qs_scroll += r.y + r.h - bottom;
		if (s->qs_scroll < 0) s->qs_scroll = 0;
		if (s->qs_scroll > s->qs_max_scroll) s->qs_scroll = s->qs_max_scroll;
		paint(s, &s->qs, draw_qs);
	} else if (s->chat_open) paint(s, &s->chat, draw_chat);
	else if (s->pwr_open) paint(s, &s->pwr, draw_pwr);
}

static void handle_status_key(struct st *s, uint32_t key) {
	if (!s->xkb_state) return;
	xkb_keysym_t sym = xkb_state_key_get_one_sym(s->xkb_state, key + 8);
	if (s->chat_open || s->pwr_open || s->qs_open) { panel_key(s, sym); return; }
	if (!s->menu_open) return;
	s->keyboard_focus = true; s->hover = HIT_NONE;
	if ((sym == XKB_KEY_p || sym == XKB_KEY_P) &&
			xkb_state_mod_name_is_active(s->xkb_state, XKB_MOD_NAME_CTRL, XKB_STATE_MODS_EFFECTIVE) > 0) {
		launch_settings("privacy"); menu_close(s); return;
	}
	size_t len = strlen(s->query);
    if (xkb_state_mod_name_is_active(s->xkb_state, XKB_MOD_NAME_CTRL, XKB_STATE_MODS_EFFECTIVE) > 0 &&
        (sym == XKB_KEY_u || sym == XKB_KEY_U)) { s->query[0] = 0; s->sel = 0; }
    else switch (sym) {
	case XKB_KEY_Escape:
		if (s->compose) xkb_compose_state_reset(s->compose);
		if (s->query[0]) { s->query[0] = 0; s->sel = 0; break; }
		menu_close(s); return;
	case XKB_KEY_Return: case XKB_KEY_KP_Enter:
		if (s->nfiltered) { launch_app(&apps[s->filtered[s->sel]]); menu_close(s); }
		return;
	case XKB_KEY_BackSpace:
		if (len) { const char *prev = g_utf8_find_prev_char(s->query, s->query + len); s->query[prev ? prev - s->query : 0] = 0; }
		s->sel = 0; break;
	case XKB_KEY_Left: if (s->sel > 0) s->sel--; break;
	case XKB_KEY_Right: if (s->sel + 1 < s->nfiltered) s->sel++; break;
	case XKB_KEY_Up: if (s->sel - s->cols >= 0) s->sel -= s->cols; break;
	case XKB_KEY_Down: if (s->sel + s->cols < s->nfiltered) s->sel += s->cols; break;
	case XKB_KEY_Page_Up: s->sel -= s->cols * (int)fmax(1, s->r_grid.h / (108 * s->scale)); break;
    case XKB_KEY_Page_Down: s->sel += s->cols * (int)fmax(1, s->r_grid.h / (108 * s->scale)); break;
    case XKB_KEY_Home: s->sel = 0; break;
	case XKB_KEY_End: if (s->nfiltered) s->sel = s->nfiltered - 1; break;
	case XKB_KEY_Tab: case XKB_KEY_ISO_Left_Tab: {
		int step = sym == XKB_KEY_Tab ? 1 : CAT_COUNT - 1;
		int c = s->cat;
		do c = (c + step) % CAT_COUNT; while (!s->cat_used[c]);
		s->cat = (enum cat)c; s->query[0] = 0; s->sel = 0; s->scroll = 0;
		break;
	}
	default:
		if (xkb_state_mod_name_is_active(s->xkb_state, XKB_MOD_NAME_CTRL, XKB_STATE_MODS_EFFECTIVE) > 0 ||
		    xkb_state_mod_name_is_active(s->xkb_state, XKB_MOD_NAME_ALT, XKB_STATE_MODS_EFFECTIVE) > 0) return;
		char utf8[64] = "";
		int n;
		if (wc_compose_key(s->compose, sym, utf8, sizeof(utf8))) n = strlen(utf8);
		else n = xkb_state_key_get_utf8(s->xkb_state, key + 8, utf8, sizeof(utf8));
		if (n <= 0 || (unsigned char)utf8[0] < 0x20 || utf8[0] == 0x7f || len + n + 1 > sizeof(s->query)) return;
		memcpy(s->query + len, utf8, n + 1);
		s->sel = 0; s->scroll = 0;
	}
	filter_apps(s);
	layout_menu(s, s->menu.w, s->menu.h);
	ensure_visible(s);
	paint(s, &s->menu, draw_menu);
}
static void k_key(void *data, struct wl_keyboard *k, uint32_t serial, uint32_t time, uint32_t key, uint32_t state) {
    struct st *s = data;
    if (state != WL_KEYBOARD_KEY_STATE_PRESSED) {
        if (key == s->repeat_key) repeat_stop(s);
        return;
    }
    repeat_stop(s);
    handle_status_key(s, key);
    if (!s->menu_open || !s->keymap || s->repeat_rate <= 0 || s->repeat_fd < 0 ||
        !xkb_keymap_key_repeats(s->keymap, key + 8)) return;
    xkb_keysym_t sym = xkb_state_key_get_one_sym(s->xkb_state, key + 8);
    if (sym == XKB_KEY_Return || sym == XKB_KEY_KP_Enter || sym == XKB_KEY_Escape ||
        sym == XKB_KEY_Tab || sym == XKB_KEY_ISO_Left_Tab ||
        (s->compose && xkb_compose_state_get_status(s->compose) == XKB_COMPOSE_COMPOSING)) return;
    s->repeat_key = key;
    int rate = s->repeat_rate > 100 ? 100 : s->repeat_rate;
    int delay = s->repeat_delay > 0 ? s->repeat_delay : 1;
    struct itimerspec repeat = {.it_value = {delay / 1000, (delay % 1000) * 1000000L},
        .it_interval = {0, 1000000000L / rate}};
    timerfd_settime(s->repeat_fd, 0, &repeat, NULL);
}

static void k_mods(void *data, struct wl_keyboard *k, uint32_t serial, uint32_t dep, uint32_t lat, uint32_t lock, uint32_t group) {
	struct st *s = data;
	if (s->xkb_state) xkb_state_update_mask(s->xkb_state, dep, lat, lock, 0, 0, group);
}
static void k_repeat(void *data, struct wl_keyboard *k, int32_t rate, int32_t delay) {
	struct st *s = data; s->repeat_rate = rate; s->repeat_delay = delay; repeat_stop(s);
}
static const struct wl_keyboard_listener keyboard_listener = { k_keymap, k_enter, k_leave, k_key, k_mods, k_repeat };

static void seat_caps(void *data, struct wl_seat *seat, uint32_t caps) {
	struct st *s = data;
	if ((caps & WL_SEAT_CAPABILITY_POINTER) && !s->pointer) {
		s->pointer = wl_seat_get_pointer(seat);
		wl_pointer_add_listener(s->pointer, &pointer_listener, s);
	} else if (!(caps & WL_SEAT_CAPABILITY_POINTER) && s->pointer) {
		wl_pointer_destroy(s->pointer); s->pointer = NULL;
	}
	if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !s->keyboard) {
		s->keyboard = wl_seat_get_keyboard(seat);
		wl_keyboard_add_listener(s->keyboard, &keyboard_listener, s);
	} else if (!(caps & WL_SEAT_CAPABILITY_KEYBOARD) && s->keyboard) {
		repeat_stop(s);
		if (s->compose) xkb_compose_state_reset(s->compose);
		wl_keyboard_destroy(s->keyboard); s->keyboard = NULL;
	}
}
static void seat_name(void *data, struct wl_seat *seat, const char *name) {}
static const struct wl_seat_listener seat_listener = { seat_caps, seat_name };

static void reg_global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t ver) {
	struct st *s = data;
	if (!strcmp(iface, wl_compositor_interface.name))
		s->comp = wl_registry_bind(reg, name, &wl_compositor_interface, 4);
	else if (!strcmp(iface, wl_shm_interface.name))
		s->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	else if (!strcmp(iface, wl_seat_interface.name) && !s->seat) {
		// v4: pointer frame/axis_source events (v5) are not needed.
		s->seat = wl_registry_bind(reg, name, &wl_seat_interface, ver < 4 ? ver : 4);
		wl_seat_add_listener(s->seat, &seat_listener, s);
	} else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name))
		s->layer_shell = wl_registry_bind(reg, name, &zwlr_layer_shell_v1_interface, ver < 4 ? ver : 4);
}
static void reg_remove(void *data, struct wl_registry *reg, uint32_t name) {}
static const struct wl_registry_listener reg_listener = { reg_global, reg_remove };

// ------------------------------------------------------------- --render
static int status_selftest(void) {
#define CHECK(condition) do { if (!(condition)) { fprintf(stderr, "cube-status selftest: line %d: %s\n", __LINE__, #condition); return 1; } } while (0)
	struct st s = { .scale = 1, .tab_h = 40, .focus = HIT_QS_PRIVACY, .qs_open = true };
	for (int width = 800; width <= 3840; width += 80) for (int step = 0; step <= 3; step++) {
		double scale = 1 + step * .5;
		struct wc_bar_layout bar = wc_bar_layout(width, scale);
		CHECK(bar.chat_x >= (int)(104 * scale));
		CHECK(bar.chat_width >= (int)(32 * scale));
		CHECK(bar.chat_x + bar.chat_width < width - (int)(160 * scale) - bar.status_width);
		CHECK(bar.tabs_end < bar.chat_x && bar.status_width > 0);
	}
	const char *connected = "{\"updated_epoch\":100,\"association_state\":\"COMPLETED\",\"ssid\":\"Private network\",\"signal_dbm\":-62,\"connectivity_ok\":false}";
	wifi_parse(&s, connected, 105);
	CHECK(!strcmp(s.wifi_label, "Private network (no internet)") && s.wifi_bars == 3);
	s.privacy_hide_network = true;
	wifi_parse(&s, connected, 105);
	CHECK(!strcmp(s.wifi_label, "Connected (no internet)"));
	CHECK(!strcmp(s.wifi_ssid, "Private network"));
	wifi_parse(&s, connected, 121);
	CHECK(!s.wifi_ssid[0] && s.wifi_bars == -1);
	s.qs.w = 800; s.qs.h = 480; s.nprofiles = MAX_PROFILES;
	s.vol_ok = true; s.bright_ok = true;
	for (int i = 0; i < MAX_PROFILES; i++) snprintf(s.profiles[i].ssid, sizeof(s.profiles[i].ssid), "Network %d", i);
	snprintf(s.wifi_ssid, sizeof(s.wifi_ssid), "%s", s.profiles[0].ssid);
	struct focus_target targets[16];
	int count = panel_targets(&s, targets);
	CHECK(count == 13);
	for (int i = 0; i < count; i++) CHECK(targets[i].hit != HIT_QS_PROF);
	for (int scale_step = 0; scale_step < 3; scale_step++) {
		s.scale = 1 + scale_step * .5; s.qs_scroll = 0; s.focus = HIT_QS_PRIVACY;
		layout_qs(&s, s.qs.w, s.qs.h);
		CHECK(s.qs_max_scroll > 0 && s.q_privacy.x >= 0);
		CHECK(s.r_qs.x + s.r_qs.w <= s.qs.w);
		panel_key(&s, XKB_KEY_End);
		CHECK(s.focus == HIT_QS_POWER + PW_OFF && s.qs_scroll > 0);
		layout_qs(&s, s.qs.w, s.qs.h);
		CHECK(s.q_power[PW_OFF].y + s.q_power[PW_OFF].h <= s.qs.h - 8 * s.scale + .01);
		panel_key(&s, XKB_KEY_Tab);
		CHECK(s.focus == HIT_QS_MUTE);
		layout_qs(&s, s.qs.w, s.qs.h);
		CHECK(s.q_mute.y >= s.tab_h * s.scale);
		panel_key(&s, XKB_KEY_ISO_Left_Tab);
		CHECK(s.focus == HIT_QS_POWER + PW_OFF);
	}
	s.vol_ok = s.bright_ok = false;
	count = panel_targets(&s, targets);
	for (int i = 0; i < count; i++) CHECK(targets[i].hit != HIT_QS_MUTE && targets[i].hit != HIT_QS_VOL && targets[i].hit != HIT_QS_BRIGHT);
	s.qs_open = false; s.chat_open = true; s.focus = HIT_CHAT_OPEN;
	panel_key(&s, XKB_KEY_Tab); CHECK(s.focus == HIT_CHAT_SETTINGS);
	panel_key(&s, XKB_KEY_ISO_Left_Tab); CHECK(s.focus == HIT_CHAT_OPEN);
	s.chat_open = false; s.pwr_open = true; s.focus = HIT_PWR_ITEM;
	panel_key(&s, XKB_KEY_Up); CHECK(s.focus == HIT_PWR_ITEM + PW_OFF);
	puts("cube-status: network privacy, stale status, keyboard navigation and scaled scrolling passed");
	return 0;
#undef CHECK
}

static void draw_preview(struct st *s, cairo_t *cr, int w, int h) {
	// Desktop preview uses the same live palette as the native widgets.
	double k = s->scale;
	color(cr, theme.background); cairo_paint(cr);
	struct wc_bar_layout bar = wc_bar_layout(w, k);
	int bh = (int)(s->tab_h * k), aw = (int)(APPS_W * k), sw = bar.status_width;
	color(cr, C_BAR); cairo_rectangle(cr, 0, 0, w, bh); cairo_fill(cr);
	draw_apps_btn(s, cr, aw, bh);
	struct rect tab = { aw + 8, 6 * k, 180 * k, bh - 12 * k }; // one example tab, as whitecube draws it
	box(cr, tab, k, C_INK, C_SURFACE);
	text(cr, "3 tabs  ▾", 13 * k, true, tab.x, tab.y, tab.w, tab.h, true, 1, C_INK);
	cairo_save(cr); cairo_translate(cr, w - (int)(STATUS_RIGHT * k) - sw, 0); draw_status(s, cr, sw, bh); cairo_restore(cr);
	draw_chat_btn(s, cr, w, bh);
	for (int i = 0; i < 4; i++) {
		color(cr, i ? C_MUTED : C_INK);
		cairo_rectangle(cr, w - (112 + POWER_W) * k + i * 26 * k, (bh - 18 * k) / 2, 18 * k, 18 * k);
		cairo_fill(cr);
	}
	cairo_save(cr); cairo_translate(cr, w - (int)(POWER_W * k), 0); draw_pwr_btn(s, cr, (int)(POWER_W * k), bh); cairo_restore(cr);
	if (s->menu_open) draw_menu(s, cr, w, h);
	if (s->qs_open) draw_qs(s, cr, w, h);
	if (s->chat_open) draw_chat(s, cr, w, h);
	if (s->pwr_open) draw_pwr(s, cr, w, h);
}

static int render_png(struct st *s, const char *path, int w, int h) {
	cairo_surface_t *cs = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, w, h);
	cairo_t *cr = cairo_create(cs);
	draw_preview(s, cr, w, h);
	cairo_destroy(cr);
	int rc = cairo_surface_write_to_png(cs, path) == CAIRO_STATUS_SUCCESS ? 0 : 1;
	cairo_surface_destroy(cs);
	printf("%s %s\n", rc ? "failed" : "wrote", path);
	return rc;
}

static int render_mode(struct st *s, const char *dir) {
	s->preview = true;
	read_geometry(s);
	status_update(s);
	snprintf(s->wifi_label, sizeof(s->wifi_label), "%s", s->privacy_hide_network ? "Connected" : "Example network");
	snprintf(s->wifi_ssid, sizeof(s->wifi_ssid), "Example network"); s->wifi_bars = 3;
	int rc = 0;
	char path[1024];
	snprintf(path, sizeof(path), "%s/bar.png", dir);
	rc |= render_png(s, path, 1600, (int)(s->tab_h * s->scale));
	load_apps();
	s->cat_used[CAT_ALL] = true;
	for (int i = 0; i < napps; i++) s->cat_used[apps[i].cat] = true;
	s->menu_open = true; s->keyboard_focus = true; s->cat = CAT_ALL; filter_apps(s);
	snprintf(path, sizeof(path), "%s/menu.png", dir);
	rc |= render_png(s, path, 1600, 900);
	snprintf(s->query, sizeof(s->query), "fire");
	filter_apps(s);
	snprintf(path, sizeof(path), "%s/menu-search.png", dir);
	rc |= render_png(s, path, 1600, 900);
	s->menu_open = false; s->qs_open = true; s->armed = PW_OFF;
	s->vol_ok = true; s->vol = 62; s->bright_ok = true; s->bright = 80;
	snprintf(s->battery_line, sizeof(s->battery_line), "Battery 84%% \u00b7 Discharging");
	snprintf(s->wifi_label, sizeof(s->wifi_label), "%s", s->privacy_hide_network ? "Connected" : "Example network");
	snprintf(s->wifi_ssid, sizeof(s->wifi_ssid), "Example network"); s->wifi_bars = 3;
	snprintf(s->profiles[0].ssid, sizeof(s->profiles[0].ssid), "Example network");
	snprintf(s->profiles[1].ssid, sizeof(s->profiles[1].ssid), "Guest network"); s->nprofiles = 2;
	snprintf(path, sizeof(path), "%s/quick.png", dir);
	rc |= render_png(s, path, 1600, 900);
	s->privacy_hide_network = true;
	snprintf(s->wifi_label, sizeof(s->wifi_label), "Connected");
	s->keyboard_focus = true; s->focus = HIT_QS_PRIVACY; s->armed = -1;
	snprintf(path, sizeof(path), "%s/quick-private.png", dir);
	rc |= render_png(s, path, 1600, 900);
	double original_scale = s->scale;
	s->scale = 1.5; s->qs.w = 1280; s->qs.h = 720;
	layout_qs(s, s->qs.w, s->qs.h);
	s->qs_scroll = s->qs_max_scroll;
	snprintf(path, sizeof(path), "%s/quick-scaled.png", dir);
	rc |= render_png(s, path, 1280, 720);
	s->scale = original_scale; s->qs_scroll = 0;
	s->menu_open = false; s->qs_open = false; s->chat_open = true;
	snprintf(path, sizeof(path), "%s/chat.png", dir);
	rc |= render_png(s, path, 1600, 900);
	s->chat_open = false; s->pwr_open = true; s->armed = PW_OFF;
	snprintf(path, sizeof(path), "%s/power.png", dir);
	rc |= render_png(s, path, 1600, 900);
	printf("%d apps\n", napps);
	return rc;
}

int main(int argc, char **argv) {
	setlocale(LC_CTYPE, "");
	setlocale(LC_COLLATE, "");
	setlocale(LC_MESSAGES, "");
	struct st s;
	memset(&s, 0, sizeof(s));
	g_st = &s;
	s.hover = HIT_NONE;
	s.armed = -1;
	s.repeat_fd = -1; s.repeat_rate = 25; s.repeat_delay = 600;
	if (argc == 2 && !strcmp(argv[1], "--selftest")) return status_selftest();
	if (argc == 3 && !strcmp(argv[1], "--render")) return render_mode(&s, argv[2]);
	// Block before anything else: an early Super press must not kill us (default SIGUSR1 action).
	sigset_t sigs; sigemptyset(&sigs); sigaddset(&sigs, SIGUSR1); sigaddset(&sigs, SIGUSR2); sigaddset(&sigs, SIGCHLD);
	sigprocmask(SIG_BLOCK, &sigs, NULL);
	int sfd = signalfd(-1, &sigs, SFD_CLOEXEC | SFD_NONBLOCK);
	signal(SIGCHLD, SIG_DFL); // job completion preserves helper exit status
	s.running = true;
	read_geometry(&s);
	status_update(&s);
	s.dpy = wl_display_connect(NULL);
	if (!s.dpy) { fprintf(stderr, "cube-status: no wayland display\n"); return 1; }
	struct wl_registry *reg = wl_display_get_registry(s.dpy);
	wl_registry_add_listener(reg, &reg_listener, &s);
	wl_display_roundtrip(s.dpy);
	if (!s.comp || !s.shm || !s.layer_shell) {
		fprintf(stderr, "cube-status: missing globals\n");
		return 1;
	}
	s.xkb = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	s.compose = wc_compose_create(s.xkb);
	s.repeat_fd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC | TFD_NONBLOCK);
	const char *size = getenv("XCURSOR_SIZE");
	s.cursor_theme = wl_cursor_theme_load(getenv("XCURSOR_THEME"), size ? atoi(size) : 24, s.shm);
	s.cursor_surf = wl_compositor_create_surface(s.comp);
	bar_create(&s);

	int tfd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC | TFD_NONBLOCK);
	struct itimerspec ts = { .it_value = { 2, 0 }, .it_interval = { 2, 0 } };
	timerfd_settime(tfd, 0, &ts, NULL);
	int dfd = wl_display_get_fd(s.dpy);
	while (s.running) {
		struct pollfd fds[4 + MAX_JOBS * 2];
		int nf = 0;
		fds[nf++] = (struct pollfd){ dfd, POLLIN, 0 };
		fds[nf++] = (struct pollfd){ tfd, POLLIN, 0 };
		fds[nf++] = (struct pollfd){ sfd, POLLIN, 0 };
		fds[nf++] = (struct pollfd){ s.repeat_fd, POLLIN, 0 };
		for (int i = 0; i < MAX_JOBS; i++) if (jobs[i].active) {
			if (jobs[i].fd >= 0) fds[nf++] = (struct pollfd){ jobs[i].fd, POLLIN, 0 };
			if (jobs[i].err >= 0) fds[nf++] = (struct pollfd){ jobs[i].err, POLLIN, 0 };
		}
		while (wl_display_prepare_read(s.dpy) != 0) wl_display_dispatch_pending(s.dpy);
		wl_display_flush(s.dpy);
		if (poll(fds, nf, -1) < 0) {
			wl_display_cancel_read(s.dpy);
			if (errno == EINTR) continue;
			break;
		}
		if (fds[0].revents & POLLIN) { if (wl_display_read_events(s.dpy) < 0) break; }
		else wl_display_cancel_read(s.dpy);
		if (fds[0].revents & (POLLERR | POLLHUP)) break;
		if (wl_display_dispatch_pending(s.dpy) < 0) break;
        jobs_pump(&s);
        if (s.qs_open) paint(&s, &s.qs, draw_qs);
        if (fds[3].revents & POLLIN) {
            uint64_t repeats;
            if (read(s.repeat_fd, &repeats, sizeof(repeats)) > 0 && s.repeat_key)
                for (uint64_t i = 0; i < repeats && i < 8 && s.repeat_key; i++) handle_status_key(&s, s.repeat_key);
        }
        notice_sync(&s);
		if (fds[2].revents & POLLIN) {
			struct signalfd_siginfo si;
			while (read(sfd, &si, sizeof(si)) == sizeof(si)) {
				if (si.ssi_signo == SIGUSR1) menu_toggle(&s);
				else if (si.ssi_signo == SIGUSR2) qs_toggle(&s);
			}
		}
		if (fds[1].revents & POLLIN) {
			uint64_t e;
			if (read(tfd, &e, sizeof(e)) < 0) { /* spurious wakeup */ }
			double old_scale = s.scale; int old_tab = s.tab_h;
			bool old_hide_network = s.privacy_hide_network;
			struct status_theme old_theme = theme;
			read_geometry(&s);
			status_update(&s);
			bool geometry_changed = s.scale != old_scale || s.tab_h != old_tab;
			if (geometry_changed) bar_resize(&s);
			if (geometry_changed || old_hide_network != s.privacy_hide_network || memcmp(&old_theme, &theme, sizeof(theme))) {
				paint(&s, &s.apps_btn, draw_apps_btn); paint(&s, &s.status, draw_status);
				paint(&s, &s.chat_btn, draw_chat_btn); paint(&s, &s.pwr_btn, draw_pwr_btn);
				if (s.menu_open) paint(&s, &s.menu, draw_menu);
				if (s.qs_open) paint(&s, &s.qs, draw_qs);
				if (s.chat_open) paint(&s, &s.chat, draw_chat);
				if (s.pwr_open) paint(&s, &s.pwr, draw_pwr);
			}
			char shown[200];
			snprintf(shown, sizeof(shown), "%s | %s %d", s.clock_text, s.wifi_label, s.wifi_bars);
			if (strcmp(shown, s.shown)) {
				snprintf(s.shown, sizeof(s.shown), "%s", shown);
				paint(&s, &s.status, draw_status);
				if (s.qs_open) paint(&s, &s.qs, draw_qs);
			}
			if ((s.qs_open || s.pwr_open) && s.armed >= 0 && time(NULL) - s.armed_at > 4) {
				s.armed = -1;
				if (s.qs_open) paint(&s, &s.qs, draw_qs);
				if (s.pwr_open) paint(&s, &s.pwr, draw_pwr);
			}
		}
	}
	return 0;
}
