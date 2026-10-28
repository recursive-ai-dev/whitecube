/* Test the native application catalog and exact argv without launching apps. */
#undef NDEBUG
#define main status_main
#include "cube-status.c"
#undef main
#include <assert.h>

static void trainer_entry(const char *base, const char *id, const char *executable, const char *visibility) {
    char *path = g_build_filename(base, "applications", id, NULL);
    FILE *file = fopen(path, "w"); assert(file);
    fprintf(file, "[Desktop Entry]\nType=Application\nName=Whitecube Trainer\nExec=%s\nStartupWMClass=org.whitecube.Trainer\n%s\n",
            executable, visibility ? visibility : "");
    fclose(file); g_free(path);
}
static void remove_entry(const char *base, const char *id) {
    char *path = g_build_filename(base, "applications", id, NULL);
    assert(!unlink(path)); g_free(path);
}
static const struct app *catalog_entry(const char *id) {
    for (int i = 0; i < napps; i++) if (!strcmp(apps[i].id, id)) return &apps[i];
    return NULL;
}
static void test_trainer_aliases(void) {
    char temporary[] = "/tmp/whitecube-trainer-aliases-XXXXXX";
    char *root = mkdtemp(temporary); assert(root);
    char *user = g_build_filename(root, "user", NULL), *system = g_build_filename(root, "system", NULL);
    char *user_apps = g_build_filename(user, "applications", NULL), *system_apps = g_build_filename(system, "applications", NULL);
    assert(!g_mkdir_with_parents(user_apps, 0700)); assert(!g_mkdir_with_parents(system_apps, 0700));
    const char *keys[] = {"HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS"};
    char *saved[3]; for (int i = 0; i < 3; i++) saved[i] = g_strdup(getenv(keys[i]));
    assert(!setenv("HOME", root, 1)); assert(!setenv("XDG_DATA_HOME", user, 1)); assert(!setenv("XDG_DATA_DIRS", system, 1));
    trainer_entry(system, "whitecube-trainer.desktop", "/fixture/standalone", NULL);
    trainer_entry(system, "cube-train.desktop", "/fixture/bundled", NULL);
    // Matching names and window identities of any other app are irrelevant.
    trainer_entry(system, "other-trainer.desktop", "/fixture/other", NULL);
    load_apps();
    assert(catalog_entry("cube-train.desktop") && !catalog_entry("whitecube-trainer.desktop"));
    assert(catalog_entry("other-trainer.desktop"));
    assert(!strcmp(catalog_entry("cube-train.desktop")->exec, "/fixture/bundled"));

    trainer_entry(user, "cube-train.desktop", "/fixture/user-bundled", NULL);
    trainer_entry(user, "whitecube-trainer.desktop", "/fixture/user-standalone", NULL);
    load_apps();
    assert(!strcmp(catalog_entry("cube-train.desktop")->exec, "/fixture/user-bundled"));
    assert(!catalog_entry("whitecube-trainer.desktop"));
    for (int hidden = 0; hidden < 2; hidden++) {
        trainer_entry(user, "cube-train.desktop", "/fixture/user-bundled", hidden ? "NoDisplay=true" : "Hidden=true");
        load_apps();
        assert(!catalog_entry("cube-train.desktop"));
        assert(!strcmp(catalog_entry("whitecube-trainer.desktop")->exec, "/fixture/user-standalone"));
        assert(catalog_entry("other-trainer.desktop"));
    }
    remove_entry(user, "cube-train.desktop"); remove_entry(system, "cube-train.desktop");
    load_apps();
    assert(!catalog_entry("cube-train.desktop"));
    assert(!strcmp(catalog_entry("whitecube-trainer.desktop")->exec, "/fixture/user-standalone"));
    for (int hidden = 0; hidden < 2; hidden++) {
        trainer_entry(user, "whitecube-trainer.desktop", "/fixture/user-standalone", hidden ? "NoDisplay=true" : "Hidden=true");
        load_apps();
        assert(!catalog_entry("cube-train.desktop") && !catalog_entry("whitecube-trainer.desktop"));
    }
    remove_entry(user, "whitecube-trainer.desktop");
    load_apps();
    assert(!strcmp(catalog_entry("whitecube-trainer.desktop")->exec, "/fixture/standalone"));
    remove_entry(system, "whitecube-trainer.desktop"); remove_entry(system, "other-trainer.desktop");
    for (int i = 0; i < 3; i++) { if (saved[i]) assert(!setenv(keys[i], saved[i], 1)); else assert(!unsetenv(keys[i])); g_free(saved[i]); }
    assert(!rmdir(user_apps)); assert(!rmdir(system_apps)); assert(!rmdir(user)); assert(!rmdir(system)); assert(!rmdir(root));
    g_free(user_apps); g_free(system_apps); g_free(user); g_free(system);
}

int main(void) {
    assert(search_matches("ÉDITEUR Café Unicode", "éditeur unicode"));
    assert(search_matches("Café", "Cafe\314\201"));
    assert(!search_matches("Cube Hash", "hash pdf"));
    assert(search_matches("Cube PDF Tools", "  pdf   cube "));
    struct app app = {0};
    snprintf(app.name, sizeof(app.name), "Cube; $(literal)");
    snprintf(app.path, sizeof(app.path), "/tmp/entry with spaces.desktop");
    snprintf(app.icon, sizeof(app.icon), "cube-icon");
    snprintf(app.exec, sizeof(app.exec), "program \"literal $(touch nope)\" %%%% %%c %%k %%i %%F");
    char **args = app_argv(&app); assert(args);
    assert(!strcmp(args[0], "program"));
    assert(!strcmp(args[1], "literal $(touch nope)"));
    assert(!strcmp(args[2], "%"));
    assert(!strcmp(args[3], app.name));
    assert(!strcmp(args[4], app.path));
    assert(!strcmp(args[5], "--icon") && !strcmp(args[6], "cube-icon") && !args[7]);
    g_strfreev(args);
    app.terminal = true; args = app_argv(&app); assert(args);
    assert(!strcmp(args[0], TERMINAL) && !strcmp(args[1], "--") && !strcmp(args[2], "program"));
    g_strfreev(args);
    snprintf(app.exec, sizeof(app.exec), "program %%invalid"); assert(!app_argv(&app));
    snprintf(app.exec, sizeof(app.exec), "program \"unclosed"); assert(!app_argv(&app));
    char temporary[] = "/tmp/whitecube-apps-XXXXXX";
    char *directory = mkdtemp(temporary); assert(directory);
    char path[2048]; snprintf(path, sizeof(path), "%s/entry.desktop", directory);
    FILE *file = fopen(path, "w"); assert(file);
    fputs("[Desktop Entry]\nType=Application\nName=Cube\\sTools\nComment=Compute checksums\nExec=program %F\nPath=/tmp\nCategories=Utility;\n", file);
    fclose(file); napps = 0; load_entry(directory, "entry.desktop");
    assert(napps == 1 && !strcmp(apps[0].name, "Cube Tools"));
    assert(!strcmp(apps[0].cwd, "/tmp") && apps[0].cat == CAT_ACCESSORIES);
    assert(search_matches(apps[0].search, "checksums"));
    load_entry(directory, "entry.desktop"); assert(napps == 1);
    file = fopen(path, "w"); assert(file);
    fputs("[Desktop Entry]\nType=Application\nName=Hidden\nExec=program\nHidden=true\n", file); fclose(file);
    napps = 0; load_entry(directory, "entry.desktop"); assert(napps == 1 && !apps[0].name[0]);
    load_entry(directory, "entry.desktop"); assert(napps == 1);
    assert(!in_path(directory));
    unlink(path); rmdir(directory);
    test_trainer_aliases();
    puts("application catalog: Unicode, multiword search, field codes, literal argv, metadata, masks and executable checks passed");
    return 0;
}
