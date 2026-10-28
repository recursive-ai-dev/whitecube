#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
// whitecube - Wayland-native tabbed compositor, white cubes everywhere.
// No X11, no XWayland. Pure Wayland + wlroots (xwayland disabled build).
// Tabbed windows above a living cube field; a persistent floating scripts panel.
// Background, windows and layer-shell overlays have separate scene trees.
#include <assert.h>
#include <getopt.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <ctype.h>
#include <math.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <wayland-server-core.h>
#include <wlr/backend.h>
#include <wlr/interfaces/wlr_buffer.h>
#include <wlr/render/allocator.h>
#include <wlr/render/wlr_renderer.h>
#include <wlr/types/wlr_cursor.h>
#include <wlr/types/wlr_compositor.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/types/wlr_input_device.h>
#include <wlr/types/wlr_keyboard.h>
#include <wlr/types/wlr_layer_shell_v1.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_output_management_v1.h>
#include <wlr/types/wlr_pointer.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_session_lock_v1.h>
#include <wlr/types/wlr_subcompositor.h>
#include <wlr/types/wlr_xcursor_manager.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/box.h>
#include <wlr/util/log.h>
#include <xkbcommon/xkbcommon.h>
#include <linux/input-event-codes.h>
#include <cairo.h>
#include <pango/pangocairo.h>
#ifndef DRM_FORMAT_ARGB8888
#define DRM_FORMAT_ARGB8888 0x34325241 // 'AR24', cairo's ARGB32 layout on little-endian
#endif
#include "desktop-state.h"
#include <wlr/types/wlr_fractional_scale_v1.h>
#include <wlr/types/wlr_idle_inhibit_v1.h>
#include <wlr/types/wlr_layer_shell_v1.h>
#include <wlr/types/wlr_presentation_time.h>
#include <wlr/types/wlr_primary_selection.h>
#include <wlr/types/wlr_primary_selection_v1.h>
#include <wlr/types/wlr_single_pixel_buffer_v1.h>
#include <wlr/types/wlr_viewporter.h>
#include <wlr/types/wlr_xdg_activation_v1.h>
#include "bar-layout.h"
#include "tab-panel.h"
#include "native-preferences.h"
#include <wlr/backend/libinput.h>
#define TAB_GAP 8
// Tab bar space owned by cube-status (Apps button left, Wi-Fi/clock left of
// the workspace cubes, power button in the far right corner); logical px before
// ui_scale, mirrored in cube-status.c.
#define BAR_APPS_W 104
#define BAR_STATUS_W 300
#define BAR_CHAT_W 140
#define BAR_POWER_W 44
// Alt+Space / a lone Super press toggle the cube-status app menu.
#define MENU_TOGGLE "pkill -USR1 -u \"$(id -u)\" -x cube-status"
#define WORKSPACES 4
static const float C_WHITE[4] = {1.0f, 1.0f, 1.0f, 1.0f};
// runtime-tunable via ~/.config/whitecube/config or /etc/whitecube/config
static int g_tab_w = 180, g_tab_h = 40, g_grid = 64;
static float g_tabbar[4] = {0.035f, 0.047f, 0.071f, 0.96f};
static float g_grid_c[4] = {0.047f, 0.059f, 0.082f, 1.0f};
static float g_border[4] = {0.05f, 0.05f, 0.05f, 1.0f};
static float g_unfocused[4] = {212.f / 255, 212.f / 255, 212.f / 255, 1.0f};
static float g_bg[4] = {0.024f, 0.031f, 0.051f, 1.0f};
static char g_terminal[128], g_chat_app_id[128], g_launcher[128];
static char g_chat_cmd[256], g_help_cmd[256], g_scripts_cmd[256];
static char g_scripts_app_id[128], g_browser_cmd[256], g_settings_cmd[256];
// Keyboard: empty strings mean the xkbcommon default (or XKB_DEFAULT_*).
static char g_xkb_model[64], g_xkb_layout[64], g_xkb_variant[64], g_xkb_options[256];
static int g_repeat_rate = 25, g_repeat_delay = 600;
static bool g_hide_titles, g_remember_layout = true, g_save_titles = true;
static bool g_clipboard = true, g_clear_clipboard_on_lock = true;
static int g_lock_minutes;
/* -101/-1 inherit the actual device defaults until the user chooses a value. */
static int g_pointer_speed = -101, g_natural_scroll = -1, g_left_handed = -1;
static int g_tap_to_click = -1, g_disable_while_typing = -1;
static struct wc_accessibility g_accessibility = {1, 1, false, false};
static int tabs_left(void) { return (int)(BAR_APPS_W * g_accessibility.ui_scale); }
// The four workspace cubes (26 px pitch) sit just left of the power button.
static int cubes_left(int W) { return W - (int)((112 + BAR_POWER_W) * g_accessibility.ui_scale); }
static int g_base_tab_w, g_base_tab_h;
static float g_saved_border[4], g_saved_unfocused[4];
static void parse_hex(const char *s, float c[4]) {
	unsigned r, g, b;
	while (*s == ' ' || *s == '#') s++;
	if (sscanf(s, "%02x%02x%02x", &r, &g, &b) == 3) {
		c[0] = r / 255.f; c[1] = g / 255.f; c[2] = b / 255.f; c[3] = 1.f;
	}
}
static int config_integer(const char *value, int previous, int minimum, int maximum) {
	char *end;
	errno = 0;
	long number = strtol(value, &end, 10);
	if (errno || end == value) return previous;
	while (isspace((unsigned char)*end)) end++;
	if (*end) return previous;
	if (number < minimum) return minimum;
	if (number > maximum) return maximum;
	return (int)number;
}
static bool config_boolean(const char *value, bool previous) {
	if (!strcmp(value, "1") || !strcmp(value, "true")) return true;
	if (!strcmp(value, "0") || !strcmp(value, "false")) return false;
	return previous;
}
static void config_apply(const char *key, const char *v, void *unused) {
	if (strcmp(key, "tab_w") == 0) g_tab_w = config_integer(v, g_tab_w, 60, 400);
	else if (strcmp(key, "tab_h") == 0) g_tab_h = config_integer(v, g_tab_h, 20, 80);
	else if (strcmp(key, "grid_step") == 0) g_grid = config_integer(v, g_grid, 16, 256);
	else if (strcmp(key, "bg") == 0) parse_hex(v, g_bg);
	else if (strcmp(key, "grid") == 0) parse_hex(v, g_grid_c);
	else if (strcmp(key, "tabbar") == 0) parse_hex(v, g_tabbar);
	else if (strcmp(key, "border") == 0) parse_hex(v, g_border);
	else if (strcmp(key, "unfocused") == 0) parse_hex(v, g_unfocused);
	else if (strcmp(key, "terminal") == 0) snprintf(g_terminal, sizeof(g_terminal), "%s", v);
	else if (strcmp(key, "chat_app_id") == 0) snprintf(g_chat_app_id, sizeof(g_chat_app_id), "%s", v);
	else if (strcmp(key, "chat_cmd") == 0) snprintf(g_chat_cmd, sizeof(g_chat_cmd), "%s", v);
	else if (strcmp(key, "help_cmd") == 0) snprintf(g_help_cmd, sizeof(g_help_cmd), "%s", v);
	else if (strcmp(key, "scripts_app_id") == 0) snprintf(g_scripts_app_id, sizeof(g_scripts_app_id), "%s", v);
	else if (strcmp(key, "scripts_cmd") == 0) snprintf(g_scripts_cmd, sizeof(g_scripts_cmd), "%s", v);
	else if (strcmp(key, "browser_cmd") == 0) snprintf(g_browser_cmd, sizeof(g_browser_cmd), "%s", v);
	else if (strcmp(key, "launcher") == 0) snprintf(g_launcher, sizeof(g_launcher), "%s", v);
	else if (strcmp(key, "settings_cmd") == 0) snprintf(g_settings_cmd, sizeof(g_settings_cmd), "%s", v);
	else if (strcmp(key, "xkb_model") == 0) snprintf(g_xkb_model, sizeof(g_xkb_model), "%s", v);
	else if (strcmp(key, "xkb_layout") == 0) snprintf(g_xkb_layout, sizeof(g_xkb_layout), "%s", v);
	else if (strcmp(key, "xkb_variant") == 0) snprintf(g_xkb_variant, sizeof(g_xkb_variant), "%s", v);
	else if (strcmp(key, "xkb_options") == 0) snprintf(g_xkb_options, sizeof(g_xkb_options), "%s", v);
	else if (strcmp(key, "repeat_rate") == 0) g_repeat_rate = config_integer(v, g_repeat_rate, 0, 100);
	else if (strcmp(key, "repeat_delay") == 0) g_repeat_delay = config_integer(v, g_repeat_delay, 100, 2000);
	else if (strcmp(key, "privacy_hide_titles") == 0) g_hide_titles = config_boolean(v, g_hide_titles);
	else if (strcmp(key, "privacy_remember_layout") == 0) g_remember_layout = config_boolean(v, g_remember_layout);
	else if (strcmp(key, "privacy_save_titles") == 0) g_save_titles = config_boolean(v, g_save_titles);
	else if (strcmp(key, "privacy_clipboard") == 0) g_clipboard = config_boolean(v, g_clipboard);
	else if (strcmp(key, "privacy_clear_clipboard_on_lock") == 0) g_clear_clipboard_on_lock = config_boolean(v, g_clear_clipboard_on_lock);
	else if (strcmp(key, "privacy_lock_minutes") == 0) g_lock_minutes = config_integer(v, g_lock_minutes, 0, 120);
	else if (!strcmp(key, "pointer_speed")) g_pointer_speed = config_integer(v, g_pointer_speed, -100, 100);
	else if (!strcmp(key, "natural_scroll")) g_natural_scroll = config_boolean(v, g_natural_scroll == 1);
	else if (!strcmp(key, "left_handed")) g_left_handed = config_boolean(v, g_left_handed == 1);
	else if (!strcmp(key, "tap_to_click")) g_tap_to_click = config_boolean(v, g_tap_to_click == 1);
	else if (!strcmp(key, "disable_while_typing")) g_disable_while_typing = config_boolean(v, g_disable_while_typing == 1);
}
static void load_config_file(const char *path) {
	FILE *f = fopen(path, "r");
	if (!f) return;
	char *line = NULL; size_t cap = 0;
	while (getline(&line, &cap, f) >= 0) {
		if (line[0] == '#' || line[0] == '\n') continue;
		char *eq = strchr(line, '=');
		if (!eq) continue;
		*eq++ = 0; eq[strcspn(eq, "\r\n")] = 0;
		config_apply(line, eq, NULL);
	}
	free(line); fclose(f);
}
// Keys missing from a reloaded config return to these built-in defaults.
static void config_defaults(void) {
	g_tab_w = 180; g_tab_h = 40; g_grid = 64;
	memcpy(g_tabbar, (float[4]){0.035f, 0.047f, 0.071f, 0.96f}, sizeof(g_tabbar));
	memcpy(g_grid_c, (float[4]){0.047f, 0.059f, 0.082f, 1.0f}, sizeof(g_grid_c));
	memcpy(g_border, (float[4]){0.05f, 0.05f, 0.05f, 1.0f}, sizeof(g_border));
	memcpy(g_unfocused, (float[4]){212.f / 255, 212.f / 255, 212.f / 255, 1.0f}, sizeof(g_unfocused));
	memcpy(g_bg, (float[4]){0.024f, 0.031f, 0.051f, 1.0f}, sizeof(g_bg));
	snprintf(g_terminal, sizeof(g_terminal), "%s", "/usr/bin/whitecube-terminal");
	snprintf(g_launcher, sizeof(g_launcher), "%s", "fuzzel");
	snprintf(g_chat_app_id, sizeof(g_chat_app_id), "%s", "cube-chat");
	snprintf(g_chat_cmd, sizeof(g_chat_cmd), "%s", "foot -a cube-chat -T cube-chat /usr/bin/cube-chat");
	snprintf(g_scripts_app_id, sizeof(g_scripts_app_id), "%s", "cube-scripts");
	snprintf(g_scripts_cmd, sizeof(g_scripts_cmd), "%s", "/usr/bin/whitecube-terminal -a cube-scripts -T Scripts /usr/bin/cube-scripts");
	snprintf(g_browser_cmd, sizeof(g_browser_cmd), "%s", "/usr/bin/whitecube-browser");
	snprintf(g_help_cmd, sizeof(g_help_cmd), "%s", "/usr/bin/whitecube-terminal -T keys -e less /usr/share/whitecube/keys.txt");
	snprintf(g_settings_cmd, sizeof(g_settings_cmd), "%s", "cube-settings");
	g_xkb_model[0] = g_xkb_layout[0] = g_xkb_variant[0] = g_xkb_options[0] = 0;
	g_repeat_rate = 25; g_repeat_delay = 600;
	g_hide_titles = false; g_remember_layout = true; g_save_titles = true;
	g_clipboard = true; g_clear_clipboard_on_lock = true; g_lock_minutes = 0;
	g_pointer_speed = -101; g_natural_scroll = g_left_handed = g_tap_to_click = g_disable_while_typing = -1;
}
static void load_config(void) {
	config_defaults();
	load_config_file("/etc/whitecube/config");
	const char *home = getenv("HOME");
	if (home) {
		char path[512];
		snprintf(path, sizeof(path), "%s/.config/whitecube/config", home);
		load_config_file(path);
	}
	wc_prefs_visit(config_apply, NULL);
	if (g_tab_w < 60) g_tab_w = 60;
	if (g_tab_h < 20) g_tab_h = 20;
	if (g_grid < 16) g_grid = 16;
	if (g_repeat_rate < 0 || g_repeat_rate > 100) g_repeat_rate = 25;
	if (g_repeat_delay < 100 || g_repeat_delay > 2000) g_repeat_delay = 600;
}
static void report_preferences(void) {
	if (!getenv("WHITECUBE_SETTINGS_REPORT")) return;
	GKeyFile *report = g_key_file_new();
	g_key_file_set_integer(report, "settings", "tab_w", g_tab_w);
	g_key_file_set_integer(report, "settings", "tab_h", g_tab_h);
	g_key_file_set_integer(report, "settings", "grid_step", g_grid);
	g_key_file_set_integer(report, "settings", "repeat_rate", g_repeat_rate);
	g_key_file_set_integer(report, "settings", "repeat_delay", g_repeat_delay);
	g_key_file_set_integer(report, "settings", "privacy_lock_minutes", g_lock_minutes);
	g_key_file_set_integer(report, "settings", "pointer_speed", g_pointer_speed);
	g_key_file_set_integer(report, "settings", "natural_scroll", g_natural_scroll);
	g_key_file_set_integer(report, "settings", "left_handed", g_left_handed);
	g_key_file_set_integer(report, "settings", "tap_to_click", g_tap_to_click);
	g_key_file_set_integer(report, "settings", "disable_while_typing", g_disable_while_typing);
	g_key_file_set_boolean(report, "settings", "privacy_hide_titles", g_hide_titles);
	g_key_file_set_boolean(report, "settings", "privacy_remember_layout", g_remember_layout);
	g_key_file_set_boolean(report, "settings", "privacy_save_titles", g_save_titles);
	g_key_file_set_boolean(report, "settings", "privacy_clipboard", g_clipboard);
	g_key_file_set_boolean(report, "settings", "privacy_clear_clipboard_on_lock", g_clear_clipboard_on_lock);
	g_key_file_set_string(report, "settings", "terminal", g_terminal);
	g_key_file_set_string(report, "settings", "launcher", g_launcher);
	g_key_file_set_string(report, "settings", "browser_cmd", g_browser_cmd);
	g_key_file_set_string(report, "settings", "settings_cmd", g_settings_cmd);
	g_key_file_set_string(report, "settings", "help_cmd", g_help_cmd);
	g_key_file_set_string(report, "settings", "chat_cmd", g_chat_cmd);
	g_key_file_set_string(report, "settings", "chat_app_id", g_chat_app_id);
	g_key_file_set_string(report, "settings", "scripts_cmd", g_scripts_cmd);
	g_key_file_set_string(report, "settings", "scripts_app_id", g_scripts_app_id);
	g_key_file_set_string(report, "settings", "xkb_model", g_xkb_model);
	g_key_file_set_string(report, "settings", "xkb_layout", g_xkb_layout);
	g_key_file_set_string(report, "settings", "xkb_variant", g_xkb_variant);
	g_key_file_set_string(report, "settings", "xkb_options", g_xkb_options);
	{ char colour[8]; snprintf(colour, sizeof(colour), "%02x%02x%02x", (unsigned)lround(g_bg[0]*255), (unsigned)lround(g_bg[1]*255), (unsigned)lround(g_bg[2]*255)); g_key_file_set_string(report, "settings", "bg", colour); }
	{ char colour[8]; snprintf(colour, sizeof(colour), "%02x%02x%02x", (unsigned)lround(g_grid_c[0]*255), (unsigned)lround(g_grid_c[1]*255), (unsigned)lround(g_grid_c[2]*255)); g_key_file_set_string(report, "settings", "grid", colour); }
	{ char colour[8]; snprintf(colour, sizeof(colour), "%02x%02x%02x", (unsigned)lround(g_tabbar[0]*255), (unsigned)lround(g_tabbar[1]*255), (unsigned)lround(g_tabbar[2]*255)); g_key_file_set_string(report, "settings", "tabbar", colour); }
	{ char colour[8]; snprintf(colour, sizeof(colour), "%02x%02x%02x", (unsigned)lround(g_border[0]*255), (unsigned)lround(g_border[1]*255), (unsigned)lround(g_border[2]*255)); g_key_file_set_string(report, "settings", "border", colour); }
	{ char colour[8]; snprintf(colour, sizeof(colour), "%02x%02x%02x", (unsigned)lround(g_unfocused[0]*255), (unsigned)lround(g_unfocused[1]*255), (unsigned)lround(g_unfocused[2]*255)); g_key_file_set_string(report, "settings", "unfocused", colour); }
	wc_pref_report(report, "compositor.ini");
}
struct wc_pointer {
	struct wl_list link;
	struct wlr_input_device *device;
	struct wl_listener destroy;
};
struct wc_drag {
	struct wc_toplevel *toplevel;
	uint32_t button;
	bool resize;
	double pointer_x, pointer_y;
	int work_w, work_h, x, y, w, h;
};
struct wc_server {
	struct wl_list pointers;
	struct wl_display *wl_display;
	struct wlr_backend *backend;
	struct wlr_renderer *renderer;
	struct wlr_allocator *allocator;
	struct wlr_scene *scene;
	struct wlr_scene_tree *background, *bottom, *windows, *top, *overlay;
	struct wlr_scene_tree *lock_tree;
	struct wlr_session_lock_manager_v1 *lock_manager;
	struct wlr_session_lock_v1 *lock;
	struct wl_listener new_lock, lock_new_surface, lock_unlock, lock_destroy;
	struct wl_list lock_surfaces;
	bool locked;
	struct timespec last_activity, last_lock_attempt;
	bool desktop_visible;
	unsigned workspace;
	struct wc_toplevel *saved_focus[WORKSPACES], *saved_normal[WORKSPACES];
	struct wlr_scene_output_layout *scene_layout;
	struct wlr_xdg_shell *xdg_shell;
	struct wl_listener new_xdg_toplevel;
	struct wl_listener new_xdg_popup;
	struct wl_list toplevels;
	struct wlr_layer_shell_v1 *layer_shell;
	struct wl_listener new_layer_surface;
	struct wlr_cursor *cursor;
	struct wlr_xcursor_manager *cursor_mgr;
	struct wl_listener cursor_motion, cursor_motion_absolute;
	struct wl_listener cursor_button, cursor_axis, cursor_frame;
	struct wlr_seat *seat;
	struct wl_listener new_input, request_cursor, request_set_selection;
	struct wl_list keyboards;
	struct wl_list outputs;
	struct wlr_output_layout *output_layout;
	struct wl_listener new_output;
	struct wlr_output_manager_v1 *output_manager;
	struct wl_listener output_manager_apply, output_manager_test, layout_change;
	struct timespec config_mtime[2];
	struct wc_toplevel *focused, *normal;
	uint64_t focus_order;
	struct wc_drag drag;
	struct wc_desktop_state layout;
	char state_directory[512];
	bool shutting_down;
	struct wl_event_source *settings_timer;
	/* Implicit grab: while a button is held, the pressed surface keeps the pointer. */
	struct wlr_surface *grab_surface; double grab_x, grab_y;
	uint32_t swallow_button; // a press the compositor consumed; its release is dropped too
	struct wl_listener request_set_primary_selection, request_activate;
	struct wlr_idle_inhibit_manager_v1 *idle_inhibit;
	struct wc_desktop_state saved_layout; bool saved_layout_valid;
};
struct wc_output {
	struct wl_list link;
	struct wc_server *server;
	struct wlr_output *wlr_output;
	struct wl_listener frame, present, request_state, destroy;
	struct wlr_scene_rect *lock_bg;
	uint32_t lock_commit_seq;
	bool lock_frame_submitted, lock_frame_presented;
	struct wlr_scene_tree *bg_tree;
	struct wlr_scene_rect *bg_flat;
	struct wlr_scene_tree *tab_tree;
	struct wlr_scene_rect *tab_bg;
	struct wlr_scene_rect *workspace_cube[WORKSPACES];
	// Tab overflow: "more" button in the last tab slot and the panel listing every tab.
	struct wlr_scene_rect *more_border, *more_cube;
	struct wlr_scene_buffer *more_label, *panel_buf;
	bool more_on, panel_open;
	int more_x, more_w, panel_scroll, panel_n, panel_selected;
	char more_key[192], panel_key[1024];
};
struct wc_toplevel {
	struct wl_list link;
	struct wc_server *server;
	struct wlr_xdg_toplevel *xdg_toplevel;
	struct wlr_scene_tree *scene_tree;
	struct wl_listener map, unmap, commit, destroy;
	struct wl_listener request_move, request_resize;
	struct wl_listener request_maximize, request_fullscreen;
	struct wlr_scene_rect *tab_border;
	struct wlr_scene_rect *tab_cube;
	struct wlr_scene_buffer *tab_label; // title and close glyph, drawn with pango
	struct wl_listener set_title;
	int label_w, label_h; float label_bg; char label_title[128];
	struct wc_output *output;
	bool mapped, hidden, fullscreen, maximized;
	int configured_w, configured_h;
	unsigned workspace;
	int saved_index, layout_x, layout_y, layout_w, layout_h;
	uint64_t focus_order;
};
struct wc_keyboard {
	struct wl_list link;
	struct wc_server *server;
	struct wlr_keyboard *wlr_keyboard;
	struct wl_listener modifiers, key, destroy;
	bool super_alone; // Super pressed with no other key since
	uint32_t swallow_key; // keycode + 1 of a press the tab panel consumed (its release is dropped too)
};
struct wc_layer_surface {
	struct wc_server *server;
	struct wlr_layer_surface_v1 *layer_surface;
	struct wlr_scene_layer_surface_v1 *scene;
	struct wl_listener map, unmap, destroy, commit;
	struct wc_output *output;
};
struct wc_lock_surface {
	struct wl_list link;
	struct wc_server *server;
	struct wlr_session_lock_surface_v1 *surface;
	struct wlr_scene_tree *tree;
	struct wl_listener map, destroy;
};
static void wc_lock_refresh(struct wc_server *s);
static void wc_lock_maybe_ack(struct wc_server *s);
static bool wc_lock_surface_allowed(struct wc_server *s, struct wlr_surface *surface);
static void wc_lock_focus(struct wc_server *s);
static void wc_activity(struct wc_server *s) { clock_gettime(CLOCK_MONOTONIC, &s->last_activity); }
static bool wc_idle_lock_due(struct wc_server *s, struct timespec now) {
	int64_t idle_ms = (int64_t)(now.tv_sec - s->last_activity.tv_sec) * 1000 +
		(now.tv_nsec - s->last_activity.tv_nsec) / 1000000;
	int64_t attempt_ms = (int64_t)(now.tv_sec - s->last_lock_attempt.tv_sec) * 1000 +
		(now.tv_nsec - s->last_lock_attempt.tv_nsec) / 1000000;
	return g_lock_minutes > 0 && !s->locked && idle_ms >= (int64_t)g_lock_minutes * 60000 && attempt_ms >= 30000;
}
static struct wc_output *server_first_output(struct wc_server *s) {
	if (wl_list_empty(&s->outputs)) return NULL;
	struct wc_output *o;
	wl_list_for_each(o, &s->outputs, link) if (o->wlr_output->enabled) return o;
	return wl_container_of(s->outputs.next, (struct wc_output *)0, link);
}
static int bounded(int value, int minimum, int maximum) {
	return value < minimum ? minimum : value > maximum ? maximum : value;
}
static void wc_cancel_drag(struct wc_server *s) {
	if (!s->drag.toplevel) return;
	/* Keep the consumed button until its physical release, even after unmap. */
	s->drag = (struct wc_drag){.button = s->drag.button};
	if (s->cursor && s->cursor_mgr) wlr_cursor_set_xcursor(s->cursor, s->cursor_mgr, "default");
}
static void wc_update_drag(struct wc_server *s, double pointer_x, double pointer_y) {
	struct wc_drag *d = &s->drag;
	struct wc_toplevel *t = d->toplevel;
	if (!t || d->work_w <= 0 || d->work_h <= 0) return;
	int dx = (int)lround((pointer_x - d->pointer_x) * 1000 / d->work_w);
	int dy = (int)lround((pointer_y - d->pointer_y) * 1000 / d->work_h);
	t->layout_x = d->x; t->layout_y = d->y;
	t->layout_w = d->w; t->layout_h = d->h;
	if (d->resize) {
		int max_w = 1000 - d->x, max_h = 1000 - d->y;
		t->layout_w = bounded(d->w + dx, max_w < 100 ? max_w : 100, max_w);
		t->layout_h = bounded(d->h + dy, max_h < 100 ? max_h : 100, max_h);
	} else {
		t->layout_x = bounded(d->x + dx, 0, 1000 - d->w);
		t->layout_y = bounded(d->y + dy, 0, 1000 - d->h);
	}
}
static void wc_focus(struct wc_toplevel *t);
static void wc_tab_label(struct wc_toplevel *t, int w, int h, bool focused);
static void wc_more_update(struct wc_server *s, struct wc_output *o, int W, int n, int vis);
static void wc_panel_close(struct wc_server *s);
/* A toplevel with a parent is a dialog (file pickers, alerts). It floats over
 * its parent instead of taking a tab's whole screen, and focusing the parent
 * hands focus to it, because a modal parent ignores input while it is open. */
static bool is_dialog(struct wc_toplevel *t) { return t->xdg_toplevel->parent != NULL; }
static struct wc_toplevel *wc_toplevel_of(struct wlr_xdg_toplevel *x) {
	struct wlr_scene_tree *tree = x && x->base ? x->base->data : NULL;
	return tree ? tree->node.data : NULL;
}
static struct wc_toplevel *wc_open_dialog(struct wc_toplevel *t) {
	struct wc_toplevel *c;
	wl_list_for_each(c, &t->server->toplevels, link)
		if (c != t && c->mapped && c->xdg_toplevel->parent == t->xdg_toplevel) return c;
	return NULL;
}
static bool wc_is_ancestor(struct wc_toplevel *t, struct wc_toplevel *of) {
	for (struct wlr_xdg_toplevel *p = of ? of->xdg_toplevel->parent : NULL; p; p = p->parent)
		if (p == t->xdg_toplevel) return true;
	return false;
}
static bool wc_in_focus_chain(struct wc_toplevel *t, struct wc_toplevel *focused) {
	return t == focused || wc_is_ancestor(t, focused);
}
static void wc_raise_family(struct wc_toplevel *t) {
	if (!t) return;
	wc_raise_family(wc_toplevel_of(t->xdg_toplevel->parent));
	if (t->scene_tree->node.enabled) wlr_scene_node_raise_to_top(&t->scene_tree->node);
}
static struct wc_toplevel *wc_fullscreen_window(struct wc_server *s) {
	if (s->desktop_visible) return NULL;
	for (struct wc_toplevel *t = s->focused; t; t = wc_toplevel_of(t->xdg_toplevel->parent))
		if (t->mapped && !t->hidden && t->workspace == s->workspace && t->fullscreen) return t;
	return NULL;
}
static bool wc_idle_inhibited(struct wc_server *s);
static void wc_save_layout(struct wc_server *s) {
	if (s->shutting_down || !s->state_directory[0]) return;
	if (!g_remember_layout) return;
	if (!g_save_titles) for (unsigned i = 0; i < s->layout.count; i++) s->layout.windows[i].title[0] = 0;
	s->layout.workspace = s->workspace; s->layout.desktop_visible = s->desktop_visible;
	struct wc_toplevel *t;
	wl_list_for_each(t, &s->toplevels, link) {
		if (!t->mapped || is_dialog(t)) continue;
		const char *app = t->xdg_toplevel->app_id, *title = t->xdg_toplevel->title;
		if (!app || !*app) continue;
		if (t->saved_index < 0) {
			if (s->layout.count >= WC_SAVED_WINDOWS) continue;
			t->saved_index = (int)s->layout.count++;
		}
		struct wc_saved_window *saved = &s->layout.windows[t->saved_index];
		snprintf(saved->app, sizeof(saved->app), "%s", app);
		snprintf(saved->title, sizeof(saved->title), "%s", g_save_titles && title ? title : "");
		for (char *p = saved->app; *p; p++) if ((unsigned char)*p < 32 || *p == 127) *p = ' ';
		for (char *p = saved->title; *p; p++) if ((unsigned char)*p < 32 || *p == 127) *p = ' ';
		saved->workspace = t->workspace; saved->hidden = t->hidden;
		saved->fullscreen = t->fullscreen; saved->maximized = t->maximized; saved->claimed = true;
		saved->x = t->layout_x; saved->y = t->layout_y; saved->w = t->layout_w; saved->h = t->layout_h;
	}
	/* Retile runs on every click; two fsyncs each time stalled input delivery. */
	if (s->saved_layout_valid && !memcmp(&s->saved_layout, &s->layout, sizeof(s->layout))) return;
	if (wc_state_save(s->state_directory, &s->layout)) { s->saved_layout = s->layout; s->saved_layout_valid = true; }
}
static bool is_scripts(struct wc_toplevel *t) {
	const char *id = t->xdg_toplevel->app_id;
	return id && !strcmp(id, g_scripts_app_id);
}
static struct wc_toplevel *wc_find_app(struct wc_server *s, const char *app_id) {
	struct wc_toplevel *t;
	wl_list_for_each(t, &s->toplevels, link) {
		const char *id = t->xdg_toplevel->app_id;
		if (t->mapped && t->workspace == s->workspace && id && !strcmp(id, app_id)) return t;
	}
	return NULL;
}
// The wallpaper samples this private runtime hint to reduce work behind fullscreen apps.
static void fullscreen_hint(bool active) {
	const char *runtime = getenv("XDG_RUNTIME_DIR");
	struct stat st;
	if (!runtime || stat(runtime, &st) || st.st_uid != getuid() || (st.st_mode & 0022)) return;
	char path[512]; snprintf(path, sizeof(path), "%s/whitecube-fullscreen", runtime);
	int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (fd >= 0) { char value = active ? '1' : '0'; ssize_t n = write(fd, &value, 1); (void)n; close(fd); }
}
static void wc_retile(struct wc_server *server) {
	struct wc_output *out = server_first_output(server);
	if (!out || !out->wlr_output) return;
	int W, H;
	wlr_output_effective_resolution(out->wlr_output, &W, &H);
	if (W <= 0 || H <= 0) return;
	struct wlr_box box;
	wlr_output_layout_get_box(server->output_layout, out->wlr_output, &box);
	struct wc_toplevel *full = wc_fullscreen_window(server);
	int work_y = full ? 0 : g_tab_h, work_h = H - work_y;
	fullscreen_hint(full != NULL || server->locked);
	wlr_scene_node_set_enabled(&server->top->node, !full && !server->locked);
	wlr_scene_rect_set_size(out->tab_bg, W, g_tab_h);
	wlr_scene_node_set_position(&out->tab_tree->node, box.x, box.y);
	for (unsigned i = 0; i < WORKSPACES; i++) {
		int unit = (int)(26 * g_accessibility.ui_scale), left = cubes_left(W);
		wlr_scene_node_set_position(&out->workspace_cube[i]->node, left + i * unit, (g_tab_h - (int)(18 * g_accessibility.ui_scale)) / 2);
		wlr_scene_rect_set_size(out->workspace_cube[i], (int)(18 * g_accessibility.ui_scale), (int)(18 * g_accessibility.ui_scale));
		wlr_scene_rect_set_color(out->workspace_cube[i], i == server->workspace ? C_WHITE : g_grid_c);
	}
	int idx = 0, n_tabs = 0;
	struct wc_toplevel *t;
	wl_list_for_each(t, &server->toplevels, link)
		if (t->tab_border && t->mapped && t->workspace == server->workspace) n_tabs++;
	struct wc_bar_layout bar = wc_bar_layout(W, g_accessibility.ui_scale);
	int vis = wc_tab_visible(n_tabs, wc_tab_capacity(tabs_left(), TAB_GAP, g_tab_w, bar.tabs_end));
	wl_list_for_each(t, &server->toplevels, link) {
		if (t->tab_border) {
			bool tab_visible = t->mapped && t->workspace == server->workspace && idx < vis;
			wlr_scene_node_set_enabled(&t->tab_border->node, tab_visible);
			wlr_scene_node_set_enabled(&t->tab_cube->node, tab_visible);
			if (t->tab_label) wlr_scene_node_set_enabled(&t->tab_label->node, tab_visible);
			if (tab_visible) {
				int x = tabs_left() + TAB_GAP + idx++ * (g_tab_w + TAB_GAP);
				wlr_scene_node_set_position(&t->tab_border->node, x, 6);
				wlr_scene_node_set_position(&t->tab_cube->node, x + 2, 8);
				wlr_scene_rect_set_size(t->tab_border, g_tab_w, g_tab_h - 12);
				wlr_scene_rect_set_size(t->tab_cube, g_tab_w - 4, g_tab_h - 16);
				wlr_scene_rect_set_color(t->tab_border, t == server->focused && !t->hidden ? C_WHITE : g_border);
				wlr_scene_rect_set_color(t->tab_cube, t == server->focused && !t->hidden ? C_WHITE : g_unfocused);
				if (t->tab_label) {
					wlr_scene_node_set_position(&t->tab_label->node, x + 2, 8);
					wc_tab_label(t, g_tab_w - 4, g_tab_h - 16, t == server->focused && !t->hidden);
				}
			}
		}
		bool show = t->mapped && t->workspace == server->workspace && !t->hidden && !server->desktop_visible &&
			(full ? t == full || (wc_is_ancestor(full, t) && wc_in_focus_chain(t, server->focused)) :
			(wc_in_focus_chain(t, server->normal) || wc_in_focus_chain(t, server->focused) || is_scripts(t)));
		wlr_scene_node_set_enabled(&t->scene_tree->node, show);
		if (!show) continue;
		int x = 0, y = work_y, w = W, h = work_h;
		if (is_scripts(t) && !full && !t->maximized) {
			w = W * 88 / 100; h = (H - g_tab_h) * 47 / 100;
			x = (W - w) / 2; y = H - h - 20;
			if (y < g_tab_h) y = g_tab_h;
		}
		if (t != full && !t->maximized && t->layout_w > 0 && t->layout_h > 0) {
			x = W * t->layout_x / 1000; y = work_y + work_h * t->layout_y / 1000;
			w = W * t->layout_w / 1000; h = work_h * t->layout_h / 1000;
		}
		if (is_dialog(t) && t != full && !t->maximized && !(t->layout_w > 0 && t->layout_h > 0)) {
			/* The client's own size (it was configured 0x0), within 90% of the screen. */
			struct wlr_box geo; wlr_xdg_surface_get_geometry(t->xdg_toplevel->base, &geo);
			int max_w = W * 9 / 10, max_h = work_h * 9 / 10;
			int min_w = t->xdg_toplevel->current.min_width, min_h = t->xdg_toplevel->current.min_height;
			w = geo.width > 0 ? geo.width : W * 7 / 10;
			h = geo.height > 0 ? geo.height : work_h * 3 / 4;
			if (w < min_w) w = min_w;
			if (h < min_h) h = min_h;
			if (w > max_w) w = max_w;
			if (h > max_h) h = max_h;
			x = (W - w) / 2; y = work_y + (work_h - h) / 2;
		}
		wlr_scene_node_set_position(&t->scene_tree->node, box.x + x, box.y + y);
		if (t->configured_w != w || t->configured_h != h) {
			wlr_xdg_toplevel_set_size(t->xdg_toplevel, w, h);
			t->configured_w = w; t->configured_h = h;
		}
	}
	/* Layout order also drives tabs, so never reorder it to change stacking.
	 * Raise parents before their dialogs, with the focused family last. */
	wc_raise_family(server->normal);
	wl_list_for_each(t, &server->toplevels, link)
		if (is_scripts(t)) wc_raise_family(t);
	wc_raise_family(server->focused);
	wc_more_update(server, out, W, n_tabs, vis);
	/* Persist the final placement once on release, rather than fsync per motion. */
	if (!server->drag.toplevel) wc_save_layout(server);
}
static void wc_focus(struct wc_toplevel *t) {
	if (!t || !t->mapped || t->workspace != t->server->workspace || t->server->locked) return;
	for (struct wc_toplevel *d = wc_open_dialog(t); d && d->workspace == t->workspace; d = wc_open_dialog(t)) t = d;
	struct wc_server *s = t->server;
	if (s->drag.toplevel && s->drag.toplevel != t) wc_cancel_drag(s);
	if (s->focused && s->focused != t)
		wlr_xdg_toplevel_set_activated(s->focused->xdg_toplevel, false);
	s->focused = t; t->hidden = false; s->desktop_visible = false;
	t->focus_order = ++s->focus_order;
	if (!is_scripts(t)) s->normal = t;
	s->saved_focus[s->workspace] = t; s->saved_normal[s->workspace] = s->normal;
	/* Each set_activated is a configure; clients repaint on it, which delayed
	 * the second click of a double-click when it was sent on every press. */
	if (!t->xdg_toplevel->scheduled.activated) wlr_xdg_toplevel_set_activated(t->xdg_toplevel, true);
	struct wlr_keyboard *kb = wlr_seat_get_keyboard(s->seat);
	if (kb) wlr_seat_keyboard_notify_enter(s->seat, t->xdg_toplevel->base->surface,
		kb->keycodes, kb->num_keycodes, &kb->modifiers);
	wc_retile(s);
}
static bool wc_start_drag(struct wc_server *s, struct wc_toplevel *t, uint32_t button) {
	if (!t || !t->mapped || t->hidden || t->fullscreen || t->maximized ||
	    t->workspace != s->workspace || s->desktop_visible) return false;
	struct wc_output *out = server_first_output(s);
	if (!out) return false;
	int W, H;
	wlr_output_effective_resolution(out->wlr_output, &W, &H);
	int work_y = wc_fullscreen_window(s) ? 0 : g_tab_h;
	H -= work_y;
	if (W <= 0 || H <= 0 || t->configured_w <= 0 || t->configured_h <= 0) return false;
	struct wlr_box box;
	wlr_output_layout_get_box(s->output_layout, out->wlr_output, &box);
	int w = bounded((int)lround(t->configured_w * 1000.0 / W), 1, 1000);
	int h = bounded((int)lround(t->configured_h * 1000.0 / H), 1, 1000);
	s->drag = (struct wc_drag){
		.toplevel = t, .button = button, .resize = button == BTN_RIGHT,
		.pointer_x = s->cursor->x, .pointer_y = s->cursor->y, .work_w = W, .work_h = H,
		.x = bounded((int)lround((t->scene_tree->node.x - box.x) * 1000.0 / W), 0, 1000 - w),
		.y = bounded((int)lround((t->scene_tree->node.y - box.y - work_y) * 1000.0 / H), 0, 1000 - h),
		.w = w, .h = h,
	};
	wlr_seat_pointer_clear_focus(s->seat);
	wlr_cursor_set_xcursor(s->cursor, s->cursor_mgr, button == BTN_RIGHT ? "se-resize" : "move");
	return true;
}
static void wc_focus_next(struct wc_server *s) {
	if (wl_list_empty(&s->toplevels)) return;
	struct wl_list *start = s->focused ? &s->focused->link : &s->toplevels;
	struct wl_list *n = start->next;
	while (n != start) {
		if (n == &s->toplevels) { n = n->next; continue; }
		struct wc_toplevel *t = wl_container_of(n, t, link);
		if (t->mapped && !t->hidden && t->workspace == s->workspace) { wc_focus(t); return; }
		n = n->next;
	}
}
static void wc_focus_previous(struct wc_server *s) {
	if (wl_list_empty(&s->toplevels)) return;
	struct wl_list *start = s->focused ? &s->focused->link : &s->toplevels, *n = start->prev;
	while (n != start) {
		if (n == &s->toplevels) { n = n->prev; continue; }
		struct wc_toplevel *t = wl_container_of(n, t, link);
		if (t->mapped && !t->hidden && t->workspace == s->workspace) { wc_focus(t); return; }
		n = n->prev;
	}
}
static void wc_adjust_window(struct wc_server *s, xkb_keysym_t sym, bool resize) {
	if (s->drag.toplevel) return;
	struct wc_toplevel *t = s->focused;
	if (!t || t->fullscreen || t->maximized) return;
	if (!t->layout_w || !t->layout_h) {
		t->layout_x = is_scripts(t) ? 60 : 0; t->layout_y = is_scripts(t) ? 490 : 0;
		t->layout_w = is_scripts(t) ? 880 : 1000; t->layout_h = is_scripts(t) ? 470 : 1000;
	}
	int dx = sym == XKB_KEY_Left ? -25 : sym == XKB_KEY_Right ? 25 : 0;
	int dy = sym == XKB_KEY_Up ? -25 : sym == XKB_KEY_Down ? 25 : 0;
	if (resize) {
		t->layout_w += dx; t->layout_h += dy;
		if (t->layout_w < 100) t->layout_w = 100;
		if (t->layout_h < 100) t->layout_h = 100;
		if (t->layout_x + t->layout_w > 1000) t->layout_w = 1000 - t->layout_x;
		if (t->layout_y + t->layout_h > 1000) t->layout_h = 1000 - t->layout_y;
	} else {
		t->layout_x += dx; t->layout_y += dy;
		if (t->layout_x < 0) t->layout_x = 0;
		if (t->layout_y < 0) t->layout_y = 0;
		if (t->layout_x + t->layout_w > 1000) t->layout_x = 1000 - t->layout_w;
		if (t->layout_y + t->layout_h > 1000) t->layout_y = 1000 - t->layout_h;
	}
	wc_retile(s);
}
static void wc_focus_nth(struct wc_server *s, int n) {
	int i = 0; struct wc_toplevel *t;
	wl_list_for_each(t, &s->toplevels, link)
		if (t->mapped && t->workspace == s->workspace && i++ == n) { wc_focus(t); return; }
}
static void wc_focus_available(struct wc_server *s) {
	if (s->focused) wlr_xdg_toplevel_set_activated(s->focused->xdg_toplevel, false);
	s->focused = NULL; s->saved_focus[s->workspace] = NULL;
	if (s->locked) { wc_lock_focus(s); wc_retile(s); return; }
	struct wc_toplevel *t, *recent = NULL;
	wl_list_for_each(t, &s->toplevels, link)
		if (t->mapped && !t->hidden && t->workspace == s->workspace &&
		    (!recent || t->focus_order > recent->focus_order)) recent = t;
	if (recent) { wc_focus(recent); return; }
	wlr_seat_keyboard_clear_focus(s->seat);
	wc_retile(s);
}
static void forget_toplevel(struct wc_toplevel *t) {
	struct wc_server *s = t->server;
	if (s->drag.toplevel == t) wc_cancel_drag(s);
	for (unsigned i = 0; i < WORKSPACES; i++) {
		if (s->saved_focus[i] == t) s->saved_focus[i] = NULL;
		if (s->saved_normal[i] == t) s->saved_normal[i] = NULL;
	}
	if (s->normal == t) s->normal = NULL;
}
/* A window and its open dialogs travel together, or the parent would be left
 * frozen behind a picker on another workspace. */
static void wc_move_family(struct wc_toplevel *t, unsigned target) {
	struct wc_toplevel *root = t, *up;
	while ((up = wc_toplevel_of(root->xdg_toplevel->parent))) root = up;
	struct wc_toplevel *c;
	wl_list_for_each(c, &t->server->toplevels, link)
		if (c == root || wc_is_ancestor(root, c)) {
			if (c != t) forget_toplevel(c);
			c->workspace = target;
		}
}
static void wc_workspace(struct wc_server *s, unsigned target, bool move) {
	if (target >= WORKSPACES || target == s->workspace) return;
	wc_cancel_drag(s);
	if (move) {
		struct wc_toplevel *t = s->focused;
		if (!t) return;
		forget_toplevel(t);
		wc_move_family(t, target);
		s->saved_focus[target] = t;
		if (!is_scripts(t)) s->saved_normal[target] = t;
		wc_focus_available(s);
	} else {
		if (s->focused) wlr_xdg_toplevel_set_activated(s->focused->xdg_toplevel, false);
		s->workspace = target;
		s->focused = s->saved_focus[target]; s->normal = s->saved_normal[target];
		s->desktop_visible = false;
		if (s->focused && s->focused->mapped && !s->focused->hidden) wc_focus(s->focused);
		else wc_focus_available(s);
	}
	wlr_seat_pointer_clear_focus(s->seat);
	wc_retile(s);
}
static void wc_close_focused(struct wc_server *s) {
	if (s->focused) wlr_xdg_toplevel_send_close(s->focused->xdg_toplevel);
}
// Tab labels: a cairo image wrapped as a wlr_buffer for the scene graph.
struct wc_cairo_buffer { struct wlr_buffer base; cairo_surface_t *surface; };
static void cairo_buffer_destroy(struct wlr_buffer *b) {
	struct wc_cairo_buffer *cb = wl_container_of(b, cb, base);
	cairo_surface_destroy(cb->surface); free(cb);
}
static bool cairo_buffer_begin(struct wlr_buffer *b, uint32_t flags, void **data, uint32_t *format, size_t *stride) {
	struct wc_cairo_buffer *cb = wl_container_of(b, cb, base);
	if (flags & WLR_BUFFER_DATA_PTR_ACCESS_WRITE) return false;
	*data = cairo_image_surface_get_data(cb->surface);
	*format = DRM_FORMAT_ARGB8888;
	*stride = cairo_image_surface_get_stride(cb->surface);
	return true;
}
static void cairo_buffer_end(struct wlr_buffer *b) {}
static const struct wlr_buffer_impl cairo_buffer_impl = {
	.destroy = cairo_buffer_destroy, .begin_data_ptr_access = cairo_buffer_begin, .end_data_ptr_access = cairo_buffer_end,
};
static int tab_close_w(void) { return g_tab_h - 16; } // square close glyph at the tab's right end
static const char *tab_title(struct wc_toplevel *t) {
	const char *title = t->xdg_toplevel->title, *app = t->xdg_toplevel->app_id;
	if (g_hide_titles) return app && *app ? app : "window";
	return title && *title ? title : app && *app ? app : "window";
}
static void wc_tab_label(struct wc_toplevel *t, int w, int h, bool focused) {
	if (!t->tab_label || w <= 0 || h <= 0) return;
	const char *title = tab_title(t);
	const float *bg = focused ? C_WHITE : g_unfocused;
	if (w == t->label_w && h == t->label_h && bg[0] == t->label_bg &&
	    !strncmp(title, t->label_title, sizeof(t->label_title) - 1)) return;
	struct wc_cairo_buffer *cb = calloc(1, sizeof(*cb));
	if (!cb) return;
	cb->surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, w, h);
	if (cairo_surface_status(cb->surface) != CAIRO_STATUS_SUCCESS) { cairo_surface_destroy(cb->surface); free(cb); return; }
	t->label_w = w; t->label_h = h; t->label_bg = bg[0];
	snprintf(t->label_title, sizeof(t->label_title), "%s", title);
	wlr_buffer_init(&cb->base, &cairo_buffer_impl, w, h);
	double lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2], ink = lum > 0.5 ? 0.05 : 0.97;
	int cw = tab_close_w() < h ? tab_close_w() : h;
	cairo_t *cr = cairo_create(cb->surface);
	cairo_set_source_rgb(cr, ink, ink, ink);
	PangoLayout *lo = pango_cairo_create_layout(cr);
	PangoFontDescription *fd = pango_font_description_from_string(focused ? "DejaVu Sans Bold" : "DejaVu Sans");
	pango_font_description_set_absolute_size(fd, h * 0.54 * PANGO_SCALE);
	pango_layout_set_font_description(lo, fd);
	pango_font_description_free(fd);
	pango_layout_set_single_paragraph_mode(lo, true);
	pango_layout_set_text(lo, title, -1);
	pango_layout_set_width(lo, (w - cw - 14) * PANGO_SCALE);
	pango_layout_set_ellipsize(lo, PANGO_ELLIPSIZE_END);
	int tw, th;
	pango_layout_get_pixel_size(lo, &tw, &th);
	cairo_move_to(cr, 8, (h - th) / 2.0);
	pango_cairo_show_layout(cr, lo);
	g_object_unref(lo);
	double cx = w - cw / 2.0 - 2, cy = h / 2.0, r = cw * 0.2;
	cairo_set_line_width(cr, h > 30 ? 2.2 : 1.6);
	cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND);
	cairo_move_to(cr, cx - r, cy - r); cairo_line_to(cr, cx + r, cy + r);
	cairo_move_to(cr, cx + r, cy - r); cairo_line_to(cr, cx - r, cy + r);
	cairo_stroke(cr);
	cairo_destroy(cr);
	cairo_surface_flush(cb->surface);
	wlr_scene_buffer_set_buffer(t->tab_label, &cb->base);
	wlr_buffer_drop(&cb->base);
}
// ---- Tab overflow: "more" button in the last tab slot, and a panel under the bar listing every tab.
static struct wc_toplevel *wc_nth_tab(struct wc_server *s, int n) {
	struct wc_toplevel *t; int i = 0;
	wl_list_for_each(t, &s->toplevels, link)
		if (t->tab_border && t->mapped && t->workspace == s->workspace && i++ == n) return t;
	return NULL;
}
static int wc_count_tabs(struct wc_server *s) {
	struct wc_toplevel *t; int n = 0;
	wl_list_for_each(t, &s->toplevels, link) if (t->tab_border && t->mapped && t->workspace == s->workspace) n++;
	return n;
}
// A tab-looking face (dark border, light fill, title, close cross) used for the panel rows and the button.
static void wc_draw_face(cairo_t *cr, int x, int y, int w, int h, bool focused, const char *prefix, const char *title, bool close, bool chevron) {
	const float *bg = focused ? C_WHITE : g_unfocused;
	cairo_set_source_rgb(cr, focused ? C_WHITE[0] : g_border[0], focused ? C_WHITE[1] : g_border[1], focused ? C_WHITE[2] : g_border[2]);
	cairo_rectangle(cr, x, y, w, h); cairo_fill(cr);
	cairo_set_source_rgb(cr, bg[0], bg[1], bg[2]);
	cairo_rectangle(cr, x + 2, y + 2, w - 4, h - 4); cairo_fill(cr);
	double lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2], ink = lum > 0.5 ? 0.05 : 0.97;
	int ih = h - 4, cw = (close || chevron) ? (ih < tab_close_w() ? ih : tab_close_w()) : 0;
	cairo_set_source_rgb(cr, ink, ink, ink);
	PangoLayout *lo = pango_cairo_create_layout(cr);
	PangoFontDescription *fd = pango_font_description_from_string(focused ? "DejaVu Sans Bold" : "DejaVu Sans");
	pango_font_description_set_absolute_size(fd, ih * 0.54 * PANGO_SCALE);
	pango_layout_set_font_description(lo, fd);
	pango_font_description_free(fd);
	pango_layout_set_single_paragraph_mode(lo, true);
	char text[192]; snprintf(text, sizeof(text), "%s%s", prefix, title);
	pango_layout_set_text(lo, text, -1);
	pango_layout_set_width(lo, (w - 4 - cw - 14) * PANGO_SCALE);
	pango_layout_set_ellipsize(lo, PANGO_ELLIPSIZE_END);
	int tw, th; pango_layout_get_pixel_size(lo, &tw, &th);
	cairo_move_to(cr, x + 2 + 8, y + 2 + (ih - th) / 2.0);
	pango_cairo_show_layout(cr, lo);
	g_object_unref(lo);
	double cx = x + w - 2 - cw / 2.0 - 2, cy = y + h / 2.0, r = cw * 0.2;
	cairo_set_line_width(cr, ih > 30 ? 2.2 : 1.6);
	cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND);
	if (close) {
		cairo_move_to(cr, cx - r, cy - r); cairo_line_to(cr, cx + r, cy + r);
		cairo_move_to(cr, cx + r, cy - r); cairo_line_to(cr, cx - r, cy + r);
		cairo_stroke(cr);
	} else if (chevron) {
		cairo_move_to(cr, cx - r, cy - r / 2); cairo_line_to(cr, cx, cy + r / 2); cairo_line_to(cr, cx + r, cy - r / 2);
		cairo_stroke(cr);
	}
}
static void wc_set_cairo(struct wlr_scene_buffer *node, cairo_surface_t *surface, int w, int h) {
	struct wc_cairo_buffer *cb = calloc(1, sizeof(*cb));
	if (!cb) { cairo_surface_destroy(surface); return; }
	cb->surface = surface;
	wlr_buffer_init(&cb->base, &cairo_buffer_impl, w, h);
	cairo_surface_flush(surface);
	wlr_scene_buffer_set_buffer(node, &cb->base);
	wlr_buffer_drop(&cb->base);
}
static bool wc_out_size(struct wc_output *o, int *W, int *H) {
	if (!o || !o->wlr_output) return false;
	wlr_output_effective_resolution(o->wlr_output, W, H);
	return *W > 0 && *H > 0;
}
static void wc_panel_close(struct wc_server *s) {
	struct wc_output *o = server_first_output(s);
	if (!o || !o->panel_buf) return;
	o->panel_open = false; o->panel_scroll = 0; o->panel_key[0] = 0;
	wlr_scene_node_set_enabled(&o->panel_buf->node, false);
}
static void wc_panel_refresh(struct wc_server *s) {
	struct wc_output *o = server_first_output(s);
	int W, H, n = wc_count_tabs(s);
	if (!o || !o->panel_open || !wc_out_size(o, &W, &H) || !o->more_on) { wc_panel_close(s); return; }
	struct wc_panel_geom g = wc_panel_geom(W, H, g_tab_w, g_tab_h, o->more_x, n);
	if (o->panel_selected >= n) o->panel_selected = n - 1;
	if (n > 0 && o->panel_selected < 0) o->panel_selected = 0;
	o->panel_scroll = wc_panel_clamp_scroll(&g, n, o->panel_scroll);
	// Re-render only when something visible changed.
	char key[1024]; int len = snprintf(key, sizeof(key), "%d,%d,%d,%d,%d,%d,%p|", g.x, g.w, g.h, n, o->panel_scroll, o->panel_selected, (void *)s->focused);
	for (int r = 0; r < g.rows && len < (int)sizeof(key) - 140; r++) {
		struct wc_toplevel *t = wc_nth_tab(s, o->panel_scroll + r);
		if (t) len += snprintf(key + len, sizeof(key) - len, "%d,%.100s|", t->hidden, tab_title(t));
	}
	wlr_scene_node_set_position(&o->panel_buf->node, g.x, g.y);
	wlr_scene_node_set_enabled(&o->panel_buf->node, true);
	wlr_scene_node_raise_to_top(&o->panel_buf->node);
	if (!strcmp(key, o->panel_key)) return;
	snprintf(o->panel_key, sizeof(o->panel_key), "%s", key);
	cairo_surface_t *surf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, g.w, g.h);
	if (cairo_surface_status(surf) != CAIRO_STATUS_SUCCESS) { cairo_surface_destroy(surf); return; }
	cairo_t *cr = cairo_create(surf);
	cairo_set_source_rgba(cr, g_tabbar[0], g_tabbar[1], g_tabbar[2], 0.98);
	cairo_paint(cr);
	cairo_set_source_rgba(cr, 1, 1, 1, 0.28);
	cairo_set_line_width(cr, 1);
	cairo_rectangle(cr, 0.5, 0.5, g.w - 1, g.h - 1); cairo_stroke(cr);
	for (int r = 0; r < g.rows; r++) {
		struct wc_toplevel *t = wc_nth_tab(s, o->panel_scroll + r);
		if (!t) break;
		char prefix[40]; int idx = o->panel_scroll + r;
		snprintf(prefix, sizeof(prefix), "%d %s%s ", idx + 1, t == s->focused && !t->hidden ? "• " : "", t->hidden ? "[hidden] " : "");
		wc_draw_face(cr, g.pad, g.pad + r * g.pitch, g.w - 2 * g.pad - (n > g.rows ? 6 : 0), g_tab_h - 12,
			idx == o->panel_selected, prefix, tab_title(t), true, false);
	}
	if (!n) wc_draw_face(cr, g.pad, g.pad, g.w - 2 * g.pad, g_tab_h - 12, false,
		"", "No open tabs", false, false);
	if (n > g.rows) { // slim scroll thumb on the right edge
		double track = g.h - 2 * g.pad, th = track * g.rows / n, ty = g.pad + track * o->panel_scroll / n;
		cairo_set_source_rgba(cr, 1, 1, 1, 0.55);
		cairo_rectangle(cr, g.w - g.pad - 3, ty, 3, th); cairo_fill(cr);
	}
	cairo_destroy(cr);
	wc_set_cairo(o->panel_buf, surf, g.w, g.h);
}
static void wc_more_label(struct wc_output *o, int w, int h, bool active, int n, const char *title) {
	char key[192]; snprintf(key, sizeof(key), "%d,%d,%d,%d,%s", w, h, active, n, active ? title : "");
	if (!strcmp(key, o->more_key)) return;
	snprintf(o->more_key, sizeof(o->more_key), "%s", key);
	cairo_surface_t *surf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, w, h);
	if (cairo_surface_status(surf) != CAIRO_STATUS_SUCCESS) { cairo_surface_destroy(surf); return; }
	cairo_t *cr = cairo_create(surf);
	char text[160]; snprintf(text, sizeof(text), "%d %s", n, n == 1 ? "tab" : "tabs");
	// the face paints its own border, so draw it at label size and let the scene rects sit underneath
	wc_draw_face(cr, 0, 0, w, h, active, "", text, false, true);
	cairo_destroy(cr);
	wc_set_cairo(o->more_label, surf, w, h);
}
static void wc_more_update(struct wc_server *s, struct wc_output *o, int W, int n, int vis) {
	if (!o->more_label) return;
	struct wc_bar_layout bar = wc_bar_layout(W, g_accessibility.ui_scale);
	int x = tabs_left() + TAB_GAP + vis * (g_tab_w + TAB_GAP), w = g_tab_w;
	if (x + w > bar.tabs_end) w = bar.tabs_end - x;
	bool on = w >= 24 && s->top->node.enabled && !s->locked;
	o->more_on = on; o->more_x = x; o->more_w = on ? w : 0;
	wlr_scene_node_set_enabled(&o->more_border->node, on);
	wlr_scene_node_set_enabled(&o->more_cube->node, on);
	wlr_scene_node_set_enabled(&o->more_label->node, on);
	if (!on) { if (o->panel_open) wc_panel_close(s); return; }
	// Highlight the count when a window on this workspace is focused; retain the count.
	struct wc_toplevel *t; int i = 0; bool active = false;
	wl_list_for_each(t, &s->toplevels, link) {
		if (!(t->tab_border && t->mapped && t->workspace == s->workspace)) continue;
		if (t == s->focused && !t->hidden && i >= vis) active = true;
		i++;
	}
	int bh = g_tab_h - 12;
	wlr_scene_node_set_position(&o->more_border->node, x, 6);
	wlr_scene_node_set_position(&o->more_cube->node, x + 2, 8);
	wlr_scene_node_set_position(&o->more_label->node, x, 6);
	wlr_scene_rect_set_size(o->more_border, w, bh);
	wlr_scene_rect_set_size(o->more_cube, w - 4, bh - 4);
	wlr_scene_rect_set_color(o->more_border, active ? C_WHITE : g_border);
	wlr_scene_rect_set_color(o->more_cube, active ? C_WHITE : g_unfocused);
	wc_more_label(o, w, bh, active, n, active && s->focused ? tab_title(s->focused) : "");
	if (o->panel_open) wc_panel_refresh(s);
}
static void wc_panel_toggle(struct wc_server *s) {
	struct wc_output *o = server_first_output(s);
	if (!o || !o->more_on) return;
	if (o->panel_open) { wc_panel_close(s); return; }
	o->panel_open = true; o->panel_scroll = 0; o->panel_key[0] = 0; o->panel_selected = 0;
	int n = wc_count_tabs(s), W, H;
	for (int i = 0; i < n; i++) if (wc_nth_tab(s, i) == s->focused) o->panel_selected = i;
	if (wc_out_size(o, &W, &H)) {
		struct wc_panel_geom g = wc_panel_geom(W, H, g_tab_w, g_tab_h, o->more_x, n);
		o->panel_scroll = wc_panel_reveal(&g, n, 0, o->panel_selected);
	}
	wc_panel_refresh(s);
}
static bool wc_panel_key(struct wc_server *s, xkb_keysym_t sym) {
	struct wc_output *o = server_first_output(s);
	if (!o || !o->panel_open) return false;
	if (sym == XKB_KEY_Escape) { wc_panel_close(s); return true; }
	int n = wc_count_tabs(s), W, H;
	if (!wc_out_size(o, &W, &H)) return false;
	struct wc_panel_geom g = wc_panel_geom(W, H, g_tab_w, g_tab_h, o->more_x, n);
	switch (sym) {
	case XKB_KEY_Down: case XKB_KEY_Tab: o->panel_selected++; break;
	case XKB_KEY_Up: case XKB_KEY_ISO_Left_Tab: o->panel_selected--; break;
	case XKB_KEY_Home: o->panel_selected = 0; break;
	case XKB_KEY_End: o->panel_selected = n - 1; break;
	case XKB_KEY_Page_Down: o->panel_selected += g.rows; break;
	case XKB_KEY_Page_Up: o->panel_selected -= g.rows; break;
	case XKB_KEY_Return: case XKB_KEY_KP_Enter: case XKB_KEY_space: {
		struct wc_toplevel *t = wc_nth_tab(s, o->panel_selected);
		wc_panel_close(s); if (t) wc_focus(t); return true;
	}
	case XKB_KEY_Delete: {
		struct wc_toplevel *t = wc_nth_tab(s, o->panel_selected);
		if (t) wlr_xdg_toplevel_send_close(t->xdg_toplevel);
		return true;
	}
	default: return false;
	}
	if (o->panel_selected >= n) o->panel_selected = n - 1;
	if (o->panel_selected < 0) o->panel_selected = 0;
	o->panel_scroll = wc_panel_reveal(&g, n, o->panel_scroll, o->panel_selected);
	wc_panel_refresh(s); return true;
}
// Pointer press while the bar is shown. Returns true when the press was consumed.
static bool wc_more_press(struct wc_server *s, struct wc_output *o, int W, int H, double px, double py, uint32_t button) {
	bool on_button = o->more_on && py >= 6 && py < g_tab_h - 6 && px >= o->more_x && px < o->more_x + o->more_w;
	if (!o->panel_open) {
		if (!on_button || button != BTN_LEFT) return false;
		wc_panel_toggle(s);
		return true;
	}
	int n = wc_count_tabs(s);
	struct wc_panel_geom g = wc_panel_geom(W, H, g_tab_w, g_tab_h, o->more_x, n);
	if (wc_panel_contains(&g, px, py)) {
		int row = wc_panel_row_at(&g, n, o->panel_scroll, px, py);
		struct wc_toplevel *t = row >= 0 ? wc_nth_tab(s, row) : NULL;
		if (!t) return true;
		if (button == BTN_MIDDLE || (button == BTN_LEFT && wc_panel_on_close(&g, tab_close_w(), px)))
			wlr_xdg_toplevel_send_close(t->xdg_toplevel);
		else if (button == BTN_LEFT) { wc_panel_close(s); wc_focus(t); }
		return true;
	}
	wc_panel_close(s);
	return on_button || py >= g_tab_h; // a click on the bar still reaches its tab or button; one on a window only dismisses
}
// Return the new session/process-group id. The readiness pipe prevents an
// immediate compositor shutdown from racing the child's setsid().
static pid_t spawn(const char *cmd) {
	if (!cmd) return -1;
	int ready[2];
	if (pipe(ready) < 0) return -1;
	fcntl(ready[0], F_SETFD, FD_CLOEXEC);
	fcntl(ready[1], F_SETFD, FD_CLOEXEC);
	pid_t pid = fork();
	if (pid == 0) {
		close(ready[0]);
		if (setsid() < 0) _exit(1);
		// Applications must be able to wait for their own children normally.
		signal(SIGCHLD, SIG_DFL);
		signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGHUP, SIG_DFL);
		// Wayland's signal event sources block these signals in the parent.
		// Children use normal Unix signal delivery, not that parent's signalfd.
		sigset_t mask; sigemptyset(&mask); sigprocmask(SIG_SETMASK, &mask, NULL);
		char value = 1;
		ssize_t n;
		do { n = write(ready[1], &value, 1); } while (n < 0 && errno == EINTR);
		close(ready[1]);
		if (n != 1) _exit(1);
		execl("/bin/sh", "sh", "-c", cmd, (void *)NULL);
		_exit(1);
	}
	close(ready[1]);
	char value = 0;
	ssize_t n;
	do { n = read(ready[0], &value, 1); } while (n < 0 && errno == EINTR);
	close(ready[0]);
	return pid > 0 && n == 1 ? pid : -1;
}
static void stop_startup(pid_t group) {
	// Startup's shell may already have exited. Its background clients still
	// belong to this group and must not survive as orphaned status processes.
	if (group > 0) kill(-group, SIGTERM);
}
static int terminate_signal(int signal_number, void *data) {
	(void)signal_number;
	wl_display_terminate(data);
	return 0;
}
static void wc_apply_keymap(struct wlr_keyboard *wk) {
	struct xkb_rule_names names = {
		.model = g_xkb_model[0] ? g_xkb_model : NULL, .layout = g_xkb_layout[0] ? g_xkb_layout : NULL,
		.variant = g_xkb_variant[0] ? g_xkb_variant : NULL, .options = g_xkb_options[0] ? g_xkb_options : NULL,
	};
	struct xkb_context *ctx = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	struct xkb_keymap *map = xkb_keymap_new_from_names(ctx, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
	if (!map) {
		wlr_log(WLR_ERROR, "keymap %s/%s/%s rejected; using default", g_xkb_layout, g_xkb_variant, g_xkb_options);
		map = xkb_keymap_new_from_names(ctx, NULL, XKB_KEYMAP_COMPILE_NO_FLAGS);
	}
	if (map) wlr_keyboard_set_keymap(wk, map);
	xkb_keymap_unref(map); xkb_context_unref(ctx);
	wlr_keyboard_set_repeat_info(wk, g_repeat_rate, g_repeat_delay);
}
// Derived values: base tab size for accessibility scaling and the contrast override.
static void apply_derived_settings(void) {
	g_base_tab_w = g_tab_w; g_base_tab_h = g_tab_h;
	memcpy(g_saved_border, g_border, sizeof(g_border)); memcpy(g_saved_unfocused, g_unfocused, sizeof(g_unfocused));
	g_tab_w = (int)(g_tab_w * g_accessibility.ui_scale); g_tab_h = (int)(g_tab_h * g_accessibility.ui_scale);
	if (g_accessibility.strong) { memcpy(g_border, C_WHITE, sizeof(g_border)); memcpy(g_unfocused, (float[4]){.5f,.5f,.5f,1}, sizeof(g_unfocused)); }
}
static void build_bg_grid(struct wc_output *o, int W, int H);
static void output_manager_update(struct wc_server *s);
static void wc_output_refresh(struct wc_output *o) {
	int W, H;
	wlr_output_effective_resolution(o->wlr_output, &W, &H);
	if (W <= 0 || H <= 0) { W = 1920; H = 1080; }
	build_bg_grid(o, W, H);
	wlr_scene_rect_set_color(o->tab_bg, g_tabbar);
	wc_lock_refresh(o->server);
}
static bool config_changed(struct wc_server *s) {
	const char *home = getenv("HOME");
	char user[512]; snprintf(user, sizeof(user), "%s/.config/whitecube/config", home ? home : "/nonexistent");
	const char *paths[2] = {"/etc/whitecube/config", user};
	bool changed = false;
	for (int i = 0; i < 2; i++) {
		struct stat st; struct timespec m = {0};
		if (!stat(paths[i], &st)) m = st.st_mtim;
		if (m.tv_sec != s->config_mtime[i].tv_sec || m.tv_nsec != s->config_mtime[i].tv_nsec) changed = true;
		s->config_mtime[i] = m;
	}
	return changed;
}
// Settings panels just save the config file; this picks it up within a second.
static void apply_pointer(struct wlr_input_device *dev);
static void wc_reload_config(struct wc_server *s) {
	wc_cancel_drag(s);
	load_config();
	if (!g_remember_layout) {
		if (s->state_directory[0] && !wc_state_forget(s->state_directory))
			wlr_log(WLR_ERROR, "could not remove the saved native layout");
		s->layout.count = 0;
		struct wc_toplevel *t;
		wl_list_for_each(t, &s->toplevels, link) t->saved_index = -1;
	}
	if (!g_clipboard) wlr_seat_set_selection(s->seat, NULL, wl_display_next_serial(s->wl_display));
	apply_derived_settings();
	struct wc_output *o;
	wl_list_for_each(o, &s->outputs, link) wc_output_refresh(o);
	struct wc_keyboard *kb;
	wl_list_for_each(kb, &s->keyboards, link) wc_apply_keymap(kb->wlr_keyboard);
	struct wc_pointer *pointer;
	wl_list_for_each(pointer, &s->pointers, link) apply_pointer(pointer->device);
	report_preferences();
	wlr_log(WLR_INFO, "config reloaded");
	wc_save_layout(s); /* Scrub saved titles even while every output is disconnected. */
	wc_retile(s);
}
static int accessibility_tick(void *data) {
	struct wc_server *s = data;
	bool file_changed = config_changed(s), prefs_changed = wc_prefs_changed();
	if (file_changed || prefs_changed) wc_reload_config(s);
	char path[600]; snprintf(path, sizeof(path), "%s/accessibility.json", s->state_directory);
	struct wc_accessibility settings = g_accessibility;
	if (s->state_directory[0] && wc_accessibility_load(path, &settings) && (settings.ui_scale != g_accessibility.ui_scale || settings.terminal_scale != g_accessibility.terminal_scale || settings.strong != g_accessibility.strong || settings.reduced_motion != g_accessibility.reduced_motion)) {
		wc_cancel_drag(s);
		g_accessibility = settings;
		g_tab_w = (int)(g_base_tab_w * settings.ui_scale); g_tab_h = (int)(g_base_tab_h * settings.ui_scale);
		memcpy(g_border, settings.strong ? C_WHITE : g_saved_border, sizeof(g_border));
		memcpy(g_unfocused, settings.strong ? (float[4]){.5f,.5f,.5f,1} : g_saved_unfocused, sizeof(g_unfocused));
		wc_retile(s);
	}
	snprintf(path, sizeof(path), "%s/native-names.json", s->state_directory);
	if (s->state_directory[0]) wc_names_load(path, &s->layout);
	if (wc_idle_inhibited(s)) wc_activity(s);
	struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
	if (wc_idle_lock_due(s, now)) {
		s->last_lock_attempt = now;
		spawn("swaylock -f -c 111827");
	}
	if (s->settings_timer) wl_event_source_timer_update(s->settings_timer, 1000);
	return 0;
}
static void dump_node(struct wlr_scene_node *n, int depth) {
	char pad[32];
	int d = depth < 15 ? depth : 15, i;
	for (i = 0; i < d; i++) pad[i] = ' ';
	pad[d] = 0;
	const char *ty = n->type == WLR_SCENE_NODE_TREE ? "tree" :
		n->type == WLR_SCENE_NODE_RECT ? "rect" : "buffer";
	struct wlr_scene_buffer *b = n->type == WLR_SCENE_NODE_BUFFER ?
		wlr_scene_buffer_from_node(n) : NULL;
	fprintf(stderr, "wc-tree %s%s at %d,%d %sabled%s\n", pad, ty, n->x, n->y,
		n->enabled ? "en" : "dis",
		b ? (b->primary_output ? " primary=yes" : " primary=NO") : "");
	if (n->type == WLR_SCENE_NODE_TREE) {
		struct wlr_scene_tree *tr = wlr_scene_tree_from_node(n);
		struct wlr_scene_node *c;
		wl_list_for_each(c, &tr->children, link) dump_node(c, depth + 1);
	}
}
static bool handle_keybinding(struct wc_server *server, xkb_keysym_t sym, uint32_t mods) {
	if (server->locked) return false;
	sym = xkb_keysym_to_lower(sym);
	if (sym >= XKB_KEY_F1 && sym <= XKB_KEY_F4) {
		wc_workspace(server, sym - XKB_KEY_F1, (mods & WLR_MODIFIER_SHIFT) != 0);
		return true;
	}
	if (sym == XKB_KEY_Left || sym == XKB_KEY_Right || sym == XKB_KEY_Up || sym == XKB_KEY_Down) {
		if (!(mods & (WLR_MODIFIER_SHIFT | WLR_MODIFIER_CTRL))) return false;
		wc_adjust_window(server, sym, (mods & WLR_MODIFIER_CTRL) != 0); return true;
	}
	switch (sym) {
	case XKB_KEY_Escape: // Alt+Shift+Esc: plain Alt+Esc is too easy to hit and ends the session
		if (!(mods & WLR_MODIFIER_SHIFT)) return false;
		wl_display_terminate(server->wl_display); break;
	case XKB_KEY_space: spawn(MENU_TOGGLE); break;
	case XKB_KEY_Return: spawn(g_terminal); break;
	case XKB_KEY_d: spawn(g_launcher); break;
	case XKB_KEY_Tab: if (mods & WLR_MODIFIER_SHIFT) wc_focus_previous(server); else wc_focus_next(server); break;
	case XKB_KEY_ISO_Left_Tab: wc_focus_previous(server); break;
	case XKB_KEY_a: spawn("/usr/bin/whitecube-terminal -T 'Whitecube display and layouts' /usr/bin/whitecube-desktop"); break;
	case XKB_KEY_c: spawn("/usr/bin/whitecube-terminal -T 'Cube field controls' cube-ca --menu"); break;
	case XKB_KEY_q: wc_close_focused(server); break;
	case XKB_KEY_n: spawn("whitecube-next-wallpaper"); break;
	case XKB_KEY_l: spawn("swaylock -f -c 111827"); break;
	case XKB_KEY_t: {
		struct wc_toplevel *found = wc_find_app(server, g_chat_app_id);
		if (found && found->mapped) wc_focus(found);
		else spawn(g_chat_cmd);
		break;
	}
	case XKB_KEY_g: wc_panel_toggle(server); break;
	case XKB_KEY_y: spawn("/usr/bin/desktop-chat --toggle"); break;
	case XKB_KEY_u: spawn("/usr/bin/desktop-chat --settings"); break;
	case XKB_KEY_b: {
		struct wc_toplevel *t = wc_find_app(server, g_scripts_app_id);
		if (!t) { spawn(g_scripts_cmd); break; }
		if (t->hidden || server->desktop_visible || !wc_in_focus_chain(t, server->focused)) { wc_focus(t); break; }
		if (server->drag.toplevel == t) wc_cancel_drag(server);
		t->hidden = true;
		wlr_xdg_toplevel_set_activated(t->xdg_toplevel, false);
		if (server->normal && server->normal->mapped) wc_focus(server->normal);
		else wc_focus_available(server);
		wc_retile(server);
		break;
	}
	case XKB_KEY_w: spawn(g_browser_cmd); break;
	case XKB_KEY_s: spawn(g_settings_cmd); break;
	case XKB_KEY_v:
		wc_cancel_drag(server);
		server->desktop_visible = !server->desktop_visible;
		if (server->desktop_visible) {
			wlr_seat_keyboard_clear_focus(server->seat);
			wlr_seat_pointer_clear_focus(server->seat);
		} else if (server->focused) wc_focus(server->focused);
		wc_retile(server);
		break;
	case XKB_KEY_h: spawn(g_help_cmd); break;
	case XKB_KEY_F12: dump_node(&server->scene->tree.node, 0); break;
	case XKB_KEY_f:
		wc_cancel_drag(server);
		if (server->focused) {
			struct wlr_xdg_toplevel *x = server->focused->xdg_toplevel;
			server->focused->fullscreen = !server->focused->fullscreen;
			wlr_xdg_toplevel_set_fullscreen(x, server->focused->fullscreen);
			wc_retile(server);
		}
		break;
	case XKB_KEY_1: wc_focus_nth(server, 0); break;
	case XKB_KEY_2: wc_focus_nth(server, 1); break;
	case XKB_KEY_3: wc_focus_nth(server, 2); break;
	case XKB_KEY_4: wc_focus_nth(server, 3); break;
	case XKB_KEY_5: wc_focus_nth(server, 4); break;
	case XKB_KEY_6: wc_focus_nth(server, 5); break;
	case XKB_KEY_7: wc_focus_nth(server, 6); break;
	case XKB_KEY_8: wc_focus_nth(server, 7); break;
	case XKB_KEY_9: wc_focus_nth(server, 8); break;
	default: return false;
	}
	return true;
}
static void kb_modifiers(struct wl_listener *l, void *data) {
	struct wc_keyboard *kb = wl_container_of(l, kb, modifiers);
	if (kb->server->locked && !wc_lock_surface_allowed(kb->server, kb->server->seat->keyboard_state.focused_surface)) return;
	wlr_seat_set_keyboard(kb->server->seat, kb->wlr_keyboard);
	if (kb->server->locked) wlr_seat_keyboard_send_modifiers(kb->server->seat, &kb->wlr_keyboard->modifiers);
	else wlr_seat_keyboard_notify_modifiers(kb->server->seat, &kb->wlr_keyboard->modifiers);
}
static void kb_key(struct wl_listener *l, void *data) {
	struct wc_keyboard *kb = wl_container_of(l, kb, key);
	struct wc_server *s = kb->server;
	struct wlr_keyboard_key_event *ev = data;
	wc_activity(s);
	if (s->locked) {
		kb->super_alone = false;
		if (wc_lock_surface_allowed(s, s->seat->keyboard_state.focused_surface)) {
			wlr_seat_set_keyboard(s->seat, kb->wlr_keyboard);
			wlr_seat_keyboard_send_key(s->seat, ev->time_msec, ev->keycode, ev->state);
		}
		return;
	}
	uint32_t kc = ev->keycode + 8;
	const xkb_keysym_t *syms; int n = xkb_state_key_get_syms(kb->wlr_keyboard->xkb_state, kc, &syms);
	bool handled = false;
	// Panel navigation is consumed on both press and release.
	struct wc_output *po = server_first_output(s);
	if (ev->state == WL_KEYBOARD_KEY_STATE_PRESSED && po && po->panel_open) {
		for (int i = 0; i < n; i++) if (wc_panel_key(s, syms[i])) {
			kb->swallow_key = ev->keycode + 1; return;
		}
	} else if (ev->state == WL_KEYBOARD_KEY_STATE_RELEASED && kb->swallow_key == ev->keycode + 1) {
		kb->swallow_key = 0; return;
	}
	for (int i = 0; i < n; i++) {
		bool super = syms[i] == XKB_KEY_Super_L || syms[i] == XKB_KEY_Super_R;
		if (ev->state == WL_KEYBOARD_KEY_STATE_PRESSED) kb->super_alone = super;
		else if (super && kb->super_alone) { kb->super_alone = false; spawn(MENU_TOGGLE); }
	}
	if ((wlr_keyboard_get_modifiers(kb->wlr_keyboard) & WLR_MODIFIER_ALT) &&
	    ev->state == WL_KEYBOARD_KEY_STATE_PRESSED) {
		for (int i = 0; i < n; i++) handled |= handle_keybinding(s, syms[i], wlr_keyboard_get_modifiers(kb->wlr_keyboard));
	}
	if (!handled) {
		wlr_seat_set_keyboard(s->seat, kb->wlr_keyboard);
		wlr_seat_keyboard_notify_key(s->seat, ev->time_msec, ev->keycode, ev->state);
	}
}
static void kb_destroy(struct wl_listener *l, void *data) {
	struct wc_keyboard *kb = wl_container_of(l, kb, destroy);
	wl_list_remove(&kb->modifiers.link); wl_list_remove(&kb->key.link);
	wl_list_remove(&kb->destroy.link); wl_list_remove(&kb->link);
	free(kb);
}
static void server_new_keyboard(struct wc_server *s, struct wlr_input_device *dev) {
	struct wlr_keyboard *wk = wlr_keyboard_from_input_device(dev);
	struct wc_keyboard *kb = calloc(1, sizeof(*kb));
	kb->server = s; kb->wlr_keyboard = wk;
	wc_apply_keymap(wk);
	kb->modifiers.notify = kb_modifiers; wl_signal_add(&wk->events.modifiers, &kb->modifiers);
	kb->key.notify = kb_key; wl_signal_add(&wk->events.key, &kb->key);
	kb->destroy.notify = kb_destroy; wl_signal_add(&dev->events.destroy, &kb->destroy);
	wlr_seat_set_keyboard(s->seat, wk);
	wl_list_insert(&s->keyboards, &kb->link);
	// A panel can map before the first keyboard is attached. Preserve any
	// layer-shell focus, otherwise deliver enter to the selected visible app.
	struct wlr_surface *surface = s->seat->keyboard_state.focused_surface;
	if (s->locked) { wc_lock_focus(s); return; }
	if (!surface && s->focused && s->focused->mapped && !s->focused->hidden && !s->desktop_visible)
		surface = s->focused->xdg_toplevel->base->surface;
	if (surface) wlr_seat_keyboard_notify_enter(s->seat, surface,
		wk->keycodes, wk->num_keycodes, &wk->modifiers);
}
static void apply_pointer(struct wlr_input_device *dev) {
	if (!wlr_input_device_is_libinput(dev)) return;
	struct libinput_device *li = wlr_libinput_get_device_handle(dev);
	/* Check every capability and preserve device defaults on reset. */
	if (libinput_device_config_accel_is_available(li))
		libinput_device_config_accel_set_speed(li, g_pointer_speed >= -100 ? g_pointer_speed / 100.0 : libinput_device_config_accel_get_default_speed(li));
	if (libinput_device_config_scroll_has_natural_scroll(li))
		libinput_device_config_scroll_set_natural_scroll_enabled(li, g_natural_scroll >= 0 ? g_natural_scroll : libinput_device_config_scroll_get_default_natural_scroll_enabled(li));
	if (libinput_device_config_left_handed_is_available(li))
		libinput_device_config_left_handed_set(li, g_left_handed >= 0 ? g_left_handed : libinput_device_config_left_handed_get_default(li));
	if (libinput_device_config_tap_get_finger_count(li) > 0)
		libinput_device_config_tap_set_enabled(li, g_tap_to_click >= 0 ? (enum libinput_config_tap_state)g_tap_to_click : libinput_device_config_tap_get_default_enabled(li));
	if (libinput_device_config_dwt_is_available(li))
		libinput_device_config_dwt_set_enabled(li, g_disable_while_typing >= 0 ? (enum libinput_config_dwt_state)g_disable_while_typing : libinput_device_config_dwt_get_default_enabled(li));
}
static void pointer_destroy(struct wl_listener *listener, void *data) {
	struct wc_pointer *pointer = wl_container_of(listener, pointer, destroy);
	wl_list_remove(&pointer->link); wl_list_remove(&pointer->destroy.link); free(pointer);
}
static void server_new_pointer(struct wc_server *s, struct wlr_input_device *dev) {
	struct wc_pointer *pointer = calloc(1, sizeof(*pointer));
	if (!pointer) return;
	pointer->device = dev; pointer->destroy.notify = pointer_destroy;
	wl_signal_add(&dev->events.destroy, &pointer->destroy);
	wl_list_insert(&s->pointers, &pointer->link);
	apply_pointer(dev);
	wlr_cursor_attach_input_device(s->cursor, dev);
}
static void server_new_input(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, new_input);
	struct wlr_input_device *dev = data;
	if (dev->type == WLR_INPUT_DEVICE_KEYBOARD) server_new_keyboard(s, dev);
	else if (dev->type == WLR_INPUT_DEVICE_POINTER) server_new_pointer(s, dev);
}
static void seat_cursor_req(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, request_cursor);
	struct wlr_seat_pointer_request_set_cursor_event *ev = data;
	if (ev->seat_client == s->seat->pointer_state.focused_client &&
	    (!s->locked || wc_lock_surface_allowed(s, s->seat->pointer_state.focused_surface)))
		wlr_cursor_set_surface(s->cursor, ev->surface, ev->hotspot_x, ev->hotspot_y);
}
static void seat_primary_req(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, request_set_primary_selection);
	struct wlr_seat_request_set_primary_selection_event *ev = data;
	if (!g_clipboard || s->locked) { if (ev->source) wlr_primary_selection_source_destroy(ev->source); return; }
	wlr_seat_set_primary_selection(s->seat, ev->source, ev->serial);
}
/* An app hands focus to a window it opened (a link, a folder, a second window).
 * Only tokens minted from real input on this seat count, so nothing can steal
 * focus on its own; the target's workspace is shown first. */
static void activation_req(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, request_activate);
	struct wlr_xdg_activation_v1_request_activate_event *ev = data;
	if (s->locked || !ev->token || ev->token->seat != s->seat || !ev->surface) return;
	struct wlr_xdg_toplevel *x = wlr_xdg_toplevel_try_from_wlr_surface(ev->surface);
	struct wc_toplevel *t = x ? wc_toplevel_of(x) : NULL;
	if (!t || !t->mapped) return;
	if (t->workspace != s->workspace) wc_workspace(s, t->workspace, false);
	t->hidden = false;
	wc_focus(t);
}
/* Video players and presentations keep the screen from locking, but only while
 * the inhibiting surface is actually on screen. */
static bool wc_idle_inhibited(struct wc_server *s) {
	if (!s->idle_inhibit || s->locked) return false;
	struct wlr_idle_inhibitor_v1 *inhibitor;
	wl_list_for_each(inhibitor, &s->idle_inhibit->inhibitors, link) {
		struct wlr_xdg_toplevel *x = wlr_xdg_toplevel_try_from_wlr_surface(wlr_surface_get_root_surface(inhibitor->surface));
		struct wc_toplevel *t = x ? wc_toplevel_of(x) : NULL;
		if (t && t->mapped && t->scene_tree->node.enabled) return true;
	}
	return false;
}
static void seat_selection_req(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, request_set_selection);
	struct wlr_seat_request_set_selection_event *ev = data;
	if (!g_clipboard || s->locked) { if (ev->source) wlr_data_source_destroy(ev->source); return; }
	wlr_seat_set_selection(s->seat, ev->source, ev->serial);
}
static struct wc_toplevel *tab_at(struct wc_server *s, double lx, double ly, int *tab_x) {
	if (ly < 0 || ly >= g_tab_h) return NULL;
	struct wc_toplevel *t; int idx = 0;
	wl_list_for_each(t, &s->toplevels, link) {
		if (!t->mapped || t->workspace != s->workspace || !t->tab_border || !t->tab_border->node.enabled) continue;
		int x = tabs_left() + TAB_GAP + idx * (g_tab_w + TAB_GAP);
		if (lx >= x && lx < x + g_tab_w) { *tab_x = x; return t; }
		idx++;
	}
	return NULL;
}
static void cursor_process(struct wc_server *s, uint32_t time) {
	if (s->drag.toplevel) {
		wc_update_drag(s, s->cursor->x, s->cursor->y);
		wc_retile(s);
		return;
	}
	if (!s->locked && s->grab_surface && s->seat->pointer_state.button_count &&
	    s->seat->pointer_state.focused_surface == s->grab_surface) {
		/* A drag that leaves the window stays with it until release (no stray releases). */
		wlr_seat_pointer_notify_motion(s->seat, time, s->cursor->x - s->grab_x, s->cursor->y - s->grab_y);
		return;
	}
	double sx, sy;
	struct wlr_scene_node *node = wlr_scene_node_at(s->locked ? &s->lock_tree->node : &s->scene->tree.node,
		s->cursor->x, s->cursor->y, &sx, &sy);
	struct wlr_scene_surface *surface = NULL;
	if (node && node->type == WLR_SCENE_NODE_BUFFER)
		surface = wlr_scene_surface_try_from_buffer(wlr_scene_buffer_from_node(node));
	if (surface && (!s->locked || wc_lock_surface_allowed(s, surface->surface))) {
		if (s->locked) {
			wlr_seat_pointer_enter(s->seat, surface->surface, sx, sy);
			wlr_seat_pointer_send_motion(s->seat, time, sx, sy);
		} else {
			wlr_seat_pointer_notify_enter(s->seat, surface->surface, sx, sy);
			wlr_seat_pointer_notify_motion(s->seat, time, sx, sy);
		}
	} else {
		wlr_seat_pointer_clear_focus(s->seat);
		wlr_cursor_set_xcursor(s->cursor, s->cursor_mgr, "default");
	}
}
static void cursor_motion(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, cursor_motion);
	wc_activity(s);
	struct wlr_pointer_motion_event *ev = data;
	wlr_cursor_move(s->cursor, &ev->pointer->base, ev->delta_x, ev->delta_y);
	cursor_process(s, ev->time_msec);
}
static void cursor_motion_abs(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, cursor_motion_absolute);
	wc_activity(s);
	struct wlr_pointer_motion_absolute_event *ev = data;
	wlr_cursor_warp_absolute(s->cursor, &ev->pointer->base, ev->x, ev->y);
	cursor_process(s, ev->time_msec);
}
static void cursor_button(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, cursor_button);
	struct wlr_pointer_button_event *ev = data;
	wc_activity(s);
	if (s->locked) {
		cursor_process(s, ev->time_msec);
		if (wc_lock_surface_allowed(s, s->seat->pointer_state.focused_surface))
			wlr_seat_pointer_send_button(s->seat, ev->time_msec, ev->button, ev->state);
		return;
	}
	if (s->drag.button && ev->state == WL_POINTER_BUTTON_STATE_RELEASED && ev->button == s->drag.button) {
		wc_cancel_drag(s);
		s->drag = (struct wc_drag){0};
		wc_retile(s);
		cursor_process(s, ev->time_msec);
		return;
	}
	if (s->drag.button) {
		return;
	}
	if (ev->state == WL_POINTER_BUTTON_STATE_RELEASED && s->swallow_button == ev->button) {
		s->swallow_button = 0; cursor_process(s, ev->time_msec); return;
	}
	if (ev->state == WL_POINTER_BUTTON_STATE_PRESSED) {
		struct wc_output *out = server_first_output(s);
		if (out && s->top->node.enabled) {
			struct wlr_box box; wlr_output_layout_get_box(s->output_layout, out->wlr_output, &box);
			int W, H; wlr_output_effective_resolution(out->wlr_output, &W, &H);
			double px = s->cursor->x - box.x, py = s->cursor->y - box.y;
			int left = cubes_left(W), unit = (int)(26 * g_accessibility.ui_scale);
			if (wc_more_press(s, out, W, H, px, py, ev->button)) { s->swallow_button = ev->button; return; }
			if (py >= 0 && py < g_tab_h && px >= left && px < left + unit * 4) {
				s->swallow_button = ev->button;
				wc_workspace(s, (unsigned)((px - left) / unit), false); return;
			}
			int tab_x = 0;
			struct wc_toplevel *t = tab_at(s, px, py, &tab_x);
			if (t) {
				s->swallow_button = ev->button;
				if (ev->button == BTN_MIDDLE || (ev->button == BTN_LEFT && px >= tab_x + g_tab_w - tab_close_w() - 6))
					wlr_xdg_toplevel_send_close(t->xdg_toplevel);
				else { wc_focus(t); cursor_process(s, ev->time_msec); }
				return;
			}
		}
		double sx, sy;
		struct wlr_scene_node *node = wlr_scene_node_at(&s->scene->tree.node, s->cursor->x, s->cursor->y, &sx, &sy);
		while (node && !node->data) node = node->parent ? &node->parent->node : NULL;
		if (node && node->data) {
			struct wc_toplevel *t = node->data;
			wc_focus(t);
			struct wlr_keyboard *kb = wlr_seat_get_keyboard(s->seat);
			if (kb && (wlr_keyboard_get_modifiers(kb) & WLR_MODIFIER_ALT) &&
			    (ev->button == BTN_LEFT || ev->button == BTN_RIGHT) &&
			    !s->seat->pointer_state.button_count && wc_start_drag(s, t, ev->button)) return;
		}
	}
	cursor_process(s, ev->time_msec);
	wlr_seat_pointer_notify_button(s->seat, ev->time_msec, ev->button, ev->state);
	if (ev->state == WL_POINTER_BUTTON_STATE_PRESSED && s->seat->pointer_state.button_count == 1 &&
	    s->seat->pointer_state.focused_surface) {
		s->grab_surface = s->seat->pointer_state.focused_surface;
		s->grab_x = s->cursor->x - s->seat->pointer_state.sx;
		s->grab_y = s->cursor->y - s->seat->pointer_state.sy;
	} else if (!s->seat->pointer_state.button_count) {
		s->grab_surface = NULL;
		cursor_process(s, ev->time_msec); // re-pick the surface under the cursor after the grab
	}
}
static void cursor_axis(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, cursor_axis);
	wc_activity(s);
	if (s->locked && !wc_lock_surface_allowed(s, s->seat->pointer_state.focused_surface)) return;
	if (s->drag.toplevel) return;
	struct wlr_pointer_axis_event *ev = data;
	struct wc_output *po = server_first_output(s);
	if (po && po->panel_open && ev->orientation == WL_POINTER_AXIS_VERTICAL_SCROLL) {
		struct wlr_box box; int W, H;
		wlr_output_layout_get_box(s->output_layout, po->wlr_output, &box);
		int n = wc_count_tabs(s);
		if (wc_out_size(po, &W, &H)) {
			struct wc_panel_geom g = wc_panel_geom(W, H, g_tab_w, g_tab_h, po->more_x, n);
			if (wc_panel_contains(&g, s->cursor->x - box.x, s->cursor->y - box.y)) {
				po->panel_scroll = wc_panel_clamp_scroll(&g, n, po->panel_scroll + (ev->delta > 0 ? 1 : -1));
				wc_panel_refresh(s);
				return;
			}
		}
	}
	if (s->locked) wlr_seat_pointer_send_axis(s->seat, ev->time_msec, ev->orientation,
		ev->delta, ev->delta_discrete, ev->source, ev->relative_direction);
	else wlr_seat_pointer_notify_axis(s->seat, ev->time_msec, ev->orientation,
		ev->delta, ev->delta_discrete, ev->source, ev->relative_direction);
}
static void cursor_frame(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, cursor_frame);
	if (s->locked && !wc_lock_surface_allowed(s, s->seat->pointer_state.focused_surface)) return;
	if (s->locked) wlr_seat_pointer_send_frame(s->seat);
	else wlr_seat_pointer_notify_frame(s->seat);
}
static void output_frame(struct wl_listener *l, void *data) {
	struct wc_output *o = wl_container_of(l, o, frame);
	struct wlr_scene_output *so = wlr_scene_get_scene_output(o->server->scene, o->wlr_output);
	if (!so) return;
	if (o->server->locked && !o->lock_frame_presented && !o->lock_frame_submitted) {
		o->lock_commit_seq = o->wlr_output->commit_seq + 1;
		o->lock_frame_submitted = true;
	}
	if (!wlr_scene_output_commit(so, NULL)) { o->lock_frame_submitted = false; return; }
	struct timespec now;
	clock_gettime(CLOCK_MONOTONIC, &now);
	wlr_scene_output_send_frame_done(so, &now);
}
static void output_present(struct wl_listener *l, void *data) {
	struct wc_output *o = wl_container_of(l, o, present);
	struct wlr_output_event_present *event = data;
	if (!o->server->locked || !o->lock_frame_submitted || !event->presented ||
	    (int32_t)(event->commit_seq - o->lock_commit_seq) < 0) return;
	o->lock_frame_presented = true;
	wc_lock_maybe_ack(o->server);
}
static void output_req_state(struct wl_listener *l, void *data) {
	struct wc_output *o = wl_container_of(l, o, request_state);
	wc_cancel_drag(o->server);
	const struct wlr_output_event_request_state *ev = data;
	wlr_output_commit_state(o->wlr_output, ev->state);
	wc_output_refresh(o);
	output_manager_update(o->server);
	wc_retile(o->server);
}
static void output_destroy(struct wl_listener *l, void *data) {
	struct wc_output *o = wl_container_of(l, o, destroy);
	struct wc_server *s = o->server;
	wc_cancel_drag(s);
	wl_list_remove(&o->frame.link); wl_list_remove(&o->request_state.link);
	wl_list_remove(&o->present.link);
	wl_list_remove(&o->destroy.link); wl_list_remove(&o->link);
	if (o->lock_bg) wlr_scene_node_destroy(&o->lock_bg->node);
	free(o);
	wc_lock_maybe_ack(s);
	if (!s->shutting_down) output_manager_update(s);
}
static void build_bg_grid(struct wc_output *o, int W, int H) {
	struct wlr_scene_node *n, *tmp;
	wl_list_for_each_safe(n, tmp, &o->bg_tree->children, link) wlr_scene_node_destroy(n);
	o->bg_flat = wlr_scene_rect_create(o->bg_tree, W, H, g_bg);
	wlr_scene_node_set_position(&o->bg_flat->node, 0, 0);
	for (int x = 0; x <= W; x += g_grid) {
		struct wlr_scene_rect *r = wlr_scene_rect_create(o->bg_tree, 1, H, g_grid_c);
		wlr_scene_node_set_position(&r->node, x, 0);
	}
	for (int y = 0; y <= H; y += g_grid) {
		struct wlr_scene_rect *r = wlr_scene_rect_create(o->bg_tree, W, 1, g_grid_c);
		wlr_scene_node_set_position(&r->node, 0, y);
	}
}
// Saved per-output settings in ~/.config/whitecube/outputs, one line per output:
//   NAME enabled=1 mode=1920x1080@60000 scale=1.25 transform=0 x=0 y=0
// cube-settings writes it after a successful live apply; it is read when an output appears.
struct wc_output_saved {
	bool found, enabled, has_mode, has_pos;
	int w, h, refresh, transform, x, y;
	float scale;
};
static struct wc_output_saved saved_output(const char *name) {
	struct wc_output_saved r = {.enabled = true, .transform = -1};
	const char *home = getenv("HOME");
	if (!home || !name) return r;
	char path[512]; snprintf(path, sizeof(path), "%s/.config/whitecube/outputs", home);
	FILE *f = fopen(path, "r");
	if (!f) return r;
	char line[512];
	while (fgets(line, sizeof(line), f)) {
		char *save = NULL, *tok = strtok_r(line, " \t\r\n", &save);
		if (!tok || tok[0] == '#' || strcmp(tok, name)) continue;
		r.found = true;
		while ((tok = strtok_r(NULL, " \t\r\n", &save))) {
			int a, b, c; float fv;
			if (sscanf(tok, "enabled=%d", &a) == 1) r.enabled = a != 0;
			else if (sscanf(tok, "mode=%dx%d@%d", &a, &b, &c) == 3 && a > 0 && b > 0 && c >= 0) { r.has_mode = true; r.w = a; r.h = b; r.refresh = c; }
			else if (sscanf(tok, "scale=%f", &fv) == 1 && fv >= 0.5f && fv <= 4.0f) r.scale = fv;
			else if (sscanf(tok, "transform=%d", &a) == 1 && a >= 0 && a <= 7) r.transform = a;
			else if (sscanf(tok, "x=%d", &a) == 1) { r.x = a; r.has_pos = true; }
			else if (sscanf(tok, "y=%d", &a) == 1) { r.y = a; r.has_pos = true; }
		}
	}
	fclose(f);
	return r;
}
static struct wlr_output_mode *find_mode(struct wlr_output *wo, int w, int h, int refresh) {
	struct wlr_output_mode *m, *best = NULL;
	wl_list_for_each(m, &wo->modes, link) {
		if (m->width != w || m->height != h) continue;
		if (!best || abs(m->refresh - refresh) < abs(best->refresh - refresh)) best = m;
	}
	return best;
}
static void wc_output_place(struct wc_server *s, struct wc_output *o, bool enabled, int x, int y) {
	struct wlr_output *wo = o->wlr_output;
	if (!enabled) { wlr_output_layout_remove(s->output_layout, wo); return; }
	struct wlr_output_layout_output *lo = wlr_output_layout_add(s->output_layout, wo, x, y);
	if (!lo) return;
	struct wlr_scene_output *so = wlr_scene_get_scene_output(s->scene, wo);
	if (!so) {
		so = wlr_scene_output_create(s->scene, wo);
		if (so) wlr_scene_output_layout_add_output(s->scene_layout, lo, so);
	} else wlr_scene_output_set_position(so, x, y);
}
// Publish the current state to wlr-output-management clients (cube-settings, wlr-randr).
static void output_manager_update(struct wc_server *s) {
	if (!s->output_manager) return;
	struct wlr_output_configuration_v1 *config = wlr_output_configuration_v1_create();
	if (!config) return;
	struct wc_output *o;
	wl_list_for_each(o, &s->outputs, link) {
		struct wlr_output_configuration_head_v1 *head = wlr_output_configuration_head_v1_create(config, o->wlr_output);
		if (!head) continue;
		struct wlr_box box; wlr_output_layout_get_box(s->output_layout, o->wlr_output, &box);
		head->state.enabled = o->wlr_output->enabled && !wlr_box_empty(&box);
		head->state.x = box.x; head->state.y = box.y;
	}
	wlr_output_manager_v1_set_configuration(s->output_manager, config);
}
static void output_manager_apply_or_test(struct wc_server *s, struct wlr_output_configuration_v1 *config, bool test) {
	if (s->locked) {
		wlr_output_configuration_v1_send_failed(config);
		wlr_output_configuration_v1_destroy(config);
		return;
	}
	if (!test) wc_cancel_drag(s);
	struct wlr_output_configuration_head_v1 *head;
	bool any_enabled = false, ok = true;
	wl_list_for_each(head, &config->heads, link) any_enabled |= head->state.enabled;
	if (!any_enabled) ok = false; // never turn every screen off
	wl_list_for_each(head, &config->heads, link) {
		if (!ok) break;
		struct wlr_output_state st; wlr_output_state_init(&st);
		wlr_output_head_v1_state_apply(&head->state, &st);
		ok = test ? wlr_output_test_state(head->state.output, &st) : wlr_output_commit_state(head->state.output, &st);
		wlr_output_state_finish(&st);
	}
	if (ok && !test) {
		wl_list_for_each(head, &config->heads, link) {
			struct wc_output *o;
			wl_list_for_each(o, &s->outputs, link) {
				if (o->wlr_output != head->state.output) continue;
				wc_output_place(s, o, head->state.enabled, head->state.x, head->state.y);
				if (head->state.enabled) wc_output_refresh(o);
			}
		}
	}
	if (ok) wlr_output_configuration_v1_send_succeeded(config);
	else wlr_output_configuration_v1_send_failed(config);
	wlr_output_configuration_v1_destroy(config);
	if (!test) { output_manager_update(s); wc_retile(s); }
}
static void output_manager_apply(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, output_manager_apply);
	output_manager_apply_or_test(s, data, false);
}
static void output_manager_test(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, output_manager_test);
	output_manager_apply_or_test(s, data, true);
}
static void server_new_output(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, new_output);
	struct wlr_output *wo = data;
	wlr_output_init_render(wo, s->allocator, s->renderer);
	struct wc_output_saved cfg = saved_output(wo->name);
	// The first screen always comes on, whatever was saved.
	if (!cfg.enabled && wl_list_empty(&s->outputs)) cfg.enabled = true;
	struct wlr_output_state st; wlr_output_state_init(&st);
	wlr_output_state_set_enabled(&st, cfg.enabled);
	struct wlr_output_mode *m = cfg.has_mode ? find_mode(wo, cfg.w, cfg.h, cfg.refresh) : NULL;
	if (m) wlr_output_state_set_mode(&st, m);
	else if (cfg.has_mode && wl_list_empty(&wo->modes)) wlr_output_state_set_custom_mode(&st, cfg.w, cfg.h, cfg.refresh);
	else if ((m = wlr_output_preferred_mode(wo))) wlr_output_state_set_mode(&st, m);
	if (cfg.scale > 0) wlr_output_state_set_scale(&st, cfg.scale);
	if (cfg.transform >= 0) wlr_output_state_set_transform(&st, (enum wl_output_transform)cfg.transform);
	if (!wlr_output_commit_state(wo, &st) && cfg.found) {
		wlr_log(WLR_ERROR, "saved settings for %s rejected; using preferred mode", wo->name);
		wlr_output_state_finish(&st); wlr_output_state_init(&st);
		wlr_output_state_set_enabled(&st, true); cfg.enabled = true; cfg.has_pos = false;
		if ((m = wlr_output_preferred_mode(wo))) wlr_output_state_set_mode(&st, m);
		wlr_output_commit_state(wo, &st);
	}
	wlr_output_state_finish(&st);
	struct wc_output *o = calloc(1, sizeof(*o));
	o->server = s; o->wlr_output = wo;
	o->frame.notify = output_frame; wl_signal_add(&wo->events.frame, &o->frame);
	o->present.notify = output_present; wl_signal_add(&wo->events.present, &o->present);
	o->request_state.notify = output_req_state; wl_signal_add(&wo->events.request_state, &o->request_state);
	o->destroy.notify = output_destroy; wl_signal_add(&wo->events.destroy, &o->destroy);
	wl_list_insert(&s->outputs, &o->link);
	if (cfg.has_pos || !cfg.enabled) wc_output_place(s, o, cfg.enabled, cfg.x, cfg.y);
	else {
		struct wlr_output_layout_output *lo = wlr_output_layout_add_auto(s->output_layout, wo);
		struct wlr_scene_output *so = wlr_scene_output_create(s->scene, wo);
		wlr_scene_output_layout_add_output(s->scene_layout, lo, so);
	}
	o->bg_tree = wlr_scene_tree_create(s->background);
	wlr_scene_node_lower_to_bottom(&o->bg_tree->node);
	o->tab_tree = wlr_scene_tree_create(s->top);
	o->lock_bg = wlr_scene_rect_create(s->lock_tree, 1, 1, (float[4]){0, 0, 0, 1});
	wlr_scene_node_lower_to_bottom(&o->lock_bg->node);
	o->tab_bg = wlr_scene_rect_create(o->tab_tree, 1, g_tab_h, g_tabbar);
	wlr_scene_node_set_position(&o->tab_bg->node, 0, 0);
	for (unsigned i = 0; i < WORKSPACES; i++)
		o->workspace_cube[i] = wlr_scene_rect_create(o->tab_tree, 18, 18, g_grid_c);
	o->more_border = wlr_scene_rect_create(o->tab_tree, 1, 1, g_border);
	o->more_cube = wlr_scene_rect_create(o->tab_tree, 1, 1, g_unfocused);
	o->more_label = wlr_scene_buffer_create(o->tab_tree, NULL);
	o->panel_buf = wlr_scene_buffer_create(o->tab_tree, NULL);
	wlr_scene_node_set_enabled(&o->more_border->node, false);
	wlr_scene_node_set_enabled(&o->more_cube->node, false);
	wlr_scene_node_set_enabled(&o->more_label->node, false);
	wlr_scene_node_set_enabled(&o->panel_buf->node, false);
	wlr_scene_node_raise_to_top(&o->tab_tree->node);
	wc_output_refresh(o);
	output_manager_update(s);
	wc_retile(s);
}
static void wc_restore_window(struct wc_toplevel *t) {
	/* A saved placement must never hide a newly opened window or send it to
	 * another workspace. Restore its geometry and presentation on the current
	 * workspace, even if the user switched while the client was starting. */
	t->workspace = t->server->workspace;
	t->hidden = false;
	if (is_dialog(t)) { t->saved_index = -1; return; }
	t->saved_index = g_remember_layout ? wc_state_match(&t->server->layout,
		t->xdg_toplevel->app_id, t->xdg_toplevel->title, t->workspace) : -1;
	if (t->saved_index < 0) return;
	struct wc_saved_window *saved = &t->server->layout.windows[t->saved_index];
	saved->claimed = true;
	t->fullscreen = saved->fullscreen; t->maximized = saved->maximized;
	t->layout_x = saved->x; t->layout_y = saved->y;
	t->layout_w = saved->w; t->layout_h = saved->h;
}

static void t_map(struct wl_listener *l, void *data) {
	struct wc_toplevel *t = wl_container_of(l, t, map);
	t->mapped = true;
	struct wc_output *out = server_first_output(t->server);
	if (out && !t->tab_border) {
		t->output = out;
		t->tab_border = wlr_scene_rect_create(out->tab_tree, g_tab_w, g_tab_h - 12, g_border);
		t->tab_cube = wlr_scene_rect_create(out->tab_tree, g_tab_w - 4, g_tab_h - 16, C_WHITE);
		t->tab_label = wlr_scene_buffer_create(out->tab_tree, NULL);
		t->label_w = 0;
	}
	wl_list_remove(&t->link);
	wl_list_insert(&t->server->toplevels, &t->link);
	wc_restore_window(t);
	if (t->saved_index >= 0) {
		wlr_xdg_toplevel_set_fullscreen(t->xdg_toplevel, t->fullscreen);
		wlr_xdg_toplevel_set_maximized(t->xdg_toplevel, t->maximized);
	}
	if (!is_scripts(t)) t->server->saved_normal[t->workspace] = t;
	wc_focus(t);
	wc_retile(t->server);
}
static void t_unmap(struct wl_listener *l, void *data) {
	struct wc_toplevel *t = wl_container_of(l, t, unmap);
	t->mapped = false;
	wlr_scene_node_set_enabled(&t->scene_tree->node, false);
	forget_toplevel(t);
	if (t->server->grab_surface == t->xdg_toplevel->base->surface) t->server->grab_surface = NULL;
	struct wc_toplevel *parent = wc_toplevel_of(t->xdg_toplevel->parent);
	if (t->server->focused == t) {
		if (parent && parent->mapped && parent->workspace == t->server->workspace) {
			t->server->focused = NULL; wc_focus(parent);
		} else wc_focus_available(t->server);
	}
	wc_retile(t->server);
}
static void t_commit(struct wl_listener *l, void *data) {
	struct wc_toplevel *t = wl_container_of(l, t, commit);
	if (t->xdg_toplevel->base->initial_commit) {
		struct wc_output *out = server_first_output(t->server);
		int W = out && out->wlr_output->width > 0 ? out->wlr_output->width : 1280;
		int H = out && out->wlr_output->height > 0 ? out->wlr_output->height : 800;
		/* 0x0 lets a dialog pick its own size; retile centres and caps it. */
		if (is_dialog(t)) wlr_xdg_toplevel_set_size(t->xdg_toplevel, 0, 0);
		else wlr_xdg_toplevel_set_size(t->xdg_toplevel, W, H - g_tab_h);
		/* No minimize and no window menu here, so clients hide those buttons. */
		wlr_xdg_toplevel_set_wm_capabilities(t->xdg_toplevel,
			WLR_XDG_TOPLEVEL_WM_CAPABILITIES_MAXIMIZE | WLR_XDG_TOPLEVEL_WM_CAPABILITIES_FULLSCREEN);
		wlr_xdg_toplevel_set_bounds(t->xdg_toplevel, W, H - g_tab_h);
	}
}
static void t_destroy(struct wl_listener *l, void *data) {
	struct wc_toplevel *t = wl_container_of(l, t, destroy);
	if (t->tab_label) wlr_scene_node_destroy(&t->tab_label->node);
	if (t->tab_cube) wlr_scene_node_destroy(&t->tab_cube->node);
	if (t->tab_border) wlr_scene_node_destroy(&t->tab_border->node);
	forget_toplevel(t);
	bool was_focused = t->server->focused == t;
	if (was_focused) t->server->focused = NULL;
	wl_list_remove(&t->link);
	wl_list_remove(&t->map.link); wl_list_remove(&t->unmap.link);
	wl_list_remove(&t->commit.link); wl_list_remove(&t->destroy.link);
	wl_list_remove(&t->request_move.link); wl_list_remove(&t->request_resize.link);
	wl_list_remove(&t->request_maximize.link); wl_list_remove(&t->request_fullscreen.link);
	wl_list_remove(&t->set_title.link);
	if (was_focused) wc_focus_available(t->server); else wc_retile(t->server);
	free(t);
}
static void t_set_title(struct wl_listener *l, void *data) {
	struct wc_toplevel *t = wl_container_of(l, t, set_title);
	wc_tab_label(t, t->label_w, t->label_h, t == t->server->focused && !t->hidden);
}
static void t_req_move(struct wl_listener *l, void *data) {}
static void t_req_resize(struct wl_listener *l, void *data) {}
static void t_req_max(struct wl_listener *l, void *data) {
	struct wc_toplevel *t = wl_container_of(l, t, request_maximize);
	if (t->server->drag.toplevel == t) wc_cancel_drag(t->server);
	t->maximized = t->xdg_toplevel->requested.maximized;
	wlr_xdg_toplevel_set_maximized(t->xdg_toplevel, t->maximized);
	wc_retile(t->server);
}
static void t_req_full(struct wl_listener *l, void *data) {
	struct wc_toplevel *t = wl_container_of(l, t, request_fullscreen);
	if (t->server->drag.toplevel == t) wc_cancel_drag(t->server);
	t->fullscreen = t->xdg_toplevel->requested.fullscreen;
	wlr_xdg_toplevel_set_fullscreen(t->xdg_toplevel, t->fullscreen);
	wc_retile(t->server);
}
static void server_new_toplevel(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, new_xdg_toplevel);
	struct wlr_xdg_toplevel *x = data;
	struct wc_toplevel *t = calloc(1, sizeof(*t));
	t->server = s; t->xdg_toplevel = x; t->workspace = s->workspace; t->saved_index = -1;
	t->scene_tree = wlr_scene_xdg_surface_create(s->windows, x->base);
	t->scene_tree->node.data = t; x->base->data = t->scene_tree;
	t->map.notify = t_map; wl_signal_add(&x->base->surface->events.map, &t->map);
	t->unmap.notify = t_unmap; wl_signal_add(&x->base->surface->events.unmap, &t->unmap);
	t->commit.notify = t_commit; wl_signal_add(&x->base->surface->events.commit, &t->commit);
	t->destroy.notify = t_destroy; wl_signal_add(&x->events.destroy, &t->destroy);
	t->request_move.notify = t_req_move; wl_signal_add(&x->events.request_move, &t->request_move);
	t->request_resize.notify = t_req_resize; wl_signal_add(&x->events.request_resize, &t->request_resize);
	t->request_maximize.notify = t_req_max; wl_signal_add(&x->events.request_maximize, &t->request_maximize);
	t->request_fullscreen.notify = t_req_full; wl_signal_add(&x->events.request_fullscreen, &t->request_fullscreen);
	t->set_title.notify = t_set_title; wl_signal_add(&x->events.set_title, &t->set_title);
	wl_list_insert(&s->toplevels, &t->link);
}
struct wc_popup { struct wlr_xdg_popup *x; struct wc_server *server; struct wl_listener commit, destroy; };
/* The scene tree a popup belongs under: its xdg parent's, or a layer surface's. */
static struct wlr_scene_tree *wc_popup_parent_tree(struct wlr_surface *parent) {
	if (!parent) return NULL;
	struct wlr_xdg_surface *xdg = wlr_xdg_surface_try_from_wlr_surface(parent);
	if (xdg) return xdg->data;
	struct wlr_layer_surface_v1 *layer = wlr_layer_surface_v1_try_from_wlr_surface(parent);
	return layer ? layer->data : NULL;
}
static void wc_popup_unconstrain(struct wc_popup *p) {
	struct wc_output *out = server_first_output(p->server);
	struct wlr_scene_tree *parent = wc_popup_parent_tree(p->x->parent);
	if (!out || !parent) return;
	/* Find the root surface (toplevel or layer) and its on-screen origin. */
	struct wlr_surface *root = p->x->parent;
	struct wlr_xdg_surface *xdg;
	while ((xdg = wlr_xdg_surface_try_from_wlr_surface(root)) && xdg->role == WLR_XDG_SURFACE_ROLE_POPUP && xdg->popup->parent)
		root = xdg->popup->parent;
	struct wlr_scene_tree *tree = wc_popup_parent_tree(root);
	int lx = 0, ly = 0;
	if (!tree || !wlr_scene_node_coords(&tree->node, &lx, &ly)) return;
	if (xdg && xdg->role == WLR_XDG_SURFACE_ROLE_TOPLEVEL) {
		struct wlr_box geo; wlr_xdg_surface_get_geometry(xdg, &geo); lx -= geo.x; ly -= geo.y;
	}
	struct wlr_box box; wlr_output_layout_get_box(p->server->output_layout, out->wlr_output, &box);
	box.x -= lx; box.y -= ly;
	wlr_xdg_popup_unconstrain_from_box(p->x, &box);
}
static void p_commit(struct wl_listener *l, void *data) {
	struct wc_popup *p = wl_container_of(l, p, commit);
	if (!p->x->base->initial_commit) return;
	if (!p->x->base->data) {
		struct wlr_scene_tree *parent = wc_popup_parent_tree(p->x->parent);
		if (!parent) { wlr_xdg_popup_destroy(p->x); return; } // no parent: nothing to show it in
		p->x->base->data = wlr_scene_xdg_surface_create(parent, p->x->base);
	}
	wc_popup_unconstrain(p);
	wlr_xdg_surface_schedule_configure(p->x->base);
}
static void p_destroy(struct wl_listener *l, void *data) {
	struct wc_popup *p = wl_container_of(l, p, destroy);
	wl_list_remove(&p->commit.link); wl_list_remove(&p->destroy.link); free(p);
}
static void server_new_popup(struct wl_listener *l, void *data) {
	struct wlr_xdg_popup *x = data;
	struct wc_popup *p = calloc(1, sizeof(*p));
	if (!p) return;
	p->x = x; p->server = wl_container_of(l, p->server, new_xdg_popup);
	/* Layer-shell popups get their parent only after creation (it used to abort here). */
	struct wlr_scene_tree *pt = wc_popup_parent_tree(x->parent);
	if (pt) x->base->data = wlr_scene_xdg_surface_create(pt, x->base);
	p->commit.notify = p_commit; wl_signal_add(&x->base->surface->events.commit, &p->commit);
	p->destroy.notify = p_destroy; wl_signal_add(&x->events.destroy, &p->destroy);
}
static void layer_map(struct wl_listener *l, void *data) {
	struct wc_layer_surface *s = wl_container_of(l, s, map);
	if (s->server->locked) return;
	if (s->layer_surface->current.keyboard_interactive != ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE) {
		struct wlr_keyboard *kb = wlr_seat_get_keyboard(s->server->seat);
		if (kb) wlr_seat_keyboard_notify_enter(s->server->seat, s->layer_surface->surface,
			kb->keycodes, kb->num_keycodes, &kb->modifiers);
	}
}
static void layer_unmap(struct wl_listener *l, void *data) {
	struct wc_layer_surface *s = wl_container_of(l, s, unmap);
	if (s->server->seat->keyboard_state.focused_surface == s->layer_surface->surface) {
		if (s->server->focused && !s->server->desktop_visible) wc_focus(s->server->focused);
		else wlr_seat_keyboard_clear_focus(s->server->seat);
	}
}
static void layer_destroy(struct wl_listener *l, void *data) {
	struct wc_layer_surface *s = wl_container_of(l, s, destroy);
	wl_list_remove(&s->map.link); wl_list_remove(&s->unmap.link);
	wl_list_remove(&s->destroy.link); wl_list_remove(&s->commit.link); free(s);
}
static void layer_commit(struct wl_listener *l, void *data) {
	struct wc_layer_surface *s = wl_container_of(l, s, commit);
	if (s->layer_surface->initial_commit || s->layer_surface->current.committed) {
		int w, h; wlr_output_effective_resolution(s->output->wlr_output, &w, &h);
		struct wlr_box full = {0, 0, w, h}, usable = full;
		wlr_scene_layer_surface_v1_configure(s->scene, &full, &usable);
	}
}
/* Only the active lock's surface tree may receive input while locked. */
static bool wc_lock_surface_allowed(struct wc_server *s, struct wlr_surface *surface) {
	if (!s->lock || !surface) return false;
	struct wlr_surface *root = wlr_surface_get_root_surface(surface);
	struct wc_lock_surface *lock_surface;
	wl_list_for_each(lock_surface, &s->lock_surfaces, link)
		if (lock_surface->surface->surface == root && root->mapped) return true;
	return false;
}
static void wc_lock_focus(struct wc_server *s) {
	struct wlr_keyboard *kb = wlr_seat_get_keyboard(s->seat);
	struct wc_lock_surface *surface;
	wl_list_for_each(surface, &s->lock_surfaces, link) {
		if (!s->lock || !surface->surface->surface->mapped) continue;
		if (kb) wlr_seat_keyboard_enter(s->seat, surface->surface->surface,
			kb->keycodes, kb->num_keycodes, &kb->modifiers);
		return;
	}
	wlr_seat_keyboard_clear_focus(s->seat);
}
static void wc_lock_maybe_ack(struct wc_server *s) {
	if (!s->lock || s->lock->locked_sent) return;
	struct wc_output *out;
	wl_list_for_each(out, &s->outputs, link)
		if (out->wlr_output->enabled && !out->lock_frame_presented) return;
	wlr_session_lock_v1_send_locked(s->lock);
}
static void wc_lock_refresh(struct wc_server *s) {
	if (!s->lock_tree) return;
	struct wc_output *out;
	wl_list_for_each(out, &s->outputs, link) {
		if (!out->lock_bg) continue;
		int width, height;
		wlr_output_effective_resolution(out->wlr_output, &width, &height);
		struct wlr_box box; wlr_output_layout_get_box(s->output_layout, out->wlr_output, &box);
		wlr_scene_rect_set_size(out->lock_bg, width > 0 ? width : 1, height > 0 ? height : 1);
		wlr_scene_node_set_position(&out->lock_bg->node, box.x, box.y);
		wlr_scene_node_set_enabled(&out->lock_bg->node, out->wlr_output->enabled);
	}
	struct wc_lock_surface *surface;
	wl_list_for_each(surface, &s->lock_surfaces, link) {
		int width, height;
		wlr_output_effective_resolution(surface->surface->output, &width, &height);
		struct wlr_box box; wlr_output_layout_get_box(s->output_layout, surface->surface->output, &box);
		wlr_scene_node_set_position(&surface->tree->node, box.x, box.y);
		if (width > 0 && height > 0 &&
		    (surface->surface->pending.width != (uint32_t)width || surface->surface->pending.height != (uint32_t)height))
			wlr_session_lock_surface_v1_configure(surface->surface, width, height);
	}
}
static void lock_surface_map(struct wl_listener *l, void *data) {
	struct wc_lock_surface *surface = wl_container_of(l, surface, map);
	wc_lock_focus(surface->server);
}
static void lock_surface_destroy(struct wl_listener *l, void *data) {
	struct wc_lock_surface *surface = wl_container_of(l, surface, destroy);
	struct wc_server *s = surface->server;
	wl_list_remove(&surface->map.link); wl_list_remove(&surface->destroy.link);
	wl_list_remove(&surface->link);
	wlr_scene_node_destroy(&surface->tree->node);
	free(surface);
	if (s->locked) wc_lock_focus(s);
}
static void lock_new_surface(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, lock_new_surface);
	struct wlr_session_lock_surface_v1 *surface = data;
	struct wc_lock_surface *obj = calloc(1, sizeof(*obj));
	if (!obj) { wl_resource_post_no_memory(surface->resource); return; }
	obj->server = s; obj->surface = surface;
	obj->tree = wlr_scene_tree_create(s->lock_tree);
	if (!obj->tree || !wlr_scene_subsurface_tree_create(obj->tree, surface->surface)) {
		if (obj->tree) wlr_scene_node_destroy(&obj->tree->node);
		free(obj); wl_resource_post_no_memory(surface->resource); return;
	}
	obj->map.notify = lock_surface_map; wl_signal_add(&surface->surface->events.map, &obj->map);
	obj->destroy.notify = lock_surface_destroy; wl_signal_add(&surface->events.destroy, &obj->destroy);
	wl_list_insert(&s->lock_surfaces, &obj->link);
	wc_lock_refresh(s);
}
static void lock_unlock(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, lock_unlock);
	s->locked = false;
	wlr_seat_keyboard_clear_focus(s->seat);
	wlr_seat_pointer_clear_focus(s->seat);
	wlr_scene_node_set_enabled(&s->lock_tree->node, false);
	wlr_scene_node_set_enabled(&s->background->node, true);
	wlr_scene_node_set_enabled(&s->bottom->node, true);
	wlr_scene_node_set_enabled(&s->windows->node, true);
	wlr_scene_node_set_enabled(&s->overlay->node, true);
	wc_activity(s);
	if (s->focused && !s->desktop_visible) wc_focus(s->focused);
	wc_retile(s);
}
static void lock_destroy(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, lock_destroy);
	wl_list_remove(&s->lock_new_surface.link); wl_list_remove(&s->lock_unlock.link);
	wl_list_remove(&s->lock_destroy.link);
	s->lock = NULL;
	/* A crashed locker must leave the desktop blank and input blocked. */
	if (s->locked) {
		wlr_seat_keyboard_clear_focus(s->seat); wlr_seat_pointer_clear_focus(s->seat);
		wlr_log(WLR_ERROR, "screen locker disconnected; session remains locked");
	}
}
static void server_new_lock(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, new_lock);
	struct wlr_session_lock_v1 *lock = data;
	if (s->lock) { wlr_session_lock_v1_destroy(lock); return; }
	s->lock = lock; s->locked = true;
	wc_cancel_drag(s); s->drag = (struct wc_drag){0};
	wlr_seat_keyboard_end_grab(s->seat); wlr_seat_pointer_end_grab(s->seat);
	wlr_seat_keyboard_clear_focus(s->seat); wlr_seat_pointer_clear_focus(s->seat);
	if (g_clear_clipboard_on_lock || !g_clipboard)
		wlr_seat_set_selection(s->seat, NULL, wl_display_next_serial(s->wl_display));
	struct wc_keyboard *kb;
	wl_list_for_each(kb, &s->keyboards, link) kb->super_alone = false;
	wlr_cursor_set_xcursor(s->cursor, s->cursor_mgr, "default");
	wlr_scene_node_set_enabled(&s->background->node, false);
	wlr_scene_node_set_enabled(&s->bottom->node, false);
	wlr_scene_node_set_enabled(&s->windows->node, false);
	wlr_scene_node_set_enabled(&s->top->node, false);
	wlr_scene_node_set_enabled(&s->overlay->node, false);
	wlr_scene_node_set_enabled(&s->lock_tree->node, true);
	wlr_scene_node_raise_to_top(&s->lock_tree->node);
	s->lock_new_surface.notify = lock_new_surface; wl_signal_add(&lock->events.new_surface, &s->lock_new_surface);
	s->lock_unlock.notify = lock_unlock; wl_signal_add(&lock->events.unlock, &s->lock_unlock);
	s->lock_destroy.notify = lock_destroy; wl_signal_add(&lock->events.destroy, &s->lock_destroy);
	wc_lock_refresh(s);
	struct wc_output *out;
	wl_list_for_each(out, &s->outputs, link) {
		out->lock_frame_submitted = false; out->lock_frame_presented = false;
		struct wlr_scene_output *scene_output = wlr_scene_get_scene_output(s->scene, out->wlr_output);
		if (scene_output) wlr_damage_ring_add_whole(&scene_output->damage_ring);
		wlr_output_schedule_frame(out->wlr_output);
	}
	fullscreen_hint(true);
	wc_lock_maybe_ack(s);
}
static void server_new_layer(struct wl_listener *l, void *data) {
	struct wc_server *s = wl_container_of(l, s, new_layer_surface);
	struct wlr_layer_surface_v1 *ls = data;
	struct wc_output *out = NULL, *o;
	wl_list_for_each(o, &s->outputs, link) if (o->wlr_output == ls->output) { out = o; break; }
	if (!out) out = server_first_output(s);
	if (!out) return;
	ls->output = out->wlr_output;
	struct wc_layer_surface *obj = calloc(1, sizeof(*obj));
	obj->server = s; obj->layer_surface = ls; obj->output = out;
	struct wlr_scene_tree *parent = s->top;
	switch (ls->pending.layer) {
	case ZWLR_LAYER_SHELL_V1_LAYER_BACKGROUND: parent = s->background; break;
	case ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM: parent = s->bottom; break;
	case ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY: parent = s->overlay; break;
	default: break;
	}
	obj->scene = wlr_scene_layer_surface_v1_create(parent, ls);
	if (obj->scene) ls->data = obj->scene->tree; // popups of this surface attach here
	struct wlr_box box; wlr_output_layout_get_box(s->output_layout, out->wlr_output, &box);
	wlr_scene_node_set_position(&obj->scene->tree->node, box.x, box.y);
	obj->map.notify = layer_map; wl_signal_add(&ls->surface->events.map, &obj->map);
	obj->unmap.notify = layer_unmap; wl_signal_add(&ls->surface->events.unmap, &obj->unmap);
	obj->destroy.notify = layer_destroy; wl_signal_add(&ls->events.destroy, &obj->destroy);
	obj->commit.notify = layer_commit; wl_signal_add(&ls->surface->events.commit, &obj->commit);
}
int main(int argc, char *argv[]) {
	wlr_log_init(WLR_INFO, NULL);
	char *startup = NULL; int c;
	while ((c = getopt(argc, argv, "s:h")) != -1) {
		if (c == 's') startup = optarg;
		else { printf("Usage: %s [-s startup command]\n", argv[0]); return 0; }
	}
	signal(SIGCHLD, SIG_IGN);
	load_config();
	struct wc_server server = {0};
	wc_state_defaults(&server.layout);
	if (wc_state_directory(server.state_directory, sizeof(server.state_directory))) {
		char path[600]; snprintf(path, sizeof(path), "%s/native-layout.txt", server.state_directory);
		if (g_remember_layout) {
			wc_state_load(path, &server.layout);
			if (!g_save_titles) {
				for (unsigned i = 0; i < server.layout.count; i++) server.layout.windows[i].title[0] = 0;
				wc_state_save(server.state_directory, &server.layout);
			}
		}
		else wc_state_forget(server.state_directory);
		server.workspace = server.layout.workspace; server.desktop_visible = server.layout.desktop_visible;
		snprintf(path, sizeof(path), "%s/accessibility.json", server.state_directory);
		wc_accessibility_load(path, &g_accessibility);
	} else server.state_directory[0] = '\0';
	apply_derived_settings();
	config_changed(&server);
	server.wl_display = wl_display_create();
	server.backend = wlr_backend_autocreate(wl_display_get_event_loop(server.wl_display), NULL);
	if (!server.backend) { wlr_log(WLR_ERROR, "backend failed"); return 1; }
	server.renderer = wlr_renderer_autocreate(server.backend);
	if (!server.renderer) { wlr_log(WLR_ERROR, "renderer failed"); return 1; }
	wlr_renderer_init_wl_display(server.renderer, server.wl_display);
	server.allocator = wlr_allocator_autocreate(server.backend, server.renderer);
	if (!server.allocator) { wlr_log(WLR_ERROR, "allocator failed"); return 1; }
	wlr_compositor_create(server.wl_display, 5, server.renderer);
	wlr_subcompositor_create(server.wl_display);
	wlr_data_device_manager_create(server.wl_display);
	server.output_layout = wlr_output_layout_create(server.wl_display);
	wl_list_init(&server.outputs);
	server.output_manager = wlr_output_manager_v1_create(server.wl_display);
	server.output_manager_apply.notify = output_manager_apply;
	wl_signal_add(&server.output_manager->events.apply, &server.output_manager_apply);
	server.output_manager_test.notify = output_manager_test;
	wl_signal_add(&server.output_manager->events.test, &server.output_manager_test);
	server.new_output.notify = server_new_output;
	wl_signal_add(&server.backend->events.new_output, &server.new_output);
	server.scene = wlr_scene_create();
	server.background = wlr_scene_tree_create(&server.scene->tree);
	server.bottom = wlr_scene_tree_create(&server.scene->tree);
	server.windows = wlr_scene_tree_create(&server.scene->tree);
	server.top = wlr_scene_tree_create(&server.scene->tree);
	server.overlay = wlr_scene_tree_create(&server.scene->tree);
	server.lock_tree = wlr_scene_tree_create(&server.scene->tree);
	wlr_scene_node_set_enabled(&server.lock_tree->node, false);
	wl_list_init(&server.lock_surfaces);
	server.lock_manager = wlr_session_lock_manager_v1_create(server.wl_display);
	server.new_lock.notify = server_new_lock;
	wl_signal_add(&server.lock_manager->events.new_lock, &server.new_lock);
	server.scene_layout = wlr_scene_attach_output_layout(server.scene, server.output_layout);
	wl_list_init(&server.toplevels);
	server.xdg_shell = wlr_xdg_shell_create(server.wl_display, 5);
	server.new_xdg_toplevel.notify = server_new_toplevel;
	wl_signal_add(&server.xdg_shell->events.new_toplevel, &server.new_xdg_toplevel);
	server.new_xdg_popup.notify = server_new_popup;
	wl_signal_add(&server.xdg_shell->events.new_popup, &server.new_xdg_popup);
	server.layer_shell = wlr_layer_shell_v1_create(server.wl_display, 4);
	server.new_layer_surface.notify = server_new_layer;
	wl_signal_add(&server.layer_shell->events.new_surface, &server.new_layer_surface);
	server.cursor = wlr_cursor_create();
	wlr_cursor_attach_output_layout(server.cursor, server.output_layout);
	server.cursor_mgr = wlr_xcursor_manager_create(NULL, 24);
	server.cursor_motion.notify = cursor_motion;
	wl_signal_add(&server.cursor->events.motion, &server.cursor_motion);
	server.cursor_motion_absolute.notify = cursor_motion_abs;
	wl_signal_add(&server.cursor->events.motion_absolute, &server.cursor_motion_absolute);
	server.cursor_button.notify = cursor_button;
	wl_signal_add(&server.cursor->events.button, &server.cursor_button);
	server.cursor_axis.notify = cursor_axis;
	wl_signal_add(&server.cursor->events.axis, &server.cursor_axis);
	server.cursor_frame.notify = cursor_frame;
	wl_signal_add(&server.cursor->events.frame, &server.cursor_frame);
	wl_list_init(&server.keyboards); wl_list_init(&server.pointers);
	server.new_input.notify = server_new_input;
	wl_signal_add(&server.backend->events.new_input, &server.new_input);
	server.seat = wlr_seat_create(server.wl_display, "seat0");
	wlr_primary_selection_v1_device_manager_create(server.wl_display);
	server.request_set_primary_selection.notify = seat_primary_req;
	wl_signal_add(&server.seat->events.request_set_primary_selection, &server.request_set_primary_selection);
	struct wlr_xdg_activation_v1 *activation = wlr_xdg_activation_v1_create(server.wl_display);
	server.request_activate.notify = activation_req;
	wl_signal_add(&activation->events.request_activate, &server.request_activate);
	server.idle_inhibit = wlr_idle_inhibit_v1_create(server.wl_display);
	wlr_viewporter_create(server.wl_display);
	wlr_presentation_create(server.wl_display, server.backend);
	wlr_single_pixel_buffer_manager_v1_create(server.wl_display);
	wlr_fractional_scale_manager_v1_create(server.wl_display, 1);
	wlr_seat_set_capabilities(server.seat, WL_SEAT_CAPABILITY_POINTER | WL_SEAT_CAPABILITY_KEYBOARD);
	server.request_cursor.notify = seat_cursor_req;
	wl_signal_add(&server.seat->events.request_set_cursor, &server.request_cursor);
	server.request_set_selection.notify = seat_selection_req;
	wl_signal_add(&server.seat->events.request_set_selection, &server.request_set_selection);
	const char *sock = wl_display_add_socket_auto(server.wl_display);
	if (!sock) { wlr_log(WLR_ERROR, "socket failed"); return 1; }
	if (!wlr_backend_start(server.backend)) { wlr_log(WLR_ERROR, "backend start failed"); return 1; }
	wlr_log(WLR_INFO, "whitecube running on %s (Wayland-native, no XWayland)", sock);
	setenv("WAYLAND_DISPLAY", sock, 1);
	report_preferences();
	struct wl_event_loop *loop = wl_display_get_event_loop(server.wl_display);
	wc_activity(&server);
	server.settings_timer = wl_event_loop_add_timer(loop, accessibility_tick, &server);
	if (server.settings_timer) wl_event_source_timer_update(server.settings_timer, 1000);
	struct wl_event_source *sigterm = wl_event_loop_add_signal(loop, SIGTERM, terminate_signal, server.wl_display);
	struct wl_event_source *sigint = wl_event_loop_add_signal(loop, SIGINT, terminate_signal, server.wl_display);
	struct wl_event_source *sighup = wl_event_loop_add_signal(loop, SIGHUP, terminate_signal, server.wl_display);
	pid_t startup_group = startup ? spawn(startup) : -1;
	wl_display_run(server.wl_display);
	wc_save_layout(&server); server.shutting_down = true;
	if (server.settings_timer) wl_event_source_remove(server.settings_timer);
	stop_startup(startup_group);
	fullscreen_hint(false);
	if (sigterm) wl_event_source_remove(sigterm);
	if (sigint) wl_event_source_remove(sigint);
	if (sighup) wl_event_source_remove(sighup);
	wl_display_destroy_clients(server.wl_display);
	wl_display_destroy(server.wl_display);
	return 0;
}
