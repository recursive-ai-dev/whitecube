#include "engine-internal.h"
#include "http.h"
#include "ipc.h"
#include <assert.h>
#include <sys/socket.h>
#include <unistd.h>

/* Reproducible mutation smoke test, not a coverage-guided fuzzing claim. */
static uint32_t state=0x09c0be42;
static uint32_t next(void){state^=state<<13;state^=state>>17;state^=state<<5;return state;}
static size_t mutate(char *out,const char *seed,size_t capacity){
    size_t n=strlen(seed);assert(n<capacity);memcpy(out,seed,n+1);
    for(unsigned i=0,changes=1+next()%12;i<changes;i++)out[next()%n]=(char)next();
    if(next()%4==0)n=next()%(n+1);
    out[n]=0;return n;
}
int main(void){
    static const char *html[]={
        "<style>:root{--a:var(--b,red)}body{margin:0}.item:nth-child(odd){color:var(--a)}.row{display:flex;flex-wrap:wrap;gap:4px}.item{flex:1 1 30px}</style><body><div class=row><p class=item>One<p class=item>Two</div><input value='café'><a href='/next'>Link</a>",
        "<body><table><caption>Grid</caption><tr><th colspan=2>Heading<tr><td rowspan=2>A<td>B<tr><td><input name=x></table><form method=post><input type=password><button>Submit</button></form>",
        "<style>@media (min-width:200px){body{--a:#2468ac}div{display:grid;grid-template-columns:repeat(3,1fr);overflow:hidden;max-height:30px}a{position:absolute;left:2px;top:3px}}</style><div><a href='/a'>One</a><p>Two<p>Three</div><script>inert < > &</script>"};
    const char *css="div{--a:var(--b,12px);padding:var(--a);width:100%;position:relative}div>p:first-child{height:1e999px;color:rgba(1,2,3,.4)}@media (max-width:640px){table{border-spacing:2px}}";
    const char *svg="<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16'><path fill='#2468ac' d='M0 0L16 0A8 8 0 0 1 16 16Z'/></svg>";
    cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,640,240);cairo_t *cr=cairo_create(surface);
    int sockets[2];assert(!socketpair(AF_UNIX,SOCK_SEQPACKET,0,sockets));
    struct cb_http_request *request=calloc(1,sizeof *request);assert(request);
    for(unsigned i=0;i<1000;i++){
        char bytes[2048],rules[2048];size_t size=mutate(bytes,html[i%3],sizeof bytes);
        struct cb_page *p=cb_page_parse(bytes,size,"https://example.org/path/page");assert(p);
        size=mutate(rules,css,sizeof rules);cb_page_add_css(p,rules,size);
        cb_page_set_environment(p,240,i%2,(i/2)%2);cb_page_layout(p,320+(int)(next()%321),.5+(next()%4)*.5);
        assert(isfinite(cb_page_height(p))&&cb_page_height(p)>=0&&cb_page_node_count(p)<=NODE_LIMIT);
        cb_page_paint(p,cr,0,240);char *text=cb_page_text(p);assert(text&&g_utf8_validate(text,-1,NULL));free(text);
        struct cb_interaction result;cb_page_interact(p,CB_INTERACT_NEXT,0,0,"",&result);
        cb_page_interact(p,CB_INTERACT_TEXT,0,0,"mutated 猫",&result);cb_page_layout(p,640,1);cb_page_free(p);
        mutate(bytes,"https://example.org:443/path/../a?x=1#fragment",sizeof bytes);
        struct cb_http_url url;char target[4096];if(!cb_http_parse(bytes,&url)){assert(!cb_http_format(&url,target));cb_same_origin(target,"https://example.org/");}
        cb_url_resolve("https://example.org/base/",bytes,target);
        mutate(rules,"default-src 'self';img-src https://*.example.org;script-src 'unsafe-inline';sandbox allow-scripts",sizeof rules);
        cb_policy_allows(rules,"https://example.org/",bytes,1+next()%6,next()%2);
        size=mutate(bytes,svg,sizeof bytes);char error[256];cairo_surface_t *image=cb_image_decode(bytes,size,"image/svg+xml",error,sizeof error);if(image)cairo_surface_destroy(image);
        memset(request,0,sizeof *request);request->version=1;strcpy(request->url,"https://example.org/");((unsigned char *)request)[next()%sizeof *request]=(unsigned char)next();cb_http_request_valid(request);
        unsigned char packet[64]={0,0,0,6,0x85,1,0,0,0,0x60};size=10;
        for(unsigned j=0;j<1+next()%4;j++)packet[next()%size]=(unsigned char)next();
        assert(send(sockets[0],packet,size,0)==(ssize_t)size);struct cb_message message;cb_recv(sockets[1],&message);assert(message.fd<0);
    }
    free(request);close(sockets[0]);close(sockets[1]);cairo_destroy(cr);cairo_surface_destroy(surface);
    puts("1000 deterministic HTML/CSS/layout/SVG/URL/policy/HTTP/IPC mutation cases passed (seed 09c0be42)");
}
