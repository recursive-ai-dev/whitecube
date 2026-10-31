#ifndef CUBE_ENGINE_H
#define CUBE_ENGINE_H

#include <cairo.h>
#include <stddef.h>

/* UI scale (0.5–3) multiplies page zoom (0.5–3) before layout. */
#define CB_MIN_EFFECTIVE_ZOOM 0.25
#define CB_MAX_EFFECTIVE_ZOOM 9.0

struct cb_page;

enum cb_resource_kind { CB_RESOURCE_CSS = 1, CB_RESOURCE_IMAGE = 2, CB_RESOURCE_SCRIPT = 3 };
enum cb_interact_action { CB_INTERACT_CLICK=1, CB_INTERACT_TEXT=2, CB_INTERACT_KEY=3,
    CB_INTERACT_NEXT=4, CB_INTERACT_PREVIOUS=5, CB_INTERACT_BLUR=6 };
enum cb_interaction_kind { CB_INTERACTION_NONE=0, CB_INTERACTION_UPDATE=1,
    CB_INTERACTION_NAVIGATE=2, CB_INTERACTION_NOTICE=3, CB_INTERACTION_SUBMIT=4,
    CB_INTERACTION_COPY=5, CB_INTERACTION_POST=6 };
enum cb_focus_kind { CB_FOCUS_NONE=0, CB_FOCUS_LINK=1, CB_FOCUS_EDIT=2, CB_FOCUS_CONTROL=3 };
struct cb_interaction { unsigned kind, focus; double y; char text[4096], body[16385]; };

/* Input is bounded to 4 MiB; DOM integration runs retained scripts separately.
 * All coordinates are pixels. */
struct cb_page *cb_page_parse(const char *html, size_t len, const char *base_url);
/* Construct a static image document; bytes are never parsed as markup. */
struct cb_page *cb_page_from_image(const void *bytes, size_t length,
                                    const char *mime, const char *url);
void cb_page_set_images_enabled(struct cb_page *page, int enabled);
void cb_page_set_policy(struct cb_page *page,const char *policy);
void cb_page_add_css(struct cb_page *page, const char *css, size_t len);
/* Immutable discovery list: at most 32 unique HTTP(S) resources. No I/O here. */
size_t cb_page_resource_count(const struct cb_page *page);
int cb_page_resource(const struct cb_page *page, size_t index, int *kind, const char **url);
/* Copies CSS or decodes an image. Call layout again after a successful update. */
int cb_page_set_resource(struct cb_page *page, size_t index, const void *bytes,
                         size_t length, const char *mime);
/* Physical viewport height; media queries use CSS pixels after dividing by zoom. */
void cb_page_set_environment(struct cb_page *page, int physical_height,
                              int reduced_motion, int strong_contrast);
void cb_page_layout(struct cb_page *page, int width, double zoom);
void cb_page_paint(struct cb_page *page, cairo_t *cr, double scroll, int viewport_height);
const char *cb_page_title(const struct cb_page *page);
double cb_page_height(const struct cb_page *page);
/* y is in document coordinates, so callers add their scroll position. */
const char *cb_page_link_at(const struct cb_page *page, double x, double y);
int cb_page_find(const struct cb_page *page, const char *query, double after_y, double *found_y);
/* Click coordinates are document pixels. KEY uses lowercase names. */
int cb_page_interact(struct cb_page *page, unsigned action, double x, double y,
                     const char *text, struct cb_interaction *out);
/* Returns 1 if found (including empty fragment), otherwise 0. */
int cb_page_anchor(const struct cb_page *page, const char *fragment, double *y);
/* Update a fragment without changing the document identity. */
int cb_page_set_url(struct cb_page *page, const char *url);
/* Returns a malloc-owned UTF-8 plain-text document. */
char *cb_page_text(const struct cb_page *page);
size_t cb_page_node_count(const struct cb_page *page);
void cb_page_free(struct cb_page *page);

#endif
