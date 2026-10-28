#define main settings_app_main
#include "cube-settings.c"
#undef main
#include <assert.h>

static void test_config(void) {
	char path[512];
	assert(cfg_set("legacy_flag", "1"));
	user_config_path(path, sizeof(path));
	struct stat st; assert(!stat(path, &st) && !(st.st_mode & 0077));
	FILE *f = fopen(path, "w"); assert(f);
	fputs("# retain my preferences\nlegacy_flag=0\nlegacy_flag=1\nlong_command=", f);
	for (int i = 0; i < 900; i++) fputc('x', f);
	fputs("\nlast=untouched", f); fclose(f);
	const char *keys[] = {"legacy_flag", "legacy_second"};
	const char *values[] = {"0", "0"};
	assert(cfg_set_many(2, keys, values));
	f = fopen(path, "r"); assert(f);
	char data[2048]; size_t n = fread(data, 1, sizeof(data)-1, f); data[n] = 0; fclose(f);
	assert(strstr(data, "# retain my preferences\n"));
	assert(strstr(data, "legacy_flag=0\n"));
	assert(!strstr(strstr(data, "legacy_flag=")+1, "legacy_flag="));
	assert(strstr(data, "last=untouched\nlegacy_second=0\n"));
	char *long_line = strstr(data, "long_command="); assert(long_line);
	assert(strchr(long_line, '\n') - long_line == 913);
	assert(!cfg_set_many(33, keys, values));
	assert(!strcmp(cfg_get("legacy_second", "1"), "0"));
	unlink(path);
}
static void test_navigation(void) {
	assert(page_from_name("privacy") == PG_PRIVACY);
	g.render_only = true;
	reg_global(NULL, NULL, 1, wl_seat_interface.name, 5);
	assert(!g.seat && !g.xkb); // PNG rendering never creates keyboard listeners.
	g.render_only = false;
	table_set(&tables[T_PRIVACY], "setting\tremember_recent\tunmanaged\tNot configured\n");
	ui.page = PG_PRIVACY; ui.privacy_section = 1;
	g.h = 480; g.w = 900;
	cairo_surface_t *surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, g.w, g.h);
	cairo_t *cr = cairo_create(surface);
	draw(cr, g.w, g.h);
	assert(ui.nhits > PG_COUNT + 15);
	ui.focus = ui.hits[ui.nhits-2].id; focus_move(1);
	assert(ui.scroll[PG_PRIVACY] > 0); // Tab can reach controls below the viewport.
	draw(cr, g.w, g.h);
	struct hit *h = focused_hit(); assert(h && h->y >= 0 && h->y+h->h <= g.h);
	// Keyboard navigation remains inside a modal; page switching cannot escape it.
	modal.kind = M_CONFIRM; ui.focus = -1;
	copy(modal.title, sizeof(modal.title), "Clear recent files");
	copy(modal.text, sizeof(modal.text), "Confirm cleanup");
	copy(modal.ok_label, sizeof(modal.ok_label), "Clear");
	draw(cr, g.w, g.h);
	key_press(XKB_KEY_Page_Down, NULL, true, false); assert(ui.page == PG_PRIVACY);
	key_press(XKB_KEY_Down, NULL, false, false); assert(ui.focus >= ui.modal_first);
	strcpy(modal.pending.input, "private passphrase");
	key_press(XKB_KEY_Escape, NULL, false, false);
	assert(modal.kind == M_NONE && modal.pending.input[0] == 0);
	// A tall interface keeps the selected sidebar page visible via scrolling.
	S = 1.6; ui.focus = PG_SYSTEM+1; ui.focus_visible = true;
	draw(cr, g.w, g.h); assert(ui.sidebar_scroll > 0);
	for (int i=0; i<ui.nhits; i++) if (ui.hits[i].id == ui.focus) {
		assert(ui.hits[i].y >= 52*S && ui.hits[i].y+ui.hits[i].h <= g.h-42*S+0.1);
	}
	cairo_destroy(cr); cairo_surface_destroy(surface);
}
static void test_theme(void) {
	assert(g_settings_set_string(wc_prefs, "bg", "06080d")); cfg_load(); settings_palette();
	assert(C_BG[0] < .1 && C_INK[0] > .9 && C_WHITE3[0] < .1);
	acc.strong = true; settings_palette(); assert(C_MUTED[0] == C_INK[0]);
	assert(g_settings_set_string(wc_prefs, "bg", "fbfbfb")); cfg_load(); acc.strong = false; settings_palette();
	assert(C_BG[0] > .9 && C_INK[0] < .1 && C_WHITE3[0] > .9);
}
static void test_accessibility_and_display(void) {
	acc.ui_scale = 135; acc.terminal_scale = 150; acc.strong = true; acc.reduced_motion = true;
	assert(acc_save());
	char path[512]; state_path(path, sizeof(path), "accessibility.json");
	struct stat status; assert(!stat(path, &status) && !(status.st_mode & 0077));
	struct wc_accessibility applied; assert(wc_accessibility_load(path, &applied));
	assert(applied.ui_scale == 1.35 && applied.terminal_scale == 1.5 && applied.strong && applied.reduced_motion);
	unlink(path); acc_load(); assert(acc.ui_scale == 100 && !acc.strong && !acc.reduced_motion);
	int coordinate; assert(!output_coordinate("12junk", &coordinate));
	assert(!output_coordinate("", &coordinate)); assert(!output_coordinate("999999999999999999", &coordinate));
	assert(output_coordinate("-32768", &coordinate) && coordinate == -32768);
	om.n = 1; struct ohead *head = &om.heads[0];
	memset(head, 0, sizeof(*head)); head->live = head->p_enabled = true; head->p_mode = -1; head->p_scale = 1.25;
	strcpy(head->name, "TEST-1"); strcpy(head->fx.buf, "-20"); strcpy(head->fy.buf, "30");
	assert(outputs_valid()); head->p_enabled = false; assert(!outputs_valid()); head->p_enabled = true;
	strcpy(head->fx.buf, "bad"); assert(!outputs_valid()); strcpy(head->fx.buf, "-20");
	om.submitted_n = 1; om.submitted[0] = *head;
	head->p_scale = 2; // An edit after submission must not change what is persisted.
	assert(save_outputs_file());
	snprintf(path, sizeof(path), "%s/.config/whitecube/outputs", home_dir());
	FILE *file = fopen(path, "r"); assert(file); char data[1024]; size_t n=fread(data,1,sizeof(data)-1,file);data[n]=0;fclose(file);
	assert(strstr(data, "scale=1.250000") && strstr(data, "x=-20 y=30"));
	assert(!stat(path,&status) && !(status.st_mode & 0077)); unlink(path);
	om.n = om.submitted_n = 0;
	char options[256] = "compose:ralt,caps:escape";
	option_toggle(options, sizeof(options), "ctrl:nocaps");
	assert(option_on(options, "ctrl:nocaps") && !option_on(options, "caps:escape") && option_on(options, "compose:ralt"));
	option_toggle(options, sizeof(options), "caps:escape");
	assert(option_on(options, "caps:escape") && !option_on(options, "ctrl:nocaps"));
}
static void test_advanced(void) {
	assert(page_from_name("advanced") == PG_ADVANCED && page_from_name("pointer") == PG_POINTER);
	ui.page = PG_ADVANCED; S = 1; g.w = 1100; g.h = 900;
	strcpy(advanced.search.buf, "repeat"); strcpy(advanced.selected, "repeat-rate");
	cairo_surface_t *surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, g.w, g.h);
	cairo_t *cr = cairo_create(surface); draw(cr, g.w, g.h);
	assert(ui.nhits > PG_COUNT + 3 && !strcmp(advanced.value.buf, "25"));
	assert(g_settings_set_int(wc_prefs, "repeat-rate", 0)); cfg_load(); draw(cr, g.w, g.h);
	assert(!strcmp(advanced.value.buf, "0"));
	strcpy(advanced.value.buf, "draft"); assert(g_settings_set_int(wc_prefs, "repeat-rate", 40)); cfg_load(); draw(cr, g.w, g.h);
	assert(!strcmp(advanced.value.buf, "draft"));
	ui.page = PG_POINTER; draw(cr,g.w,g.h); assert(ui.nhits >= PG_COUNT + 5);
	cairo_destroy(cr); cairo_surface_destroy(surface);
}
static void wait_for_writes(void) {
	double deadline = now_s() + 15;
	while (any_action_running() && now_s() < deadline) {
		for (int i = 0; i < MAXJOBS; i++) if (jobs[i].used) job_read(&jobs[i]);
		usleep(1000);
	}
	assert(!any_action_running());
}
static int test_real_transactions(const char *helper) {
	assert(getenv("WHITECUBE_SETTINGS_REPORT")); /* Only called in the disposable integration profile. */
	copy(preferences_helper, sizeof(preferences_helper), helper); cfg_load(); ui.page = PG_ADVANCED;
	char previous[256]; copy(previous, sizeof(previous), cfg_get("tab_w", "180"));
	const char *keys[] = {"tab_w", "repeat_rate"}, *values[] = {"270", "36"};
	assert(cfg_set_many(2, keys, values)); wait_for_writes();
	assert(strstr(ui.toast, "saved and verified") && !strcmp(cfg_get("tab_w", ""), "270") && !strcmp(cfg_get("repeat_rate", ""), "36"));
	assert(cfg_set("repeat_rate", "9999")); wait_for_writes();
	assert(strstr(ui.toast, "failed") && !strcmp(cfg_get("repeat_rate", ""), "36"));
	action("Reset", P_PREFS, 2, "reset", "tab-w"); wait_for_writes();
	assert(!strcmp(cfg_get("tab_w", "180"), previous));
	action("Reset", P_PREFS, 2, "reset", "repeat-rate"); wait_for_writes();
	puts("settings UI transactions: grouped save, helper execution, verified success, rejected change and reset passed");
	return 0;
}
static void test_text_input(void) {
	assert(setlocale(LC_CTYPE, "C.UTF-8"));
	g.xkb = xkb_context_new(XKB_CONTEXT_NO_FLAGS); assert(g.xkb);
	struct xkb_rule_names names = {.layout = "us", .options = "compose:ralt"};
	g.keymap = xkb_keymap_new_from_names(g.xkb, &names, XKB_KEYMAP_COMPILE_NO_FLAGS); assert(g.keymap);
	g.xstate = xkb_state_new(g.keymap); g.compose = wc_compose_create(g.xkb); assert(g.compose);
	struct field field = {0};
	ui.nhits = 1; ui.hits[0] = (struct hit){.id = 100, .kind = H_FIELD}; ui.focus = 100; ui.edit = &field;
	handle_key(xkb_keymap_key_by_name(g.keymap, "RALT") - 8);
	handle_key(xkb_keymap_key_by_name(g.keymap, "AC11") - 8);
	handle_key(xkb_keymap_key_by_name(g.keymap, "AD03") - 8);
	assert(!strcmp(field.buf, "é"));
	handle_key(xkb_keymap_key_by_name(g.keymap, "BKSP") - 8); assert(!field.buf[0]);
	handle_key(xkb_keymap_key_by_name(g.keymap, "RALT") - 8);
	kb_leave(NULL, NULL, 0, NULL);
	assert(xkb_compose_state_get_status(g.compose) == XKB_COMPOSE_NOTHING);
	xkb_state_unref(g.xstate); xkb_keymap_unref(g.keymap);
	names.variant = "intl";
	g.keymap = xkb_keymap_new_from_names(g.xkb, &names, XKB_KEYMAP_COMPILE_NO_FLAGS); assert(g.keymap);
	g.xstate = xkb_state_new(g.keymap);
	handle_key(xkb_keymap_key_by_name(g.keymap, "AC11") - 8);
	handle_key(xkb_keymap_key_by_name(g.keymap, "AD03") - 8);
	assert(!strcmp(field.buf, "é"));
	xkb_state_unref(g.xstate); xkb_keymap_unref(g.keymap); xkb_compose_state_unref(g.compose); xkb_context_unref(g.xkb);
	g.xkb = NULL; g.keymap = NULL; g.xstate = NULL; g.compose = NULL; ui.edit = NULL; ui.nhits = 0; ui.focus = -1;
}

static void test_launch_errors(void) {
	ui.toast[0] = 0;
	spawn_detached("/missing-whitecube-settings-launch");
	double deadline = now_s() + 3;
	while (any_job() && now_s() < deadline) pump_jobs(10);
	assert(!any_job() && strstr(ui.toast, "Cannot launch") && strstr(ui.toast, "No such file"));
	ui.toast[0] = 0;
	spawn_detached("/bin/true");
	while (any_job() && now_s() < deadline) pump_jobs(10);
	assert(!any_job() && !ui.toast[0]);
	spawn_detached("/bin/sh -c 'exit 17'");
	while (any_job() && now_s() < deadline) pump_jobs(10);
	assert(!any_job() && !ui.toast[0]);
}

int main(int argc, char **argv) {
	if (argc == 3 && !strcmp(argv[1], "--transactions")) return test_real_transactions(argv[2]);
	char temporary[] = "/tmp/whitecube-settings-test-XXXXXX";
	char *dir = mkdtemp(temporary); assert(dir); assert(!setenv("HOME", dir, 1));
	assert(!setenv("GSETTINGS_BACKEND", "memory", 1));
	assert(!setenv("XDG_STATE_HOME", dir, 1));
	ncfg_sys = ncfg_user = 0;
	assert(wc_prefs_init());
	test_config(); test_navigation(); test_theme(); test_accessibility_and_display(); test_advanced();
	test_text_input();
	test_launch_errors();
	char path[512]; user_config_path(path,sizeof(path)); unlink(path);
	snprintf(path,sizeof(path),"%s/.config/whitecube",dir); rmdir(path);
	snprintf(path,sizeof(path),"%s/.config",dir); rmdir(path);
	snprintf(path,sizeof(path),"%s/whitecube",dir); rmdir(path); rmdir(dir);
	puts("settings: private atomic saves, preserved lines, privacy navigation, modal isolation, keyboard scrolling and theme contrast passed");
	return 0;
}
