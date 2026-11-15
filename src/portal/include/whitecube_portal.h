/* SPDX-License-Identifier: MIT
 *
 * whitecube_portal.h - shared types for the whitecube desktop portal.
 *
 * whitecube-portal implements org.freedesktop.portal.Desktop directly on the
 * whitecube-bus session bus.  No xdg-desktop-portal, no impl.portal backends,
 * no X11, no GLib/libdbus/sd-bus.
 */
#ifndef WHITECUBE_PORTAL_H
#define WHITECUBE_PORTAL_H

#include <limits.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#include "dbus_wire.h"

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define WC_PORTAL_NAME       "org.freedesktop.portal.Desktop"
#define WC_PORTAL_PATH       "/org/freedesktop/portal/desktop"
#define WC_REQUEST_PREFIX    WC_PORTAL_PATH "/request/"

#define WC_MAX_REQUESTS_HARD 64      /* compile time ceiling                */
#define WC_DEFAULT_REQUESTS  8       /* concurrent picker cap (spec)        */
#define WC_MAX_URIS          256     /* selections honoured per response    */
#define WC_MAX_FILTERS       32
#define WC_FILTER_MAX        256
#define WC_TITLE_MAX         256
#define WC_TOKEN_MAX         64
#define WC_SENDER_MAX        64
#define WC_URI_MAX           (3 * PATH_MAX + 16)
#define WC_CHOOSER_MAX_BYTES (256 * 1024)
#define WC_DEFAULT_TIMEOUT_S 300
#define WC_KILL_GRACE_MS     2000

/* ------------------------------------------------------------------ */
/* Logging / small utilities (util.c)                                  */
/* ------------------------------------------------------------------ */

typedef enum {
    WC_LOG_ERROR = 0,
    WC_LOG_WARN  = 1,
    WC_LOG_INFO  = 2,
    WC_LOG_DEBUG = 3
} wc_loglevel;

extern wc_loglevel wc_log_level;

void wc_log(wc_loglevel lvl, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

#define wc_err(...)   wc_log(WC_LOG_ERROR, __VA_ARGS__)
#define wc_warn(...)  wc_log(WC_LOG_WARN,  __VA_ARGS__)
#define wc_info(...)  wc_log(WC_LOG_INFO,  __VA_ARGS__)
#define wc_dbg(...)   wc_log(WC_LOG_DEBUG, __VA_ARGS__)

int64_t wc_now_ms(void);
/* snprintf that reports truncation instead of silently clipping. */
bool wc_fmt(char *dst, size_t cap, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));

/* Copy `src` into dst, dropping control characters and clipping to maxlen. */
void wc_sanitize_text(char *dst, size_t cap, const char *src, size_t maxlen);
/* True when the string has no C0/C1 control characters and is not empty. */
bool wc_text_is_safe(const char *s);
/* Object-path element from a client supplied handle_token. */
void wc_sanitize_token(const char *token, uint32_t serial, char *out, size_t cap);
/* ":1.42" -> "1_42" (exactly what GTK/libportal/xdg-desktop-portal derive). */
bool wc_sender_element(const char *sender, char *out, size_t cap);

/* RFC 8089 file URI with full percent encoding. */
bool wc_uri_encode(const char *path, char *out, size_t cap);

bool wc_runtime_dir(char *out, size_t cap);
int  wc_mkstemp_runtime(const char *runtimedir, char *path_out, size_t cap);
void wc_close_inherited_fds(void);
/* fork + execve, no shell, no fd leaks, detached session. */
pid_t wc_spawn(char *const argv[]);
bool wc_find_exec(const char *name, char *out, size_t cap);
bool wc_is_absolute_path(const char *s);
bool wc_read_file_line(const char *path, char *out, size_t cap);

/* ------------------------------------------------------------------ */
/* Bus client (dbus_client.c)                                          */
/* ------------------------------------------------------------------ */

#define WC_MSG_METHOD_CALL   1
#define WC_MSG_METHOD_RETURN 2
#define WC_MSG_ERROR         3
#define WC_MSG_SIGNAL        4

typedef struct {
    uint8_t  type;
    uint8_t  flags;
    uint32_t serial;
    uint32_t reply_serial;
    const char *path;
    const char *interface;
    const char *member;
    const char *destination;
    const char *sender;
    const char *signature;
    const char *error_name;
    const uint8_t *body;
    size_t body_len;
    bool big_endian;
    uint8_t *raw;      /* owned */
    size_t raw_len;
} wc_msg;

typedef struct {
    uint8_t     type;
    uint8_t     flags;
    const char *path;
    const char *interface;
    const char *member;
    const char *destination;
    const char *signature;
    const char *error_name;
    uint32_t    reply_serial;
} wc_hdr;

typedef struct {
    int      fd;
    char     unique_name[WC_SENDER_MAX];
    uint32_t next_serial;
    uint8_t *in;
    size_t   in_len, in_cap;
    uint8_t *out;
    size_t   out_len, out_cap, out_sent;
    bool     dead;
} wc_bus;

void wc_msg_clear(wc_msg *m);

bool wc_bus_connect(wc_bus *b, const char *address);
void wc_bus_close(wc_bus *b);
bool wc_bus_hello(wc_bus *b);
bool wc_bus_request_name(wc_bus *b, const char *name, bool replace);
bool wc_bus_add_match(wc_bus *b, const char *rule);

uint32_t wc_bus_send(wc_bus *b, const wc_hdr *h, const uint8_t *body, size_t len);
bool wc_bus_flush(wc_bus *b);                    /* non blocking */
int  wc_bus_recv(wc_bus *b);                     /* 1 data, 0 none, -1 gone */
int  wc_bus_next(wc_bus *b, wc_msg *out);        /* 1 msg, 0 none, -1 error */

void wc_bus_reply(wc_bus *b, const wc_msg *call, const char *sig,
                  const wc_writer *body);
void wc_bus_reply_empty(wc_bus *b, const wc_msg *call);
void wc_bus_error(wc_bus *b, const wc_msg *call, const char *name,
                  const char *fmt, ...) __attribute__((format(printf, 4, 5)));
void wc_bus_signal(wc_bus *b, const char *path, const char *iface,
                   const char *member, const char *sig, const wc_writer *body,
                   const char *destination);

/* ------------------------------------------------------------------ */
/* Portal state                                                        */
/* ------------------------------------------------------------------ */

typedef enum { WC_REQ_OPEN = 0, WC_REQ_SAVE, WC_REQ_URI } wc_req_kind;

typedef struct {
    bool     used;
    char     path[2 * WC_TOKEN_MAX + WC_SENDER_MAX + 64];
    char     sender[WC_SENDER_MAX];
    char     token[WC_TOKEN_MAX];
    char     chooser[PATH_MAX];
    pid_t    pid;
    wc_req_kind kind;
    bool     multiple;
    bool     directory;
    bool     responded;
    bool     closing;
    int64_t  deadline_ms;
} wc_request;

typedef struct {
    const char *picker_cmd;
    const char *browser_cmd;
    const char *bus_address;
    uint32_t    color_scheme;    /* 0 none, 1 dark, 2 light */
    double      accent[3];
    uint32_t    contrast;
    bool        color_scheme_set;
    bool        accent_set;
    bool        contrast_set;
    unsigned    max_requests;
    unsigned    timeout_s;
    bool        replace;
    bool        cancel_on_disconnect;
} wc_config;

typedef struct {
    wc_bus      bus;
    wc_config   cfg;
    wc_request  reqs[WC_MAX_REQUESTS_HARD];
    unsigned    active;
    char        runtime_dir[PATH_MAX];
    char        picker[PATH_MAX];
    char        browser[PATH_MAX];
    char        home[PATH_MAX];
    char        machine_id[64];
    bool        running;
} wc_portal;

/* request registry (request.c) */
wc_request *wc_req_new(wc_portal *p, const wc_msg *call, const char *token,
                       wc_req_kind kind);
wc_request *wc_req_by_path(wc_portal *p, const char *path);
wc_request *wc_req_by_pid(wc_portal *p, pid_t pid);
void wc_req_free(wc_portal *p, wc_request *r);
void wc_req_respond(wc_portal *p, wc_request *r, uint32_t code,
                    const char *const *uris, size_t n_uris);
void wc_req_cancel(wc_portal *p, wc_request *r, uint32_t code);
void wc_req_reap(wc_portal *p);
void wc_req_expire(wc_portal *p, int64_t now);
void wc_req_sender_vanished(wc_portal *p, const char *name);
int64_t wc_req_next_deadline(wc_portal *p);
void wc_req_shutdown(wc_portal *p);
void wc_request_handle_close(wc_portal *p, const wc_msg *m);

/* interface handlers */
void wc_settings_handle(wc_portal *p, const wc_msg *m);
void wc_settings_sync(wc_portal *p, bool emit_signals);
void wc_filechooser_handle(wc_portal *p, const wc_msg *m);
void wc_uri_handle(wc_portal *p, const wc_msg *m);
void wc_properties_handle(wc_portal *p, const wc_msg *m);
void wc_introspect_handle(wc_portal *p, const wc_msg *m);
void wc_filechooser_child_done(wc_portal *p, wc_request *r, int status);

/* Version table shared by Properties and Introspect (introspect.c). */
typedef struct {
    const char *iface;
    uint32_t    version;
} wc_iface_version;
extern const wc_iface_version wc_iface_versions[];
bool wc_iface_version_get(const char *iface, uint32_t *out);
bool wc_iface_denied(const char *iface);

/* Well known portal error names. */
#define WC_ERR_NOT_ALLOWED  "org.freedesktop.portal.Error.NotAllowed"
#define WC_ERR_NOT_FOUND    "org.freedesktop.portal.Error.NotFound"
#define WC_ERR_INVALID_ARG  "org.freedesktop.portal.Error.InvalidArgument"
#define WC_ERR_FAILED       "org.freedesktop.portal.Error.Failed"
#define WC_ERR_CANCELLED    "org.freedesktop.portal.Error.Cancelled"
#define WC_ERR_UNKNOWN_METH "org.freedesktop.DBus.Error.UnknownMethod"
#define WC_ERR_UNKNOWN_IFACE "org.freedesktop.DBus.Error.UnknownInterface"
#define WC_ERR_UNKNOWN_OBJ  "org.freedesktop.DBus.Error.UnknownObject"
#define WC_ERR_LIMITS       "org.freedesktop.DBus.Error.LimitsExceeded"
#define WC_ERR_READONLY     "org.freedesktop.DBus.Error.PropertyReadOnly"
#define WC_ERR_INVALID_ARGS "org.freedesktop.DBus.Error.InvalidArgs"

#endif /* WHITECUBE_PORTAL_H */