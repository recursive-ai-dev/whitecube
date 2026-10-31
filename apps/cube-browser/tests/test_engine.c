#define _POSIX_C_SOURCE 200809L
#include "engine.h"
#include <assert.h>
#include <cairo.h>
#include <glib.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct cb_page *page(const char *html) {
    struct cb_page *p=cb_page_parse(html,strlen(html),"https://example.org/docs/page.html");
    assert(p);cb_page_layout(p,640,1);return p;
}
static uint32_t pixel(struct cb_page *p, int x, int y) {
    cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,640,480);
    cairo_t *cr=cairo_create(surface);cb_page_paint(p,cr,0,480);cairo_destroy(cr);
    cairo_surface_flush(surface);
    int stride=cairo_image_surface_get_stride(surface);
    uint32_t value=*(uint32_t *)(cairo_image_surface_get_data(surface)+stride*y+x*4);
    assert(cairo_surface_status(surface)==CAIRO_STATUS_SUCCESS);
    cairo_surface_destroy(surface);return value & 0xffffffu;
}
static void parser(void) {
    const char *html="<!doctype html><html><head><title>A &amp; B</title>"
        "<script>document.write('script secret')</script><style>.gone{display:none}</style></head>"
        "<body><p>Hello &lt;world&gt; &#x1f642; &amp;lt; &#0;</p>"
        "<!-- comment secret --><div hidden>hidden secret</div><p class=gone>css secret</p>"
        "<p>One<p>Two<br>Three<img src=x alt='An image'><input><p>After</body></html>";
    struct cb_page *p=page(html);assert(!strcmp(cb_page_title(p),"A & B"));
    char *text=cb_page_text(p);
    assert(strstr(text,"Hello <world>"));assert(strstr(text,"&lt;"));
    assert(strstr(text,"One\nTwo\nThree"));assert(strstr(text,"An image"));assert(strstr(text,"After"));
    assert(!strstr(text,"secret"));assert(!strstr(text,"A & B"));free(text);
    assert(cb_page_height(p)>50);cb_page_free(p);
    const char invalid[]="<p>valid\xff invalid &#xD800;";
    p=cb_page_parse(invalid,sizeof invalid-1,NULL);assert(p);cb_page_layout(p,32,1);
    text=cb_page_text(p);assert(strstr(text,"valid"));free(text);cb_page_free(p);
}
static void cascade(void) {
    struct cb_page *p=page("<style>div {background:red} #box{background:#123456} .tile {background:green}</style>"
        "<body style='margin:0'><div id=box class=tile style='min-height:80px'>Cascade</div></body>");
    assert(pixel(p,300,50)==0x123456);cb_page_free(p);
    p=page("<style>body > .tile {background:#124578 !important} .outer .tile{background:red}</style>"
        "<body class=outer style='margin:0'><div class=tile style='background:green;min-height:80px'>Priority</div></body>");
    assert(pixel(p,300,50)==0x124578);
    cb_page_add_css(p,"#absent{display:none}.tile{background:#abcdef !important}",strlen("#absent{display:none}.tile{background:#abcdef !important}"));
    cb_page_layout(p,640,1);
    /* The existing child selector is more specific than the later class. */
    assert(pixel(p,300,50)==0x124578);cb_page_free(p);
    p=page("<body style='margin:0'><div style='background:#abcdef; background:nonsense; min-height:80px'>Recovery</div></body>");
    assert(pixel(p,300,50)==0xabcdef);cb_page_free(p);
}
static void layout_and_links(void) {
    const char *html="<body style='margin:12px'><h1>Wrapping</h1><p>"
        "A paragraph with <strong>bold words</strong> and <a href='../next?q=1&amp;x=2'>"
        "a link which wraps across several lines on a narrow viewport</a>. "
        "Text after the link stays on the same paragraph.</p>"
        "<p><a href='javascript:alert(1)'>Inert script link</a></p>"
        "<p><a href='data:text/html,hi'>Inert data link</a></p>"
        "<p>Longwordabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz</p></body>";
    struct cb_page *p=page(html);double wide=cb_page_height(p);
    cb_page_layout(p,160,1);double narrow=cb_page_height(p);assert(narrow>wide);
    int hits=0,first_y=-1,last_y=-1;
    for(int y=0;y<(int)narrow;y++)for(int x=0;x<160;x++) {
        const char *url=cb_page_link_at(p,x,y);
        if(!url)continue;
        assert(!strcmp(url,"https://example.org/next?q=1&x=2"));hits++;
        if(first_y<0)first_y=y;
        last_y=y;
    }
    assert(hits>100);assert(last_y-first_y>30);assert(!cb_page_link_at(p,-1,0));
    double found=-1;
    assert(cb_page_find(p,"BOLD WORDS",0,&found));assert(found>0);
    assert(cb_page_find(p,"which wraps across several lines",0,&found));
    assert(!cb_page_find(p,"not present",0,&found));
    cb_page_layout(p,160,1.5);assert(cb_page_height(p)>narrow);
    cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,160,240);
    cairo_t *cr=cairo_create(surface);cb_page_paint(p,cr,30,240);assert(cairo_status(cr)==CAIRO_STATUS_SUCCESS);
    cairo_destroy(cr);cairo_surface_destroy(surface);cb_page_free(p);
}
static void flex(void) {
    struct cb_page *p=page("<style>.row{display:flex;gap:20px}.row > a{flex:1;display:block;padding:12px;background:#eee}</style>"
        "<body style='margin:0'><div class=row><a href='/one'>First card</a><a href='/two'>Second card</a></div></body>");
    int first=-1,second=-1;
    for(int y=0;y<100;y++)for(int x=0;x<640;x++) {
        const char *url=cb_page_link_at(p,x,y);if(!url)continue;
        if(strstr(url,"/one") && first<0)first=x;
        if(strstr(url,"/two") && second<0)second=x;
    }
    assert(first>=12 && first<300);assert(second>=330);cb_page_free(p);
}
static void limits(void) {
    size_t cap=160000;
    char *html=calloc(cap,1);assert(html);
    for(int i=0;i<3000;i++)strcat(html,"<div>");
    strcat(html,"too deep");
    for(int i=0;i<3000;i++)strcat(html,"</div>");
    strcat(html,"<p>Recovered</p>");
    struct cb_page *p=page(html);assert(cb_page_node_count(p)<200);
    char *text=cb_page_text(p);assert(strstr(text,"Recovered"));free(text);cb_page_free(p);
    html[0]=0;for(int i=0;i<20000;i++)strcat(html,"<br>");
    p=cb_page_parse(html,strlen(html),NULL);assert(cb_page_node_count(p)<=16000);cb_page_free(p);free(html);
    p=page("<p>Still works</p><style>@media(min-width:0){p{display:none}} p{color:#123456}</style>");
    assert(isfinite(cb_page_height(p)));cb_page_free(p);
}
static void resources(void) {
    struct cb_page *p=page("<link rel=stylesheet href='../first.css'><img src='../picture.png#first'>"
        "<link rel='STYLESHEET' href='../first.css#second'><img src='../picture.png'>"
        "<link rel=stylesheet href='javascript:bad'><img src='file:///secret'>"
        "<link rel='alternate stylesheet' href='/alternate.css'><link rel=stylesheet disabled href='/disabled.css'>"
        "<link rel=stylesheet media=print href='/print.css'><link rel=stylesheet media='(width:1px)' href='/query.css'>");
    assert(cb_page_resource_count(p)==3);
    int kind;const char *url;
    assert(cb_page_resource(p,0,&kind,&url)==0 && kind==CB_RESOURCE_CSS);
    assert(!strcmp(url,"https://example.org/first.css"));
    assert(cb_page_resource(p,1,&kind,&url)==0 && kind==CB_RESOURCE_IMAGE);
    assert(!strcmp(url,"https://example.org/picture.png"));
    assert(cb_page_resource(p,2,&kind,&url)==0 && kind==CB_RESOURCE_CSS);
    assert(!strcmp(url,"https://example.org/query.css"));
    assert(cb_page_resource(p,3,&kind,&url)==-1);
    assert(cb_page_set_resource(p,0,"p{}",3,"text/html")==-1);
    assert(cb_page_set_resource(p,0,"x",1024u*1024u+1,"text/css")==-1);
    assert(cb_page_set_resource(p,0,"a\0b",3,"text/css")==-1);
    cb_page_free(p);
    const char *html="<link rel=stylesheet href='https://example.org/a.css'><img src='https://example.org/a.png'>";
    p=cb_page_parse(html,strlen(html),"file:///local.html");assert(cb_page_resource_count(p)==0);cb_page_free(p);
    char many[8192]={0};
    for(int i=0;i<50;i++){char part[128];snprintf(part,sizeof part,"<img src='/image-%d.png'>",i);strcat(many,part);}
    p=page(many);assert(cb_page_resource_count(p)==32);cb_page_free(p);
}
static void external_cascade(void) {
    struct cb_page *p=page("<style>.tile{background:red}</style><link rel=stylesheet href='/first.css'>"
        "<style>.tile{background:green}</style><style media=print>.tile{background:red}</style><link rel=stylesheet href='/last.css'>"
        "<body style='margin:0'><div class=tile style='min-height:80px'>Order</div></body>");
    assert(cb_page_resource_count(p)==2);assert(pixel(p,300,50)==0x008000);
    const char *last=".tile{background:#123456}", *first=".tile{background:#abcdef}";
    assert(cb_page_set_resource(p,1,last,strlen(last),"text/css; charset=UTF-8")==0);
    cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x123456);
    assert(cb_page_set_resource(p,0,first,strlen(first),"text/css")==0);
    cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x123456);
    assert(cb_page_set_resource(p,1,first,strlen(first),"text/html")==-1);
    cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x123456);
    cb_page_free(p);
    p=page("<link rel=stylesheet href='/shared.css'><style>.tile{background:red}</style>"
        "<link rel=stylesheet href='/shared.css'><body style='margin:0'><div class=tile style='min-height:80px'>Repeated sheet</div></body>");
    assert(cb_page_resource_count(p)==1);
    const char *css="@import url('https://example.org/ignored.css'); .tile{background:#234567}";
    assert(cb_page_set_resource(p,0,css,strlen(css),"text/css")==0);
    cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x234567);
    assert(cb_page_resource_count(p)==1);
    cb_page_add_css(p,".tile{background:#765432}",strlen(".tile{background:#765432}"));
    cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x765432);
    cb_page_free(p);
}
struct png_bytes {unsigned char *data;size_t length;};
static cairo_status_t png_write(void *closure,const unsigned char *data,unsigned int length) {
    struct png_bytes *png=closure;
    unsigned char *next=realloc(png->data,png->length+length);
    if(!next)return CAIRO_STATUS_NO_MEMORY;
    png->data=next;memcpy(png->data+png->length,data,length);png->length+=length;
    return CAIRO_STATUS_SUCCESS;
}
static struct png_bytes image_fixture_size(int width,int height) {
    struct png_bytes result={0};
    cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,width,height);
    cairo_t *cr=cairo_create(surface);cairo_set_source_rgb(cr,0xd1/255.,0x3a/255.,0x2f/255.);cairo_paint(cr);cairo_destroy(cr);
    assert(cairo_surface_write_to_png_stream(surface,png_write,&result)==CAIRO_STATUS_SUCCESS);
    cairo_surface_destroy(surface);return result;
}
static void image_documents(void) {
    struct png_bytes png=image_fixture_size(40,20);
    struct cb_page *p=cb_page_from_image(png.data,png.length,"image/png","https://example.org/image.png");
    assert(p && cb_page_resource_count(p)==0);cb_page_layout(p,640,1);
    assert(pixel(p,20,10)==0xd13a2f);
    cb_page_set_images_enabled(p,0);cb_page_layout(p,640,1);assert(pixel(p,20,10)!=0xd13a2f);
    cb_page_free(p);
    assert(!cb_page_from_image("<script>bad</script>",20,"image/png","https://example.org/image.png"));
    char *encoded=g_base64_encode(png.data,png.length);
    char *html=g_strdup_printf("<body style='margin:0'><img src='data:image/png;base64,%s' width=80 height=40></body>",encoded);
    p=page(html);assert(cb_page_resource_count(p)==0);assert(pixel(p,20,10)==0xd13a2f);
    cb_page_set_policy(p,"img-src 'self'");cb_page_layout(p,640,1);assert(pixel(p,20,10)!=0xd13a2f);
    cb_page_set_policy(p,"img-src data:");cb_page_layout(p,640,1);assert(pixel(p,20,10)==0xd13a2f);
    cb_page_set_images_enabled(p,0);cb_page_layout(p,640,1);assert(pixel(p,20,10)!=0xd13a2f);
    cb_page_set_images_enabled(p,1);cb_page_layout(p,640,1);assert(pixel(p,20,10)==0xd13a2f);
    cb_page_free(p);g_free(encoded);g_free(html);free(png.data);
}
static void images(void) {
    struct png_bytes png=image_fixture_size(40,20);
    struct cb_page *p=page("<body style='margin:0'><a href='/destination' style='display:block'>"
        "<img src='/image.png' alt='Colored rectangle' width=80 style='display:block'></a></body>");
    assert(cb_page_set_resource(p,0,png.data,png.length,"image/png")==0);
    cb_page_layout(p,640,1);assert(fabs(cb_page_height(p)-40)<0.01);
    assert(pixel(p,20,10)==0xd13a2f);assert(pixel(p,79,39)==0xd13a2f);assert(pixel(p,90,10)==0xffffff);
    const char *link=cb_page_link_at(p,20,10);assert(link && !strcmp(link,"https://example.org/destination"));
    assert(cb_page_set_resource(p,0,png.data,png.length," Image/PNG; charset=binary")==0);
    assert(cb_page_set_resource(p,0,png.data,png.length,"text/html")==-1);
    assert(cb_page_set_resource(p,0,"bad",3,"image/png")==-1);
    cb_page_layout(p,640,1);assert(pixel(p,20,10)==0xd13a2f);
    char *text=cb_page_text(p);assert(strstr(text,"Colored rectangle"));free(text);cb_page_free(p);
    p=page("<body style='margin:0'><img src='/image.png' width=80 style='display:block;width:60px;max-width:100%'></body>");
    assert(cb_page_set_resource(p,0,png.data,png.length,"image/png")==0);
    cb_page_layout(p,640,1);assert(fabs(cb_page_height(p)-30)<0.01);
    cb_page_layout(p,30,1);assert(fabs(cb_page_height(p)-15)<0.01);cb_page_free(p);
    p=page("<body style='margin:0'><img src='/image.png' height=40 style='display:block'></body>");
    assert(cb_page_set_resource(p,0,png.data,png.length,"image/png")==0);
    cb_page_layout(p,640,1);assert(fabs(cb_page_height(p)-40)<0.01);assert(pixel(p,60,20)==0xd13a2f);cb_page_free(p);
    p=page("<body style='margin:0'><p style='margin:0'>Before <a href='/inline'><img src='/image.png' width=80></a> after.</p></body>");
    assert(cb_page_set_resource(p,0,png.data,png.length,"image/png")==0);
    cb_page_layout(p,640,1);
    int first_x=-1,first_y=-1;
    for(int y=0;y<100;y++)for(int x=0;x<300;x++)if(first_x<0 && cb_page_link_at(p,x,y)){first_x=x;first_y=y;}
    assert(first_x>0 && first_y>=0);assert(pixel(p,first_x+20,first_y+20)==0xd13a2f);
    double wide=cb_page_height(p);cb_page_layout(p,90,1);assert(cb_page_height(p)>wide);
    cb_page_free(p);free(png.data);
}

static void resource_budgets(void) {
    struct cb_page *p=page("<link rel=stylesheet href='/one.css'><link rel=stylesheet href='/two.css'>");
    char *css=malloc(1024u*1024u);assert(css);memset(css,' ',1024u*1024u);
    assert(cb_page_set_resource(p,0,css,1024u*1024u,"text/css")==0);
    assert(cb_page_set_resource(p,1,"p{}",3,"text/css")==-1);
    assert(cb_page_set_resource(p,0,"p{}",3,"text/css")==0);
    assert(cb_page_set_resource(p,1,"p{}",3,"text/css")==0);
    free(css);cb_page_free(p);
    struct png_bytes large=image_fixture_size(2000,2000),small=image_fixture_size(40,20);
    p=page("<img src='/1.png'><img src='/2.png'><img src='/3.png'><img src='/4.png'><img src='/5.png'>");
    for(size_t i=0;i<4;i++)assert(cb_page_set_resource(p,i,large.data,large.length,"image/png")==0);
    assert(cb_page_set_resource(p,4,large.data,large.length,"image/png")==-1);
    assert(cb_page_set_resource(p,0,large.data,large.length,"image/png")==0);
    assert(cb_page_set_resource(p,0,small.data,small.length,"image/png")==0);
    assert(cb_page_set_resource(p,4,large.data,large.length,"image/png")==0);
    cb_page_free(p);free(large.data);free(small.data);
}

static struct cb_interaction interact(struct cb_page *p,unsigned action,const char *text) {
    struct cb_interaction out;assert(cb_page_interact(p,action,0,0,text,&out)==0);return out;
}
static void form_editing(void) {
    struct cb_page *p=page("<body style='margin:0'><a href='/link'>A wrapped link with several words</a>"
        "<form action='/search?discard=1'><input disabled name=bad value=secret><input type=hidden name=token value=x>"
        "<input name=q value=initial><input readonly name=fixed value=locked><button name=go value=yes>Go</button></form></body>");
    cb_page_layout(p,180,1);
    struct cb_interaction out=interact(p,CB_INTERACT_NEXT,"");assert(out.focus==CB_FOCUS_LINK);
    out=interact(p,CB_INTERACT_NEXT,"");assert(out.focus==CB_FOCUS_EDIT);
    double field_y=out.y;
    interact(p,CB_INTERACT_KEY,"select-all");interact(p,CB_INTERACT_TEXT,"Aé猫Z");
    interact(p,CB_INTERACT_KEY,"left");interact(p,CB_INTERACT_KEY,"backspace");
    interact(p,CB_INTERACT_KEY,"home");interact(p,CB_INTERACT_KEY,"delete");
    interact(p,CB_INTERACT_KEY,"end");interact(p,CB_INTERACT_TEXT," &+");
    interact(p,CB_INTERACT_KEY,"select-all");out=interact(p,CB_INTERACT_KEY,"copy");
    assert(out.kind==CB_INTERACTION_COPY && !strcmp(out.text,"éZ &+"));
    out=interact(p,CB_INTERACT_KEY,"cut");assert(out.kind==CB_INTERACTION_COPY);
    interact(p,CB_INTERACT_TEXT,out.text);cb_page_layout(p,320,1);
    /* Paint the changed Unicode field before another full layout as well. */
    interact(p,CB_INTERACT_TEXT,"猫");(void)pixel(p,20,(int)field_y+5);
    interact(p,CB_INTERACT_KEY,"backspace");
    out=interact(p,CB_INTERACT_KEY,"enter");assert(out.kind==CB_INTERACTION_SUBMIT);
    assert(!strcmp(out.text,"https://example.org/search?token=x&q=%C3%A9Z+%26%2B&fixed=locked&go=yes"));
    out=interact(p,CB_INTERACT_NEXT,"");assert(out.focus==CB_FOCUS_EDIT);
    out=interact(p,CB_INTERACT_TEXT,"overwrite");assert(out.kind==CB_INTERACTION_NOTICE);
    interact(p,CB_INTERACT_KEY,"select-all");out=interact(p,CB_INTERACT_KEY,"copy");assert(!strcmp(out.text,"locked"));
    out=interact(p,CB_INTERACT_KEY,"cut");assert(out.kind==CB_INTERACTION_NOTICE);
    out=interact(p,CB_INTERACT_NEXT,"");assert(out.focus==CB_FOCUS_CONTROL);
    out=interact(p,CB_INTERACT_KEY,"enter");assert(out.kind==CB_INTERACTION_SUBMIT);
    assert(strstr(out.text,"fixed=locked"));cb_page_free(p);
}
static void form_controls(void) {
    struct cb_page *p=page("<form action='/choose'><input type=checkbox name=box>"
        "<input type=radio name=radio value=first checked><input type=radio name=radio value=second>"
        "<select name=choice><option value=one>One<option value=two selected>Two</select>"
        "<button type=reset>Reset</button><button name=go value=yes>Go</button></form>");
    interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_KEY,"space");
    interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_KEY,"space");
    interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_KEY,"up");
    interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_KEY,"enter"); /* Reset. */
    interact(p,CB_INTERACT_NEXT,"");struct cb_interaction out=interact(p,CB_INTERACT_KEY,"enter");
    assert(out.kind==CB_INTERACTION_SUBMIT);
    assert(!strcmp(out.text,"https://example.org/choose?radio=first&choice=two&go=yes"));cb_page_free(p);
    p=page("<form action='/multiline'><textarea name=t>&lt;b&gt;first</textarea><button>Send</button></form>");
    interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_KEY,"select-all");
    interact(p,CB_INTERACT_TEXT,"one\r\ntwo\tthree");interact(p,CB_INTERACT_KEY,"enter");interact(p,CB_INTERACT_TEXT,"猫");
    interact(p,CB_INTERACT_KEY,"up");interact(p,CB_INTERACT_KEY,"down");
    interact(p,CB_INTERACT_KEY,"select-all");out=interact(p,CB_INTERACT_KEY,"copy");assert(!strcmp(out.text,"one\ntwo\tthree\n猫"));
    interact(p,CB_INTERACT_NEXT,"");out=interact(p,CB_INTERACT_KEY,"enter");assert(out.kind==CB_INTERACTION_SUBMIT);
    assert(strstr(out.text,"one%0D%0Atwo%09three%0D%0A%E7%8C%AB"));cb_page_free(p);
    p=page("<form><input name=q maxlength=3><button>Go</button></form>");
    interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_TEXT,"Aé猫");out=interact(p,CB_INTERACT_TEXT,"Z");assert(out.kind==CB_INTERACTION_NOTICE);
    interact(p,CB_INTERACT_KEY,"select-all");out=interact(p,CB_INTERACT_KEY,"copy");assert(!strcmp(out.text,"Aé猫"));cb_page_free(p);
}
static void form_unsupported_and_anchors(void) {
    const char *variants[]={"<form method=put><input name=q><button>Go</button></form>",
        "<form><input type=password name=secret><button>Go</button></form>",
        "<form><input type=file name=upload><button>Go</button></form>"};
    for(size_t i=0;i<sizeof variants/sizeof variants[0];i++) {
        struct cb_page *p=page(variants[i]);interact(p,CB_INTERACT_NEXT,"");
        struct cb_interaction out=interact(p,CB_INTERACT_TEXT,"secret");
        if(i==2)assert(out.kind==CB_INTERACTION_NOTICE);
        interact(p,CB_INTERACT_NEXT,"");out=interact(p,CB_INTERACT_KEY,"enter");assert(out.kind==CB_INTERACTION_NOTICE);cb_page_free(p);
    }
    struct cb_page *p=page("<body style='margin:0'><div style='min-height:600px'>Above</div>"
        "<h2 id='café'>Target</h2><a name=old></a><span id=hidden hidden>Invisible</span><form><input name=q></form></body>");
    double y=-1;assert(cb_page_anchor(p,"caf%C3%A9",&y) && y>=600);
    assert(cb_page_anchor(p,"old",&y) && y>=600);assert(!cb_page_anchor(p,"hidden",&y));
    assert(!cb_page_anchor(p,"missing",&y));assert(cb_page_anchor(p,"",&y) && y==0);
    assert(cb_page_set_url(p,"https://example.org/docs/page.html#old")==0);
    assert(cb_page_set_url(p,"https://other.example/#old")==-1);
    interact(p,CB_INTERACT_NEXT,"");struct cb_interaction out=interact(p,CB_INTERACT_KEY,"enter");
    assert(out.kind==CB_INTERACTION_SUBMIT && !strcmp(out.text,"https://example.org/docs/page.html?q=#old"));
    out=interact(p,CB_INTERACT_BLUR,"");assert(out.focus==CB_FOCUS_NONE && out.y==-1);cb_page_free(p);
    p=page("<form><input name=q><fieldset disabled><input name=skip></fieldset><button>Go</button></form>");
    assert(interact(p,CB_INTERACT_NEXT,"").focus==CB_FOCUS_EDIT);
    assert(interact(p,CB_INTERACT_NEXT,"").focus==CB_FOCUS_CONTROL);cb_page_free(p);
}

static void form_submission_regressions(void) {
    struct cb_page *p=page("<input type=hidden name=token value='a&#9;b' form=f><form id=f action='/go'><button>Go</button></form>");
    interact(p,CB_INTERACT_NEXT,"");struct cb_interaction out=interact(p,CB_INTERACT_KEY,"enter");
    assert(out.kind==CB_INTERACTION_SUBMIT && !strcmp(out.text,"https://example.org/go?token=a%09b"));cb_page_free(p);
    p=page("<form id=f><button>Go</button></form><input type=password name=secret form=f>");
    interact(p,CB_INTERACT_NEXT,"");assert(interact(p,CB_INTERACT_KEY,"enter").kind==CB_INTERACTION_NOTICE);cb_page_free(p);
    p=page("<form><input name=q><button disabled name=default value=save>Save</button><button name=delete value=yes>Delete</button></form>");
    interact(p,CB_INTERACT_NEXT,"");assert(interact(p,CB_INTERACT_KEY,"enter").kind==CB_INTERACTION_NOTICE);cb_page_free(p);
    p=page("<form><input name=first><input name=second></form>");
    interact(p,CB_INTERACT_NEXT,"");assert(interact(p,CB_INTERACT_KEY,"enter").kind==CB_INTERACTION_NOTICE);cb_page_free(p);
    p=page("<form><select name=choice><option value=none selected disabled>Choose<option value=pay>Pay</select><button>Go</button></form>");
    interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_NEXT,"");out=interact(p,CB_INTERACT_KEY,"enter");
    assert(out.kind==CB_INTERACTION_SUBMIT && !strstr(out.text,"choice="));cb_page_free(p);
    p=page("<form><fieldset disabled><legend><input name=allowed value=yes></legend><input name=blocked value=no></fieldset><button>Go</button></form>");
    assert(interact(p,CB_INTERACT_NEXT,"").focus==CB_FOCUS_EDIT);interact(p,CB_INTERACT_NEXT,"");out=interact(p,CB_INTERACT_KEY,"enter");
    assert(out.kind==CB_INTERACTION_SUBMIT && strstr(out.text,"allowed=yes") && !strstr(out.text,"blocked"));cb_page_free(p);
    char *html=calloc(200000,1);assert(html);strcpy(html,"<form><select name=choice>");
    for(int i=0;i<65;i++)strcat(html,"<option value=x>X</option>");
    strcat(html,"</select><button>Go</button></form>");
    p=page(html);interact(p,CB_INTERACT_NEXT,"");interact(p,CB_INTERACT_NEXT,"");assert(interact(p,CB_INTERACT_KEY,"enter").kind==CB_INTERACTION_NOTICE);cb_page_free(p);
    strcpy(html,"<form><button>Go</button>");for(int i=0;i<17000;i++)strcat(html,"<br>");strcat(html,"<input type=hidden name=missing value=token></form>");
    p=page(html);interact(p,CB_INTERACT_NEXT,"");assert(interact(p,CB_INTERACT_KEY,"enter").kind==CB_INTERACTION_NOTICE);cb_page_free(p);
    strcpy(html,"<form><button>Go</button>");for(int i=0;i<140;i++)strcat(html,"<div>");strcat(html,"<input type=password name=secret>");
    p=page(html);interact(p,CB_INTERACT_NEXT,"");assert(interact(p,CB_INTERACT_KEY,"enter").kind==CB_INTERACTION_NOTICE);cb_page_free(p);
    strcpy(html,"<form><button>Go</button><input type=hidden name=token value='");size_t length=strlen(html);memset(html+length,'a',5000);html[length+5000]=0;strcat(html,"'></form>");
    p=page(html);interact(p,CB_INTERACT_NEXT,"");assert(interact(p,CB_INTERACT_KEY,"enter").kind==CB_INTERACTION_NOTICE);cb_page_free(p);
    strcpy(html,"<form><button>Go</button><input type=hidden name=token value='");
    for(int i=0;i<2000;i++)strcat(html,"&#97;");
    strcat(html,"'></form>");p=page(html);interact(p,CB_INTERACT_NEXT,"");
    assert(interact(p,CB_INTERACT_KEY,"enter").kind==CB_INTERACTION_NOTICE);cb_page_free(p);free(html);
    p=page("<div id=f></div><form id=f><button>Go</button></form><input form=f type=hidden name=excluded value=yes>");
    interact(p,CB_INTERACT_NEXT,"");out=interact(p,CB_INTERACT_KEY,"enter");
    assert(out.kind==CB_INTERACTION_SUBMIT && !strstr(out.text,"excluded"));cb_page_free(p);
    p=page("<form><input name=q maxlength=99999><button>Go</button></form>");interact(p,CB_INTERACT_NEXT,"");
    assert(interact(p,CB_INTERACT_TEXT,"accepted").kind==CB_INTERACTION_UPDATE);
    assert(cb_page_set_url(p,"https://EXAMPLE.org:443/docs/page.html#normalized")==0);cb_page_free(p);
}

static void responsive_css(void) {
    struct cb_page *p=page("<style>body{margin:0}.tile{background:#112233;min-height:80px}"
        "@media screen and (min-width:400px){.tile{background:#223344}"
        "@media (orientation:portrait){.tile{background:#334455}}}"
        "@media (max-height:500px){.tile{background:#445566}}"
        "@media (prefers-reduced-motion:reduce){.tile{background:#556677}}"
        "@media (prefers-contrast:more){.tile{background:#667788}}</style>"
        "<body><div class=tile>Responsive</div></body>");
    assert(pixel(p,300,50)==0x334455); /* Default 640 by 800 CSS pixels. */
    cb_page_layout(p,320,1);assert(pixel(p,200,50)==0x112233);
    cb_page_set_environment(p,480,0,0);cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x445566);
    cb_page_set_environment(p,1200,0,0);cb_page_layout(p,640,2);assert(pixel(p,300,50)==0x112233);
    cb_page_set_environment(p,800,1,0);cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x556677);
    cb_page_set_environment(p,800,1,1);cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x667788);
    cb_page_set_environment(p,800,0,0);cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x334455);cb_page_free(p);

    /* A repeated remote sheet occupies each original position even while one
     * occurrence is inactive; viewport changes never require a new catalog. */
    p=page("<style>body{margin:0}.tile{background:red;min-height:80px}</style>"
        "<link rel=stylesheet media='(min-width:700px)' href='/shared.css'>"
        "<style>.tile{background:#123456}</style>"
        "<link rel=stylesheet media='screen and (max-width:500px)' href='/shared.css'>"
        "<style media='(prefers-contrast:more)'>.tile{background:#765432}</style>"
        "<body><div class=tile>Slots</div></body>");
    assert(cb_page_resource_count(p)==1);const char *css=".tile{background:#abcdef}";
    assert(cb_page_set_resource(p,0,css,strlen(css),"text/css")==0);
    cb_page_layout(p,640,1);assert(pixel(p,300,50)==0x123456);
    cb_page_layout(p,800,1);assert(pixel(p,300,50)==0x123456);
    cb_page_layout(p,400,1);assert(pixel(p,300,50)==0xabcdef);
    cb_page_set_environment(p,800,0,1);cb_page_layout(p,400,1);assert(pixel(p,300,50)==0x765432);
    assert(cb_page_resource_count(p)==1);cb_page_free(p);

    p=page("<style>body{margin:0}.tile{min-height:80px;background:#13579b}"
        "@media {.tile{background:red}}"
        "@unknown \"}\" {.tile{background:red}@media all {.tile{background:red}}}"
        "@media (max-width:100px){.tile{background:red}}"
        "@media all {.tile{unsupported:\"} /* literal */ {\";background:#2468ac}}"
        ".tile{color:green}</style><body><div class=tile>Recovery</div></body>");
    assert(pixel(p,300,50)==0x2468ac);cb_page_free(p);

    p=page("<style>@media(max-width:400px){input{width:90px}}"
        "@media(prefers-contrast:more){input{display:none}}</style>"
        "<form action='/responsive'><input name=q value=before><button>Go</button></form>");
    assert(interact(p,CB_INTERACT_NEXT,"").focus==CB_FOCUS_EDIT);
    interact(p,CB_INTERACT_KEY,"select-all");interact(p,CB_INTERACT_TEXT,"typed 猫");
    cb_page_layout(p,320,1);interact(p,CB_INTERACT_KEY,"select-all");
    struct cb_interaction out=interact(p,CB_INTERACT_KEY,"copy");assert(!strcmp(out.text,"typed 猫"));
    cb_page_set_environment(p,480,0,1);cb_page_layout(p,320,1);
    out=interact(p,CB_INTERACT_KEY,"copy");assert(out.focus==CB_FOCUS_NONE);
    cb_page_set_environment(p,480,0,0);cb_page_layout(p,640,1);
    assert(interact(p,CB_INTERACT_NEXT,"").focus==CB_FOCUS_EDIT);
    out=interact(p,CB_INTERACT_KEY,"enter");assert(out.kind==CB_INTERACTION_SUBMIT);
    assert(!strcmp(out.text,"https://example.org/responsive?q=typed+%E7%8C%AB"));cb_page_free(p);
}

struct bounds {int left,top,right,bottom,count;};
static struct bounds color_bounds(struct cb_page *p,uint32_t color) {
    struct bounds b={640,480,-1,-1,0};
    cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,640,480);
    cairo_t *cr=cairo_create(surface);cb_page_paint(p,cr,0,480);cairo_destroy(cr);cairo_surface_flush(surface);
    unsigned char *pixels=cairo_image_surface_get_data(surface);int stride=cairo_image_surface_get_stride(surface);
    for(int y=0;y<480;y++)for(int x=0;x<640;x++)if((*(uint32_t *)(pixels+y*stride+x*4)&0xffffffu)==color) {
        if(x<b.left)b.left=x;
        if(y<b.top)b.top=y;
        if(x>b.right)b.right=x;
        if(y>b.bottom)b.bottom=y;
        b.count++;
    }
    cairo_surface_destroy(surface);assert(b.count>0);return b;
}
static void tables(void) {
    struct cb_page *p=page("<style>body{margin:0}table{border-spacing:4px}td,th{padding:8px;border:2px solid #000;min-height:60px}"
        ".a{background:#123456}.b{background:#234567}.c{background:#345678}.d{background:#456789}</style>"
        "<body><table><caption id=caption>Table caption</caption><thead><tr><th class=a id=a>Heading<th class=b id=b>Second</thead>"
        "<tbody id=body><tr><td class=c id=c><a href='/cell'>Cell destination</a><td class=d id=d>Other cell</tbody></table></body>");
    struct bounds a=color_bounds(p,0x123456),b=color_bounds(p,0x234567),c=color_bounds(p,0x345678),d=color_bounds(p,0x456789);
    assert(a.top==b.top && a.bottom==b.bottom && a.right<b.left);
    assert(c.top==d.top && c.bottom==d.bottom && c.right<d.left && c.top>a.bottom);
    double ay,by,cy,dy,caption,group;
    assert(cb_page_anchor(p,"a",&ay) && cb_page_anchor(p,"b",&by) && cb_page_anchor(p,"c",&cy) && cb_page_anchor(p,"d",&dy));
    assert(ay==by && cy==dy && cy>ay);
    assert(cb_page_anchor(p,"caption",&caption) && cb_page_anchor(p,"body",&group));assert(caption<ay && group==cy);
    int hit=0;
    for(int y=c.top;y<=c.bottom && !hit;y++)for(int x=c.left;x<=c.right && !hit;x++) {
        const char *url=cb_page_link_at(p,x,y);if(!url)continue;
        assert(!strcmp(url,"https://example.org/cell"));struct cb_interaction out;
        assert(cb_page_interact(p,CB_INTERACT_CLICK,x,y,"",&out)==0 && out.kind==CB_INTERACTION_NAVIGATE);hit=1;
    }
    assert(hit);assert(pixel(p,a.left-1,a.top+10)==0x000000);cb_page_free(p);

    p=page("<style>body{margin:0}table{border-collapse:collapse}td{padding:8px;min-height:50px}"
        ".wide{background:#123456}.tall{background:#234567}.right{background:#345678}.last{background:#456789}</style>"
        "<body><table><tbody><tr><td class=wide colspan=2>Wide</td></tr>"
        "<tr><td class=tall rowspan=0>Tall</td><td class=right>Right</td></tr>"
        "<tr><td class=last>Last</td></tr></tbody></table></body>");
    a=color_bounds(p,0x123456);b=color_bounds(p,0x234567);c=color_bounds(p,0x345678);d=color_bounds(p,0x456789);
    assert(a.left==b.left && a.right==c.right && a.right-a.left>b.right-b.left);
    assert(b.top==c.top && b.bottom==d.bottom && b.right<c.left && d.top>c.top);
    assert(fabs(cb_page_height(p)-150)<0.1);cb_page_free(p);

    p=page("<style>body{margin:0}td{padding:8px;background:#eee}"
        "@media(max-width:400px){table,tr,td,tbody{display:block}input{width:160px}}</style>"
        "<body><form action='/table'><table><tr><td id=label>Query</td><td><input name=q></td></tr>"
        "<tr><td>Submit</td><td><button name=go value=yes>Send</button></td></tr></table></form></body>");
    struct cb_interaction out=interact(p,CB_INTERACT_NEXT,"");assert(out.focus==CB_FOCUS_EDIT);double initial_y=out.y;
    interact(p,CB_INTERACT_TEXT,"inside 猫");cb_page_layout(p,320,1);
    interact(p,CB_INTERACT_KEY,"select-all");out=interact(p,CB_INTERACT_KEY,"copy");
    assert(out.focus==CB_FOCUS_EDIT && out.y>initial_y && !strcmp(out.text,"inside 猫"));
    cb_page_layout(p,640,1);out=interact(p,CB_INTERACT_KEY,"enter");
    assert(out.kind==CB_INTERACTION_SUBMIT && !strcmp(out.text,"https://example.org/table?q=inside+%E7%8C%AB&go=yes"));cb_page_free(p);

    /* Unsupported span size keeps content and interactive controls in flow. */
    p=page("<body><table><tr><td colspan=999 id=first>First</td><td id=second><a href='/fallback'>Kept</a></td></tr></table></body>");
    assert(cb_page_anchor(p,"first",&ay) && cb_page_anchor(p,"second",&by) && by>ay);
    assert(interact(p,CB_INTERACT_NEXT,"").focus==CB_FOCUS_LINK);out=interact(p,CB_INTERACT_KEY,"enter");
    assert(out.kind==CB_INTERACTION_NAVIGATE && strstr(out.text,"/fallback"));cb_page_free(p);
}

static void svg_resource(void) {
    struct cb_page *p=page("<body style='margin:0'><img src='/mark.svg' width=80 style='display:block'></body>");
    const char *svg="<svg xmlns='http://www.w3.org/2000/svg' width='40' height='20'><rect width='40' height='20' fill='#123456'/></svg>";
    assert(cb_page_set_resource(p,0,svg,strlen(svg),"Image/SVG+XML; charset=utf-8")==0);
    cb_page_layout(p,640,1);assert(fabs(cb_page_height(p)-40)<0.1);
    assert(pixel(p,20,10)==0x123456);cb_page_free(p);
}

int main(void) {
    parser();cascade();layout_and_links();flex();limits();resources();external_cascade();images();image_documents();resource_budgets();form_editing();form_controls();form_unsupported_and_anchors();form_submission_regressions();responsive_css();tables();svg_resource();
    puts("engine: parser, entities, cascade, wrapping, links, flex, search, bounds, resource order, images, forms, focus, anchors, responsive CSS and tables passed");return 0;
}
