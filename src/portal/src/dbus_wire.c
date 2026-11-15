/* SPDX-License-Identifier: MIT
 *
 * dbus_wire.c - D-Bus marshalling / unmarshalling, no external dependencies.
 *
 * All reads are bounds checked against the message buffer; all writes are
 * bounds checked against WC_MAX_MESSAGE.  Errors are sticky: once a reader or
 * writer sets ->err every subsequent operation is a no-op, so callers can
 * check once at the end of a compound parse.
 */
#include "dbus_wire.h"

#include <stdlib.h>
#include <string.h>

/* ================================================================== */
/* Signature helpers                                                   */
/* ================================================================== */

size_t wc_sig_align(const char *sig)
{
    if (!sig || !*sig)
        return 1;
    switch (*sig) {
    case 'y': case 'g': case 'v':                 return 1;
    case 'n': case 'q':                           return 2;
    case 'b': case 'i': case 'u': case 's':
    case 'o': case 'a': case 'h':                 return 4;
    case 'x': case 't': case 'd': case '(':
    case '{':                                     return 8;
    default:                                      return 1;
    }
}

static size_t type_len_rec(const char *s, unsigned depth)
{
    if (depth > WC_MAX_DEPTH || !*s)
        return 0;

    switch (*s) {
    case 'y': case 'b': case 'n': case 'q': case 'i': case 'u':
    case 'x': case 't': case 'd': case 's': case 'o': case 'g':
    case 'v': case 'h':
        return 1;
    case 'a':
        {
            size_t n = type_len_rec(s + 1, depth + 1);
            return n ? n + 1 : 0;
        }
    case '(':
        {
            size_t n = 1;
            if (s[1] == ')')
                return 0;                    /* empty structs are invalid */
            while (s[n] && s[n] != ')') {
                size_t t;
                if (s[n] == '{')
                    return 0;                /* dict entry only inside 'a' */
                t = type_len_rec(s + n, depth + 1);
                if (!t)
                    return 0;
                n += t;
            }
            return s[n] == ')' ? n + 1 : 0;
        }
    case '{':
        {
            size_t v;
            if (!s[1] || !strchr("ybnqiuxtdsog", s[1]))
                return 0;                    /* dict key must be basic */
            v = type_len_rec(s + 2, depth + 1);
            if (!v)
                return 0;
            return s[2 + v] == '}' ? v + 3 : 0;
        }
    default:
        return 0;
    }
}

size_t wc_sig_type_len(const char *sig)
{
    return sig ? type_len_rec(sig, 0) : 0;
}

bool wc_sig_valid(const char *sig)
{
    size_t total = 0, n;

    if (!sig)
        return false;
    while (sig[total]) {
        if (sig[total] == '{')
            return false;      /* dict entries are only valid inside arrays */
        n = wc_sig_type_len(sig + total);
        if (!n)
            return false;
        total += n;
        if (total > WC_MAX_SIG_LEN)
            return false;
    }
    return true;
}

/* ================================================================== */
/* Writer                                                              */
/* ================================================================== */

void wc_w_init(wc_writer *w)
{
    w->buf = NULL;
    w->len = 0;
    w->cap = 0;
    w->err = false;
}

void wc_w_free(wc_writer *w)
{
    free(w->buf);
    wc_w_init(w);
}

bool wc_w_ok(const wc_writer *w)
{
    return !w->err;
}

static bool w_reserve(wc_writer *w, size_t n)
{
    size_t need, cap;
    uint8_t *p;

    if (w->err)
        return false;
    if (n > WC_MAX_MESSAGE || w->len > WC_MAX_MESSAGE - n) {
        w->err = true;
        return false;
    }
    need = w->len + n;
    if (need <= w->cap)
        return true;

    cap = w->cap ? w->cap : 256;
    while (cap < need)
        cap *= 2;
    p = realloc(w->buf, cap);
    if (!p) {
        w->err = true;
        return false;
    }
    w->buf = p;
    w->cap = cap;
    return true;
}

void wc_w_pad(wc_writer *w, size_t align)
{
    size_t rem;

    if (!align || align > 8 || w->err)
        return;
    rem = w->len % align;
    if (!rem)
        return;
    rem = align - rem;
    if (!w_reserve(w, rem))
        return;
    memset(w->buf + w->len, 0, rem);
    w->len += rem;
}

void wc_w_byte(wc_writer *w, uint8_t v)
{
    if (!w_reserve(w, 1))
        return;
    w->buf[w->len++] = v;
}

void wc_w_bool(wc_writer *w, bool v)
{
    wc_w_u32(w, v ? 1u : 0u);
}

void wc_w_u16(wc_writer *w, uint16_t v)
{
    size_t i;
    wc_w_pad(w, 2);
    if (!w_reserve(w, 2))
        return;
    for (i = 0; i < 2; i++)
        w->buf[w->len++] = (uint8_t)(v >> (8 * i));
}

void wc_w_u32(wc_writer *w, uint32_t v)
{
    size_t i;
    wc_w_pad(w, 4);
    if (!w_reserve(w, 4))
        return;
    for (i = 0; i < 4; i++)
        w->buf[w->len++] = (uint8_t)(v >> (8 * i));
}

void wc_w_i32(wc_writer *w, int32_t v)
{
    wc_w_u32(w, (uint32_t)v);
}

void wc_w_u64(wc_writer *w, uint64_t v)
{
    size_t i;
    wc_w_pad(w, 8);
    if (!w_reserve(w, 8))
        return;
    for (i = 0; i < 8; i++)
        w->buf[w->len++] = (uint8_t)(v >> (8 * i));
}

void wc_w_double(wc_writer *w, double v)
{
    uint64_t bits;
    /* C11 guarantees double is IEEE-754 binary64 on every target we support;
     * memcpy avoids strict-aliasing violations. */
    if (sizeof(double) != sizeof(uint64_t)) {
        w->err = true;
        return;
    }
    memcpy(&bits, &v, sizeof bits);
    wc_w_u64(w, bits);
}

void wc_w_string(wc_writer *w, const char *s)
{
    size_t n;

    if (!s)
        s = "";
    n = strlen(s);
    if (n > 0xffffffu) {
        w->err = true;
        return;
    }
    wc_w_u32(w, (uint32_t)n);
    if (!w_reserve(w, n + 1))
        return;
    memcpy(w->buf + w->len, s, n);
    w->len += n;
    w->buf[w->len++] = 0;
}

void wc_w_signature(wc_writer *w, const char *s)
{
    size_t n;

    if (!s)
        s = "";
    n = strlen(s);
    if (n > WC_MAX_SIG_LEN) {
        w->err = true;
        return;
    }
    if (!w_reserve(w, n + 2))
        return;
    w->buf[w->len++] = (uint8_t)n;
    memcpy(w->buf + w->len, s, n);
    w->len += n;
    w->buf[w->len++] = 0;
}

void wc_w_struct_begin(wc_writer *w)
{
    wc_w_pad(w, 8);
}

void wc_w_variant(wc_writer *w, const char *sig)
{
    wc_w_signature(w, sig);
}

wc_array_mark wc_w_array_begin(wc_writer *w, size_t elem_align)
{
    wc_array_mark m;

    wc_w_pad(w, 4);
    m.len_pos = w->len;
    wc_w_u32(w, 0);
    wc_w_pad(w, elem_align);
    m.start = w->len;
    return m;
}

void wc_w_array_end(wc_writer *w, wc_array_mark m)
{
    uint32_t n;
    size_t i;

    if (w->err || m.start < m.len_pos + 4 || w->len < m.start) {
        w->err = true;
        return;
    }
    n = (uint32_t)(w->len - m.start);
    for (i = 0; i < 4; i++)
        w->buf[m.len_pos + i] = (uint8_t)(n >> (8 * i));
}

wc_array_mark wc_w_dict_begin(wc_writer *w)
{
    return wc_w_array_begin(w, 8);   /* a{sv}: dict entries align to 8 */
}

void wc_w_dict_entry(wc_writer *w, const char *key, const char *sig)
{
    wc_w_pad(w, 8);
    wc_w_string(w, key);
    wc_w_variant(w, sig);
}

void wc_w_dict_end(wc_writer *w, wc_array_mark m)
{
    wc_w_array_end(w, m);
}

/* ================================================================== */
/* Reader                                                              */
/* ================================================================== */

void wc_r_init(wc_reader *r, const void *data, size_t len, bool big_endian)
{
    r->data = (const uint8_t *)data;
    r->len = len;
    r->pos = 0;
    r->big_endian = big_endian;
    r->err = (data == NULL && len > 0);
    r->depth = 0;
}

bool wc_r_ok(const wc_reader *r)
{
    return !r->err;
}

size_t wc_r_left(const wc_reader *r)
{
    return r->err ? 0 : r->len - r->pos;
}

static const uint8_t *r_take(wc_reader *r, size_t n)
{
    const uint8_t *p;

    if (r->err || n > r->len - r->pos) {
        r->err = true;
        return NULL;
    }
    p = r->data + r->pos;
    r->pos += n;
    return p;
}

void wc_r_pad(wc_reader *r, size_t align)
{
    size_t rem;

    if (!align || align > 8 || r->err)
        return;
    rem = r->pos % align;
    if (!rem)
        return;
    if (!r_take(r, align - rem))
        r->err = true;
}

static uint64_t r_uint(wc_reader *r, size_t n)
{
    const uint8_t *p = r_take(r, n);
    uint64_t v = 0;
    size_t i;

    if (!p)
        return 0;
    if (r->big_endian) {
        for (i = 0; i < n; i++)
            v = (v << 8) | p[i];
    } else {
        for (i = n; i > 0; i--)
            v = (v << 8) | p[i - 1];
    }
    return v;
}

uint8_t wc_r_byte(wc_reader *r)
{
    const uint8_t *p = r_take(r, 1);
    return p ? *p : 0;
}

uint16_t wc_r_u16(wc_reader *r)
{
    wc_r_pad(r, 2);
    return (uint16_t)r_uint(r, 2);
}

uint32_t wc_r_u32(wc_reader *r)
{
    wc_r_pad(r, 4);
    return (uint32_t)r_uint(r, 4);
}

int32_t wc_r_i32(wc_reader *r)
{
    return (int32_t)wc_r_u32(r);
}

uint64_t wc_r_u64(wc_reader *r)
{
    wc_r_pad(r, 8);
    return r_uint(r, 8);
}

bool wc_r_bool(wc_reader *r)
{
    uint32_t v = wc_r_u32(r);
    if (v > 1)
        r->err = true;      /* D-Bus booleans must be 0 or 1 */
    return v != 0;
}

double wc_r_double(wc_reader *r)
{
    uint64_t bits = wc_r_u64(r);
    double d = 0.0;
    memcpy(&d, &bits, sizeof d);
    return d;
}

const uint8_t *wc_r_bytes(wc_reader *r, size_t n)
{
    return r_take(r, n);
}

const char *wc_r_string(wc_reader *r)
{
    uint32_t n = wc_r_u32(r);
    const uint8_t *p;

    if (r->err)
        return NULL;
    p = r_take(r, (size_t)n + 1);
    if (!p || p[n] != 0 || memchr(p, 0, n) != NULL) {
        r->err = true;
        return NULL;
    }
    return (const char *)p;
}

const char *wc_r_signature(wc_reader *r)
{
    uint8_t n = wc_r_byte(r);
    const uint8_t *p;

    if (r->err)
        return NULL;
    p = r_take(r, (size_t)n + 1);
    if (!p || p[n] != 0 || memchr(p, 0, n) != NULL) {
        r->err = true;
        return NULL;
    }
    if (!wc_sig_valid((const char *)p)) {
        r->err = true;
        return NULL;
    }
    return (const char *)p;
}

wc_array_iter wc_r_array_begin(wc_reader *r, size_t elem_align)
{
    wc_array_iter it;
    uint32_t n = wc_r_u32(r);

    wc_r_pad(r, elem_align);
    it.elem_align = elem_align;
    it.end = r->pos;
    if (!r->err) {
        if ((size_t)n > r->len - r->pos) {
            r->err = true;
        } else {
            it.end = r->pos + n;
        }
    }
    return it;
}

bool wc_r_array_more(wc_reader *r, wc_array_iter it)
{
    if (r->err)
        return false;
    if (r->pos > it.end) {
        r->err = true;
        return false;
    }
    return r->pos < it.end;
}

void wc_r_array_end(wc_reader *r, wc_array_iter it)
{
    if (r->err)
        return;
    if (r->pos != it.end) {
        r->err = true;
        return;
    }
}

void wc_r_struct_begin(wc_reader *r)
{
    if (r->depth >= WC_MAX_DEPTH) {
        r->err = true;
        return;
    }
    r->depth++;
    wc_r_pad(r, 8);
}

void wc_r_struct_end(wc_reader *r)
{
    if (r->depth)
        r->depth--;
}

const char *wc_r_variant(wc_reader *r)
{
    return wc_r_signature(r);
}

/* Skip a single complete type starting at *ps (advanced past it). */
static bool skip_one(wc_reader *r, const char **ps)
{
    const char *s = *ps;
    size_t n;
    char c;
    bool ok = true;

    if (r->err || !s || !*s) {
        r->err = true;
        return false;
    }
    c = *s;
    n = wc_sig_type_len(s);
    if (!n) {
        r->err = true;
        return false;
    }
    *ps = s + n;

    switch (c) {
    case 'y':
        (void)wc_r_byte(r);
        break;
    case 'b':
        (void)wc_r_bool(r);
        break;
    case 'n': case 'q':
        (void)wc_r_u16(r);
        break;
    case 'i': case 'h':
        (void)wc_r_i32(r);
        break;
    case 'u':
        (void)wc_r_u32(r);
        break;
    case 'x': case 't':
        (void)wc_r_u64(r);
        break;
    case 'd':
        (void)wc_r_double(r);
        break;
    case 's': case 'o':
        (void)wc_r_string(r);
        break;
    case 'g':
        (void)wc_r_signature(r);
        break;
    case 'v':
        {
            const char *vs = wc_r_variant(r);
            if (!vs || !*vs || wc_sig_type_len(vs) != strlen(vs)) {
                ok = false;
                break;
            }
            if (r->depth >= WC_MAX_DEPTH) {
                ok = false;
                break;
            }
            r->depth++;
            while (*vs && !r->err)
                if (!skip_one(r, &vs)) {
                    ok = false;
                    break;
                }
            r->depth--;
        }
        break;
    case 'a':
        {
            const char *elem = s + 1;
            size_t elem_len = wc_sig_type_len(elem);
            wc_array_iter it;
            if (!elem_len) {
                ok = false;
                break;
            }
            it = wc_r_array_begin(r, wc_sig_align(elem));
            if (r->depth >= WC_MAX_DEPTH) {
                ok = false;
                break;
            }
            r->depth++;
            while (wc_r_array_more(r, it)) {
                const char *p = elem;
                size_t before = r->pos;
                if (!skip_one(r, &p)) {
                    ok = false;
                    break;
                }
                if (r->pos == before) {   /* never loop without progress */
                    ok = false;
                    break;
                }
            }
            r->depth--;
            wc_r_array_end(r, it);
        }
        break;
    case '(':
        {
            const char *p = s + 1;
            wc_r_struct_begin(r);
            while (*p && *p != ')' && !r->err)
                if (!skip_one(r, &p)) {
                    ok = false;
                    break;
                }
            wc_r_struct_end(r);
            if (*p != ')')
                ok = false;
        }
        break;
    case '{':
        {
            const char *p = s + 1;
            wc_r_struct_begin(r);
            if (!skip_one(r, &p) || !skip_one(r, &p))
                ok = false;
            wc_r_struct_end(r);
            if (*p != '}')
                ok = false;
        }
        break;
    default:
        ok = false;
        break;
    }
    if (!ok)
        r->err = true;
    return !r->err;
}

bool wc_r_skip(wc_reader *r, const char *sig)
{
    const char *p = sig;

    if (!sig || !wc_sig_valid(sig))
        return false;
    while (*p && !r->err) {
        if (!skip_one(r, &p))
            return false;
    }
    return !r->err;
}

/* ================================================================== */
/* Variant decoding                                                    */
/* ================================================================== */

bool wc_value_read(wc_reader *r, wc_value *v)
{
    const char *sig;

    memset(v, 0, sizeof *v);
    sig = wc_r_variant(r);
    if (!sig)
        return false;
    v->sig = sig;

    if (!strcmp(sig, "b")) {
        v->b = wc_r_bool(r);
    } else if (!strcmp(sig, "y")) {
        v->u32 = wc_r_byte(r);
    } else if (!strcmp(sig, "u")) {
        v->u32 = wc_r_u32(r);
    } else if (!strcmp(sig, "i")) {
        v->i32 = wc_r_i32(r);
    } else if (!strcmp(sig, "t")) {
        v->u64 = wc_r_u64(r);
    } else if (!strcmp(sig, "d")) {
        v->dbl = wc_r_double(r);
    } else if (!strcmp(sig, "s") || !strcmp(sig, "o") || !strcmp(sig, "g")) {
        v->str = (sig[0] == 'g') ? wc_r_signature(r) : wc_r_string(r);
        if (!v->str)
            return false;
    } else if (!strcmp(sig, "ay")) {
        wc_array_iter it = wc_r_array_begin(r, 1);
        size_t n = it.end - r->pos;
        v->bytes.data = wc_r_bytes(r, n);
        v->bytes.len = n;
        wc_r_array_end(r, it);
    } else if (!strcmp(sig, "as")) {
        wc_array_iter it = wc_r_array_begin(r, 4);
        while (wc_r_array_more(r, it)) {
            const char *s = wc_r_string(r);
            if (!s)
                break;
            if (v->strs.count < WC_VALUE_MAX_ITEMS)
                v->strs.item[v->strs.count++] = s;
        }
        wc_r_array_end(r, it);
    } else {
        if (!wc_r_skip(r, sig))
            return false;
    }
    return !r->err;
}