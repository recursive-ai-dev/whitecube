#define _GNU_SOURCE
#include "net.h"
#include "ipc.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/prctl.h>
#include <unistd.h>
static int mime_is(const char *value,const char *mime){
    size_t length=strlen(mime);
    if(strncasecmp(value,mime,length))return 0;
    const char *suffix=value+length;
    while(*suffix==' '||*suffix=='\t')suffix++;
    return !*suffix||*suffix==';';
}
static int fetch_kind(unsigned kind,const char *url,const char *initiator,const char *policy,struct cb_response *r){
    if(kind>2){memset(r,0,sizeof *r);snprintf(r->error,sizeof r->error,"Unknown resource kind (expected 0, 1 or 2)");return -1;}
    int result=kind?(policy?cb_fetch_restricted(url,initiator,policy,kind,r):cb_fetch_resource(url,initiator,r)):cb_fetch(url,r);
    if(result)return result;
    if(kind==1&&!mime_is(r->content_type,"text/css")){
        snprintf(r->error,sizeof r->error,"Stylesheet rejected: expected text/css, received %.127s",r->content_type[0]?r->content_type:"no Content-Type");return -1;
    }
    if(kind==2&&!mime_is(r->content_type,"image/png")&&!mime_is(r->content_type,"image/jpeg")&&!mime_is(r->content_type,"image/svg+xml")){
        snprintf(r->error,sizeof r->error,"Image rejected: expected image/png, image/jpeg or image/svg+xml, received %.127s",r->content_type[0]?r->content_type:"no Content-Type");return -1;
    }
    return 0;
}
static int command_fetch(unsigned kind,const char *url,const char *initiator){
    struct cb_response r={0};int failed=fetch_kind(kind,url,initiator,NULL,&r);
    if(failed)fprintf(stderr,"%s\n",r.error);
    else {
        failed=r.length&&fwrite(r.body,1,r.length,stdout)!=r.length;
        fprintf(stderr,"HTTP %d %s %s\n",r.status,r.content_type,r.url);
    }
    cb_response_free(&r);return failed?1:0;
}
int main(int argc,char **argv){
    unsetenv("DISPLAY");cb_worker_limits();prctl(PR_SET_NAME,"cube-netd",0,0,0);
    if(cb_network_restrict(CB_CA_BUNDLE)){perror("Network sandbox unavailable");return 1;}
    if(argc==4&&!strcmp(argv[1],"--resolve")){
        char url[4096];if(cb_url_resolve(argv[2],argv[3],url))return 1;puts(url);return 0;
    }
    if(argc==3&&!strcmp(argv[1],"--fetch")){
        return command_fetch(0,argv[2],NULL);
    }
    if(argc==5&&!strcmp(argv[1],"--resource")){
        unsigned kind;
        if(!strcmp(argv[2],"1"))kind=1;
        else if(!strcmp(argv[2],"2"))kind=2;
        else {fprintf(stderr,"Resource kind must be 1 (CSS) or 2 (image)\n");return 2;}
        return command_fetch(kind,argv[4],argv[3]);
    }
    if(argc!=3||strcmp(argv[1],"--ipc"))return 2;
    int socket=cb_ipc_fd(argv[2]);if(socket<0)return 2;
    struct cb_message req;
    if(cb_recv(socket,&req)!=1){close(socket);return 1;}
    if(req.type==CB_HTTP_REQUEST){
        alarm(25);char *payload=NULL;size_t length=0;
        struct cb_response r={0};int ok=-1,download=0;
        if(req.fd>=0&&!req.a&&!req.b&&!req.c&&!req.text[0]&&!cb_read_document(req.fd,&payload,&length)&&length==sizeof(struct cb_http_request))
            {download=((const struct cb_http_request *)payload)->download;ok=cb_fetch_request_progress((const struct cb_http_request *)payload,&r,socket);}
        else snprintf(r.error,sizeof r.error,"Invalid HTTP request payload");
        if(req.fd>=0)close(req.fd);
        free(payload);
        struct cb_message reply={.type=ok?CB_ERROR:CB_HTTP_META,.fd=-1};
        if(ok)snprintf(reply.text,sizeof reply.text,"%s",r.error);
        else reply.fd=cb_document_fd((const char *)&r.headers,sizeof r.headers);
        int result=(!ok&&reply.fd<0)?-1:cb_send(socket,&reply);if(reply.fd>=0)close(reply.fd);
        if(!ok&&!result){reply=(struct cb_message){.type=CB_DOCUMENT,.a=r.status,.fd=download?cb_blob_fd(r.body,r.length):cb_document_fd(r.body,r.length)};
            result=reply.fd<0?-1:cb_send(socket,&reply);if(reply.fd>=0)close(reply.fd);}
        cb_response_free(&r);close(socket);return result?1:0;
    }
    if(req.fd>=0){close(req.fd);close(socket);return 2;}
    if(req.type!=CB_FETCH){close(socket);return 2;}
    alarm(25);
    struct cb_response r={0};int ok=-1;const char *initiator=NULL,*policy=NULL;
    if(req.a>2||req.b||req.c>1)snprintf(r.error,sizeof r.error,"Invalid fetch kind or arguments");
    else {
        if(req.a){
            char *separator=strchr(req.text,'\n');
            if(separator&&separator!=req.text&&separator[1]){
                *separator='\0';initiator=separator+1;
                char *next=strchr(separator+1,'\n');
                if(next){*next=0;if(req.c)policy=next+1;else initiator=NULL;}
            }else snprintf(r.error,sizeof r.error,"Resource request needs target URL and document URL");
        }
        if(!req.a||initiator)ok=fetch_kind(req.a,req.text,initiator,policy,&r);
    }
    struct cb_message reply={.fd=-1,.type=ok?CB_ERROR:CB_DOCUMENT,.a=r.status};
    if(ok)snprintf(reply.text,sizeof(reply.text),"%s",r.error);
    else {
        if(!req.a&&r.content_type[0]&&!mime_is(r.content_type,"text/html")&&!mime_is(r.content_type,"text/plain")&&!mime_is(r.content_type,"application/xhtml+xml")){
            reply.type=CB_ERROR;snprintf(reply.text,sizeof(reply.text),"This build cannot display %s. Only HTML and plain text are supported.",r.content_type);
        }else{
            reply.b=req.a?req.a:(unsigned)mime_is(r.content_type,"text/plain");
            reply.fd=cb_document_fd(r.body,r.length);
            if(req.a)snprintf(reply.text,sizeof(reply.text),"%s\n%s",r.url,r.content_type);
            else snprintf(reply.text,sizeof(reply.text),"%s",r.url);
            if(reply.fd<0){reply.type=CB_ERROR;snprintf(reply.text,sizeof(reply.text),"Cannot prepare bounded response buffer.");}
        }
    }
    if(req.c&&!req.a&&!ok){struct cb_message meta={.type=CB_POLICY,.fd=-1};snprintf(meta.text,sizeof meta.text,"%s",r.policy);if(cb_send(socket,&meta)){if(reply.fd>=0)close(reply.fd);cb_response_free(&r);close(socket);return 1;}}
    int result=cb_send(socket,&reply);if(reply.fd>=0)close(reply.fd);cb_response_free(&r);close(socket);return result?1:0;
}
