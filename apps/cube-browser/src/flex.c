#include "engine-internal.h"

/* Bounded row flex and equal-track grid. Intrinsic width uses shaped text;
 * min-content sizing, baseline alignment and reverse directions are deferred. */
struct flex_box {
    struct node *node;
    double size, minimum, maximum, grow, shrink, height;
    size_t first_item, last_item, first_hit, last_hit;
    int frozen;
};

void cb_engine_translate(struct cb_page *p,struct node *n,size_t first_item,size_t last_item,
                         size_t first_hit,size_t last_hit,double dx,double dy) {
    for(size_t i=first_item;i<last_item;i++){p->items[i].x+=dx;p->items[i].y+=dy;}
    for(size_t i=first_hit;i<last_hit;i++){p->hits[i].x+=dx;p->hits[i].y+=dy;}
    cb_engine_translate_nodes(n,dx,dy);
}
void cb_engine_translate_nodes(struct node *n,double dx,double dy) {
    if(n->laid_out){n->x+=dx;n->y+=dy;}
    for(struct node *c=n->first;c;c=c->next)cb_engine_translate_nodes(c,dx,dy);
}
static double intrinsic(struct cb_page *p,struct node *n,double width) {
    if(n->control){double w,h;cb_engine_control_size(p,n->control,width,&w,&h);return w;}
    if(cb_engine_node_image(p,n)){double w,h;cb_engine_image_size(p,n,width,&w,&h);return w;}
    char *text=cb_engine_node_text(n,0);
    PangoLayout *layout=pango_layout_new(p->context);
    PangoFontDescription *font=pango_font_description_new();
    pango_font_description_set_family(font,n->style.family==1?"monospace":n->style.family==2?"serif":"sans-serif");
    pango_font_description_set_absolute_size(font,n->style.font_size*p->zoom*PANGO_SCALE);
    pango_layout_set_font_description(layout,font);pango_layout_set_text(layout,text,-1);
    int w,h;pango_layout_get_pixel_size(layout,&w,&h);
    pango_font_description_free(font);g_object_unref(layout);g_free(text);
    return MAX(1,w);
}
static void distribute(struct flex_box *boxes,unsigned count,double available) {
    for(unsigned pass=0;pass<=count;pass++) {
        double total=0;for(unsigned i=0;i<count;i++)total+=boxes[i].size;
        double free=available-total;if(fabs(free)<.01)break;
        double weight=0;for(unsigned i=0;i<count;i++)if(!boxes[i].frozen)weight+=free>0?boxes[i].grow:boxes[i].shrink*boxes[i].size;
        if(weight<=0)break;
        double divisor=free>0?MAX(1,weight):weight;int froze=0;
        for(unsigned i=0;i<count;i++) {
            struct flex_box *b=&boxes[i];if(b->frozen)continue;
            double share=free*(free>0?b->grow:b->shrink*b->size)/divisor;
            double next=CLAMP(b->size+share,b->minimum,b->maximum);
            if(fabs(next-(b->size+share))>.01){b->frozen=1;froze=1;}
            b->size=next;
        }
        if(!froze)break;
    }
}
static void stretch_box(struct cb_page *p,struct flex_box *b,double extra) {
    if(!extra)return;
    struct node *n=b->node;
    for(size_t i=b->first_item;i<b->last_item;i++){
        struct item *item=&p->items[i];
        if(!item->layout&&!item->image&&!item->control&&item->owner==n&&fabs(item->y-n->y)<.1&&fabs(item->h-n->h)<.1)item->h+=extra;
    }
    n->h+=extra;
}
static double column_layout(struct cb_page *p,struct node *n,double x,double y,double width) {
    struct flex_box *boxes=g_new0(struct flex_box,512);unsigned count=0;
    double used=0,gap=n->style.row_gap*p->zoom;
    for(struct node *c=n->first;c;c=c->next){
        if(c->style.display==D_NONE||c->style.position==2||cb_engine_whitespace_node(c))continue;
        if(count==512){g_free(boxes);return -1;}
        struct flex_box *b=&boxes[count++];b->node=c;b->grow=c->style.grow;b->shrink=c->style.shrink;
        b->first_item=p->item_count;b->first_hit=p->hit_count;
        struct length saved=c->style.height;
        if(c->style.basis.kind!=L_AUTO)c->style.height=c->style.basis;
        b->height=b->size=cb_engine_layout_node(p,c,x,y,width);c->style.height=saved;
        b->last_item=p->item_count;b->last_hit=p->hit_count;
        b->minimum=MAX(0,cb_engine_length_value(c->style.min_height,width,c->style.font_size,p->zoom));
        b->maximum=c->style.max_height.kind==L_AUTO?32767:MAX(b->minimum,cb_engine_length_value(c->style.max_height,width,c->style.font_size,p->zoom));
        used+=b->height;
    }
    if(!count){g_free(boxes);return 0;}
    used+=gap*(count-1);double available=used;
    if(n->style.height.kind!=L_AUTO){
        available=cb_engine_length_value(n->style.height,width,n->style.font_size,p->zoom);
        if(!n->style.content_box)available-=cb_engine_length_value(n->style.padding[0],width,n->style.font_size,p->zoom)+cb_engine_length_value(n->style.padding[2],width,n->style.font_size,p->zoom)+2*n->style.border*p->zoom;
        distribute(boxes,count,MAX(0,available-gap*(count-1)));
    }
    used=gap*(count-1);for(unsigned i=0;i<count;i++)used+=boxes[i].size;
    double free=MAX(0,available-used),cursor=y;
    if(n->style.justify==1)cursor+=free/2;else if(n->style.justify==2)cursor+=free;
    else if(n->style.justify==3&&count>1)gap+=free/(count-1);
    else if(n->style.justify==4){gap+=free/count;cursor+=free/count/2;}
    else if(n->style.justify==5){gap+=free/(count+1);cursor+=free/(count+1);}
    for(unsigned i=0;i<count;i++){
        struct flex_box *b=&boxes[i];int align=b->node->style.align_self<0?n->style.align_items:b->node->style.align_self;
        double extra=MAX(0,width-b->node->w),dx=align==1?extra/2:align==2?extra:0;
        cb_engine_translate(p,b->node,b->first_item,b->last_item,b->first_hit,b->last_hit,dx,cursor-y);
        stretch_box(p,b,b->size-b->height);cursor+=b->size+(i+1<count?gap:0);
    }
    g_free(boxes);return MAX(used,available);
}
double cb_engine_layout_flex(struct cb_page *p,struct node *n,double x,double y,double width) {
    unsigned count=0;
    for(struct node *c=n->first;c;c=c->next)
        if(c->style.display!=D_NONE&&c->style.position!=2&&!cb_engine_whitespace_node(c))count++;
    if(count>512)return -1;
    if(!count)return 0;
    if(n->style.display==D_FLEX&&n->style.flex_column)return column_layout(p,n,x,y,width);
    struct flex_box *boxes=g_new0(struct flex_box,count);
    unsigned at=0;struct style *s=&n->style;
    unsigned columns=s->display==D_GRID?(unsigned)MAX(1,s->grid_columns):0;
    double gap=MIN(s->gap*p->zoom,width),row_gap=s->row_gap*p->zoom;
    double grid_width=columns?MAX(1,(width-gap*(columns-1))/columns):0;
    for(struct node *c=n->first;c;c=c->next){
        if(c->style.display==D_NONE||c->style.position==2||cb_engine_whitespace_node(c))continue;
        struct style *cs=&c->style;struct flex_box *b=&boxes[at++];b->node=c;b->grow=cs->grow;b->shrink=cs->shrink;
        double extra=2*cs->border*p->zoom;
        for(int side=1;side<4;side+=2)extra+=cb_engine_length_value(cs->padding[side],width,cs->font_size,p->zoom);
        double margins=cb_engine_length_value(cs->margin[1],width,cs->font_size,p->zoom)+cb_engine_length_value(cs->margin[3],width,cs->font_size,p->zoom);
        struct length basis=cs->basis.kind!=L_AUTO?cs->basis:cs->width;
        b->size=columns?grid_width:(basis.kind!=L_AUTO?cb_engine_length_value(basis,width,cs->font_size,p->zoom)+(cs->content_box?extra:0):intrinsic(p,c,width)+extra)+margins;
        b->minimum=cs->min_width.kind==L_AUTO?1:MAX(1,cb_engine_length_value(cs->min_width,width,cs->font_size,p->zoom)+margins);
        b->maximum=cs->max_width.kind==L_AUTO?32767:MAX(b->minimum,cb_engine_length_value(cs->max_width,width,cs->font_size,p->zoom)+margins);
        b->size=CLAMP(b->size,b->minimum,b->maximum);
    }
    double cursor=y;
    for(unsigned start=0;start<count;){
        unsigned end=start;double occupied=0;
        while(end<count){
            double next=boxes[end].size+(end>start?gap:0);
            if(end>start&&((columns&&end-start>=columns)||(!columns&&s->wrap&&occupied+next>width)))break;
            occupied+=next;end++;
        }
        unsigned amount=end-start;
        if(!columns)distribute(boxes+start,amount,MAX(1,width-gap*(amount-1)));
        occupied=gap*(amount-1);for(unsigned i=start;i<end;i++)occupied+=boxes[i].size;
        double free=MAX(0,width-occupied),offset=0,spacing=gap;
        if(!columns){
            if(s->justify==1)offset=free/2;else if(s->justify==2)offset=free;
            else if(s->justify==3&&amount>1)spacing+=free/(amount-1);
            else if(s->justify==4){spacing+=free/amount;offset=free/amount/2;}
            else if(s->justify==5){spacing+=free/(amount+1);offset=free/(amount+1);}
        }
        double column=x+offset,tallest=0;
        for(unsigned i=start;i<end;i++){
            struct flex_box *b=&boxes[i];struct style saved=b->node->style;
            b->first_item=p->item_count;b->first_hit=p->hit_count;
            b->node->style.width=(struct length){0,L_AUTO};b->node->style.content_box=0;
            b->height=cb_engine_layout_node(p,b->node,column,cursor,b->size);
            b->node->style=saved;b->last_item=p->item_count;b->last_hit=p->hit_count;
            tallest=MAX(tallest,b->height);column+=b->size+spacing;
        }
        if(start==0&&end==count&&s->height.kind!=L_AUTO){
            double definite=cb_engine_length_value(s->height,width,s->font_size,p->zoom);
            if(!s->content_box)definite-=cb_engine_length_value(s->padding[0],width,s->font_size,p->zoom)+cb_engine_length_value(s->padding[2],width,s->font_size,p->zoom)+2*s->border*p->zoom;
            tallest=MAX(tallest,definite);
        }
        for(unsigned i=start;i<end;i++){
            struct flex_box *b=&boxes[i];int align=b->node->style.align_self<0?s->align_items:b->node->style.align_self;
            double extra=MAX(0,tallest-b->height),dy=align==1?extra/2:align==2?extra:0;
            if(dy)cb_engine_translate(p,b->node,b->first_item,b->last_item,b->first_hit,b->last_hit,0,dy);
            if(align==0&&b->node->style.height.kind==L_AUTO)stretch_box(p,b,extra);
        }
        cursor+=tallest+(end<count?row_gap:0);start=end;
    }
    g_free(boxes);return cursor-y;
}
