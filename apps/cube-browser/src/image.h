#ifndef CB_IMAGE_H
#define CB_IMAGE_H

#include <cairo.h>
#include <stddef.h>

#define CB_IMAGE_MAX_BYTES (2U * 1024U * 1024U)
#define CB_IMAGE_MAX_DIMENSION 4096U
#define CB_IMAGE_MAX_PIXELS 8000000U

/* Decode PNG/JPEG or restricted static SVG from memory, checking MIME and budgets.
 * No files or network are accessed. Caller destroys the returned image surface.
 * Intrinsic dimensions use cairo_image_surface_get_width/height().
 * NULL indicates failure; an optional bounded error message describes it.
 */
cairo_surface_t *cb_image_decode(const void *data, size_t length, const char *mime,
                                  char *error, size_t error_size);

#endif
