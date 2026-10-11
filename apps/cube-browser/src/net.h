#ifndef CUBE_BROWSER_NET_H
#define CUBE_BROWSER_NET_H
#include <stddef.h>
#include "policy.h"
#include "http.h"
#ifndef CB_CA_BUNDLE
#define CB_CA_BUNDLE "/etc/ssl/certs/ca-certificates.crt"
#endif

#define CB_MAX_BODY (2u * 1024u * 1024u)
struct cb_response {
    char *body;
    size_t length;
    char url[4096], content_type[128], error[256];
    char policy[CB_POLICY_LIMIT];
    int status;
    struct cb_http_metadata headers;
};

/* HTTP status errors still return 0: their bodies may be rendered. Network,
 * protocol, certificate and size errors return -1 and populate error. */
int cb_fetch(const char *url, struct cb_response *response);
int cb_fetch_request_progress(const struct cb_http_request *request,struct cb_response *response,int socket);
int cb_fetch_request(const struct cb_http_request *request,struct cb_response *response);
/* Fetch a brokered subresource. Initiator must be a valid HTTP(S) document URL;
 * HTTPS documents require HTTPS throughout the redirect chain. Only a final
 * 2xx status is successful. MIME policy is applied by the network worker. */
int cb_fetch_resource(const char *url, const char *initiator, struct cb_response *response);
int cb_fetch_restricted(const char *url,const char *initiator,const char *policy,unsigned kind,struct cb_response *r);
void cb_response_free(struct cb_response *response);
/* Resolve an HTTP(S) reference, preserving explicit fragments and refusing
 * credentials, controls and other schemes. HTTP redirect fragment inheritance
 * is applied separately by the fetch loop, per RFC 9110 section10.2.2. */
int cb_url_resolve(const char *base, const char *reference, char out[4096]);
#endif
