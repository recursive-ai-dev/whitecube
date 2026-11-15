/* SPDX-License-Identifier: MIT
 *
 * dbus_wire.h - dependency-free D-Bus wire codec (marshalling + unmarshalling).
 *
 * Pure C11, no libdbus / no sd-bus / no glib.  Everything is bounds checked:
 * a hostile peer must not be able to make this code read or write out of
 * bounds, allocate unbounded memory, or recurse without limit.
 *
 * Reference: https://dbus.freedesktop.org/doc/dbus-specification.html
 */
#ifndef WC_DBUS_WIRE_H
#define WC_DBUS_WIRE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Hard limits (defensive, not protocol requirements). */
#define WC_MAX_MESSAGE   (1u * 1024u * 1024u)  /* 1 MiB per message           */
#define WC_MAX_DEPTH     32                    /* struct/array nesting        */
#define WC_MAX_SIG_LEN   255                   /* protocol maximum            */
#define WC_VALUE_MAX_ITEMS 64                  /* decoded 'as' item ceiling   */

/* ------------------------------------------------------------------ */
/* Signatures                                                          */
/* ------------------------------------------------------------------ */

/* Alignment of the type described by the first complete type in `sig`. */
size_t wc_sig_align(const char *sig);
/* Length in characters of the first complete type in `sig`, 0 if malformed. */
size_t wc_sig_type_len(const char *sig);
/* True if `sig` is a valid *complete* signature (sequence of complete types). */
bool wc_sig_valid(const char *sig);

/* ------------------------------------------------------------------ */
/* Writer (always emits little endian)                                 */
/* ------------------------------------------------------------------ */

typedef struct {
    uint8_t *buf;
    size_t   len;
    size_t   cap;
    bool     err;
} wc_writer;

typedef struct {
    size_t len_pos;   /* offset of the u32 length placeholder */
    size_t start;     /* offset of the first element          */
} wc_array_mark;

void   wc_w_init(wc_writer *w);
void   wc_w_free(wc_writer *w);
bool   wc_w_ok(const wc_writer *w);
void   wc_w_pad(wc_writer *w, size_t align);
void   wc_w_byte(wc_writer *w, uint8_t v);
void   wc_w_bool(wc_writer *w, bool v);
void   wc_w_u16(wc_writer *w, uint16_t v);
void   wc_w_i32(wc_writer *w, int32_t v);
void   wc_w_u32(wc_writer *w, uint32_t v);
void   wc_w_u64(wc_writer *w, uint64_t v);
void   wc_w_double(wc_writer *w, double v);
void   wc_w_string(wc_writer *w, const char *s);   /* 's' and 'o' */
void   wc_w_signature(wc_writer *w, const char *s);/* 'g'          */
void   wc_w_struct_begin(wc_writer *w);            /* align 8      */
void   wc_w_variant(wc_writer *w, const char *sig);/* 'g' header   */
wc_array_mark wc_w_array_begin(wc_writer *w, size_t elem_align);
void   wc_w_array_end(wc_writer *w, wc_array_mark m);

/* Convenience: a{sv} dictionary helpers. */
wc_array_mark wc_w_dict_begin(wc_writer *w);                 /* a{sv} */
void   wc_w_dict_entry(wc_writer *w, const char *key, const char *sig);
void   wc_w_dict_end(wc_writer *w, wc_array_mark m);

/* ------------------------------------------------------------------ */
/* Reader                                                              */
/* ------------------------------------------------------------------ */

typedef struct {
    const uint8_t *data;
    size_t         len;
    size_t         pos;
    bool           big_endian;
    bool           err;
    unsigned       depth;
} wc_reader;

typedef struct {
    size_t end;
    size_t elem_align;
} wc_array_iter;

void        wc_r_init(wc_reader *r, const void *data, size_t len, bool big_endian);
bool        wc_r_ok(const wc_reader *r);
size_t      wc_r_left(const wc_reader *r);
void        wc_r_pad(wc_reader *r, size_t align);
uint8_t     wc_r_byte(wc_reader *r);
bool        wc_r_bool(wc_reader *r);
uint16_t    wc_r_u16(wc_reader *r);
uint32_t    wc_r_u32(wc_reader *r);
int32_t     wc_r_i32(wc_reader *r);
uint64_t    wc_r_u64(wc_reader *r);
double      wc_r_double(wc_reader *r);
const char *wc_r_string(wc_reader *r);      /* 's' / 'o', NUL terminated */
const char *wc_r_signature(wc_reader *r);   /* 'g' */
const uint8_t *wc_r_bytes(wc_reader *r, size_t n);

wc_array_iter wc_r_array_begin(wc_reader *r, size_t elem_align);
bool        wc_r_array_more(wc_reader *r, wc_array_iter it);
void        wc_r_array_end(wc_reader *r, wc_array_iter it);
void        wc_r_struct_begin(wc_reader *r);
void        wc_r_struct_end(wc_reader *r);
const char *wc_r_variant(wc_reader *r);     /* returns the contained signature */

/* Skip one value whose complete type is the first type in `sig`. */
bool        wc_r_skip(wc_reader *r, const char *sig);

/* ------------------------------------------------------------------ */
/* Decoded variant                                                     */
/* ------------------------------------------------------------------ */

typedef struct {
    const char *sig;       /* signature of the variant contents (into msg) */
    bool        b;
    uint32_t    u32;
    int32_t     i32;
    uint64_t    u64;
    double      dbl;
    const char *str;       /* 's', 'o', 'g' */
    struct { const uint8_t *data; size_t len; } bytes;   /* 'ay' */
    struct { const char *item[WC_VALUE_MAX_ITEMS]; size_t count; } strs; /* 'as' */
} wc_value;

/* Read a variant ('v') and decode the scalar/string/array types the portal
 * needs.  Unknown types are skipped and only v->sig is filled in. */
bool wc_value_read(wc_reader *r, wc_value *v);

#endif /* WC_DBUS_WIRE_H */