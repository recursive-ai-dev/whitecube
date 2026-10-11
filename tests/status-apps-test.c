/* Test the native application catalog and exact argv without launching apps. */
#undef NDEBUG
#define main status_main
#include "cube-status.c"
#undef main
#include <assert.h>

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
    puts("application catalog: Unicode, multiword search, field codes, literal argv, metadata, masks and executable checks passed");
    return 0;
}
