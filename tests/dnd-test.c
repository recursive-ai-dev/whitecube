/* Drag and drop between two real Wayland clients through the compositor's own input
 * handlers on a headless backend: refused serials, a completed drop and a cancelled drag. */
#undef NDEBUG
#define main whitecube_main
#include "whitecube.c"
#undef main
#include <wlr/backend/headless.h>
#include <wlr/types/wlr_shm.h>
#include <sys/wait.h>

static struct wc_server server;
static char dir[64];
static uint32_t clock_ms = 1;

static void spin_once(void) {
	wl_display_flush_clients(server.wl_display);
	wl_event_loop_dispatch(wl_display_get_event_loop(server.wl_display), 5);
	wl_display_flush_clients(server.wl_display);
}
static void spin(int ms) { for (int i = 0; i < ms / 5; i++) spin_once(); }
#define WAIT_FOR(cond) do { int waited_ = 0; while (!(cond)) { assert(waited_++ < 800 && #cond); spin_once(); } } while (0)
static bool log_has(const char *name, const char *text) {
	char path[128], data[4096]; snprintf(path, sizeof(path), "%s/%s.log", dir, name);
	FILE *f = fopen(path, "r"); if (!f) return false;
	size_t n = fread(data, 1, sizeof(data) - 1, f); fclose(f); data[n] = 0;
	return strstr(data, text) != NULL;
}
static int log_count(const char *name, const char *text) {
	char path[128], data[4096]; snprintf(path, sizeof(path), "%s/%s.log", dir, name);
	FILE *f = fopen(path, "r"); if (!f) return 0;
	size_t n = fread(data, 1, sizeof(data) - 1, f); fclose(f); data[n] = 0;
	int count = 0; for (char *p = data; (p = strstr(p, text)); p += strlen(text)) count++;
	return count;
}
static pid_t launch(const char *client, const char *mode) {
	pid_t pid = fork(); assert(pid >= 0);
	if (!pid) { execl(client, client, mode, dir, (char *)NULL); _exit(127); }
	return pid;
}
static struct wc_toplevel *window(const char *app) {
	struct wc_toplevel *t;
	wl_list_for_each(t, &server.toplevels, link)
		if (t->mapped && t->xdg_toplevel->app_id && !strcmp(t->xdg_toplevel->app_id, app)) return t;
	return NULL;
}
static void move_to(double x, double y) {
	wlr_cursor_warp(server.cursor, NULL, x, y);
	cursor_process(&server, clock_ms++);
}
static void press(uint32_t state) {
	struct wlr_pointer_button_event ev = {.time_msec = clock_ms++, .button = BTN_LEFT, .state = state};
	cursor_button(&server.cursor_button, &ev);
	wl_display_flush_clients(server.wl_display);
}
static void at_corner(struct wc_toplevel *t, bool far) {
	int x, y; wlr_scene_node_coords(&t->scene_tree->node, &x, &y);
	struct wlr_box box; wlr_xdg_surface_get_geometry(t->xdg_toplevel->base, &box);
	move_to(x + (far ? box.width - 6 : 6), y + (far ? box.height - 6 : 6));
}
static struct wlr_surface *surface_of(struct wc_toplevel *t) { return t->xdg_toplevel->base->surface; }

int main(int argc, char **argv) {
	assert(argc == 2);
	setenv("WLR_RENDERER", "pixman", 1);
	snprintf(dir, sizeof(dir), "/tmp/whitecube-dnd-XXXXXX"); assert(mkdtemp(dir));
	setenv("HOME", dir, 1); setenv("XDG_STATE_HOME", dir, 1); setenv("XDG_RUNTIME_DIR", dir, 1);
	config_defaults(); apply_derived_settings();
	struct wc_server *s = &server;
	s->wl_display = wl_display_create(); assert(s->wl_display);
	struct wl_event_loop *loop = wl_display_get_event_loop(s->wl_display);
	s->backend = wlr_headless_backend_create(loop); assert(s->backend);
	s->renderer = wlr_renderer_autocreate(s->backend); assert(s->renderer);
	wlr_renderer_init_wl_display(s->renderer, s->wl_display);
	s->allocator = wlr_allocator_autocreate(s->backend, s->renderer); assert(s->allocator);
	wlr_compositor_create(s->wl_display, 5, s->renderer);
	wlr_subcompositor_create(s->wl_display);
	wlr_data_device_manager_create(s->wl_display);
	s->output_layout = wlr_output_layout_create(s->wl_display);
	wl_list_init(&s->outputs);
	s->output_manager = wlr_output_manager_v1_create(s->wl_display);
	s->scene = wlr_scene_create();
	s->background = wlr_scene_tree_create(&s->scene->tree);
	s->bottom = wlr_scene_tree_create(&s->scene->tree);
	s->windows = wlr_scene_tree_create(&s->scene->tree);
	s->top = wlr_scene_tree_create(&s->scene->tree);
	s->overlay = wlr_scene_tree_create(&s->scene->tree);
	s->drag_icons = wlr_scene_tree_create(&s->scene->tree);
	s->lock_tree = wlr_scene_tree_create(&s->scene->tree);
	wlr_scene_node_set_enabled(&s->lock_tree->node, false);
	wl_list_init(&s->lock_surfaces); wl_list_init(&s->toplevels); wl_list_init(&s->keyboards); wl_list_init(&s->pointers);
	s->scene_layout = wlr_scene_attach_output_layout(s->scene, s->output_layout);
	s->new_output.notify = server_new_output;
	wl_signal_add(&s->backend->events.new_output, &s->new_output);
	s->xdg_shell = wlr_xdg_shell_create(s->wl_display, 5);
	s->new_xdg_toplevel.notify = server_new_toplevel;
	wl_signal_add(&s->xdg_shell->events.new_toplevel, &s->new_xdg_toplevel);
	s->new_xdg_popup.notify = server_new_popup;
	wl_signal_add(&s->xdg_shell->events.new_popup, &s->new_xdg_popup);
	s->cursor = wlr_cursor_create();
	wlr_cursor_attach_output_layout(s->cursor, s->output_layout);
	s->cursor_mgr = wlr_xcursor_manager_create(NULL, 24);
	s->seat = wlr_seat_create(s->wl_display, "seat0");
	wc_seat_listen_drag(s);
	wlr_seat_set_capabilities(s->seat, WL_SEAT_CAPABILITY_POINTER);
	const char *socket = wl_display_add_socket_auto(s->wl_display); assert(socket);
	setenv("WAYLAND_DISPLAY", socket, 1);
	assert(wlr_backend_start(s->backend));
	assert(wlr_headless_add_output(s->backend, 1280, 800));
	spin(20);

	pid_t source = launch(argv[1], "source");
	WAIT_FOR(window("dnd-source"));
	pid_t target = launch(argv[1], "target");
	WAIT_FOR(window("dnd-target"));
	struct wc_toplevel *from = window("dnd-source"), *to = window("dnd-target");
	spin(50);

	/* A press alone must not start a drag when the serial is forged. */
	at_corner(from, false);
	press(WL_POINTER_BUTTON_STATE_PRESSED);
	WAIT_FOR(log_has("source", "cancelled 1"));
	assert(!s->data_drag);
	press(WL_POINTER_BUTTON_STATE_RELEASED);
	spin(20);

	/* A real drag crosses windows even with the button held, then drops. */
	at_corner(from, false);
	press(WL_POINTER_BUTTON_STATE_PRESSED);
	WAIT_FOR(s->data_drag);
	spin(20);
	assert(!wl_list_empty(&s->drag_icons->children));
	at_corner(to, true);
	WAIT_FOR(log_count("target", "enter") == 1);
	assert(s->data_drag->focus == surface_of(to));
	spin(20);
	press(WL_POINTER_BUTTON_STATE_RELEASED);
	WAIT_FOR(log_has("target", "dropped file:///tmp/whitecube-dropped.txt"));
	WAIT_FOR(log_has("source", "finished 2"));
	assert(!s->data_drag);
	assert(wl_list_empty(&s->drag_icons->children));
	assert(s->seat->pointer_state.focused_surface == surface_of(to));

	/* Cancelling (as locking the screen does) tells both clients and never drops. */
	at_corner(from, false);
	press(WL_POINTER_BUTTON_STATE_PRESSED);
	WAIT_FOR(s->data_drag);
	spin(20);
	at_corner(to, true);
	WAIT_FOR(log_count("target", "enter") == 2);
	wc_cancel_data_drag(s);
	assert(!s->data_drag);
	WAIT_FOR(log_has("source", "cancelled 3"));
	WAIT_FOR(log_count("target", "leave") == 2);
	assert(log_count("target", "dropped file:") == 1);
	press(WL_POINTER_BUTTON_STATE_RELEASED);

	int status = -1;
	WAIT_FOR(waitpid(source, &status, WNOHANG) == source);
	assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	kill(target, SIGTERM); waitpid(target, NULL, 0);
	s->shutting_down = true;
	wl_display_destroy_clients(s->wl_display);
	wl_display_destroy(s->wl_display);
	char path[128];
	snprintf(path, sizeof(path), "%s/source.log", dir); unlink(path);
	snprintf(path, sizeof(path), "%s/target.log", dir); unlink(path);
	rmdir(dir);
	puts("dnd: forged serial refused, cross-client drop delivered, cancel reaches both clients");
	return 0;
}
