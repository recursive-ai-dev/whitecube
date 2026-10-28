/* Real output hotplug against the compositor's own handlers, with a wlroots headless backend.
 * Run under ASan/UBSan (SANITIZE=1 ./BUILD.sh) it also proves add/remove cycles do not leak or use freed outputs. */
#undef NDEBUG
#define main whitecube_main
#include "whitecube.c"
#undef main
#include <wlr/backend/headless.h>
#include <wlr/interfaces/wlr_output.h>

static int count_outputs(struct wc_server *s) {
	int n = 0; struct wc_output *o;
	wl_list_for_each(o, &s->outputs, link) n++;
	return n;
}
static int layout_outputs(struct wc_server *s) {
	int n = 0; struct wlr_output_layout_output *lo;
	wl_list_for_each(lo, &s->output_layout->outputs, link) n++;
	return n;
}
static void spin(struct wc_server *s, int ms) {
	struct wl_event_loop *loop = wl_display_get_event_loop(s->wl_display);
	for (int i = 0; i < ms / 5; i++) { wl_event_loop_dispatch(loop, 5); wl_display_flush_clients(s->wl_display); }
}

static void setup(struct wc_server *s, char *home, const char *saved_outputs) {
	setenv("WLR_RENDERER", "pixman", 1);
	snprintf(home, 64, "/tmp/whitecube-hotplug-XXXXXX");
	assert(mkdtemp(home));
	setenv("HOME", home, 1);                    /* saved output settings come from here: none */
	setenv("XDG_STATE_HOME", home, 1);
	if (saved_outputs) {                        /* ~/.config/whitecube/outputs, as cube-settings writes it */
		char dir[128], file[160];
		snprintf(dir, sizeof(dir), "%s/.config", home); assert(!mkdir(dir, 0700));
		snprintf(dir, sizeof(dir), "%s/.config/whitecube", home); assert(!mkdir(dir, 0700));
		snprintf(file, sizeof(file), "%s/outputs", dir);
		FILE *f = fopen(file, "w"); assert(f && fputs(saved_outputs, f) >= 0 && !fclose(f));
	}
	memset(s, 0, sizeof(*s));
	config_defaults();
	apply_derived_settings();
	s->state_directory[0] = '\0';
	s->wl_display = wl_display_create(); assert(s->wl_display);
	struct wl_event_loop *loop = wl_display_get_event_loop(s->wl_display);
	s->backend = wlr_headless_backend_create(loop); assert(s->backend);
	s->renderer = wlr_renderer_autocreate(s->backend); assert(s->renderer);
	wlr_renderer_init_wl_display(s->renderer, s->wl_display);
	s->allocator = wlr_allocator_autocreate(s->backend, s->renderer); assert(s->allocator);
	wlr_compositor_create(s->wl_display, 5, s->renderer);
	s->output_layout = wlr_output_layout_create(s->wl_display);
	wl_list_init(&s->outputs);
	s->output_manager = wlr_output_manager_v1_create(s->wl_display);
	s->scene = wlr_scene_create();
	s->background = wlr_scene_tree_create(&s->scene->tree);
	s->bottom = wlr_scene_tree_create(&s->scene->tree);
	s->windows = wlr_scene_tree_create(&s->scene->tree);
	s->top = wlr_scene_tree_create(&s->scene->tree);
	s->overlay = wlr_scene_tree_create(&s->scene->tree);
	s->lock_tree = wlr_scene_tree_create(&s->scene->tree);
	wlr_scene_node_set_enabled(&s->lock_tree->node, false);
	wl_list_init(&s->lock_surfaces); wl_list_init(&s->toplevels); wl_list_init(&s->keyboards);
	s->scene_layout = wlr_scene_attach_output_layout(s->scene, s->output_layout);
	s->new_output.notify = server_new_output;
	wl_signal_add(&s->backend->events.new_output, &s->new_output);
	assert(wlr_backend_start(s->backend));
}

static int g_plugged;   /* wlroots numbers headless outputs HEADLESS-1, -2, ... across the whole process */
static struct wlr_output *plug(struct wc_server *s, unsigned w, unsigned h) {
	struct wlr_output *o = wlr_headless_add_output(s->backend, w, h);
	assert(o);
	g_plugged++;
	char expected[32]; snprintf(expected, sizeof(expected), "HEADLESS-%d", g_plugged);
	assert(!strcmp(o->name, expected));
	spin(s, 30);
	return o;
}

/* A rejected DRM commit schedules no page flip. Reproduce that with the real
 * scene renderer, then require recovery without any new input or scene damage. */
static const struct wlr_output_impl *real_output_impl;
static struct wlr_output_impl failing_output_impl;
static unsigned failures_left, commit_attempts;
static bool failing_output_commit(struct wlr_output *output, const struct wlr_output_state *state) {
	commit_attempts++;
	if (failures_left) { failures_left--; errno = EBUSY; return false; }
	output->impl = real_output_impl;
	bool ok = real_output_impl->commit(output, state);
	output->impl = &failing_output_impl;
	return ok;
}
static void fail_frames(struct wlr_output *screen, unsigned count) {
	real_output_impl = screen->impl;
	failing_output_impl = *real_output_impl;
	failing_output_impl.commit = failing_output_commit;
	screen->impl = &failing_output_impl;
	failures_left = count; commit_attempts = 0;
}
static void test_failed_frame_recovers(struct wc_server *s) {
	struct wlr_output *screen = plug(s, 800, 600);
	spin(s, 60);
	struct wc_output *o = server_first_output(s);
	fail_frames(screen, 2);
	uint32_t before = screen->commit_seq;
	wlr_scene_rect_set_color(o->bg_flat, C_WHITE);
	spin(s, 200);
	assert(commit_attempts == 3 && failures_left == 0);
	assert(screen->commit_seq == before + 1 && !screen->needs_frame);
	spin(s, 100);
	assert(commit_attempts == 3); /* Successful recovery cancels further retries. */
	screen->impl = real_output_impl;
	wlr_output_destroy(screen); spin(s, 20);
}
static void test_failed_frame_backoff_and_unplug(struct wc_server *s) {
	struct wlr_output *screen = plug(s, 800, 600);
	spin(s, 60);
	fail_frames(screen, 100);
	wlr_output_schedule_frame(screen);
	spin(s, 200);
	/* Persistent failure leaves the event loop responsive, with bounded work. */
	assert(commit_attempts >= 2 && commit_attempts <= 5);
	unsigned attempts = commit_attempts;
	screen->impl = real_output_impl;
	wlr_output_destroy(screen);
	spin(s, 300); /* The pending retry must not access the disconnected output. */
	assert(commit_attempts == attempts && count_outputs(s) == 0);
}
static void test_failed_lock_frame_recovers(struct wc_server *s) {
	struct wlr_output *screen = plug(s, 800, 600);
	spin(s, 60);
	struct wc_output *o = server_first_output(s);
	fail_frames(screen, 2);
	s->locked = true;
	wlr_scene_node_set_enabled(&s->lock_tree->node, true);
	wc_lock_refresh(s);
	spin(s, 5);
	assert(commit_attempts == 1 && !o->lock_frame_submitted && !o->lock_frame_presented);
	spin(s, 200);
	assert(commit_attempts == 3 && o->lock_frame_submitted && o->lock_frame_presented);
	screen->impl = real_output_impl;
	wlr_output_destroy(screen); spin(s, 20);
	s->locked = false;
	wlr_scene_node_set_enabled(&s->lock_tree->node, false);
}

static void test_hotplug(struct wc_server *s) {
	assert(count_outputs(s) == 0);
	struct wlr_output *a = plug(s, 1280, 720);
	assert(count_outputs(s) == 1 && layout_outputs(s) == 1 && a->enabled);
	struct wlr_output *b = plug(s, 1920, 1080);
	assert(count_outputs(s) == 2 && layout_outputs(s) == 2 && b->enabled);
	struct wlr_box box_a, box_b;
	wlr_output_layout_get_box(s->output_layout, a, &box_a);
	wlr_output_layout_get_box(s->output_layout, b, &box_b);
	assert(box_a.x + box_a.width <= box_b.x || box_b.x + box_b.width <= box_a.x);   /* side by side, not overlapping */
	spin(s, 100);                                                                    /* frames render on both */
	wlr_output_destroy(b); spin(s, 30);
	assert(count_outputs(s) == 1 && layout_outputs(s) == 1);
	assert(a->enabled);
	wlr_output_destroy(a); spin(s, 30);
	assert(count_outputs(s) == 0 && layout_outputs(s) == 0);                         /* last screen unplugged: still alive */
	struct wlr_output *c = plug(s, 1024, 768);
	assert(count_outputs(s) == 1 && c->enabled);                                      /* the first screen always comes on */
	wlr_output_destroy(c); spin(s, 30);
	assert(count_outputs(s) == 0);
}

static void test_grouped_tabs(struct wc_server *s) {
	struct wlr_output *screen = plug(s, 1280, 300);
	wc_retile(s);
	struct wc_output *o = server_first_output(s);
	assert(o->more_on); // Even an empty workspace has a count button.
	wc_panel_toggle(s);
	assert(o->panel_open && o->panel_buf->node.enabled);
	assert(wc_panel_key(s, XKB_KEY_Escape) && !o->panel_open);
	struct wc_toplevel tabs[20] = {0};
	struct wlr_xdg_toplevel xdg[20] = {0};
	for (int i = 0; i < 20; i++) {
		xdg[i].title = "Test window"; xdg[i].app_id = "test";
		tabs[i].mapped = true; tabs[i].xdg_toplevel = &xdg[i];
		tabs[i].tab_border = o->more_border;
		wl_list_insert(s->toplevels.prev, &tabs[i].link);
	}
	s->focused = &tabs[19];
	wc_panel_toggle(s);
	assert(o->panel_selected == 19 && o->panel_scroll > 0);
	assert(wc_panel_key(s, XKB_KEY_Home) && o->panel_selected == 0 && !o->panel_scroll);
	assert(wc_panel_key(s, XKB_KEY_Down) && o->panel_selected == 1);
	assert(wc_panel_key(s, XKB_KEY_Page_Down) && o->panel_selected > 1);
	assert(wc_panel_key(s, XKB_KEY_End) && o->panel_selected == 19);
	assert(!wc_panel_key(s, XKB_KEY_a));
	wc_panel_key(s, XKB_KEY_Escape);
	for (int i = 0; i < 20; i++) wl_list_remove(&tabs[i].link);
	s->focused = NULL;
	wlr_output_destroy(screen); spin(s, 20);
}

static void test_cycles(struct wc_server *s) {
	for (int i = 0; i < 25; i++) {
		struct wlr_output *a = plug(s, 800 + i, 600), *b = plug(s, 640, 480 + i);
		assert(count_outputs(s) == 2);
		if (i % 2) { wlr_output_destroy(a); wlr_output_destroy(b); }
		else { wlr_output_destroy(b); wlr_output_destroy(a); }
		spin(s, 10);
		assert(count_outputs(s) == 0 && layout_outputs(s) == 0);
	}
}

static void test_unplug_while_dragging_and_retiling(struct wc_server *s) {
	struct wlr_output *a = plug(s, 1280, 720), *b = plug(s, 1280, 720);
	wc_retile(s);
	wlr_output_destroy(b); spin(s, 20);
	wc_retile(s);                                  /* retiling after a removal must not touch the freed output */
	wlr_output_destroy(a); spin(s, 20);
	wc_retile(s);
	assert(count_outputs(s) == 0);
}

static void test_a_screen_plugged_in_while_locked_is_shielded(struct wc_server *s) {
	struct wlr_output *a = plug(s, 1280, 720);
	s->locked = true;
	wlr_scene_node_set_enabled(&s->lock_tree->node, true);
	wc_lock_refresh(s);
	struct wc_output *first = wl_container_of(s->outputs.next, first, link);
	assert(first->lock_bg && first->lock_bg->node.enabled);
	struct wlr_output *b = plug(s, 1920, 1080);   /* a monitor appears while the session is locked */
	wc_lock_refresh(s);
	assert(count_outputs(s) == 2);
	struct wc_output *o;
	wl_list_for_each(o, &s->outputs, link) {
		assert(o->lock_bg && o->lock_bg->node.enabled);                       /* every screen has an opaque shield */
		assert(o->lock_bg->color[3] == 1);
		int w, h; wlr_output_effective_resolution(o->wlr_output, &w, &h);
		assert(o->lock_bg->width == w && o->lock_bg->height == h);          /* covering the whole screen */
	}
	assert(!s->top->node.enabled || s->locked);
	wlr_output_destroy(b); wlr_output_destroy(a); spin(s, 20);
	s->locked = false;
	assert(count_outputs(s) == 0);
}

static void test_saved_output_settings(struct wc_server *s) {
	/* The next two outputs are saved as disabled, the third with scale 2 (see main). */
	struct wlr_output *first = plug(s, 1280, 720);
	assert(first->enabled);                                   /* the first screen always comes on, whatever was saved */
	assert(count_outputs(s) == 1 && layout_outputs(s) == 1);
	struct wlr_output *second = plug(s, 1280, 720);
	assert(!second->enabled);                                  /* a later screen honours "disabled" ... */
	assert(count_outputs(s) == 2 && layout_outputs(s) == 1);   /* ... and takes no space in the layout */
	struct wlr_output *third = plug(s, 1280, 720);
	assert(third->enabled && third->scale == 2.0f);            /* saved scale is applied on hotplug */
	assert(layout_outputs(s) == 2);
	wlr_output_destroy(second); wlr_output_destroy(third); wlr_output_destroy(first); spin(s, 30);
	assert(count_outputs(s) == 0 && layout_outputs(s) == 0);
}

static void teardown(struct wc_server *s, const char *home) {
	s->shutting_down = true;
	wl_display_destroy_clients(s->wl_display);
	wl_display_destroy(s->wl_display);
	free(s);
	char path[160];
	snprintf(path, sizeof(path), "%s/.config/whitecube/outputs", home); unlink(path);
	snprintf(path, sizeof(path), "%s/.config/whitecube", home); rmdir(path);
	snprintf(path, sizeof(path), "%s/.config", home); rmdir(path);
	rmdir(home);
}

int main(void) {
	char home[64];
	struct wc_server *s = calloc(1, sizeof(*s));
	setup(s, home, NULL);
	test_failed_frame_recovers(s);
	test_failed_frame_backoff_and_unplug(s);
	test_failed_lock_frame_recovers(s);
	test_hotplug(s);
	test_grouped_tabs(s);
	test_cycles(s);
	test_unplug_while_dragging_and_retiling(s);
	test_a_screen_plugged_in_while_locked_is_shielded(s);
	teardown(s, home);
	char saved[160];                           /* settings are looked up by output name, so name the next three */
	snprintf(saved, sizeof(saved), "HEADLESS-%d enabled=0\nHEADLESS-%d enabled=0\nHEADLESS-%d scale=2\n",
		g_plugged + 1, g_plugged + 2, g_plugged + 3);
	s = calloc(1, sizeof(*s));
	setup(s, home, saved);
	test_saved_output_settings(s);
	teardown(s, home);
	puts("hotplug-test: OK");
	return 0;
}
