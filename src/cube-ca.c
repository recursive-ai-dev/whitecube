// cube-ca: animated cellular-automata wallpaper for whitecube.
// Wayland-native layer-shell (background) client, SHM software rendering.
// Loops the simulation with different variables: rule, density, cell size,
// tick rate, seed symmetry. White cubes evolve over a star field.
// Each cube is a live cell in the actual toroidal simulation, not a video.
// One process draws one output; started bare, cube-ca supervises one child per output.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <signal.h>
#include <dirent.h>
#include <sys/mman.h>
#include <sys/timerfd.h>
#include <sys/signalfd.h>
#include <sys/prctl.h>
#include <sys/random.h>
#include <sys/wait.h>
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
#define FADE_MS 1200
#define MAX_DAMAGE 256
#define MAX_OUTPUTS 16

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
	unsigned seed; // fixed for a cosmos session so rule changes do not redraw the continents
	float *dens, *hmap, *cmap, dmean, dscale;
	int dw, dh;
	uint32_t *layer; size_t layer_n;
	float *gas; size_t gas_n;
	struct cosmos_star stars[COS_STARS]; int nstars, star_next;
	float *wx, *wy, *wb, nodex[36], nodey[36]; int nnodes, nedges, edge_a[72], edge_b[72];
	uint64_t last_ms;
};

struct ca;
struct output_ref {
	struct ca *c;
	struct wl_output *wl;
	uint32_t global, ver;
	int scale, pending_scale;
	char name[64];
	pid_t pid; uint64_t started_ms; int crashes;
};

struct rect { int x, y, w, h; };

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
	struct wl_callback *frame_cb;
	struct {
		struct wl_buffer *buf; void *px; int fd; bool busy; size_t size;
		uint8_t *shown; size_t shown_n; bool valid; int cols, rows, cell; // cell states this buffer currently shows
	} b[2];
	uint32_t *stars;
	int W, H, stride;     // buffer pixels
	int lw, lh, scale;    // logical surface size and the buffer scale in use
	int out_scale, comp_ver, last_bi;
	bool configured, running, dirty, waiting_frame, parked;
	int tfd, control_fd;
	char control_path[108];
	bool volume, paused, edit, manual, pressure, reduced_motion, on_battery, spin;
	int depth, slice, draw_button, effective_fps;
	double yaw, pointer_x, pointer_y;
	uint64_t last_pressure_ms;
	// outputs: the one this process draws, or all of them when supervising
	struct output_ref outs[MAX_OUTPUTS]; int nouts;
	struct wl_output *target, *entered;
	char want_output[64];
	bool supervisor;
	// sim
	uint8_t *grid, *next, *age, *state; size_t state_n;
	int cols, rows;
	unsigned rule; // NRULES selects the custom rule
	struct rule custom; char custom_name[24];
	uint64_t active_ms, last_tick_ms, last_check_tick;
	uint64_t tick;
	uint32_t last_hash; int still_count;
	unsigned seed, base_seed; // seed is the generator state; base_seed recreates the current soup
	uint32_t *fade_px; uint64_t fade_start_ms;
	struct cosmos cs;
};

static void cosmos_rebuild(struct ca *c);
static void update_input(struct ca *c);
static bool apply_geometry(struct ca *c);
static void present(struct ca *c);

static uint64_t now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
static const struct rule *rule_of(const struct ca *c) { return c->rule < NRULES ? &RULES[c->rule] : &c->custom; }
static int scale_of(const struct ca *c) { return c->scale > 1 ? c->scale : 1; }
static int cell_px(const struct ca *c) { return rule_of(c)->cell * scale_of(c); }

// "B36/S23", either order, any case, slash optional. B0 is refused: it would strobe the whole screen.
static bool parse_rule(const char *text, uint16_t *birth, uint16_t *surv, char *name, size_t size) {
	uint16_t m[2] = {0, 0}; bool seen[2] = {false, false};
	for (const char *p = text; *p;) {
		int k = (*p == 'B' || *p == 'b') ? 0 : (*p == 'S' || *p == 's') ? 1 : -1;
		if (k < 0 || seen[k]) return false;
		seen[k] = true; p++;
		while (*p >= '0' && *p <= '8') { m[k] |= 1u << (*p - '0'); p++; }
		if (*p == '/') p++;
	}
	if (!seen[0] || !seen[1] || !m[0] || (m[0] & 1)) return false;
	char out[24]; size_t n = 0; out[n++] = 'B';
	for (int d = 0; d <= 8; d++) if (m[0] >> d & 1) out[n++] = '0' + d;
	out[n++] = '/'; out[n++] = 'S';
	for (int d = 0; d <= 8; d++) if (m[1] >> d & 1) out[n++] = '0' + d;
	out[n] = 0;
	if (n >= size) return false;
	memcpy(name, out, n + 1); *birth = m[0]; *surv = m[1];
	return true;
}
static void set_custom_rule(struct ca *c, uint16_t birth, uint16_t surv) {
	size_t n = 0; char *name = c->custom_name; name[n++] = 'B';
	for (int d = 0; d <= 8; d++) if (birth >> d & 1) name[n++] = '0' + d;
	name[n++] = '/'; name[n++] = 'S';
	for (int d = 0; d <= 8; d++) if (surv >> d & 1) name[n++] = '0' + d;
	name[n] = 0;
	c->custom = (struct rule){ c->custom_name, birth, surv, 0.20f, 24, 12, 45 };
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
		c->b[i].busy = false; c->b[i].valid = false;
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
		// Opaque format: the compositor never has to blend the wallpaper.
		struct wl_buffer *buf = wl_shm_pool_create_buffer(pool, 0,
			c->W, c->H, c->stride, WL_SHM_FORMAT_XRGB8888);
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
	int cell = cell_px(c);
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

// The whole soup follows from one number, which is logged and reported so a good one can be replayed with 'seed N'.
static void reseed_from(struct ca *c, unsigned seed) {
	const struct rule *r = rule_of(c);
	c->base_seed = c->seed = seed ? seed : 1;
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
	c->active_ms = 0;
	c->last_check_tick = 0;
	c->still_count = 0;
	c->last_hash = 0;
	c->dirty = true;
	fprintf(stderr, "cube-ca: rule=%s seed=%u density=%.2f cell=%d fps=%d sym=%d\n",
		c->volume ? "3D B5/S4,5" : r->name, c->base_seed, r->density, r->cell, r->fps, sym);
}
static void reseed(struct ca *c) { reseed_from(c, rnd(c)); }

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
	const struct rule *r = rule_of(c);
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

// Keeps the running world through a geometry change when its size is unchanged (or it was painted/replayed).
static bool sim_refit(struct ca *c) {
	int oc = c->cols, orow = c->rows, od = c->depth; uint8_t *og = c->grid, *oa = c->age;
	free(c->next); c->grid = c->next = c->age = NULL;
	if (!sim_alloc(c)) { free(og); free(oa); return false; }
	bool keep = og && od == c->depth && (c->manual || (oc == c->cols && orow == c->rows));
	if (keep) {
		int w = oc < c->cols ? oc : c->cols, h = orow < c->rows ? orow : c->rows;
		for (int z = 0; z < c->depth; z++) for (int y = 0; y < h; y++) {
			memcpy(c->grid + ((size_t)z * c->rows + y) * c->cols, og + ((size_t)z * orow + y) * oc, w);
			memcpy(c->age + ((size_t)z * c->rows + y) * c->cols, oa + ((size_t)z * orow + y) * oc, w);
		}
	}
	free(og); free(oa);
	if (!keep) reseed(c);
	return true;
}

static void pixel(struct ca *c, uint32_t *p, int x, int y, uint32_t col) {
	if (x >= 0 && y >= 0 && x < c->W && y < c->H) p[(size_t)y * c->W + x] = col;
}
static uint32_t mix_rgb(uint32_t a, uint32_t b, int t) { // t: 0..256 towards b
	uint32_t rb = ((a & 0xFF00FF) * (256 - t) + (b & 0xFF00FF) * t) >> 8 & 0xFF00FF;
	uint32_t g = ((a & 0xFF00) * (256 - t) + (b & 0xFF00) * t) >> 8 & 0xFF00;
	return 0xFF000000u | rb | g;
}
// Newborn (white), then eight steps that cool toward steel blue as a cell survives: still lifes read differently from churn.
static int age_level(int age) { return age < 3 ? 0 : age >= 59 ? 8 : 1 + (age - 3) / 8; }
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
	int q = r / 2, h = r, lv = age_level(age);
	uint32_t top = lv ? mix_rgb(0xFFF2F6FC, 0xFFAFC3E2, (lv - 1) * 256 / 7) : 0xFFFFFFFF;
	quad(c, p, (int[]){x-r,x,x,x-r}, (int[]){y,y+q,y+q+h,y+h}, mix_rgb(0xFF9CADC5, 0xFF6C7FA2, lv * 32));
	quad(c, p, (int[]){x,x+r,x+r,x}, (int[]){y+q,y,y+h,y+q+h}, mix_rgb(0xFFD4DFED, 0xFF9AAED0, lv * 32));
	quad(c, p, (int[]){x,x+r,x,x-r}, (int[]){y-q,y,y+q,y}, top);
}
// The star field is laid out in logical pixels, so it looks the same at any buffer scale.
static bool make_stars(struct ca *c) {
	free(c->stars);
	c->stars = malloc((size_t)c->W * c->H * 4);
	if (!c->stars) return false;
	int s = scale_of(c), lw = c->W / s, lh = c->H / s;
	for (int y = 0; y < c->H; y++) for (int x = 0; x < c->W; x++) {
		int glow = 10 * (c->H - y) / c->H;
		c->stars[(size_t)y * c->W + x] = 0xFF000000u | ((4+glow/3)<<16) | ((7+glow/2)<<8) | (13+glow);
	}
	if (lw < 1 || lh < 1) return true;
#define STAR(X, Y, COL) for (int sy = 0; sy < s; sy++) for (int sx = 0; sx < s; sx++) pixel(c, c->stars, (X) * s + sx, (Y) * s + sy, (COL))
	unsigned state = 0x6C756265;
	int n = lw * lh / 1800;
	for (int i = 0; i < n; i++) {
		state = state * 1664525u + 1013904223u; int x = state % lw;
		state = state * 1664525u + 1013904223u; int y = state % lh;
		unsigned v = 75 + (state >> 24) / 2;
		STAR(x, y, 0xFF000000u | (v<<16) | ((v+10)<<8) | (v+25));
		if (i % 21 == 0) {
			STAR(x-1, y, 0xFF26344B); STAR(x+1, y, 0xFF26344B);
			STAR(x, y-1, 0xFF26344B); STAR(x, y+1, 0xFF26344B);
		}
	}
#undef STAR
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
// Everything a 2D cell draws stays inside its own cell box, so cells can be redrawn one at a time.
static void draw_cell(struct ca *c, uint32_t *p, int gx, int gy, int cell) {
	int s = scale_of(c); size_t i = (size_t)gy * c->cols + gx;
	if (c->edit) for (int d = 0; d < s * s; d++) pixel(c, p, gx * cell + cell / 2 + d % s, gy * cell + cell / 2 + d / s, 0xFF667588);
	if (c->grid[i]) cube(c, p, gx * cell + cell / 2, gy * cell + cell / 4, cell / 2 - 2 * s, c->age[i]);
}
static void redraw_cell(struct ca *c, uint32_t *p, int gx, int gy, int cell) {
	int x0 = gx * cell, y0 = gy * cell, x1 = x0 + cell, y1 = y0 + cell;
	if (x1 > c->W) x1 = c->W;
	if (y1 > c->H) y1 = c->H;
	if (x0 >= x1 || y0 >= y1) return;
	for (int y = y0; y < y1; y++) memcpy(p + (size_t)y * c->W + x0, c->stars + (size_t)y * c->W + x0, (size_t)(x1 - x0) * 4);
	draw_cell(c, p, gx, gy, cell);
}
// Full repaint of buffer bi.
static void render(struct ca *c, int bi) {
	uint32_t *p = c->b[bi].px;
	int cell = cell_px(c);
	memcpy(p, c->stars, (size_t)c->W * c->H * 4);
	if (c->cs.on) { render_cosmos(c, p); return; }
	if (c->volume) { render_volume(c, p); return; }
	for (int gy = 0; gy < c->rows; gy++) for (int gx = 0; gx < c->cols; gx++) draw_cell(c, p, gx, gy, cell);
}
static uint8_t cell_state(const struct ca *c, size_t i) { return (c->grid[i] ? 1 + age_level(c->age[i]) : 0) | (c->edit ? 0x80 : 0); }
static void blend_fade(struct ca *c, uint32_t *p) {
	int t = (int)((now_ms() - c->fade_start_ms) * 256 / FADE_MS); if (t > 256) t = 256;
	t = t * t * (768 - 2 * t) >> 16; // smoothstep
	for (size_t i = 0, n = (size_t)c->W * c->H; i < n; i++) p[i] = mix_rgb(c->fade_px[i], p[i], t);
}
static void start_fade(struct ca *c) { // crossfade from the frame on screen to whatever comes next
	if (c->reduced_motion || c->last_bi < 0 || !c->b[c->last_bi].px || !c->b[c->last_bi].buf) return;
	size_t n = (size_t)c->W * c->H;
	if (!c->fade_px) c->fade_px = malloc(n * 4);
	if (!c->fade_px) return;
	memcpy(c->fade_px, c->b[c->last_bi].px, n * 4);
	c->fade_start_ms = now_ms(); c->dirty = true;
}
// Draws buffer bi and fills dmg with what changed since the last committed frame.
// Returns the rectangle count (0: nothing visible changed) or -1 for the whole surface.
#define SAME_GEOMETRY(B) ((B).valid && (B).cols == c->cols && (B).rows == c->rows && (B).cell == cell)
static int render_frame(struct ca *c, int bi, struct rect *dmg) {
	if (c->fade_px && now_ms() - c->fade_start_ms >= FADE_MS) { free(c->fade_px); c->fade_px = NULL; }
	bool flat = !c->cs.on && !c->volume && c->depth == 1;
	int cell = cell_px(c); size_t n = (size_t)c->cols * c->rows;
	if (flat && c->state_n != n) { free(c->state); c->state = malloc(n); c->state_n = c->state ? n : 0; flat = c->state != NULL; }
	if (flat && c->b[bi].shown_n != n) {
		free(c->b[bi].shown); c->b[bi].shown = malloc(n); c->b[bi].shown_n = c->b[bi].shown ? n : 0; c->b[bi].valid = false;
		flat = c->b[bi].shown != NULL;
	}
	if (!flat || c->fade_px) {
		render(c, bi);
		if (c->fade_px) blend_fade(c, c->b[bi].px);
		c->b[bi].valid = false;
		return -1;
	}
	for (size_t i = 0; i < n; i++) c->state[i] = cell_state(c, i);
	int count = -1;
	if (c->last_bi >= 0 && SAME_GEOMETRY(c->b[c->last_bi])) {
		const uint8_t *was = c->b[c->last_bi].shown; count = 0;
		for (int gy = 0; gy < c->rows && count >= 0; gy++) {
			const uint8_t *now = c->state + (size_t)gy * c->cols, *old = was + (size_t)gy * c->cols;
			for (int gx = 0; gx < c->cols; gx++) {
				if (now[gx] == old[gx]) continue;
				int start = gx; while (gx + 1 < c->cols && now[gx + 1] != old[gx + 1]) gx++;
				if (start * cell >= c->W || gy * cell >= c->H) continue;
				if (count == MAX_DAMAGE) { count = -1; break; }
				dmg[count++] = (struct rect){ start * cell, gy * cell, (gx - start + 1) * cell, cell };
			}
		}
	}
	if (SAME_GEOMETRY(c->b[bi])) {
		for (size_t i = 0; i < n; i++) if (c->state[i] != c->b[bi].shown[i]) redraw_cell(c, c->b[bi].px, (int)(i % c->cols), (int)(i / c->cols), cell);
	} else render(c, bi);
	memcpy(c->b[bi].shown, c->state, n);
	c->b[bi].valid = true; c->b[bi].cols = c->cols; c->b[bi].rows = c->rows; c->b[bi].cell = cell;
	return count;
}
#undef SAME_GEOMETRY

static void advance_rule(struct ca *c) {
	c->rule = c->rule >= NRULES ? 0 : (c->rule + 1) % NRULES;
	if (!sim_alloc(c)) { c->running = false; return; }
	reseed(c);
}

static double dur_scale(void) {
	static double scale = -1;
	if (scale < 0) { const char *e = getenv("CUBE_CA_DUR_SCALE"); scale = (e && atof(e) > 0) ? atof(e) : 1.0; }
	return scale;
}
// Rule budgets count simulated time only: pauses, hidden screens and reduced motion do not use them up.
static void maybe_advance(struct ca *c) {
	if (c->manual || c->paused) return;
	if (c->volume) { if (c->tick >= 300) { start_fade(c); reseed(c); return; } }
	else if (c->active_ms > (uint64_t)(rule_of(c)->dur_s * 1000 * dur_scale())) { start_fade(c); advance_rule(c); return; }
	if (c->tick - c->last_check_tick < 30) return;
	c->last_check_tick = c->tick;
	uint32_t h = 2166136261u; long live = 0; size_t n = sim_count(c);
	for (size_t i = 0; i < n; i++) {
		if (c->grid[i]) { live++; h = (h ^ (uint32_t)i) * 16777619u; }
	}
	c->still_count = live == 0 || h == c->last_hash ? c->still_count + 1 : 0;
	c->last_hash = h;
	if (live == 0 || c->still_count >= 2) { start_fade(c); if (c->volume) reseed(c); else advance_rule(c); }
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
static bool read_line(const char *dir, const char *name, char *out, size_t size) {
	char path[PATH_MAX];
	if (snprintf(path, sizeof(path), "%s/%s", dir, name) >= (int)sizeof(path)) return false;
	FILE *f = fopen(path, "r"); if (!f) return false;
	bool ok = fgets(out, size, f) != NULL; fclose(f);
	if (ok) out[strcspn(out, "\n")] = 0;
	return ok;
}
// A system battery present and no mains or USB supply online. Mice and headsets (scope Device) do not count.
static bool on_battery(void) {
	glob_t g = {0}; bool battery = false, mains = false;
	if (!glob("/sys/class/power_supply/*", GLOB_ONLYDIR, NULL, &g)) for (size_t i = 0; i < g.gl_pathc; i++) {
		char type[32], value[32];
		if (!read_line(g.gl_pathv[i], "type", type, sizeof(type))) continue;
		if (!strcmp(type, "Battery")) { if (!read_line(g.gl_pathv[i], "scope", value, sizeof(value)) || strcmp(value, "Device")) battery = true; }
		else if ((!strcmp(type, "Mains") || !strncmp(type, "USB", 3)) && read_line(g.gl_pathv[i], "online", value, sizeof(value)) && atoi(value) > 0) mains = true;
	}
	globfree(&g);
	return battery && !mains;
}
static int adaptive_fps(struct ca *c) {
	uint64_t now = now_ms();
	if (now - c->last_pressure_ms > 2000 || !c->last_pressure_ms) {
		c->last_pressure_ms = now; c->pressure = thermal_pressure(); c->on_battery = on_battery();
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
	int fps = c->reduced_motion ? 1 : c->pressure ? 2 : c->paused ? (c->edit ? 8 : 2) : c->cs.on ? 20 : c->volume ? 8 : rule_of(c)->fps;
	if (c->on_battery && fps > 2) fps = fps * 2 / 3 > 2 ? fps * 2 / 3 : 2;
	return fps;
}
static void arm_timer(struct ca *c) {
	if (c->parked) return; // the next frame callback re-arms
	struct itimerspec ts = {0};
	c->effective_fps = adaptive_fps(c);
	long ns = 1000000000L / c->effective_fps;
	ts.it_value.tv_sec = ns / 1000000000L;
	ts.it_value.tv_nsec = ns % 1000000000L;
	ts.it_interval = ts.it_value;
	timerfd_settime(c->tfd, 0, &ts, NULL);
}
static void frame_done(void *data, struct wl_callback *cb, uint32_t ms) {
	struct ca *c = data;
	wl_callback_destroy(cb); c->frame_cb = NULL; c->waiting_frame = false;
	if (c->parked) { c->parked = false; arm_timer(c); }
}
static const struct wl_callback_listener frame_listener = { frame_done };
static void present(struct ca *c) {
	for (int i = 0; i < 2; i++) {
		if (c->b[i].busy || !c->b[i].buf) continue;
		struct rect dmg[MAX_DAMAGE];
		int n = render_frame(c, i, dmg);
		c->dirty = false;
		if (n == 0) return; // nothing visible changed: leave the compositor idle
		wl_surface_attach(c->surf, c->b[i].buf, 0, 0);
		if (c->comp_ver >= 3) wl_surface_set_buffer_scale(c->surf, scale_of(c));
		if (n < 0) wl_surface_damage_buffer(c->surf, 0, 0, c->W, c->H);
		else for (int k = 0; k < n; k++) wl_surface_damage_buffer(c->surf, dmg[k].x, dmg[k].y, dmg[k].w, dmg[k].h);
		if (!c->frame_cb) { c->frame_cb = wl_surface_frame(c->surf); wl_callback_add_listener(c->frame_cb, &frame_listener, c); }
		c->waiting_frame = true;
		wl_surface_commit(c->surf);
		c->b[i].busy = true; c->last_bi = i;
		return;
	}
}
static void on_tick(struct ca *c) {
	uint64_t exp = 0;
	if (read(c->tfd, &exp, sizeof(exp)) != sizeof(exp)) return;
	if (!c->configured) return;
	if (c->waiting_frame) { // the last frame is not on screen yet (hidden, covered or output off): sleep until it is
		struct itimerspec off = {0}; timerfd_settime(c->tfd, 0, &off, NULL); c->parked = true; return;
	}
	uint64_t now = now_ms(), gap = c->last_tick_ms ? now - c->last_tick_ms : 0;
	c->last_tick_ms = now;
	bool moving = !c->paused && !c->reduced_motion;
	if (c->cs.on && moving) { cosmos_advance(c, c->cs.last_ms ? (now - c->cs.last_ms) / 1000.0 : 0.05); c->cs.last_ms = now; c->dirty = true; }
	else c->cs.last_ms = 0;
	if (moving) {
		sim_step(c); if (c->cs.on) cosmos_observe(c);
		c->active_ms += gap < 1000 ? gap : 1000; c->dirty = true;
		if (c->volume && c->slice < 0 && c->spin && !c->edit) c->yaw += .004;
	}
	if (!c->reduced_motion) maybe_advance(c);
	if (!c->running) return;
	if (c->fade_px) c->dirty = true;
	arm_timer(c);
	if (c->dirty) present(c);
	wl_display_flush(c->dpy);
}

// Buffer scale follows the output, except in the cosmos: it is drawn per pixel and stays at scale 1.
static bool apply_geometry(struct ca *c) {
	if (!c->surf || c->lw <= 0 || c->lh <= 0) return true;
	int scale = c->cs.on || c->out_scale < 1 ? 1 : c->out_scale > 4 ? 4 : c->out_scale;
	if (c->lw > 16384 / scale || c->lh > 16384 / scale) scale = 1;
	int W = c->lw * scale, H = c->lh * scale;
	if (c->grid && W == c->W && H == c->H && scale == scale_of(c)) return true;
	c->W = W; c->H = H; c->scale = scale;
	free(c->fade_px); c->fade_px = NULL; c->last_bi = -1;
	if (!make_buffers(c) || !make_stars(c) || !sim_refit(c)) {
		fprintf(stderr, "cube-ca: unable to allocate resized surface\n");
		c->running = false; return false;
	}
	struct wl_region *opaque = wl_compositor_create_region(c->comp);
	wl_region_add(opaque, 0, 0, c->lw, c->lh);
	wl_surface_set_opaque_region(c->surf, opaque); wl_region_destroy(opaque);
	c->dirty = true;
	fprintf(stderr, "cube-ca: mapped %dx%d at scale %d\n", c->lw, c->lh, scale);
	return true;
}
static void layer_configure(void *data, struct zwlr_layer_surface_v1 *s, uint32_t serial, uint32_t w, uint32_t h) {
	struct ca *c = data;
	zwlr_layer_surface_v1_ack_configure(s, serial);
	if (!w || !h || w > 16384 || h > 16384) { c->running = false; return; }
	c->lw = w; c->lh = h;
	if (!apply_geometry(c)) return;
	if (!c->configured) {
		c->configured = true;
		if (c->cs.want_on) { c->cs.want_on = false; cosmos_set(c, true); }
		arm_timer(c);
	}
	if (c->dirty) present(c);
}
static void layer_closed(void *data, struct zwlr_layer_surface_v1 *s) {
	struct ca *c = data;
	(void)s;
	c->running = false;
}
static const struct zwlr_layer_surface_v1_listener layer_listener = { layer_configure, layer_closed };

#include "cube-field.h"

// --- outputs: per-output scale for this process, or one child process per output when supervising ---
static void set_out_scale(struct ca *c, int scale) {
	if (scale == c->out_scale) return;
	c->out_scale = scale;
	if (c->configured && apply_geometry(c)) present(c);
}
static void supervise(struct ca *c);
static void output_geometry(void *d, struct wl_output *o, int32_t x, int32_t y, int32_t pw, int32_t ph, int32_t sp, const char *make, const char *model, int32_t t) {}
static void output_mode(void *d, struct wl_output *o, uint32_t flags, int32_t w, int32_t h, int32_t refresh) {}
static void output_scale(void *data, struct wl_output *o, int32_t factor) { ((struct output_ref *)data)->pending_scale = factor; }
static void output_name(void *data, struct wl_output *o, const char *name) { struct output_ref *ref = data; snprintf(ref->name, sizeof(ref->name), "%s", name); }
static void output_description(void *d, struct wl_output *o, const char *description) {}
static void output_done(void *data, struct wl_output *o) {
	struct output_ref *ref = data; struct ca *c = ref->c;
	if (ref->pending_scale > 0) ref->scale = ref->pending_scale;
	if (c->supervisor) supervise(c);
	else if (o == (c->target ? c->target : c->entered)) set_out_scale(c, ref->scale);
}
static const struct wl_output_listener output_listener = {
	.geometry = output_geometry, .mode = output_mode, .done = output_done, .scale = output_scale,
	.name = output_name, .description = output_description,
};
static void surface_enter(void *data, struct wl_surface *s, struct wl_output *o) {
	struct ca *c = data;
	if (c->target) return;
	c->entered = o;
	for (int i = 0; i < c->nouts; i++) if (c->outs[i].wl == o) set_out_scale(c, c->outs[i].scale);
}
static void surface_leave(void *data, struct wl_surface *s, struct wl_output *o) {}
static const struct wl_surface_listener surface_listener = { .enter = surface_enter, .leave = surface_leave };

static void registry_global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t ver) {
	struct ca *c = data;
	if (!strcmp(iface, wl_compositor_interface.name)) {
		c->comp_ver = ver < 4 ? ver : 4;
		c->comp = wl_registry_bind(reg, name, &wl_compositor_interface, c->comp_ver);
	} else if (!strcmp(iface, wl_seat_interface.name)) {
		if (c->seat) return;
		c->seat = wl_registry_bind(reg, name, &wl_seat_interface, ver < 5 ? ver : 5);
		wl_seat_add_listener(c->seat, &seat_listener, c);
	} else if (!strcmp(iface, wl_shm_interface.name))
		c->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name))
		c->layer_shell = wl_registry_bind(reg, name, &zwlr_layer_shell_v1_interface, ver < 4 ? ver : 4);
	else if (!strcmp(iface, wl_output_interface.name)) {
		struct output_ref *o = NULL;
		for (int i = 0; i < c->nouts && !o; i++) if (!c->outs[i].wl && !c->outs[i].pid) o = &c->outs[i];
		if (!o && c->nouts < MAX_OUTPUTS) o = &c->outs[c->nouts++];
		if (!o) return;
		*o = (struct output_ref){ .c = c, .global = name, .ver = ver < 4 ? ver : 4, .scale = 1 };
		o->wl = wl_registry_bind(reg, name, &wl_output_interface, o->ver);
		wl_output_add_listener(o->wl, &output_listener, o);
	}
}
static void registry_remove(void *data, struct wl_registry *reg, uint32_t name) {
	struct ca *c = data;
	for (int i = 0; i < c->nouts; i++) {
		struct output_ref *o = &c->outs[i];
		if (!o->wl || o->global != name) continue;
		if (o->wl == c->entered) c->entered = NULL;
		if (o->ver >= 3) wl_output_release(o->wl); else wl_output_destroy(o->wl);
		o->wl = NULL; o->name[0] = 0; // a supervised child exits on its own when its layer surface closes
		if (c->supervisor && o->pid > 0) kill(o->pid, SIGTERM);
	}
}
static const struct wl_registry_listener reg_listener = { registry_global, registry_remove };

static void spawn_instance(struct ca *c, struct output_ref *o) {
	pid_t parent = getpid(), pid = fork();
	if (pid < 0) { perror("cube-ca: fork"); return; }
	if (pid == 0) {
		prctl(PR_SET_PDEATHSIG, SIGTERM);
		if (getppid() != parent) _exit(0);
		sigset_t none; sigemptyset(&none); sigprocmask(SIG_SETMASK, &none, NULL);
		setenv("CUBE_CA_OUTPUT", o->name, 1);
		char *argv[] = { "cube-ca", NULL };
		execv("/proc/self/exe", argv);
		_exit(127);
	}
	o->pid = pid; o->started_ms = now_ms();
	fprintf(stderr, "cube-ca: drawing %s in process %ld\n", o->name, (long)pid);
}
// One child per named output. A child that keeps dying within a minute is given up on after five tries.
static void supervise(struct ca *c) {
	for (int i = 0; i < c->nouts; i++) {
		struct output_ref *o = &c->outs[i];
		if (o->wl && o->name[0] && !o->pid && o->crashes < 5) spawn_instance(c, o);
	}
}
static void reap(struct ca *c, int sigfd) {
	struct signalfd_siginfo si;
	while (read(sigfd, &si, sizeof(si)) == sizeof(si)) {}
	int status; pid_t pid;
	while ((pid = waitpid(-1, &status, WNOHANG)) > 0) for (int i = 0; i < c->nouts; i++) {
		struct output_ref *o = &c->outs[i];
		if (o->pid != pid) continue;
		o->pid = 0;
		o->crashes = now_ms() - o->started_ms < 60000 ? o->crashes + 1 : 0;
		if (o->wl && o->crashes >= 5) fprintf(stderr, "cube-ca: giving up on %s after repeated exits\n", o->name);
	}
	supervise(c);
}
static int supervisor_main(struct ca *c) {
	sigset_t chld; sigemptyset(&chld); sigaddset(&chld, SIGCHLD);
	sigprocmask(SIG_BLOCK, &chld, NULL);
	int sigfd = signalfd(-1, &chld, SFD_CLOEXEC | SFD_NONBLOCK);
	if (sigfd < 0) { perror("cube-ca: signalfd"); return 1; }
	c->supervisor = true;
	supervise(c);
	struct pollfd fds[2] = { { .fd = wl_display_get_fd(c->dpy), .events = POLLIN }, { .fd = sigfd, .events = POLLIN } };
	for (;;) {
		while (wl_display_prepare_read(c->dpy) != 0)
			if (wl_display_dispatch_pending(c->dpy) < 0) goto done;
		if (wl_display_flush(c->dpy) < 0 && errno != EAGAIN) {
			wl_display_cancel_read(c->dpy);
			break;
		}
		if (poll(fds, 2, -1) < 0) {
			wl_display_cancel_read(c->dpy);
			if (errno == EINTR) continue;
			break;
		}
		if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
			if (wl_display_read_events(c->dpy) < 0) break;
		} else wl_display_cancel_read(c->dpy);
		if (wl_display_dispatch_pending(c->dpy) < 0) break;
		if (fds[1].revents & POLLIN) reap(c, sigfd);
		if (fds[0].revents & (POLLHUP | POLLERR)) break;
	}
done:
	for (int i = 0; i < c->nouts; i++) if (c->outs[i].pid > 0) kill(c->outs[i].pid, SIGTERM);
	for (int spin = 0; spin < 25; spin++) {
		int status;
		while (waitpid(-1, &status, WNOHANG) > 0) {}
		bool any = false;
		for (int i = 0; i < c->nouts; i++) if (c->outs[i].pid > 0 && kill(c->outs[i].pid, 0) == 0) any = true;
		if (!any) break;
		usleep(2000);
	}
	close(sigfd);
	wl_display_disconnect(c->dpy);
	return 0;
}

static void cosmos_offscreen_free(struct ca *c) { cos_free(c); free(c->b[0].px); free(c->stars); free(c->grid); free(c->next); free(c->age); free(c->state); }
// Offscreen cosmos surface for tests and previews: a warmed-up field, star background, no Wayland.
static bool cosmos_offscreen(struct ca *c, int w, int h, unsigned seed, int warm) {
	memset(c, 0, sizeof(*c));
	c->W = w; c->H = h; c->seed = seed; c->slice = -1; c->configured = true; c->cs.speed = 1; c->last_bi = -1;
	if (!sim_alloc(c) || !make_stars(c)) { cosmos_offscreen_free(c); return false; }
	c->b[0].px = malloc((size_t)w * h * 4); if (!c->b[0].px) { cosmos_offscreen_free(c); return false; }
	reseed(c); c->cs.on = true; c->cs.auto_zoom = true; c->cs.dir = 1; c->cs.seed = seed;
	for (int i = 0; i < warm; i++) { sim_step(c); cosmos_observe(c); }
	cosmos_rebuild(c);
	return true;
}
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
	if (cosmos_control(&c, "phase", "nowhere") || cosmos_control(&c, "zoom", "9") || cosmos_control(&c, "speed", "100") || cosmos_control(&c, "zoom", "nan") || cosmos_control(&c, "speed", "nan")) return 8;
	if (!cosmos_control(&c, "zoom", "2.5") || fabs(c.cs.zoom - 2.5) > 1e-9 || !cosmos_control(&c, "speed", "2") || c.cs.speed != 2) return 9;
	if (!apply_control(&c, "phase 4") || apply_control(&c, "edit") || apply_control(&c, "3d")) return 10;
	// A rule change mid-journey keeps the landscape, the forming stars and the expansion.
	unsigned land = c.cs.seed; c.cs.nstars = 5; c.cs.uni_t = 20; float mean = c.cs.dmean;
	if (!apply_control(&c, "rule daynight") || c.cs.seed != land || c.cs.nstars != 5 || c.cs.uni_t != 20) return 12;
	cosmos_observe(&c); // daynight's dense new soup is only 8% of the first average: the history was resampled, not restarted
	if (!c.cs.dens || c.cs.dw != c.cols || fabsf(c.cs.dmean - mean) > .08f) return 13;
	if (!apply_control(&c, "cosmos off") || c.cs.on) return 11;
	cosmos_offscreen_free(&c);
	// The environment overrides the config file in both directions.
	char home[] = "/tmp/cube-ca-home-XXXXXX", path[600]; if (!mkdtemp(home)) return 14;
	snprintf(path, sizeof(path), "%s/.config", home); mkdir(path, 0700);
	snprintf(path, sizeof(path), "%s/.config/whitecube", home); mkdir(path, 0700);
	snprintf(path, sizeof(path), "%s/.config/whitecube/config", home);
	FILE *f = fopen(path, "w"); if (!f) return 14;
	fputs("cosmos=0\n#", f);
	for (int i = 0; i < 126; i++) fputc('x', f);
	fputs("cosmos=1\n", f);
	fclose(f);
	char *oldhome = getenv("HOME") ? strdup(getenv("HOME")) : NULL, *oldenv = getenv("CUBE_CA_COSMOS") ? strdup(getenv("CUBE_CA_COSMOS")) : NULL;
	setenv("HOME", home, 1); setenv("CUBE_CA_COSMOS", "1", 1);
	struct ca e = {0}; cosmos_config(&e); bool env_on = e.cs.want_on;
	unsetenv("CUBE_CA_COSMOS"); cosmos_config(&e); bool file_off = !e.cs.want_on;
	if (oldhome) { setenv("HOME", oldhome, 1); free(oldhome); } else unsetenv("HOME");
	if (oldenv) { setenv("CUBE_CA_COSMOS", oldenv, 1); free(oldenv); }
	unlink(path); snprintf(path, sizeof(path), "%s/.config/whitecube", home); rmdir(path);
	snprintf(path, sizeof(path), "%s/.config", home); rmdir(path); rmdir(home);
	if (!env_on || !file_off) return 15;
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
	FILE *f = fopen(path, "wb"); if (!f) { perror(path); cosmos_offscreen_free(&c); return 1; }
	fprintf(f, "P6\n%d %d\n255\n", c.W, c.H);
	for (int i = 0; i < c.W * c.H; i++) { uint32_t v = ((uint32_t *)c.b[0].px)[i]; unsigned char rgb[3] = {v >> 16, v >> 8, v}; if (fwrite(rgb, 1, 3, f) != 3) { fclose(f); cosmos_offscreen_free(&c); return 1; } }
	int rc = fclose(f); cosmos_offscreen_free(&c); return rc != 0;
}
// Incremental frames must match a full repaint pixel for pixel, and the damage must cover every change.
static int render_self_test(void) {
	struct ca c = {.W = 400, .H = 300, .seed = 99, .slice = -1, .configured = true, .last_bi = -1, .scale = 2};
	if (!sim_alloc(&c) || !make_stars(&c)) return 1;
	size_t n = (size_t)c.W * c.H; uint32_t *full = malloc(n * 4), *before = malloc(n * 4);
	c.b[0].px = malloc(n * 4); c.b[1].px = malloc(n * 4);
	if (!full || !before || !c.b[0].px || !c.b[1].px) return 1;
	reseed(&c);
	int rc = 0; struct rect dmg[MAX_DAMAGE];
	for (int frame = 0; frame < 40 && !rc; frame++) {
		int bi = frame % 3 == 2 ? c.last_bi : (frame & 1); // alternate buffers, sometimes reuse the shown one
		if (bi < 0) bi = 0;
		int prev = c.last_bi; if (prev >= 0) memcpy(before, c.b[prev].px, n * 4);
		if (frame == 20) c.edit = true; // flips every cell's state
		int count = render_frame(&c, bi, dmg); c.last_bi = bi;
		void *keep = c.b[bi].px; c.b[bi].px = full; render(&c, bi); c.b[bi].px = keep;
		if (memcmp(full, keep, n * 4)) rc = 2;
		if (prev >= 0 && count >= 0) for (size_t i = 0; i < n && !rc; i++) if (before[i] != full[i]) {
			int x = (int)(i % c.W), y = (int)(i / c.W); bool covered = false;
			for (int k = 0; k < count && !covered; k++) covered = x >= dmg[k].x && x < dmg[k].x + dmg[k].w && y >= dmg[k].y && y < dmg[k].y + dmg[k].h;
			if (!covered) rc = 3;
		}
		sim_step(&c);
	}
	// Age changes colour; a newborn and an old cube differ.
	uint32_t *p = c.b[0].px; memcpy(p, c.stars, n * 4); cube(&c, p, 50, 50, 20, 0); uint32_t young = p[45 * c.W + 50];
	cube(&c, p, 50, 50, 20, 200); if (!rc && young == p[45 * c.W + 50]) rc = 4;
	// The crossfade starts from the old frame and blends toward the new one.
	c.last_bi = 0; c.b[0].buf = (struct wl_buffer *)1; memset(c.b[0].px, 0, n * 4);
	start_fade(&c); c.b[0].buf = NULL;
	if (!rc && !c.fade_px) rc = 5;
	if (!rc) { c.fade_start_ms = now_ms() - FADE_MS / 2; memset(c.b[1].px, 0xFF, n * 4); blend_fade(&c, c.b[1].px); uint32_t v = ((uint32_t *)c.b[1].px)[0] & 0xFF;
		if (v < 64 || v > 192) rc = 6; }
	free(c.fade_px); free(full); free(before); free(c.b[0].px); free(c.b[1].px); free(c.b[0].shown); free(c.b[1].shown);
	free(c.state); free(c.stars); free(c.grid); free(c.next); free(c.age);
	return rc;
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
	// One seed number recreates the same soup.
	reseed_from(&c, 4242); uint8_t *first = malloc(sim_count(&c)); if (!first) return 1;
	memcpy(first, c.grid, sim_count(&c)); reseed(&c); reseed_from(&c, 4242);
	bool same = !memcmp(first, c.grid, sim_count(&c)) && c.base_seed == 4242; free(first);
	if (!same) return 8;
	// Custom rules: B/S notation in either order; B0 and junk are refused.
	uint16_t b, s; char name[24];
	if (!parse_rule("B36/S23", &b, &s, name, sizeof(name)) || b != ((1u<<3)|(1u<<6)) || s != ((1u<<2)|(1u<<3)) || strcmp(name, "B36/S23")) return 9;
	if (!parse_rule("s23b3", &b, &s, name, sizeof(name)) || strcmp(name, "B3/S23") || !parse_rule("B2/S", &b, &s, name, sizeof(name))) return 9;
	if (parse_rule("B0/S8", &b, &s, name, sizeof(name)) || parse_rule("B9/S2", &b, &s, name, sizeof(name)) || parse_rule("B3/S23x", &b, &s, name, sizeof(name)) || parse_rule("B3", &b, &s, name, sizeof(name)) || parse_rule("B/S23", &b, &s, name, sizeof(name))) return 9;
	// A painted world survives a resize; an automatic one keeps running when its size does not change.
	c.rule = 0; sim_alloc(&c); reseed(&c); c.manual = true; c.grid[2 * c.cols + 3] = 1;
	c.W = 300; if (!sim_refit(&c) || !c.grid[2 * c.cols + 3]) return 20;
	c.manual = false; uint8_t *copy = malloc(sim_count(&c)); if (!copy) return 1;
	memcpy(copy, c.grid, sim_count(&c)); c.W = 301; same = sim_refit(&c) && !memcmp(copy, c.grid, sim_count(&c)); free(copy);
	if (!same) return 21;
	// Extinct 3D volumes are replaced at the next check, not after the whole 300-tick budget.
	c.volume = true; sim_alloc(&c); c.tick = 30; c.last_check_tick = 0; c.last_bi = -1; maybe_advance(&c);
	live = 0; for (size_t i = 0; i < sim_count(&c); i++) live += c.grid[i];
	if (!live) return 22;
	c.volume = false; c.manual = false;
	free(c.grid); free(c.next); free(c.age);
	if (field_self_test()) return 7;
	{ int rc = render_self_test(); if (rc) { fprintf(stderr, "cube-ca: render self-test failed (%d)\n", rc); return 30 + rc; } }
	{ int rc = cosmos_self_test(); if (rc) { fprintf(stderr, "cube-ca: cosmos self-test failed (%d)\n", rc); return 10 + rc; } }
	puts("cube-ca: 2D toroidal oscillator, six rules plus B/S rules, 26-neighbour volume, editing, seed replay, incremental damage, crossfade and cosmic zoom passed");
	return 0;
}
static int preview(const char *path, bool volume) {
	struct ca c = {.W=1440, .H=900, .seed=0x1234567, .volume=volume, .slice=-1, .yaw=.65};
	if (!sim_alloc(&c) || !make_stars(&c)) { cosmos_offscreen_free(&c); return 1; }
	reseed(&c);
	for (int i = 0; i < 18; i++) sim_step(&c);
	c.b[0].px = malloc((size_t)c.W*c.H*4);
	if (!c.b[0].px) { cosmos_offscreen_free(&c); return 1; }
	render(&c, 0);
	FILE *f = fopen(path, "wb");
	if (!f) { perror(path); cosmos_offscreen_free(&c); return 1; }
	fprintf(f, "P6\n%d %d\n255\n", c.W, c.H);
	for (int i = 0; i < c.W*c.H; i++) {
		uint32_t v = ((uint32_t *)c.b[0].px)[i];
		unsigned char rgb[3] = {v>>16, v>>8, v};
		if (fwrite(rgb, 1, 3, f) != 3) { fclose(f); cosmos_offscreen_free(&c); return 1; }
	}
	int rc = fclose(f);
	cosmos_offscreen_free(&c);
	return rc != 0;
}
int main(int argc, char **argv) {
	if (argc >= 3 && !strcmp(argv[1], "--control")) {
		const char *target = argv[2][0] == '@' ? argv[2] + 1 : NULL; int first = target ? 3 : 2;
		if (first >= argc) return 1;
		char command[256] = {0}; size_t len = 0;
		for (int i = first; i < argc; i++) { int n = snprintf(command + len, sizeof(command) - len, "%s%s", i == first ? "" : " ", argv[i]);
			if (n < 0 || (size_t)n >= sizeof(command) - len) return 1;
			len += n; }
		return send_control(target, command);
	}
	if (argc == 2 && !strcmp(argv[1], "--menu")) return control_menu();
	if (argc == 2 && !strcmp(argv[1], "--self-test")) return self_test();
	if (argc == 3 && !strcmp(argv[1], "--preview")) return preview(argv[2], false);
	if (argc == 3 && !strcmp(argv[1], "--preview-3d")) return preview(argv[2], true);
	if (argc == 4 && !strcmp(argv[1], "--preview-zoom")) return preview_zoom(argv[3], atof(argv[2]));
	if (argc != 1) { fprintf(stderr, "Usage: cube-ca [--menu | --control [@OUTPUT] COMMAND [ARG] | --self-test | --preview output.ppm | --preview-3d output.ppm | --preview-zoom 0..6 output.ppm]\n"); return 1; }
	struct ca c;
	memset(&c, 0, sizeof(c));
	c.b[0].fd = -1; c.b[1].fd = -1; c.control_fd = -1; c.slice = -1; c.yaw = .65; c.last_bi = -1; c.spin = true; c.out_scale = 1;
	const char *want = getenv("CUBE_CA_OUTPUT");
	if (want) snprintf(c.want_output, sizeof(c.want_output), "%s", want);
	c.tfd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC | TFD_NONBLOCK);
	if (c.tfd < 0) { perror("cube-ca: timerfd"); return 1; }
	// Per-output processes start in the same second with neighbouring pids: draw the seed from the kernel.
	if (getrandom(&c.seed, sizeof(c.seed), GRND_NONBLOCK) != sizeof(c.seed)) c.seed = (unsigned)time(NULL) * 2654435761u ^ (unsigned)getpid() * 0x9E3779B9u;
	if (!c.seed) c.seed = 1;
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
	wl_display_roundtrip(c.dpy); // output names and scales
	bool named = c.nouts > 0;
	for (int i = 0; i < c.nouts; i++) if (!c.outs[i].name[0]) named = false;
	if (*c.want_output) {
		for (int i = 0; i < c.nouts; i++) if (c.outs[i].wl && !strcmp(c.outs[i].name, c.want_output)) { c.target = c.outs[i].wl; c.out_scale = c.outs[i].scale; }
		if (!c.target) { fprintf(stderr, "cube-ca: output %s is gone\n", c.want_output); return 0; }
	} else if (named && getenv("WAYLAND_DISPLAY") && !getenv("CUBE_CA_SINGLE")) {
		close(c.tfd); return supervisor_main(&c);
	}
	c.surf = wl_compositor_create_surface(c.comp);
	wl_surface_add_listener(c.surf, &surface_listener, &c);
	c.layer = zwlr_layer_shell_v1_get_layer_surface(c.layer_shell, c.surf, c.target,
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
	free_buffers(&c); free(c.stars); free(c.grid); free(c.next); free(c.age); free(c.state); free(c.fade_px); cos_free(&c);
	for (int i = 0; i < 2; i++) free(c.b[i].shown);
	close(c.tfd);
	if (c.control_fd >= 0) { close(c.control_fd); unlink(c.control_path); }
	if (c.frame_cb) wl_callback_destroy(c.frame_cb);
	if (c.pointer) wl_pointer_destroy(c.pointer);
	if (c.seat) wl_seat_destroy(c.seat);
	for (int i = 0; i < c.nouts; i++) if (c.outs[i].wl) wl_output_destroy(c.outs[i].wl);
	zwlr_layer_surface_v1_destroy(c.layer); wl_surface_destroy(c.surf);
	zwlr_layer_shell_v1_destroy(c.layer_shell); wl_shm_destroy(c.shm);
	wl_compositor_destroy(c.comp); wl_registry_destroy(c.reg);
	wl_display_disconnect(c.dpy);
	return 0;
}
