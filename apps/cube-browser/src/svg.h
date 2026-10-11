#ifndef CB_SVG_H
#define CB_SVG_H

#include <cairo.h>
#include <stddef.h>

/* Restricted, static SVG image decoding. Memory input only; no resource I/O. */
cairo_surface_t *cb_svg_decode(const void *data, size_t length,
                               char *error, size_t error_size);

#endif
