/* Exercise the compositor's configuration and bindings without a display. */
#undef NDEBUG
#define main whitecube_main
#include "whitecube.c"
#undef main
#include <wlr/interfaces/wlr_keyboard.h>

static void test_applied_keyboard(void) {
	struct wlr_keyboard keyboard;
	static const struct wlr_keyboard_impl impl = {.name = "settings-test"};
	wlr_keyboard_init(&keyboard, &impl, "Settings keyboard");
	config_defaults(); g_repeat_rate = 0; g_repeat_delay = 1700;
	wc_apply_keymap(&keyboard);
	assert(keyboard.keymap && keyboard.repeat_info.rate == 0 && keyboard.repeat_info.delay == 1700);
	snprintf(g_xkb_layout, sizeof(g_xkb_layout), "fr");
	g_repeat_rate = 45; g_repeat_delay = 1800; wc_apply_keymap(&keyboard);
	assert(keyboard.repeat_info.rate == 45 && keyboard.repeat_info.delay == 1800);
	struct xkb_state *state = xkb_state_new(keyboard.keymap); assert(state);
	assert(xkb_state_key_get_one_sym(state, xkb_keymap_key_by_name(keyboard.keymap, "AD01")) == XKB_KEY_a);
	xkb_state_unref(state); wlr_keyboard_finish(&keyboard); config_defaults();
}

static void load_fixture(const char *text) {
	char path[] = "/tmp/whitecube-config-XXXXXX";
	int fd = mkstemp(path);
	assert(fd >= 0);
	FILE *f = fdopen(fd, "w");
	assert(f && fputs(text, f) >= 0);
	assert(fclose(f) == 0);
	load_config_file(path);
	assert(unlink(path) == 0);
}

static void test_reload_defaults(void) {
	config_defaults();
	load_fixture("terminal=custom-terminal\nlauncher=custom-launcher\n"
		"chat_app_id=custom-chat\nchat_cmd=custom-chat-command\n"
		"scripts_app_id=custom-scripts\nscripts_cmd=custom-scripts-command\n"
		"browser_cmd=custom-browser\nhelp_cmd=custom-help\nsettings_cmd=custom-settings\n"
		"xkb_layout=fr\nxkb_model=pc105\nxkb_variant=oss\nxkb_options=compose:ralt\n"
		"tab_w=220\ntab_h=44\ngrid_step=80\nrepeat_rate=30\nrepeat_delay=700\n");
	assert(!strcmp(g_terminal, "custom-terminal"));
	assert(!strcmp(g_launcher, "custom-launcher"));
	assert(!strcmp(g_xkb_layout, "fr") && g_repeat_rate == 30);
	assert(g_tab_w == 220 && g_tab_h == 44 && g_grid == 80);
	/* Reload after command keys were removed: only the new setting survives. */
	config_defaults();
	load_fixture("tab_w=200\n");
	assert(!strcmp(g_terminal, "/usr/bin/whitecube-terminal"));
	assert(!strcmp(g_launcher, "fuzzel"));
	assert(!strcmp(g_chat_app_id, "cube-chat"));
	assert(!strcmp(g_chat_cmd, "foot -a cube-chat -T cube-chat /usr/bin/cube-chat"));
	assert(!strcmp(g_scripts_app_id, "cube-scripts"));
	assert(!strcmp(g_scripts_cmd, "/usr/bin/whitecube-terminal -a cube-scripts -T Scripts /usr/bin/cube-scripts"));
	assert(!strcmp(g_browser_cmd, "/usr/bin/whitecube-browser"));
	assert(!strcmp(g_help_cmd, "/usr/bin/whitecube-terminal -T keys -e less /usr/share/whitecube/keys.txt"));
	assert(!strcmp(g_settings_cmd, "cube-settings"));
	assert(!g_xkb_model[0] && !g_xkb_layout[0] && !g_xkb_variant[0] && !g_xkb_options[0]);
	assert(g_repeat_rate == 25 && g_repeat_delay == 600);
	assert(g_tab_w == 200 && g_tab_h == 40 && g_grid == 64);
	/* System settings form the base; user settings override only named keys. */
	config_defaults();
	load_fixture("terminal=system-terminal\nlauncher=system-launcher\n");
	load_fixture("launcher=user-launcher\n");
	assert(!strcmp(g_terminal, "system-terminal") && !strcmp(g_launcher, "user-launcher"));
}

static void test_numeric_settings(void) {
	config_defaults();
	load_fixture("tab_w=250\ntab_h=48\ngrid_step=128\nrepeat_rate=40\nrepeat_delay=800\n");
	load_fixture("tab_w=broken\ntab_h=44junk\ngrid_step=999999999999999999999999999\n"
		"repeat_rate=\nrepeat_delay=700junk\n");
	assert(g_tab_w == 250 && g_tab_h == 48 && g_grid == 128);
	assert(g_repeat_rate == 40 && g_repeat_delay == 800);
	load_fixture("tab_w=2147483647\ntab_h=2147483647\ngrid_step=2147483647\n"
		"repeat_rate=2147483647\nrepeat_delay=2147483647\n");
	assert(g_tab_w == 400 && g_tab_h == 80 && g_grid == 256);
	assert(g_repeat_rate == 100 && g_repeat_delay == 2000);
	load_fixture("tab_w=-1\ntab_h=-1\ngrid_step=-1\nrepeat_rate=-1\nrepeat_delay=-1\n");
	assert(g_tab_w == 60 && g_tab_h == 20 && g_grid == 16);
	assert(g_repeat_rate == 0 && g_repeat_delay == 100);
	load_fixture("tab_w= 240 \nrepeat_rate= 35\n");
	assert(g_tab_w == 240 && g_repeat_rate == 35);
}

static void test_caps_lock_binding(void) {
	struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	assert(context);
	struct xkb_rule_names names = {.layout = "us"};
	struct xkb_keymap *keymap = xkb_keymap_new_from_names(context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
	assert(keymap);
	struct xkb_state *state = xkb_state_new(keymap);
	assert(state);
	xkb_keycode_t q = xkb_keymap_key_by_name(keymap, "AD01");
	xkb_mod_index_t caps = xkb_keymap_mod_get_index(keymap, XKB_MOD_NAME_CAPS);
	assert(q != XKB_KEYCODE_INVALID && caps != XKB_MOD_INVALID);
	xkb_state_update_mask(state, 0, 0, (xkb_mod_mask_t)1 << caps, 0, 0, 0);
	xkb_keysym_t sym = xkb_state_key_get_one_sym(state, q);
	assert(sym == XKB_KEY_Q);
	struct wc_server server = {0};
	wl_list_init(&server.toplevels);
	/* No focused window: Alt+Q is handled but sends no close request. */
	assert(handle_keybinding(&server, sym, WLR_MODIFIER_ALT | WLR_MODIFIER_CAPS));
	assert(handle_keybinding(&server, XKB_KEY_q, WLR_MODIFIER_ALT));
	assert(handle_keybinding(&server, XKB_KEY_ISO_Left_Tab, WLR_MODIFIER_ALT | WLR_MODIFIER_SHIFT));
	assert(!handle_keybinding(&server, XKB_KEY_F5, WLR_MODIFIER_ALT));
	xkb_state_unref(state);
	xkb_keymap_unref(keymap);
	xkb_context_unref(context);
}

static void test_pointer_placement(void) {
	struct wc_server server = {0};
	struct wc_toplevel window = {.server = &server};
	server.drag = (struct wc_drag){.toplevel = &window, .work_w = 1000, .work_h = 800,
		.pointer_x = 200, .pointer_y = 100, .x = 100, .y = 150, .w = 600, .h = 400};
	wc_update_drag(&server, 300, 180);
	assert(window.layout_x == 200 && window.layout_y == 250);
	assert(window.layout_w == 600 && window.layout_h == 400);
	wc_update_drag(&server, 5000, 5000);
	assert(window.layout_x == 400 && window.layout_y == 600);
	wc_update_drag(&server, -5000, -5000);
	assert(window.layout_x == 0 && window.layout_y == 0);
	server.drag.resize = true;
	wc_update_drag(&server, 300, 180);
	assert(window.layout_x == 100 && window.layout_y == 150);
	assert(window.layout_w == 700 && window.layout_h == 500);
	wc_update_drag(&server, 5000, 5000);
	assert(window.layout_w == 900 && window.layout_h == 850);
	wc_update_drag(&server, -5000, -5000);
	assert(window.layout_w == 100 && window.layout_h == 100);
	server.drag.x = 990; server.drag.y = 990; server.drag.w = 10; server.drag.h = 10;
	wc_update_drag(&server, -5000, -5000);
	assert(window.layout_w == 10 && window.layout_h == 10);
	/* Closing a grabbed client cancels the operation before freeing it. */
	server.drag.button = BTN_LEFT;
	forget_toplevel(&window);
	assert(!server.drag.toplevel && server.drag.button == BTN_LEFT);
	window.layout_w = 123;
	wc_update_drag(&server, 5000, 5000);
	assert(window.layout_w == 123);
}

static void test_privacy_settings(void) {
	config_defaults();
	assert(!g_hide_titles && g_remember_layout && g_save_titles && g_clipboard && g_clear_clipboard_on_lock && !g_lock_minutes);
	load_fixture("privacy_hide_titles=1\nprivacy_remember_layout=false\nprivacy_save_titles=0\n"
		"privacy_clipboard=0\nprivacy_clear_clipboard_on_lock=false\nprivacy_lock_minutes=15\n");
	assert(g_hide_titles && !g_remember_layout && !g_save_titles && !g_clipboard && !g_clear_clipboard_on_lock && g_lock_minutes == 15);
	load_fixture("privacy_hide_titles=broken\nprivacy_remember_layout=2\nprivacy_lock_minutes=garbage\n");
	assert(g_hide_titles && !g_remember_layout && g_lock_minutes == 15);
	struct wlr_xdg_toplevel xdg = {.title = "private document", .app_id = "editor"};
	struct wc_toplevel window = {.xdg_toplevel = &xdg};
	assert(!strcmp(tab_title(&window), "editor"));
	xdg.app_id = NULL;
	assert(!strcmp(tab_title(&window), "window"));
	config_defaults();
	assert(!strcmp(tab_title(&window), "private document"));
	char temporary[] = "/tmp/whitecube-privacy-XXXXXX";
	char *directory = mkdtemp(temporary); assert(directory);
	struct wc_server server = {0};
	wl_list_init(&server.toplevels);
	wc_state_defaults(&server.layout);
	snprintf(server.state_directory, sizeof(server.state_directory), "%s", directory);
	server.layout.count = 1;
	snprintf(server.layout.windows[0].app, 128, "old-app");
	snprintf(server.layout.windows[0].title, 256, "old private document");
	window.mapped = true; window.saved_index = -1; xdg.app_id = "editor";
	wl_list_insert(&server.toplevels, &window.link);
	g_save_titles = false; wc_save_layout(&server);
	char path[600]; snprintf(path, sizeof(path), "%s/native-layout.txt", directory);
	struct wc_desktop_state saved;
	assert(wc_state_load(path, &saved) && saved.count == 2);
	assert(!saved.windows[0].title[0] && !saved.windows[1].title[0]);
	assert(wc_state_forget(directory));
	g_remember_layout = false; wc_save_layout(&server);
	assert(access(path, F_OK) < 0);
	assert(!rmdir(directory));
	config_defaults();
}

static void init_test_lock(struct wlr_session_lock_v1 *lock) {
	memset(lock, 0, sizeof(*lock));
	/* Protocol ack is verified by the isolated headless integration check. */
	lock->locked_sent = true;
	wl_signal_init(&lock->events.new_surface);
	wl_signal_init(&lock->events.unlock);
	wl_signal_init(&lock->events.destroy);
}
static void test_lock_isolation(void) {
	struct wc_server server = {0};
	server.wl_display = wl_display_create(); assert(server.wl_display);
	server.seat = wlr_seat_create(server.wl_display, "test-seat"); assert(server.seat);
	server.scene = wlr_scene_create(); assert(server.scene);
	server.background = wlr_scene_tree_create(&server.scene->tree);
	server.bottom = wlr_scene_tree_create(&server.scene->tree);
	server.windows = wlr_scene_tree_create(&server.scene->tree);
	server.top = wlr_scene_tree_create(&server.scene->tree);
	server.overlay = wlr_scene_tree_create(&server.scene->tree);
	server.lock_tree = wlr_scene_tree_create(&server.scene->tree);
	server.cursor = wlr_cursor_create();
	server.cursor_mgr = wlr_xcursor_manager_create(NULL, 24);
	wl_list_init(&server.outputs); wl_list_init(&server.keyboards);
	wl_list_init(&server.toplevels); wl_list_init(&server.lock_surfaces);
	struct wlr_session_lock_v1 first, replacement;
	init_test_lock(&first); init_test_lock(&replacement);
	server_new_lock(&server.new_lock, &first);
	assert(server.locked && server.lock == &first && server.lock_tree->node.enabled);
	assert(!server.background->node.enabled && !server.windows->node.enabled);
	assert(!server.top->node.enabled && !server.overlay->node.enabled && !server.bottom->node.enabled);
	/* Every compositor shortcut is blocked, including quit, launcher and lock. */
	assert(!handle_keybinding(&server, XKB_KEY_Escape, WLR_MODIFIER_ALT | WLR_MODIFIER_SHIFT));
	assert(!handle_keybinding(&server, XKB_KEY_Return, WLR_MODIFIER_ALT));
	assert(!handle_keybinding(&server, XKB_KEY_l, WLR_MODIFIER_ALT));
	assert(!handle_keybinding(&server, XKB_KEY_F2, WLR_MODIFIER_ALT));
	assert(!wc_lock_surface_allowed(&server, NULL));
	/* Stale ordinary-client focus cannot receive keys, modifiers or scrolling. */
	struct wlr_surface app = {0};
	server.seat->keyboard_state.focused_surface = &app;
	server.seat->pointer_state.focused_surface = &app;
	struct wc_keyboard fake_keyboard = {.server = &server};
	struct wlr_keyboard_key_event key = {.keycode = KEY_A, .state = WL_KEYBOARD_KEY_STATE_PRESSED};
	kb_key(&fake_keyboard.key, &key);
	kb_modifiers(&fake_keyboard.modifiers, NULL);
	cursor_axis(&server.cursor_axis, NULL);
	cursor_frame(&server.cursor_frame, NULL);
	server.seat->keyboard_state.focused_surface = NULL;
	server.seat->pointer_state.focused_surface = NULL;
	wl_signal_emit_mutable(&first.events.destroy, NULL);
	assert(server.locked && !server.lock && server.lock_tree->node.enabled);
	assert(!server.windows->node.enabled && !server.overlay->node.enabled);
	server_new_lock(&server.new_lock, &replacement);
	assert(server.locked && server.lock == &replacement);
	wl_signal_emit_mutable(&replacement.events.unlock, NULL);
	assert(!server.locked && !server.lock_tree->node.enabled);
	assert(server.background->node.enabled && server.windows->node.enabled && server.overlay->node.enabled);
	wl_signal_emit_mutable(&replacement.events.destroy, NULL);
	assert(!server.locked && !server.lock);
	wlr_cursor_destroy(server.cursor); wlr_xcursor_manager_destroy(server.cursor_mgr);
	wlr_scene_node_destroy(&server.scene->tree.node);
	wl_display_destroy(server.wl_display);
}

static void test_idle_lock(void) {
	config_defaults();
	struct wc_server server = {.last_activity = {100, 900000000}};
	assert(!wc_idle_lock_due(&server, (struct timespec){1000, 0}));
	g_lock_minutes = 1;
	assert(!wc_idle_lock_due(&server, (struct timespec){160, 800000000}));
	assert(wc_idle_lock_due(&server, (struct timespec){160, 900000000}));
	server.locked = true;
	assert(!wc_idle_lock_due(&server, (struct timespec){1000, 0}));
	server.locked = false; server.last_lock_attempt = (struct timespec){160, 900000000};
	assert(!wc_idle_lock_due(&server, (struct timespec){190, 800000000}));
	assert(wc_idle_lock_due(&server, (struct timespec){190, 900000000}));
	server.last_activity = (struct timespec){200, 0};
	assert(!wc_idle_lock_due(&server, (struct timespec){210, 0}));
	config_defaults();
}

static void test_lock_output_geometry(void) {
	struct wc_server server = {0};
	server.wl_display = wl_display_create(); assert(server.wl_display);
	server.output_layout = wlr_output_layout_create(server.wl_display);
	server.scene = wlr_scene_create(); assert(server.scene);
	server.lock_tree = wlr_scene_tree_create(&server.scene->tree);
	wl_list_init(&server.outputs); wl_list_init(&server.lock_surfaces);
	struct wlr_output first = {.width = 1920, .height = 1080, .scale = 1, .enabled = true};
	struct wlr_output second = {.width = 2560, .height = 1440, .scale = 2, .enabled = true};
	wlr_addon_set_init(&first.addons); wlr_addon_set_init(&second.addons);
	struct wc_output a = {.server = &server, .wlr_output = &first};
	struct wc_output b = {.server = &server, .wlr_output = &second};
	a.lock_bg = wlr_scene_rect_create(server.lock_tree, 1, 1, (float[4]){0,0,0,1});
	b.lock_bg = wlr_scene_rect_create(server.lock_tree, 1, 1, (float[4]){0,0,0,1});
	wl_list_insert(&server.outputs, &a.link);
	wc_lock_refresh(&server);
	assert(a.lock_bg->width == 1920 && a.lock_bg->height == 1080);
	/* An additional output receives an independent full-size opaque shield. */
	wl_list_insert(&server.outputs, &b.link);
	wc_lock_refresh(&server);
	assert(b.lock_bg->width == 1280 && b.lock_bg->height == 720 && b.lock_bg->color[3] == 1);
	first.width = 2560; first.height = 1440; first.scale = 1.25;
	second.transform = WL_OUTPUT_TRANSFORM_90;
	wc_lock_refresh(&server);
	assert(a.lock_bg->width == 2048 && a.lock_bg->height == 1152);
	assert(b.lock_bg->width == 720 && b.lock_bg->height == 1280);
	second.enabled = false; wc_lock_refresh(&server);
	assert(!b.lock_bg->node.enabled && a.lock_bg->node.enabled);
	wl_list_remove(&b.link); wlr_scene_node_destroy(&b.lock_bg->node);
	wc_lock_refresh(&server);
	assert(a.lock_bg->width == 2048 && a.lock_bg->node.enabled);
	wlr_scene_node_destroy(&server.scene->tree.node);
	wl_display_destroy(server.wl_display);
}

static void test_tab_overflow(void) {
	/* Every workspace uses a single count button regardless of available width. */
	struct wc_bar_layout bar = wc_bar_layout(1920, 1.0);
	int cap = wc_tab_capacity(104, 8, 180, bar.tabs_end);
	assert(cap > 4);
	assert(wc_tab_visible(0, cap) == 0 && wc_tab_visible(1, cap) == 0);
	assert(wc_tab_visible(5, cap) == 0 && wc_tab_visible(30, cap) == 0);
	assert(bar.chat_x + bar.chat_width + 4 == 1920 - 160 - bar.status_width);
	assert(wc_tab_capacity(104, 8, 180, 100) == 0 && wc_tab_visible(2, 0) == 0);
	assert(wc_tab_capacity(104, 8, 0, 900) == 0);
	/* Panel: one row per tab, clamped on screen, scrolls when taller than the screen. */
	struct wc_panel_geom g = wc_panel_geom(1920, 1080, 180, 40, 700, 6);
	assert(g.x == 700 && g.y == 40 && g.rows == 6 && g.h == 6 * 34 + 12 && g.w == 396);
	g = wc_panel_geom(1920, 1080, 180, 40, 1800, 6);
	assert(g.x + g.w == 1920);
	g = wc_panel_geom(1920, 1080, 180, 40, -50, 6);
	assert(g.x == 0);
	g = wc_panel_geom(1920, 300, 180, 40, 500, 50);
	assert(g.rows == (300 - 40 - 12) / 34 && wc_panel_max_scroll(&g, 50) == 50 - g.rows);
	assert(wc_panel_clamp_scroll(&g, 50, 999) == 50 - g.rows && wc_panel_clamp_scroll(&g, 50, -3) == 0);
	assert(wc_panel_clamp_scroll(&g, 3, 5) == 0);
	g = wc_panel_geom(1920, 1080, 180, 40, 500, 4);
	assert(wc_panel_row_at(&g, 4, 0, 510, 40 + 6 + 1) == 0);
	assert(wc_panel_row_at(&g, 4, 0, 510, 40 + 6 + 34 * 3 + 5) == 3);
	assert(wc_panel_row_at(&g, 4, 0, 510, 40 + 2) == -1);          /* top padding */
	assert(wc_panel_row_at(&g, 4, 0, 499, 60) == -1);              /* left of the panel */
	assert(wc_panel_row_at(&g, 4, 0, 510, 40 + g.h + 1) == -1);    /* below it */
	g = wc_panel_geom(1920, 300, 180, 40, 500, 50);
	assert(wc_panel_row_at(&g, 50, 10, 510, 40 + 6 + 1) == 10);
	assert(wc_panel_on_close(&g, 24, g.x + g.w - 20) && !wc_panel_on_close(&g, 24, g.x + 40));
	assert(wc_panel_reveal(&g, 50, 0, 49) == 50 - g.rows);
	assert(wc_panel_reveal(&g, 50, 40, 0) == 0);
	g = wc_panel_geom(1920, 1080, 180, 40, 104, 0);
	assert(g.rows == 1 && g.h > 0 && wc_panel_row_at(&g, 0, 0, 120, 48) == -1);
}

static void test_reopened_window_is_visible(void) {
	config_defaults();
	struct wc_server server = {.workspace = 2, .desktop_visible = true};
	struct wlr_xdg_toplevel xdg = {.app_id = "editor", .title = "Project"};
	struct wc_toplevel window = {.server = &server, .xdg_toplevel = &xdg, .workspace = 1, .hidden = true};
	server.layout.count = 1;
	server.layout.windows[0] = (struct wc_saved_window){.workspace = 0, .hidden = true,
		.maximized = true, .x = 60, .y = 40, .w = 800, .h = 700};
	snprintf(server.layout.windows[0].app, 128, "editor");
	snprintf(server.layout.windows[0].title, 256, "Project");
	wc_restore_window(&window);
	assert(window.workspace == 2 && !window.hidden && window.saved_index == 0);
	assert(window.maximized && window.layout_x == 60 && window.layout_w == 800);
	assert(server.layout.windows[0].claimed);
	/* Another instance cannot consume an already claimed placement. */
	struct wc_toplevel second = {.server = &server, .xdg_toplevel = &xdg};
	wc_restore_window(&second);
	assert(second.workspace == 2 && !second.hidden && second.saved_index == -1);
	/* The same visibility rule applies with persistence disabled. */
	g_remember_layout = false;
	server.workspace = 3;
	wc_restore_window(&second);
	assert(second.workspace == 3 && !second.hidden && second.saved_index == -1);
	config_defaults();
}

int main(void) {
	test_applied_keyboard();
	test_reload_defaults();
	test_numeric_settings();
	test_caps_lock_binding();
	test_pointer_placement();
	test_privacy_settings();
	test_reopened_window_is_visible();
	test_lock_isolation();
	test_idle_lock();
	test_lock_output_geometry();
	test_tab_overflow();
	puts("compositor: configuration, shortcuts, placement, privacy persistence and fail-closed lock isolation passed");
	return 0;
}
