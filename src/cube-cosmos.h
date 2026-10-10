// Cosmic zoom for cube-ca, included by cube-ca.c after the cube renderer.
// Phases zoom out from the live cells: cells > geometry > land > planet > stars forming > galaxy > expanding universe.
// The automaton stays the engine: its time-averaged density shapes terrain and gas, and live cells become
// lights on the land, the planet's night side and the lattice. Each phase draws into a scratch layer that is
// added onto the frame with a weight, so neighbouring phases crossfade while their scales slide past each other.

#define COS_NAMES_INIT { "cells", "geometry", "land", "planet", "stars", "galaxy", "universe" }
static const char *const COS_NAMES[COS_PHASES] = COS_NAMES_INIT;
#define COS_SEA 0.52f
#define COS_MAP_W 256
#define COS_MAP_H 128
#define COS_WEB_NODES 36
#define COS_WEB_GALAXIES 4200

static inline float cos_clamp(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }
static inline float cos_lerp(float a, float b, float t) { return a + (b - a) * t; }
static inline uint32_t cos_rgb(float r, float g, float b) {
	return 0xFF000000u | ((unsigned)cos_clamp(r, 0, 255) << 16) | ((unsigned)cos_clamp(g, 0, 255) << 8) | (unsigned)cos_clamp(b, 0, 255);
}
static inline uint32_t cos_mix(uint32_t a, uint32_t b, float t) {
	return cos_rgb(cos_lerp((a >> 16) & 255, (b >> 16) & 255, t), cos_lerp((a >> 8) & 255, (b >> 8) & 255, t), cos_lerp(a & 255, b & 255, t));
}
static float cos_hash(int x, int y, unsigned s) {
	unsigned h = (unsigned)x * 374761393u + (unsigned)y * 668265263u + s * 2246822519u;
	h = (h ^ (h >> 13)) * 1274126177u; h ^= h >> 16;
	return (h & 0xFFFFFF) / 16777216.0f;
}
static float cos_vnoise(float x, float y, unsigned s, int per) {
	int xi = (int)floorf(x), yi = (int)floorf(y); float fx = x - xi, fy = y - yi;
	fx = fx * fx * (3 - 2 * fx); fy = fy * fy * (3 - 2 * fy);
	int x0 = ((xi % per) + per) % per, x1 = (x0 + 1) % per, y0 = ((yi % per) + per) % per, y1 = (y0 + 1) % per;
	float a = cos_hash(x0, y0, s), b = cos_hash(x1, y0, s), c = cos_hash(x0, y1, s), d = cos_hash(x1, y1, s);
	return cos_lerp(cos_lerp(a, b, fx), cos_lerp(c, d, fx), fy);
}
static float cos_fbm(float x, float y, unsigned s, int per) {
	float amp = .5f, sum = 0;
	for (int o = 0; o < 4; o++) { sum += amp * cos_vnoise(x, y, s + o * 17, per); x *= 2; y *= 2; per *= 2; amp *= .5f; }
	return sum / .9375f;
}

// --- layer primitives (the layer starts black; everything composites additively) ---
static inline void cos_add(struct ca *c, uint32_t *p, int x, int y, uint32_t rgb, float a) {
	if (x < 0 || y < 0 || x >= c->W || y >= c->H || a <= 0) return;
	if (a > 1) a = 1;
	int ia = (int)(a * 256); uint32_t *d = &p[(size_t)y * c->W + x]; uint32_t o = *d;
	unsigned r = (o >> 16 & 255) + (((rgb >> 16) & 255) * ia >> 8), g = (o >> 8 & 255) + (((rgb >> 8) & 255) * ia >> 8), b = (o & 255) + ((rgb & 255) * ia >> 8);
	*d = 0xFF000000u | ((r > 255 ? 255 : r) << 16) | ((g > 255 ? 255 : g) << 8) | (b > 255 ? 255 : b);
}
static void cos_glow(struct ca *c, uint32_t *p, double cx, double cy, double radius, uint32_t rgb, float intensity) {
	if (radius < .6) { cos_add(c, p, (int)cx, (int)cy, rgb, intensity); return; }
	if (radius > 90) radius = 90;
	int x0 = (int)floor(cx - radius), x1 = (int)ceil(cx + radius), y0 = (int)floor(cy - radius), y1 = (int)ceil(cy + radius);
	for (int y = y0; y <= y1; y++) for (int x = x0; x <= x1; x++) {
		double d = hypot(x - cx, y - cy) / radius; if (d >= 1) continue;
		float f = (float)((1 - d) * (1 - d)); cos_add(c, p, x, y, rgb, f * intensity);
	}
}
static void cos_line(struct ca *c, uint32_t *p, double x0, double y0, double x1, double y1, uint32_t rgb, float a) {
	double dx = x1 - x0, dy = y1 - y0, len = fmax(fabs(dx), fabs(dy));
	if (len > 4000) return;
	int n = (int)len + 1;
	for (int i = 0; i <= n; i++) { double t = n ? (double)i / n : 0; cos_add(c, p, (int)(x0 + dx * t), (int)(y0 + dy * t), rgb, a); }
}

// --- state upkeep ---
static void cos_free(struct ca *c) { free(c->cs.dens); free(c->cs.hmap); free(c->cs.cmap); free(c->cs.layer); free(c->cs.wx); free(c->cs.wy); free(c->cs.wb); free(c->cs.gas);
	c->cs.gas = NULL; c->cs.gas_n = 0; c->cs.dens = NULL; c->cs.hmap = NULL; c->cs.cmap = NULL; c->cs.layer = NULL; c->cs.wx = c->cs.wy = c->cs.wb = NULL; c->cs.dw = c->cs.dh = 0; c->cs.layer_n = 0; }

static float cos_dens_at(const struct cosmos *cs, float u, float v) { // bilinear, wrapped; u,v in turns
	if (!cs->dens || cs->dw < 2 || cs->dh < 2) return 0;
	float x = (u - floorf(u)) * cs->dw, y = (v - floorf(v)) * cs->dh;
	int x0 = (int)x % cs->dw, y0 = (int)y % cs->dh, x1 = (x0 + 1) % cs->dw, y1 = (y0 + 1) % cs->dh; float fx = x - floorf(x), fy = y - floorf(y);
	return cos_lerp(cos_lerp(cs->dens[y0 * cs->dw + x0], cs->dens[y0 * cs->dw + x1], fx), cos_lerp(cs->dens[y1 * cs->dw + x0], cs->dens[y1 * cs->dw + x1], fx), fy);
}
static float cos_dens_norm(const struct cosmos *cs, float u, float v) { // 0..1, centred on the field's own average
	float d = cos_dens_at(cs, u, v);
	return cos_clamp((d - cs->dmean) / (cs->dscale + .02f) * .5f + .5f, 0, 1);
}

static void cos_build_maps(struct ca *c) {
	struct cosmos *cs = &c->cs;
	if (!cs->hmap) cs->hmap = malloc(sizeof(float) * COS_MAP_W * COS_MAP_H);
	if (!cs->cmap) cs->cmap = malloc(sizeof(float) * COS_MAP_W * COS_MAP_H);
	if (!cs->hmap || !cs->cmap) return;
	for (int y = 0; y < COS_MAP_H; y++) for (int x = 0; x < COS_MAP_W; x++) {
		float u = (float)x / COS_MAP_W, v = (float)y / COS_MAP_H;
		float continents = cos_fbm(u * 8, v * 4, cs->seed, 8);
		float live = cos_dens_norm(cs, u, v);
		cs->hmap[y * COS_MAP_W + x] = cos_clamp(.62f * continents + .38f * live, 0, 1);
		cs->cmap[y * COS_MAP_W + x] = cos_fbm(u * 12, v * 6, cs->seed ^ 0x5bd1e995u, 12);
	}
}
static void cos_build_web(struct ca *c) {
	struct cosmos *cs = &c->cs;
	free(cs->wx); free(cs->wy); free(cs->wb);
	cs->wx = malloc(sizeof(float) * COS_WEB_GALAXIES); cs->wy = malloc(sizeof(float) * COS_WEB_GALAXIES); cs->wb = malloc(sizeof(float) * COS_WEB_GALAXIES);
	if (!cs->wx || !cs->wy || !cs->wb) return;
	float nx[COS_WEB_NODES], ny[COS_WEB_NODES]; int ea[COS_WEB_NODES * 2], eb[COS_WEB_NODES * 2], ne = 0;
	unsigned s = cs->seed ^ 0xC05305u;
	for (int i = 0; i < COS_WEB_NODES; i++) { nx[i] = cos_hash(i, 1, s) * 2 - 1; ny[i] = cos_hash(i, 2, s) * 2 - 1; }
	for (int i = 0; i < COS_WEB_NODES; i++) { // each node links to its two nearest neighbours: filaments
		int best[2] = {-1, -1}; float bd[2] = {1e9f, 1e9f};
		for (int j = 0; j < COS_WEB_NODES; j++) if (j != i) {
			float d = hypotf(nx[i] - nx[j], ny[i] - ny[j]);
			if (d < bd[0]) { bd[1] = bd[0]; best[1] = best[0]; bd[0] = d; best[0] = j; } else if (d < bd[1]) { bd[1] = d; best[1] = j; }
		}
		for (int k = 0; k < 2 && ne < COS_WEB_NODES * 2; k++) if (best[k] >= 0) { ea[ne] = i; eb[ne] = best[k]; ne++; }
	}
	cs->nnodes = COS_WEB_NODES; cs->nedges = ne;
	for (int i = 0; i < COS_WEB_NODES; i++) { cs->nodex[i] = nx[i]; cs->nodey[i] = ny[i]; }
	for (int e = 0; e < ne; e++) { cs->edge_a[e] = ea[e]; cs->edge_b[e] = eb[e]; }
	for (int g = 0; g < COS_WEB_GALAXIES; g++) {
		float jx = (cos_hash(g, 3, s) + cos_hash(g, 4, s) + cos_hash(g, 5, s) - 1.5f) * .09f, jy = (cos_hash(g, 6, s) + cos_hash(g, 7, s) + cos_hash(g, 8, s) - 1.5f) * .09f;
		if (cos_hash(g, 9, s) < .12f) { cs->wx[g] = cos_hash(g, 10, s) * 2 - 1; cs->wy[g] = cos_hash(g, 11, s) * 2 - 1; }
		else { int e = (int)(cos_hash(g, 12, s) * ne) % ne; float t = cos_hash(g, 13, s);
			cs->wx[g] = cos_lerp(nx[ea[e]], nx[eb[e]], t) + jx; cs->wy[g] = cos_lerp(ny[ea[e]], ny[eb[e]], t) + jy; }
		cs->wb[g] = .25f + cos_hash(g, 14, s) * .75f;
	}
}
static void cosmos_rebuild(struct ca *c) { cos_build_maps(c); cos_build_web(c); c->cs.nstars = 0; c->cs.star_next = 0; c->cs.uni_t = 0; }

// Called every simulation step: time-average the live cells so the cosmos reflects what the automaton has been doing.
static void cosmos_observe(struct ca *c) {
	struct cosmos *cs = &c->cs;
	if (c->volume || c->depth != 1) return;
	if (cs->dw != c->cols || cs->dh != c->rows || !cs->dens) {
		// A rule change resizes the grid: resample the history instead of restarting it, so the land does not jump.
		float *d = calloc((size_t)c->cols * c->rows, sizeof(float)); if (!d) return;
		for (int y = 0; y < c->rows; y++) for (int x = 0; x < c->cols; x++)
			d[y * c->cols + x] = cs->dens ? cos_dens_at(cs, (x + .5f) / c->cols, (y + .5f) / c->rows) : c->grid[y * c->cols + x];
		free(cs->dens); cs->dens = d; cs->dw = c->cols; cs->dh = c->rows;
	}
	double sum = 0, sq = 0; int n = c->cols * c->rows;
	for (int i = 0; i < n; i++) { cs->dens[i] = cs->dens[i] * .92f + c->grid[i] * .08f; sum += cs->dens[i]; }
	float mean = (float)(sum / n);
	for (int i = 0; i < n; i++) { float d = cs->dens[i] - mean; sq += d * d; }
	cs->dmean = mean; cs->dscale = sqrtf((float)(sq / n)) * 2.5f;
	if (++cs->obs % 20 == 0 || !cs->hmap) cos_build_maps(c);
}

static float cos_gas(const struct cosmos *cs, float u, float v, uint32_t seed);

// --- zoom clock ---
static void cos_view(double zoom, int k, double *scale, double *weight) {
	double d = zoom - k, ad = fabs(d);
	*weight = ad >= 1 ? 0 : (1 - ad) * (1 - ad) * (3 - 2 * (1 - ad));
	*scale = exp2(-2.0 * d);
}
static void cosmos_goto(struct cosmos *cs, int phase) { cs->target = phase < 0 ? 0 : phase >= COS_PHASES ? COS_PHASES - 1 : phase; cs->auto_zoom = false; }
static void cosmos_advance(struct ca *c, double dt) {
	struct cosmos *cs = &c->cs;
	if (dt > .25) dt = .25;
	cs->t += dt;
	if (cs->zoom > 5.5) cs->uni_t += dt; else if (cs->zoom < 4.5) cs->uni_t = 0;
	double step = dt * cs->speed;
	if (!cs->auto_zoom) { // glide to a requested phase, then hold
		double to = cs->target, d = to - cs->zoom, m = 1.6 * step;
		cs->zoom = fabs(d) <= m ? to : cs->zoom + (d > 0 ? m : -m);
	} else if (cs->dwell > 0) cs->dwell -= step;
	else if (cs->dir > 0) {
		double old = cs->zoom, now = old + step / COS_TRANSIT_S;
		int k = (int)floor(old) + 1;
		if (now >= k) { now = k; cs->dwell = k >= COS_PHASES - 1 ? COS_FINAL_DWELL_S : COS_DWELL_S; if (k >= COS_PHASES - 1) cs->dir = -1; }
		cs->zoom = now;
	} else {
		cs->zoom -= step / COS_REWIND_S * (COS_PHASES - 1);
		if (cs->zoom <= 0) { cs->zoom = 0; cs->dir = 1; cs->dwell = COS_DWELL_S; }
	}
	if (cs->zoom < 0) cs->zoom = 0;
	if (cs->zoom > COS_PHASES - 1) cs->zoom = COS_PHASES - 1;
	// Stars condense out of dense gas only while the star phase is on screen.
	if (cs->zoom > 3.3 && cs->zoom < 4.8 && cs->dens) {
		for (int attempt = 0; attempt < 3; attempt++) {
			float u = (rnd(c) % 10000) / 10000.f, v = (rnd(c) % 10000) / 10000.f;
			float g = cos_gas(cs, u, v, cs->seed);
			if (g > .5f) {
				struct cosmos_star *s = &cs->stars[cs->star_next % COS_STARS];
				*s = (struct cosmos_star){ u, v, .35f + (rnd(c) % 1000) / 1540.f, cs->t };
				cs->star_next++; if (cs->nstars < COS_STARS) cs->nstars++;
			}
		}
	} else if (cs->zoom < 3.0) { cs->nstars = 0; cs->star_next = 0; }
}

// --- phases (s = scale about the screen centre; layer is black on entry) ---
static void cos_cells(struct ca *c, uint32_t *p, double s) {
	const struct cosmos *cs = &c->cs; (void)cs;
	double sp = cell_px(c) * s; if (sp < 1.4) return;
	int r = (int)(sp / 2 - 2 * s);
	int gx0 = (int)floor(c->cols / 2.0 - (c->W / 2.0) / sp) - 1, gx1 = (int)ceil(c->cols / 2.0 + (c->W / 2.0) / sp) + 1;
	int gy0 = (int)floor(c->rows / 2.0 - (c->H / 2.0) / sp) - 1, gy1 = (int)ceil(c->rows / 2.0 + (c->H / 2.0) / sp) + 1;
	for (int gy = gy0; gy <= gy1; gy++) for (int gx = gx0; gx <= gx1; gx++) {
		int col = ((gx % c->cols) + c->cols) % c->cols, row = ((gy % c->rows) + c->rows) % c->rows, i = row * c->cols + col;
		if (!c->grid[i]) continue;
		double x = c->W / 2.0 + (gx - c->cols / 2.0 + .5) * sp, y = c->H / 2.0 + (gy - c->rows / 2.0 + .25) * sp;
		if (r >= 3) cube(c, p, (int)x, (int)y, r, c->age[i]);
		else cos_add(c, p, (int)x, (int)y, 0xFFE8EEF8, .9f);
	}
}
static void cos_geometry(struct ca *c, uint32_t *p, double s) {
	// Wider than the cell lattice so the torus does not visibly repeat across the screen at full weight.
	double sp = cell_px(c) * .8 * s; if (sp < 3) return;
	int gx0 = (int)floor(c->cols / 2.0 - (c->W / 2.0) / sp) - 1, gx1 = (int)ceil(c->cols / 2.0 + (c->W / 2.0) / sp) + 1;
	int gy0 = (int)floor(c->rows / 2.0 - (c->H / 2.0) / sp) - 1, gy1 = (int)ceil(c->rows / 2.0 + (c->H / 2.0) / sp) + 1;
#define LIVE(X, Y) (c->grid[((((Y) % c->rows) + c->rows) % c->rows) * c->cols + ((((X) % c->cols) + c->cols) % c->cols)])
#define SX(X) (c->W / 2.0 + ((X) - c->cols / 2.0 + .5) * sp)
#define SY(Y) (c->H / 2.0 + ((Y) - c->rows / 2.0 + .5) * sp)
	for (int gy = gy0; gy <= gy1; gy++) for (int gx = gx0; gx <= gx1; gx++) {
		if (!LIVE(gx, gy)) continue;
		double x = SX(gx), y = SY(gy);
		if (LIVE(gx + 1, gy + 1) && LIVE(gx + 1, gy) && LIVE(gx, gy + 1)) // 2x2 live block: a filled face
			quad(c, p, (int[]){(int)x, (int)SX(gx + 1), (int)SX(gx + 1), (int)x}, (int[]){(int)y, (int)y, (int)SY(gy + 1), (int)SY(gy + 1)}, 0xFF26385C);
		static const int nb[4][2] = {{1, 0}, {0, 1}, {1, 1}, {1, -1}};
		for (int k = 0; k < 4; k++) if (LIVE(gx + nb[k][0], gy + nb[k][1]))
			cos_line(c, p, x, y, SX(gx + nb[k][0]), SY(gy + nb[k][1]), 0xFFB4C8EC, .95f);
	}
	for (int gy = gy0; gy <= gy1; gy++) for (int gx = gx0; gx <= gx1; gx++) if (LIVE(gx, gy))
		cos_glow(c, p, SX(gx), SY(gy), fmax(2.5, sp * .38), 0xFFEAF1FF, 1.f);
#undef LIVE
#undef SX
#undef SY
}
static void cos_terrain_colour(float h, float shade, uint32_t *out) {
	uint32_t col;
	if (h < COS_SEA) col = cos_mix(0xFF0A1E46, 0xFF2A5C96, cos_clamp((h - .25f) / (COS_SEA - .25f), 0, 1));
	else if (h < COS_SEA + .04f) col = 0xFFC2B68A;
	else if (h < .70f) col = cos_mix(0xFF4F7A55, 0xFF2F5B3F, (h - COS_SEA) / .18f);
	else if (h < .82f) col = cos_mix(0xFF7A6E62, 0xFF9A9088, (h - .70f) / .12f);
	else col = 0xFFF0F4F8;
	*out = cos_mix(0xFF000000, col, cos_clamp(shade, 0, 1.25f));
}
static void cos_land(struct ca *c, uint32_t *p, double s) {
	const struct cosmos *cs = &c->cs; if (!cs->hmap) return;
	enum { N = 64 };
	static float hh[(N + 1) * (N + 1)];
	for (int j = 0; j <= N; j++) for (int i = 0; i <= N; i++) {
		int mx = (i % N) * COS_MAP_W / N, my = (j % N) * COS_MAP_H / N;
		hh[j * (N + 1) + i] = cs->hmap[my * COS_MAP_W + mx];
	}
	double tw = c->W * 1.1 / N * s, th = tw * .5, hz = tw * 4.5, cx = c->W / 2.0, cy = c->H * .52;
#define VH(I, J) fmaxf(hh[(J) * (N + 1) + (I)], COS_SEA)
#define PX(I, J) (cx + ((I) - (J)) * tw / 2)
#define PY(I, J) (cy + ((I) + (J) - N) * th / 2 - (VH(I, J) - COS_SEA) * hz)
	for (int d = 0; d <= 2 * N - 2; d++) for (int i = (d - (N - 1) > 0 ? d - (N - 1) : 0); i <= (d < N - 1 ? d : N - 1); i++) {
		int j = d - i;
		float h = (hh[j * (N + 1) + i] + hh[j * (N + 1) + i + 1] + hh[(j + 1) * (N + 1) + i + 1] + hh[(j + 1) * (N + 1) + i]) * .25f;
		float slope = (VH(i + 1, j) - VH(i, j + 1)) * 5.f;
		uint32_t col; cos_terrain_colour(h, .78f + slope * .35f + (h < COS_SEA ? .06f * sinf((float)cs->t * 1.7f + i * .9f + j * .6f) : 0), &col);
		int xs[4] = {(int)PX(i, j), (int)PX(i + 1, j), (int)PX(i + 1, j + 1), (int)PX(i, j + 1)};
		int ys[4] = {(int)PY(i, j), (int)PY(i + 1, j), (int)PY(i + 1, j + 1), (int)PY(i, j + 1)};
		quad(c, p, xs, ys, col);
		// A live cell on dry land is a lit settlement.
		if (h > COS_SEA + .05f) {
			int ci = ((i * c->cols / N) % c->cols), cj = ((j * c->rows / N) % c->rows);
			if (c->grid[cj * c->cols + ci]) cube(c, p, (int)((xs[0] + xs[2]) / 2), (int)((ys[0] + ys[2]) / 2 - tw * .25), (int)fmax(1, tw * .22), c->age[cj * c->cols + ci]);
		}
	}
#undef VH
#undef PX
#undef PY
}
static void cos_planet(struct ca *c, uint32_t *p, double s) {
	const struct cosmos *cs = &c->cs; if (!cs->hmap || !cs->cmap) return;
	int R = (int)(fmin(c->W, c->H) * .27 * s); if (R < 2) return;
	int cx = c->W / 2, cy = c->H / 2, step = R > 200 ? 2 : 1;
	double rot = cs->t * .05, tilt = .4, ct = cos(tilt), st = sin(tilt);
	const double lx = -.55, ly = -.45, lz = .70, ln = sqrt(lx * lx + ly * ly + lz * lz);
	for (int y = cy - (int)(R * 1.2); y <= cy + (int)(R * 1.2); y += step) for (int x = cx - (int)(R * 1.2); x <= cx + (int)(R * 1.2); x += step) {
		if (x < 0 || y < 0 || x >= c->W || y >= c->H) continue;
		double dx = (double)(x - cx) / R, dy = (double)(y - cy) / R, r2 = dx * dx + dy * dy;
		if (r2 <= 1.0) {
			double nz = sqrt(1 - r2), ny = -dy, nx = dx;
			double shade = fmax(0, (nx * lx + ny * ly + nz * lz) / ln);
			double py = ny * ct - nz * st, pz = ny * st + nz * ct; // tilt the globe's axis
			double lon = atan2(nx, pz) / (2 * M_PI) + rot, lat = asin(py < -1 ? -1 : py > 1 ? 1 : py);
			lon -= floor(lon);
			int mx = (int)(lon * COS_MAP_W) % COS_MAP_W, my = (int)((.5 - lat / M_PI) * COS_MAP_H); if (my < 0) my = 0; if (my >= COS_MAP_H) my = COS_MAP_H - 1;
			float h = cs->hmap[my * COS_MAP_W + mx]; uint32_t col;
			{ float pf = cos_clamp((float)(fabs(lat) - 1.0) / .4f, 0, 1); h = cos_lerp(h, .6f, pf * pf); } // flatten the polar pinch
			cos_terrain_colour(h, (float)(.08 + shade * 1.0), &col);
			{ float ice = cos_clamp((float)(fabs(lat) - 1.18) / .2f, 0, 1); col = cos_mix(col, cos_mix(0xFF1A2230, 0xFFC9D6E6, (float)fmin(1., .2 + shade)), ice); }
			float cl = cs->cmap[my * COS_MAP_W + (mx + (int)(cs->t * 1.5)) % COS_MAP_W];
			if (cl > .58f) col = cos_mix(col, 0xFFF4F7FB, cos_clamp((cl - .58f) * 3, 0, .75f) * (float)((.15 + shade) * cos(lat)));
			if (shade < .04 && h > COS_SEA) { // night side: live cells are city lights
				int gx = (int)(lon * c->cols) % c->cols, gy = (int)((.5 - lat / M_PI) * c->rows); if (gy < 0) gy = 0; if (gy >= c->rows) gy = c->rows - 1;
				if (c->grid[gy * c->cols + gx]) col = 0xFFFFD98A;
			}
			for (int oy = 0; oy < step; oy++) for (int ox = 0; ox < step; ox++) if (x + ox < c->W && y + oy < c->H) p[(size_t)(y + oy) * c->W + x + ox] = col;
		} else if (r2 <= 1.17) { // atmosphere
			double rim = 1 - (sqrt(r2) - 1) / .17; double lit = .25 + .75 * fmax(0, (dx * lx - dy * ly) / ln + .3);
			for (int oy = 0; oy < step; oy++) for (int ox = 0; ox < step; ox++) cos_add(c, p, x + ox, y + oy, 0xFF4F8CFF, (float)(rim * rim * .55 * lit));
		}
	}
}
static float cos_gas_w(const struct cosmos *cs, float u, float v, uint32_t seed, float dens_weight) {
	float n = cos_fbm(u * 5 + (float)cs->t * .004f, v * 3, seed ^ 0x9e3779b9u, 5);
	return cos_clamp(.85f * n + dens_weight * cos_dens_norm(cs, u, v) - .22f, 0, 1);
}
static float cos_gas(const struct cosmos *cs, float u, float v, uint32_t seed) { return cos_gas_w(cs, u, v, seed, .3f); }
// Gas is sampled every 12 px and smoothly upsampled: the noise is the expensive part, the picture is soft anyway.
static void cos_gas_draw(struct ca *c, uint32_t *p, double wide, double tall, uint32_t seed, float dens_weight, uint32_t c0, uint32_t c1, float gain) {
	const struct cosmos *cs = &c->cs; const int step = 12;
	int gw = c->W / step + 2, gh = c->H / step + 2; size_t need = (size_t)gw * gh;
	if (c->cs.gas_n < need) { free(c->cs.gas); c->cs.gas = malloc(sizeof(float) * need); c->cs.gas_n = c->cs.gas ? need : 0; }
	float *f = c->cs.gas; if (!f) return;
	uint32_t lut[64]; for (int i = 0; i < 64; i++) lut[i] = cos_mix(c0, c1, cos_clamp(i / 63.f * 1.3f - .3f, 0, 1));
	for (int j = 0; j < gh; j++) for (int i = 0; i < gw; i++)
		f[j * gw + i] = cos_gas_w(cs, (float)((i * step - c->W / 2.0) / wide + .5), (float)((j * step - c->H / 2.0) / tall + .5), seed, dens_weight);
	for (int y = 0; y < c->H; y++) {
		int j = y / step; float fy = (float)(y % step) / step;
		for (int x = 0; x < c->W; x++) {
			int i = x / step; float fx = (float)(x % step) / step;
			float g = cos_lerp(cos_lerp(f[j * gw + i], f[j * gw + i + 1], fx), cos_lerp(f[(j + 1) * gw + i], f[(j + 1) * gw + i + 1], fx), fy);
			if (g < .1f) continue;
			cos_add(c, p, x, y, lut[(int)(g * 63)], g * g * gain);
		}
	}
}
static void cos_stars(struct ca *c, uint32_t *p, double s) {
	const struct cosmos *cs = &c->cs;
	double wide = c->W * s, tall = c->W * .6 * s; if (wide < 8) return;
	cos_gas_draw(c, p, wide, tall, cs->seed, .3f, 0xFF243C8C, 0xFFB45AA0, .7f);
	for (int i = 0; i < cs->nstars; i++) for (int ty = -1; ty <= 1; ty++) for (int tx = -1; tx <= 1; tx++) {
		const struct cosmos_star *st = &cs->stars[i];
		double x = c->W / 2.0 + (st->x + tx - .5) * wide, y = c->H / 2.0 + (st->y + ty - .5) * tall;
		if (x < -40 || y < -40 || x > c->W + 40 || y > c->H + 40) continue;
		double age = cs->t - st->born; float flash = (float)(2 * exp(-age / 1.2)), rise = (float)fmin(1, age / .8);
		float inten = st->mag * (rise * .85f + flash);
		uint32_t col = cos_mix(0xFFFFD27A, 0xFFBFD8FF, st->mag);
		cos_glow(c, p, x, y, (2 + 4 * st->mag) * fmax(.5, s) + flash * 5, col, inten);
		cos_add(c, p, (int)x, (int)y, 0xFFFFFFFF, inten);
		if (st->mag > .75f) { double L = (6 + 14 * st->mag) * fmax(.5, s);
			cos_line(c, p, x - L, y, x + L, y, col, inten * .5f); cos_line(c, p, x, y - L, x, y + L, col, inten * .5f); }
	}
}
// The galaxy's stars never change, only their angle: hashes, radii and colours are computed once.
#define COS_GALAXY_STARS 7000
static struct { float r, th, omega, bright; uint32_t col; } cos_gal[COS_GALAXY_STARS];
static void cos_galaxy_init(void) {
	static bool ready; if (ready) return; ready = true;
	for (int i = 0; i < COS_GALAXY_STARS; i++) {
		float h1 = cos_hash(i, 1, 77), h2 = cos_hash(i, 2, 77), h3 = cos_hash(i, 3, 77);
		if (i < 1300) { float r = powf(h1, 2.f) * .2f; cos_gal[i].r = r; cos_gal[i].th = h2 * 6.2832f; cos_gal[i].omega = .15f; cos_gal[i].bright = .6f; cos_gal[i].col = 0xFFFFDDAA; } // bulge
		else { float r = powf(h1, .7f); cos_gal[i].r = r; cos_gal[i].omega = .5f / (.15f + r) * .05f;
			cos_gal[i].th = (i & 1) * 3.14159f + r * 5.5f + (h2 - .5f) * .6f * (1.15f - r);
			cos_gal[i].bright = cos_clamp(.32f + (1 - r) * .5f + h3 * .35f, 0, 1);
			cos_gal[i].col = cos_mix(0xFFFFD6A0, 0xFFA8C4FF, cos_clamp(r * 1.2f, 0, 1)); }
	}
}
static void cos_galaxy(struct ca *c, uint32_t *p, double s) {
	const struct cosmos *cs = &c->cs;
	double Rg = fmin(c->W, c->H) * .62 * s; if (Rg < 4) return;
	double tilt = .5, ct = cos(tilt), st = sin(tilt), cx = c->W / 2.0, cy = c->H / 2.0;
	cos_galaxy_init();
	cos_glow(c, p, cx, cy, Rg * .26, 0xFFFFD9A0, .6f);
	for (int i = 0; i < COS_GALAXY_STARS; i++) {
		float r = cos_gal[i].r, th = cos_gal[i].th + (float)cs->t * cos_gal[i].omega, bright = cos_gal[i].bright; uint32_t col = cos_gal[i].col;
		double gx = r * cos(th), gy = r * sin(th) * .46;
		double x = cx + (gx * ct - gy * st) * Rg, y = cy + (gx * st + gy * ct) * Rg;
		cos_add(c, p, (int)x, (int)y, col, bright);
		if (bright > .6f) { cos_add(c, p, (int)x + 1, (int)y, col, bright * .6f); cos_add(c, p, (int)x, (int)y + 1, col, bright * .6f); cos_add(c, p, (int)x + 1, (int)y + 1, col, bright * .4f); }
	}
}
static void cos_universe(struct ca *c, uint32_t *p, double s) {
	const struct cosmos *cs = &c->cs; if (!cs->wx) return;
	double a = 1 + .014 * cs->uni_t, R = fmin(c->W, c->H) * .5 * s * a, cx = c->W / 2.0, cy = c->H / 2.0, ax = c->W / (double)c->H;
	cos_gas_draw(c, p, c->W * a * s * 1.6, c->W * .6 * a * s * 1.6, cs->seed ^ 0xC0FFEEu, 0.f, 0xFF3A1018, 0xFFB04050, .85f); // relic glow: warm and smooth
	for (int e = 0; e < cs->nedges; e++)
		cos_line(c, p, cx + cs->nodex[cs->edge_a[e]] * R * ax, cy + cs->nodey[cs->edge_a[e]] * R, cx + cs->nodex[cs->edge_b[e]] * R * ax, cy + cs->nodey[cs->edge_b[e]] * R, 0xFF7C96C8, .26f);
	for (int g = 0; g < COS_WEB_GALAXIES; g++) {
		double x = cx + cs->wx[g] * R * ax, y = cy + cs->wy[g] * R;
		if (x < 0 || y < 0 || x >= c->W || y >= c->H) continue;
		float z = cos_clamp((float)(hypot(cs->wx[g], cs->wy[g]) * a * .55), 0, 1); // redshift grows with distance
		uint32_t col = cos_mix(0xFFD8E6FF, 0xFFFF7A45, z);
		float b = cs->wb[g] * (1 - z * .25f) * 1.3f;
		cos_add(c, p, (int)x, (int)y, col, b); cos_add(c, p, (int)x + 1, (int)y, col, b * .6f); cos_add(c, p, (int)x, (int)y + 1, col, b * .6f);
		if (cs->wb[g] > .7f) cos_glow(c, p, x, y, 3.5, col, .55f);
	}
}

static void cosmos_layer_to_frame(struct ca *c, uint32_t *frame, const uint32_t *layer, double weight) {
	int w256 = (int)(weight * 256); if (w256 <= 0) return;
	size_t n = (size_t)c->W * c->H;
	for (size_t i = 0; i < n; i++) {
		uint32_t l = layer[i]; if (!(l & 0xFFFFFF)) continue;
		uint32_t o = frame[i];
		unsigned r = (o >> 16 & 255) + (((l >> 16) & 255) * w256 >> 8), g = (o >> 8 & 255) + (((l >> 8) & 255) * w256 >> 8), b = (o & 255) + ((l & 255) * w256 >> 8);
		frame[i] = 0xFF000000u | ((r > 255 ? 255 : r) << 16) | ((g > 255 ? 255 : g) << 8) | (b > 255 ? 255 : b);
	}
}
static void render_cosmos(struct ca *c, uint32_t *frame) {
	struct cosmos *cs = &c->cs;
	size_t need = (size_t)c->W * c->H;
	if (cs->layer_n != need) { free(cs->layer); cs->layer = malloc(need * 4); cs->layer_n = cs->layer ? need : 0; }
	if (!cs->layer) return;
	typedef void (*phase_fn)(struct ca *, uint32_t *, double);
	static const phase_fn fns[COS_PHASES] = { cos_cells, cos_geometry, cos_land, cos_planet, cos_stars, cos_galaxy, cos_universe };
	for (int k = 0; k < COS_PHASES; k++) {
		double s, w; cos_view(cs->zoom, k, &s, &w);
		if (w <= 0.003) continue;
		memset(cs->layer, 0, need * 4);
		fns[k](c, cs->layer, s);
		cosmos_layer_to_frame(c, frame, cs->layer, w);
	}
}

static bool cosmos_set(struct ca *c, bool on) {
	if (on == c->cs.on) return true;
	c->cs.on = on;
	if (on) {
		c->edit = false; c->manual = false; update_input(c);
		if (c->volume) { c->volume = false; c->slice = -1; if (!sim_alloc(c)) { c->running = false; return false; } reseed(c); }
		if (c->cs.speed <= 0) c->cs.speed = 1;
		c->cs.auto_zoom = true; c->cs.dir = 1; c->cs.dwell = COS_DWELL_S;
		c->cs.seed = c->base_seed ? c->base_seed : c->seed | 1; // fixed for the session: rule changes keep the landscape
	}
	if (!apply_geometry(c)) return false; // the cosmos draws at buffer scale 1
	if (on) { cosmos_observe(c); cosmos_rebuild(c); }
	return true;
}
static int cosmos_phase_index(const char *name) {
	for (int k = 0; k < COS_PHASES; k++) if (!strcmp(name, COS_NAMES[k])) return k;
	char *end; long v = strtol(name, &end, 10);
	return (*name && !*end && v >= 0 && v < COS_PHASES) ? (int)v : -1;
}
static bool cosmos_control(struct ca *c, const char *verb, const char *arg) {
	struct cosmos *cs = &c->cs;
	if (!strcmp(verb, "cosmos")) {
		if (!*arg || !strcmp(arg, "toggle")) return cosmos_set(c, !cs->on);
		if (!strcmp(arg, "on")) return cosmos_set(c, true);
		if (!strcmp(arg, "off")) return cosmos_set(c, false);
		return false;
	}
	if (!cs->on) return false;
	if (!strcmp(verb, "phase")) { int k = cosmos_phase_index(arg); if (k < 0) return false; cosmos_goto(cs, k); return true; }
	if (!strcmp(verb, "zoom")) {
		if (!strcmp(arg, "auto")) { cs->auto_zoom = true; cs->dwell = 0; cs->dir = cs->zoom >= COS_PHASES - 1 ? -1 : 1; return true; }
		if (!strcmp(arg, "hold")) { cs->auto_zoom = false; cs->target = (int)lround(cs->zoom); cs->zoom = cs->target; return true; }
		char *end; double z = strtod(arg, &end); if (!*arg || *end || !isfinite(z) || z < 0 || z > COS_PHASES - 1) return false;
		cs->auto_zoom = false; cs->zoom = z; cs->target = (int)lround(z); return true;
	}
	if (!strcmp(verb, "speed")) { char *end; double v = strtod(arg, &end); if (!*arg || *end || !isfinite(v) || v < .1 || v > 8) return false; cs->speed = v; return true; }
	return false;
}
static void cosmos_config(struct ca *c) {
	const char *env = getenv("CUBE_CA_COSMOS"); bool want = false;
	char user[512]; const char *home = getenv("HOME"); const char *paths[2] = { "/etc/whitecube/config", NULL };
	if (home && snprintf(user, sizeof(user), "%s/.config/whitecube/config", home) < (int)sizeof(user)) paths[1] = user;
	for (int i = 0; i < 2; i++) { if (!paths[i]) continue; FILE *f = fopen(paths[i], "r"); char line[128]; if (!f) continue;
		while (fgets(line, sizeof(line), f)) { if (!strncmp(line, "cosmos=", 7)) want = line[7] == '1'; }
		fclose(f); }
	if (env && *env) want = *env == '1'; // the environment wins over both config files
	c->cs.speed = 1; c->cs.want_on = want;
}
