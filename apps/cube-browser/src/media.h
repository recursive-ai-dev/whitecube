#ifndef CB_MEDIA_H
#define CB_MEDIA_H
/* Viewport dimensions are CSS pixels; initial em/rem is 16 CSS pixels. */
struct cb_media_env { double width, height; int reduced_motion, strong_contrast; };
/* Bounded screen media-query subset. Unknown/malformed queries do not match;
 * comma-separated alternatives recover independently. Empty attributes match. */
int cb_media_matches(const char *query, const struct cb_media_env *environment);
#endif
