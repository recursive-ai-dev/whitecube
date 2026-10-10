#ifndef WHITECUBE_TAB_PANEL_H
#define WHITECUBE_TAB_PANEL_H

// All tabs live under one count button. A panel under the bar lists every tab. Pure geometry, shared
// by the compositor and its tests. All values are output logical pixels.

// How many tabs fit with no overflow button: the largest k with left + gap + k*(w+gap) < end.
static inline int wc_tab_capacity(int left, int gap, int tab_w, int end) {
	int k = 0;
	if (tab_w < 1) return 0;
	while (left + gap + (k + 1) * (tab_w + gap) < end) k++;
	return k;
}
// The strip always reserves its only tab slot for the count button.
static inline int wc_tab_visible(int n, int capacity) {
	(void)n; (void)capacity;
	return 0;
}

struct wc_panel_geom { int x, y, w, h, pitch, pad, rows; };

// Panel under the bar, left edge at anchor_x (clamped on screen), one row per tab, as many rows as fit.
static inline struct wc_panel_geom wc_panel_geom(int W, int H, int tab_w, int tab_h, int anchor_x, int n) {
	struct wc_panel_geom g = { 0, tab_h, 0, 0, tab_h - 6, 6, 0 };
	if (g.pitch < 1) g.pitch = 1;
	int max_rows = (H - tab_h - 2 * g.pad) / g.pitch;
	if (max_rows < 1) max_rows = 1;
	g.rows = n < max_rows ? n : max_rows;
	if (g.rows < 1) g.rows = 1; // Empty workspace has a visible empty state.
	g.w = tab_w * 22 / 10;
	if (g.w > W) g.w = W;
	g.x = anchor_x;
	if (g.x + g.w > W) g.x = W - g.w;
	if (g.x < 0) g.x = 0;
	g.h = g.rows * g.pitch + 2 * g.pad;
	return g;
}
static inline int wc_panel_max_scroll(const struct wc_panel_geom *g, int n) {
	return n > g->rows ? n - g->rows : 0;
}
static inline int wc_panel_clamp_scroll(const struct wc_panel_geom *g, int n, int scroll) {
	int max = wc_panel_max_scroll(g, n);
	return scroll < 0 ? 0 : scroll > max ? max : scroll;
}
static inline int wc_panel_contains(const struct wc_panel_geom *g, double px, double py) {
	return px >= g->x && px < g->x + g->w && py >= g->y && py < g->y + g->h;
}
// Index of the tab under the pointer (scroll already added), or -1 for padding / outside.
static inline int wc_panel_row_at(const struct wc_panel_geom *g, int n, int scroll, double px, double py) {
	if (!wc_panel_contains(g, px, py)) return -1;
	int r = (int)(py - g->y - g->pad);
	if (r < 0) return -1;
	r /= g->pitch;
	if (r >= g->rows || r + scroll >= n) return -1;
	return r + scroll;
}
// True when the pointer is on the close square at the right end of a row.
static inline int wc_panel_on_close(const struct wc_panel_geom *g, int close_w, double px) {
	return px >= g->x + g->w - g->pad - close_w - 6;
}

// Keep a keyboard-selected row in view, including when the list shrinks.
static inline int wc_panel_reveal(const struct wc_panel_geom *g, int n, int scroll, int selected) {
	if (selected < scroll) scroll = selected;
	if (selected >= scroll + g->rows) scroll = selected - g->rows + 1;
	return wc_panel_clamp_scroll(g, n, scroll);
}

#endif
