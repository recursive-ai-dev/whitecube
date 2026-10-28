#define _POSIX_C_SOURCE 200809L
#include "url.h"
#include <glib.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
int cb_url_remote(const char *url){return url&&(!strncasecmp(url,"http://",7)||!strncasecmp(url,"https://",8));}
static int clean_url(const char *url){
    if(!url||strnlen(url,4096)>4095||!g_utf8_validate(url,-1,NULL))return 0;
    for(const unsigned char *p=(const unsigned char *)url;*p;p++)if(*p<32||*p==127||*p=='\\')return 0;
    return 1;
}
static int valid_http(const char *url,GUri *uri){
    if(!uri||!cb_url_remote(url)||g_uri_get_userinfo(uri))return 0;
    const char *host=g_uri_get_host(uri);if(!host||!*host||g_uri_get_port(uri)==0)return 0;
    if(!g_hostname_is_ip_address(host)){
        if(strlen(host)>253)return 0;
        size_t label=0;
        for(const unsigned char *p=(const unsigned char *)host;*p;p++){
            if(*p=='.'){if(!label)return 0;label=0;}
            else if((!g_ascii_isalnum(*p)&&*p!='-')||++label>63)return 0;
        }
    }
    const char *start=strstr(url,"://")+3,*end=start+strcspn(start,"/?#"),*port=NULL;
    if(*start=='['){const char *close=memchr(start,']',(size_t)(end-start));if(!close)return 0;if(close+1<end){if(close[1]!=':')return 0;port=close+2;}}
    else{const char *colon=memchr(start,':',(size_t)(end-start));if(colon)port=colon+1;}
    if(port){
        if(port==end)return 0;
        unsigned number=0;
        for(const char *p=port;p<end;p++){if(*p<'0'||*p>'9')return 0;number=number*10u+(unsigned)(*p-'0');if(number>65535)return 0;}
        if(!number)return 0;
    }
    return 1;
}
static GUri *comparison_uri(const char *url){
    if(!clean_url(url)||!*url)return NULL;
    GUri *uri=g_uri_parse(url,G_URI_FLAGS_ENCODED|G_URI_FLAGS_SCHEME_NORMALIZE,NULL);
    if(!uri)return NULL;
    const char *scheme=g_uri_get_scheme(uri);
    int supported=cb_url_remote(url)?valid_http(url,uri):
        scheme&&(!g_ascii_strcasecmp(scheme,"file")||!g_ascii_strcasecmp(scheme,"cube"));
    if(!supported||g_uri_get_userinfo(uri)){g_uri_unref(uri);return NULL;}
    return uri;
}
int cb_url_same_document(const char *a,const char *b){
    GUri *left=comparison_uri(a);if(!left)return 0;
    char *resolved=NULL;
    if(b&&b[0]=='#'&&clean_url(b))resolved=g_uri_resolve_relative(a,b,G_URI_FLAGS_ENCODED|G_URI_FLAGS_SCHEME_NORMALIZE,NULL);
    GUri *right=comparison_uri(resolved?resolved:b);g_free(resolved);
    if(!right){g_uri_unref(left);return 0;}
    const char *lh=g_uri_get_host(left),*rh=g_uri_get_host(right);
    int equal=!g_ascii_strcasecmp(g_uri_get_scheme(left),g_uri_get_scheme(right))&&
        ((!lh&&!rh)||(lh&&rh&&!g_ascii_strcasecmp(lh,rh)))&&
        g_uri_get_port(left)==g_uri_get_port(right)&&
        !g_strcmp0(g_uri_get_path(left),g_uri_get_path(right))&&
        !g_strcmp0(g_uri_get_query(left),g_uri_get_query(right));
    g_uri_unref(left);g_uri_unref(right);return equal;
}
int cb_url_input(const char *input,const char *search,char *out,size_t cap){
    if(!input||!out||!cap||!clean_url(input))return -1;
    char *t=g_strdup(input);g_strstrip(t);if(!*t){g_free(t);return -1;}
    for(char *p=t;*p;p++)if((unsigned char)*p<32||(unsigned char)*p==127){g_free(t);return -1;}
    char *url=NULL;
    if(!strcmp(t,"cube:start"))url=g_strdup("cube:home");
    else if(!strcmp(t,"cube:bookmarks")||!strcmp(t,"cube:home")||!strcmp(t,"cube:history")||!strcmp(t,"cube:about"))url=g_strdup(t);
    else if(cb_url_remote(t)||g_str_has_prefix(t,"file:///"))url=g_strdup(t);
    else if(*t=='/')url=g_filename_to_uri(t,NULL,NULL);
    else if(!strchr(t,' ')&&(strchr(t,'.')||g_str_has_prefix(t,"localhost")||g_str_has_prefix(t,"127."))&&!strstr(t,"://"))
        url=g_strdup_printf("%s://%s",g_str_has_prefix(t,"localhost")||g_str_has_prefix(t,"127.")?"http":"https",t);
    else if(strchr(t,':')&&!strchr(t,' ')){g_free(t);return -1;}
    else {char *q=g_uri_escape_string(t,NULL,FALSE);const char *marker=search?strstr(search,"%s"):NULL;
        if(marker)url=g_strdup_printf("%.*s%s%s",(int)(marker-search),search,q,marker+2);
        g_free(q);}
    g_free(t);if(!url)return -1;
    if(cb_url_remote(url)){
        GError *error=NULL;GUri *u=g_uri_parse(url,G_URI_FLAGS_ENCODED,&error);
        if(!valid_http(url,u)){if(u)g_uri_unref(u);g_clear_error(&error);g_free(url);return -1;}
        char *normalized=g_uri_to_string(u);g_free(url);url=normalized;
        g_uri_unref(u);
    }
    size_t len=strlen(url);if(len>=cap){g_free(url);return -1;}memcpy(out,url,len+1);g_free(url);return 0;
}
char *cb_html_escape(const char *text){char *g=g_markup_escape_text(text?text:"",-1);char *r=strdup(g);g_free(g);return r;}
