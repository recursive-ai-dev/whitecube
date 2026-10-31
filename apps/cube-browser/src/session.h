#ifndef CB_SESSION_H
#define CB_SESSION_H
#include <stdbool.h>
#include <stddef.h>
#include <time.h>
#include "http.h"
struct cb_session;
struct cb_config;
struct cb_session *cb_session_open(bool private_mode);
/* Applies the current window's preferences without deleting saved site data. */
void cb_session_configure(struct cb_session *s,const struct cb_config *config);
void cb_session_close(struct cb_session *s);
void cb_session_clear(struct cb_session *s);
/* Cookie evaluation always uses trusted request/top-level URLs, never renderer headers. */
int cb_cookie_set(struct cb_session *s,const char *url,const char *line,time_t now);
void cb_cookie_header(struct cb_session *s,const char *url,const char *site,bool top,bool post,time_t now,char out[CB_COOKIE_HEADER]);
int cb_same_site(const char *a,const char *b);
int cb_public_suffix(const char *host);
/* Local/session storage: 128 entries, 256 KiB per origin; 64 origins globally. */
char *cb_site_get(struct cb_session *s,const char *url,bool local,const char *key);
int cb_site_set(struct cb_session *s,const char *url,bool local,const char *key,const char *value);
int cb_site_clear(struct cb_session *s,const char *url,bool local);
char *cb_site_snapshot(struct cb_session *s,const char *url,bool local);
/* Memory cache: conditional GET only; no cookies, Vary or private/no-store responses. */
void cb_cache_prepare(struct cb_session *s,struct cb_http_request *r);
int cb_cache_finish(struct cb_session *s,const struct cb_http_request *r,struct cb_http_metadata *m,int *fd);
int cb_site_scripts(struct cb_session *s,const char *url,int fallback);
int cb_site_scripts_set(struct cb_session *s,const char *url,int enabled);
#endif
