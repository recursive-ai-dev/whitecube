#define main settings_app_main
#include "cube-settings.c"
#undef main
#include <assert.h>

static void test_config(void) {
	char path[512];
	assert(cfg_set("privacy_hide_titles", "1"));
	user_config_path(path, sizeof(path));
	struct stat st; assert(!stat(path, &st) && !(st.st_mode & 0077));
	FILE *f = fopen(path, "w"); assert(f);
	fputs("# retain my preferences\nprivacy_hide_titles=0\nprivacy_hide_titles=1\nlong_command=", f);
	for (int i = 0; i < 900; i++) fputc('x', f);
	fputs("\nlast=untouched", f); fclose(f);
	const char *keys[] = {"privacy_hide_titles", "privacy_clipboard"};
	const char *values[] = {"0", "0"};
	assert(cfg_set_many(2, keys, values));
	f = fopen(path, "r"); assert(f);
	char data[2048]; size_t n = fread(data, 1, sizeof(data)-1, f); data[n] = 0; fclose(f);
	assert(strstr(data, "# retain my preferences\n"));
	assert(strstr(data, "privacy_hide_titles=0\n"));
	assert(!strstr(strstr(data, "privacy_hide_titles=")+1, "privacy_hide_titles="));
	assert(strstr(data, "last=untouched\nprivacy_clipboard=0\n"));
	char *long_line = strstr(data, "long_command="); assert(long_line);
	assert(strchr(long_line, '\n') - long_line == 913);
	assert(!cfg_set_many(33, keys, values));
	assert(!strcmp(cfg_get("privacy_clipboard", "1"), "0"));
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
	assert(cfg_set("bg", "06080d")); settings_palette();
	assert(C_BG[0] < .1 && C_INK[0] > .9 && C_WHITE3[0] < .1);
	acc.strong = true; settings_palette(); assert(C_MUTED[0] == C_INK[0]);
	assert(cfg_set("bg", "fbfbfb")); acc.strong = false; settings_palette();
	assert(C_BG[0] > .9 && C_INK[0] < .1 && C_WHITE3[0] > .9);
}
int main(void) {
	char temporary[] = "/tmp/whitecube-settings-test-XXXXXX";
	char *dir = mkdtemp(temporary); assert(dir); assert(!setenv("HOME", dir, 1));
	ncfg_sys = ncfg_user = 0;
	test_config(); test_navigation(); test_theme();
	char path[512]; user_config_path(path,sizeof(path)); unlink(path);
	snprintf(path,sizeof(path),"%s/.config/whitecube",dir); rmdir(path);
	snprintf(path,sizeof(path),"%s/.config",dir); rmdir(path); rmdir(dir);
	puts("settings: private atomic saves, preserved lines, privacy navigation, modal isolation, keyboard scrolling and theme contrast passed");
	return 0;
}
