// cube-ca: animated cellular-automata wallpaper for whitecube.
// Wayland-native layer-shell (background) client, SHM software rendering.
// Loops the simulation with different variables: rule, density, cell size,
// tick rate, seed symmetry. White cubes evolve over a star field.
// Each cube is a live cell in the actual toroidal simulation, not a video.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <sys/mman.h>
#include <sys/timerfd.h>
#include <poll.h>
#include <limits.h>
#include <errno.h>
#include <math.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <glob.h>
#include <linux/input-event-codes.h>
#include <wayland-client.h>
#include "wlr-layer-shell-client.h"
#include "desktop-state.h"

struct rule {
	const char *name;
	uint16_t birth, surv; // bit n = true for n neighbours
	float density;
	int cell;   // px per cell
	int fps;    // sim ticks per second
	int dur_s;  // max seconds before switching
};

static const struct rule RULES[] = {
	{ "life",     1u<<3,                          (1u<<2)|(1u<<3),               0.16f, 24, 12, 45 },
	{ "highlife", (1u<<3)|(1u<<6),                (1u<<2)|(1u<<3),               0.14f, 24, 12, 45 },
	{ "diamoeba", (1u<<3)|(1u<<5)|(1u<<6)|(1u<<7)|(1u<<8), (1u<<5)|(1u<<6)|(1u<<7)|(1u<<8), 0.34f, 24, 10, 40 },
	{ "daynight", (1u<<3)|(1u<<6)|(1u<<7)|(1u<<8), (1u<<3)|(1u<<4)|(1u<<6)|(1u<<7)|(1u<<8), 0.35f, 22, 10, 45 },
	{ "anneal",   (1u<<4)|(1u<<6)|(1u<<7)|(1u<<8), (1u<<3)|(1u<<5)|(1u<<6)|(1u<<7)|(1u<<8), 0.50f, 28, 10, 40 },
	{ "seeds",    (1u<<2),                         0,                             0.06f, 22, 14, 20 },
};
#define NRULES (sizeof(RULES) / sizeof(RULES[0]))

#define COS_PHASES 7
#define COS_STARS 420
#define COS_TRANSIT_S 9.0
#define COS_DWELL_S 22.0
#define COS_FINAL_DWELL_S 40.0
#define COS_REWIND_S 12.0
struct cosmos_star { float x, y, mag; double born; };
struct cosmos {
	bool on, want_on, auto_zoom;
	double zoom, speed, t, dwell, uni_t;
	int dir, target, obs;
	float *dens, *hmap, *cmap, dmean, dscale;
	int dw, dh;
	uint32_t *layer; size_t layer_n;
	struct cosmos_star stars[COS_STARS]; int nstars, star_next;
	float *wx, *wy, *wb, nodex[36], nodey[36]; int nnodes, nedges, edge_a[72], edge_b[72];
	uint64_t last_ms;
};

struct ca {
	// wayland
	struct wl_display *dpy;
	struct wl_registry *reg;
	struct wl_compositor *comp;
	struct wl_shm *shm;
	struct wl_seat *seat;
	struct wl_pointer *pointer;
	struct zwlr_layer_shell_v1 *layer_shell;
	struct wl_surface *surf;
	struct zwlr_layer_surface_v1 *layer;
	struct { struct wl_buffer *buf; void *px; int fd; bool busy; size_t size; } b[2];
	uint32_t *stars;
	int W, H, stride;
	bool configured, running;
	int tfd, control_fd;
	char control_path[108];
	bool volume, paused, edit, manual, pressure, reduced_motion;
	int depth, slice, draw_button, effective_fps;
	double yaw, pointer_x, pointer_y;
	uint64_t last_pressure_ms;
	// sim
	uint8_t *grid, *next, *age;
	int cols, rows;
	unsigned rule;
	uint64_t rule_start_ms, last_tick_ms, last_check_tick;
	uint64_t tick;
	uint32_t last_hash; int still_count;
	unsigned seed;
	struct cosmos cs;
};

static void cosmos_rebuild(struct ca *c);
static void update_input(struct ca *c);

static uint64_t now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int shm_alloc(int size) {
	int fd = memfd_create("cube-ca", MFD_CLOEXEC);
	if (fd < 0) return -1;
	if (ftruncate(fd, size) < 0) { close(fd); return -1; }
	return fd;
}

struct bufslot { struct ca *c; int i; };
static struct bufslot slots[2];
static void buf_release(void *data, struct wl_buffer *b) {
	struct bufslot *s = data;
	(void)b;
	s->c->b[s->i].busy = false;
}
static const struct wl_buffer_listener buf_listener = { buf_release };

static void free_buffers(struct ca *c) {
	for (int i = 0; i < 2; i++) {
		if (c->b[i].buf) { wl_buffer_destroy(c->b[i].buf); c->b[i].buf = NULL; }
		if (c->b[i].fd >= 0) {
			munmap(c->b[i].px, c->b[i].size);
			c->b[i].px = NULL; c->b[i].size = 0;
			close(c->b[i].fd); c->b[i].fd = -1;
		}
		c->b[i].busy = false;
	}
}
static bool make_buffers(struct ca *c) {
	free_buffers(c);
	if (c->W <= 0 || c->H <= 0 || c->W > INT_MAX / 4 || (size_t)c->W * c->H > INT_MAX / 4) return false;
	c->stride = c->W * 4;
	for (int i = 0; i < 2; i++) {
		int fd = shm_alloc(c->stride * c->H);
		if (fd < 0) { free_buffers(c); return false; }
		void *px = mmap(NULL, c->stride * c->H, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
		if (px == MAP_FAILED) { close(fd); free_buffers(c); return false; }
		struct wl_shm_pool *pool = wl_shm_create_pool(c->shm, fd, c->stride * c->H);
		struct wl_buffer *buf = wl_shm_pool_create_buffer(pool, 0,
			c->W, c->H, c->stride, WL_SHM_FORMAT_ARGB8888);
		wl_shm_pool_destroy(pool);
		slots[i].c = c; slots[i].i = i;
		wl_buffer_add_listener(buf, &buf_listener, &slots[i]);
		c->b[i].buf = buf; c->b[i].px = px; c->b[i].fd = fd; c->b[i].busy = false; c->b[i].size = (size_t)c->stride * c->H;
	}
	return true;
}

static size_t sim_count(const struct ca *c) { return (size_t)c->cols * c->rows * c->depth; }
static bool alloc_grid(struct ca *c) {
	free(c->grid); free(c->next); free(c->age);
	c->grid = calloc(sim_count(c), 1); c->next = calloc(sim_count(c), 1); c->age = calloc(sim_count(c), 1);
	return c->grid && c->next && c->age;
}
static bool sim_alloc(struct ca *c) {
	int cell = RULES[c->rule].cell;
	c->cols = c->volume ? 18 : c->W / cell + 2;
	c->rows = c->volume ? 18 : c->H / cell + 2;
	c->depth = c->volume ? 18 : 1;
	return alloc_grid(c);
}

static unsigned rnd(struct ca *c) { // xorshift32
	c->seed ^= c->seed << 13; c->seed ^= c->seed >> 17; c->seed ^= c->seed << 5;
	return c->seed;
}

static void stamp_glider(struct ca *c, int gx, int gy, int rot) {
	static const int g[4][5][2] = {
		{{1,0},{2,1},{0,2},{1,2},{2,2}}, {{0,0},{0,1},{0,2},{1,2},{2,1}},
		{{0,0},{1,0},{2,0},{0,1},{1,2}}, {{1,0},{2,0},{0,1},{1,1},{1,2}},
	};
	for (int i = 0; i < 5; i++) {
		int x = (gx + g[rot][i][0] + c->cols) % c->cols;
		int y = (gy + g[rot][i][1] + c->rows) % c->rows;
		c->grid[y * c->cols + x] = 1;
		c->age[y * c->cols + x] = 0;
	}
}

static void stamp_pulsar(struct ca *c, int gx, int gy) {
	static const int off[] = { 2, 3, 4, 8, 9, 10 };
	for (unsigned i = 0; i < 6; i++) for (unsigned j = 0; j < 6; j++) {
		int pts[4][2] = { {off[i],off[j]}, {-off[i],off[j]}, {off[i],-off[j]}, {-off[i],-off[j]} };
		for (int k = 0; k < 4; k++) {
			int x = (gx + pts[k][0] + c->cols * 4) % c->cols;
			int y = (gy + pts[k][1] + c->rows * 4) % c->rows;
			c->grid[y * c->cols + x] = 1;
			c->age[y * c->cols + x] = 0;
		}
	}
}

static void reseed(struct ca *c) {
	const struct rule *r = &RULES[c->rule];
	memset(c->grid, 0, sim_count(c));
	memset(c->age, 0, sim_count(c));
	bool sym = (rnd(c) % 3) == 0; // 4-way mirrored soup vs raw soup
	for (int y = 0; y < c->rows; y++) for (int x = 0; x < c->cols; x++) {
		float v;
		if (sym) {
			int mx = x < c->cols / 2 ? x : c->cols - 1 - x;
			int my = y < c->rows / 2 ? y : c->rows - 1 - y;
			unsigned h = ((unsigned)mx * 73856093u) ^ ((unsigned)my * 19349663u) ^ c->seed;
			v = (h % 10000) / 10000.0f;
		} else {
			v = (rnd(c) % 10000) / 10000.0f;
		}
		c->grid[y * c->cols + x] = v < r->density ? 1 : 0;
	}
	for (int i = 0; i < 3; i++)
		stamp_glider(c, rnd(c) % c->cols, rnd(c) % c->rows, rnd(c) % 4);
	if ((rnd(c) % 4) == 0 && c->cols > 30 && c->rows > 30)
		stamp_pulsar(c, rnd(c) % c->cols, rnd(c) % c->rows);
	if (c->volume) for (size_t i = 0; i < sim_count(c); i++)
		c->grid[i] = rnd(c) % 10000 < 1500;
	c->tick = 0;
	c->rule_start_ms = now_ms();
	c->last_check_tick = 0;
	c->still_count = 0;
	c->last_hash = 0;
	if (c->cs.on) cosmos_rebuild(c);
	fprintf(stderr, "cube-ca: rule=%s density=%.2f cell=%d fps=%d sym=%d\n",
		r->name, r->density, r->cell, r->fps, sym);
}

// A true periodic volume: each cell reads all 26 surrounding voxels. B5/S4,5.
static void sim_step_volume(struct ca *c) {
	for (int z = 0; z < c->depth; z++) for (int y = 0; y < c->rows; y++) for (int x = 0; x < c->cols; x++) {
		int n = 0;
		for (int dz = -1; dz <= 1; dz++) for (int dy = -1; dy <= 1; dy++) for (int dx = -1; dx <= 1; dx++) {
			if (!dx && !dy && !dz) continue;
			int zz = (z + dz + c->depth) % c->depth, yy = (y + dy + c->rows) % c->rows, xx = (x + dx + c->cols) % c->cols;
			n += c->grid[(zz * c->rows + yy) * c->cols + xx];
		}
		int i = (z * c->rows + y) * c->cols + x;
		c->next[i] = n == 5 || (c->grid[i] && n == 4);
		c->age[i] = c->next[i] ? (c->age[i] < 255 ? c->age[i] + 1 : 255) : 0;
	}
	uint8_t *swap = c->grid; c->grid = c->next; c->next = swap; c->tick++;
}
static void sim_step(struct ca *c) {
	if (c->volume) { sim_step_volume(c); return; }
	const struct rule *r = &RULES[c->rule];
	int cols = c->cols, rows = c->rows;
	for (int y = 0; y < rows; y++) {
		int yu = (y - 1 + rows) % rows, yd = (y + 1) % rows;
		for (int x = 0; x < cols; x++) {
			int xl = (x - 1 + cols) % cols, xr = (x + 1) % cols;
			int n = c->grid[yu*cols+xl] + c->grid[yu*cols+x] + c->grid[yu*cols+xr]
			      + c->grid[y*cols+xl]  + c->grid[y*cols+xr]
			      + c->grid[yd*cols+xl] + c->grid[yd*cols+x] + c->grid[yd*cols+xr];
			int alive = c->grid[y*cols+x];
			int out = alive ? ((r->surv >> n) & 1) : ((r->birth >> n) & 1);
			c->next[y*cols+x] = out;
			if (out) {
				int a = c->age[y*cols+x];
				c->age[y*cols+x] = a < 255 ? a + 1 : 255;
			} else {
				c->age[y*cols+x] = 0;
			}
		}
	}
	uint8_t *t = c->grid; c->grid = c->next; c->next = t;
	c->tick++;
}

static void pixel(struct ca *c, uint32_t *p, int x, int y, uint32_t col) {
	if (x >= 0 && y >= 0 && x < c->W && y < c->H) p[(size_t)y * c->W + x] = col;
}
// Scanline-filled convex quadrilateral. All three cube faces share vertices.
static void quad(struct ca *c, uint32_t *p, const int x[4], const int y[4], uint32_t col) {
	int top = y[0], bot = y[0];
	for (int i = 1; i < 4; i++) { if (y[i] < top) top = y[i]; if (y[i] > bot) bot = y[i]; }
	if (top < 0) top = 0;
	if (bot >= c->H) bot = c->H - 1;
	for (int py = top; py <= bot; py++) {
		int left = INT_MAX, right = INT_MIN;
		for (int i = 0; i < 4; i++) {
			int j = (i + 1) % 4;
			if (y[i] == y[j]) continue;
			if (py < (y[i] < y[j] ? y[i] : y[j]) || py > (y[i] > y[j] ? y[i] : y[j])) continue;
			int px = x[i] + (py - y[i]) * (x[j] - x[i]) / (y[j] - y[i]);
			if (px < left) left = px;
			if (px > right) right = px;
		}
		if (left < 0) left = 0;
		if (right >= c->W) right = c->W - 1;
		for (int px = left; px <= right; px++) p[(size_t)py * c->W + px] = col;
	}
}
static void cube(struct ca *c, uint32_t *p, int x, int y, int r, int age) {
	int q = r / 2, h = r;
	uint32_t top = age < 3 ? 0xFFFFFFFF : 0xFFF2F6FC;
	quad(c, p, (int[]){x-r,x,x,x-r}, (int[]){y,y+q,y+q+h,y+h}, 0xFF9CADC5);
	quad(c, p, (int[]){x,x+r,x+r,x}, (int[]){y+q,y,y+h,y+q+h}, 0xFFD4DFED);
	quad(c, p, (int[]){x,x+r,x,x-r}, (int[]){y-q,y,y+q,y}, top);
}
static bool make_stars(struct ca *c) {
	free(c->stars);
	c->stars = malloc((size_t)c->W * c->H * 4);
	if (!c->stars) return false;
	for (int y = 0; y < c->H; y++) for (int x = 0; x < c->W; x++) {
		int glow = 10 * (c->H - y) / c->H;
		c->stars[(size_t)y * c->W + x] = 0xFF000000u | ((4+glow/3)<<16) | ((7+glow/2)<<8) | (13+glow);
	}
	unsigned state = 0x6C756265;
	int n = c->W * c->H / 1800;
	for (int i = 0; i < n; i++) {
		state = state * 1664525u + 1013904223u; int x = state % c->W;
		state = state * 1664525u + 1013904223u; int y = state % c->H;
		unsigned v = 75 + (state >> 24) / 2;
		pixel(c, c->stars, x, y, 0xFF000000u | (v<<16) | ((v+10)<<8) | (v+25));
		if (i % 21 == 0) {
			pixel(c,c->stars,x-1,y,0xFF26344B); pixel(c,c->stars,x+1,y,0xFF26344B);
			pixel(c,c->stars,x,y-1,0xFF26344B); pixel(c,c->stars,x,y+1,0xFF26344B);
		}
	}
	return true;
}
struct projected_cell { int x, y, radius, age; double depth; };
static int compare_cell(const void *a, const void *b) {
	double d = ((const struct projected_cell *)a)->depth - ((const struct projected_cell *)b)->depth;
	return (d > 0) - (d < 0);
}
static void render_volume(struct ca *c, uint32_t *p) {
	if (c->slice >= 0) {
		int cell = (c->W < c->H ? c->W : c->H) / 20;
		int ox = (c->W - c->cols * cell) / 2, oy = (c->H - c->rows * cell) / 2;
		for (int y = 0; y < c->rows; y++) for (int x = 0; x < c->cols; x++) {
			int i = (c->slice * c->rows + y) * c->cols + x;
			if (c->edit) pixel(c, p, ox + x * cell + cell / 2, oy + y * cell + cell / 2, 0xFF8594A9);
			if (c->grid[i]) cube(c, p, ox + x * cell + cell / 2, oy + y * cell + cell / 4, cell / 2 - 2, c->age[i]);
		}
		return;
	}
	struct projected_cell cells[18 * 18 * 18]; int count = 0;
	double angle = c->yaw, co = cos(angle), si = sin(angle);
	double scale = (c->W < c->H ? c->W : c->H) / 39.0;
	for (int z = 0; z < c->depth; z++) for (int y = 0; y < c->rows; y++) for (int x = 0; x < c->cols; x++) {
		int i = (z * c->rows + y) * c->cols + x; if (!c->grid[i]) continue;
		double xx = x - (c->cols - 1) / 2.0, yy = y - (c->rows - 1) / 2.0, zz = z - (c->depth - 1) / 2.0;
		double rx = xx * co - zz * si, rz = xx * si + zz * co;
		cells[count++] = (struct projected_cell){(int)(c->W / 2 + rx * scale), (int)(c->H / 2 + (yy * .86 - rz * .5) * scale), (int)(scale * .42), c->age[i], rz + yy * .5};
	}
	qsort(cells, count, sizeof(cells[0]), compare_cell);
	for (int i = 0; i < count; i++) cube(c, p, cells[i].x, cells[i].y, cells[i].radius, cells[i].age);
}
static bool cosmos_control(struct ca *c, const char *verb, const char *arg);
#include "cube-cosmos.h"
static void render(struct ca *c, int bi) {
	uint32_t *p = c->b[bi].px;
	int cell = RULES[c->rule].cell;
	memcpy(p, c->stars, (size_t)c->W * c->H * 4);
	if (c->cs.on) { render_cosmos(c, p); return; }
	if (c->volume) { render_volume(c, p); return; }
	for (int gy = 0; gy < c->rows; gy++) for (int gx = 0; gx < c->cols; gx++) {
		if (c->edit) pixel(c, p, gx * cell + cell / 2, gy * cell + cell / 2, 0xFF667588);
		if (!c->grid[gy * c->cols + gx]) continue;
		int x = gx * cell + cell / 2;
		int y = gy * cell + cell / 4;
		cube(c, p, x, y, cell / 2 - 2, c->age[gy * c->cols + gx]);
	}
}

static void advance_rule(struct ca *c) {
	c->rule = (c->rule + 1) % NRULES;
	if (!sim_alloc(c)) { c->running = false; return; }
	reseed(c);
}

static double dur_scale(void) {
	const char *e = getenv("CUBE_CA_DUR_SCALE");
	return (e && atof(e) > 0) ? atof(e) : 1.0;
}
static void maybe_advance(struct ca *c) {
	if (c->manual || c->paused) return;
	if (c->volume) { if (c->tick && c->tick % 300 == 0) reseed(c); return; }
	const struct rule *r = &RULES[c->rule];
	uint64_t budget = (uint64_t)(r->dur_s * 1000 * dur_scale());
	if (now_ms() - c->rule_start_ms > budget) { advance_rule(c); return; }
	if (c->tick - c->last_check_tick < 30) return;
	c->last_check_tick = c->tick;
	uint32_t h = 2166136261u; long live = 0;
	for (int i = 0; i < c->cols * c->rows; i++) {
		if (c->grid[i]) { live++; h = (h ^ (uint32_t)i) * 16777619u; }
	}
	if (live == 0 || h == c->last_hash) {
		if (++c->still_count >= 2) { advance_rule(c); return; }
	} else {
		c->still_count = 0;
	}
	c->last_hash = h;
}

static bool thermal_pressure(void) {
	glob_t paths = {0}; bool hot = false;
	if (!glob("/sys/class/thermal/thermal_zone*/temp", 0, NULL, &paths)) {
		for (size_t i = 0; i < paths.gl_pathc; i++) {
			FILE *f = fopen(paths.gl_pathv[i], "r"); long temp = 0;
			if (f) { if (fscanf(f, "%ld", &temp) == 1 && temp >= 75000 && temp < 150000) hot = true; fclose(f); }
		}
	}
	globfree(&paths); return hot;
}
static int adaptive_fps(struct ca *c) {
	uint64_t now = now_ms();
	if (now - c->last_pressure_ms > 2000 || !c->last_pressure_ms) {
		c->last_pressure_ms = now; c->pressure = thermal_pressure();
		char directory[512];
		if (wc_state_directory(directory, sizeof(directory))) {
			char path[600]; snprintf(path, sizeof(path), "%s/accessibility.json", directory);
			struct wc_accessibility settings;
			if (wc_accessibility_load(path, &settings)) c->reduced_motion = settings.reduced_motion;
		}
		const char *runtime = getenv("XDG_RUNTIME_DIR");
		if (runtime) {
			char path[512]; snprintf(path, sizeof(path), "%s/whitecube-fullscreen", runtime);
			int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW); char v = 0;
			if (fd >= 0) { if (read(fd, &v, 1) == 1 && v == '1') c->pressure = true; close(fd); }
		}
	}
	return c->reduced_motion ? 1 : c->pressure ? 2 : c->paused ? (c->edit ? 8 : 2) : c->cs.on ? 20 : c->volume ? 8 : RULES[c->rule].fps;
}
static void arm_timer(struct ca *c) {
	struct itimerspec ts = {0};
	c->effective_fps = adaptive_fps(c);
	long ns = 1000000000L / c->effective_fps;
	ts.it_value.tv_sec = ns / 1000000000L;
	ts.it_value.tv_nsec = ns % 1000000000L;
	ts.it_interval = ts.it_value;
	timerfd_settime(c->tfd, 0, &ts, NULL);
}
static void on_tick(struct ca *c) {
	uint64_t exp = 0;
	if (read(c->tfd, &exp, sizeof(exp)) != sizeof(exp)) return;
	if (!c->configured) return;
	if (exp > 1) exp = 1; // Do not catch up by burning CPU when the compositor is busy.
	if (c->cs.on && !c->paused && !c->reduced_motion) { uint64_t now = now_ms(); cosmos_advance(c, c->cs.last_ms ? (now - c->cs.last_ms) / 1000.0 : 0.05); c->cs.last_ms = now; }
	else c->cs.last_ms = 0;
	for (uint64_t i = 0; i < exp; i++) {
		if (!c->paused && !c->reduced_motion) { sim_step(c); if (c->cs.on) cosmos_observe(c); }
		if (!c->reduced_motion) maybe_advance(c);
		if (!c->running) return;
	}
	arm_timer(c);
	for (int i = 0; i < 2; i++) if (!c->b[i].busy && c->b[i].buf) {
		render(c, i);
		wl_surface_attach(c->surf, c->b[i].buf, 0, 0);
		wl_surface_damage_buffer(c->surf, 0, 0, c->W, c->H);
		wl_surface_commit(c->surf);
		c->b[i].busy = true;
		break;
	}
	wl_display_flush(c->dpy);
}

static void layer_configure(void *data, struct zwlr_layer_surface_v1 *s, uint32_t serial, uint32_t w, uint32_t h) {
	struct ca *c = data;
	zwlr_layer_surface_v1_ack_configure(s, serial);
	if (!w || !h || w > 16384 || h > 16384) { c->running = false; return; }
	if ((int)w != c->W || (int)h != c->H) {
		c->W = w; c->H = h;
		if (!make_buffers(c) || !sim_alloc(c) || !make_stars(c)) {
			fprintf(stderr, "cube-ca: unable to allocate resized surface\n");
			c->running = false; return;
		}
		c->configured = true;
		reseed(c);
		if (c->cs.want_on) { c->cs.want_on = false; cosmos_set(c, true); }
		arm_timer(c);
		render(c, 0);
		wl_surface_attach(c->surf, c->b[0].buf, 0, 0);
		wl_surface_damage_buffer(c->surf, 0, 0, c->W, c->H);
		wl_surface_commit(c->surf);
		c->b[0].busy = true;
		fprintf(stderr, "cube-ca: mapped %dx%d\n", w, h);
	}
}
static void layer_closed(void *data, struct zwlr_layer_surface_v1 *s) {
	struct ca *c = data;
	(void)s;
	c->running = false;
}
static const struct zwlr_layer_surface_v1_listener layer_listener = { layer_configure, layer_closed };

#include "cube-field.h"

static void registry_global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t ver) {
	struct ca *c = data;
	if (!strcmp(iface, wl_compositor_interface.name))
		c->comp = wl_registry_bind(reg, name, &wl_compositor_interface, ver < 4 ? ver : 4);
	else if (!strcmp(iface, wl_seat_interface.name)) {
		c->seat = wl_registry_bind(reg, name, &wl_seat_interface, ver < 5 ? ver : 5);
		wl_seat_add_listener(c->seat, &seat_listener, c);
	} else if (!strcmp(iface, wl_shm_interface.name))
		c->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name))
		c->layer_shell = wl_registry_bind(reg, name, &zwlr_layer_shell_v1_interface, ver < 4 ? ver : 4);
}
static void registry_remove(void *data, struct wl_registry *reg, uint32_t name) {
	(void)data; (void)reg; (void)name;
}
static const struct wl_registry_listener reg_listener = { registry_global, registry_remove };

// Offscreen cosmos surface for tests and previews: a warmed-up field, star background, no Wayland.
static bool cosmos_offscreen(struct ca *c, int w, int h, unsigned seed, int warm) {
	memset(c, 0, sizeof(*c));
	c->W = w; c->H = h; c->seed = seed; c->slice = -1; c->configured = true; c->cs.speed = 1;
	if (!sim_alloc(c) || !make_stars(c)) return false;
	c->b[0].px = malloc((size_t)w * h * 4); if (!c->b[0].px) return false;
	reseed(c); c->cs.on = true; c->cs.auto_zoom = true; c->cs.dir = 1;
	for (int i = 0; i < warm; i++) { sim_step(c); cosmos_observe(c); }
	cosmos_rebuild(c);
	return true;
}
static void cosmos_offscreen_free(struct ca *c) { cos_free(c); free(c->b[0].px); free(c->stars); free(c->grid); free(c->next); free(c->age); }
static size_t lit_pixels(const uint32_t *px, size_t n) { size_t k = 0; for (size_t i = 0; i < n; i++) if ((px[i] & 0xFFFFFF) > 0x202830) k++; return k; }
static int cosmos_self_test(void) {
	// Crossfade weights must always sum to one so brightness never dips mid-transition.
	for (double z = 0; z <= COS_PHASES - 1; z += .05) { double sum = 0, s, w; for (int k = 0; k < COS_PHASES; k++) { cos_view(z, k, &s, &w); sum += w; } if (fabs(sum - 1) > 1e-6) return 1; }
	struct ca c; if (!cosmos_offscreen(&c, 320, 200, 7, 120)) return 2;
	// Automatic journey: reaches the last phase in order, dwells, then rewinds and starts over.
	double prev = 0, peak = 0; bool rewound = false;
	for (int i = 0; i < 20000 && !rewound; i++) {
		cosmos_advance(&c, .1); if (c.cs.zoom + 1e-9 < prev - .5) return 3; // no sudden jumps
		if (c.cs.zoom > peak) peak = c.cs.zoom;
		if (peak >= COS_PHASES - 1 && c.cs.zoom <= 0) rewound = true;
		prev = c.cs.zoom;
	}
	if (peak < COS_PHASES - 1 || !rewound) return 4;
	// Every phase draws something, in bounds, at integer and fractional zooms.
	for (double z = 0; z <= COS_PHASES - 1; z += .5) {
		c.cs.zoom = z; c.cs.t = 30; c.cs.uni_t = 20; memcpy(c.b[0].px, c.stars, (size_t)c.W * c.H * 4); render_cosmos(&c, c.b[0].px);
		if (lit_pixels(c.b[0].px, (size_t)c.W * c.H) < 40) return 5;
	}
	// Control commands, including rejection of bad input and of edit/3d while the cosmos runs.
	c.cs.on = false; c.configured = true;
	if (!cosmos_control(&c, "cosmos", "on") || !c.cs.on) return 6;
	if (!cosmos_control(&c, "phase", "land") || c.cs.auto_zoom || c.cs.target != 2) return 7;
	if (cosmos_control(&c, "phase", "nowhere") || cosmos_control(&c, "zoom", "9") || cosmos_control(&c, "speed", "100") || cosmos_control(&c, "zoom", "nan")) return 8;
	if (!cosmos_control(&c, "zoom", "2.5") || fabs(c.cs.zoom - 2.5) > 1e-9 || !cosmos_control(&c, "speed", "2") || c.cs.speed != 2) return 9;
	if (!apply_control(&c, "phase 4") || apply_control(&c, "edit") || apply_control(&c, "3d")) return 10;
	if (!apply_control(&c, "cosmos off") || c.cs.on) return 11;
	cosmos_offscreen_free(&c);
	return 0;
}
static int preview_zoom(const char *path, double zoom) {
	struct ca c; if (!cosmos_offscreen(&c, 1440, 900, 0x1234567, 400)) return 1;
	c.cs.zoom = zoom; c.cs.t = 40; c.cs.uni_t = 30;
	for (int i = 0; i < 400; i++) { c.cs.t = 40; cosmos_advance(&c, 0); }
	for (int i = 0; zoom > 3.3 && zoom < 4.8 && i < 6000; i++) { c.cs.zoom = zoom; cosmos_advance(&c, .02); }
	c.cs.zoom = zoom;
	memcpy(c.b[0].px, c.stars, (size_t)c.W * c.H * 4); render_cosmos(&c, c.b[0].px);
	if (getenv("CUBE_CA_BENCH")) { // per-frame cost of this zoom, including the background copy
		uint64_t t0 = now_ms(); for (int i = 0; i < 20; i++) { c.cs.t += .05; memcpy(c.b[0].px, c.stars, (size_t)c.W * c.H * 4); render_cosmos(&c, c.b[0].px); }
		fprintf(stderr, "zoom %.2f: %.1f ms/frame\n", zoom, (now_ms() - t0) / 20.0);
	}
	FILE *f = fopen(path, "wb"); if (!f) { perror(path); return 1; }
	fprintf(f, "P6\n%d %d\n255\n", c.W, c.H);
	for (int i = 0; i < c.W * c.H; i++) { uint32_t v = ((uint32_t *)c.b[0].px)[i]; unsigned char rgb[3] = {v >> 16, v >> 8, v}; if (fwrite(rgb, 1, 3, f) != 3) { fclose(f); return 1; } }
	int rc = fclose(f); cosmos_offscreen_free(&c); return rc != 0;
}
static int self_test(void) {
	struct ca c = {.W = 192, .H = 192, .seed = 1};
	if (!sim_alloc(&c)) return 1;
	// A blinker crossing the toroidal seam must return after two generations.
	int w = c.cols, h = c.rows;
	c.grid[(h-1)*w] = c.grid[0] = c.grid[w] = 1;
	sim_step(&c);
	if (!c.grid[w-1] || !c.grid[0] || !c.grid[1]) return 2;
	int live = 0;
	for (int i = 0; i < w*h; i++) live += c.grid[i];
	if (live != 3) return 3;
	sim_step(&c);
	if (!c.grid[(h-1)*w] || !c.grid[0] || !c.grid[w]) return 4;
	for (unsigned r = 0; r < NRULES; r++) {
		c.rule = r;
		if (!sim_alloc(&c)) return 5;
		reseed(&c);
		for (int step = 0; step < 100; step++) sim_step(&c);
		for (int i = 0; i < c.cols*c.rows; i++) if (c.grid[i] > 1) return 6;
	}
	free(c.grid); free(c.next); free(c.age);
	if (field_self_test()) return 7;
	{ int rc = cosmos_self_test(); if (rc) { fprintf(stderr, "cube-ca: cosmos self-test failed (%d)\n", rc); return 10 + rc; } }
	puts("cube-ca: 2D toroidal oscillator, six rules, 26-neighbour volume, editing, seed replay and cosmic zoom passed");
	return 0;
}
static int preview(const char *path, bool volume) {
	struct ca c = {.W=1440, .H=900, .seed=0x1234567, .volume=volume, .slice=-1, .yaw=.65};
	if (!sim_alloc(&c) || !make_stars(&c)) return 1;
	reseed(&c);
	for (int i = 0; i < 18; i++) sim_step(&c);
	c.b[0].px = malloc((size_t)c.W*c.H*4);
	if (!c.b[0].px) return 1;
	render(&c, 0);
	FILE *f = fopen(path, "wb");
	if (!f) { perror(path); return 1; }
	fprintf(f, "P6\n%d %d\n255\n", c.W, c.H);
	for (int i = 0; i < c.W*c.H; i++) {
		uint32_t v = ((uint32_t *)c.b[0].px)[i];
		unsigned char rgb[3] = {v>>16, v>>8, v};
		if (fwrite(rgb, 1, 3, f) != 3) { fclose(f); return 1; }
	}
	int rc = fclose(f);
	free(c.b[0].px); free(c.stars); free(c.grid); free(c.next); free(c.age);
	return rc != 0;
}
int main(int argc, char **argv) {
	if (argc >= 3 && !strcmp(argv[1], "--control")) {
		char command[256] = {0}; size_t len = 0;
		for (int i = 2; i < argc; i++) { int n = snprintf(command + len, sizeof(command) - len, "%s%s", i == 2 ? "" : " ", argv[i]);
			if (n < 0 || (size_t)n >= sizeof(command) - len) return 1;
			len += n; }
		return send_control(command);
	}
	if (argc == 2 && !strcmp(argv[1], "--menu")) return control_menu();
	if (argc == 2 && !strcmp(argv[1], "--self-test")) return self_test();
	if (argc == 3 && !strcmp(argv[1], "--preview")) return preview(argv[2], false);
	if (argc == 3 && !strcmp(argv[1], "--preview-3d")) return preview(argv[2], true);
	if (argc == 4 && !strcmp(argv[1], "--preview-zoom")) return preview_zoom(argv[3], atof(argv[2]));
	if (argc != 1) { fprintf(stderr, "Usage: cube-ca [--menu | --control COMMAND [ARG] | --self-test | --preview output.ppm | --preview-3d output.ppm | --preview-zoom 0..6 output.ppm]\n"); return 1; }
	struct ca c;
	memset(&c, 0, sizeof(c));
	c.b[0].fd = -1; c.b[1].fd = -1; c.control_fd = -1; c.slice = -1; c.yaw = .65;
	c.tfd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC | TFD_NONBLOCK);
	if (c.tfd < 0) { perror("cube-ca: timerfd"); return 1; }
	c.seed = ((unsigned)time(NULL) ^ (unsigned)getpid()) | 1;
	cosmos_config(&c);
	c.running = true;
	c.dpy = wl_display_connect(NULL);
	if (!c.dpy) { fprintf(stderr, "cube-ca: no wayland display\n"); return 1; }
	c.reg = wl_display_get_registry(c.dpy);
	wl_registry_add_listener(c.reg, &reg_listener, &c);
	wl_display_roundtrip(c.dpy);
	if (!c.comp || !c.shm || !c.layer_shell) {
		fprintf(stderr, "cube-ca: missing globals\n");
		return 1;
	}
	c.surf = wl_compositor_create_surface(c.comp);
	c.layer = zwlr_layer_shell_v1_get_layer_surface(c.layer_shell, c.surf, NULL,
		ZWLR_LAYER_SHELL_V1_LAYER_BACKGROUND, "cube-ca-wallpaper");
	zwlr_layer_surface_v1_set_size(c.layer, 0, 0);
	zwlr_layer_surface_v1_set_anchor(c.layer,
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
		ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
	zwlr_layer_surface_v1_set_exclusive_zone(c.layer, -1);
	zwlr_layer_surface_v1_add_listener(c.layer, &layer_listener, &c);
	update_input(&c);
	wl_display_flush(c.dpy);
	c.control_fd = open_control(&c);
	int dfd = wl_display_get_fd(c.dpy);
	struct pollfd fds[3] = {
		{ .fd = dfd, .events = POLLIN },
		{ .fd = c.tfd, .events = POLLIN },
		{ .fd = c.control_fd, .events = POLLIN },
	};
	while (c.running) {
		while (wl_display_prepare_read(c.dpy) != 0) {
			if (wl_display_dispatch_pending(c.dpy) < 0) { c.running = false; break; }
		}
		if (!c.running) break;
		fds[0].events = POLLIN;
		if (wl_display_flush(c.dpy) < 0 && errno == EAGAIN) fds[0].events |= POLLOUT;
		int ready = poll(fds, 3, -1);
		if (ready < 0) {
			wl_display_cancel_read(c.dpy);
			if (errno == EINTR) continue;
			break;
		}
		if (fds[0].revents & POLLIN) {
			if (wl_display_read_events(c.dpy) < 0) break;
		} else wl_display_cancel_read(c.dpy);
		if (fds[0].revents & (POLLHUP | POLLERR | POLLNVAL)) break;
		if (wl_display_dispatch_pending(c.dpy) < 0 || !c.running) break;
		if (fds[2].revents & POLLIN) receive_control(&c);
		if (fds[1].revents & POLLIN) on_tick(&c);
	}
	free_buffers(&c); free(c.stars); free(c.grid); free(c.next); free(c.age); cos_free(&c);
	close(c.tfd);
	if (c.control_fd >= 0) { close(c.control_fd); unlink(c.control_path); }
	if (c.pointer) wl_pointer_destroy(c.pointer);
	if (c.seat) wl_seat_destroy(c.seat);
	zwlr_layer_surface_v1_destroy(c.layer); wl_surface_destroy(c.surf);
	zwlr_layer_shell_v1_destroy(c.layer_shell); wl_shm_destroy(c.shm);
	wl_compositor_destroy(c.comp); wl_registry_destroy(c.reg);
	wl_display_disconnect(c.dpy);
	return 0;
}
