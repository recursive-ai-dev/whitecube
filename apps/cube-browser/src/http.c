#include "http.h"
#include <string.h>
static int bounded(const char *s,size_t n,int multiline){
    if(!memchr(s,0,n))return 0;
    for(;*s;s++)if((unsigned char)*s<32&&!(multiline&&(*s=='\n'||*s=='\t')))return 0;
    return 1;
}
#define FIELD(o,k,m) bounded((o)->k,sizeof (o)->k,m)
int cb_http_request_valid(const struct cb_http_request *r){
    struct cb_http_url url;
    return r->version==CB_HTTP_VERSION&&r->kind<=5&&r->post<=1&&r->download<=1&&(!r->download||!r->post)&&r->length<=CB_POST_LIMIT&&
        (!r->length||r->post)&&FIELD(r,url,0)&&!cb_http_parse(r->url,&url)&&
        FIELD(r,initiator,0)&&(!*r->initiator||!cb_http_parse(r->initiator,&url))&&
        FIELD(r,policy,1)&&FIELD(r,cookie,0)&&FIELD(r,etag,0)&&FIELD(r,modified,0)&&
        r->body[r->length]==0;
}
int cb_http_metadata_valid(const struct cb_http_metadata *m){
    if(m->version!=CB_HTTP_VERSION||m->status<200||m->status>599||m->cookie_count>CB_COOKIE_LINES||
        !FIELD(m,policy,1)||!FIELD(m,location,0)||!FIELD(m,mime,0)||!FIELD(m,etag,0)||
        !FIELD(m,modified,0)||!FIELD(m,cache_control,0)||!FIELD(m,vary,0)||
        !FIELD(m,allow_origin,0)||!FIELD(m,allow_credentials,0)||!FIELD(m,disposition,0))return 0;
    for(unsigned i=0;i<m->cookie_count;i++)if(!bounded(m->cookies[i],sizeof m->cookies[i],0))return 0;
    return 1;
}
