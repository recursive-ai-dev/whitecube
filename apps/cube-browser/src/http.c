#include "http.h"
#include <string.h>
#include <strings.h>
int cb_javascript_mime(const char *value,size_t length){
    /* https://mimesniff.spec.whatwg.org/#javascript-mime-type */
    static const char *const types[]={"application/ecmascript","application/javascript",
        "application/x-ecmascript","application/x-javascript","text/ecmascript","text/javascript",
        "text/javascript1.0","text/javascript1.1","text/javascript1.2","text/javascript1.3",
        "text/javascript1.4","text/javascript1.5","text/jscript","text/livescript",
        "text/x-ecmascript","text/x-javascript"};
    if(!value)return 0;
    for(size_t i=0;i<sizeof types/sizeof *types;i++)if(length==strlen(types[i])&&!strncasecmp(value,types[i],length))return 1;
    return 0;
}
int cb_http_javascript_mime(const char *value){
    if(!value)return 0;
    value+=strspn(value," \t\r\n\f");size_t length=strcspn(value,";");
    while(length&&strchr(" \t\r\n\f",value[length-1]))length--;
    return cb_javascript_mime(value,length);
}
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
