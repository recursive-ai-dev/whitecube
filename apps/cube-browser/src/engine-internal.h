#ifndef CB_ENGINE_INTERNAL_H
#define CB_ENGINE_INTERNAL_H
#define _POSIX_C_SOURCE 200809L
#include "engine.h"
#include "image.h"
#include "forms.h"
#include "media.h"
#include "policy.h"

#include <ctype.h>
#include <errno.h>
#include <glib.h>
#include <math.h>
#include <pango/pangocairo.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#define HTML_LIMIT (4u * 1024u * 1024u)
#define CSS_LIMIT (1024u * 1024u)
#define NODE_LIMIT 16000u
#define RULE_LIMIT 1024u
#define DEPTH_LIMIT 128u
#define TEXT_LIMIT (1024u * 1024u)
#define ITEM_LIMIT 32000u
#define ATTR_LIMIT 8192u
#define RESOURCE_LIMIT 32u
#define STYLESHEET_LIMIT 256u
#define CONTROL_LIMIT 128u
#define VALUE_LIMIT 4095u
#define OPTION_LIMIT 64u
#define MEDIA_DEPTH_LIMIT 16u
#define MEDIA_QUERY_LIMIT 2048u
#define TABLE_ROW_LIMIT 128u
#define TABLE_COLUMN_LIMIT 16u
#define TABLE_CELL_LIMIT 1024u

enum display { D_INLINE, D_BLOCK, D_NONE, D_FLEX, D_TABLE, D_ROWGROUP, D_ROW, D_CELL, D_CAPTION, D_GRID };
enum len_kind { L_AUTO, L_PX, L_PERCENT, L_EM };
struct length { double value; enum len_kind kind; };
struct color { double r, g, b, a; };
struct style {
    enum display display;
    struct color color, background, border_color;
    double font_size, line_height, border, gap, table_spacing;
    int collapse_borders;
    struct length margin[4], padding[4], width, height, max_width, min_height;
    int weight, italic, underline, family, align, pre, flex_column;
    double grow, shrink, row_gap;
    struct length basis, min_width, max_height, inset[4];
    int wrap, justify, align_items, align_self, grid_columns, position, overflow, content_box;
};
enum control_kind { C_TEXT, C_HIDDEN, C_CHECKBOX, C_RADIO, C_SUBMIT, C_RESET, C_BUTTON,
    C_TEXTAREA, C_SELECT, C_PASSWORD, C_FILE, C_UNSUPPORTED };
struct control;
struct node {
    struct cb_page *page;
    unsigned uid;
    GHashTable *attrs, *custom;
    char tag[24];
    char *text, *id, *classes, *inline_css, *href, *alt;
    char *src, *rel, *media;
    char *name, *value, *type, *action, *method, *placeholder, *external_form;
    char *submit_action, *submit_method;
    unsigned maxlength;
    struct control *control;
    double x,y,w,h;
    unsigned laid_out : 1;
    unsigned clipped_out : 1;
    unsigned readonly : 1;
    unsigned checked : 1;
    unsigned selected : 1;
    unsigned multiple : 1;
    unsigned tab_skip : 1;
    unsigned image_width, image_height, colspan, rowspan;
    size_t resource;
    struct node *parent, *first, *last, *next;
    struct style style;
    unsigned hidden : 1;
    unsigned disabled : 1;
};
struct option { struct node *node; char *label, *value; int incomplete; };
struct control {
    struct node *node, *form;
    enum control_kind kind;
    char *value, *initial, *label;
    size_t cursor, maxlength;
    int selected, initial_selected;
    struct option options[OPTION_LIMIT];
    unsigned option_count;
    unsigned checked : 1;
    unsigned initial_checked : 1;
    unsigned select_all : 1;
    unsigned incomplete : 1;
    unsigned initial_incomplete : 1;
    unsigned options_overflow : 1;
};
struct rule { char *selector, *declarations; unsigned specificity, order; };
struct resource { int kind,loaded,failed; char *url, *css; size_t css_length; cairo_surface_t *image; };
struct stylesheet { char *css; size_t length, resource; char *media; };
struct item {
    const struct node *owner;
    int clipped;
    double clip_x, clip_y, clip_w, clip_h;
    double x, y, w, h;
    struct color background, border_color;
    double border;
    PangoLayout *layout;
    cairo_surface_t *image;
    struct control *control;
    PangoRectangle caret;
    int text_height, placeholder;
};
struct hit { double x, y, w, h; const char *url; const struct node *node; };
struct page_script {struct node *node;size_t resource;char *source;int defer,done,unsupported;};
struct cb_page {
    GPtrArray *nodes;
    struct page_script scripts[64];
    size_t script_count,script_bytes;
    struct node *root;
    char *title, *base, *policy;
    struct rule *rules;
    size_t rule_count, node_count;
    int custom_rules;
    struct resource resources[RESOURCE_LIMIT];
    size_t resource_count, image_pixels;
    struct stylesheet sheets[STYLESHEET_LIMIT];
    size_t sheet_count, css_bytes;
    int parsing;
    struct item *items;
    size_t item_count;
    struct hit *hits;
    size_t hit_count;
    double height, zoom;
    int width, has_body, viewport_height, reduced_motion, strong_contrast;
    PangoContext *context;
    struct control controls[CONTROL_LIMIT];
    size_t control_count;
    int control_overflow, parse_incomplete;
    struct node *focus;
};
struct span { guint begin, end; struct style style; const char *url; const struct node *image, *origin; struct control *control; };
struct inline_flow { GString *text; GArray *spans; };

static inline struct color cb_engine_rgb(unsigned v) {
    return (struct color){ ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0,
                           (v & 255) / 255.0, 1 };
}
static inline char *cb_engine_slice(const char *s, size_t len) { return g_strndup(s, len); }
static inline char *cb_engine_trim(char *s) { return g_strstrip(s); }
static inline int cb_engine_tag_is(const struct node *n, const char *tag) { return !strcmp(n->tag, tag); }
static inline int cb_engine_block_display(enum display d){return d!=D_INLINE && d!=D_NONE;}
static inline int cb_engine_in_words(const char *words, const char *word) {
    size_t len = strlen(word);
    while (*words) {
        while (g_ascii_isspace(*words)) words++;
        const char *end = words;
        while (*end && !g_ascii_isspace(*end)) end++;
        if ((size_t)(end - words) == len && !strncmp(words, word, len)) return 1;
        words = end;
    }
    return 0;
}
static inline const char *cb_engine_find_ci(const char *s, const char *needle) {
    size_t len = strlen(needle);
    for (; *s; s++) if (!g_ascii_strncasecmp(s, needle, len)) return s;
    return NULL;
}

/* Entity decoding is single-pass: replacement text is never expanded again. */

struct table_grid;
double cb_engine_layout_flex(struct cb_page *p,struct node *n,double x,double y,double width);
void cb_engine_translate(struct cb_page *p,struct node *n,size_t first_item,size_t last_item,
                         size_t first_hit,size_t last_hit,double dx,double dy);
void cb_engine_translate_nodes(struct node *n,double dx,double dy);

/* Internal engine contracts; use engine.h outside the engine. */
char *cb_engine_decode(const char *s, size_t len);
struct node *cb_engine_new_node(struct cb_page *p, struct node *parent, const char *tag);
int cb_engine_is_block_tag(const char *tag);
int cb_engine_is_void(const char *tag);
char *cb_engine_resolve_url(struct cb_page *p, const char *href);
int cb_engine_name_char(char c);
unsigned cb_engine_specificity(const char *s);
int cb_engine_compound_matches(const struct node *n, const char *s, size_t len);
int cb_engine_selector_matches(const struct node *n, const char *s);
struct cb_media_env cb_engine_media_environment(const struct cb_page *p);
char *cb_engine_css_boundary(char *start,char *end,int closing);
void cb_engine_parse_css_range(struct cb_page *p,char *start,char *end,unsigned depth);
void cb_engine_parse_css_rules(struct cb_page *p,const char *css,size_t len);
void cb_engine_rebuild_rules(struct cb_page *p);
void cb_engine_add_css_media(struct cb_page *p,const char *css,size_t len,const char *media);
int cb_engine_remote_url(const char *url);
size_t cb_engine_discover_resource(struct cb_page *p, int kind, const char *value);
int cb_engine_mime_is(const char *mime, const char *expected);
int cb_engine_screen_media(const char *media);
unsigned cb_engine_image_dimension(const char *value, size_t length);
unsigned cb_engine_parse_span(const char *value,size_t length,int row);
unsigned cb_engine_parse_maxlength(const char *value,size_t length);
char *cb_engine_bounded_value(const char *text, int multiline);
void cb_engine_raw_node_text(const struct node *n,GString *out);
char *cb_engine_node_text(const struct node *n,int multiline);
int cb_engine_node_disabled(const struct node *n);
struct node *cb_engine_form_id(struct node *n,const char *id);
size_t cb_engine_raw_text_length(const struct node *n);
char *cb_engine_exact_value(const char *text);
char *cb_engine_initial_value(const char *text,enum control_kind kind);
void cb_engine_init_options(struct control *c,struct node *n);
void cb_engine_init_controls(struct cb_page *p,struct node *n);
int cb_engine_parse_color(const char *s, struct color *out);
int cb_engine_parse_length(const char *value, struct length *out);
double cb_engine_length_value(struct length l, double basis, double font, double zoom);
void cb_engine_set_sides(struct length *out, uint64_t *scores, uint64_t rank, const char *v, int side);
void cb_engine_apply_declaration(struct style *s, const struct style *parent, uint64_t *scores,
                              const char *name, const char *v, uint64_t rank);
void cb_engine_apply_declarations(struct style *s, const struct style *parent, uint64_t *scores,
                               const char *css, unsigned spec, unsigned order, const struct node *node);
void cb_engine_compute_style(struct cb_page *p, struct node *n, const struct style *parent);
cairo_surface_t *cb_engine_node_image(const struct cb_page *p, const struct node *n);
void cb_engine_image_size(const struct cb_page *p, const struct node *n, double available,
                       double *width, double *height);
int cb_engine_editable(const struct control *c);
void cb_engine_control_size(const struct cb_page *p,const struct control *c,double available,double *w,double *h);
void cb_engine_node_box(struct node *n,double x,double y,double w,double h);
void cb_engine_inline_box(const struct node *origin,double x,double y,double w,double h);
const struct node *cb_engine_link_node(const struct node *n);
struct inline_flow cb_engine_flow_new(void);
void cb_engine_flow_free(struct inline_flow *f);
void cb_engine_flow_append(struct inline_flow *f, const char *text, const struct style *s, const char *url, const struct node *origin);
void cb_engine_collect_inline(struct cb_page *p, struct inline_flow *f, const struct node *n, const char *link);
struct item *cb_engine_add_item(struct cb_page *p);
void cb_engine_prepare_control_layout(struct cb_page *p,struct item *item);
void cb_engine_control_item(struct cb_page *p,struct control *c,double x,double y,double w,double h);
void cb_engine_add_attr(PangoAttrList *attrs, PangoAttribute *a, guint start, guint end);
void cb_engine_add_hits(struct cb_page *p, PangoLayout *layout, struct inline_flow *f, double x, double y);
void cb_engine_add_inline_images(struct cb_page *p, PangoLayout *layout, const struct inline_flow *f,
                              double x, double y, double width);
double cb_engine_flush_flow(struct cb_page *p, struct inline_flow *f, double x, double y, double width,
                         const struct style *block);
double cb_engine_layout_node(struct cb_page *p, struct node *n, double x, double y, double available);
int cb_engine_whitespace_node(const struct node *n);
void cb_engine_table_rows(struct table_grid *grid,struct node *parent,struct node *group);
void cb_engine_table_cells(struct table_grid *g);
void cb_engine_shift_node_geometry(struct node *n,double dy);
size_t cb_engine_table_background(struct cb_page *p,const struct node *n,double x,double y,double width);
double cb_engine_layout_table(struct cb_page *p,struct node *table,double x,double y,double width);
void cb_engine_clear_layout(struct cb_page *p);
void cb_engine_clear_geometry(struct node *n);
void cb_engine_source_color(cairo_t *cr, struct color c);
void cb_engine_paint_control(struct cb_page *p,cairo_t *cr,const struct item *item);
void cb_engine_extract_text(const struct node *n, GString *out);
void cb_engine_free_node(struct node *n);
unsigned cb_engine_focus_kind(const struct node *n);
void cb_engine_interaction_focus(const struct cb_page *p,struct cb_interaction *out);
void cb_engine_notice(struct cb_interaction *out,const char *message);
int cb_engine_focusable(const struct node *n);
void cb_engine_focus_nodes(struct node *n,struct node **nodes,size_t *count);
void cb_engine_submit_form(struct cb_page *p,struct control *source,struct cb_interaction *out);
void cb_engine_activate_control(struct cb_page *p,struct control *c,struct cb_interaction *out);
void cb_engine_navigate_link(const struct node *n,struct cb_interaction *out);
void cb_engine_insert_value(struct control *c,const char *text,struct cb_interaction *out);
void cb_engine_vertical_cursor(struct cb_page *p,struct control *c,int direction);
const struct node *cb_engine_find_anchor(const struct node *n,const char *name);

#endif
