#include "policy.h"
#include "http.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static void javascript_mimes(void){
    const char *types[]={"application/ecmascript","application/javascript","application/x-ecmascript",
        "application/x-javascript","text/ecmascript","text/javascript","text/javascript1.0",
        "text/javascript1.1","text/javascript1.2","text/javascript1.3","text/javascript1.4",
        "text/javascript1.5","text/jscript","text/livescript","text/x-ecmascript","text/x-javascript"};
    for(size_t i=0;i<sizeof types/sizeof *types;i++){
        assert(cb_javascript_mime(types[i],strlen(types[i])));
        char header[128];snprintf(header,sizeof header," %s ; charset=utf-8",types[i]);assert(cb_http_javascript_mime(header));
    }
    const char *invalid[]={"","text/plain","application/json","text/javascriptx","text/javascript garbage","application/x-javascriptx"};
    for(size_t i=0;i<sizeof invalid/sizeof *invalid;i++)assert(!cb_http_javascript_mime(invalid[i]));
    assert(cb_http_javascript_mime("TEXT/JAVASCRIPT; charset=utf-8"));
    assert(!cb_javascript_mime("text/javascript; charset=utf-8",strlen("text/javascript; charset=utf-8")));
    assert(!cb_http_javascript_mime(NULL));
}
int main(void){
    javascript_mimes();
    const char *doc="https://example.org/page";char origin[320];
    assert(!cb_origin("HTTPS://EXAMPLE.org:443/x#y",origin));assert(!strcmp(origin,"https://example.org:443"));
    assert(!cb_origin_header("HTTPS://EXAMPLE.org:443/x#y",origin));assert(!strcmp(origin,"https://example.org"));
    assert(!cb_origin_header("http://example.org:8123/x",origin));assert(!strcmp(origin,"http://example.org:8123"));
    assert(cb_same_origin(doc,"https://example.org:443/other"));
    assert(!cb_same_origin(doc,"https://example.org:444/"));
    assert(!cb_same_origin(doc,"http://example.org/"));
    assert(cb_origin("https://user:secret@example.org/",origin));
    assert(cb_origin("file:///tmp/page",origin));
    assert(cb_origin("http://example.org:0/",origin));
    assert(cb_origin("http://example.org\\evil/",origin));
    assert(cb_policy_allows("",doc,doc,CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("img-src *",doc,"http://example.org/p.png",CB_POLICY_IMAGE,0));
    const char *data="data:image/png;base64,aGVsbG8=";
    assert(cb_policy_allows("",doc,data,CB_POLICY_IMAGE,0));
    assert(cb_policy_allows("img-src data:",doc,data,CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("img-src *",doc,data,CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("img-src 'self'",doc,data,CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("default-src 'none'",doc,data,CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("img-src data:\nimg-src 'self'",doc,data,CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("script-src data:",doc,data,CB_POLICY_SCRIPT,0));
    assert(!cb_policy_allows("default-src 'none'",doc,doc,CB_POLICY_STYLE,0));
    assert(cb_policy_allows("default-src 'none'",doc,doc,CB_POLICY_FORM,0));
    assert(!cb_policy_allows("form-action 'none'",doc,doc,CB_POLICY_FORM,0));
    assert(cb_policy_allows("default-src 'none'; img-src 'self'",doc,doc,CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("img-src *\ndefault-src 'self'",doc,"https://foreign.org/",CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("img-src 'none'; img-src *",doc,doc,CB_POLICY_IMAGE,0));
    assert(cb_policy_allows("img-src https://*.example.org:*/assets/",doc,"https://a.example.org:444/assets/p.png",CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("img-src https://*.example.org/assets/",doc,"https://example.org/assets/p.png",CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("img-src https://cdn.example.org/assets/",doc,"https://cdn.example.org/secret",CB_POLICY_IMAGE,0));
    assert(!cb_policy_allows("script-src 'self'",doc,doc,CB_POLICY_SCRIPT,1));
    assert(cb_policy_allows("script-src 'unsafe-inline'",doc,doc,CB_POLICY_SCRIPT,1));
    assert(!cb_policy_allows("script-src 'unsafe-inline' 'nonce-secret'",doc,doc,CB_POLICY_SCRIPT,1));
    assert(!cb_policy_allows("script-src *;script-src-elem 'none'",doc,doc,CB_POLICY_SCRIPT,0));
    assert(!cb_policy_allows("script-src-elem 'none';script-src *",doc,doc,CB_POLICY_SCRIPT,0));
    assert(!cb_policy_allows("sandbox allow-forms;script-src *",doc,doc,CB_POLICY_SCRIPT,0));
    assert(cb_policy_allows("sandbox allow-forms allow-same-origin;form-action 'self'",doc,doc,CB_POLICY_FORM,0));
    assert(cb_policy_opaque("sandbox allow-scripts"));
    assert(!cb_policy_allows("sandbox allow-scripts",doc,doc,CB_POLICY_SCRIPT,1));
    assert(!cb_policy_allows("style-src 'none'; style-src-elem 'unsafe-inline'",doc,doc,CB_POLICY_STYLE_ATTR,1));
    assert(cb_policy_allows("style-src 'none'; style-src-attr 'unsafe-inline'",doc,doc,CB_POLICY_STYLE_ATTR,1));
    puts("origins, CSP source lists, intersection, inline and sandbox policy passed");
}
