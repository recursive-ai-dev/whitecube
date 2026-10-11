#ifndef CB_POLICY_H
#define CB_POLICY_H
#include <stdbool.h>
#define CB_POLICY_LIMIT 4096
enum cb_policy_kind { CB_POLICY_STYLE=1, CB_POLICY_IMAGE, CB_POLICY_SCRIPT, CB_POLICY_CONNECT, CB_POLICY_FORM, CB_POLICY_STYLE_ATTR };
struct cb_http_url { char host[254],path[4096],fragment[4096];unsigned port;bool tls,has_fragment; };
int cb_http_format(const struct cb_http_url *u,char out[4096]);
int cb_url_resolve(const char *base,const char *reference,char out[4096]);
int cb_http_parse(const char *url,struct cb_http_url *out);
int cb_origin(const char *url,char out[320]);
/* Wire serialization omits a default port; storage keys keep explicit ports. */
int cb_origin_header(const char *url,char out[320]);
int cb_same_origin(const char *a,const char *b);
/* HTTP(S) only. Multiple comma/newline-separated policies intersect. */
int cb_policy_allows(const char *policy,const char *document,const char *target,unsigned kind,int inline_code);
int cb_policy_eval_allowed(const char *policy);
int cb_policy_opaque(const char *policy);
#endif
