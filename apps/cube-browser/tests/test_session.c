#define _GNU_SOURCE
#include "session.h"
#include "ipc.h"
#include "broker.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
static void cookies(void){
    struct cb_session *s=cb_session_open(true);assert(s);time_t now=time(NULL);char header[CB_COOKIE_HEADER];
    assert(cb_public_suffix("co.uk"));assert(cb_public_suffix("github.io"));assert(cb_public_suffix("b.ck"));assert(!cb_public_suffix("www.ck"));
    assert(cb_same_site("https://one.example.co.uk/","https://two.example.co.uk/"));assert(!cb_same_site("https://a.github.io/","https://b.github.io/"));assert(!cb_same_site("http://example.org/","https://example.org/"));
    assert(!cb_cookie_set(s,"https://a.example.org/path/login","sid=one; Secure; HttpOnly; Path=/; SameSite=Lax",now));
    assert(!cb_cookie_set(s,"https://a.example.org/path/login","strict=two; Secure; Path=/; SameSite=Strict",now));
    assert(!cb_cookie_set(s,"https://a.example.org/path/login","shared=yes; Domain=example.org; Path=/",now));
    assert(cb_cookie_set(s,"https://a.example.org/","bad=yes; Domain=org",now));
    assert(cb_cookie_set(s,"https://a.github.io/","bad=yes; Domain=github.io",now));
    assert(cb_cookie_set(s,"https://a.example.org/","bad=yes; Domain=foreign.org",now));
    assert(cb_cookie_set(s,"http://a.example.org/","sid=overwrite; Path=/",now));
    assert(cb_cookie_set(s,"https://a.example.org/","none=bad; SameSite=None",now));
    assert(cb_cookie_set(s,"https://a.example.org/","__Host-id=bad; Secure; Domain=example.org; Path=/",now));
    assert(!cb_cookie_set(s,"https://a.example.org/","__Host-id=ok; Secure; Path=/",now));
    cb_cookie_header(s,"https://a.example.org/path","https://a.example.org",false,false,now,header);assert(strstr(header,"sid=one")&&strstr(header,"strict=two"));
    cb_cookie_header(s,"https://a.example.org/path","https://foreign.org",false,false,now,header);assert(!*header);
    cb_cookie_header(s,"https://a.example.org/path","https://foreign.org",true,false,now,header);assert(strstr(header,"sid=one")&&!strstr(header,"strict=two"));
    cb_cookie_header(s,"https://a.example.org/path","https://foreign.org",true,true,now,header);assert(!*header);
    cb_cookie_header(s,"https://b.example.org/path","https://b.example.org",false,false,now,header);assert(!strcmp(header,"shared=yes"));
    assert(!cb_cookie_set(s,"https://a.example.org/path","narrow=1; Path=/path; Max-Age=1",now));
    cb_cookie_header(s,"https://a.example.org/path/a","https://a.example.org",false,false,now,header);assert(!strncmp(header,"narrow=1; ",10));
    cb_cookie_header(s,"https://a.example.org/path/a","https://a.example.org",false,false,now+2,header);assert(!strstr(header,"narrow="));
    assert(!cb_cookie_set(s,"https://a.example.org/","sid=; Secure; Path=/; Max-Age=0",now));
    cb_cookie_header(s,"https://a.example.org/","https://a.example.org",true,false,now,header);assert(!strstr(header,"sid="));
    cb_session_clear(s);cb_cookie_header(s,"https://a.example.org/","https://a.example.org",true,false,now,header);assert(!*header);cb_session_close(s);
}
static void storage(void){
    char dir[]="/tmp/cube-session-XXXXXX";assert(mkdtemp(dir));setenv("XDG_DATA_HOME",dir,1);
    struct cb_session *s=cb_session_open(false);assert(s);time_t now=time(NULL);
    assert(!cb_site_scripts_set(s,"https://example.org/a",0));assert(!cb_site_scripts(s,"https://example.org/b",1));
    assert(!cb_site_set(s,"https://example.org/a",true,"key","saved"));assert(!cb_site_set(s,"https://example.org/a",false,"key","temporary"));
    assert(!cb_site_get(s,"https://foreign.org",true,"key"));assert(!cb_site_get(s,"http://example.org",true,"key"));assert(cb_site_set(s,"file:///tmp/a",true,"key","value"));
    assert(!cb_cookie_set(s,"https://example.org","persist=yes; Max-Age=3600",now));assert(!cb_cookie_set(s,"https://example.org","session=yes",now));cb_session_close(s);
    s=cb_session_open(false);assert(s);assert(!cb_site_scripts(s,"https://example.org",1));char *v=cb_site_get(s,"https://example.org/b",true,"key");assert(v&&!strcmp(v,"saved"));free(v);assert(!cb_site_get(s,"https://example.org",false,"key"));
    char header[CB_COOKIE_HEADER];cb_cookie_header(s,"https://example.org","https://example.org",true,false,now,header);assert(strstr(header,"persist=yes")&&!strstr(header,"session=yes"));
    struct cb_session *private=cb_session_open(true);assert(private);assert(cb_site_scripts(private,"https://example.org",1));assert(!cb_site_get(private,"https://example.org",true,"key"));assert(!cb_site_set(private,"https://example.org",true,"key","private"));cb_session_close(private);
    v=cb_site_get(s,"https://example.org",true,"key");assert(v&&!strcmp(v,"saved"));free(v);
    for(unsigned i=0;i<127;i++){char key[32];snprintf(key,sizeof key,"k%u",i);assert(!cb_site_set(s,"https://example.org",true,key,"v"));}assert(cb_site_set(s,"https://example.org",true,"overflow","no"));
    assert(!cb_site_set(s,"https://example.org",true,"key","changed"));v=cb_site_snapshot(s,"https://example.org",true);assert(strstr(v,"\"key\":\"changed\""));free(v);
    cb_session_clear(s);assert(!cb_site_get(s,"https://example.org",true,"key"));cb_session_close(s);
    char file[512];snprintf(file,sizeof file,"%s/cube-browser/sites.sqlite3",dir);struct stat st;assert(!stat(file,&st)&&!(st.st_mode&077));unlink(file);snprintf(file,sizeof file,"%s/cube-browser",dir);rmdir(file);rmdir(dir);
}
static void encoded_quota(void){
    struct cb_session *s=cb_session_open(true);assert(s);char value[16385];memset(value,1,sizeof value-1);value[sizeof value-1]=0;
    for(unsigned local=0;local<2;local++){
        assert(!cb_site_set(s,"https://example.org",local,"one",value));
        assert(!cb_site_set(s,"https://example.org",local,"two",value));
        assert(cb_site_set(s,"https://example.org",local,"three",value));
        char *snapshot=cb_site_snapshot(s,"https://example.org",local);assert(strlen(snapshot)<=262144);free(snapshot);
    }
    cb_session_close(s);
}
static void storage_disclosure(void){
    const char *url="https://example.org";struct cb_session *s=cb_session_open(true);assert(s);
    assert(!cb_site_set(s,url,true,"secret","local-value"));assert(!cb_site_set(s,url,false,"secret","session-value"));
    char *json=cb_broker_storage(s,url,"",true);assert(strstr(json,"local-value")&&strstr(json,"session-value"));free(json);
    const char *policies[]={"sandbox","sandbox allow-scripts","sandbox allow-same-origin\nsandbox allow-forms"};
    for(unsigned i=0;i<sizeof policies/sizeof *policies;i++){
        json=cb_broker_storage(s,url,policies[i],true);assert(!strcmp(json,"[{},{}]"));free(json);
        struct cb_http_job *job=cb_broker_new("https://example.org/p.png",url,url,policies[i],CB_POLICY_IMAGE,false,NULL);
        assert(job&&!job->credentials);free(job);
    }
    json=cb_broker_storage(s,url,"",false);assert(!strcmp(json,"[{},{}]"));free(json);
    json=cb_broker_storage(s,"https://foreign.org","",true);assert(!strcmp(json,"[{},{}]"));free(json);
    cb_session_close(s);
}
static void cache(void){
    struct cb_session *s=cb_session_open(true);assert(s);struct cb_http_request q={.version=1};strcpy(q.url,"https://example.org/a");
    struct cb_http_metadata *m=calloc(1,sizeof *m);m->version=1;m->status=200;strcpy(m->mime,"text/html");strcpy(m->etag,"\"one\"");
    int fd=cb_document_fd("cached",6);assert(!cb_cache_finish(s,&q,m,&fd));close(fd);cb_cache_prepare(s,&q);assert(!strcmp(q.etag,"\"one\""));
    m->status=304;fd=cb_document_fd("",0);assert(!cb_cache_finish(s,&q,m,&fd));assert(m->status==200);char *body=NULL;size_t size=0;assert(!cb_read_document(fd,&body,&size)&&size==6&&!strcmp(body,"cached"));free(body);close(fd);
    strcpy(m->cache_control,"no-store");fd=cb_document_fd("fresh",5);assert(!cb_cache_finish(s,&q,m,&fd));close(fd);cb_cache_prepare(s,&q);assert(!*q.etag);
    free(m);cb_session_close(s);
}
static void cors_revalidation(void){
    struct cb_session *s=cb_session_open(true);assert(s);
    struct cb_http_job *j=cb_broker_new("https://api.example.net/data","https://example.org/","https://example.org/","",CB_POLICY_CONNECT,false,NULL);assert(j);
    j->credentials=1;j->have_meta=true;j->meta.version=1;j->meta.status=200;
    strcpy(j->meta.mime,"text/plain");strcpy(j->meta.etag,"\"cors\"");strcpy(j->meta.allow_origin,"https://example.org");
    struct cb_message m={.type=CB_DOCUMENT,.a=200,.fd=cb_document_fd("CORS_BODY",9)};
    assert(cb_broker_receive(j,s,&m)==1);close(m.fd);
    for(unsigned revoke=0;revoke<2;revoke++){
        cb_cache_prepare(s,&j->request);assert(*j->request.etag);
        memset(&j->meta,0,sizeof j->meta);j->meta.version=1;j->meta.status=304;
        if(revoke)strcpy(j->meta.allow_origin,"https://other.example.org");
        m=(struct cb_message){.type=CB_DOCUMENT,.a=304,.fd=cb_document_fd("",0)};
        int result=cb_broker_receive(j,s,&m);
        if(revoke){assert(result==-1&&strstr(j->error,"CORS"));}
        else{assert(result==1);char *body=NULL;size_t n=0;assert(!cb_read_document(m.fd,&body,&n)&&n==9&&!strcmp(body,"CORS_BODY"));free(body);}
        close(m.fd);
    }
    free(j);cb_session_close(s);
}
int main(void){cookies();storage();encoded_quota();storage_disclosure();cache();cors_revalidation();puts("PSL, cookie policy, persistence/private storage, quotas and cache validation passed");}
