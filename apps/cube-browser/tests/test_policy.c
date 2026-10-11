#include "policy.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
int main(void){
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
