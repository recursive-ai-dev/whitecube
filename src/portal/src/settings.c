/* SPDX-License-Identifier: MIT
 *
 * settings.c - org.freedesktop.portal.Settings (version 2).
 *
 * Implements Read (v1), ReadOne (v2), ReadAll, and live sync against Whitecube
 * preferences (~/.config/whitecube/config, ~/.local/state/whitecube/accessibility.json).
 */
#include "whitecube_portal.h"

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define NS_APPEARANCE "org.freedesktop.appearance"

typedef enum {
    KEY_COLOR_SCHEME = 0,
    KEY_ACCENT_COLOR,
    KEY_CONTRAST,
    KEY_UNKNOWN
} setting_key;

static setting_key key_lookup(const char *ns, const char *key)
{
    if (!ns || !key || strcmp(ns, NS_APPEARANCE) != 0)
        return KEY_UNKNOWN;
    if (!strcmp(key, "color-scheme"))
        return KEY_COLOR_SCHEME;
    if (!strcmp(key, "accent-color"))
        return KEY_ACCENT_COLOR;
    if (!strcmp(key, "contrast"))
        return KEY_CONTRAST;
    return KEY_UNKNOWN;
}

static bool read_safe_file(const char *path, char *buf, size_t cap, bool allow_root)
{
    int fd;
    struct stat st;
    ssize_t n, total = 0;

    if (!path || !*path)
        return false;
    fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0)
        return false;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) ||
        (st.st_uid != getuid() && !(allow_root && st.st_uid == 0)) ||
        st.st_size < 0 || (size_t)st.st_size >= cap) {
        close(fd);
        return false;
    }
    while (total < (ssize_t)cap - 1) {
        n = read(fd, buf + total, cap - 1 - (size_t)total);
        if (n <= 0)
            break;
        total += n;
    }
    close(fd);
    buf[total] = 0;
    return total > 0;
}

static int hex_val(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static bool parse_hex6(const char *s, unsigned *r, unsigned *g, unsigned *b)
{
    int v[6];
    size_t i;

    if (!s)
        return false;
    if (*s == '#')
        s++;
    for (i = 0; i < 6; i++) {
        v[i] = hex_val(s[i]);
        if (v[i] < 0)
            return false;
    }
    *r = (unsigned)((v[0] << 4) | v[1]);
    *g = (unsigned)((v[2] << 4) | v[3]);
    *b = (unsigned)((v[4] << 4) | v[5]);
    return true;
}

static uint32_t detect_color_scheme(wc_portal *p, uint32_t fallback)
{
    const char *xdg_cfg = getenv("XDG_CONFIG_HOME");
    char path[PATH_MAX];
    char buf[16384];
    char *line, *save = NULL;
    bool loaded = false;

    if (xdg_cfg && wc_is_absolute_path(xdg_cfg) &&
        wc_fmt(path, sizeof path, "%s/whitecube/config", xdg_cfg))
        loaded = read_safe_file(path, buf, sizeof buf, false);
    if (!loaded && p->home[0] &&
        wc_fmt(path, sizeof path, "%s/.config/whitecube/config", p->home))
        loaded = read_safe_file(path, buf, sizeof buf, false);
    if (!loaded)
        loaded = read_safe_file("/etc/whitecube/config", buf, sizeof buf, true);
    if (!loaded)
        return fallback;

    uint32_t result = fallback;
    for (line = strtok_r(buf, "\r\n", &save); line; line = strtok_r(NULL, "\r\n", &save)) {
        char *eq, *k, *v, *end;
        while (*line == ' ' || *line == '\t') line++;
        if (!*line || *line == '#') continue;
        eq = strchr(line, '=');
        if (!eq) continue;
        *eq = 0;
        k = line;
        v = eq + 1;
        end = eq;
        while (end > k && (end[-1] == ' ' || end[-1] == '\t')) *--end = 0;
        while (*v == ' ' || *v == '\t') v++;
        end = v + strlen(v);
        while (end > v && (end[-1] == ' ' || end[-1] == '\t')) *--end = 0;
        if (!strcmp(k, "color_scheme")) {
            if (!strcmp(v, "dark") || !strcmp(v, "1")) return 1;
            if (!strcmp(v, "light") || !strcmp(v, "2")) return 2;
            if (!strcmp(v, "none") || !strcmp(v, "0")) return 0;
        } else if (!strcmp(k, "bg")) {
            unsigned r, g, b;
            if (parse_hex6(v, &r, &g, &b)) {
                double lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
                result = (lum >= 128.0) ? 2u : 1u;
            }
        }
    }
    return result;
}

static uint32_t detect_contrast(wc_portal *p, uint32_t fallback)
{
    const char *xdg_state = getenv("XDG_STATE_HOME");
    char path[PATH_MAX];
    char buf[16384];
    bool loaded = false;
    const char *q;

    if (xdg_state && wc_is_absolute_path(xdg_state) &&
        wc_fmt(path, sizeof path, "%s/whitecube/accessibility.json", xdg_state))
        loaded = read_safe_file(path, buf, sizeof buf, false);
    if (!loaded && p->home[0] &&
        wc_fmt(path, sizeof path, "%s/.local/state/whitecube/accessibility.json", p->home))
        loaded = read_safe_file(path, buf, sizeof buf, false);
    if (!loaded)
        return fallback;

    q = strstr(buf, "\"contrast\"");
    if (!q)
        return fallback;
    q += 10;
    while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
    if (*q != ':')
        return fallback;
    q++;
    while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
    if (!strncmp(q, "\"strong\"", 8))
        return 1;
    if (!strncmp(q, "\"normal\"", 8))
        return 0;
    return fallback;
}

static void write_value(wc_portal *p, wc_writer *w, setting_key k)
{
    switch (k) {
    case KEY_COLOR_SCHEME:
        wc_w_variant(w, "u");
        wc_w_u32(w, p->cfg.color_scheme);
        break;
    case KEY_ACCENT_COLOR:
        wc_w_variant(w, "(ddd)");
        wc_w_struct_begin(w);
        wc_w_double(w, p->cfg.accent[0]);
        wc_w_double(w, p->cfg.accent[1]);
        wc_w_double(w, p->cfg.accent[2]);
        break;
    case KEY_CONTRAST:
        wc_w_variant(w, "u");
        wc_w_u32(w, p->cfg.contrast);
        break;
    default:
        break;
    }
}

static void emit_setting_changed(wc_portal *p, const char *key, setting_key k)
{
    wc_writer w;

    if (p->bus.fd < 0 || p->bus.dead)
        return;
    wc_w_init(&w);
    wc_w_string(&w, NS_APPEARANCE);
    wc_w_string(&w, key);
    write_value(p, &w, k);
    if (wc_w_ok(&w))
        wc_bus_signal(&p->bus, WC_PORTAL_PATH, "org.freedesktop.portal.Settings",
                      "SettingChanged", "ssv", &w, NULL);
    wc_w_free(&w);
}

void wc_settings_sync(wc_portal *p, bool emit_signals)
{
    uint32_t old_cs = p->cfg.color_scheme;
    uint32_t old_ct = p->cfg.contrast;

    if (!p->cfg.color_scheme_set)
        p->cfg.color_scheme = detect_color_scheme(p, 1);
    if (!p->cfg.contrast_set)
        p->cfg.contrast = detect_contrast(p, 0);

    if (emit_signals) {
        if (p->cfg.color_scheme != old_cs)
            emit_setting_changed(p, "color-scheme", KEY_COLOR_SCHEME);
        if (p->cfg.contrast != old_ct)
            emit_setting_changed(p, "contrast", KEY_CONTRAST);
    }
}

/* Settings.Read(in s namespace, in s key) -> (out v value)
 * Note: v1 historical quirk returns a variant wrapping the value variant. */
static void settings_read_impl(wc_portal *p, const wc_msg *m, bool double_wrap)
{
    wc_reader r;
    const char *ns, *key;
    setting_key k;
    wc_writer w;

    wc_settings_sync(p, p->running);
    wc_r_init(&r, m->body, m->body_len, m->big_endian);
    ns = wc_r_string(&r);
    key = wc_r_string(&r);
    if (!wc_r_ok(&r) || !ns || !key) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "expected (ss)");
        return;
    }
    k = key_lookup(ns, key);
    if (k == KEY_UNKNOWN) {
        wc_bus_error(&p->bus, m, WC_ERR_NOT_FOUND, "no such setting: %s %s",
                     ns, key);
        return;
    }
    wc_w_init(&w);
    if (double_wrap)
        wc_w_variant(&w, "v");
    write_value(p, &w, k);
    if (wc_w_ok(&w))
        wc_bus_reply(&p->bus, m, "v", &w);
    else
        wc_bus_error(&p->bus, m, WC_ERR_FAILED, "marshal failure");
    wc_w_free(&w);
}

/* Settings.ReadAll(in as namespaces) -> (out a{sa{sv}} values) */
static void settings_read_all(wc_portal *p, const wc_msg *m)
{
    wc_reader r;
    wc_array_iter ns_it;
    wc_array_mark dict, inner;
    wc_writer w;
    const char *namespaces[WC_VALUE_MAX_ITEMS];
    size_t n_ns = 0, i;
    static const setting_key keys[] = { KEY_COLOR_SCHEME, KEY_ACCENT_COLOR,
                                        KEY_CONTRAST };

    wc_settings_sync(p, p->running);
    wc_r_init(&r, m->body, m->body_len, m->big_endian);
    ns_it = wc_r_array_begin(&r, 4);
    while (wc_r_array_more(&r, ns_it)) {
        const char *s = wc_r_string(&r);
        if (!s)
            break;
        if (n_ns < WC_VALUE_MAX_ITEMS)
            namespaces[n_ns++] = s;
    }
    wc_r_array_end(&r, ns_it);
    if (!wc_r_ok(&r)) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "expected (as)");
        return;
    }

    wc_w_init(&w);
    dict = wc_w_array_begin(&w, 8);          /* a{sa{sv}} */
    if (n_ns == 0) {
        /* Empty list means "everything we know about". */
        namespaces[n_ns++] = NS_APPEARANCE;
    }
    for (i = 0; i < n_ns; i++) {
        size_t j;
        bool known = !strcmp(namespaces[i], NS_APPEARANCE);

        wc_w_struct_begin(&w);
        wc_w_string(&w, namespaces[i]);
        inner = wc_w_array_begin(&w, 8);     /* a{sv} */
        if (known) {
            for (j = 0; j < sizeof keys / sizeof keys[0]; j++) {
                static const char *const names[] = { "color-scheme",
                                                     "accent-color",
                                                     "contrast" };
                wc_w_struct_begin(&w);
                wc_w_string(&w, names[j]);
                write_value(p, &w, keys[j]);
            }
        }
        wc_w_array_end(&w, inner);
    }
    wc_w_array_end(&w, dict);

    if (wc_w_ok(&w))
        wc_bus_reply(&p->bus, m, "a{sa{sv}}", &w);
    else
        wc_bus_error(&p->bus, m, WC_ERR_FAILED, "marshal failure");
    wc_w_free(&w);
}

void wc_settings_handle(wc_portal *p, const wc_msg *m)
{
    const char *member = m->member ? m->member : "";

    if (!strcmp(member, "Read"))
        settings_read_impl(p, m, true);
    else if (!strcmp(member, "ReadOne"))
        settings_read_impl(p, m, false);
    else if (!strcmp(member, "ReadAll"))
        settings_read_all(p, m);
    else
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_METH,
                     "org.freedesktop.portal.Settings has no method '%s' "
                     "(version 2: Read, ReadOne, ReadAll)", member);
}