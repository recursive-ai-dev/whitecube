#define _POSIX_C_SOURCE 200809L
#include "policy.h"
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

int cb_http_parse(const char *s,struct cb_http_url *u) {
    if(!s||!*s||strlen(s)>=4096)return -1;
    for(const unsigned char *p=(const unsigned char *)s;*p;p++)if(*p<=32||*p==127||*p=='\\')return -1;
    memset(u,0,sizeof *u);
    if(!strncasecmp(s,"https://",8)){u->tls=true;u->port=443;s+=8;}
    else if(!strncasecmp(s,"http://",7)){u->port=80;s+=7;}else return -1;
    const char *end=s+strcspn(s,"/?#"),*colon=memchr(s,':',(size_t)(end-s));
    size_t n=(size_t)((colon?colon:end)-s);
    if(!n||n>=sizeof u->host||memchr(s,'@',(size_t)(end-s)))return -1;
    memcpy(u->host,s,n);
    size_t label=0;
    for(size_t i=0;i<n;i++){
        unsigned char c=(unsigned char)u->host[i];
        if(c=='.'){if(!label)return -1;label=0;}
        else if(c>=128||!(isalnum(c)||c=='-')||++label>63)return -1;
        u->host[i]=(char)tolower(c);
    }
    if(colon){
        if(colon+1==end)return -1;
        unsigned port=0;
        for(const char *p=colon+1;p<end;p++){if(!isdigit((unsigned char)*p))return -1;port=port*10u+(unsigned)(*p-'0');if(port>65535)return -1;}
        if(!port)return -1;
        u->port=port;
    }
    const char *fragment=strchr(end,'#');size_t length=fragment?(size_t)(fragment-end):strlen(end);
    if(fragment){u->has_fragment=true;snprintf(u->fragment,sizeof u->fragment,"%s",fragment+1);}
    if(length+2>=sizeof u->path)return -1;
    if(*end=='/')memcpy(u->path,end,length);else{u->path[0]='/';memcpy(u->path+1,end,length);}
    return 0;
}
int cb_origin(const char *url,char out[320]) {
    struct cb_http_url u;if(cb_http_parse(url,&u)){out[0]=0;return -1;}
    snprintf(out,320,"%s://%s:%u",u.tls?"https":"http",u.host,u.port);return 0;
}
int cb_origin_header(const char *url,char out[320]) {
    struct cb_http_url u;if(cb_http_parse(url,&u)){out[0]=0;return -1;}
    if(u.port==(u.tls?443u:80u))snprintf(out,320,"%s://%s",u.tls?"https":"http",u.host);
    else snprintf(out,320,"%s://%s:%u",u.tls?"https":"http",u.host,u.port);
    return 0;
}
int cb_same_origin(const char *a,const char *b){char left[320],right[320];return !cb_origin(a,left)&&!cb_origin(b,right)&&!strcmp(left,right);}
static char *trim(char *s){while(isspace((unsigned char)*s))s++;char *end=s+strlen(s);while(end>s&&isspace((unsigned char)end[-1]))*--end=0;return s;}
static int source_matches(const char *source,const char *document,const char *target){
    if(!strcmp(source,"'self'"))return cb_same_origin(document,target);
    if(!strncasecmp(target,"data:",5))return !strcmp(source,"data:");
    struct cb_http_url d,t;if(cb_http_parse(document,&d)||cb_http_parse(target,&t))return 0;
    if(!strcmp(source,"*"))return 1;
    if(!strcmp(source,"https:"))return t.tls;
    if(!strcmp(source,"http:"))return 1;
    if(*source=='\'')return 0;
    char absolute[4096];
    if(strstr(source,"://")){if(strlen(source)>=sizeof absolute)return 0;strcpy(absolute,source);}
    else if(snprintf(absolute,sizeof absolute,"%s://%s",d.tls?"https":"http",source)>=(int)sizeof absolute)return 0;
    char *host=strstr(absolute,"://");if(!host)return 0;host+=3;
    int wildcard=!strncmp(host,"*.",2);if(wildcard)memmove(host,host+2,strlen(host+2)+1);
    char *authority_end=host+strcspn(host,"/?#"),*port=memchr(host,':',(size_t)(authority_end-host));
    int any_port=port&&port+2==authority_end&&port[1]=='*';
    if(any_port)memmove(port,port+2,strlen(port+2)+1);
    struct cb_http_url allowed;if(cb_http_parse(absolute,&allowed))return 0;
    if(allowed.tls&&!t.tls)return 0;
    if(wildcard){size_t a=strlen(allowed.host),b=strlen(t.host);if(b<=a||t.host[b-a-1]!='.'||strcmp(t.host+b-a,allowed.host))return 0;}
    else if(strcmp(allowed.host,t.host))return 0;
    if(!any_port&&allowed.port!=t.port&&!(port==NULL&&!allowed.tls&&t.tls&&t.port==443))return 0;
    const char *path=strchr(strstr(absolute,"://")+3,'/');
    if(path){size_t n=strcspn(allowed.path,"?#"),tn=strcspn(t.path,"?#");
        if(n&&allowed.path[n-1]=='/'){if(tn<n||strncmp(allowed.path,t.path,n))return 0;}
        else if(tn!=n||strncmp(allowed.path,t.path,n))return 0;
    }
    return 1;
}
static int list_allows(char *list,const char *document,const char *target,int inline_code){
    int nonce=0,unsafe_inline=0,matched=0;char *save=NULL;
    for(char *s=strtok_r(list," \t\r",&save);s;s=strtok_r(NULL," \t\r",&save)){
        if(!strncmp(s,"'nonce-",7)||!strncmp(s,"'sha256-",8)||!strncmp(s,"'sha384-",8)||!strncmp(s,"'sha512-",8))nonce=1;
        if(!strcmp(s,"'unsafe-inline'"))unsafe_inline=1;
        if(!inline_code&&source_matches(s,document,target))matched=1;
    }
    return inline_code?unsafe_inline&&!nonce:matched;
}
static int one_policy(char *policy,const char *document,const char *target,unsigned kind,int inline_code){
    const char *names[]={"default-src","style-src","img-src","script-src","connect-src","form-action","style-src-attr"};
    char *lists[7]={0},*sandbox=NULL,*element=NULL,*save=NULL;
    for(char *d=strtok_r(policy,";",&save);d;d=strtok_r(NULL,";",&save)){
        d=trim(d);char *end=d;while(*end&&!isspace((unsigned char)*end))end++;
        if(*end)*end++=0;
        end=trim(end);
        for(unsigned i=0;i<7;i++)if(!strcasecmp(d,names[i])&&!lists[i])lists[i]=end;
        if(!strcasecmp(d,"sandbox")&&!sandbox)sandbox=end;
        if(kind==CB_POLICY_SCRIPT&&!strcasecmp(d,"script-src-elem")&&!element)element=end;
        if(kind==CB_POLICY_STYLE&&!strcasecmp(d,"style-src-elem")&&!element)element=end;
    }
    if(sandbox&&(kind==CB_POLICY_SCRIPT||kind==CB_POLICY_FORM)){
        char *s=NULL;int allowed=0;
        for(char *v=strtok_r(sandbox," \t",&s);v;v=strtok_r(NULL," \t",&s))
            if(!strcmp(v,kind==CB_POLICY_SCRIPT?"allow-scripts":"allow-forms"))allowed=1;
        if(!allowed)return 0;
    }
    if(element)lists[kind]=element;
    char *list=lists[kind]?lists[kind]:kind==CB_POLICY_STYLE_ATTR&&lists[CB_POLICY_STYLE]?lists[CB_POLICY_STYLE]:kind==CB_POLICY_FORM?NULL:lists[0];
    return !list||list_allows(list,document,target,inline_code);
}
int cb_policy_allows(const char *policy,const char *document,const char *target,unsigned kind,int inline_code){
    struct cb_http_url d,t;
    int data_image=kind==CB_POLICY_IMAGE&&target&&!strncasecmp(target,"data:",5);
    if(kind<1||kind>6||cb_http_parse(document,&d)||(!inline_code&&!data_image&&cb_http_parse(target,&t)))return 0;
    if(!inline_code&&!data_image&&d.tls&&!t.tls)return 0;
    if(!policy||!*policy)return 1;
    if((kind==CB_POLICY_SCRIPT||kind==CB_POLICY_FORM||kind==CB_POLICY_CONNECT)&&cb_policy_opaque(policy))return 0;
    if(strlen(policy)>=CB_POLICY_LIMIT)return 0;
    char copy[CB_POLICY_LIMIT];strcpy(copy,policy);char *save=NULL;
    for(char *p=strtok_r(copy,"\n,",&save);p;p=strtok_r(NULL,"\n,",&save))
        if(!one_policy(p,document,target,kind,inline_code))return 0;
    return 1;
}

int cb_http_format(const struct cb_http_url *u, char out[4096]) {
    char authority[280];
    if (u->port == (u->tls?443u:80u)) snprintf(authority,sizeof authority,"%s",u->host);
    else snprintf(authority,sizeof authority,"%s:%u",u->host,u->port);
    return snprintf(out,4096,"%s://%s%s%s%s",u->tls?"https":"http",authority,u->path,
                    u->has_fragment?"#":"",u->has_fragment?u->fragment:"") >= 4096 ? -1 : 0;
}
int cb_url_resolve(const char *base, const char *reference, char out[4096]) {
    struct cb_http_url u; char candidate[8192];
    if (!reference || strlen(reference)>=4096 || cb_http_parse(base,&u)) return -1;
    const char *scheme_end = strchr(reference, ':');
    if (scheme_end && (size_t)(scheme_end-reference) < strcspn(reference,"/?#") &&
        strncasecmp(reference,"http://",7) && strncasecmp(reference,"https://",8)) return -1;
    if (strstr(reference,"://")) snprintf(candidate,sizeof candidate,"%s",reference);
    else if (!strncmp(reference,"//",2)) snprintf(candidate,sizeof candidate,"%s:%s",u.tls?"https":"http",reference);
    else {
        char authority[4096]; u.path[0]='\0';u.has_fragment=false; if(cb_http_format(&u,authority)) return -1;
        struct cb_http_url original; if(cb_http_parse(base,&original)) return -1;
        char *query=strchr(original.path,'?');
        if (*reference=='/') snprintf(candidate,sizeof candidate,"%s%s",authority,reference);
        else if (*reference=='?') { if(query)*query='\0'; snprintf(candidate,sizeof candidate,"%s%s%s",authority,original.path,reference); }
        else if (!*reference || *reference=='#') snprintf(candidate,sizeof candidate,"%s%s%s",authority,original.path,reference);
        else { if(query)*query='\0'; char *slash=strrchr(original.path,'/'); if(slash)slash[1]='\0'; snprintf(candidate,sizeof candidate,"%s%s%s",authority,original.path,reference); }
    }
    if (cb_http_parse(candidate,&u)) return -1;
    /* Normalize dot segments, keeping the query untouched. */
    char clean[4096]="", path[4096], suffix[4096]="";
    snprintf(path,sizeof path,"%s",u.path); char *query=strchr(path,'?'); if(query) { snprintf(suffix,sizeof suffix,"%s",query); *query='\0'; }
    size_t size=0; const char *p=path;
    while (*p) {
        if (!strncmp(p,"/./",3)) { p+=2; continue; }
        if (!strcmp(p,"/.")) { p="/"; continue; }
        if (!strncmp(p,"/../",4) || !strcmp(p,"/..")) {
            if (size) { size_t at=size; while(at && clean[at-1]!='/')at--; size=at?at-1:0; clean[size]='\0'; }
            p+=3; if(!*p)p="/"; continue;
        }
        const char *next=strchr(p+(*p=='/'),'/'); size_t count=next?(size_t)(next-p):strlen(p);
        if(size+count>=sizeof clean)return -1;
        memcpy(clean+size,p,count); size+=count; clean[size]='\0'; p+=count;
    }
    if (snprintf(u.path,sizeof u.path,"%s%s",size?clean:"/",suffix) >= (int)sizeof u.path) return -1;
    return cb_http_format(&u,out);
}

int cb_policy_eval_allowed(const char *policy){
    if(!policy||!*policy)return 1;
    if(strlen(policy)>=CB_POLICY_LIMIT)return 0;
    char copy[CB_POLICY_LIMIT];strcpy(copy,policy);char *ps=NULL;
    for(char *p=strtok_r(copy,"\n,",&ps);p;p=strtok_r(NULL,"\n,",&ps)){
        char *fallback=NULL,*script=NULL,*ds=NULL;
        for(char *d=strtok_r(p,";",&ds);d;d=strtok_r(NULL,";",&ds)){d=trim(d);char *e=d;while(*e&&!isspace((unsigned char)*e))e++;if(*e)*e++=0;
            if(!strcasecmp(d,"script-src")&&!script)script=e;
            if(!strcasecmp(d,"default-src")&&!fallback)fallback=e;}
        char *list=script?script:fallback;if(!list)continue;int allowed=0;char *save=NULL;
        for(char *s=strtok_r(list," \t",&save);s;s=strtok_r(NULL," \t",&save))if(!strcmp(s,"'unsafe-eval'"))allowed=1;
        if(!allowed)return 0;
    }return 1;
}

int cb_policy_opaque(const char *policy){
    if(!policy||!*policy)return 0;
    if(strlen(policy)>=CB_POLICY_LIMIT)return 1;
    char copy[CB_POLICY_LIMIT];strcpy(copy,policy);char *ps=NULL;
    for(char *p=strtok_r(copy,"\n,",&ps);p;p=strtok_r(NULL,"\n,",&ps)){char *ds=NULL;
        for(char *d=strtok_r(p,";",&ds);d;d=strtok_r(NULL,";",&ds)){d=trim(d);char *end=d;while(*end&&!isspace((unsigned char)*end))end++;if(*end)*end++=0;
            if(strcasecmp(d,"sandbox"))continue;
            char *save=NULL;int same=0;
            for(char *s=strtok_r(end," \t",&save);s;s=strtok_r(NULL," \t",&save))if(!strcmp(s,"allow-same-origin"))same=1;
            if(!same)return 1;
            break;
        }
    }return 0;
}
