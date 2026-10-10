/* Bounded, command-free native layout persistence and shared display settings. */
#ifndef WHITECUBE_DESKTOP_STATE_H
#define WHITECUBE_DESKTOP_STATE_H
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <errno.h>
#define WC_SAVED_WINDOWS 64
struct wc_saved_window {
    char app[128], title[256];
    unsigned workspace;
    bool hidden, fullscreen, maximized, claimed;
    int x, y, w, h; /* thousandths of the available work area */
};
struct wc_desktop_state {
    unsigned workspace;
    bool desktop_visible;
    char names[4][64];
    struct wc_saved_window windows[WC_SAVED_WINDOWS];
    unsigned count;
};
struct wc_accessibility { double ui_scale, terminal_scale; bool strong, reduced_motion; };
static inline void wc_state_defaults(struct wc_desktop_state *state) {
    memset(state, 0, sizeof(*state));
    const char *names[] = {"Coding", "Play", "Monitor", "Browse"};
    for (unsigned i = 0; i < 4; i++) snprintf(state->names[i], 64, "%s", names[i]);
}
static inline bool wc_state_directory(char *path, size_t capacity) {
    const char *base = getenv("XDG_STATE_HOME"), *home = getenv("HOME");
    int length;
    if (base && base[0] == '/') length = snprintf(path, capacity, "%s/whitecube", base);
    else if (home && home[0] == '/') length = snprintf(path, capacity, "%s/.local/state/whitecube", home);
    else return false;
    if (length < 0 || (size_t)length >= capacity) return false;
    for (char *p = path + 1; ; p++) {
        if (*p != '/' && *p != '\0') continue;
        char saved = *p; *p = '\0';
        struct stat st;
        if (lstat(path, &st)) {
            if (errno != ENOENT || mkdir(path, 0700)) { *p = saved; return false; }
            if (lstat(path, &st)) { *p = saved; return false; }
        }
        if (!S_ISDIR(st.st_mode) || S_ISLNK(st.st_mode)) { *p = saved; return false; }
        *p = saved;
        if (!saved) break;
    }
    struct stat st;
    return !lstat(path, &st) && st.st_uid == getuid() && !(st.st_mode & 0022);
}
static inline void wc_hex_encode(const char *value, char *out, size_t capacity) {
    const char *digits = "0123456789abcdef";
    size_t i = 0;
    while (value[i] && i * 2 + 2 < capacity) {
        unsigned c = (unsigned char)value[i]; out[i * 2] = digits[c >> 4]; out[i * 2 + 1] = digits[c & 15]; i++;
    }
    if (!i) { out[0] = '-'; out[1] = '\0'; }
    else out[i * 2] = '\0';
}
static inline bool wc_hex_decode(const char *value, char *out, size_t capacity) {
    if (!strcmp(value, "-")) { out[0] = '\0'; return true; }
    size_t length = strlen(value);
    if (length % 2 || length / 2 >= capacity) return false;
    for (size_t i = 0; i < length; i += 2) {
        unsigned n = 0;
        for (unsigned j = 0; j < 2; j++) {
            char c = value[i + j]; unsigned v;
            if (c >= '0' && c <= '9') v = c - '0';
            else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
            else return false;
            n = n * 16 + v;
        }
        if (!n || n < 32 || n == 127) return false;
        out[i / 2] = (char)n;
    }
    out[length / 2] = '\0'; return true;
}
static inline bool wc_state_load(const char *path, struct wc_desktop_state *state) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) return false;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() || st.st_size > 262144) { close(fd); return false; }
    FILE *f = fdopen(fd, "r"); if (!f) { close(fd); return false; }
    struct wc_desktop_state result; wc_state_defaults(&result);
    char line[1200], a[257], title[513], trailing;
    unsigned workspace, desktop;
    bool valid = fgets(line, sizeof(line), f) && sscanf(line, "WC_LAYOUT_1 %u %u %c", &workspace, &desktop, &trailing) == 2 && workspace < 4 && desktop < 2;
    if (valid) { result.workspace = workspace; result.desktop_visible = desktop; }
    while (valid && fgets(line, sizeof(line), f)) {
        if (line[0] == 'N') {
            unsigned index;
            valid = sscanf(line, "N %u %256s %c", &index, a, &trailing) == 2 && index < 4 && wc_hex_decode(a, result.names[index], 64);
        } else if (line[0] == 'W') {
            if (result.count >= WC_SAVED_WINDOWS) { valid = false; break; }
            struct wc_saved_window *win = &result.windows[result.count]; unsigned hidden, full, max;
            valid = result.count < WC_SAVED_WINDOWS && sscanf(line, "W %u %u %u %u %d %d %d %d %256s %512s %c", &win->workspace, &hidden, &full, &max, &win->x, &win->y, &win->w, &win->h, a, title, &trailing) == 10;
            valid = valid && win->workspace < 4 && hidden < 2 && full < 2 && max < 2 && win->x >= 0 && win->y >= 0 && win->w >= 0 && win->h >= 0 && win->x <= 1000 && win->y <= 1000 && win->w <= 1000 && win->h <= 1000 && win->x + win->w <= 1000 && win->y + win->h <= 1000;
            valid = valid && wc_hex_decode(a, win->app, sizeof(win->app)) && wc_hex_decode(title, win->title, sizeof(win->title));
            if (valid) { win->hidden = hidden; win->fullscreen = full; win->maximized = max; result.count++; }
        } else valid = false;
    }
    if (ferror(f)) valid = false;
    fclose(f);
    if (valid) *state = result;
    return valid;
}
/* Delete only the compositor's own valid layout, without following a link. */
static inline bool wc_state_forget(const char *directory) {
    char path[1024];
    int length = snprintf(path, sizeof(path), "%s/native-layout.txt", directory);
    if (length < 0 || (size_t)length >= sizeof(path)) return false;
    struct stat st;
    if (lstat(path, &st)) return errno == ENOENT;
    struct wc_desktop_state saved;
    if (!wc_state_load(path, &saved)) return false;
    int parent = open(directory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (parent < 0) return false;
    struct stat current;
    bool okay = !fstat(parent, &current) && current.st_uid == getuid() && !(current.st_mode & 0022)
        && !fstatat(parent, "native-layout.txt", &current, AT_SYMLINK_NOFOLLOW)
        && current.st_dev == st.st_dev && current.st_ino == st.st_ino
        && !unlinkat(parent, "native-layout.txt", 0) && !fsync(parent);
    close(parent);
    return okay;
}
static inline bool wc_state_save(const char *directory, const struct wc_desktop_state *state) {
    int parent = open(directory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (parent < 0) return false;
    struct stat st;
    if (fstat(parent, &st) || st.st_uid != getuid() || (st.st_mode & 0022)) { close(parent); return false; }
    static unsigned serial;
    char name[80]; int fd = -1;
    for (unsigned attempt = 0; attempt < 8 && fd < 0; attempt++) {
        snprintf(name, sizeof(name), ".native-layout-%ld-%u", (long)getpid(), serial++);
        fd = openat(parent, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd < 0 && errno != EEXIST) break;
    }
    if (fd < 0) { close(parent); return false; }
    FILE *f = fdopen(fd, "w"); if (!f) { close(fd); unlinkat(parent, name, 0); close(parent); return false; }
    fprintf(f, "WC_LAYOUT_1 %u %u\n", state->workspace, state->desktop_visible);
    char a[257], title[513];
    for (unsigned i = 0; i < 4; i++) { wc_hex_encode(state->names[i], a, sizeof(a)); fprintf(f, "N %u %s\n", i, a); }
    for (unsigned i = 0; i < state->count && i < WC_SAVED_WINDOWS; i++) {
        const struct wc_saved_window *win = &state->windows[i];
        wc_hex_encode(win->app, a, sizeof(a)); wc_hex_encode(win->title, title, sizeof(title));
        fprintf(f, "W %u %u %u %u %d %d %d %d %s %s\n", win->workspace, win->hidden, win->fullscreen, win->maximized, win->x, win->y, win->w, win->h, a, title);
    }
    bool okay = !ferror(f) && !fflush(f) && !fsync(fd);
    if (fclose(f)) okay = false;
    if (okay) okay = !renameat(parent, name, parent, "native-layout.txt") && !fsync(parent);
    if (!okay) unlinkat(parent, name, 0);
    close(parent); return okay;
}
static inline int wc_state_match(struct wc_desktop_state *state, const char *app, const char *title, unsigned workspace) {
    if (!app || !*app) return -1;
    int fallback = -1, exact = -1;
    for (unsigned i = 0; i < state->count; i++) {
        struct wc_saved_window *saved = &state->windows[i];
        if (saved->claimed || strcmp(saved->app, app)) continue;
        if (title && !strcmp(saved->title, title)) {
            if (saved->workspace == workspace) return (int)i;
            if (exact < 0) exact = (int)i;
        }
        if (fallback < 0 || saved->workspace == workspace) fallback = (int)i;
    }
    return exact >= 0 ? exact : fallback;
}
static inline bool wc_names_load(const char *path, struct wc_desktop_state *state) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK); if (fd < 0) return false;
    struct stat st; char data[2049];
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() || st.st_size > 2048) { close(fd); return false; }
    ssize_t size = read(fd, data, 2048); close(fd); if (size <= 0) return false; data[size] = '\0';
    char names[4][64]; memcpy(names, state->names, sizeof(names));
    for (unsigned i = 0; i < 4; i++) {
        char key[16], encoded[128]; snprintf(key, sizeof(key), "\"w%u\"", i);
        const char *p = strstr(data, key);
        if (!p || !(p = strchr(p, ':')) || sscanf(p + 1, " \"%127[^\"]\"", encoded) != 1 || !wc_hex_decode(encoded, names[i], 64) || !names[i][0]) return false;
    }
    memcpy(state->names, names, sizeof(names)); return true;
}
static inline double wc_json_number(const char *data, const char *key, double fallback, double minimum, double maximum) {
    char pattern[80]; snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(data, pattern); if (!p || !(p = strchr(p, ':'))) return fallback;
    char *end; double value = strtod(p + 1, &end);
    return end != p + 1 && value >= minimum && value <= maximum ? value : fallback;
}
static inline bool wc_json_true(const char *data, const char *key) {
    char pattern[80]; snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(data, pattern); if (!p || !(p = strchr(p, ':'))) return false;
    do { p++; } while (*p == ' ' || *p == '\t');
    return !strncmp(p, "true", 4);
}
static inline bool wc_accessibility_load(const char *path, struct wc_accessibility *settings) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK); if (fd < 0) return false;
    struct stat st; char data[8193];
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() || st.st_size > 8192) { close(fd); return false; }
    ssize_t size = read(fd, data, 8192); close(fd); if (size <= 0) return false; data[size] = '\0';
    struct wc_accessibility result = {1, 1, false, false};
    result.ui_scale = wc_json_number(data, "ui_scale", 1, .8, 1.6);
    result.terminal_scale = wc_json_number(data, "terminal_scale", 1, .8, 2);
    result.strong = wc_json_true(data, "high_contrast");
    const char *p = strstr(data, "\"contrast\"");
    if (p && (p = strchr(p, ':'))) { do { p++; } while (*p == ' ' || *p == '\t'); if (!strncmp(p, "\"strong\"", 8)) result.strong = true; }
    result.reduced_motion = wc_json_true(data, "reduced_motion");
    *settings = result; return true;
}
#endif
