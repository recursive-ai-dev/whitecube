#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
#include <assert.h>
#include "desktop-state.h"
int main(void) {
    char temporary[] = "/tmp/whitecube-layout-XXXXXX";
    char *directory = mkdtemp(temporary); assert(directory);
    struct wc_desktop_state state; wc_state_defaults(&state);
    state.workspace = 2; state.desktop_visible = true; state.count = 2;
    snprintf(state.names[0], 64, "Editor / coding");
    state.windows[0] = (struct wc_saved_window){.workspace=1,.hidden=true,.fullscreen=true,.x=60,.y=400,.w=880,.h=470};
    snprintf(state.windows[0].app, 128, "cube-scripts"); snprintf(state.windows[0].title, 256, "Game; literal $() title");
    state.windows[1] = (struct wc_saved_window){.workspace=2,.maximized=true};
    snprintf(state.windows[1].app, 128, "editor"); snprintf(state.windows[1].title, 256, "Project / browser");
    assert(wc_state_save(directory, &state));
    char path[512]; snprintf(path, sizeof(path), "%s/native-layout.txt", directory);
    struct stat st; assert(!stat(path, &st) && !(st.st_mode & 0077));
    struct wc_desktop_state loaded; wc_state_defaults(&loaded); assert(wc_state_load(path, &loaded));
    assert(loaded.workspace == 2 && loaded.desktop_visible && loaded.count == 2);
    assert(!strcmp(loaded.names[0], "Editor / coding"));
    assert(loaded.windows[0].hidden && loaded.windows[0].fullscreen && loaded.windows[0].w == 880);
    assert(!strcmp(loaded.windows[0].title, "Game; literal $() title"));
    assert(wc_state_match(&loaded, "cube-scripts", "Game; literal $() title", 0) == 0);
    loaded.windows[0].claimed = true; assert(wc_state_match(&loaded, "cube-scripts", "Game; literal $() title", 0) == -1);
    FILE *f = fopen(path, "w"); assert(f); fprintf(f, "WC_LAYOUT_1 4 0\n"); fclose(f);
    assert(!wc_state_load(path, &loaded) && loaded.workspace == 2);
    f = fopen(path, "w"); assert(f); fprintf(f, "WC_LAYOUT_1 0 0\nW 0 0 0 0 999 0 500 100 61 62\n"); fclose(f);
    assert(!wc_state_load(path, &loaded));
    f = fopen(path, "w"); assert(f); fprintf(f, "WC_LAYOUT_1 0 0\n");
    for (unsigned i = 0; i <= WC_SAVED_WINDOWS; i++) fprintf(f, "W 0 0 0 0 0 0 0 0 61 62\n");
    fclose(f); assert(!wc_state_load(path, &loaded));
    assert(!wc_state_forget(directory)); /* Do not delete unrecognized/invalid files. */
    unlink(path); assert(!symlink("/etc/passwd", path)); assert(!wc_state_load(path, &loaded)); unlink(path);
    assert(wc_state_save(directory, &state));
    assert(wc_state_forget(directory) && access(path, F_OK) < 0);
    assert(wc_state_forget(directory)); /* Idempotent when already absent. */
    assert(!symlink("/etc/passwd", path));
    assert(!wc_state_forget(directory)); assert(!lstat(path, &st) && S_ISLNK(st.st_mode)); unlink(path);
    snprintf(path, sizeof(path), "%s/accessibility.json", directory);
    f = fopen(path, "w"); assert(f); fprintf(f, "{\"ui_scale\":1.4,\"terminal_scale\":1.8,\"contrast\":\"strong\",\"reduced_motion\":true}"); fclose(f);
    struct wc_accessibility settings; assert(wc_accessibility_load(path, &settings));
    assert(settings.ui_scale == 1.4 && settings.terminal_scale == 1.8 && settings.strong && settings.reduced_motion);
    f = fopen(path, "w"); assert(f); fprintf(f, "{\"ui_scale\":99,\"terminal_scale\":0,\"reduced_motion\":false}"); fclose(f);
    assert(wc_accessibility_load(path, &settings) && settings.ui_scale == 1 && settings.terminal_scale == 1 && !settings.reduced_motion);
    unlink(path); snprintf(path, sizeof(path), "%s/native-names.json", directory);
    f = fopen(path, "w"); assert(f); fprintf(f, "{\"w0\":\"436f6465\",\"w1\":\"506c6179\",\"w2\":\"4d6f6e69746f72\",\"w3\":\"42726f777365\"}"); fclose(f);
    assert(wc_names_load(path, &loaded) && !strcmp(loaded.names[0], "Code"));
    unlink(path); rmdir(directory);
    puts("native layouts: private roundtrip, command-free identity matching, geometry bounds, symlink refusal and shared accessibility passed");
    return 0;
}
