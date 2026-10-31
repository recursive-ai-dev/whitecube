#include "engine-internal.h"

/* Inline data stays in the renderer: no URL is sent to a network worker. The
 * parser's 8192-byte attribute limit also bounds each encoded inline image. */
static cairo_surface_t *data_image(const char *url)
{
    const char *comma = strchr(url, ',');
    if (!comma || comma - url > 128) return NULL;
    char *header = g_ascii_strdown(url + 5, comma - url - 5);
    char *parameters = strchr(header, ';');
    int base64 = 0;
    if (parameters) {
        *parameters++ = 0;
        if (!strcmp(parameters, "base64")) base64 = 1;
        else { g_free(header); return NULL; }
    }
    if (strcmp(header, "image/png") && strcmp(header, "image/jpeg") &&
        strcmp(header, "image/svg+xml")) { g_free(header); return NULL; }
    const char *body = comma + 1;
    size_t length = strcspn(body, "#"), decoded_length = 0;
    unsigned char *decoded = NULL;
    if (base64) {
        unsigned padding = 0;
        if (!length || length % 4) { g_free(header); return NULL; }
        for (size_t i = 0; i < length; i++) {
            unsigned char c = body[i];
            if (c == '=') { if (++padding > 2) { g_free(header); return NULL; } }
            else if (padding || !(g_ascii_isalnum(c) || c == '+' || c == '/')) {
                g_free(header); return NULL;
            }
        }
        char *encoded = g_strndup(body, length);
        decoded = g_base64_decode(encoded, &decoded_length);
        g_free(encoded);
    } else {
        decoded = g_malloc(length + 1);
        for (size_t i = 0; i < length; i++) {
            unsigned char c = body[i];
            if (c == '%') {
                if (i + 2 >= length || !g_ascii_isxdigit(body[i + 1]) ||
                    !g_ascii_isxdigit(body[i + 2])) { g_free(decoded); g_free(header); return NULL; }
                c = (unsigned char)((g_ascii_xdigit_value(body[i + 1]) << 4) |
                                     g_ascii_xdigit_value(body[i + 2]));
                i += 2;
            }
            decoded[decoded_length++] = c;
        }
    }
    cairo_surface_t *surface = cb_image_decode(decoded, decoded_length, header, NULL, 0);
    g_free(decoded); g_free(header);
    return surface;
}

void cb_engine_inline_images(struct cb_page *page)
{
    unsigned count = 0;
    for (guint i = 0; page->nodes && i < page->nodes->len; i++) {
        struct node *node = g_ptr_array_index(page->nodes, i);
        if (!node->src || g_ascii_strncasecmp(node->src, "data:", 5)) continue;
        if (node->inline_image) {
            page->image_pixels -= (size_t)cairo_image_surface_get_width(node->inline_image) *
                                          cairo_image_surface_get_height(node->inline_image);
            cairo_surface_destroy(node->inline_image); node->inline_image = NULL;
        }
        if (++count > RESOURCE_LIMIT || !page->images_enabled) continue;
        if (cb_engine_remote_url(page->base) &&
            !cb_policy_allows(page->policy, page->base, node->src, CB_POLICY_IMAGE, 0)) continue;
        cairo_surface_t *image = data_image(node->src);
        if (!image) continue;
        size_t pixels = (size_t)cairo_image_surface_get_width(image) * cairo_image_surface_get_height(image);
        if (pixels > 16u * 1024u * 1024u - page->image_pixels) { cairo_surface_destroy(image); continue; }
        page->image_pixels += pixels; node->inline_image = image;
    }
}

void cb_page_set_images_enabled(struct cb_page *page, int enabled)
{
    if (!page) return;
    page->images_enabled = !!enabled;
    cb_engine_inline_images(page);
}

struct cb_page *cb_page_from_image(const void *bytes, size_t length,
                                    const char *mime, const char *url)
{
    cairo_surface_t *image = cb_image_decode(bytes, length, mime, NULL, 0);
    if (!image) return NULL;
    const char html[] = "<html><head><title>Image</title></head><body><img alt='Image' style='display:block;max-width:100%'></body></html>";
    struct cb_page *page = cb_page_parse(html, sizeof html - 1, url);
    if (!page) { cairo_surface_destroy(image); return NULL; }
    for (guint i = 0; i < page->nodes->len; i++) {
        struct node *node = g_ptr_array_index(page->nodes, i);
        if (!cb_engine_tag_is(node, "img")) continue;
        node->inline_image = image;
        page->image_pixels = (size_t)cairo_image_surface_get_width(image) * cairo_image_surface_get_height(image);
        return page;
    }
    cairo_surface_destroy(image); cb_page_free(page); return NULL;
}
