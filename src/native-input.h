#ifndef WHITECUBE_NATIVE_INPUT_H
#define WHITECUBE_NATIVE_INPUT_H

#include <locale.h>
#include <xkbcommon/xkbcommon-compose.h>

// Each native client owns a Compose state; toolkit-free text fields need the
// same dead-key and Compose processing that GTK normally supplies.
static inline struct xkb_compose_state *wc_compose_create(struct xkb_context *context) {
	const char *locale = setlocale(LC_CTYPE, NULL);
	struct xkb_compose_table *table = xkb_compose_table_new_from_locale(context,
		locale && *locale ? locale : "C.UTF-8", XKB_COMPOSE_COMPILE_NO_FLAGS);
	if (!table) return NULL;
	struct xkb_compose_state *state = xkb_compose_state_new(table, XKB_COMPOSE_STATE_NO_FLAGS);
	xkb_compose_table_unref(table);
	return state;
}

// Return true when the key is part of a Compose sequence. A completed sequence
// returns its UTF-8 text; an unfinished/cancelled sequence consumes the key.
static inline bool wc_compose_key(struct xkb_compose_state *state, xkb_keysym_t symbol,
		char *text, size_t capacity) {
	if (!state || xkb_compose_state_feed(state, symbol) == XKB_COMPOSE_FEED_IGNORED) return false;
	switch (xkb_compose_state_get_status(state)) {
	case XKB_COMPOSE_COMPOSING: text[0] = 0; return true;
	case XKB_COMPOSE_COMPOSED:
		xkb_compose_state_get_utf8(state, text, capacity);
		xkb_compose_state_reset(state); return true;
	case XKB_COMPOSE_CANCELLED:
		xkb_compose_state_reset(state); text[0] = 0; return true;
	default: return false;
	}
}

#endif
