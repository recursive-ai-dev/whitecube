/* Shared palette for the native bar, launcher and quick settings. */
#ifndef WHITECUBE_STATUS_THEME_H
#define WHITECUBE_STATUS_THEME_H
#include <ctype.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct status_theme {
	char background[7], bar[7], panel[7], surface[7], hover[7];
	char ink[7], muted[7], cube_top[7], cube_side[7];
};
static inline bool status_theme_color(char out[7], const char *value) {
	if (!value) return false;
	while (isspace((unsigned char)*value)) value++;
	if (*value == '#') value++;
	for (unsigned i = 0; i < 6; i++) if (!isxdigit((unsigned char)value[i])) return false;
	const char *rest = value + 6;
	while (isspace((unsigned char)*rest)) rest++;
	if (*rest != '\0') return false;
	for (unsigned i = 0; i < 6; i++) out[i] = (char)tolower((unsigned char)value[i]);
	out[6] = '\0';
	return true;
}
static inline double status_theme_luminance(const char *hex) {
	unsigned value = (unsigned)strtoul(hex, NULL, 16);
	double channel[3];
	for (unsigned i = 0; i < 3; i++) {
		double c = ((value >> (16 - i * 8)) & 255) / 255.0;
		channel[i] = c <= .04045 ? c / 12.92 : pow((c + .055) / 1.055, 2.4);
	}
	return .2126 * channel[0] + .7152 * channel[1] + .0722 * channel[2];
}
static inline void status_theme_mix(char out[7], const char *a, const char *b, double amount) {
	unsigned av = (unsigned)strtoul(a, NULL, 16), bv = (unsigned)strtoul(b, NULL, 16), result = 0;
	for (unsigned shift = 0; shift <= 16; shift += 8) {
		unsigned channel = (unsigned)lround(((av >> shift) & 255) * (1 - amount) + ((bv >> shift) & 255) * amount);
		result |= channel << shift;
	}
	snprintf(out, 7, "%06x", result);
}
static inline void status_theme_defaults(struct status_theme *theme) {
	*theme = (struct status_theme){.background = "06080d", .bar = "090c12"};
}
static inline void status_theme_finish(struct status_theme *theme, bool strong) {
	bool dark = status_theme_luminance(theme->background) < .18;
	snprintf(theme->ink, 7, "%s", dark ? "ffffff" : "0d0d0d");
	if (strong) {
		snprintf(theme->background, 7, "%s", dark ? "000000" : "ffffff");
		snprintf(theme->bar, 7, "%s", theme->background);
		snprintf(theme->panel, 7, "%s", theme->background);
		snprintf(theme->surface, 7, "%s", theme->background);
		snprintf(theme->ink, 7, "%s", dark ? "ffffff" : "000000");
		snprintf(theme->muted, 7, "%s", theme->ink);
	} else {
		status_theme_mix(theme->panel, theme->background, theme->ink, .045);
		status_theme_mix(theme->surface, theme->panel, theme->ink, .035);
		status_theme_mix(theme->muted, theme->panel, theme->ink, .68);
	}
	status_theme_mix(theme->hover, theme->surface, theme->ink, .09);
	status_theme_mix(theme->cube_top, theme->surface, theme->ink, .16);
	status_theme_mix(theme->cube_side, theme->surface, theme->ink, .08);
}
#endif
