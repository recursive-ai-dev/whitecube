#include "svg.h"
#include "image.h"

#include <errno.h>
#include <expat.h>
#include <glib.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NANOSVG_ALL_COLOR_KEYWORDS
#define NANOSVG_IMPLEMENTATION
#if defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wsign-compare"
#endif
#include "../third_party/nanosvg/nanosvg.h"
#if defined(__GNUC__)
#pragma GCC diagnostic pop
#endif

/* Bound both source work and raster work, independently of encoded bytes. */
#define SVG_ELEMENTS 2048u
#define SVG_SHAPES 512u
#define SVG_DEPTH 32u
#define SVG_NUMBERS 32768u
#define SVG_POINTS 32768u
#define SVG_GRADIENTS 64u
#define SVG_STOPS 256u
#define SVG_GEOMETRY 65536.0
#define SVG_PAINT_PIXELS 32000000.0

enum element { E_SVG, E_GROUP, E_DEFS, E_LINEAR, E_RADIAL, E_STOP,
    E_PATH, E_RECT, E_CIRCLE, E_ELLIPSE, E_LINE, E_POLYLINE, E_POLYGON,
    E_TITLE, E_DESC, E_INVALID };
static const char *const tags[] = {"svg","g","defs","linearGradient","radialGradient","stop",
    "path","rect","circle","ellipse","line","polyline","polygon","title","desc"};

struct validator {
    XML_Parser parser;
    GString *xml;
    GHashTable *ids, *gradients;
    GPtrArray *references;
    enum element stack[SVG_DEPTH];
    unsigned depth, elements, shapes, numbers, stops, gradient_count, gradient_stops;
    bool root_seen;
    char error[160];
};

static void reject(struct validator *v, const char *message)
{
    if (!v->error[0]) g_strlcpy(v->error, message, sizeof v->error);
    XML_StopParser(v->parser, XML_FALSE);
}

static bool one_of(const char *value, const char *words)
{
    size_t length = strlen(value);
    for (const char *p = words; *p;) {
        size_t n = strcspn(p, " ");
        if (n == length && !memcmp(p, value, n)) return true;
        p += n;
        if (*p) p++;
    }
    return false;
}

static void separators(const char **cursor)
{
    while (g_ascii_isspace(**cursor) || **cursor == ',') (*cursor)++;
}

/* Parse a bounded SVG number, avoiding libc's hex/NaN/Inf extensions. */
static bool number(struct validator *v, const char **cursor, double *value)
{
    const char *start = *cursor, *p = start;
    if (*p == '+' || *p == '-') p++;
    unsigned digits = 0;
    while (g_ascii_isdigit(*p)) { p++; digits++; }
    if (*p == '.') { p++; while (g_ascii_isdigit(*p)) { p++; digits++; } }
    if (!digits) return false;
    if (*p == 'e' || *p == 'E') {
        p++;
        if (*p == '+' || *p == '-') p++;
        const char *exponent = p;
        while (g_ascii_isdigit(*p)) p++;
        if (p == exponent) return false;
    }
    if ((size_t)(p - start) > 32 || ++v->numbers > SVG_NUMBERS) return false;
    errno = 0;
    char *end;
    *value = g_ascii_strtod(start, &end);
    if (end != p || errno || !isfinite(*value) || fabs(*value) > SVG_GEOMETRY ||
        (*value && fabs(*value) < 1e-8)) return false;
    *cursor = p;
    return true;
}

static bool scalar(struct validator *v, const char *value, bool units, double *out)
{
    const char *p = value;
    while (g_ascii_isspace(*p)) p++;
    if (!number(v, &p, out)) return false;
    if (units && *p && !g_ascii_isspace(*p)) {
        const char *start = p;
        while (*p && !g_ascii_isspace(*p)) p++;
        char unit[8]; size_t length = (size_t)(p - start);
        if (length >= sizeof unit) return false;
        memcpy(unit, start, length); unit[length] = 0;
        if (!one_of(unit, "% px pt pc mm cm in")) return false;
    }
    while (g_ascii_isspace(*p)) p++;
    return !*p;
}

static bool list(struct validator *v, const char *value, double *values, size_t min, size_t max, size_t *count)
{
    *count = 0;
    const char *p = value;
    while (*p) {
        separators(&p);
        if (!*p) break;
        double x;
        if (*count == max || !number(v, &p, &x)) return false;
        if (values) values[*count] = x;
        (*count)++;
    }
    return *count >= min;
}

static bool identifier(const char *value)
{
    size_t length = strlen(value);
    if (!length || length > 63) return false;
    for (const unsigned char *p = (const unsigned char *)value; *p; p++)
        if (!g_ascii_isalnum(*p) && *p != '_' && *p != '-' && *p != '.') return false;
    return true;
}

static bool path_data(struct validator *v, const char *value)
{
    const char *p = value;
    char command = 0;
    bool first = true, needs_args = false;
    double x = 0, y = 0, start_x = 0, start_y = 0;
    while (*p) {
        separators(&p);
        if (!*p) break;
        if (g_ascii_isalpha(*p)) {
            if (needs_args) return false;
            command = *p++;
            if (!strchr("MmLlHhVvCcSsQqTtAaZz", command)) return false;
            if (first && command != 'M' && command != 'm') return false;
            if (command == 'Z' || command == 'z') { x=start_x; y=start_y; command=0; continue; }
            needs_args = true;
            separators(&p);
        }
        if (!command) return false;
        char kind = g_ascii_toupper(command);
        unsigned count = kind=='H' || kind=='V' ? 1 : kind=='C' ? 6 :
                         kind=='S' || kind=='Q' ? 4 : kind=='A' ? 7 : 2;
        double args[7];
        for (unsigned i=0; i<count; i++) {
            separators(&p);
            if (kind=='A' && (i==3 || i==4)) {
                if (*p!='0' && *p!='1') return false;
                args[i]=*p++-'0';
            } else if (!number(v,&p,&args[i])) return false;
        }
        bool relative=g_ascii_islower(command);
        if (kind=='A') {
            /* Keep NanoSVG's float arc arithmetic away from extreme ratios. */
            if (args[0]<0 || args[1]<0 || (args[0] && args[0]<0.001) ||
                (args[1] && args[1]<0.001)) return false;
            x=(relative?x:0)+args[5]; y=(relative?y:0)+args[6];
        } else if (kind=='H') x=(relative?x:0)+args[0];
        else if (kind=='V') y=(relative?y:0)+args[0];
        else {
            for (unsigned i=0;i<count;i+=2)
                if (fabs((relative?x:0)+args[i])>SVG_GEOMETRY ||
                    fabs((relative?y:0)+args[i+1])>SVG_GEOMETRY) return false;
            x=(relative?x:0)+args[count-2]; y=(relative?y:0)+args[count-1];
        }
        if (fabs(x)>SVG_GEOMETRY || fabs(y)>SVG_GEOMETRY) return false;
        if (kind=='M') { start_x=x; start_y=y; command=relative?'l':'L'; }
        first=false; needs_args=false;
    }
    return !needs_args;
}

static bool transform(struct validator *v, const char *value)
{
    const char *p=value;
    unsigned operations=0;
    while (*p) {
        separators(&p);
        if (!*p) break;
        const char *start=p;
        while (g_ascii_isalpha(*p)) p++;
        size_t length=(size_t)(p-start);
        char name[16];
        if (!length || length>=sizeof name || ++operations>32) return false;
        memcpy(name,start,length); name[length]=0;
        while (g_ascii_isspace(*p)) p++;
        if (*p++!='(') return false;
        double args[6]; unsigned count=0;
        while (*p && *p!=')') {
            separators(&p);
            if (*p==')') break;
            if (count==6 || !number(v,&p,&args[count++])) return false;
        }
        if (*p++!=')') return false;
        if ((!strcmp(name,"matrix") && count!=6) ||
            (!strcmp(name,"translate") && count!=1 && count!=2) ||
            (!strcmp(name,"scale") && count!=1 && count!=2) ||
            (!strcmp(name,"rotate") && count!=1 && count!=3) ||
            (one_of(name,"skewX skewY") && (count!=1 || fabs(args[0])>=89)) ||
            !one_of(name,"matrix translate scale rotate skewX skewY")) return false;
    }
    float matrix[6]; nsvg__parseTransform(matrix,value);
    for (unsigned i=0;i<6;i++) if (!isfinite(matrix[i]) || fabs(matrix[i])>SVG_GEOMETRY) return false;
    double determinant=(double)matrix[0]*matrix[3]-(double)matrix[1]*matrix[2];
    return isfinite(determinant) && fabs(determinant)>=1e-10;
}

static bool paint(struct validator *v, const char *value, bool reference)
{
    if (!strcmp(value,"none")) return true;
    if (*value=='#') {
        size_t n=strlen(++value);
        if (n!=3 && n!=6) return false;
        for (;*value;value++) if (!g_ascii_isxdigit(*value)) return false;
        return true;
    }
    if (reference && g_str_has_prefix(value,"url(#")) {
        size_t n=strlen(value);
        if (n<7 || value[n-1]!=')') return false;
        char *id=g_strndup(value+5,n-6);
        if (!identifier(id)) { g_free(id); return false; }
        g_ptr_array_add(v->references,id); return true;
    }
    if (g_str_has_prefix(value,"rgb(")) {
        const char *p=value+4; bool percent=false;
        for (unsigned i=0;i<3;i++) {
            while (g_ascii_isspace(*p)) p++;
            double x;
            if (!number(v,&p,&x) || x<0) return false;
            bool current=*p=='%'; if (current) p++;
            if (!i) percent=current;
            if (percent!=current || x>(percent?100:255) || (!percent && floor(x)!=x)) return false;
            while (g_ascii_isspace(*p)) p++;
            if (*p++!=(i==2?')':',')) return false;
        }
        return !*p;
    }
    for (size_t i=0;i<G_N_ELEMENTS(nsvg__colors);i++)
        if (!strcmp(value,nsvg__colors[i].name)) return true;
    return false;
}

static bool presentation(struct validator *v, enum element kind, const char *name, const char *value)
{
    double x;
    if (kind==E_DEFS || kind==E_LINEAR || kind==E_RADIAL || kind==E_TITLE || kind==E_DESC) return false;
    if (kind==E_STOP && !one_of(name,"stop-color stop-opacity")) return false;
    if (one_of(name,"fill stroke")) return paint(v,value,true);
    if (!strcmp(name,"stop-color")) return strcmp(value,"none") && paint(v,value,false);
    if (one_of(name,"opacity fill-opacity stroke-opacity stop-opacity")) {
        if (!scalar(v,value,false,&x) || x<0 || x>1) return false;
        /* NanoSVG flattens groups; do not silently change group compositing. */
        return strcmp(name,"opacity") || (kind!=E_SVG && kind!=E_GROUP) || x==1;
    }
    if (!strcmp(name,"display")) return one_of(value,"none inline");
    if (!strcmp(name,"fill-rule")) return one_of(value,"nonzero evenodd");
    if (!strcmp(name,"stroke-linecap")) return one_of(value,"butt round square");
    if (!strcmp(name,"stroke-linejoin")) return one_of(value,"miter round bevel");
    if (!strcmp(name,"stroke-miterlimit")) return scalar(v,value,false,&x) && x>=1 && x<=100;
    if (!strcmp(name,"stroke-width")) return scalar(v,value,true,&x) && x>=0 && x<=4096;
    if (!strcmp(name,"stroke-dashoffset")) return scalar(v,value,true,&x);
    if (!strcmp(name,"stroke-dasharray")) {
        if (!strcmp(value,"none")) return true;
        double values[8]; size_t count;
        if (!list(v,value,values,1,8,&count)) return false;
        for (size_t i=0;i<count;i++) if (values[i]<0.01 || values[i]>4096) return false;
        return true;
    }
    if (!strcmp(name,"paint-order")) return one_of(value,"normal fill stroke") ||
        !strcmp(value,"fill stroke") || !strcmp(value,"stroke fill");
    return false;
}

static bool attribute(struct validator *v, enum element kind, const char *name, const char *value)
{
    double x; size_t count;
    if (one_of(name,"fill stroke stop-color opacity fill-opacity stroke-opacity stop-opacity display fill-rule stroke-linecap stroke-linejoin stroke-miterlimit stroke-width stroke-dashoffset stroke-dasharray paint-order"))
        return presentation(v,kind,name,value);
    if (!strcmp(name,"transform")) return (kind==E_SVG || kind==E_GROUP || (kind>=E_PATH && kind<=E_POLYGON)) && transform(v,value);
    if (!strcmp(name,"gradientTransform")) return (kind==E_LINEAR || kind==E_RADIAL) && transform(v,value);
    if (!strcmp(name,"gradientUnits")) return (kind==E_LINEAR || kind==E_RADIAL) && one_of(value,"userSpaceOnUse objectBoundingBox");
    if (!strcmp(name,"spreadMethod")) return (kind==E_LINEAR || kind==E_RADIAL) && one_of(value,"pad reflect repeat");
    if (!strcmp(name,"offset")) return kind==E_STOP && scalar(v,value,true,&x) && x>=0 && x<=(strchr(value,'%')?100:1);
    if (!strcmp(name,"d")) return kind==E_PATH && path_data(v,value);
    if (!strcmp(name,"points")) return (kind==E_POLYGON || kind==E_POLYLINE) &&
        list(v,value,NULL,4,SVG_NUMBERS,&count) && !(count%2);
    if (!strcmp(name,"viewBox")) {
        double box[4];
        return kind==E_SVG && list(v,value,box,4,4,&count) && box[2]>=0.001 && box[3]>=0.001;
    }
    if (!strcmp(name,"preserveAspectRatio")) {
        if (kind!=E_SVG) return false;
        if (!strcmp(value,"none")) return true;
        char **parts=g_strsplit(value," ",-1);
        bool ok=g_strv_length(parts)>=1 && g_strv_length(parts)<=2 &&
            one_of(parts[0],"xMinYMin xMidYMin xMaxYMin xMinYMid xMidYMid xMaxYMid xMinYMax xMidYMax xMaxYMax") &&
            (!parts[1] || one_of(parts[1],"meet slice"));
        g_strfreev(parts); return ok;
    }
    bool allowed=(one_of(name,"width height") && (kind==E_SVG || kind==E_RECT)) ||
        (one_of(name,"x y rx ry") && kind==E_RECT) ||
        (one_of(name,"cx cy r") && (kind==E_CIRCLE || kind==E_RADIAL)) ||
        (one_of(name,"cx cy rx ry") && kind==E_ELLIPSE) ||
        (one_of(name,"x1 y1 x2 y2") && (kind==E_LINE || kind==E_LINEAR));
    if (!allowed || !scalar(v,value,true,&x)) return false;
    if (one_of(name,"width height rx ry r") && x<0) return false;
    if (kind==E_SVG && one_of(name,"width height") && (x<=0 || strchr(value,'%'))) return false;
    return true;
}

static void emit_attribute(struct validator *v, const char *name, const char *value)
{
    /* Values have a restricted ASCII grammar: no XML delimiters/escapes. */
    char *trimmed=g_strdup(value);
    g_string_append_printf(v->xml," %s=\"%s\"",name,g_strstrip(trimmed));
    g_free(trimmed);
}

static bool emit_style(struct validator *v, enum element kind, const char *style)
{
    char **declarations=g_strsplit(style,";",-1);
    bool ok=true; unsigned count=0;
    for (size_t i=0;declarations[i] && ok;i++) {
        char *name=g_strstrip(declarations[i]);
        if (!*name) continue;
        /* NanoSVG has 126 attribute pairs; raw attributes add at most 32. */
        if (++count>64) { ok=false; break; }
        char *colon=strchr(name,':');
        if (!colon) { ok=false; break; }
        *colon++=0; g_strstrip(name); char *value=g_strstrip(colon);
        if (!presentation(v,kind,name,value)) { ok=false; break; }
        emit_attribute(v,name,value);
    }
    g_strfreev(declarations); return ok;
}

static void XMLCALL start_element(void *user, const char *name, const char **attrs)
{
    struct validator *v=user;
    enum element kind=E_INVALID;
    for (unsigned i=0;i<G_N_ELEMENTS(tags);i++) if (!strcmp(tags[i],name)) { kind=(enum element)i; break; }
    if (kind==E_INVALID) { reject(v,"Unsupported SVG element (text, images, scripts, filters and references are not supported)"); return; }
    if (++v->elements>SVG_ELEMENTS || v->depth==SVG_DEPTH) { reject(v,"SVG element or nesting budget exceeded"); return; }
    enum element parent=v->depth?v->stack[v->depth-1]:E_INVALID;
    if ((!v->depth && (kind!=E_SVG || v->root_seen)) || (v->depth && kind==E_SVG) ||
        (v->depth && parent!=E_SVG && parent!=E_GROUP && parent!=E_DEFS && parent!=E_LINEAR && parent!=E_RADIAL) ||
        (kind==E_STOP && parent!=E_LINEAR && parent!=E_RADIAL) ||
        ((parent==E_LINEAR || parent==E_RADIAL) && kind!=E_STOP) ||
        (parent==E_DEFS && kind!=E_LINEAR && kind!=E_RADIAL && kind!=E_TITLE && kind!=E_DESC)) {
        reject(v,"Unsupported SVG element nesting"); return;
    }
    if (kind==E_SVG) v->root_seen=true;
    if (kind>=E_PATH && kind<=E_POLYGON && ++v->shapes>SVG_SHAPES) { reject(v,"SVG shape budget exceeded"); return; }
    if (kind==E_LINEAR || kind==E_RADIAL) {
        if (++v->gradient_count>SVG_GRADIENTS) { reject(v,"SVG gradient budget exceeded"); return; }
        v->gradient_stops=0;
    }
    if (kind==E_STOP && (++v->stops>SVG_STOPS || ++v->gradient_stops>64)) { reject(v,"SVG gradient-stop budget exceeded"); return; }
    v->stack[v->depth++]=kind;
    bool metadata=kind==E_TITLE || kind==E_DESC;
    if (!metadata) {
        g_string_append_printf(v->xml,"<%s",name);
        if (kind==E_SVG) emit_attribute(v,"preserveAspectRatio","xMidYMid meet");
    }
    const char *style=NULL, *id=NULL;
    unsigned count=0;
    for (unsigned i=0;attrs[i];i+=2) {
        const char *key=attrs[i], *value=attrs[i+1];
        if (++count>32 || strlen(value)>(one_of(key,"d points")?262144u:8192u)) { reject(v,"SVG attribute budget exceeded"); return; }
        if (!strcmp(key,"xmlns") || !strcmp(key,"xmlns:xlink")) {
            const char *uri=!strcmp(key,"xmlns")?"http://www.w3.org/2000/svg":"http://www.w3.org/1999/xlink";
            if (kind!=E_SVG || strcmp(value,uri)) { reject(v,"Unsupported SVG namespace"); return; }
            continue;
        }
        if (one_of(key,"version role aria-label aria-labelledby aria-hidden")) continue;
        if (!strcmp(key,"id")) {
            if (!identifier(value) || g_hash_table_contains(v->ids,value)) { reject(v,"Invalid or duplicate SVG identifier"); return; }
            id=value; g_hash_table_add(v->ids,g_strdup(value));
            if (kind==E_LINEAR || kind==E_RADIAL) g_hash_table_add(v->gradients,g_strdup(value));
        } else if (!strcmp(key,"style")) { style=value; continue; }
        else if (metadata || !attribute(v,kind,key,value)) { reject(v,"Unsupported or invalid SVG attribute/value"); return; }
        if (!metadata) emit_attribute(v,key,value);
    }
    if ((kind==E_LINEAR || kind==E_RADIAL) && !id) { reject(v,"SVG gradient requires a local identifier"); return; }
    if (style && !emit_style(v,kind,style)) { reject(v,"Unsupported SVG style property/value"); return; }
    if (!metadata) g_string_append_c(v->xml,'>');
}

static void XMLCALL end_element(void *user, const char *name)
{
    struct validator *v=user;
    if (!v->depth) return;
    enum element kind=v->stack[--v->depth];
    if ((kind==E_LINEAR || kind==E_RADIAL) && !v->gradient_stops) { reject(v,"SVG gradient has no supported color stops"); return; }
    if (kind!=E_TITLE && kind!=E_DESC) g_string_append_printf(v->xml,"</%s>",name);
}

static void XMLCALL content(void *user, const char *text, int length)
{
    struct validator *v=user;
    if (v->depth && (v->stack[v->depth-1]==E_TITLE || v->stack[v->depth-1]==E_DESC)) return;
    for (int i=0;i<length;i++) if (!g_ascii_isspace(text[i])) { reject(v,"SVG text rendering is not supported"); return; }
}

static void XMLCALL doctype(void *user, const char *name, const char *system_id, const char *public_id, int internal)
{
    (void)name; (void)system_id; (void)public_id; (void)internal;
    reject(user,"SVG DTDs and entity declarations are not supported");
}

static void XMLCALL instruction(void *user, const char *target, const char *data)
{
    (void)target; (void)data; reject(user,"SVG processing instructions are not supported");
}

static int XMLCALL external_entity(XML_Parser parser, const char *context, const char *base, const char *system_id, const char *public_id)
{
    (void)context; (void)base; (void)system_id; (void)public_id;
    reject(XML_GetUserData(parser),"External SVG entities are not supported"); return XML_STATUS_ERROR;
}

static void XMLCALL declaration(void *user, const char *version, const char *encoding, int standalone)
{
    (void)standalone;
    if (strcmp(version,"1.0") || (encoding && g_ascii_strcasecmp(encoding,"UTF-8")))
        reject(user,"SVG requires XML 1.0 encoded as UTF-8");
}

static char *validated_xml(const void *data, size_t length, char *error, size_t error_size)
{
    if (!data || !length || length>CB_IMAGE_MAX_BYTES || memchr(data,0,length) ||
        !g_utf8_validate(data,(gssize)length,NULL)) {
        if (error && error_size) g_strlcpy(error,"Invalid or oversized UTF-8 SVG data",error_size);
        return NULL;
    }
    struct validator v={.parser=XML_ParserCreate("UTF-8"),.xml=g_string_new(NULL),
        .ids=g_hash_table_new_full(g_str_hash,g_str_equal,g_free,NULL),
        .gradients=g_hash_table_new_full(g_str_hash,g_str_equal,g_free,NULL),
        .references=g_ptr_array_new_with_free_func(g_free)};
    bool ok=v.parser!=NULL;
    if (ok) {
        XML_SetUserData(v.parser,&v);
        XML_SetElementHandler(v.parser,start_element,end_element);
        XML_SetCharacterDataHandler(v.parser,content);
        XML_SetStartDoctypeDeclHandler(v.parser,doctype);
        XML_SetProcessingInstructionHandler(v.parser,instruction);
        XML_SetExternalEntityRefHandler(v.parser,external_entity);
        XML_SetXmlDeclHandler(v.parser,declaration);
        XML_SetParamEntityParsing(v.parser,XML_PARAM_ENTITY_PARSING_NEVER);
        ok=XML_Parse(v.parser,data,(int)length,XML_TRUE)==XML_STATUS_OK && v.root_seen && !v.depth;
        for (unsigned i=0;ok && i<v.references->len;i++)
            if (!g_hash_table_contains(v.gradients,g_ptr_array_index(v.references,i))) {
                g_strlcpy(v.error,"SVG paint references an undefined gradient",sizeof v.error); ok=false;
            }
    }
    if (!ok && error && error_size) g_strlcpy(error,v.error[0]?v.error:"Malformed or truncated SVG XML",error_size);
    if (v.parser) XML_ParserFree(v.parser);
    g_hash_table_destroy(v.ids); g_hash_table_destroy(v.gradients); g_ptr_array_free(v.references,TRUE);
    char *result=g_string_free(v.xml,!ok);
    return ok?result:NULL;
}

static bool finite_array(const float *values, size_t count)
{
    for (size_t i=0;i<count;i++) if (!isfinite(values[i]) || fabs(values[i])>SVG_GEOMETRY) return false;
    return true;
}

static bool valid_paint(const NSVGpaint *paint)
{
    if (paint->type==NSVG_PAINT_NONE || paint->type==NSVG_PAINT_COLOR) return true;
    if (paint->type!=NSVG_PAINT_LINEAR_GRADIENT && paint->type!=NSVG_PAINT_RADIAL_GRADIENT) return false;
    const NSVGgradient *g=paint->gradient;
    if (!g || g->nstops<1 || g->nstops>64 || !finite_array(g->xform,6)) return false;
    double determinant=(double)g->xform[0]*g->xform[3]-(double)g->xform[1]*g->xform[2];
    if (!isfinite(determinant) || fabs(determinant)<1e-12) return false;
    for (int i=0;i<g->nstops;i++) if (!isfinite(g->stops[i].offset) || g->stops[i].offset<0 || g->stops[i].offset>1) return false;
    return true;
}

static bool valid_image(const NSVGimage *image)
{
    if (!image || !isfinite(image->width) || !isfinite(image->height) || image->width<=0 || image->height<=0 ||
        image->width>CB_IMAGE_MAX_DIMENSION || image->height>CB_IMAGE_MAX_DIMENSION ||
        ceil(image->width)*ceil(image->height)>CB_IMAGE_MAX_PIXELS) return false;
    unsigned shapes=0, points=0, paths=0; double work=0;
    for (const NSVGshape *s=image->shapes;s;s=s->next) {
        if (++shapes>SVG_SHAPES || !finite_array(s->bounds,4) || !isfinite(s->opacity) || s->opacity<0 || s->opacity>1 ||
            !isfinite(s->strokeWidth) || s->strokeWidth<0 || s->strokeWidth>4096 ||
            !isfinite(s->miterLimit) || s->miterLimit<1 || s->miterLimit>100 ||
            !isfinite(s->strokeDashOffset) || fabs(s->strokeDashOffset)>SVG_GEOMETRY ||
            s->strokeDashCount<0 || s->strokeDashCount>8 || !finite_array(s->strokeDashArray,(size_t)s->strokeDashCount) ||
            !valid_paint(&s->fill) || !valid_paint(&s->stroke)) return false;
        if ((s->fillGradient[0] && s->fill.type==NSVG_PAINT_NONE) ||
            (s->strokeGradient[0] && s->stroke.type==NSVG_PAINT_NONE)) return false;
        for (const NSVGpath *p=s->paths;p;p=p->next) {
            if (++paths>SVG_POINTS || p->npts<1 || p->npts>(int)SVG_POINTS || (p->npts-1)%3 ||
                (points+=(unsigned)p->npts)>SVG_POINTS || !finite_array(p->pts,(size_t)p->npts*2)) return false;
        }
        if (!(s->flags&NSVG_FLAGS_VISIBLE)) continue;
        double margin=s->stroke.type==NSVG_PAINT_NONE?1:s->strokeWidth*s->miterLimit+1;
        double width=fmax(0,fmin(image->width,s->bounds[2]+margin)-fmax(0,s->bounds[0]-margin));
        double height=fmax(0,fmin(image->height,s->bounds[3]+margin)-fmax(0,s->bounds[1]-margin));
        work+=width*height*(s->opacity<1?2:1);
        if (work>SVG_PAINT_PIXELS) return false;
        if (s->strokeDashCount) {
            double sum=0, perimeter=0;
            for (int i=0;i<s->strokeDashCount;i++) { if (s->strokeDashArray[i]<0.01) return false; sum+=s->strokeDashArray[i]; }
            for (const NSVGpath *p=s->paths;p;p=p->next) for (int i=1;i<p->npts;i++)
                perimeter+=hypot(p->pts[i*2]-p->pts[(i-1)*2],p->pts[i*2+1]-p->pts[(i-1)*2+1]);
            if (perimeter/sum*s->strokeDashCount>SVG_POINTS) return false;
        }
    }
    return true;
}

static void color(cairo_t *cr, unsigned rgba)
{
    cairo_set_source_rgba(cr,(rgba&255)/255.0,((rgba>>8)&255)/255.0,((rgba>>16)&255)/255.0,((rgba>>24)&255)/255.0);
}

static void source(cairo_t *cr, const NSVGpaint *paint)
{
    if (paint->type==NSVG_PAINT_COLOR) { color(cr,paint->color); return; }
    const NSVGgradient *g=paint->gradient;
    cairo_pattern_t *pattern=paint->type==NSVG_PAINT_LINEAR_GRADIENT?
        cairo_pattern_create_linear(0,0,0,1):cairo_pattern_create_radial(0,0,0,0,0,1);
    for (int i=0;i<g->nstops;i++) {
        unsigned rgba=g->stops[i].color;
        cairo_pattern_add_color_stop_rgba(pattern,g->stops[i].offset,(rgba&255)/255.0,
            ((rgba>>8)&255)/255.0,((rgba>>16)&255)/255.0,((rgba>>24)&255)/255.0);
    }
    cairo_matrix_t matrix; cairo_matrix_init(&matrix,g->xform[0],g->xform[1],g->xform[2],g->xform[3],g->xform[4],g->xform[5]);
    cairo_pattern_set_matrix(pattern,&matrix);
    cairo_pattern_set_extend(pattern,g->spread==NSVG_SPREAD_REPEAT?CAIRO_EXTEND_REPEAT:
        g->spread==NSVG_SPREAD_REFLECT?CAIRO_EXTEND_REFLECT:CAIRO_EXTEND_PAD);
    cairo_set_source(cr,pattern); cairo_pattern_destroy(pattern);
}

static void draw_shape(cairo_t *cr, const NSVGshape *shape)
{
    cairo_new_path(cr);
    for (const NSVGpath *p=shape->paths;p;p=p->next) {
        cairo_move_to(cr,p->pts[0],p->pts[1]);
        for (int i=1;i+2<p->npts;i+=3)
            cairo_curve_to(cr,p->pts[i*2],p->pts[i*2+1],p->pts[i*2+2],p->pts[i*2+3],p->pts[i*2+4],p->pts[i*2+5]);
        if (p->closed) cairo_close_path(cr);
    }
    cairo_set_fill_rule(cr,shape->fillRule==NSVG_FILLRULE_EVENODD?CAIRO_FILL_RULE_EVEN_ODD:CAIRO_FILL_RULE_WINDING);
    cairo_set_line_width(cr,shape->strokeWidth);
    cairo_set_line_join(cr,shape->strokeLineJoin==NSVG_JOIN_ROUND?CAIRO_LINE_JOIN_ROUND:
        shape->strokeLineJoin==NSVG_JOIN_BEVEL?CAIRO_LINE_JOIN_BEVEL:CAIRO_LINE_JOIN_MITER);
    cairo_set_line_cap(cr,shape->strokeLineCap==NSVG_CAP_ROUND?CAIRO_LINE_CAP_ROUND:
        shape->strokeLineCap==NSVG_CAP_SQUARE?CAIRO_LINE_CAP_SQUARE:CAIRO_LINE_CAP_BUTT);
    cairo_set_miter_limit(cr,shape->miterLimit);
    double dashes[8]; for (int i=0;i<shape->strokeDashCount;i++) dashes[i]=shape->strokeDashArray[i];
    cairo_set_dash(cr,dashes,shape->strokeDashCount,shape->strokeDashOffset);
    if (shape->opacity<1) cairo_push_group(cr);
    for (unsigned i=0;i<3;i++) {
        unsigned operation=(shape->paintOrder>>(i*2))&3;
        if (operation==NSVG_PAINT_FILL && shape->fill.type!=NSVG_PAINT_NONE) { source(cr,&shape->fill); cairo_fill_preserve(cr); }
        if (operation==NSVG_PAINT_STROKE && shape->stroke.type!=NSVG_PAINT_NONE && shape->strokeWidth>0) { source(cr,&shape->stroke); cairo_stroke_preserve(cr); }
    }
    if (shape->opacity<1) { cairo_pop_group_to_source(cr); cairo_paint_with_alpha(cr,shape->opacity); }
}

cairo_surface_t *cb_svg_decode(const void *data, size_t length, char *error, size_t error_size)
{
    if (error && error_size) error[0]=0;
    char *xml=validated_xml(data,length,error,error_size);
    if (!xml) return NULL;
    NSVGimage *image=nsvgParse(xml,"px",96); g_free(xml);
    if (!valid_image(image)) {
        nsvgDelete(image);
        if (error && error_size) g_strlcpy(error,"SVG dimensions, geometry or rendering complexity exceed the supported budget",error_size);
        return NULL;
    }
    cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,(int)ceil(image->width),(int)ceil(image->height));
    cairo_t *cr=cairo_create(surface);
    for (const NSVGshape *s=image->shapes;s && cairo_status(cr)==CAIRO_STATUS_SUCCESS;s=s->next)
        if (s->flags&NSVG_FLAGS_VISIBLE) draw_shape(cr,s);
    bool ok=cairo_status(cr)==CAIRO_STATUS_SUCCESS && cairo_surface_status(surface)==CAIRO_STATUS_SUCCESS;
    cairo_destroy(cr); nsvgDelete(image);
    if (!ok) {
        cairo_surface_destroy(surface);
        if (error && error_size) g_strlcpy(error,"SVG rasterization failed",error_size);
        return NULL;
    }
    return surface;
}
