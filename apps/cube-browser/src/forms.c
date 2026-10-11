#define _POSIX_C_SOURCE 200809L
#include "forms.h"
#include <glib.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

/* Semantics: HTML form-control-infrastructure#concept-submit-mutate-action,
 * #converting-an-entry-list-to-a-list-of-name-value-pairs, and the WHATWG URL
 * Standard #concept-urlencoded-serializer. This is UTF-8 GET/POST URL-encoded submission. */
#define URI_FLAGS (G_URI_FLAGS_ENCODED | G_URI_FLAGS_SCHEME_NORMALIZE)

static int fail(char *out,size_t out_size,char *error,size_t error_size,const char *message){
    if(out&&out_size)out[0]=0;
    if(error&&error_size)snprintf(error,error_size,"%s",message);
    return -1;
}
static bool clean_reference(const char *text){
    if(!text||strnlen(text,CB_FORM_MAX_URL+1u)>CB_FORM_MAX_URL||!g_utf8_validate(text,-1,NULL))return false;
    for(const unsigned char *p=(const unsigned char *)text;*p;p++)
        if(*p<32||*p==127||*p=='\\')return false;
    return true;
}
/* GLib accepts an empty port and normalizes it away. Reject that spelling and
 * port zero before resolution, including scheme-relative action attributes. */
static bool authority_valid(const char *text){
    const char *start=NULL;
    if(!g_ascii_strncasecmp(text,"http://",7))start=text+7;
    else if(!g_ascii_strncasecmp(text,"https://",8))start=text+8;
    else if(!strncmp(text,"//",2))start=text+2;
    if(!start)return true;
    const char *end=start+strcspn(start,"/?#");
    if(start==end||memchr(start,'@',(size_t)(end-start)))return false;
    const char *port=NULL;
    if(*start=='['){
        const char *close=memchr(start,']',(size_t)(end-start));
        if(!close||close==start+1)return false;
        if(close+1<end){if(close[1]!=':')return false;port=close+2;}
    }else{
        const char *colon=memchr(start,':',(size_t)(end-start));
        if(colon){if(colon==start)return false;port=colon+1;}
    }
    if(port){
        if(port==end)return false;
        unsigned value=0;
        for(const char *p=port;p<end;p++){
            if(*p<'0'||*p>'9')return false;
            value=value*10u+(unsigned)(*p-'0');
            if(value>65535u)return false;
        }
        if(!value)return false;
    }
    return true;
}
static bool host_valid(const char *host){
    if(!host||!*host)return false;
    if(g_hostname_is_ip_address(host))return true;
    if(strlen(host)>253)return false;
    size_t label=0;
    for(const unsigned char *p=(const unsigned char *)host;*p;p++){
        if(*p=='.'){if(!label)return false;label=0;continue;}
        if(!g_ascii_isalnum(*p)&&*p!='-')return false;
        if(++label>63)return false;
    }
    return true;
}
static GUri *parse_http(const char *text){
    if(!clean_reference(text)||!authority_valid(text))return NULL;
    GUri *uri=g_uri_parse(text,URI_FLAGS,NULL);
    if(!uri)return NULL;
    const char *scheme=g_uri_get_scheme(uri);
    if(!scheme||(g_ascii_strcasecmp(scheme,"http")&&g_ascii_strcasecmp(scheme,"https"))||
       !host_valid(g_uri_get_host(uri))||g_uri_get_userinfo(uri)||g_uri_get_port(uri)==0){
        g_uri_unref(uri);return NULL;
    }
    return uri;
}
static int append(char query[16385],size_t *length,const char *bytes,size_t count){
    if(count>16384u-*length)return -1;
    memcpy(query+*length,bytes,count);*length+=count;query[*length]=0;return 0;
}
static int encode_field(char query[16385],size_t *length,const char *text){
    static const char hex[]="0123456789ABCDEF";
    for(const unsigned char *p=(const unsigned char *)text;*p;p++){
        if(*p=='\r'||*p=='\n'){
            if(*p=='\r'&&p[1]=='\n')p++;
            if(append(query,length,"%0D%0A",6))return -1;
        }else if(*p==' '){if(append(query,length,"+",1))return -1;}
        else if(g_ascii_isalnum(*p)||*p=='*'||*p=='-'||*p=='.'||*p=='_'){
            if(append(query,length,(const char *)p,1))return -1;
        }else{
            char encoded[]={'%',hex[*p>>4],hex[*p&15]};
            if(append(query,length,encoded,sizeof encoded))return -1;
        }
    }
    return 0;
}
static int form_build(int post,char *body,size_t body_size,const char *document_url,const char *action,
                    const struct cb_form_pair *pairs,size_t count,
                    char *out,size_t out_size,char *error,size_t error_size){
    if(error&&error_size)error[0]=0;
    if(!out||!out_size)return fail(out,out_size,error,error_size,"Missing form URL output buffer");
    if(count>CB_FORM_MAX_PAIRS||(!pairs&&count))return fail(out,out_size,error,error_size,"Form exceeds the 128-field limit");
    GUri *document=parse_http(document_url);
    if(!document)return fail(out,out_size,error,error_size,"Forms require a valid HTTP(S) document URL without credentials");
    if(action&&(!clean_reference(action)||!authority_valid(action))){
        g_uri_unref(document);
        return fail(out,out_size,error,error_size,"Invalid form action URL, controls, credentials or port");
    }
    char *reference=g_strdup(action?action:"");g_strstrip(reference);
    if(!authority_valid(reference)){
        g_free(reference);g_uri_unref(document);
        return fail(out,out_size,error,error_size,"Invalid form action credentials or port");
    }
    /* Validate an explicit authority before GLib serializes it. Otherwise an
     * escaped slash in a hostname could become an ordinary path separator. */
    if(g_uri_peek_scheme(reference)||!strncmp(reference,"//",2)){
        char *absolute=!strncmp(reference,"//",2)?g_strdup_printf("%s:%s",g_uri_get_scheme(document),reference):g_strdup(reference);
        GUri *check=parse_http(absolute);g_free(absolute);
        if(!check){g_free(reference);g_uri_unref(document);return fail(out,out_size,error,error_size,"Form action must be a valid HTTP(S) URL without credentials");}
        g_uri_unref(check);
    }
    /* Empty action is the document URL, including its fragment. Generic URL
     * resolution of an empty reference would discard that fragment. */
    char *resolved=*reference?g_uri_resolve_relative(document_url,reference,URI_FLAGS,NULL):g_strdup(document_url);
    g_free(reference);
    GUri *target=resolved?parse_http(resolved):NULL;g_free(resolved);
    if(!target){g_uri_unref(document);return fail(out,out_size,error,error_size,"Form action must be a valid HTTP(S) URL without credentials");}
    bool downgrade=!g_ascii_strcasecmp(g_uri_get_scheme(document),"https")&&
                   g_ascii_strcasecmp(g_uri_get_scheme(target),"https");
    g_uri_unref(document);
    if(downgrade){g_uri_unref(target);return fail(out,out_size,error,error_size,"HTTPS pages cannot submit forms over HTTP");}
    char query[16385]="";size_t length=0;
    const char *problem=NULL;
    for(size_t i=0;i<count;i++){
        if(!pairs[i].name||!*pairs[i].name||!pairs[i].value){problem="Form fields need a nonempty name and a value";break;}
        size_t name_length=strnlen(pairs[i].name,CB_FORM_MAX_NAME+1u);
        size_t value_length=strnlen(pairs[i].value,CB_FORM_MAX_VALUE+1u);
        if(name_length>CB_FORM_MAX_NAME||value_length>CB_FORM_MAX_VALUE){problem="Form names are limited to 1024 bytes and values to 4096 bytes";break;}
        if(!g_utf8_validate(pairs[i].name,(gssize)name_length,NULL)||
           !g_utf8_validate(pairs[i].value,(gssize)value_length,NULL)){problem="Form fields must contain valid UTF-8";break;}
        if((i&&append(query,&length,"&",1))||encode_field(query,&length,pairs[i].name)||
           append(query,&length,"=",1)||encode_field(query,&length,pairs[i].value)){
            problem="Encoded form data exceeds the 16384-byte limit";break;
        }
    }
    if(problem){g_uri_unref(target);return fail(out,out_size,error,error_size,problem);}
    char *host=g_ascii_strdown(g_uri_get_host(target),-1);
    char *scheme=g_ascii_strdown(g_uri_get_scheme(target),-1);
    GUri *result=g_uri_build(URI_FLAGS,scheme,NULL,host,g_uri_get_port(target),
                            g_uri_get_path(target),post?g_uri_get_query(target):query,g_uri_get_fragment(target));
    g_free(host);g_free(scheme);g_uri_unref(target);
    char *serialized=g_uri_to_string(result);g_uri_unref(result);
    size_t size=strlen(serialized);
    if(size>CB_FORM_MAX_URL||size>=out_size){g_free(serialized);return fail(out,out_size,error,error_size,"Encoded form URL exceeds the output size or 4095-byte limit");}
    if(post){if(length>=body_size){g_free(serialized);return fail(out,out_size,error,error_size,"Encoded POST body exceeds the output size");}memcpy(body,query,length+1);}
    memcpy(out,serialized,size+1);g_free(serialized);return 0;
}

int cb_form_get_url(const char *document_url,const char *action,const struct cb_form_pair *pairs,size_t count,char *out,size_t out_size,char *error,size_t error_size){
    return form_build(0,NULL,0,document_url,action,pairs,count,out,out_size,error,error_size);
}
int cb_form_post(const char *document_url,const char *action,const struct cb_form_pair *pairs,size_t count,char *out,size_t out_size,char *body,size_t body_size,char *error,size_t error_size){
    return form_build(1,body,body_size,document_url,action,pairs,count,out,out_size,error,error_size);
}
