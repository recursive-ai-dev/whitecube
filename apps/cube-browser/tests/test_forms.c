#include "forms.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void expect(const char *document,const char *action,const struct cb_form_pair *pairs,size_t count,const char *expected){
    char result[4096],error[256];
    int status=cb_form_get_url(document,action,pairs,count,result,sizeof result,error,sizeof error);
    if(status||strcmp(result,expected))fprintf(stderr,"action=%s: got '%s' (%s), expected '%s'\n",action?action:"(null)",result,error,expected);
    assert(status==0&&!strcmp(result,expected)&&!error[0]);
}
static void reject(const char *document,const char *action,const struct cb_form_pair *pairs,size_t count){
    char result[4096]="unchanged",error[256]="";
    int status=cb_form_get_url(document,action,pairs,count,result,sizeof result,error,sizeof error);
    if(!status)fprintf(stderr,"Unexpected accepted form action '%s' from '%s': %s\n",action?action:"(null)",document?document:"(null)",result);
    assert(status<0);
    assert(!result[0]&&error[0]);
}
static void encoding(void){
    const char *base="https://example.org/a/page?old=discard#previous";
    struct cb_form_pair pairs[]={{"q","hello world"},{"q","second"},{"empty",""}};
    expect(base,"../search?ignored=1#results",pairs,3,"https://example.org/search?q=hello+world&q=second&empty=#results");
    expect(base,"",pairs,1,"https://example.org/a/page?q=hello+world#previous");
    expect(base,NULL,pairs,1,"https://example.org/a/page?q=hello+world#previous");
    expect(base,"  ",pairs,1,"https://example.org/a/page?q=hello+world#previous");
    expect(base,"#",pairs,1,"https://example.org/a/page?q=hello+world#");
    expect(base,"#new?still-fragment",pairs,1,"https://example.org/a/page?q=hello+world#new?still-fragment");
    expect(base,"?ignored=2",pairs,1,"https://example.org/a/page?q=hello+world");
    expect(base,"",NULL,0,"https://example.org/a/page?#previous");
    expect(base,"/blank#",NULL,0,"https://example.org/blank?#");
    expect(base,"//EXAMPLE.NET:443/find",pairs,1,"https://example.net/find?q=hello+world");
    expect("http://localhost:8080/page","./find",pairs,1,"http://localhost:8080/find?q=hello+world");
    expect("http://localhost/page","https://example.org/search",pairs,1,"https://example.org/search?q=hello+world");
    struct cb_form_pair punctuation={"q","azAZ09*.-_~!'()@+/&=%\t"};
    expect(base,"/",&punctuation,1,"https://example.org/?q=azAZ09*.-_%7E%21%27%28%29%40%2B%2F%26%3D%25%09");
    struct cb_form_pair unicode={"café","雪 🙂"};
    expect(base,"/",&unicode,1,"https://example.org/?caf%C3%A9=%E9%9B%AA+%F0%9F%99%82");
    expect(base,"https://BÜCHER.example/été#fin",&unicode,1,"https://xn--bcher-kva.example/%C3%A9t%C3%A9?caf%C3%A9=%E9%9B%AA+%F0%9F%99%82#fin");
    struct cb_form_pair lines={"a\nb","one\rtwo\nthree\r\nfour"};
    expect(base,"/",&lines,1,"https://example.org/?a%0D%0Ab=one%0D%0Atwo%0D%0Athree%0D%0Afour");
    struct cb_form_pair repeats={"a","\r\r\n\n"};
    expect(base,"/",&repeats,1,"https://example.org/?a=%0D%0A%0D%0A%0D%0A");
}
static void policy(void){
    const char *base="https://example.org/doc";
    struct cb_form_pair pair={"q","hello"};
    const char *bad[]={"file:///etc/passwd","data:text/html,x","javascript:void(0)","cube:home",
        "http://example.org/","http://localhost/","https://user:pass@example.org/","https://@example.org/",
        "https://example.org:0/","https://example.org:/","https://example.org:65536/","https://example.org:-1/",
        "https://example.org:999999999999999999/","//example.org:/"," //example.org:0/ ","https://example.org/a\r\nInjected",
        "https://example.org/a\\b","https://example.org/%zz","https://example.org%2Fevil/path","https://example.org%00/",
        "https:///missing-host","https://example..org/",NULL};
    for(size_t i=0;bad[i];i++)reject(base,bad[i],&pair,1);
    reject("file:///tmp/doc.html","https://example.org/",&pair,1);
    reject("https://user:pass@example.org/","/",&pair,1);
    reject("https://example.org:/","/",&pair,1);
    reject(NULL,"/",&pair,1);
    reject(base,"/",NULL,1);
    struct cb_form_pair empty={"","value"},null_name={NULL,"value"},null_value={"name",NULL};
    reject(base,"/",&empty,1);reject(base,"/",&null_name,1);reject(base,"/",&null_value,1);
    struct cb_form_pair bad_utf8={"name","\xff"};reject(base,"/",&bad_utf8,1);
    bad_utf8.name="\xed\xa0\x80";bad_utf8.value="value";reject(base,"/",&bad_utf8,1);
}
static void limits(void){
    struct cb_form_pair pairs[129];for(size_t i=0;i<129;i++)pairs[i]=(struct cb_form_pair){"q",""};
    char result[4096],error[256];
    assert(!cb_form_get_url("https://x/","",pairs,128,result,sizeof result,error,sizeof error));
    reject("https://x/","",pairs,129);
    char name[1026],value[4098];memset(name,'n',1024);name[1024]=0;
    pairs[0]=(struct cb_form_pair){name,""};
    assert(!cb_form_get_url("https://x/","",pairs,1,result,sizeof result,error,sizeof error));
    name[1024]='n';name[1025]=0;reject("https://x/","",pairs,1);
    memset(value,'v',sizeof value);value[4082]=0;pairs[0]=(struct cb_form_pair){"n",value};
    assert(!cb_form_get_url("https://x/","",pairs,1,result,sizeof result,error,sizeof error));
    assert(strlen(result)==4095);
    value[4082]='v';value[4083]=0;reject("https://x/","",pairs,1);
    value[4083]='v';value[4097]=0;reject("https://x/","",pairs,1);
    pairs[0]=(struct cb_form_pair){"n","ok"};char tiny[4]="old",small_error[4]="old";
    assert(cb_form_get_url("https://x/","",pairs,1,tiny,sizeof tiny,small_error,sizeof small_error)<0);
    assert(!tiny[0]&&small_error[3]==0);
    assert(cb_form_get_url("https://x/","",pairs,1,NULL,0,NULL,0)<0);
    char huge[4097];memset(huge,'x',sizeof huge-1);huge[sizeof huge-1]=0;
    reject("https://x/",huge,pairs,1);
    /* Percent expansion is bounded independently of the UTF-8 input length. */
    memset(value,'&',1400);value[1400]=0;pairs[0].value=value;reject("https://x/","",pairs,1);
}
int main(void){encoding();policy();limits();puts("GET forms: encoding, newline normalization, action policy, query/fragment and bounds passed");return 0;}
