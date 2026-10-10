/* Shared, typed dconf overrides. Legacy files remain the inheritance layer. */
#ifndef WC_NATIVE_PREFERENCES_H
#define WC_NATIVE_PREFERENCES_H
#include <gio/gio.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define WC_PREF_SCHEMA "org.whitecube.desktop"
static GSettings *wc_prefs;
static GSettingsSchema *wc_schema;
static char **wc_pref_keys;

static inline gboolean wc_prefs_init(void) {
    if (wc_prefs) return TRUE;
    char exe[4096]; ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe)-1);
    if (n < 0) return FALSE;
    exe[n] = 0;
    char *directory = g_path_get_dirname(exe);
    char *paths[] = {
        g_strdup(g_getenv("GSETTINGS_SCHEMA_DIR")),
        g_build_filename(directory, "schemas", NULL),
        g_build_filename(directory, "..", "share", "whitecube", "schemas", NULL),
        g_build_filename(g_get_user_data_dir(), "whitecube", "schemas", NULL)
    };
    for (unsigned i = 0; i < G_N_ELEMENTS(paths); i++) {
        if (!wc_schema && paths[i]) {
            GSettingsSchemaSource *source = g_settings_schema_source_new_from_directory(paths[i], NULL, FALSE, NULL);
            if (source) {
                wc_schema = g_settings_schema_source_lookup(source, WC_PREF_SCHEMA, FALSE);
                g_settings_schema_source_unref(source);
            }
        }
        g_free(paths[i]);
    }
    g_free(directory);
    if (!wc_schema) {
        GSettingsSchemaSource *source = g_settings_schema_source_get_default();
        if (source) wc_schema = g_settings_schema_source_lookup(source, WC_PREF_SCHEMA, TRUE);
    }
    if (!wc_schema) return FALSE;
    wc_prefs = g_settings_new_full(wc_schema, NULL, NULL);
    wc_pref_keys = g_settings_schema_list_keys(wc_schema);
    return TRUE;
}
static inline void wc_pref_name(const char *config, char *key, size_t cap) {
    g_strlcpy(key, config, cap);
    for (char *p = key; *p; p++) if (*p == '_') *p = '-';
}
static inline gboolean wc_pref_known(const char *config) {
    char key[64]; wc_pref_name(config, key, sizeof(key));
    return wc_prefs_init() && g_settings_schema_has_key(wc_schema, key);
}
static inline char *wc_pref_text(GVariant *value) {
    if (g_variant_is_of_type(value, G_VARIANT_TYPE_STRING)) return g_variant_dup_string(value, NULL);
    if (g_variant_is_of_type(value, G_VARIANT_TYPE_BOOLEAN)) return g_strdup(g_variant_get_boolean(value) ? "1" : "0");
    return g_variant_print(value, FALSE);
}
/* Return only explicit overrides: resetting restores the user's original file. */
static inline char *wc_pref_override(const char *config) {
    char key[64]; wc_pref_name(config, key, sizeof(key));
    if (!wc_pref_known(config)) return NULL;
    GVariant *value = g_settings_get_user_value(wc_prefs, key);
    if (!value) return NULL;
    GSettingsSchemaKey *schema_key = g_settings_schema_get_key(wc_schema, key);
    char *text = g_settings_schema_key_range_check(schema_key, value) ? wc_pref_text(value) : NULL;
    g_settings_schema_key_unref(schema_key); g_variant_unref(value);
    if (text) {
        size_t limit = (!strcmp(key, "terminal") || !strcmp(key, "launcher") || !strcmp(key, "chat-app-id") || !strcmp(key, "scripts-app-id")) ? 127 :
            (!strcmp(key, "xkb-model") || !strcmp(key, "xkb-layout") || !strcmp(key, "xkb-variant")) ? 63 : 255;
        gboolean valid = strlen(text) <= limit && g_utf8_validate(text, -1, NULL);
        for (const unsigned char *p = (const unsigned char *)text; *p; p++) if (*p < 32 || *p == 127) valid = FALSE;
        if (!strcmp(key, "bg") || !strcmp(key, "grid") || !strcmp(key, "tabbar") || !strcmp(key, "border") || !strcmp(key, "unfocused"))
            valid &= strlen(text) == 6 && strspn(text, "0123456789abcdefABCDEF") == 6;
        if (!strcmp(key, "terminal") || !strcmp(key, "launcher") || g_str_has_suffix(key, "-cmd"))
            valid &= strspn(text, " ") != strlen(text);
        if (g_str_has_prefix(key, "xkb-"))
            valid &= strspn(text, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_,:+.-") == strlen(text);
        if (!valid) { g_free(text); text = NULL; }
    }
    return text;
}
typedef void (*wc_pref_visitor)(const char *, const char *, void *);
static inline void wc_prefs_visit(wc_pref_visitor visitor, void *data) {
    if (!wc_prefs_init()) return;
    while (g_main_context_iteration(NULL, FALSE)) {}
    for (char **key = wc_pref_keys; *key; key++) {
        char config[64]; g_strlcpy(config, *key, sizeof(config));
        for (char *p = config; *p; p++) if (*p == '-') *p = '_';
        char *value = wc_pref_override(config);
        if (value) { visitor(config, value, data); g_free(value); }
    }
}
static inline gboolean wc_prefs_changed(void) {
    static char *previous;
    if (!wc_prefs_init()) return FALSE;
    while (g_main_context_iteration(NULL, FALSE)) {}
    GString *state = g_string_new(NULL);
    for (char **key = wc_pref_keys; *key; key++) {
        char *value = wc_pref_override(*key);
        g_string_append_printf(state, "%s=%zu:%s\n", *key, value ? strlen(value) : 0, value ? value : "");
        g_free(value);
    }
    gboolean changed = g_strcmp0(previous, state->str) != 0;
    g_free(previous); previous = g_string_free(state, FALSE);
    return changed;
}
/* Opt-in diagnostics for integration checks; nothing is written in normal use. */
static inline void wc_pref_report(GKeyFile *report, const char *component) {
    const char *directory = g_getenv("WHITECUBE_SETTINGS_REPORT");
    if (directory && *directory) {
        g_key_file_set_integer(report, "runtime", "pid", getpid());
        g_key_file_set_string(report, "runtime", "version", "0.2.0");
        char *path = g_build_filename(directory, component, NULL);
        gsize size; char *data = g_key_file_to_data(report, &size, NULL);
        g_file_set_contents_full(path, data, size, G_FILE_SET_CONTENTS_CONSISTENT, 0600, NULL);
        g_free(path); g_free(data);
    }
    g_key_file_unref(report);
}
#endif
