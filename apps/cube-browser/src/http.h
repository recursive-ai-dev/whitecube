#ifndef CB_HTTP_H
#define CB_HTTP_H
#include <stddef.h>
#include <stdint.h>
#include "policy.h"
#define CB_HTTP_VERSION 1u
#define CB_POST_LIMIT 16384u
#define CB_COOKIE_HEADER 4096u
#define CB_COOKIE_LINES 16u
/* Fixed versioned, sealed IPC payloads; no pointers or arbitrary request headers. */
struct cb_http_request {
    uint32_t version, kind, post, length, download;
    char url[4096], initiator[4096], policy[CB_POLICY_LIMIT];
    char cookie[CB_COOKIE_HEADER], etag[256], modified[128];
    char body[CB_POST_LIMIT+1];
};
struct cb_http_metadata {
    uint32_t version, status, cookie_count;
    char policy[CB_POLICY_LIMIT], location[4096], mime[128];
    char etag[256], modified[128], cache_control[512], vary[256];
    char allow_origin[320], allow_credentials[16], disposition[512];
    char cookies[CB_COOKIE_LINES][4096];
};
int cb_http_request_valid(const struct cb_http_request *r);
int cb_http_metadata_valid(const struct cb_http_metadata *m);
#endif
