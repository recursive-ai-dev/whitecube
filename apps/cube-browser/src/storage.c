#define _POSIX_C_SOURCE 200809L
#include "storage.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <sqlite3.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define CB_PATH_MAX 4096
#define CB_HISTORY_DAYS (90 * 86400)

static int errorf(char *error, size_t size, const char *message)
{
    if (error && size) snprintf(error, size, "%s", message);
    return -1;
}

static int xdg_path(char *path, size_t size, const char *variable,
                    const char *fallback, const char *suffix)
{
    const char *base = getenv(variable);
    int length;
    if (base && base[0] == '/') {
        length = snprintf(path, size, "%s/%s", base, suffix);
    } else {
        const char *home = getenv("HOME");
        if (!home || home[0] != '/') return -1;
        length = snprintf(path, size, "%s/%s/%s", home, fallback, suffix);
    }
    return length < 0 || (size_t)length >= size ? -1 : 0;
}

static int private_dir(char *path)
{
    /* Ancestors may be conventional user symlinks; the profile itself may not. */
    for (char *p = path + 1; *p; p++) {
        if (*p != '/') continue;
        *p = '\0';
        int result = mkdir(path, 0700);
        *p = '/';
        if (result < 0 && errno != EEXIST) return -1;
    }
    if (mkdir(path, 0700) < 0 && errno != EEXIST) return -1;
    struct stat st;
    if (lstat(path, &st) || !S_ISDIR(st.st_mode) || st.st_uid != getuid()) return -1;
    return chmod(path, 0700);
}

static int profile_path(char *path, size_t size, bool config, const char *file)
{
    if (xdg_path(path, size, config ? "XDG_CONFIG_HOME" : "XDG_DATA_HOME",
                 config ? ".config" : ".local/share", "cube-browser")) return -1;
    if (private_dir(path)) return -1;
    size_t used = strlen(path);
    int count = snprintf(path + used, size - used, "/%s", file);
    return count < 0 || (size_t)count >= size - used ? -1 : 0;
}

/* Read-only profile access: 1 absent, 0 found, -1 invalid; never mkdir/chmod. */
static int profile_read_path(char *path, size_t size, bool config, const char *file)
{
    if (xdg_path(path, size, config ? "XDG_CONFIG_HOME" : "XDG_DATA_HOME",
                 config ? ".config" : ".local/share", "cube-browser")) return -1;
    struct stat st;
    if (lstat(path, &st)) return errno == ENOENT ? 1 : -1;
    if (!S_ISDIR(st.st_mode) || st.st_uid != getuid()) return -1;
    size_t used = strlen(path);
    int count = snprintf(path + used, size - used, "/%s", file);
    return count < 0 || (size_t)count >= size - used ? -1 : 0;
}

static int read_private(const char *path, char *buffer, size_t size)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return errno == ENOENT ? 1 : -1;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() ||
        st.st_size < 0 || (uintmax_t)st.st_size >= size) {
        close(fd);
        return -1;
    }
    size_t used = 0;
    for (;;) {
        ssize_t count = read(fd, buffer + used, size - 1 - used);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) { close(fd); return -1; }
        if (!count) break;
        used += (size_t)count;
        if (used == size - 1) { close(fd); return -1; }
    }
    close(fd);
    if (memchr(buffer, '\0', used)) return -1;
    buffer[used] = '\0';
    return 0;
}

void cb_config_defaults(struct cb_config *config)
{
    memset(config, 0, sizeof(*config));
    snprintf(config->homepage, sizeof(config->homepage), "cube:home");
    snprintf(config->search_engine, sizeof(config->search_engine),
             "https://duckduckgo.com/html/?q=%%s");
    config->zoom = 1.0;config->javascript=true;
}

static bool clean_text(const char *text, size_t capacity)
{
    size_t length = strnlen(text, capacity);
    if (!length || length == capacity) return false;
    for (size_t i = 0; i < length; i++)
        if ((unsigned char)text[i] < 32 || (unsigned char)text[i] == 127) return false;
    return true;
}

static bool valid_config(const struct cb_config *config)
{
    if (!clean_text(config->homepage, sizeof(config->homepage)) ||
        !clean_text(config->search_engine, sizeof(config->search_engine)) ||
        !isfinite(config->zoom) || config->zoom < .5 || config->zoom > 3.0)
        return false;
    if (strncmp(config->search_engine, "https://", 8) &&
        strncmp(config->search_engine, "http://", 7)) return false;
    const char *slot = strstr(config->search_engine, "%s");
    if (!slot || strstr(slot + 2, "%s")) return false;
    /* The caller substitutes the placeholder, never treats this as printf. */
    return true;
}

static char *trim(char *text)
{
    while (isspace((unsigned char)*text)) text++;
    size_t length = strlen(text);
    while (length && isspace((unsigned char)text[length - 1])) text[--length] = '\0';
    return text;
}

int cb_config_load(struct cb_config *config, char *error, size_t error_size)
{
    cb_config_defaults(config);
    if (error && error_size) error[0] = '\0';
    char path[CB_PATH_MAX], data[16384];
    int path_result = profile_read_path(path, sizeof(path), true, "config");
    if (path_result == 1) return 0;
    if (path_result)
        return errorf(error, error_size, "Cannot access the private config directory");
    int result = read_private(path, data, sizeof(data));
    if (result == 1) return 0;
    if (result) return errorf(error, error_size, "Cannot read a regular, bounded config file");
    struct cb_config parsed = *config;
    char *saveptr = NULL;
    for (char *line = strtok_r(data, "\n", &saveptr); line;
         line = strtok_r(NULL, "\n", &saveptr)) {
        line = trim(line);
        if (!*line || *line == '#') continue;
        char *equals = strchr(line, '=');
        if (!equals) return errorf(error, error_size, "Config expects key=value lines");
        *equals = '\0';
        char *key = trim(line), *value = trim(equals + 1);
        if (!strcmp(key, "homepage")) {
            if (strlen(value) >= sizeof(parsed.homepage)) return errorf(error, error_size, "Homepage is too long");
            strcpy(parsed.homepage, value);
        } else if (!strcmp(key, "search_engine")) {
            if (strlen(value) >= sizeof(parsed.search_engine)) return errorf(error, error_size, "Search engine is too long");
            strcpy(parsed.search_engine, value);
        } else if (!strcmp(key,"javascript")) {
            if(strcmp(value,"true")&&strcmp(value,"false"))return errorf(error,error_size,"JavaScript expects true or false");
            parsed.javascript=!strcmp(value,"true");
        } else if (!strcmp(key, "zoom")) {
            char *end;
            parsed.zoom = strtod(value, &end);
            if (end == value || *end) return errorf(error, error_size, "Invalid zoom setting");
        }
    }
    if (!valid_config(&parsed)) return errorf(error, error_size, "Invalid homepage, search engine, or zoom setting");
    *config = parsed;
    return 0;
}

int cb_config_save(const struct cb_config *config, char *error, size_t error_size)
{
    if (error && error_size) error[0] = '\0';
    if (!valid_config(config)) return errorf(error, error_size, "Invalid config values");
    char path[CB_PATH_MAX], temporary[CB_PATH_MAX];
    if (profile_path(path, sizeof(path), true, "config"))
        return errorf(error, error_size, "Cannot access the private config directory");
    int length = snprintf(temporary, sizeof(temporary), "%s.XXXXXX", path);
    if (length < 0 || (size_t)length >= sizeof(temporary)) return errorf(error, error_size, "Config path is too long");
    int fd = mkstemp(temporary);
    if (fd < 0) return errorf(error, error_size, "Cannot create config file");
    int result = fchmod(fd, 0600);
    if (result == 0 && dprintf(fd, "# cube-browser\nhomepage=%s\nsearch_engine=%s\nzoom=%.3f\njavascript=%s\n",
                               config->homepage, config->search_engine, config->zoom,config->javascript?"true":"false") < 0) result = -1;
    if (result == 0 && fsync(fd)) result = -1;
    if (close(fd)) result = -1;
    if (result == 0 && rename(temporary, path)) result = -1;
    if (result) {
        unlink(temporary);
        return errorf(error, error_size, "Cannot write config file");
    }
    return 0;
}

/* A small bounded JSON reader: only top-level accessibility values are used. */
static void json_space(const char **p)
{
    while (**p == ' ' || **p == '\t' || **p == '\r' || **p == '\n') (*p)++;
}

static int json_string(const char **p, char *output, size_t capacity)
{
    if (*(*p)++ != '"') return -1;
    size_t used = 0;
    while (**p && **p != '"') {
        unsigned char c = (unsigned char)*(*p)++;
        if (c < 32) return -1;
        if (c == '\\') {
            c = (unsigned char)*(*p)++;
            if (!c || !strchr("\"\\/bfnrtu", c)) return -1;
            if (c == 'u') {
                for (int i = 0; i < 4; i++) {
                    if (!isxdigit((unsigned char)**p)) return -1;
                    (*p)++;
                }
                c = '?'; /* Escaped keys are unknown; unknown strings are skipped. */
            }
        }
        if (output && used + 1 < capacity) output[used++] = (char)c;
    }
    if (**p != '"') return -1;
    (*p)++;
    if (output && capacity) output[used] = '\0';
    return 0;
}

static int json_skip(const char **p, unsigned depth)
{
    if (depth > 16) return -1;
    json_space(p);
    if (**p == '"') return json_string(p, NULL, 0);
    if (**p == '{' || **p == '[') {
        char begin = *(*p)++, end = begin == '{' ? '}' : ']';
        json_space(p);
        if (**p == end) { (*p)++; return 0; }
        for (;;) {
            if (begin == '{') {
                if (json_string(p, NULL, 0)) return -1;
                json_space(p);
                if (*(*p)++ != ':') return -1;
            }
            if (json_skip(p, depth + 1)) return -1;
            json_space(p);
            if (**p == end) { (*p)++; return 0; }
            if (*(*p)++ != ',') return -1;
            json_space(p);
        }
    }
    if (!strncmp(*p, "true", 4)) { *p += 4; return 0; }
    if (!strncmp(*p, "false", 5)) { *p += 5; return 0; }
    if (!strncmp(*p, "null", 4)) { *p += 4; return 0; }
    const char *start = *p;
    if (**p == '-') (*p)++;
    if (**p == '0') (*p)++;
    else {
        if (**p < '1' || **p > '9') return -1;
        while (isdigit((unsigned char)**p)) (*p)++;
    }
    if (**p == '.') {
        (*p)++;
        if (!isdigit((unsigned char)**p)) return -1;
        while (isdigit((unsigned char)**p)) (*p)++;
    }
    if (**p == 'e' || **p == 'E') {
        (*p)++;
        if (**p == '+' || **p == '-') (*p)++;
        if (!isdigit((unsigned char)**p)) return -1;
        while (isdigit((unsigned char)**p)) (*p)++;
    }
    return *p == start ? -1 : 0;
}

int cb_accessibility_load(struct cb_accessibility *settings)
{
    *settings = (struct cb_accessibility){.ui_scale = 1.0};
    char path[CB_PATH_MAX], data[16384];
    if (xdg_path(path, sizeof(path), "XDG_STATE_HOME", ".local/state", "whitecube/accessibility.json")) return -1;
    int result = read_private(path, data, sizeof(data));
    if (result == 1) return 0;
    if (result) return -1;
    struct cb_accessibility parsed = *settings;
    const char *p = data;
    json_space(&p);
    if (*p++ != '{') return -1;
    json_space(&p);
    if (*p != '}') {
        for (;;) {
            char key[64];
            if (json_string(&p, key, sizeof(key))) return -1;
            json_space(&p);
            if (*p++ != ':') return -1;
            json_space(&p);
            const char *value = p;
            if (json_skip(&p, 0)) return -1;
            if (!strcmp(key, "ui_scale")) {
                char *end;
                double scale = strtod(value, &end);
                if (end != p || !isfinite(scale) || scale < .5 || scale > 3.0) return -1;
                parsed.ui_scale = scale;
            } else if (!strcmp(key, "contrast")) {
                const char *text = value;
                char contrast[32];
                if (*text != '"' || json_string(&text, contrast, sizeof(contrast)) || text != p)
                    return -1;
                if (!strcmp(contrast, "strong")) parsed.strong_contrast = true;
                else if (!strcmp(contrast, "normal")) parsed.strong_contrast = false;
                else return -1;
            } else if (!strcmp(key, "strong_contrast") || !strcmp(key, "reduced_motion")) {
                bool flag;
                if (p - value == 4 && !strncmp(value, "true", 4)) flag = true;
                else if (p - value == 5 && !strncmp(value, "false", 5)) flag = false;
                else return -1;
                if (!strcmp(key, "strong_contrast")) parsed.strong_contrast = flag;
                else parsed.reduced_motion = flag;
            }
            json_space(&p);
            if (*p == '}') break;
            if (*p++ != ',') return -1;
            json_space(&p);
        }
    }
    p++;
    json_space(&p);
    if (*p) return -1;
    *settings = parsed;
    return 0;
}

sqlite3 *cb_storage_open(const char *name)
{
    char path[CB_PATH_MAX];
    if (profile_path(path, sizeof(path), false, name)) return NULL;
    int fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0600);
    if (fd < 0) return NULL;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() || fchmod(fd, 0600)) {
        close(fd);
        return NULL;
    }
    close(fd);
    sqlite3 *db = NULL;
    if (sqlite3_open_v2(path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_NOMUTEX, NULL) != SQLITE_OK) {
        sqlite3_close(db);
        return NULL;
    }
    sqlite3_busy_timeout(db, 1000);
    sqlite3_limit(db, SQLITE_LIMIT_LENGTH, 1024 * 1024);
    sqlite3_exec(db,"PRAGMA trusted_schema=OFF; PRAGMA secure_delete=ON;",NULL,NULL,NULL);
    return db;
}
static sqlite3 *history_open(void)
{
    sqlite3 *db=cb_storage_open("history.sqlite3");if(!db)return NULL;
    const char *schema = "PRAGMA trusted_schema=OFF; PRAGMA secure_delete=ON;"
        "CREATE TABLE IF NOT EXISTS history (url TEXT PRIMARY KEY, title TEXT NOT NULL,"
        "visited_at INTEGER NOT NULL, visits INTEGER NOT NULL DEFAULT 1);"
        "CREATE INDEX IF NOT EXISTS history_time ON history(visited_at DESC);";
    if (sqlite3_exec(db, schema, NULL, NULL, NULL) != SQLITE_OK) {
        sqlite3_close(db);
        return NULL;
    }
    sqlite3_stmt *prune = NULL;
    if (sqlite3_prepare_v2(db, "DELETE FROM history WHERE visited_at < ?", -1, &prune, NULL) != SQLITE_OK) {
        sqlite3_close(db);
        return NULL;
    }
    sqlite3_bind_int64(prune, 1, (sqlite3_int64)time(NULL) - CB_HISTORY_DAYS);
    int result = sqlite3_step(prune);
    sqlite3_finalize(prune);
    if (result != SQLITE_DONE) { sqlite3_close(db); return NULL; }
    return db;
}

static bool history_url(const char *url)
{
    return url && clean_text(url, 4096) &&
        (!strncmp(url, "https://", 8) || !strncmp(url, "http://", 7) || !strncmp(url, "file://", 7));
}

int cb_history_visit(const char *url, const char *title)
{
    if (!history_url(url)) return -1;
    if (!title || !*title) title = url;
    if (strnlen(title, 4096) >= 4096) return -1;
    sqlite3 *db = history_open();
    if (!db) return -1;
    sqlite3_stmt *statement = NULL;
    const char *query = "INSERT INTO history(url,title,visited_at) VALUES(?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title,visited_at=excluded.visited_at,visits=visits+1";
    int result = -1;
    if (sqlite3_prepare_v2(db, query, -1, &statement, NULL) == SQLITE_OK) {
        sqlite3_bind_text(statement, 1, url, -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(statement, 2, title, -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(statement, 3, (sqlite3_int64)time(NULL));
        if (sqlite3_step(statement) == SQLITE_DONE) result = 0;
    }
    sqlite3_finalize(statement);
    if (!result && sqlite3_exec(db, "DELETE FROM history WHERE url IN (SELECT url FROM history "
                               "ORDER BY visited_at DESC LIMIT -1 OFFSET 10000)", NULL, NULL, NULL) != SQLITE_OK) result = -1;
    sqlite3_close(db);
    return result;
}

struct html_buffer { char *data; size_t used, capacity; };

static int append(struct html_buffer *buffer, const char *text)
{
    size_t size = strlen(text);
    if (size > 4 * 1024 * 1024 - buffer->used) return -1;
    size_t required = buffer->used + size + 1;
    if (required > buffer->capacity) {
        size_t capacity = required * 2;
        char *next = realloc(buffer->data, capacity);
        if (!next) return -1;
        buffer->data = next;
        buffer->capacity = capacity;
    }
    memcpy(buffer->data + buffer->used, text, size + 1);
    buffer->used += size;
    return 0;
}

static int escape(struct html_buffer *buffer, const char *text)
{
    for (; *text; text++) {
        const char *replacement = NULL;
        switch (*text) {
            case '&': replacement = "&amp;"; break;
            case '<': replacement = "&lt;"; break;
            case '>': replacement = "&gt;"; break;
            case '"': replacement = "&quot;"; break;
            case '\'': replacement = "&#39;"; break;
        }
        char single[2] = {*text, '\0'};
        if (append(buffer, replacement ? replacement : single)) return -1;
    }
    return 0;
}

char *cb_history_html(void)
{
    sqlite3 *db = history_open();
    if (!db) return NULL;
    struct html_buffer buffer = {0};
    sqlite3_stmt *statement = NULL;
    int result = append(&buffer, "<!doctype html><html><head><title>History — cube-browser</title></head>"
        "<body><h1>History</h1><p>Recent visits, retained for 90 days. Showing up to 500 pages.</p>"
        "<p><a href=\"cube:history-clear\">Clear browsing history</a> · <a href=\"cube:home\">Home</a> · "
        "<a href=\"cube:bookmarks\">Bookmarks</a></p>");
    if (result || sqlite3_prepare_v2(db, "SELECT url,title,visited_at FROM history "
                                   "ORDER BY visited_at DESC,url ASC LIMIT 500", -1, &statement, NULL) != SQLITE_OK) goto failure;
    size_t count = 0;
    while ((result = sqlite3_step(statement)) == SQLITE_ROW) {
        const char *url = (const char *)sqlite3_column_text(statement, 0);
        const char *title = (const char *)sqlite3_column_text(statement, 1);
        if (!history_url(url) || !title || strnlen(title, 4096) >= 4096) continue;
        time_t stamp = (time_t)sqlite3_column_int64(statement, 2);
        struct tm tm;
        char date[64] = "";
        if (localtime_r(&stamp, &tm)) strftime(date, sizeof(date), "%Y-%m-%d %H:%M", &tm);
        if (append(&buffer, "<p><a href=\"") || escape(&buffer, url) ||
            append(&buffer, "\">") || escape(&buffer, title) ||
            append(&buffer, "</a><br>") || escape(&buffer, url) ||
            append(&buffer, "<br>") || append(&buffer, date) || append(&buffer, "</p>")) goto failure;
        count++;
    }
    if (result != SQLITE_DONE) goto failure;
    if ((!count && append(&buffer, "<p>No browsing history yet.</p>")) ||
        append(&buffer, "</body></html>")) goto failure;
    sqlite3_finalize(statement);
    sqlite3_close(db);
    return buffer.data;
failure:
    sqlite3_finalize(statement);
    sqlite3_close(db);
    free(buffer.data);
    return NULL;
}

int cb_history_clear(void)
{
    sqlite3 *db = history_open();
    if (!db) return -1;
    int result = sqlite3_exec(db, "DELETE FROM history; VACUUM;", NULL, NULL, NULL);
    sqlite3_close(db);
    return result == SQLITE_OK ? 0 : -1;
}

#define CB_BOOKMARK_LIMIT 1000

/* Unlike profile_path(), this path check never creates or chmods directories.
 * Reading saved pages in a private window must leave the profile untouched. */
static sqlite3 *bookmarks_open(bool writable, bool *missing)
{
    *missing = false;
    sqlite3 *db = NULL;
    if (writable) {
        db = cb_storage_open("bookmarks.sqlite3");
        if (!db) return NULL;
        if (sqlite3_exec(db, "CREATE TABLE IF NOT EXISTS bookmarks("
                "url TEXT PRIMARY KEY,title TEXT NOT NULL,added_at INTEGER NOT NULL)",
                NULL, NULL, NULL) != SQLITE_OK) {
            sqlite3_close(db);
            return NULL;
        }
        return db;
    }
    char path[CB_PATH_MAX];
    int path_result = profile_read_path(path, sizeof(path), false, "bookmarks.sqlite3");
    if (path_result) { *missing = path_result == 1; return NULL; }
    struct stat st;
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) { *missing = errno == ENOENT; return NULL; }
    bool valid = !fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_uid == getuid();
    close(fd);
    if (!valid) return NULL;
    if (sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX, NULL) != SQLITE_OK) {
        sqlite3_close(db);
        return NULL;
    }
    sqlite3_busy_timeout(db, 1000);
    sqlite3_limit(db, SQLITE_LIMIT_LENGTH, 1024 * 1024);
    sqlite3_exec(db, "PRAGMA trusted_schema=OFF", NULL, NULL, NULL);
    return db;
}

/* Strict UTF-8 validation without a locale or an extra storage dependency. */
static bool bookmark_text(const char *text)
{
    if (!text || !clean_text(text, 4096)) return false;
    const unsigned char *p = (const unsigned char *)text;
    while (*p) {
        uint32_t code = *p++;
        if (code < 0x80) continue;
        unsigned count;
        uint32_t minimum;
        if (code >= 0xc2 && code <= 0xdf) { count = 1; minimum = 0x80; code &= 0x1f; }
        else if (code >= 0xe0 && code <= 0xef) { count = 2; minimum = 0x800; code &= 0x0f; }
        else if (code >= 0xf0 && code <= 0xf4) { count = 3; minimum = 0x10000; code &= 0x07; }
        else return false;
        for (unsigned i = 0; i < count; i++) {
            if ((*p & 0xc0) != 0x80) return false;
            code = (code << 6) | (*p++ & 0x3f);
        }
        if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return false;
    }
    return true;
}

static bool bookmark_url(const char *url)
{
    if (!bookmark_text(url) || !history_url(url) || strchr(url, '\\')) return false;
    if (!strncmp(url, "file:///", 8)) return true;
    if (!strncmp(url, "file:", 5)) return false;
    const char *authority = strstr(url, "://") + 3;
    size_t size = strcspn(authority, "/?#");
    if (!size) return false;
    for (size_t i = 0; i < size; i++)
        if (authority[i] == '@' || authority[i] == '%' || (unsigned char)authority[i] <= 32) return false;
    return true;
}

int cb_bookmark_add(const char *url, const char *title, bool private_mode)
{
    if (private_mode || !bookmark_url(url)) return -1;
    if (!title || !*title) title = url;
    if (!bookmark_text(title)) return -1;
    bool missing;
    sqlite3 *db = bookmarks_open(true, &missing);
    if (!db) return -1;
    sqlite3_stmt *statement = NULL;
    int result = -1;
    /* A single SQLite statement makes the capacity check and upsert atomic. */
    const char *query = "INSERT INTO bookmarks(url,title,added_at) SELECT ?,?,? "
        "WHERE (SELECT count(*) FROM bookmarks) < 1000 OR EXISTS(SELECT 1 FROM bookmarks WHERE url=?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title,added_at=excluded.added_at";
    if (sqlite3_prepare_v2(db, query, -1, &statement, NULL) == SQLITE_OK &&
        sqlite3_bind_text(statement, 1, url, -1, SQLITE_TRANSIENT) == SQLITE_OK &&
        sqlite3_bind_text(statement, 2, title, -1, SQLITE_TRANSIENT) == SQLITE_OK &&
        sqlite3_bind_int64(statement, 3, (sqlite3_int64)time(NULL)) == SQLITE_OK &&
        sqlite3_bind_text(statement, 4, url, -1, SQLITE_TRANSIENT) == SQLITE_OK &&
        sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db) == 1) result = 0;
    sqlite3_finalize(statement);
    sqlite3_close(db);
    return result;
}

int cb_bookmark_remove(const char *url, bool private_mode)
{
    if (private_mode || !bookmark_url(url)) return -1;
    bool missing;
    /* Avoid creating an empty database when the user removes an absent URL. */
    sqlite3 *db = bookmarks_open(false, &missing);
    if (!db) return missing ? 0 : -1;
    sqlite3_close(db);
    db = bookmarks_open(true, &missing);
    if (!db) return -1;
    sqlite3_stmt *statement = NULL;
    int result = -1;
    if (sqlite3_prepare_v2(db, "DELETE FROM bookmarks WHERE url=?", -1, &statement, NULL) == SQLITE_OK &&
        sqlite3_bind_text(statement, 1, url, -1, SQLITE_TRANSIENT) == SQLITE_OK &&
        sqlite3_step(statement) == SQLITE_DONE) result = 0;
    sqlite3_finalize(statement);
    sqlite3_close(db);
    return result;
}

int cb_bookmark_contains(const char *url)
{
    if (!bookmark_url(url)) return -1;
    bool missing;
    sqlite3 *db = bookmarks_open(false, &missing);
    if (!db) return missing ? 0 : -1;
    sqlite3_stmt *statement = NULL;
    int result = -1;
    if (sqlite3_prepare_v2(db, "SELECT 1 FROM bookmarks WHERE url=?", -1, &statement, NULL) == SQLITE_OK &&
        sqlite3_bind_text(statement, 1, url, -1, SQLITE_TRANSIENT) == SQLITE_OK) {
        int step = sqlite3_step(statement);
        if (step == SQLITE_ROW) result = 1;
        else if (step == SQLITE_DONE) result = 0;
    }
    sqlite3_finalize(statement);
    sqlite3_close(db);
    return result;
}

int cb_bookmark_list(struct cb_bookmark **items, size_t *count)
{
    if (!items || !count) return -1;
    *items = NULL; *count = 0;
    bool missing;
    sqlite3 *db = bookmarks_open(false, &missing);
    if (!db) return missing ? 0 : -1;
    sqlite3_stmt *statement = NULL;
    struct cb_bookmark *list = NULL;
    size_t used = 0, capacity = 0;
    if (sqlite3_prepare_v2(db, "SELECT url,title FROM bookmarks ORDER BY added_at DESC,url ASC LIMIT 1000",
                          -1, &statement, NULL) != SQLITE_OK) goto failure;
    int step;
    while ((step = sqlite3_step(statement)) == SQLITE_ROW) {
        const char *url = (const char *)sqlite3_column_text(statement, 0);
        const char *title = (const char *)sqlite3_column_text(statement, 1);
        if (!bookmark_url(url) || !bookmark_text(title)) continue;
        if (used == capacity) {
            capacity = capacity ? capacity * 2 : 16;
            if (capacity > CB_BOOKMARK_LIMIT) capacity = CB_BOOKMARK_LIMIT;
            struct cb_bookmark *next = realloc(list, capacity * sizeof(*list));
            if (!next) goto failure;
            list = next;
        }
        strcpy(list[used].url, url);
        strcpy(list[used].title, title);
        used++;
    }
    if (step != SQLITE_DONE) goto failure;
    sqlite3_finalize(statement);
    sqlite3_close(db);
    *items = list; *count = used;
    return 0;
failure:
    free(list);
    sqlite3_finalize(statement);
    sqlite3_close(db);
    return -1;
}

char *cb_bookmarks_html(void)
{
    struct cb_bookmark *items = NULL;
    size_t count = 0;
    if (cb_bookmark_list(&items, &count)) return NULL;
    struct html_buffer buffer = {0};
    if (append(&buffer, "<!doctype html><html><head><title>Bookmarks — Cube Browser</title>"
            "<style>body{font-family:sans-serif;max-width:900px;margin:40px auto;padding:24px;}"
            "h1{font-size:36px;}article{padding:16px 0;border-bottom:1px solid #ddd;}"
            "a{color:#111;}p{color:#555;}</style></head><body>"
            "<p>CUBE / LIBRARY</p><h1>Bookmarks</h1>"
            "<p>Your saved pages. Press Ctrl+D on a page to add or remove it. "
            "Private windows can read your bookmarks.</p>"
            "<p><a href=\"cube:home\">Home</a> · <a href=\"cube:history\">History</a></p>")) goto failure;
    for (size_t i = 0; i < count; i++) {
        /* Leave room for the largest fully escaped row and the document footer. */
        if (buffer.used > 2 * 1024 * 1024 - 80000) {
            if (append(&buffer, "<p>More bookmarks are saved. Remove older pages to show additional entries.</p>")) goto failure;
            break;
        }
        if (append(&buffer, "<article><a href=\"") || escape(&buffer, items[i].url) ||
            append(&buffer, "\">") || escape(&buffer, items[i].title) ||
            append(&buffer, "</a><p>") || escape(&buffer, items[i].url) ||
            append(&buffer, "</p></article>")) goto failure;
    }
    if ((!count && append(&buffer, "<p>No bookmarks yet. Open a page and press Ctrl+D to save it.</p>")) ||
        append(&buffer, "</body></html>")) goto failure;
    free(items);
    return buffer.data;
failure:
    free(items);
    free(buffer.data);
    return NULL;
}
