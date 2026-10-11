#define _GNU_SOURCE
#include "session.h"
#include "storage.h"
#include "ipc.h"
#include "suffix-data.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <glib.h>
#include <sqlite3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#define COOKIE_LIMIT 256
#define CACHE_LIMIT 32
#define CACHE_BYTES (16u*1024u*1024u)
struct cookie {char name[256],value[3840],domain[254],path[1024];time_t expires,created;bool host,secure,http_only;unsigned same;};
struct cache {char url[4096];char *body;size_t size;struct cb_http_metadata meta;unsigned age;};
struct cb_session {struct cookie cookies[COOKIE_LIMIT];size_t count;sqlite3 *db;bool private_mode;struct cache cache[CACHE_LIMIT];unsigned age;size_t bytes;};
static int rule(const char *value){
    size_t low=0,high=G_N_ELEMENTS(suffix_rules);
    while(low<high){size_t m=low+(high-low)/2;int c=strcmp(value,suffix_rules[m]);if(!c)return 1;if(c<0)high=m;else low=m+1;}return 0;
}
static const char *suffix(const char *host){
    const char *best=strrchr(host,'.');best=best?best+1:host;
    for(const char *part=host;part;){char key[260];snprintf(key,sizeof key,"!%s",part);
        if(rule(key)){const char *p=strchr(part,'.');return p?p+1:part;}
        if(rule(part)&&strlen(part)>strlen(best))best=part;
        const char *dot=strchr(part,'.');if(dot){snprintf(key,sizeof key,"*.%s",dot+1);if(rule(key)&&strlen(part)>strlen(best))best=part;}
        part=dot?dot+1:NULL;
    }return best;
}
int cb_public_suffix(const char *host){return !strcmp(host,suffix(host));}
static const char *site_host(const char *host){
    struct in_addr addr;if(inet_pton(AF_INET,host,&addr)==1)return host;
    const char *s=suffix(host);if(s==host)return host;
    const char *p=s-1;while(p>host&&p[-1]!='.')p--;return p;
}
int cb_same_site(const char *a,const char *b){struct cb_http_url x,y;return !cb_http_parse(a,&x)&&!cb_http_parse(b,&y)&&x.tls==y.tls&&!strcmp(site_host(x.host),site_host(y.host));}
static int domain_match(const char *host,const char *domain){size_t a=strlen(host),b=strlen(domain);return a>=b&&!strcmp(host+a-b,domain)&&(a==b||host[a-b-1]=='.');}
static int path_match(const char *path,const char *cookie){size_t n=strlen(cookie);return !strncmp(path,cookie,n)&&(!path[n]||path[n]=='?'||cookie[n-1]=='/'||path[n]=='/');}
static int clean(const char *s){for(;*s;s++)if((unsigned char)*s<32||(unsigned char)*s>126)return 0;return 1;}
static void persist(struct cb_session *s,const struct cookie *c){
    if(!s->db||s->private_mode)return;
    sqlite3_stmt *q=NULL;
    if(sqlite3_prepare_v2(s->db,"DELETE FROM cookies WHERE name=? AND domain=? AND path=?",-1,&q,NULL)==SQLITE_OK){
        sqlite3_bind_text(q,1,c->name,-1,SQLITE_TRANSIENT);sqlite3_bind_text(q,2,c->domain,-1,SQLITE_TRANSIENT);sqlite3_bind_text(q,3,c->path,-1,SQLITE_TRANSIENT);sqlite3_step(q);}sqlite3_finalize(q);
    if(!c->expires||c->expires<=time(NULL))return;
    if(sqlite3_prepare_v2(s->db,"INSERT INTO cookies VALUES(?,?,?,?,?,?,?,?,?,?)",-1,&q,NULL)==SQLITE_OK){
        sqlite3_bind_text(q,1,c->name,-1,SQLITE_TRANSIENT);sqlite3_bind_text(q,2,c->domain,-1,SQLITE_TRANSIENT);sqlite3_bind_text(q,3,c->path,-1,SQLITE_TRANSIENT);sqlite3_bind_text(q,4,c->value,-1,SQLITE_TRANSIENT);
        sqlite3_bind_int64(q,5,c->expires);sqlite3_bind_int64(q,6,c->created);sqlite3_bind_int(q,7,c->host);sqlite3_bind_int(q,8,c->secure);sqlite3_bind_int(q,9,c->http_only);sqlite3_bind_int(q,10,c->same);sqlite3_step(q);}sqlite3_finalize(q);
}
struct cb_session *cb_session_open(bool private_mode){
    struct cb_session *s=calloc(1,sizeof *s);if(!s)return NULL;s->private_mode=private_mode;
    if(private_mode){if(sqlite3_open(":memory:",&s->db)!=SQLITE_OK){cb_session_close(s);return NULL;}}
    else s->db=cb_storage_open("sites.sqlite3");
    if(!s->db){cb_session_close(s);return NULL;}
    sqlite3_limit(s->db,SQLITE_LIMIT_LENGTH,2*1024*1024);
    const char *sql="PRAGMA trusted_schema=OFF;PRAGMA secure_delete=ON;"
        "CREATE TABLE IF NOT EXISTS cookies(name TEXT,domain TEXT,path TEXT,value TEXT,expires INTEGER,created INTEGER,host INTEGER,secure INTEGER,httponly INTEGER,same INTEGER,PRIMARY KEY(name,domain,path));"
        "CREATE TABLE IF NOT EXISTS local(origin TEXT,key TEXT,value TEXT,PRIMARY KEY(origin,key));"
        "CREATE TABLE IF NOT EXISTS permissions(origin TEXT PRIMARY KEY,javascript INTEGER);"
        "CREATE TEMP TABLE session(origin TEXT,key TEXT,value TEXT,PRIMARY KEY(origin,key));"
        "DELETE FROM cookies WHERE expires<=unixepoch();";
    if(sqlite3_exec(s->db,sql,NULL,NULL,NULL)!=SQLITE_OK){cb_session_close(s);return NULL;}
    sqlite3_stmt *q=NULL;
    if(sqlite3_prepare_v2(s->db,"SELECT * FROM cookies ORDER BY created DESC LIMIT 256",-1,&q,NULL)==SQLITE_OK){
        while(sqlite3_step(q)==SQLITE_ROW&&s->count<COOKIE_LIMIT){struct cookie c={0};char *fields[]={c.name,c.domain,c.path,c.value};size_t caps[]={sizeof c.name,sizeof c.domain,sizeof c.path,sizeof c.value};int valid=1;
            for(int i=0;i<4;i++){const char *v=(const char *)sqlite3_column_text(q,i);if(!v||strlen(v)>=caps[i]||!clean(v)){valid=0;break;}strcpy(fields[i],v);}
            if(!valid||!*c.name||!*c.domain||c.path[0]!='/')continue;
            c.expires=sqlite3_column_int64(q,4);c.created=sqlite3_column_int64(q,5);c.host=sqlite3_column_int(q,6);c.secure=sqlite3_column_int(q,7);c.http_only=sqlite3_column_int(q,8);c.same=sqlite3_column_int(q,9);
            if(c.same>2||(!c.host&&cb_public_suffix(c.domain)))continue;
            s->cookies[s->count++]=c;
        }
    }sqlite3_finalize(q);return s;
}
void cb_session_close(struct cb_session *s){if(!s)return;for(unsigned i=0;i<CACHE_LIMIT;i++)free(s->cache[i].body);if(s->db)sqlite3_close(s->db);free(s);}
void cb_session_clear(struct cb_session *s){if(!s)return;s->count=0;for(unsigned i=0;i<CACHE_LIMIT;i++){free(s->cache[i].body);memset(&s->cache[i],0,sizeof s->cache[i]);}s->bytes=0;sqlite3_exec(s->db,"DELETE FROM cookies;DELETE FROM local;DELETE FROM session;DELETE FROM permissions;VACUUM;",NULL,NULL,NULL);}
int cb_cookie_set(struct cb_session *s,const char *url,const char *line,time_t now){
    if(!s||!line||strlen(line)>=4096||!clean(line))return -1;
    struct cb_http_url u;if(cb_http_parse(url,&u))return -1;
    char input[4096];strcpy(input,line);char *save=NULL,*pair=strtok_r(input,";",&save);if(!pair)return -1;
    char *eq=strchr(pair,'=');if(!eq)return -1;*eq++=0;char *name=g_strstrip(pair),*value=g_strstrip(eq);
    if(!*name||strlen(name)>=256||strlen(value)>=3840||strpbrk(name,"()<>@,:\\\"/[]?={} \t"))return -1;
    for(const char *p=value;*p;p++)if(*p=='\\'||*p==','||*p==' '||*p=='\t')return -1;
    struct cookie c={.host=true,.same=1,.created=now};strcpy(c.name,name);strcpy(c.value,value);strcpy(c.domain,u.host);
    snprintf(c.path,sizeof c.path,"%.*s",(int)strcspn(u.path,"?"),u.path);char *last=strrchr(c.path,'/');if(last&&last!=c.path)*last=0;else strcpy(c.path,"/");
    bool max_age=false,domain=false,path=false;
    for(char *attr=strtok_r(NULL,";",&save);attr;attr=strtok_r(NULL,";",&save)){
        attr=g_strstrip(attr);char *v=strchr(attr,'=');if(v){*v++=0;v=g_strstrip(v);}else v="";g_strchomp(attr);
        if(!strcasecmp(attr,"domain")){
            if(*v=='.')v++;
            size_t n=strlen(v);if(!n||n>=sizeof c.domain||v[n-1]=='.')return -1;
            for(char *p=v;*p;p++)*p=(char)tolower((unsigned char)*p);
            struct in_addr ip;if(inet_pton(AF_INET,u.host,&ip)==1||!domain_match(u.host,v)||cb_public_suffix(v))return -1;
            strcpy(c.domain,v);c.host=false;domain=true;
        }else if(!strcasecmp(attr,"path")&&*v=='/'){if(strlen(v)>=sizeof c.path)return -1;strcpy(c.path,v);path=true;}
        else if(!strcasecmp(attr,"secure"))c.secure=true;
        else if(!strcasecmp(attr,"httponly"))c.http_only=true;
        else if(!strcasecmp(attr,"samesite"))c.same=!strcasecmp(v,"strict")?2:!strcasecmp(v,"none")?0:1;
        else if(!strcasecmp(attr,"max-age")){char *end=NULL;long long age=strtoll(v,&end,10);if(*v&&end&&!*end){if(age>34560000)age=34560000;c.expires=age<=0?1:now+age;max_age=true;}}
        else if(!strcasecmp(attr,"expires")&&!max_age){struct tm tm={0};char *end=strptime(v,"%a, %d %b %Y %H:%M:%S GMT",&tm);if(end&&!*end){time_t t=timegm(&tm);c.expires=t<=0?1:t>now+34560000?now+34560000:t;}}
        else if(!strcasecmp(attr,"partitioned"))return -1;
    }
    if((c.secure&&!u.tls)||(!c.same&&!c.secure))return -1;
    if(!strncasecmp(c.name,"__Secure-",9)&&(!c.secure||!u.tls))return -1;
    if(!strncasecmp(c.name,"__Host-",7)&&(!c.secure||!u.tls||domain||!path||strcmp(c.path,"/")))return -1;
    size_t match=s->count,per_domain=0,oldest=0;
    for(size_t i=0;i<s->count;i++){
        struct cookie *old=&s->cookies[i];
        if(!u.tls&&old->secure&&!strcmp(old->name,c.name)&&domain_match(c.domain,old->domain)&&path_match(c.path,old->path))return -1;
        if(!strcmp(old->domain,c.domain))per_domain++;
        if(old->created<s->cookies[oldest].created)oldest=i;
        if(!strcmp(old->name,c.name)&&!strcmp(old->domain,c.domain)&&!strcmp(old->path,c.path))match=i;
    }
    if(match<s->count)c.created=s->cookies[match].created;
    if(c.expires&&c.expires<=now){persist(s,&c);if(match<s->count)s->cookies[match]=s->cookies[--s->count];return 0;}
    if(match==s->count){if(per_domain>=50)return -1;if(s->count==COOKIE_LIMIT){s->cookies[oldest].expires=1;persist(s,&s->cookies[oldest]);match=oldest;}else s->count++;}
    s->cookies[match]=c;persist(s,&c);return 0;
}
void cb_cookie_header(struct cb_session *s,const char *url,const char *site,bool top,bool post,time_t now,char out[CB_COOKIE_HEADER]){
    out[0]=0;struct cb_http_url u;if(!s||cb_http_parse(url,&u))return;
    bool same=cb_same_site(url,site);if(!top&&!same)return; /* Third-party cookies disabled. */
    size_t indices[COOKIE_LIMIT],count=0;
    for(size_t i=0;i<s->count;i++){struct cookie *c=&s->cookies[i];
        if((c->expires&&c->expires<=now)||(c->secure&&!u.tls)||(c->host?strcmp(c->domain,u.host):!domain_match(u.host,c->domain))||!path_match(u.path,c->path))continue;
        if(!same&&(c->same==2||(c->same==1&&(!top||post))))continue;
        size_t j=count;while(j&&strlen(s->cookies[indices[j-1]].path)<strlen(c->path)){indices[j]=indices[j-1];j--;}indices[j]=i;count++;
    }
    size_t used=0;for(size_t i=0;i<count;i++){struct cookie *c=&s->cookies[indices[i]];size_t n=strlen(c->name)+strlen(c->value)+1+(used?2:0);if(n>=CB_COOKIE_HEADER-used)continue;used+=(size_t)snprintf(out+used,CB_COOKIE_HEADER-used,"%s%s=%s",used?"; ":"",c->name,c->value);}
}
static sqlite3_stmt *site_query(struct cb_session *s,const char *url,bool local,const char *sql,const char *key,const char *value){
    char origin[320],query[512];if(!s||cb_origin(url,origin))return NULL;
    snprintf(query,sizeof query,sql,local?"local":"session");sqlite3_stmt *q=NULL;
    if(sqlite3_prepare_v2(s->db,query,-1,&q,NULL)!=SQLITE_OK)return NULL;
    sqlite3_bind_text(q,1,origin,-1,SQLITE_TRANSIENT);if(key)sqlite3_bind_text(q,2,key,-1,SQLITE_TRANSIENT);if(value)sqlite3_bind_text(q,3,value,-1,SQLITE_TRANSIENT);return q;
}
char *cb_site_get(struct cb_session *s,const char *url,bool local,const char *key){
    sqlite3_stmt *q=site_query(s,url,local,"SELECT value FROM %s WHERE origin=?1 AND key=?2",key,NULL);char *v=NULL;
    if(q&&sqlite3_step(q)==SQLITE_ROW)v=strdup((const char *)sqlite3_column_text(q,0));
    sqlite3_finalize(q);return v;
}
static size_t json_size(const char *s){size_t n=2;for(const unsigned char *p=(const unsigned char *)s;*p;p++)n+=*p<32?6:*p=='"'||*p=='\\'?2:1;return n;}
int cb_site_set(struct cb_session *s,const char *url,bool local,const char *key,const char *value){
    if(!key||strlen(key)>1024||(value&&strlen(value)>16384)||!g_utf8_validate(key,-1,NULL)||(value&&!g_utf8_validate(value,-1,NULL)))return -1;
    sqlite3_stmt *q=site_query(s,url,local,"SELECT key,value FROM %s WHERE origin=?1 AND key<>?2",key,NULL);
    size_t bytes=value?json_size(key)+json_size(value)+4:2;unsigned count=0;int step=SQLITE_ERROR;
    while(q&&(step=sqlite3_step(q))==SQLITE_ROW){count++;bytes+=json_size((const char *)sqlite3_column_text(q,0))+json_size((const char *)sqlite3_column_text(q,1))+2;}
    int valid=step==SQLITE_DONE&&(!value||(count<128&&bytes<=262144));sqlite3_finalize(q);if(!valid)return -1;
    q=site_query(s,url,local,"SELECT count(DISTINCT origin) FROM %s WHERE origin<>?1",NULL,NULL);valid=q&&sqlite3_step(q)==SQLITE_ROW&&sqlite3_column_int(q,0)<64;sqlite3_finalize(q);if(!valid)return -1;
    q=site_query(s,url,local,value?"INSERT OR REPLACE INTO %s VALUES(?1,?2,?3)":"DELETE FROM %s WHERE origin=?1 AND key=?2",key,value);int rc=q&&sqlite3_step(q)==SQLITE_DONE?0:-1;sqlite3_finalize(q);return rc;
}
int cb_site_clear(struct cb_session *s,const char *url,bool local){sqlite3_stmt *q=site_query(s,url,local,"DELETE FROM %s WHERE origin=?1",NULL,NULL);int rc=q&&sqlite3_step(q)==SQLITE_DONE?0:-1;sqlite3_finalize(q);return rc;}
static void json_string(GString *out,const char *s){g_string_append_c(out,'"');for(const unsigned char *p=(const unsigned char *)s;*p;p++){if(*p=='"'||*p=='\\'){g_string_append_c(out,'\\');g_string_append_c(out,*p);}else if(*p<32)g_string_append_printf(out,"\\u%04x",*p);else g_string_append_c(out,*p);}g_string_append_c(out,'"');}
char *cb_site_snapshot(struct cb_session *s,const char *url,bool local){
    GString *out=g_string_new("{");sqlite3_stmt *q=site_query(s,url,local,"SELECT key,value FROM %s WHERE origin=?1 ORDER BY rowid",NULL,NULL);unsigned count=0;
    while(q&&sqlite3_step(q)==SQLITE_ROW&&count<128){if(count++)g_string_append_c(out,',');json_string(out,(const char *)sqlite3_column_text(q,0));g_string_append_c(out,':');json_string(out,(const char *)sqlite3_column_text(q,1));}
    sqlite3_finalize(q);g_string_append_c(out,'}');return g_string_free(out,FALSE);
}
void cb_cache_prepare(struct cb_session *s,struct cb_http_request *r){
    r->etag[0]=r->modified[0]=0;if(!s||r->post||*r->cookie)return;
    for(unsigned i=0;i<CACHE_LIMIT;i++)if(s->cache[i].body&&!strcmp(s->cache[i].url,r->url)){strcpy(r->etag,s->cache[i].meta.etag);strcpy(r->modified,s->cache[i].meta.modified);return;}
}
int cb_cache_finish(struct cb_session *s,const struct cb_http_request *r,struct cb_http_metadata *m,int *fd){
    if(!s)return -1;
    struct cache *found=NULL,*oldest=&s->cache[0];
    for(unsigned i=0;i<CACHE_LIMIT;i++){struct cache *c=&s->cache[i];if(!strcmp(c->url,r->url))found=c;if(!c->body||c->age<oldest->age)oldest=c;}
    if(m->status==304){
        if(!found||!found->body||r->post||*r->cookie||(!*r->etag&&!*r->modified))return -1;
        if(*fd>=0)close(*fd);
        *fd=cb_document_fd(found->body,found->size);struct cb_http_metadata saved=found->meta;
        /* Retain current response cookies (never serve stale Set-Cookie). */
#define UPDATED(field) if(*m->field)strcpy(saved.field,m->field)
        UPDATED(policy);UPDATED(mime);UPDATED(etag);UPDATED(modified);UPDATED(cache_control);UPDATED(vary);
        UPDATED(allow_origin);UPDATED(allow_credentials);UPDATED(disposition);
#undef UPDATED
        saved.cookie_count=m->cookie_count;memcpy(saved.cookies,m->cookies,sizeof saved.cookies);*m=saved;found->meta=saved;found->meta.cookie_count=0;found->age=++s->age;
        if(saved.cookie_count||*saved.vary||strcasestr(saved.cache_control,"no-store")||strcasestr(saved.cache_control,"private")){s->bytes-=found->size;free(found->body);memset(found,0,sizeof *found);}
        return *fd<0?-1:0;
    }
    bool eligible=!r->post&&!*r->cookie&&m->status==200&&!m->cookie_count&&!*m->vary&&
        !strcasestr(m->cache_control,"no-store")&&!strcasestr(m->cache_control,"private")&&(*m->etag||*m->modified);
    if(found){s->bytes-=found->size;free(found->body);memset(found,0,sizeof *found);}
    if(!eligible)return 0;
    char *body=NULL;size_t size;if(cb_read_document(*fd,&body,&size))return -1;
    struct cache *c=found?found:oldest;if(s->bytes-c->size+size>CACHE_BYTES){free(body);return 0;}
    s->bytes-=c->size;free(c->body);memset(c,0,sizeof *c);c->body=body;c->size=size;c->meta=*m;c->age=++s->age;strcpy(c->url,r->url);s->bytes+=size;return 0;
}

int cb_site_scripts(struct cb_session *s,const char *url,int fallback){
    char origin[320];if(!s||cb_origin(url,origin))return fallback;sqlite3_stmt *q=NULL;
    if(sqlite3_prepare_v2(s->db,"SELECT javascript FROM permissions WHERE origin=?",-1,&q,NULL)==SQLITE_OK){sqlite3_bind_text(q,1,origin,-1,SQLITE_TRANSIENT);if(sqlite3_step(q)==SQLITE_ROW)fallback=sqlite3_column_int(q,0)!=0;}sqlite3_finalize(q);return fallback;
}
int cb_site_scripts_set(struct cb_session *s,const char *url,int enabled){
    char origin[320];if(!s||cb_origin(url,origin))return -1;sqlite3_stmt *q=NULL;int result=-1;
    if(sqlite3_prepare_v2(s->db,"INSERT OR REPLACE INTO permissions VALUES(?,?)",-1,&q,NULL)==SQLITE_OK){sqlite3_bind_text(q,1,origin,-1,SQLITE_TRANSIENT);sqlite3_bind_int(q,2,!!enabled);result=sqlite3_step(q)==SQLITE_DONE?0:-1;}sqlite3_finalize(q);return result;
}
