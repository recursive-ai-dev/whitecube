#define _POSIX_C_SOURCE 200809L
#include "storage.h"

#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <sqlite3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static char workspace[4096];

static void path_for(char *path, size_t size, const char *suffix)
{
    int n = snprintf(path, size, "%s/%s", workspace, suffix);
    assert(n > 0 && (size_t)n < size);
}

static void write_file(const char *suffix, const char *data)
{
    char path[8192];
    path_for(path, sizeof(path), suffix);
    FILE *file = fopen(path, "w");
    assert(file);
    assert(fputs(data, file) >= 0);
    assert(fclose(file) == 0);
}

static void permissions(const char *suffix, mode_t expected)
{
    char path[8192];
    struct stat st;
    path_for(path, sizeof(path), suffix);
    assert(stat(path, &st) == 0);
    assert((st.st_mode & 0777) == expected);
}

static void config_tests(void)
{
    char error[256];
    struct cb_config config;
    assert(cb_config_load(&config, error, sizeof(error)) == 0);
    assert(strcmp(config.homepage, "cube:start") == 0);
    assert(config.zoom == 1.0);
    strcpy(config.homepage, "https://example.com/?a=1&b=2");
    strcpy(config.search_engine, "https://example.com/search?q=%s&from=cube%20browser");
    config.zoom = 1.25;
    assert(cb_config_save(&config, error, sizeof(error)) == 0);
    permissions("config/cube-browser", 0700);
    permissions("config/cube-browser/config", 0600);
    memset(&config, 0, sizeof(config));
    assert(cb_config_load(&config, error, sizeof(error)) == 0);
    assert(strcmp(config.homepage, "https://example.com/?a=1&b=2") == 0);
    assert(config.zoom == 1.25);

    strcpy(config.homepage, "cube:start\nzoom=3");
    assert(cb_config_save(&config, error, sizeof(error)) == -1);
    write_file("config/cube-browser/config", "homepage=https://example.org/\nzoom=nan\n");
    assert(cb_config_load(&config, error, sizeof(error)) == -1);
    assert(strcmp(config.homepage, "cube:start") == 0);
    assert(config.zoom == 1.0);
    write_file("config/cube-browser/config", " # comment\n zoom = 1.5\n future_setting = yes\n");
    assert(cb_config_load(&config, error, sizeof(error)) == 0);
    assert(config.zoom == 1.5);

    char path[8192];
    path_for(path, sizeof(path), "config/cube-browser/config");
    assert(unlink(path) == 0);
    assert(symlink("/dev/null", path) == 0);
    assert(cb_config_load(&config, error, sizeof(error)) == -1);
    assert(unlink(path) == 0);
    assert(cb_config_save(&config, error, sizeof(error)) == 0);
}

static void accessibility_tests(void)
{
    struct cb_accessibility settings;
    assert(cb_accessibility_load(&settings) == 0);
    assert(settings.ui_scale == 1.0 && !settings.strong_contrast && !settings.reduced_motion);
    char path[8192];
    path_for(path, sizeof(path), "state");
    assert(mkdir(path, 0700) == 0);
    path_for(path, sizeof(path), "state/whitecube");
    assert(mkdir(path, 0700) == 0);
    write_file("state/whitecube/accessibility.json",
        "{\"ui_scale\":1.5,\"strong_contrast\":true,\"reduced_motion\":true,"
        "\"unknown\":{\"ui_scale\":3,\"nested\":[1,\"text\",null]}}");
    assert(cb_accessibility_load(&settings) == 0);
    assert(settings.ui_scale == 1.5 && settings.strong_contrast && settings.reduced_motion);
    write_file("state/whitecube/accessibility.json",
        "{\"ui_scale\":1.60,\"terminal_scale\":1.00,\"contrast\":\"strong\",\"reduced_motion\":true}");
    assert(cb_accessibility_load(&settings) == 0);
    assert(settings.ui_scale == 1.6 && settings.strong_contrast && settings.reduced_motion);
    write_file("state/whitecube/accessibility.json", "{\"contrast\":\"normal\"}");
    assert(cb_accessibility_load(&settings) == 0 && !settings.strong_contrast);
    write_file("state/whitecube/accessibility.json", "{\"ui_scale\":9}");
    assert(cb_accessibility_load(&settings) == -1);
    assert(settings.ui_scale == 1.0);
    write_file("state/whitecube/accessibility.json", "{\"ui_scale\":1.5,}");
    assert(cb_accessibility_load(&settings) == -1);
    write_file("state/whitecube/accessibility.json", "{\"strong_contrast\":\"true\"}");
    assert(cb_accessibility_load(&settings) == -1);
    write_file("state/whitecube/accessibility.json", "{\"ui_scale\":1.2");
    assert(cb_accessibility_load(&settings) == -1);
    write_file("state/whitecube/accessibility.json", "{}");
    assert(cb_accessibility_load(&settings) == 0);
    const char *malformed[] = {"", "{", "{\"", "{\"x\"", "{\"x\":", "{\"x\":\"\\", "{\"x\":\"\\u1", "{\"x\":[", "{\"x\":{", "{\"x\":-}", "{\"x\":01}", "{\"x\":1e}", "{}tail"};
    for (size_t i = 0; i < sizeof(malformed) / sizeof(malformed[0]); i++) {
        write_file("state/whitecube/accessibility.json", malformed[i]);
        assert(cb_accessibility_load(&settings) == -1);
    }
}

static void history_tests(void)
{
    char *html = cb_history_html();
    assert(html && strstr(html, "No browsing history yet."));
    free(html);
    assert(cb_history_visit("javascript:alert(1)", "Blocked") == -1);
    assert(cb_history_visit("cube:history-clear", "Internal") == -1);
    assert(cb_history_visit("https://example.com/\"?x=1&y=2", "<script>bad & \"quoted\"</script>") == 0);
    assert(cb_history_visit("https://example.org/", "First title") == 0);
    assert(cb_history_visit("https://example.org/", "Second title") == 0);
    permissions("data/cube-browser", 0700);
    permissions("data/cube-browser/history.sqlite3", 0600);
    html = cb_history_html();
    assert(html && !strstr(html, "<script>") && !strstr(html, "First title"));
    assert(strstr(html, "&lt;script&gt;bad &amp; &quot;quoted&quot;&lt;/script&gt;"));
    assert(strstr(html, "href=\"https://example.com/&quot;?x=1&amp;y=2\""));
    assert(strstr(html, "Second title"));
    free(html);

    char path[8192];
    path_for(path, sizeof(path), "data/cube-browser/history.sqlite3");
    sqlite3 *db = NULL;
    assert(sqlite3_open(path, &db) == SQLITE_OK);
    assert(sqlite3_exec(db, "UPDATE history SET visited_at=0 WHERE url='https://example.org/'", NULL, NULL, NULL) == SQLITE_OK);
    sqlite3_stmt *statement = NULL;
    assert(sqlite3_prepare_v2(db, "SELECT visits FROM history WHERE url='https://example.org/'", -1, &statement, NULL) == SQLITE_OK);
    assert(sqlite3_step(statement) == SQLITE_ROW);
    assert(sqlite3_column_int(statement, 0) == 2);
    sqlite3_finalize(statement);
    assert(sqlite3_close(db) == SQLITE_OK);
    html = cb_history_html();
    assert(html && !strstr(html, "Second title"));
    free(html);
    assert(cb_history_clear() == 0);
    html = cb_history_html();
    assert(html && strstr(html, "No browsing history yet.") && !strstr(html, "example.com"));
    free(html);

    assert(unlink(path) == 0);
    assert(symlink("/dev/null", path) == 0);
    assert(cb_history_visit("https://example.com/", "test") == -1);
    assert(unlink(path) == 0);
}

int main(void)
{
    strcpy(workspace, "/tmp/cube-browser-storage-XXXXXX");
    assert(mkdtemp(workspace));
    char path[8192];
    path_for(path, sizeof(path), "config");
    assert(setenv("XDG_CONFIG_HOME", path, 1) == 0);
    path_for(path, sizeof(path), "data");
    assert(setenv("XDG_DATA_HOME", path, 1) == 0);
    path_for(path, sizeof(path), "state");
    assert(setenv("XDG_STATE_HOME", path, 1) == 0);
    config_tests();
    accessibility_tests();
    history_tests();

    const char *files[] = {"config/cube-browser/config", "state/whitecube/accessibility.json"};
    for (size_t i = 0; i < sizeof(files) / sizeof(files[0]); i++) {
        path_for(path, sizeof(path), files[i]);
        assert(unlink(path) == 0);
    }
    const char *dirs[] = {"config/cube-browser", "config", "data/cube-browser", "data", "state/whitecube", "state"};
    for (size_t i = 0; i < sizeof(dirs) / sizeof(dirs[0]); i++) {
        path_for(path, sizeof(path), dirs[i]);
        assert(rmdir(path) == 0);
    }
    assert(rmdir(workspace) == 0);
    puts("storage: config, accessibility, SQLite retention/escaping/clear passed");
    return 0;
}
