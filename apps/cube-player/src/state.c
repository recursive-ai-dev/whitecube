#define _GNU_SOURCE
#include "state.h"
#include <gio/gio.h>
#include <math.h>
#include <errno.h>
#include <stdio.h>
#include <sys/stat.h>
#include <string.h>
#include <stdlib.h>

static double number(GKeyFile *k, const char *key, double fallback, double lo, double hi) {
    GError *e = NULL;
    double v = g_key_file_get_double(k, "Player", key, &e);
    if (e || !isfinite(v)) v = fallback;
    g_clear_error(&e);
    return CLAMP(v, lo, hi);
}
static bool boolean(GKeyFile *k, const char *key, bool fallback) {
    GError *e = NULL;
    bool v = g_key_file_get_boolean(k, "Player", key, &e);
    if (e) v = fallback;
    g_clear_error(&e);
    return v;
}
void state_init(State *s) {
    memset(s, 0, sizeof(*s));
    s->config_path = g_build_filename(g_get_user_config_dir(), "cube-player", "settings.ini", NULL);
    s->history_path = g_build_filename(g_get_user_state_dir(), "cube-player", "history.ini", NULL);
    s->settings = g_key_file_new(); s->history = g_key_file_new();
    g_key_file_load_from_file(s->settings, s->config_path, G_KEY_FILE_NONE, NULL);
    g_key_file_load_from_file(s->history, s->history_path, G_KEY_FILE_NONE, NULL);
    s->volume = number(s->settings, "volume", 70, 0, 100);
    s->speed = number(s->settings, "speed", 1, .25, 3);
    s->theme = number(s->settings, "theme", 0, 0, 2);
    s->repeat = number(s->settings, "repeat", 0, 0, 2);
    s->resume = boolean(s->settings, "resume", true);
    s->sidebar = boolean(s->settings, "sidebar", true);
    s->shuffle = boolean(s->settings, "shuffle", false);
    s->recent = g_ptr_array_new_with_free_func(g_free);
    gsize count = 0;
    char **recent = g_key_file_get_string_list(s->history, "Recent", "paths", &count, NULL);
    for (gsize i = 0; i < MIN(count, 40); i++) if (*recent[i]) g_ptr_array_add(s->recent, g_strdup(recent[i]));
    g_strfreev(recent);
}
static bool save_keyfile(GKeyFile *k, const char *path) {
    char *dir = g_path_get_dirname(path);
    bool ok = g_mkdir_with_parents(dir, 0700) == 0;
    g_free(dir);
    if (!ok) return false;
    gsize len;
    char *text = g_key_file_to_data(k, &len, NULL);
    GError *e = NULL;
    ok = g_file_set_contents_full(path, text, len,
        G_FILE_SET_CONTENTS_CONSISTENT | G_FILE_SET_CONTENTS_DURABLE, 0600, &e);
    if (e) { g_printerr("Cube Player: %s\n", e->message); g_error_free(e); }
    g_free(text);
    return ok;
}
bool state_save(State *s) {
    g_key_file_set_double(s->settings, "Player", "volume", s->volume);
    g_key_file_set_double(s->settings, "Player", "speed", s->speed);
    g_key_file_set_integer(s->settings, "Player", "theme", s->theme);
    g_key_file_set_integer(s->settings, "Player", "repeat", s->repeat);
    g_key_file_set_boolean(s->settings, "Player", "resume", s->resume);
    g_key_file_set_boolean(s->settings, "Player", "sidebar", s->sidebar);
    g_key_file_set_boolean(s->settings, "Player", "shuffle", s->shuffle);
    g_key_file_set_string_list(s->history, "Recent", "paths", (const char *const *)s->recent->pdata, s->recent->len);
    bool a = save_keyfile(s->settings, s->config_path);
    bool b = save_keyfile(s->history, s->history_path);
    return a && b;
}
void state_free(State *s) {
    g_key_file_unref(s->settings); g_key_file_unref(s->history);
    g_ptr_array_free(s->recent, TRUE);
    g_free(s->config_path); g_free(s->history_path);
}
double state_position(State *s, const char *path) {
    if (!s->resume || !path) return 0;
    char *key = g_compute_checksum_for_string(G_CHECKSUM_SHA256, path, -1);
    double v = g_key_file_get_double(s->history, key, "position", NULL);
    g_free(key);
    return isfinite(v) && v > 0 ? v : 0;
}
void state_remember(State *s, const char *path, double pos, double duration) {
    if (!path || !*path || !isfinite(pos)) return;
    char *key = g_compute_checksum_for_string(G_CHECKSUM_SHA256, path, -1);
    if (!s->resume || (duration > 0 && pos > duration - 5)) pos = 0;
    g_key_file_set_double(s->history, key, "position", MAX(0, pos));
    g_free(key);
    for (guint i = 0; i < s->recent->len; i++) {
        if (!strcmp(g_ptr_array_index(s->recent, i), path)) { g_ptr_array_remove_index(s->recent, i); break; }
    }
    g_ptr_array_insert(s->recent, 0, g_strdup(path));
    while (s->recent->len > 40) {
        const char *old = g_ptr_array_index(s->recent, s->recent->len - 1);
        key = g_compute_checksum_for_string(G_CHECKSUM_SHA256, old, -1);
        g_key_file_remove_group(s->history, key, NULL); g_free(key);
        g_ptr_array_remove_index(s->recent, s->recent->len - 1);
    }
}
void state_forget(State *s) {
    g_key_file_unref(s->history); s->history = g_key_file_new();
    g_ptr_array_set_size(s->recent, 0);
}
char *media_path(const char *input, GError **error) {
    if (!input || !*input || !g_utf8_validate(input, -1, NULL)) {
        g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "Enter a file path or an HTTP(S) media URL."); return NULL;
    }
    char *scheme = g_uri_parse_scheme(input), *result = NULL;
    if (scheme && (!g_ascii_strcasecmp(scheme, "http") || !g_ascii_strcasecmp(scheme, "https"))) {
        GUri *uri = g_uri_parse(input, G_URI_FLAGS_NONE, error);
        if (uri && g_uri_get_host(uri) && *g_uri_get_host(uri)) result = g_strdup(input);
        else if (uri) g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "The media URL needs a host name.");
        if (uri) g_uri_unref(uri);
    } else if (scheme && !g_ascii_strcasecmp(scheme, "file")) {
        char *host = NULL;
        result = g_filename_from_uri(input, &host, error);
        if (host && *host && g_ascii_strcasecmp(host, "localhost")) {
            g_clear_pointer(&result, g_free);
            g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "Only local file URLs are supported.");
        }
        g_free(host);
    } else if (scheme) {
        g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED, "Use a local file or an HTTP(S) media URL.");
    } else {
        char *expanded = g_str_has_prefix(input, "~/") ? g_build_filename(g_get_home_dir(), input + 2, NULL) : g_strdup(input);
        result = g_canonicalize_filename(expanded, NULL); g_free(expanded);
    }
    g_free(scheme);
    if (result && result[0] == '/' && !g_file_test(result, G_FILE_TEST_IS_REGULAR | G_FILE_TEST_IS_DIR)) {
        g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "This file or folder could not be found."); g_clear_pointer(&result, g_free);
    }
    return result;
}
char *media_title(const char *path) {
    if (strstr(path, "://")) {
        GUri *u = g_uri_parse(path, G_URI_FLAGS_PARSE_RELAXED, NULL);
        const char *p = u ? g_uri_get_path(u) : NULL;
        char *name = p && strlen(p) > 1 ? g_path_get_basename(p) : g_strdup(u ? g_uri_get_host(u) : "Media stream");
        char *decoded = g_uri_unescape_string(name, NULL);
        g_free(name); if (u) g_uri_unref(u);
        if (decoded && g_utf8_validate(decoded, -1, NULL)) return decoded;
        g_free(decoded); return g_strdup("Media stream");
    }
    return g_filename_display_basename(path);
}
static bool extension(const char *path, const char *const *list) {
    const char *ext = strrchr(path, '.');
    if (!ext) return false;
    for (int i = 0; list[i]; i++) if (!g_ascii_strcasecmp(ext + 1, list[i])) return true;
    return false;
}
bool media_supported(const char *path) {
    static const char *const exts[] = {"mp4","mkv","webm","mov","avi","m4v","mpeg","mpg","ts","m2ts","ogv","wmv","flv","3gp","vob","mp3","flac","wav","ogg","opus","m4a","aac","aiff","wma","alac","ape","m3u","m3u8","pls","xspf",NULL};
    return extension(path, exts);
}
bool media_subtitle(const char *path) {
    static const char *const exts[] = {"srt","ass","ssa","vtt","sub",NULL};
    return extension(path, exts);
}
bool media_playlist_path(const char *path) {
    static const char *const exts[] = {"m3u", "m3u8", "pls", "xspf", NULL};
    return g_path_is_absolute(path) && extension(path, exts);
}
static char *playlist_item(const char *value, const char *dir) {
    char *scheme = g_uri_parse_scheme(value);
    char *item = scheme || g_path_is_absolute(value) ? g_strdup(value) : g_canonicalize_filename(value, dir);
    g_free(scheme);
    return item;
}
static gint index_cmp(gconstpointer a, gconstpointer b) {
    guint x = *(const guint *)a, y = *(const guint *)b;
    return (x > y) - (x < y);
}
typedef struct {
    GPtrArray *items, *bases;
    GString *location;
    char *track;
    guint tracklist_depth, track_depth, location_depth, count;
} Xspf;
static const char *xml_name(const char *name) {
    const char *colon = strrchr(name, ':');
    return colon ? colon + 1 : name;
}
static void xspf_start(GMarkupParseContext *context, const char *name,
                       const char **attrs, const char **values, gpointer data, GError **error) {
    (void)context;
    Xspf *x = data;
    guint depth = x->bases->len;
    name = xml_name(name);
    if (depth > 64 || x->location_depth || (depth == 1 && strcmp(name, "playlist"))) {
        g_set_error_literal(error, G_MARKUP_ERROR, G_MARKUP_ERROR_INVALID_CONTENT,
                            "Invalid XSPF playlist structure or excessive XML nesting.");
        return;
    }
    const char *parent = g_ptr_array_index(x->bases, depth - 1);
    char *base = g_strdup(parent);
    for (guint i = 0; attrs[i]; i++) if (!strcmp(attrs[i], "xml:base")) {
        g_free(base);
        base = g_uri_resolve_relative(parent, values[i], G_URI_FLAGS_NONE, error);
        if (!base) return;
    }
    g_ptr_array_add(x->bases, base);
    if (depth == 2 && !strcmp(name, "trackList")) x->tracklist_depth = depth;
    else if (depth == 3 && x->tracklist_depth && !strcmp(name, "track")) {
        if (++x->count > 2048) {
            g_set_error_literal(error, G_MARKUP_ERROR, G_MARKUP_ERROR_INVALID_CONTENT,
                                "An XSPF playlist can contain at most 2,048 tracks.");
            return;
        }
        x->track_depth = depth;
    } else if (depth == 4 && x->track_depth && !strcmp(name, "location")) {
        x->location_depth = depth;
        g_string_truncate(x->location, 0);
    }
}
static void xspf_text(GMarkupParseContext *context, const char *text, gsize len,
                      gpointer data, GError **error) {
    (void)context; (void)error;
    Xspf *x = data;
    if (x->location_depth) g_string_append_len(x->location, text, len);
}
static void xspf_end(GMarkupParseContext *context, const char *name, gpointer data, GError **error) {
    (void)context; (void)name;
    Xspf *x = data;
    guint depth = x->bases->len - 1;
    if (x->location_depth == depth) {
        char *value = g_strstrip(x->location->str);
        if (!x->track && *value) {
            const char *base = g_ptr_array_index(x->bases, depth);
            char *resolved = g_uri_resolve_relative(base, value, G_URI_FLAGS_NONE, error);
            if (!resolved) return;
            char *scheme = g_uri_parse_scheme(resolved);
            // XSPF locations can offer alternatives. Retain the first supported one.
            if (scheme && (!g_ascii_strcasecmp(scheme, "file") ||
                           !g_ascii_strcasecmp(scheme, "http") || !g_ascii_strcasecmp(scheme, "https")))
                x->track = resolved;
            else g_free(resolved);
            g_free(scheme);
        }
        x->location_depth = 0;
    } else if (x->track_depth == depth) {
        if (x->track) { g_ptr_array_add(x->items, x->track); x->track = NULL; }
        x->track_depth = 0;
    } else if (x->tracklist_depth == depth) x->tracklist_depth = 0;
    g_ptr_array_remove_index(x->bases, depth);
}
static bool xspf_read(const char *path, const char *text, GPtrArray *items, GError **error) {
    char *base = g_filename_to_uri(path, NULL, error);
    if (!base) return false;
    Xspf x = {.items = items, .bases = g_ptr_array_new_with_free_func(g_free), .location = g_string_new(NULL)};
    g_ptr_array_add(x.bases, base);
    const GMarkupParser parser = {.start_element = xspf_start, .end_element = xspf_end, .text = xspf_text};
    GMarkupParseContext *context = g_markup_parse_context_new(&parser, G_MARKUP_TREAT_CDATA_AS_TEXT, &x, NULL);
    bool ok = g_markup_parse_context_parse(context, text, -1, error) && g_markup_parse_context_end_parse(context, error);
    g_markup_parse_context_free(context);
    g_ptr_array_free(x.bases, TRUE); g_string_free(x.location, TRUE); g_free(x.track);
    return ok;
}
GPtrArray *media_playlist_read(const char *path, bool *stream, GError **error) {
    enum { MAX_PLAYLIST_BYTES = 2 * 1024 * 1024, MAX_PLAYLIST_ITEMS = 2048 };
    *stream = false;
    FILE *file = fopen(path, "rb");
    if (!file) {
        g_set_error(error, G_FILE_ERROR, g_file_error_from_errno(errno), "Could not open playlist: %s", g_strerror(errno));
        return NULL;
    }
    char *data = g_malloc(MAX_PLAYLIST_BYTES + 2);
    gsize size = fread(data, 1, MAX_PLAYLIST_BYTES + 1, file);
    bool ok = !ferror(file);
    if (!ok) g_set_error(error, G_FILE_ERROR, g_file_error_from_errno(errno), "Could not read playlist: %s", g_strerror(errno));
    fclose(file);
    if (!ok) { g_free(data); return NULL; }
    if (size > MAX_PLAYLIST_BYTES || memchr(data, 0, size) || !g_utf8_validate(data, size, NULL)) {
        g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                            "Playlists must be UTF-8 text without NUL bytes, at most 2 MiB.");
        g_free(data); return NULL;
    }
    data[size] = 0;
    char *text = g_str_has_prefix(data, "\xef\xbb\xbf") ? data + 3 : data;
    char *dir = g_path_get_dirname(path);
    GPtrArray *items = g_ptr_array_new_with_free_func(g_free);
    const char *ext = strrchr(path, '.');
    if (ext && !g_ascii_strcasecmp(ext, ".xspf")) ok = xspf_read(path, text, items, error);
    else if (ext && !g_ascii_strcasecmp(ext, ".pls")) {
        GKeyFile *keys = g_key_file_new();
        if (!g_key_file_load_from_data(keys, text, -1, G_KEY_FILE_NONE, error)) ok = false;
        else if (!g_key_file_has_group(keys, "playlist")) {
            g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "The PLS file needs a [playlist] section.");
            ok = false;
        } else {
            gsize count = 0;
            char **names = g_key_file_get_keys(keys, "playlist", &count, NULL);
            GArray *indices = g_array_new(FALSE, FALSE, sizeof(guint));
            for (gsize i = 0; i < count; i++) {
                if (!g_str_has_prefix(names[i], "File") || !g_ascii_isdigit(names[i][4])) continue;
                char *end = NULL;
                guint64 n = g_ascii_strtoull(names[i] + 4, &end, 10);
                if (*end || n == 0 || n > MAX_PLAYLIST_ITEMS) continue;
                guint index = n;
                g_array_append_val(indices, index);
            }
            g_array_sort(indices, index_cmp);
            for (guint i = 0; i < indices->len; i++) {
                guint index = g_array_index(indices, guint, i);
                if (i && index == g_array_index(indices, guint, i - 1)) continue;
                char *key = g_strdup_printf("File%u", index);
                // PLS values are literal paths/URLs, not GKeyFile escape sequences.
                char *value = g_key_file_get_value(keys, "playlist", key, NULL);
                if (value && *g_strstrip(value)) g_ptr_array_add(items, playlist_item(value, dir));
                g_free(value); g_free(key);
            }
            g_array_free(indices, TRUE); g_strfreev(names);
        }
        g_key_file_unref(keys);
    } else {
        char **lines = g_strsplit(text, "\n", -1);
        for (guint i = 0; lines[i]; i++) {
            char *line = g_strstrip(lines[i]);
            if (g_str_has_prefix(line, "#EXT-X-")) { *stream = true; break; }
            if (!*line || *line == '#') continue;
            if (items->len >= MAX_PLAYLIST_ITEMS) {
                g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "A playlist can contain at most 2,048 items.");
                ok = false; break;
            }
            g_ptr_array_add(items, playlist_item(line, dir));
        }
        g_strfreev(lines);
        if (*stream) g_ptr_array_set_size(items, 0);
    }
    g_free(dir); g_free(data);
    if (!ok) { g_ptr_array_free(items, TRUE); return NULL; }
    return items;
}
Media *media_new(const char *path) {
    Media *m = g_new0(Media, 1); m->path = g_strdup(path); m->title = media_title(path); return m;
}
void media_free(gpointer ptr) { Media *m = ptr; g_free(m->path); g_free(m->title); g_free(m); }
void format_time(double seconds, char out[32]) {
    long long n = isfinite(seconds) ? (long long)CLAMP(seconds, 0, 359999999) : 0;
    if (n >= 3600) g_snprintf(out, 32, "%lld:%02lld:%02lld", n / 3600, n / 60 % 60, n % 60);
    else g_snprintf(out, 32, "%02lld:%02lld", n / 60, n % 60);
}
