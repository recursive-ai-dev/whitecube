/* Real Wayland clients for dnd-test: "source" starts drags from pointer presses and
 * "target" accepts and reads a drop. Each records protocol results in <dir>/<mode>.log. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <linux/input-event-codes.h>
#include <wayland-client.h>
#include "xdg-shell-client.h"

#define PAYLOAD "file:///tmp/whitecube-dropped.txt\r\n"
#define MIME "text/uri-list"
static struct wl_compositor *compositor; static struct wl_shm *shm; static struct xdg_wm_base *wm;
static struct wl_seat *seat; static struct wl_data_device_manager *manager; static struct wl_data_device *device;
static struct wl_surface *surface; static struct xdg_toplevel *toplevel; static FILE *logfile;
static int is_source, presses, done; static struct wl_data_offer *offer; static struct wl_display *display;

static void note(const char *format, int value) { fprintf(logfile, format, value); fputc('\n', logfile); fflush(logfile); }
static struct wl_buffer *buffer_new(int w, int h) {
	int size = w * h * 4, fd = memfd_create("dnd-client", MFD_CLOEXEC);
	if (fd < 0 || ftruncate(fd, size)) exit(2);
	void *pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (pixels == MAP_FAILED) exit(2);
	memset(pixels, 0xff, size); munmap(pixels, size);
	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	struct wl_buffer *buffer = wl_shm_pool_create_buffer(pool, 0, w, h, w * 4, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool); close(fd);
	return buffer;
}
static void source_target(void *d, struct wl_data_source *s, const char *m) { (void)d; (void)s; (void)m; }
static void source_send(void *d, struct wl_data_source *s, const char *mime, int fd) {
	(void)d; (void)s;
	if (!strcmp(mime, MIME) && write(fd, PAYLOAD, strlen(PAYLOAD)) < 0) exit(2);
	close(fd);
}
static void source_cancelled(void *d, struct wl_data_source *s) {
	(void)d; wl_data_source_destroy(s); note("cancelled %d", presses);
	if (presses >= 3) done = 1;
}
static void source_performed(void *d, struct wl_data_source *s) { (void)d; (void)s; note("performed %d", presses); }
static void source_finished(void *d, struct wl_data_source *s) { (void)d; wl_data_source_destroy(s); note("finished %d", presses); }
static void source_action(void *d, struct wl_data_source *s, uint32_t a) { (void)d; (void)s; (void)a; }
static const struct wl_data_source_listener source_listener = {
	source_target, source_send, source_cancelled, source_performed, source_finished, source_action,
};
static void start_drag(uint32_t serial) {
	struct wl_data_source *source = wl_data_device_manager_create_data_source(manager);
	wl_data_source_add_listener(source, &source_listener, NULL);
	wl_data_source_offer(source, MIME);
	wl_data_source_set_actions(source, WL_DATA_DEVICE_MANAGER_DND_ACTION_COPY);
	struct wl_surface *icon = NULL;
	if (presses == 2) { /* A real icon sits directly under the cursor and must not hide the drop target. */
		icon = wl_compositor_create_surface(compositor);
		wl_surface_attach(icon, buffer_new(32, 32), -16, -16);
		wl_surface_damage_buffer(icon, 0, 0, 32, 32);
	}
	/* The first press uses a forged serial, which the compositor must refuse. */
	wl_data_device_start_drag(device, source, surface, icon, presses == 1 ? serial + 1000 : serial);
	if (icon) wl_surface_commit(icon);
}
static void pointer_enter(void *d, struct wl_pointer *p, uint32_t s, struct wl_surface *f, wl_fixed_t x, wl_fixed_t y) { (void)d; (void)p; (void)s; (void)f; (void)x; (void)y; }
static void pointer_leave(void *d, struct wl_pointer *p, uint32_t s, struct wl_surface *f) { (void)d; (void)p; (void)s; (void)f; }
static void pointer_motion(void *d, struct wl_pointer *p, uint32_t t, wl_fixed_t x, wl_fixed_t y) { (void)d; (void)p; (void)t; (void)x; (void)y; }
static void pointer_button(void *d, struct wl_pointer *p, uint32_t serial, uint32_t t, uint32_t button, uint32_t state) {
	(void)d; (void)p; (void)t;
	if (!is_source || button != BTN_LEFT || state != WL_POINTER_BUTTON_STATE_PRESSED) return;
	presses++; start_drag(serial);
}
static void pointer_axis(void *d, struct wl_pointer *p, uint32_t t, uint32_t a, wl_fixed_t v) { (void)d; (void)p; (void)t; (void)a; (void)v; }
static void pointer_frame(void *d, struct wl_pointer *p) { (void)d; (void)p; }
static void pointer_source(void *d, struct wl_pointer *p, uint32_t s) { (void)d; (void)p; (void)s; }
static void pointer_stop(void *d, struct wl_pointer *p, uint32_t t, uint32_t a) { (void)d; (void)p; (void)t; (void)a; }
static void pointer_discrete(void *d, struct wl_pointer *p, uint32_t a, int32_t v) { (void)d; (void)p; (void)a; (void)v; }
static const struct wl_pointer_listener pointer_listener = {
	.enter = pointer_enter, .leave = pointer_leave, .motion = pointer_motion, .button = pointer_button, .axis = pointer_axis,
	.frame = pointer_frame, .axis_source = pointer_source, .axis_stop = pointer_stop, .axis_discrete = pointer_discrete,
};
static void offer_mime(void *d, struct wl_data_offer *o, const char *m) { (void)d; (void)o; (void)m; }
static void offer_source_actions(void *d, struct wl_data_offer *o, uint32_t a) { (void)d; (void)o; (void)a; }
static void offer_action(void *d, struct wl_data_offer *o, uint32_t a) { (void)d; (void)o; (void)a; }
static const struct wl_data_offer_listener offer_listener = {offer_mime, offer_source_actions, offer_action};
static void device_offer(void *d, struct wl_data_device *v, struct wl_data_offer *o) { (void)d; (void)v; wl_data_offer_add_listener(o, &offer_listener, NULL); }
static void device_enter(void *d, struct wl_data_device *v, uint32_t serial, struct wl_surface *f, wl_fixed_t x, wl_fixed_t y, struct wl_data_offer *o) {
	(void)d; (void)v; (void)f; (void)x; (void)y;
	if (!o) return;
	offer = o; note("enter %d", 1);
	wl_data_offer_accept(o, serial, MIME);
	wl_data_offer_set_actions(o, WL_DATA_DEVICE_MANAGER_DND_ACTION_COPY, WL_DATA_DEVICE_MANAGER_DND_ACTION_COPY);
}
static void device_leave(void *d, struct wl_data_device *v) {
	(void)d; (void)v; note("leave %d", 1);
	if (offer) { wl_data_offer_destroy(offer); offer = NULL; }
}
static void device_motion(void *d, struct wl_data_device *v, uint32_t t, wl_fixed_t x, wl_fixed_t y) { (void)d; (void)v; (void)t; (void)x; (void)y; }
static void device_drop(void *d, struct wl_data_device *v) {
	(void)d; (void)v;
	if (!offer) return;
	int fds[2]; if (pipe2(fds, O_CLOEXEC)) exit(2);
	wl_data_offer_receive(offer, MIME, fds[1]); close(fds[1]);
	wl_display_flush(display);
	char text[256]; size_t used = 0; ssize_t n;
	struct pollfd ready = {fds[0], POLLIN, 0};
	while (used < sizeof(text) - 1 && poll(&ready, 1, 3000) > 0 && (n = read(fds[0], text + used, sizeof(text) - 1 - used)) > 0) used += (size_t)n;
	text[used] = 0; close(fds[0]);
	fprintf(logfile, "dropped %s", text); fflush(logfile);
	wl_data_offer_finish(offer); wl_data_offer_destroy(offer); offer = NULL;
}
static void device_selection(void *d, struct wl_data_device *v, struct wl_data_offer *o) { (void)d; (void)v; if (o) wl_data_offer_destroy(o); }
static const struct wl_data_device_listener device_listener = {device_offer, device_enter, device_leave, device_motion, device_drop, device_selection};
static void wm_ping(void *d, struct xdg_wm_base *b, uint32_t serial) { (void)d; xdg_wm_base_pong(b, serial); }
static const struct xdg_wm_base_listener wm_listener = {wm_ping};
static void surface_configure(void *d, struct xdg_surface *x, uint32_t serial) {
	(void)d; xdg_surface_ack_configure(x, serial);
	static int attached;
	if (attached++) { wl_surface_commit(surface); return; }
	wl_surface_attach(surface, buffer_new(240, 180), 0, 0);
	wl_surface_damage_buffer(surface, 0, 0, 240, 180);
	wl_surface_commit(surface);
}
static const struct xdg_surface_listener surface_listener = {surface_configure};
static void top_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h, struct wl_array *s) { (void)d; (void)t; (void)w; (void)h; (void)s; }
static void top_close(void *d, struct xdg_toplevel *t) { (void)d; (void)t; done = 1; }
static void top_bounds(void *d, struct xdg_toplevel *t, int32_t w, int32_t h) { (void)d; (void)t; (void)w; (void)h; }
static void top_caps(void *d, struct xdg_toplevel *t, struct wl_array *c) { (void)d; (void)t; (void)c; }
static const struct xdg_toplevel_listener top_listener = {top_configure, top_close, top_bounds, top_caps};
static void global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t version) {
	(void)d; (void)version;
	if (!strcmp(iface, wl_compositor_interface.name)) compositor = wl_registry_bind(r, name, &wl_compositor_interface, 4);
	else if (!strcmp(iface, wl_shm_interface.name)) shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
	else if (!strcmp(iface, xdg_wm_base_interface.name)) wm = wl_registry_bind(r, name, &xdg_wm_base_interface, 1);
	else if (!strcmp(iface, wl_seat_interface.name)) seat = wl_registry_bind(r, name, &wl_seat_interface, 5);
	else if (!strcmp(iface, wl_data_device_manager_interface.name)) manager = wl_registry_bind(r, name, &wl_data_device_manager_interface, 3);
}
static void global_remove(void *d, struct wl_registry *r, uint32_t n) { (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {global, global_remove};
int main(int argc, char **argv) {
	if (argc != 3) return 2;
	is_source = !strcmp(argv[1], "source");
	char path[512]; snprintf(path, sizeof(path), "%s/%s.log", argv[2], argv[1]);
	if (!(logfile = fopen(path, "w"))) return 2;
	display = wl_display_connect(NULL);
	if (!display) return 2;
	wl_registry_add_listener(wl_display_get_registry(display), &registry_listener, NULL);
	wl_display_roundtrip(display);
	if (!compositor || !shm || !wm || !seat || !manager) return 2;
	xdg_wm_base_add_listener(wm, &wm_listener, NULL);
	wl_pointer_add_listener(wl_seat_get_pointer(seat), &pointer_listener, NULL);
	device = wl_data_device_manager_get_data_device(manager, seat);
	wl_data_device_add_listener(device, &device_listener, NULL);
	surface = wl_compositor_create_surface(compositor);
	struct xdg_surface *x = xdg_wm_base_get_xdg_surface(wm, surface);
	xdg_surface_add_listener(x, &surface_listener, NULL);
	toplevel = xdg_surface_get_toplevel(x);
	xdg_toplevel_add_listener(toplevel, &top_listener, NULL);
	xdg_toplevel_set_app_id(toplevel, is_source ? "dnd-source" : "dnd-target");
	wl_surface_commit(surface);
	while (!done && wl_display_dispatch(display) >= 0) {}
	wl_display_roundtrip(display);
	return done ? 0 : 3;
}
