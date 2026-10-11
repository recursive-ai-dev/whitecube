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
};

struct cb_accessibility {
    double ui_scale;
    bool strong_contrast;
    bool reduced_motion;
};

void cb_config_defaults(struct cb_config *config);
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

#endif
