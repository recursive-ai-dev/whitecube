#ifndef CB_STORAGE_H
#define CB_STORAGE_H

#include <stdbool.h>
#include <stddef.h>

struct sqlite3;
/* Private, owner-checked SQLite file. Caller closes it; NULL on failure. */
struct sqlite3 *cb_storage_open(const char *name);

struct cb_config {
    char homepage[4096];
    char search_engine[1024]; /* Exactly one %s placeholder for an encoded query. */
    double zoom;
    bool javascript;
    bool images;
    bool cookies; /* Third-party cookies remain blocked when enabled. */
    bool site_storage;
    bool cache;
    bool save_history;
    bool clear_on_exit;
    bool https_only;
    char download_directory[4096]; /* Empty selects XDG_DOWNLOAD_DIR or ~/Downloads. */
};

struct cb_accessibility {
    double ui_scale;
    bool strong_contrast;
    bool reduced_motion;
};

void cb_config_defaults(struct cb_config *config);
int cb_config_validate(const struct cb_config *config, char *error, size_t error_size);
/* Missing config is success with defaults. Errors leave defaults in place. */
int cb_config_load(struct cb_config *config, char *error, size_t error_size);
int cb_config_save(const struct cb_config *config, char *error, size_t error_size);
/* Missing or malformed accessibility settings produce safe defaults. */
int cb_accessibility_load(struct cb_accessibility *settings);

/* History is private SQLite, retained for 90 days and bounded to 10,000 URLs. */
int cb_history_visit(const char *url, const char *title);
/* Caller frees the escaped, complete HTML document; NULL means storage error. */
char *cb_history_html(void);
int cb_history_clear(void);

/* Bookmarks are independent of browsing history, bounded to 1,000 URLs.
 * Private windows may read bookmarks but mutations explicitly fail. */
struct cb_bookmark {
    char url[4096];
    char title[4096];
};
int cb_bookmark_add(const char *url, const char *title, bool private_mode);
int cb_bookmark_remove(const char *url, bool private_mode);
/* 1 saved, 0 absent, -1 storage/validation error. Reads never create files. */
int cb_bookmark_contains(const char *url);
/* Caller frees *items; an empty/missing database succeeds with NULL and zero. */
int cb_bookmark_list(struct cb_bookmark **items, size_t *count);
/* Caller frees the escaped complete HTML document; NULL on storage error. */
char *cb_bookmarks_html(void);

#endif
