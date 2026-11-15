/* whitecube-bus: D-Bus wire framing, validation and re-marshalling. */
#include "whitecube_bus.h"
#include <stdlib.h>
#include <string.h>

#define AL(v, a) (((v) + ((size_t)(a) - 1)) & ~((size_t)(a) - 1))

/* ---------------- buffers ---------------- */
bool buf_reserve(Buf *b, size_t extra)
{
    if (b->err) return false;
    if (extra <= b->cap - b->len) return true;
    size_t need = b->len + extra;
    if (need < b->len) { b->err = true; return false; }
    size_t ncap = b->cap ? b->cap : 128;
    while (ncap < need) {
        if (ncap > SIZE_MAX / 2) { b->err = true; return false; }
        ncap *= 2;
    }
    uint8_t *np = realloc(b->p, ncap);
    if (!np) { b->err = true; return false; }
    b->p = np; b->cap = ncap;
    return true;
}

bool buf_put(Buf *b, const void *p, size_t n)
{
    if (!buf_reserve(b, n)) return false;
    if (n) memcpy(b->p + b->len, p, n);
    b->len += n;
    return true;
}

bool buf_pad(Buf *b, size_t a)
{
    size_t t = AL(b->len, a);
    if (!buf_reserve(b, t - b->len)) return false;
    memset(b->p + b->len, 0, t - b->len);
    b->len = t;
    return true;
}

void buf_free(Buf *b) { free(b->p); b->p = NULL; b->len = b->cap = 0; b->err = false; }

/* ---------------- endian ---------------- */
static uint32_t rd32(const uint8_t *p, bool be)
{
    if (be) return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
                   ((uint32_t)p[2] << 8)  | (uint32_t)p[3];
    return ((uint32_t)p[3] << 24) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[1] << 8)  | (uint32_t)p[0];
}

static void wr32(uint8_t *p, uint32_t v, bool be)
{
    if (be) { p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
              p[2] = (uint8_t)(v >> 8);  p[3] = (uint8_t)v; }
    else    { p[3] = (uint8_t)(v >> 24); p[2] = (uint8_t)(v >> 16);
              p[1] = (uint8_t)(v >> 8);  p[0] = (uint8_t)v; }
}

static size_t align_of(char t)
{
    switch (t) {
    case 'y': case 'g': case 'v': return 1;
    case 'n': case 'q': return 2;
    case 'b': case 'i': case 'u': case 'h': case 's': case 'o': case 'a': return 4;
    case 'x': case 't': case 'd': case '(': case '{': return 8;
    default: return 0;
    }
}

/* ---------------- validators ---------------- */
bool wc_valid_utf8(const char *s, size_t n)
{
    const unsigned char *p = (const unsigned char *)s;
    for (size_t i = 0; i < n; ) {
        unsigned c = p[i];
        if (c == 0) return false;                 /* no embedded NUL */
        if (c < 0x80) { i++; continue; }
        size_t k; unsigned cp, lo;
        if ((c & 0xE0) == 0xC0) { k = 2; cp = c & 0x1F; lo = 0x80; }
        else if ((c & 0xF0) == 0xE0) { k = 3; cp = c & 0x0F; lo = 0x800; }
        else if ((c & 0xF8) == 0xF0) { k = 4; cp = c & 0x07; lo = 0x10000; }
        else return false;
        if (i + k > n) return false;
        for (size_t j = 1; j < k; j++) {
            if ((p[i + j] & 0xC0) != 0x80) return false;
            cp = (cp << 6) | (p[i + j] & 0x3F);
        }
        if (cp < lo || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return false;
        i += k;
    }
    return true;
}

bool wc_valid_path_n(const char *s, size_t n)
{
    if (!n || s[0] != '/') return false;
    if (n == 1) return true;
    if (s[n - 1] == '/') return false;
    size_t elem = 0;
    for (size_t i = 1; i < n; i++) {
        char c = s[i];
        if (c == '/') { if (!elem) return false; elem = 0; continue; }
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_')) return false;
        elem++;
    }
    return elem > 0;
}

static size_t type_span(const char *s, size_t n)
{
    if (!n) return 0;
    switch (s[0]) {
    case 'a': { size_t r = type_span(s + 1, n - 1); return r ? 1 + r : 0; }
    case '(': case '{': {
        size_t i = 1; int depth = 1;
        while (i < n) {
            if (s[i] == '(' || s[i] == '{') depth++;
            else if (s[i] == ')' || s[i] == '}') { if (--depth == 0) return i + 1; }
            i++;
        }
        return 0;
    }
    default:
        return strchr("ybnqiuxtdsogvh", s[0]) && s[0] ? 1 : 0;
    }
}

static size_t sig_one(const char *s, size_t n, int depth, bool in_array)
{
    if (!n || depth > 32) return 0;
    char c = s[0];
    if (c == 'a') {
        size_t k = sig_one(s + 1, n - 1, depth + 1, true);
        return k ? 1 + k : 0;
    }
    if (c == '(' || c == '{') {
        if (c == '{' && !in_array) return 0;
        char close = (c == '(') ? ')' : '}';
        size_t i = 1, cnt = 0;
        while (i < n && s[i] != close) {
            size_t k = sig_one(s + i, n - i, depth + 1, false);
            if (!k) return 0;
            if (c == '{' && cnt == 0 && !strchr("ybnqiuxtdsogh", s[i])) return 0;
            i += k; cnt++;
        }
        if (i >= n || s[i] != close) return 0;
        if (c == '{' && cnt != 2) return 0;
        if (c == '(' && cnt == 0) return 0;
        return i + 1;
    }
    return (strchr("ybnqiuxtdsogvh", c) && c) ? 1 : 0;
}

bool wc_valid_signature(const char *s, size_t n)
{
    if (n > 255) return false;
    size_t i = 0;
    while (i < n) {
        size_t k = sig_one(s + i, n - i, 0, false);
        if (!k) return false;
        i += k;
    }
    return true;
}

static bool name_chars(const char *s, bool hyphen, bool digits_first)
{
    for (const char *p = s; *p; p++) {
        char c = *p;
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_') continue;
        if (c >= '0' && c <= '9') { if (p == s && !digits_first) return false; continue; }
        if (c == '-' && hyphen) { if (p == s) return false; continue; }
        return false;
    }
    return true;
}

bool wc_valid_unique(const char *s)
{
    if (!s || s[0] != ':') return false;
    size_t n = strlen(s);
    if (n < 3 || n > 255) return false;
    int dots = 0;
    for (size_t i = 1; i < n; i++) {
        char c = s[i];
        if (c == '.') { dots++; continue; }
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
    }
    return dots >= 1;
}

bool wc_valid_wellknown(const char *s)
{
    if (!s || s[0] == ':') return false;
    size_t n = strlen(s);
    if (n < 3 || n > 255 || s[0] == '.' || s[n - 1] == '.') return false;
    int dots = 0;
    const char *elem = s;
    for (size_t i = 0; i <= n; i++) {
        if (s[i] == '.' || s[i] == 0) {
            if (i == 0) return false;
            size_t len = (size_t)(s + i - elem);
            if (!len) return false;
            char tmp[256];
            memcpy(tmp, elem, len); tmp[len] = 0;
            if (!name_chars(tmp, true, false)) return false;
            if (s[i] == '.') dots++;
            elem = s + i + 1;
        }
    }
    return dots >= 1;
}

bool wc_valid_interface(const char *s)
{
    if (!s) return false;
    size_t n = strlen(s);
    if (n < 3 || n > 255 || s[0] == '.' || s[n - 1] == '.') return false;
    int dots = 0;
    const char *elem = s;
    for (size_t i = 0; i <= n; i++) {
        if (s[i] == '.' || s[i] == 0) {
            size_t len = (size_t)(s + i - elem);
            if (!len) return false;
            char tmp[256];
            memcpy(tmp, elem, len); tmp[len] = 0;
            if (!name_chars(tmp, false, false)) return false;
            if (s[i] == '.') dots++;
            elem = s + i + 1;
        }
    }
    return dots >= 1;
}

bool wc_valid_member(const char *s)
{
    if (!s) return false;
    size_t n = strlen(s);
    if (!n || n > 255) return false;
    return name_chars(s, false, false);
}

/* ---------------- signature-driven skip / validation ---------------- */
static bool skip_val(const uint8_t *p, size_t end, bool be,
                     const char *sig, size_t sl, size_t *off, int depth)
{
    if (depth > 32 || !sl) return false;
    size_t a = align_of(sig[0]);
    if (!a) return false;
    size_t o = AL(*off, a);
    if (o > end) return false;

    switch (sig[0]) {
    case 'y':
        if (o + 1 > end) return false;
        o += 1;
        break;
    case 'n': case 'q':
        if (o + 2 > end) return false;
        o += 2;
        break;
    case 'b': case 'i': case 'u': case 'h':
        if (o + 4 > end) return false;
        if (sig[0] == 'b' && rd32(p + o, be) > 1) return false;
        o += 4;
        break;
    case 'x': case 't': case 'd':
        if (o + 8 > end) return false;
        o += 8;
        break;
    case 's': case 'o': {
        if (o + 4 > end) return false;
        uint32_t n = rd32(p + o, be); o += 4;
        if (n > end - o || o + n + 1 > end) return false;
        if (p[o + n] != 0) return false;
        if (sig[0] == 's' && !wc_valid_utf8((const char *)p + o, n)) return false;
        if (sig[0] == 'o' && !wc_valid_path_n((const char *)p + o, n)) return false;
        o += n + 1; break;
    }
    case 'g': {
        if (o + 1 > end) return false;
        uint8_t n = p[o]; o += 1;
        if (o + n + 1 > end) return false;
        if (p[o + n] != 0) return false;
        if (!wc_valid_signature((const char *)p + o, n)) return false;
        o += n + 1; break;
    }
    case 'v': {
        if (o + 1 > end) return false;
        uint8_t n = p[o]; o += 1;
        if (o + n + 1 > end) return false;
        if (p[o + n] != 0) return false;
        const char *vs = (const char *)p + o;
        if (!wc_valid_signature(vs, n)) return false;
        o += n + 1;
        if (!skip_val(p, end, be, vs, n, &o, depth + 1)) return false;
        break;
    }
    case 'a': {
        if (sl < 2) return false;
        size_t el = type_span(sig + 1, sl - 1);
        size_t ea = align_of(sig[1]);
        if (!el || !ea) return false;
        if (o + 4 > end) return false;
        uint32_t n = rd32(p + o, be); o += 4;
        size_t s0 = AL(o, ea);
        if (n == 0) {
            if (s0 <= end) o = s0;
            break;
        }
        if (s0 > end || n > end - s0) return false;
        size_t ae = s0 + n;
        o = s0;
        while (o < ae) {
            size_t before = o;
            if (!skip_val(p, ae, be, sig + 1, el, &o, depth + 1)) return false;
            if (o <= before) return false;
        }
        if (o != ae) return false;
        break;
    }
    case '(': case '{': {
        size_t i = 1;
        char close = (sig[0] == '(') ? ')' : '}';
        while (i < sl && sig[i] != close) {
            size_t k = type_span(sig + i, sl - i);
            if (!k) return false;
            if (!skip_val(p, end, be, sig + i, k, &o, depth + 1)) return false;
            i += k;
        }
        if (i >= sl || sig[i] != close) return false;
        break;
    }
    default: return false;
    }
    *off = o;
    return true;
}

/* ---------------- header parsing ---------------- */
WireResult wire_parse(const uint8_t *p, size_t len, Msg *m, size_t *need)
{
    memset(m, 0, sizeof *m);
    *need = 16;
    if (len < 16) return WIRE_NEED_MORE;
    if (p[0] != 'l' && p[0] != 'B') return WIRE_BAD;
    if (p[3] != 1) return WIRE_BAD;
    if (p[1] < 1 || p[1] > 4) return WIRE_BAD;
    if (p[2] & (uint8_t)~0x07) return WIRE_BAD;

    bool be = (p[0] == 'B');
    uint32_t bl = rd32(p + 4, be), serial = rd32(p + 8, be), fl = rd32(p + 12, be);
    if (!serial) return WIRE_BAD;
    if (bl > WC_MAX_MESSAGE || fl > WC_MAX_MESSAGE) return WIRE_BAD;

    size_t body_off = AL(16 + (size_t)fl, 8);
    size_t total = body_off + bl;
    if (total > WC_MAX_MESSAGE) return WIRE_BAD;
    if (len < total) { *need = total; return WIRE_NEED_MORE; }

    m->be = be; m->type = p[1]; m->flags = p[2];
    m->body_len = bl; m->serial = serial; m->fields_len = fl;
    m->total = total; m->body_off = body_off;

    size_t off = 16, end = 16 + (size_t)fl;
    while (off < end) {
        off = AL(off, 8);
        if (off >= end) break;
        if (m->nf >= WC_MAX_FIELDS) return WIRE_BAD;
        size_t start = off;
        uint8_t code = p[off++];
        if (!code) return WIRE_BAD;
        if (off >= end) return WIRE_BAD;
        uint8_t sl = p[off++];
        if (!sl || off + sl + 1 > end) return WIRE_BAD;
        const char *sig = (const char *)p + off;
        off += sl;
        if (p[off] != 0) return WIRE_BAD;
        off++;

        size_t a = align_of(sig[0]);
        if (!a) return WIRE_BAD;
        size_t vo = AL(off, a);
        if (vo > end) return WIRE_BAD;

        size_t after = vo;
        if (!skip_val(p, end, be, sig, sl, &after, 0)) return WIRE_BAD;

        if (sl == 1) {
            switch (code) {
            case 1: if (sig[0] != 'o' || m->path) return WIRE_BAD;
                    m->path = (const char *)p + vo + 4; break;
            case 2: if (sig[0] != 's' || m->iface) return WIRE_BAD;
                    m->iface = (const char *)p + vo + 4; break;
            case 3: if (sig[0] != 's' || m->member) return WIRE_BAD;
                    m->member = (const char *)p + vo + 4; break;
            case 4: if (sig[0] != 's' || m->error_name) return WIRE_BAD;
                    m->error_name = (const char *)p + vo + 4; break;
            case 5: if (sig[0] != 'u' || m->has_reply_serial) return WIRE_BAD;
                    m->reply_serial = rd32(p + vo, be); m->has_reply_serial = true; break;
            case 6: if (sig[0] != 's' || m->dest) return WIRE_BAD;
                    m->dest = (const char *)p + vo + 4; break;
            case 7: if (sig[0] != 's' || m->sender) return WIRE_BAD;
                    m->sender = (const char *)p + vo + 4; break;
            case 8: if (sig[0] != 'g' || m->sig) return WIRE_BAD;
                    m->sig = (const char *)p + vo + 1; break;
            case 9: if (sig[0] != 'u' || m->has_unix_fds) return WIRE_BAD;
                    m->unix_fds = rd32(p + vo, be); m->has_unix_fds = true; break;
            default: break;   /* unknown field: validated, skipped, preserved */
            }
        }
        m->f[m->nf].start = start;
        m->f[m->nf].end = after;
        m->f[m->nf].code = code;
        m->nf++;
        off = after;
    }

    /* required fields */
    switch (m->type) {
    case 1: if (!m->path || !m->member) return WIRE_BAD; break;
    case 2: if (!m->has_reply_serial) return WIRE_BAD; break;
    case 3: if (!m->error_name || !m->has_reply_serial) return WIRE_BAD; break;
    case 4: if (!m->path || !m->iface || !m->member) return WIRE_BAD; break;
    default: return WIRE_BAD;
    }
    if (m->path && !wc_valid_path_n(m->path, strlen(m->path))) return WIRE_BAD;
    if (m->iface && !wc_valid_interface(m->iface)) return WIRE_BAD;
    if (m->member && !wc_valid_member(m->member)) return WIRE_BAD;
    if (m->error_name && !wc_valid_interface(m->error_name)) return WIRE_BAD;
    if (m->dest && !wc_valid_unique(m->dest) && !wc_valid_wellknown(m->dest) &&
        strcmp(m->dest, "org.freedesktop.DBus") != 0) return WIRE_BAD;
    if (m->has_unix_fds && m->unix_fds > WC_MAX_FDS_MSG) return WIRE_BAD;

    /* body validation against SIGNATURE */
    size_t bend = body_off + bl;
    if (m->sig && *m->sig) {
        size_t sl = strlen(m->sig), i = 0, o = body_off;
        if (!wc_valid_signature(m->sig, sl)) return WIRE_BAD;
        while (i < sl) {
            size_t k = type_span(m->sig + i, sl - i);
            if (!k) return WIRE_BAD;
            if (!skip_val(p, bend, be, m->sig + i, k, &o, 0)) return WIRE_BAD;
            i += k;
        }
        if (o > bend) return WIRE_BAD;
        for (; o < bend; o++) if (p[o]) return WIRE_BAD;   /* trailing pad must be NUL */
    } else if (bl) {
        return WIRE_BAD;
    }
    return WIRE_OK;
}

/* ---------------- SENDER injection ----------------
 * Every original field span is copied verbatim (unknown fields preserved),
 * field 7 is dropped and re-appended with the broker-assigned unique name.
 * Each span is re-aligned to 8 in the destination, so padding stays legal.
 */
bool wire_rebuild(const uint8_t *src, const Msg *m, const char *sender, Buf *out)
{
    Buf f; memset(&f, 0, sizeof f);
    size_t slen = strlen(sender);
    const uint8_t z = 0;

    for (size_t i = 0; i < m->nf; i++) {
        if (m->f[i].code == 7) continue;
        buf_pad(&f, 8);
        buf_put(&f, src + m->f[i].start, m->f[i].end - m->f[i].start);
    }
    buf_pad(&f, 8);
    uint8_t head[4] = { 7, 1, 's', 0 };
    buf_put(&f, head, 4);
    buf_pad(&f, 4);
    uint8_t lb[4]; wr32(lb, (uint32_t)slen, m->be);
    buf_put(&f, lb, 4);
    buf_put(&f, sender, slen);
    buf_put(&f, &z, 1);

    bool ok = !f.err;
    if (ok) {
        size_t body_off = AL(16 + f.len, 8);
        ok = buf_reserve(out, body_off + m->body_len);
        if (ok) {
            uint8_t *h = out->p + out->len;
            memset(h, 0, body_off);
            h[0] = m->be ? 'B' : 'l'; h[1] = m->type; h[2] = m->flags; h[3] = 1;
            wr32(h + 4, m->body_len, m->be);
            wr32(h + 8, m->serial, m->be);
            wr32(h + 12, (uint32_t)f.len, m->be);
            if (f.len) memcpy(h + 16, f.p, f.len);
            if (m->body_len) memcpy(h + body_off, src + m->body_off, m->body_len);
            out->len += body_off + m->body_len;
        }
    }
    buf_free(&f);
    return ok;
}

/* ---------------- body argument readers ---------------- */
bool wire_arg_s(const uint8_t *raw, const Msg *m, size_t *off, const char **s)
{
    size_t base = m->body_off, end = base + m->body_len;
    size_t o = base + AL(*off, 4);
    if (o + 4 > end) return false;
    uint32_t n = rd32(raw + o, m->be); o += 4;
    if (o + n + 1 > end || raw[o + n] != 0) return false;
    *s = (const char *)raw + o;
    *off = (o + n + 1) - base;
    return true;
}

bool wire_arg_u(const uint8_t *raw, const Msg *m, size_t *off, uint32_t *v)
{
    size_t base = m->body_off, end = base + m->body_len;
    size_t o = base + AL(*off, 4);
    if (o + 4 > end) return false;
    *v = rd32(raw + o, m->be);
    *off = (o + 4) - base;
    return true;
}

bool wire_arg_nth_string(const uint8_t *raw, const Msg *m, unsigned idx, const char **s, char *type_out)
{
    if (!m->sig || !*m->sig) return false;
    size_t sl = strlen(m->sig), pos = 0, o = m->body_off, end = m->body_off + m->body_len;
    unsigned cur = 0;

    while (pos < sl) {
        size_t span = type_span(m->sig + pos, sl - pos);
        if (!span) return false;
        if (cur == idx) {
            if (span != 1 || (m->sig[pos] != 's' && m->sig[pos] != 'o')) return false;
            size_t rel = o - m->body_off;
            if (!wire_arg_s(raw, m, &rel, s)) return false;
            if (type_out) *type_out = m->sig[pos];
            return true;
        }
        size_t a = align_of(m->sig[pos]);
        if (!a) return false;
        o = AL(o, a);
        if (!skip_val(raw, end, m->be, m->sig + pos, span, &o, 0)) return false;
        pos += span;
        cur++;
    }
    return false;
}

bool wire_iter_as(const uint8_t *raw, const Msg *m, size_t *off, size_t *end_out)
{
    uint32_t blen = 0;
    if (!wire_arg_u(raw, m, off, &blen)) return false;
    if (blen > m->body_len || *off + blen > m->body_len) return false;
    *end_out = *off + blen;
    return true;
}

/* ---------------- builders ---------------- */
void bld_init(Build *b, uint8_t type, uint8_t flags, uint32_t serial)
{
    memset(b, 0, sizeof *b);
    b->type = type; b->flags = flags; b->serial = serial;
}

static void field_head(Build *b, uint8_t code, const char *sig)
{
    buf_pad(&b->fields, 8);
    uint8_t pre[2] = { code, (uint8_t)strlen(sig) };
    buf_put(&b->fields, pre, 2);
    buf_put(&b->fields, sig, strlen(sig));
    uint8_t z = 0;
    buf_put(&b->fields, &z, 1);
}

void bld_u(Buf *b, uint32_t v)
{
    buf_pad(b, 4);
    uint8_t t[4]; wr32(t, v, false);
    buf_put(b, t, 4);
}

void bld_s(Buf *b, const char *v)
{
    size_t n = v ? strlen(v) : 0;
    buf_pad(b, 4);
    uint8_t t[4]; wr32(t, (uint32_t)n, false);
    buf_put(b, t, 4);
    if (n) buf_put(b, v, n);
    uint8_t z = 0; buf_put(b, &z, 1);
}

void bld_field_s(Build *b, uint8_t code, const char *v) { field_head(b, code, "s"); bld_s(&b->fields, v); }
void bld_field_u(Build *b, uint8_t code, uint32_t v)     { field_head(b, code, "u"); bld_u(&b->fields, v); }

void bld_field_g(Build *b, uint8_t code, const char *v)
{
    field_head(b, code, "g");
    size_t n = strlen(v);
    uint8_t l = (uint8_t)n, z = 0;
    buf_put(&b->fields, &l, 1);
    buf_put(&b->fields, v, n);
    buf_put(&b->fields, &z, 1);
}

void bld_as(Buf *body, char *const *v, size_t n)
{
    buf_pad(body, 4);
    size_t lenpos = body->len;
    uint8_t z4[4] = { 0, 0, 0, 0 };
    buf_put(body, z4, 4);
    size_t start = body->len;              /* element alignment 4: already aligned */
    for (size_t i = 0; i < n; i++) bld_s(body, v[i]);
    if (!body->err) wr32(body->p + lenpos, (uint32_t)(body->len - start), false);
}

/* a{sv} with UnixUserID and ProcessID */
void bld_creds(Buf *body, uint32_t uid, uint32_t pid)
{
    buf_pad(body, 4);
    size_t lenpos = body->len;
    uint8_t z4[4] = { 0, 0, 0, 0 };
    buf_put(body, z4, 4);
    buf_pad(body, 8);                      /* first dict entry: 8-aligned, not counted */
    size_t start = body->len;
    const char *keys[2] = { "UnixUserID", "ProcessID" };
    uint32_t vals[2] = { uid, pid };
    for (int i = 0; i < 2; i++) {
        buf_pad(body, 8);
        bld_s(body, keys[i]);
        uint8_t vs[3] = { 1, 'u', 0 };
        buf_put(body, vs, 3);
        bld_u(body, vals[i]);
    }
    if (!body->err) wr32(body->p + lenpos, (uint32_t)(body->len - start), false);
}

uint8_t *bld_finish(Build *b, size_t *len)
{
    uint8_t *p = NULL;
    if (!b->fields.err && !b->body.err) {
        size_t body_off = AL(16 + b->fields.len, 8);
        size_t total = body_off + b->body.len;
        p = malloc(total);
        if (p) {
            memset(p, 0, body_off);
            p[0] = 'l'; p[1] = b->type; p[2] = b->flags; p[3] = 1;
            wr32(p + 4, (uint32_t)b->body.len, false);
            wr32(p + 8, b->serial, false);
            wr32(p + 12, (uint32_t)b->fields.len, false);
            if (b->fields.len) memcpy(p + 16, b->fields.p, b->fields.len);
            if (b->body.len) memcpy(p + body_off, b->body.p, b->body.len);
            *len = total;
        }
    }
    buf_free(&b->fields);
    buf_free(&b->body);
    return p;
}
