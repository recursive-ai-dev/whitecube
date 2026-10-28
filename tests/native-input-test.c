#undef NDEBUG
#include <assert.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include <xkbcommon/xkbcommon.h>
#include "native-input.h"

int main(void) {
    assert(setlocale(LC_CTYPE, "C.UTF-8"));
    struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS); assert(context);
    struct xkb_compose_state *state = wc_compose_create(context); assert(state);
    char text[64] = "";
    assert(wc_compose_key(state, XKB_KEY_Multi_key, text, sizeof(text)) && !text[0]);
    assert(wc_compose_key(state, XKB_KEY_apostrophe, text, sizeof(text)) && !text[0]);
    assert(wc_compose_key(state, XKB_KEY_e, text, sizeof(text)) && !strcmp(text, "é"));
    assert(wc_compose_key(state, XKB_KEY_dead_circumflex, text, sizeof(text)) && !text[0]);
    assert(wc_compose_key(state, XKB_KEY_o, text, sizeof(text)) && !strcmp(text, "ô"));
    assert(!wc_compose_key(state, XKB_KEY_a, text, sizeof(text)));
    assert(wc_compose_key(state, XKB_KEY_dead_acute, text, sizeof(text)) && !text[0]);
    xkb_compose_state_reset(state); // focus loss or keymap replacement cancels composition
    assert(!wc_compose_key(state, XKB_KEY_e, text, sizeof(text)));
    assert(wc_compose_key(state, XKB_KEY_dead_acute, text, sizeof(text)));
    assert(wc_compose_key(state, XKB_KEY_F1, text, sizeof(text)) && !text[0]);
    assert(!wc_compose_key(state, XKB_KEY_b, text, sizeof(text)));
    xkb_compose_state_unref(state); xkb_context_unref(context);
    puts("native input: Compose, dead keys, cancellation and focus reset passed");
    return 0;
}
