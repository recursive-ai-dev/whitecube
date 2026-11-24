#ifndef WHITECUBE_BUS_H
#define WHITECUBE_BUS_H

#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#define WC_VERSION        "0.2.0"
#define WC_MAX_CONNS      256
#define WC_MAX_FDS_MSG    16
#define WC_MAX_FDS_CONN   64
#define WC_MAX_MATCH      256
#define WC_MAX_MATCH_LEN  1024
#define WC_MAX_RULE_ARGS  16
#define WC_MAX_NAMES      1024
#define WC_MAX_FIELDS     32
#define WC_OUT_CAP        (1024u * 1024u)
#define WC_MAX_MESSAGE    (1024u * 1024u)
#define WC_AUTH_LINE_MAX  512
#define WC_AUTH_MAX_FAILS 8

/* ---------------- growable byte buffer ---------------- */
typedef struct { uint8_t *p; size_t len, cap; bool err; } Buf;

bool buf_reserve(Buf *b, size_t extra);
bool buf_put(Buf *b, const void *p, size_t n);
bool buf_pad(Buf *b, size_t a);
void buf_free(Buf *b);

/* ---------------- parsed message ---------------- */
typedef struct { size_t start, end; uint8_t code; } FieldSpan;

typedef struct {
    bool     be;
    uint8_t  type, flags;
    uint32_t body_len, serial, fields_len;
    size_t   total, body_off;
    const char *path, *iface, *member, *error_name, *dest, *sender, *sig;
    uint32_t reply_serial, unix_fds;
    bool     has_reply_serial, has_unix_fds;
    FieldSpan f[WC_MAX_FIELDS];
    size_t   nf;
} Msg;

typedef enum { WIRE_OK = 0, WIRE_NEED_MORE, WIRE_BAD } WireResult;

WireResult wire_parse(const uint8_t *p, size_t len, Msg *m, size_t *need);
bool wire_rebuild(const uint8_t *src, const Msg *m, const char *sender, Buf *out);
bool wire_arg_s(const uint8_t *raw, const Msg *m, size_t *off, const char **s);
bool wire_arg_u(const uint8_t *raw, const Msg *m, size_t *off, uint32_t *v);
bool wire_arg_nth_string(const uint8_t *raw, const Msg *m, unsigned idx, const char **s, char *type_out);
bool wire_iter_as(const uint8_t *raw, const Msg *m, size_t *off, size_t *end_out);

/* ---------------- message construction (always little endian) ---------------- */
typedef struct { Buf fields, body; uint8_t type, flags; uint32_t serial; } Build;

void     bld_init(Build *b, uint8_t type, uint8_t flags, uint32_t serial);
void     bld_field_s(Build *b, uint8_t code, const char *v);
void     bld_field_u(Build *b, uint8_t code, uint32_t v);
void     bld_field_g(Build *b, uint8_t code, const char *v);
void     bld_s(Buf *body, const char *v);
void     bld_u(Buf *body, uint32_t v);
void     bld_as(Buf *body, char *const *v, size_t n);
void     bld_creds(Buf *body, uint32_t uid, uint32_t pid);
uint8_t *bld_finish(Build *b, size_t *len);   /* frees internals; caller frees result */

/* ---------------- validators ---------------- */
bool wc_valid_utf8(const char *s, size_t n);
bool wc_valid_path_n(const char *s, size_t n);
bool wc_valid_signature(const char *s, size_t n);
bool wc_valid_unique(const char *s);
bool wc_valid_wellknown(const char *s);
bool wc_valid_interface(const char *s);
bool wc_valid_member(const char *s);

/* ---------------- bus ---------------- */
typedef struct Bus Bus;

typedef struct {
    const char *socket_path;
    bool audit, allow_insecure_dir;
    int  seccomp;              /* 0 = off, 1 = errno, 2 = kill */
    char **allow; size_t n_allow;
} Options;

Bus  *bus_new(Options *opt, int listen_fd);
int   bus_loop(Bus *b, volatile sig_atomic_t *stop);
void  bus_destroy(Bus *b);

/* ---------------- hardening ---------------- */
int wc_seccomp_apply(int mode);

#endif /* WHITECUBE_BUS_H */
