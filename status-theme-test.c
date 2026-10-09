#undef NDEBUG
#include <assert.h>
#include "status-theme.h"

static double contrast(const char *a, const char *b) {
	double x = status_theme_luminance(a), y = status_theme_luminance(b);
	return x > y ? (x + .05) / (y + .05) : (y + .05) / (x + .05);
}
int main(void) {
	const char *backgrounds[] = {"06080d", "fbfbfb", "1b1b1b", "f4efe6"};
	for (unsigned i = 0; i < sizeof(backgrounds) / sizeof(*backgrounds); i++) {
		struct status_theme theme;
		status_theme_defaults(&theme);
		assert(status_theme_color(theme.background, backgrounds[i]));
		status_theme_finish(&theme, false);
		assert(contrast(theme.panel, theme.ink) > 4.5);
		assert(contrast(theme.surface, theme.ink) > 4.5);
		assert(contrast(theme.hover, theme.ink) > 4.5);
		assert(contrast(theme.panel, theme.muted) > 4.5);
		assert(strcmp(theme.surface, theme.hover));
		char original[7]; memcpy(original, theme.background, sizeof(original));
		assert(!status_theme_color(theme.background, "oops") && !strcmp(theme.background, original));
		assert(!status_theme_color(theme.background, "12345g") && !strcmp(theme.background, original));
		assert(!status_theme_color(theme.background, "1234567") && !strcmp(theme.background, original));
		status_theme_finish(&theme, true);
		assert(contrast(theme.panel, theme.ink) == 21);
		assert(!strcmp(theme.muted, theme.ink));
	}
	char encoded[7];
	assert(status_theme_color(encoded, "#ABCDEF") && !strcmp(encoded, "abcdef"));
	puts("status themes: Space, White cube, Graphite and Paper contrast, strong contrast and invalid-color handling passed");
	return 0;
}
