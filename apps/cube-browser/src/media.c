#define _POSIX_C_SOURCE 200809L
#include "media.h"
#include <glib.h>
#include <math.h>
#include <stddef.h>
#include <string.h>

/* CSS Media Queries 4 sections 2–4 and Media Queries 5 preferences.
 * https://www.w3.org/TR/mediaqueries-4/#evaluating
 * Preserve unknown through negation, rather than enabling unsupported syntax. */
enum truth { NO, YES, UNKNOWN };
struct parser { const char *at,*end; const struct cb_media_env *env; unsigned depth,terms; int bad; };
static enum truth invert(enum truth v){return v==UNKNOWN?UNKNOWN:v==NO?YES:NO;}
static enum truth both(enum truth a,enum truth b){return a==NO||b==NO?NO:a==UNKNOWN||b==UNKNOWN?UNKNOWN:YES;}
static enum truth either(enum truth a,enum truth b){return a==YES||b==YES?YES:a==UNKNOWN||b==UNKNOWN?UNKNOWN:NO;}
static void space(struct parser *p){while(p->at<p->end&&g_ascii_isspace(*p->at))p->at++;}
static int ident_char(char c){return g_ascii_isalnum(c)||c=='-'||c=='_';}
static int word(struct parser *p,const char *text){
    space(p);size_t n=strlen(text);
    if((size_t)(p->end-p->at)<n||g_ascii_strncasecmp(p->at,text,n)||
       (p->at+n<p->end&&(ident_char(p->at[n])||p->at[n]=='(')))return 0;
    p->at+=n;return 1;
}
static int identifier(struct parser *p,char *out,size_t size){
    space(p);const char *start=p->at;
    if(p->at==p->end||(!g_ascii_isalpha(*p->at)&&*p->at!='-'&&*p->at!='_'))return 0;
    while(p->at<p->end&&ident_char(*p->at))p->at++;
    size_t n=(size_t)(p->at-start);if(n>=size){p->bad=1;return 0;}
    for(size_t i=0;i<n;i++)out[i]=g_ascii_tolower(start[i]);
    out[n]=0;return 1;
}
static int dimension(const char *text,double *value){
    /* Reject non-CSS spellings accepted by strtod (NaN, infinity, hex). */
    const char *s=text;if(*s=='+'||*s=='-')s++;
    int digits=0;while(g_ascii_isdigit(*s)){s++;digits++;}
    if(*s=='.'){if(!g_ascii_isdigit(s[1]))return 0;s++;while(g_ascii_isdigit(*s)){s++;digits++;}}
    if(!digits)return 0;
    if(*s=='e'||*s=='E'){
        const char *exponent=s+1;if(*exponent=='+'||*exponent=='-')exponent++;
        if(g_ascii_isdigit(*exponent)){s=exponent+1;while(g_ascii_isdigit(*s))s++;}
    }
    char *end;double number=g_ascii_strtod(text,&end);if(end!=s||!isfinite(number))return 0;
    double unit=1;
    if(!g_ascii_strcasecmp(s,"em")||!g_ascii_strcasecmp(s,"rem"))unit=16;
    else if(g_ascii_strcasecmp(s,"px")&&(*s||number!=0))return 0;
    *value=number*unit;return isfinite(*value);
}
static enum truth feature(struct parser *p,const char *start,const char *end){
    if((size_t)(end-start)>256)return UNKNOWN;
    char content[257];memcpy(content,start,(size_t)(end-start));content[end-start]=0;
    char *name=g_strstrip(content),*value=strchr(name,':');
    if(value){*value++=0;value=g_strstrip(value);g_strstrip(name);if(!*value)return UNKNOWN;}
    for(char *c=name;*c;c++)*c=g_ascii_tolower(*c);
    const char *base=name;int compare=0;
    if(!strncmp(base,"min-",4)){base+=4;compare=1;}
    else if(!strncmp(base,"max-",4)){base+=4;compare=-1;}
    if(!strcmp(base,"width")||!strcmp(base,"height")){
        double actual=!strcmp(base,"width")?p->env->width:p->env->height;
        if(!value)return compare?UNKNOWN:actual>0?YES:NO;
        double wanted;if(!dimension(value,&wanted))return UNKNOWN;
        return (compare>0?actual>=wanted:compare<0?actual<=wanted:actual==wanted)?YES:NO;
    }
    if(compare)return UNKNOWN;
    if(!strcmp(name,"orientation")){
        if(!value)return YES;
        if(!g_ascii_strcasecmp(value,"portrait"))return p->env->height>=p->env->width?YES:NO;
        if(!g_ascii_strcasecmp(value,"landscape"))return p->env->width>p->env->height?YES:NO;
        return UNKNOWN;
    }
    if(!strcmp(name,"prefers-reduced-motion")||!strcmp(name,"prefers-contrast")){
        int enabled=!strcmp(name,"prefers-reduced-motion")?p->env->reduced_motion:p->env->strong_contrast;
        const char *positive=!strcmp(name,"prefers-reduced-motion")?"reduce":"more";
        if(!value)return enabled?YES:NO;
        if(!g_ascii_strcasecmp(value,positive))return enabled?YES:NO;
        if(!g_ascii_strcasecmp(value,"no-preference"))return enabled?NO:YES;
        if(!strcmp(name,"prefers-contrast")&&(!g_ascii_strcasecmp(value,"less")||!g_ascii_strcasecmp(value,"custom")))return NO;
        return UNKNOWN;
    }
    return UNKNOWN;
}
static enum truth condition(struct parser *p,int allow_or);
static enum truth term(struct parser *p){
    space(p);
    if(++p->terms>128||p->depth>=16||p->at==p->end||*p->at++!='('){p->bad=1;return UNKNOWN;}
    p->depth++;space(p);const char *start=p->at;
    struct parser look=*p;
    int nested=p->at<p->end&&(*p->at=='('||word(&look,"not"));
    enum truth result;
    if(nested){
        result=condition(p,1);space(p);
        if(p->at==p->end||*p->at++!=')')p->bad=1;
    }else{
        unsigned depth=0;char quote=0;
        while(p->at<p->end){
            char c=*p->at;
            if(quote){if(c=='\\'&&p->at+1<p->end)p->at++;else if(c==quote)quote=0;}
            else if(c=='\''||c=='"')quote=c;
            else if(c=='(')depth++;
            else if(c==')'){if(!depth)break;depth--;}
            p->at++;
        }
        if(p->at==p->end){p->bad=1;result=UNKNOWN;}
        else{result=feature(p,start,p->at);p->at++;}
    }
    p->depth--;return result;
}
static enum truth condition(struct parser *p,int allow_or){
    if(word(p,"not"))return invert(term(p));
    enum truth result=term(p);int operation=0;
    while(!p->bad){
        const char *saved=p->at;int next=word(p,"and")?1:word(p,"or")?2:0;
        if(!next){p->at=saved;break;}
        if((operation&&operation!=next)||(next==2&&!allow_or)){p->bad=1;break;}
        operation=next;enum truth rhs=term(p);
        result=operation==1?both(result,rhs):either(result,rhs);
    }
    return result;
}
static int query(const char *start,const char *end,const struct cb_media_env *env){
    struct parser p={.at=start,.end=end,.env=env};space(&p);if(p.at==p.end)return 0;
    enum truth result;struct parser look=p;
    if(*p.at=='('||(word(&look,"not")&&(space(&look),look.at<look.end&&*look.at=='('))){
        result=condition(&p,1);
    }else{
        int negate=word(&p,"not");if(!negate)word(&p,"only");
        char type[64];
        if(!identifier(&p,type,sizeof type)||!strcmp(type,"not")||!strcmp(type,"only")||!strcmp(type,"and")||!strcmp(type,"or"))return 0;
        result=!strcmp(type,"screen")||!strcmp(type,"all")?YES:NO;
        if(word(&p,"and"))result=both(result,condition(&p,0));
        if(negate)result=invert(result);
    }
    space(&p);return !p.bad&&p.at==p.end&&result==YES;
}
int cb_media_matches(const char *text,const struct cb_media_env *env){
    if(!env||!isfinite(env->width)||!isfinite(env->height)||env->width<0||env->height<0)return 0;
    if(!text)return 1;
    size_t length=strnlen(text,2049);if(length>2048)return 0;
    char copy[2049];memcpy(copy,text,length+1);
    for(char *s=copy;(s=strstr(s,"/*"));){
        char *end=strstr(s+2,"*/");if(!end)return 0;memset(s,' ',(size_t)(end+2-s));s=end+2;
    }
    const char *start=g_strstrip(copy);if(!*start)return 1;
    int depth=0,matched=0;unsigned count=0;char quote=0;
    for(const char *s=start;;s++){
        char c=*s;
        if(!c||(!depth&&!quote&&c==',')){
            if(++count>32)return 0;
            if(!depth&&!quote)matched|=query(start,s,env);
            start=s+1;if(!c)break;
        }else if(quote){if(c=='\\'&&s[1])s++;else if(c==quote)quote=0;}
        else if(c=='\''||c=='"')quote=c;
        else if(c=='('){if(++depth>16)return 0;}
        else if(c==')'){if(depth)depth--;}
    }
    return matched;
}
