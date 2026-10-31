#include "engine-internal.h"
#include <assert.h>

static struct node *id(struct node *n,const char *value) {
    if(n->id&&!strcmp(n->id,value))return n;
    for(struct node *c=n->first;c;c=c->next){struct node *found=id(c,value);if(found)return found;}
    return NULL;
}
static struct cb_page *page(const char *html) {
    struct cb_page *p=cb_page_parse(html,strlen(html),"https://example.org/");
    assert(p);cb_page_layout(p,640,1);return p;
}
static void near(double a,double b){assert(fabs(a-b)<.1);}
static unsigned pixel(struct cb_page *p,int x,int y) {
    cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,640,480);
    cairo_t *cr=cairo_create(surface);cb_page_paint(p,cr,0,480);cairo_destroy(cr);cairo_surface_flush(surface);
    unsigned value=*(uint32_t *)(cairo_image_surface_get_data(surface)+y*cairo_image_surface_get_stride(surface)+x*4)&0xffffff;
    cairo_surface_destroy(surface);return value;
}
static void effective_zoom_limits(void) {
    struct cb_page *p=page("<body style='margin:0'><div id=probe style='width:10px;height:10px;background:#2f8bba'></div></body>");
    near(id(p->root,"probe")->w,10);near(id(p->root,"probe")->h,10);
    cb_page_layout(p,640,9);
    near(id(p->root,"probe")->w,90);near(id(p->root,"probe")->h,90);
    assert(pixel(p,80,80)==0x2f8bba);assert(pixel(p,95,80)==0xffffff);
    cb_page_layout(p,640,100);near(p->zoom,9);near(id(p->root,"probe")->w,90);
    cb_page_layout(p,640,0);near(p->zoom,.25);near(id(p->root,"probe")->w,2.5);
    cb_page_layout(p,640,NAN);near(p->zoom,1);near(id(p->root,"probe")->w,10);
    cb_page_layout(p,640,INFINITY);near(p->zoom,1);near(id(p->root,"probe")->w,10);
    cb_page_free(p);
}
static void variables_and_selectors(void) {
    struct cb_page *p=page("<html><style>:root{--a:var(--b);--b:#2468ac;--Case:40px}"
        "body{margin:0}.box{height:var(--Case);background:var(--a)}"
        "[data-kind='first item']:first-child{--b:red}"
        ".box + .box:not(.excluded){background:var(--missing,var(--a))}"
        "#cycle{--x:var(--y);--y:var(--x);background:var(--x,#80c090)}"
        "</style><body>\n<div id=first class=box data-kind='first item'></div>"
        "<div id=second class=box></div><div id=cycle class=box></div></body></html>");
    /* Inherited custom properties are resolved on their declaring element. */
    assert(pixel(p,100,20)==0x2468ac);assert(pixel(p,100,60)==0x2468ac);assert(pixel(p,100,100)==0x80c090);
    near(id(p->root,"second")->y,40);
    assert(cb_engine_selector_matches(id(p->root,"first"),"body > [data-kind='first item']:first-child"));
    assert(cb_engine_selector_matches(id(p->root,"second"),".box:nth-child(2)"));
    assert(!cb_engine_selector_matches(id(p->root,"first"),".box:not(.box)"));
    cb_page_free(p);
    p=page("<body style='margin:0;--a:#124578'><div id=a style='--a:#abcdef!important;--a:red;height:40px;background:var(--a)'></div>"
           "<div style='--a:initial;height:40px;background:var(--a,#6789ab)'></div></body>");
    assert(pixel(p,100,20)==0xabcdef);assert(pixel(p,100,60)==0x6789ab);cb_page_free(p);
}
static void flexible_rows(void) {
    struct cb_page *p=page("<body style='margin:0'><div style='display:flex;flex-wrap:wrap;width:300px;gap:10px'>"
        "<a id=a href=/a style='flex:0 0 140px;height:30px'>One</a>"
        "<a id=b href=/b style='flex:0 0 140px;height:30px'>Two</a>"
        "<a id=c href=/c style='flex:0 0 140px;height:30px'>Three</a></div></body>");
    near(id(p->root,"a")->w,140);near(id(p->root,"b")->x,150);near(id(p->root,"c")->y,40);
    assert(strstr(cb_page_link_at(p,5,45),"/c"));
    cb_page_layout(p,220,1);near(id(p->root,"b")->y,0); /* explicit 300px width retains its size */
    cb_page_free(p);
    p=page("<body style='margin:0'><div style='display:flex;width:300px'>"
        "<div id=a style='flex:1 1 50px;height:20px'></div><div id=b style='flex:3 1 50px;height:20px'></div></div></body>");
    near(id(p->root,"a")->w,100);near(id(p->root,"b")->w,200);cb_page_free(p);
    p=page("<body style='margin:0'><div style='display:flex;width:300px'>"
        "<div id=a style='flex:0 1 200px;min-width:180px;height:20px'></div>"
        "<div id=b style='flex:0 1 200px;height:20px'></div></div></body>");
    near(id(p->root,"a")->w,180);near(id(p->root,"b")->w,120);cb_page_free(p);
    p=page("<body style='margin:0'><div style='display:flex;flex-direction:column;height:200px;width:100px;gap:10px'>"
        "<div id=a style='flex:1 1 50px'></div><div id=b style='flex:1 1 50px'></div></div></body>");
    near(id(p->root,"a")->h,95);near(id(p->root,"b")->h,95);near(id(p->root,"b")->y,105);cb_page_free(p);
}
static void grid_and_position(void) {
    struct cb_page *p=page("<body style='margin:0'><div style='display:grid;grid-template-columns:repeat(2,1fr);width:300px;gap:10px'>"
        "<div id=a style='height:20px'></div><div id=b style='height:20px'></div><div id=c style='height:20px'></div></div></body>");
    near(id(p->root,"a")->w,145);near(id(p->root,"b")->x,155);near(id(p->root,"c")->y,30);cb_page_free(p);
    p=page("<body style='margin:0'><div style='position:relative;width:300px;height:100px'>"
        "<a id=a href=/positioned style='position:absolute;left:20px;top:30px;width:60px;height:20px;background:#abcdef'>Link</a>"
        "<div id=b style='position:relative;left:10px;top:5px;height:20px'></div></div><div id=c>After</div></body>");
    near(id(p->root,"a")->x,20);near(id(p->root,"a")->y,30);near(id(p->root,"b")->x,10);near(id(p->root,"b")->y,5);near(id(p->root,"c")->y,100);
    assert(pixel(p,75,45)==0xabcdef);assert(strstr(cb_page_link_at(p,25,35),"/positioned"));cb_page_free(p);
}
static void clipping_and_recovery(void) {
    struct cb_page *p=page("<body style='margin:0'><div style='height:20px;overflow:hidden'><div style='height:80px;background:red'>"
        "<a id=hidden href=/hidden style='display:block;margin-top:40px'>Hidden link</a></div></div>"
        "<a id=visible href=/visible style='display:block;margin-top:80px'>Visible link</a></body>");
    assert(pixel(p,500,10)==0xff0000);assert(pixel(p,500,70)==0xffffff);
    assert(!cb_page_link_at(p,5,45));assert(id(p->root,"hidden")->clipped_out);
    struct cb_interaction result;assert(!cb_page_interact(p,CB_INTERACT_NEXT,0,0,"",&result));
    assert(p->focus==id(p->root,"visible"));cb_page_free(p);
    p=page("<body><p id=p><b>First<div id=d>Second</div><ul><li id=a><em>One<li id=b>Two</ul></body>");
    assert(id(p->root,"d")->parent==id(p->root,"p")->parent);
    assert(id(p->root,"a")->parent==id(p->root,"b")->parent);cb_page_free(p);
}
int main(void){effective_zoom_limits();variables_and_selectors();flexible_rows();grid_and_position();clipping_and_recovery();puts("layout compatibility passed");}
