#define _GNU_SOURCE
#include "broker.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include "download.h"
#include <glib.h>
char *cb_broker_storage(struct cb_session *s,const char *url,const char *policy,bool enabled){
    char origin[320];if(!enabled||cb_origin(url,origin)||cb_policy_opaque(policy))return g_strdup("[{},{}]");
    char *local=cb_site_snapshot(s,url,true),*session=cb_site_snapshot(s,url,false);
    char *result=g_strdup_printf("[%s,%s]",local,session);g_free(local);g_free(session);return result;
}
static int failure(struct cb_http_job *j,const char *message){snprintf(j->error,sizeof j->error,"%.255s",message);return -1;}
static int mime_is(const char *value,const char *mime){size_t n=strlen(mime);if(strncasecmp(value,mime,n))return 0;value+=n;while(*value==' '||*value=='\t')value++;return !*value||*value==';';}
struct cb_http_job *cb_broker_new(const char *url,const char *site,const char *initiator,const char *policy,unsigned kind,bool top,const char *body){
    struct cb_http_job *j=calloc(1,sizeof *j);if(!j)return NULL;
    j->credentials=cb_policy_opaque(policy)?0:2;j->request.version=CB_HTTP_VERSION;j->request.kind=kind;j->top=top;
    snprintf(j->request.url,sizeof j->request.url,"%s",url);
    snprintf(j->request.initiator,sizeof j->request.initiator,"%s",initiator?initiator:"");
    snprintf(j->request.policy,sizeof j->request.policy,"%s",policy?policy:"");
    snprintf(j->site,sizeof j->site,"%s",site?site:url);
    if(body){size_t n=strlen(body);if(n>CB_POST_LIMIT){free(j);return NULL;}memcpy(j->request.body,body,n+1);j->request.length=n;j->request.post=1;}
    if(!cb_http_request_valid(&j->request)){free(j);return NULL;}return j;
}
int cb_broker_send(struct cb_http_job *j,struct cb_session *s,int socket){
    if(j->request.kind&&!cb_policy_allows(j->request.policy,j->request.initiator,j->request.url,j->request.kind,0))return failure(j,"Content Security Policy blocked this request");
    cb_cookie_header(s,j->request.url,j->cross_site?"https://cross-site.invalid/":j->site,j->top,j->request.post,time(NULL),j->request.cookie);
    if(!j->credentials||(j->credentials==1&&!cb_same_origin(j->request.url,j->request.initiator)))j->request.cookie[0]=0;
    if(!j->request.download)cb_cache_prepare(s,&j->request);
    j->have_meta=false;
    struct cb_message m={.type=CB_HTTP_REQUEST,.fd=cb_document_fd((const char *)&j->request,sizeof j->request)};
    int rc=m.fd<0?-1:cb_send(socket,&m);if(m.fd>=0)close(m.fd);if(rc)return failure(j,"Could not send the HTTP request");return 0;
}
int cb_broker_receive(struct cb_http_job *j,struct cb_session *s,struct cb_message *m){
    if(m->type==CB_HTTP_PROGRESS){if(!j->request.download||m->fd>=0||m->c||*m->text||m->a>CB_DOWNLOAD_LIMIT||m->b>CB_DOWNLOAD_LIMIT||m->a<j->received)return failure(j,"Invalid download progress");j->received=m->a;j->total=m->b;return 0;}
    if(m->type==CB_ERROR){if(m->fd>=0)return failure(j,"Invalid network error");return failure(j,m->text);}
    if(m->type==CB_HTTP_META){
        char *data=NULL;size_t size=0;int valid=!j->have_meta&&m->fd>=0&&!m->a&&!m->b&&!m->c&&!m->text[0]&&!cb_read_document(m->fd,&data,&size)&&size==sizeof j->meta;
        if(valid){memcpy(&j->meta,data,size);valid=cb_http_metadata_valid(&j->meta);}free(data);if(m->fd>=0)close(m->fd);m->fd=-1;
        if(!valid)return failure(j,"Invalid HTTP metadata from worker");
        j->have_meta=true;return 0;
    }
    if(m->type!=CB_DOCUMENT||m->fd<0||!j->have_meta||m->a!=j->meta.status||m->b||m->c||m->text[0])return failure(j,"Invalid HTTP response from worker");
    if(j->credentials&&(j->credentials!=1||cb_same_origin(j->request.url,j->request.initiator))&&(j->top||cb_same_site(j->request.url,j->site)))
        for(unsigned i=0;i<j->meta.cookie_count;i++)cb_cookie_set(s,j->request.url,j->meta.cookies[i],time(NULL));
    bool revalidated=j->meta.status==304&&!j->request.download;
    if(revalidated&&cb_cache_finish(s,&j->request,&j->meta,&m->fd))return failure(j,"Invalid cache revalidation response");
    unsigned status=j->meta.status;
    if(j->request.kind==CB_POLICY_CONNECT&&!cb_same_origin(j->request.url,j->request.initiator)){
        char origin[320];if(cb_origin_header(j->request.initiator,origin))return failure(j,"Invalid fetch origin");
        if(strcmp(j->meta.allow_origin,"*")&&strcmp(j->meta.allow_origin,origin))return failure(j,"CORS blocked this response");
    }
    if((status==301||status==302||status==303||status==307||status==308)&&*j->meta.location){
        if(j->redirects++>=5)return failure(j,"Too many redirects (limit 5)");
        char next[4096];struct cb_http_url from,to;
        if(cb_url_resolve(j->request.url,j->meta.location,next)||cb_http_parse(next,&to)||cb_http_parse(j->request.url,&from))return failure(j,"Invalid redirect URL");
        if(from.tls&&!to.tls)return failure(j,"HTTPS to HTTP redirect refused");
        if(j->request.kind==CB_POLICY_CONNECT&&!cb_same_origin(j->request.url,next))return failure(j,"Cross-origin fetch redirect refused");
        if(j->request.post&&(status==307||status==308)&&!cb_same_origin(j->request.url,next))return failure(j,"Cross-origin POST replay refused");
        if(!strchr(j->meta.location,'#')&&from.has_fragment){to.has_fragment=true;strcpy(to.fragment,from.fragment);if(cb_http_format(&to,next))return failure(j,"Redirect URL too long");}
        if(j->request.post&&(status==301||status==302||status==303)){j->request.post=0;j->request.length=0;memset(j->request.body,0,sizeof j->request.body);}
        if(!cb_same_site(j->request.url,next))j->cross_site=true;
        j->received=j->total=0;strcpy(j->request.url,next);close(m->fd);m->fd=-1;return 2;
    }
    if(!j->request.download&&!revalidated&&cb_cache_finish(s,&j->request,&j->meta,&m->fd))return failure(j,"Invalid cache revalidation response");
    status=j->meta.status;const char *mime=j->meta.mime;
    if(!j->top&&j->request.kind!=4&&(status<200||status>299))return failure(j,"Subresource returned an unsuccessful HTTP status");
    if(j->top&&!j->request.download&&*mime&&!mime_is(mime,"text/html")&&!mime_is(mime,"application/xhtml+xml")&&!mime_is(mime,"text/plain")&&!mime_is(mime,"image/png")&&!mime_is(mime,"image/jpeg")&&!mime_is(mime,"image/svg+xml"))return failure(j,"This content type cannot be displayed. Press Ctrl+S to download it.");
    if(!j->top&&j->request.kind==1&&!mime_is(mime,"text/css"))return failure(j,"Stylesheet MIME type refused");
    if(!j->top&&j->request.kind==2&&!mime_is(mime,"image/png")&&!mime_is(mime,"image/jpeg")&&!mime_is(mime,"image/svg+xml"))return failure(j,"Image MIME type refused");
    if(!j->top&&j->request.kind==3&&!mime_is(mime,"text/javascript")&&!mime_is(mime,"application/javascript"))return failure(j,"Script MIME type refused");
    m->a=status;m->b=j->top?(mime_is(mime,"image/png")?CB_DOCUMENT_PNG:
        mime_is(mime,"image/jpeg")?CB_DOCUMENT_JPEG:mime_is(mime,"image/svg+xml")?CB_DOCUMENT_SVG:
        mime_is(mime,"text/plain")?CB_DOCUMENT_TEXT:CB_DOCUMENT_HTML):j->request.kind;
    if(j->top)snprintf(m->text,sizeof m->text,"%s",j->request.url);
    else snprintf(m->text,sizeof m->text,"%s\n%s",j->request.url,mime);
    return 1;
}
