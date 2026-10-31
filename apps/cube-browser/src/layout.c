#include "engine-internal.h"

cairo_surface_t *cb_engine_node_image(const struct cb_page *p, const struct node *n) {
    if(!p->images_enabled)return NULL;
    if(n->inline_image)return n->inline_image;
    return n->resource<p->resource_count?p->resources[n->resource].image:NULL;
}
void cb_engine_image_size(const struct cb_page *p, const struct node *n, double available,
                       double *width, double *height) {
    cairo_surface_t *image=cb_engine_node_image(p,n);
    double iw=image?cairo_image_surface_get_width(image):1;
    double ih=image?cairo_image_surface_get_height(image):1;
    const struct style *s=&n->style;
    double w=s->width.kind==L_AUTO?iw*p->zoom:MAX(0,cb_engine_length_value(s->width,available,s->font_size,p->zoom));
    double h=s->height.kind==L_AUTO?ih*p->zoom:MAX(0,cb_engine_length_value(s->height,available,s->font_size,p->zoom));
    if(s->width.kind!=L_AUTO && s->height.kind==L_AUTO)h=w*ih/iw;
    if(s->height.kind!=L_AUTO && s->width.kind==L_AUTO)w=h*iw/ih;
    double max_width=MAX(0,available);
    if(s->max_width.kind!=L_AUTO)max_width=MIN(max_width,MAX(0,cb_engine_length_value(s->max_width,available,s->font_size,p->zoom)));
    if(w>max_width) {
        if(s->height.kind==L_AUTO)h*=max_width/w;
        w=max_width;
    }
    *width=CLAMP(w,0,32767);*height=CLAMP(h,0,32767);
}

int cb_engine_editable(const struct control *c){return c && (c->kind==C_TEXT || c->kind==C_TEXTAREA || c->kind==C_PASSWORD);}
void cb_engine_control_size(const struct cb_page *p,const struct control *c,double available,double *w,double *h) {
    const struct style *s=&c->node->style;double z=p->zoom;
    double width=240,height=MAX(32,s->font_size*1.4+10);
    if(c->kind==C_CHECKBOX || c->kind==C_RADIO){width=22;height=22;}
    else if(c->kind==C_TEXTAREA){width=320;height=110;}
    else if(c->kind==C_SUBMIT || c->kind==C_RESET || c->kind==C_BUTTON)
        width=MAX(72,g_utf8_strlen(c->label,-1)*s->font_size*0.65+26);
    *w=CLAMP(s->width.kind==L_AUTO?width*z:cb_engine_length_value(s->width,available,s->font_size,z),1,MAX(1,available));
    if(s->max_width.kind!=L_AUTO)*w=MIN(*w,MAX(1,cb_engine_length_value(s->max_width,available,s->font_size,z)));
    *h=CLAMP(s->height.kind==L_AUTO?height*z:cb_engine_length_value(s->height,available,s->font_size,z),1,2048);
}
void cb_engine_node_box(struct node *n,double x,double y,double w,double h) {
    n->x=x;n->y=y;n->w=w;n->h=h;n->laid_out=1;
}
void cb_engine_inline_box(const struct node *origin,double x,double y,double w,double h) {
    for(struct node *n=(struct node *)origin;n;n=n->parent) {
        if(cb_engine_block_display(n->style.display))break;
        if(!n->laid_out)cb_engine_node_box(n,x,y,w,h);
        else {double right=MAX(n->x+n->w,x+w),bottom=MAX(n->y+n->h,y+h);n->x=MIN(n->x,x);n->y=MIN(n->y,y);n->w=right-n->x;n->h=bottom-n->y;}
    }
}
const struct node *cb_engine_link_node(const struct node *n) {
    while(n && !n->href)n=n->parent;
    return n;
}
void cb_engine_control_item(struct cb_page *p,struct control *c,double x,double y,double w,double h);

struct inline_flow cb_engine_flow_new(void) {
    return (struct inline_flow){g_string_new(NULL),g_array_new(FALSE,FALSE,sizeof(struct span))};
}
void cb_engine_flow_free(struct inline_flow *f) { g_string_free(f->text,TRUE);g_array_free(f->spans,TRUE); }
void cb_engine_flow_append(struct inline_flow *f, const char *text, const struct style *s, const char *url, const struct node *origin) {
    if(!text || f->text->len>=TEXT_LIMIT)return;
    guint begin=(guint)f->text->len;
    for(const char *c=text;*c && f->text->len<TEXT_LIMIT;c++) {
        if(!s->pre && g_ascii_isspace(*c)) {
            if(f->text->len && !g_ascii_isspace(f->text->str[f->text->len-1]))g_string_append_c(f->text,' ');
        } else g_string_append_c(f->text,*c);
    }
    /* Never hand Pango a truncated UTF-8 codepoint at the text budget. */
    const char *invalid=NULL;
    if(!g_utf8_validate(f->text->str+begin,f->text->len-begin,&invalid))g_string_truncate(f->text,(gsize)(invalid-f->text->str));
    if(f->text->len>begin) {
        struct span span={begin,(guint)f->text->len,*s,url,NULL,origin,NULL};
        g_array_append_val(f->spans,span);
    }
}
void cb_engine_collect_inline(struct cb_page *p, struct inline_flow *f, const struct node *n, const char *link) {
    if(n->style.display==D_NONE || n->style.position==2)return;
    if(n->href)link=n->href;
    if(n->control) {
        if(n->control->kind==C_HIDDEN)return;
        guint spans=f->spans->len;cb_engine_flow_append(f,"\357\277\274",&n->style,NULL,n);
        if(f->spans->len>spans)g_array_index(f->spans,struct span,f->spans->len-1).control=n->control;
        return;
    }
    if(n->text){cb_engine_flow_append(f,n->text,&n->style,link,n);return;}
    if(cb_engine_tag_is(n,"br")) {
        struct style pre=n->style;pre.pre=1;cb_engine_flow_append(f,"\n",&pre,link,n);return;
    }
    if(cb_engine_tag_is(n,"img")) {
        if(cb_engine_node_image(p,n)) {
            guint spans=f->spans->len;
            cb_engine_flow_append(f,"\357\277\274",&n->style,link,n);
            if(f->spans->len>spans)g_array_index(f->spans,struct span,f->spans->len-1).image=n;
            return;
        }
        char *label=g_strdup_printf("[Image: %s]",n->alt && *n->alt?n->alt:"image");
        cb_engine_flow_append(f,label,&n->style,link,n);g_free(label);return;
    }
    if(cb_engine_tag_is(n,"input") || cb_engine_tag_is(n,"select") || cb_engine_tag_is(n,"textarea")) {
        cb_engine_flow_append(f,"[Control limit]",&n->style,link,n);return;
    }
    if(!n->first && (n->id || (cb_engine_tag_is(n,"a") && n->name)))cb_engine_flow_append(f,"\342\200\213",&n->style,link,n);
    for(const struct node *child=n->first;child;child=child->next)cb_engine_collect_inline(p,f,child,link);
}
struct item *cb_engine_add_item(struct cb_page *p) {
    if(p->item_count>=ITEM_LIMIT)return NULL;
    p->items=g_realloc_n(p->items,p->item_count+1,sizeof *p->items);
    struct item *item=&p->items[p->item_count++];memset(item,0,sizeof *item);return item;
}
void cb_engine_prepare_control_layout(struct cb_page *p,struct item *item) {
    struct control *c=item->control;const struct node *n=c->node;
    if(item->layout){g_object_unref(item->layout);item->layout=NULL;}
    if(c->kind==C_CHECKBOX || c->kind==C_RADIO)return;
    const char *label=c->value;item->placeholder=0;
    if(c->kind==C_SELECT)label=c->selected>=0?c->options[c->selected].label:"";
    else if(!cb_engine_editable(c))label=c->label;
    else if(!*label && n->placeholder){label=n->placeholder;item->placeholder=1;}
    char mask[4096];
    if(c->kind==C_PASSWORD&&!item->placeholder){size_t count=(size_t)g_utf8_strlen(c->value,-1);memset(mask,'*',count);mask[count]=0;label=mask;}
    PangoLayout *layout=pango_layout_new(p->context);item->layout=layout;
    PangoFontDescription *font=pango_font_description_new();pango_font_description_set_family(font,"sans-serif");
    pango_font_description_set_absolute_size(font,n->style.font_size*p->zoom*PANGO_SCALE);
    pango_layout_set_font_description(layout,font);pango_font_description_free(font);
    pango_layout_set_text(layout,label,-1);
    double inner=MAX(1,item->w-12*p->zoom-(c->kind==C_SELECT?18*p->zoom:0));
    if(c->kind==C_TEXTAREA){pango_layout_set_width(layout,(int)(inner*PANGO_SCALE));pango_layout_set_wrap(layout,PANGO_WRAP_WORD_CHAR);}
    else {pango_layout_set_single_paragraph_mode(layout,TRUE);if(!cb_engine_editable(c)){pango_layout_set_width(layout,(int)(inner*PANGO_SCALE));pango_layout_set_ellipsize(layout,PANGO_ELLIPSIZE_END);}}
    int width;pango_layout_get_pixel_size(layout,&width,&item->text_height);
    item->caret=(PangoRectangle){0};
    if(cb_engine_editable(c) && !item->placeholder)pango_layout_get_cursor_pos(layout,c->kind==C_PASSWORD?(int)g_utf8_strlen(c->value,c->cursor):(int)c->cursor,&item->caret,NULL);
}

void cb_engine_control_item(struct cb_page *p,struct control *c,double x,double y,double w,double h) {
    struct item *item=cb_engine_add_item(p);if(!item)return;
    item->x=x;item->y=y;item->w=w;item->h=h;item->control=c;cb_engine_prepare_control_layout(p,item);
    cb_engine_node_box(c->node,x,y,w,h);
    if(p->hit_count<ITEM_LIMIT) {
        p->hits=g_realloc_n(p->hits,p->hit_count+1,sizeof *p->hits);
        p->hits[p->hit_count++]=(struct hit){x,y,w,h,NULL,c->node};
    }
}

void cb_engine_add_attr(PangoAttrList *attrs, PangoAttribute *a, guint start, guint end) {
    a->start_index=start;a->end_index=end;pango_attr_list_insert(attrs,a);
}
void cb_engine_add_hits(struct cb_page *p, PangoLayout *layout, struct inline_flow *f, double x, double y) {
    PangoLayoutIter *iter=pango_layout_get_iter(layout);
    guint span_cursor=0;
    do {
        PangoLayoutLine *line=pango_layout_iter_get_line_readonly(iter);
        PangoRectangle logical;pango_layout_iter_get_line_extents(iter,NULL,&logical);
        while(span_cursor<f->spans->len &&
              g_array_index(f->spans,struct span,span_cursor).end<=(guint)line->start_index)span_cursor++;
        for(guint i=span_cursor;i<f->spans->len && p->hit_count<ITEM_LIMIT;i++) {
            struct span *span=&g_array_index(f->spans,struct span,i);
            if(span->begin>=(guint)(line->start_index+line->length))break;
            int begin=MAX((int)span->begin,line->start_index);
            int end=MIN((int)span->end,line->start_index+line->length);
            if(end<=begin)continue;
            PangoRectangle pos;pango_layout_index_to_pos(layout,begin,&pos);
            cb_engine_inline_box(span->origin,x+pos.x/(double)PANGO_SCALE,y+logical.y/(double)PANGO_SCALE,MAX(0,pos.width)/(double)PANGO_SCALE,logical.height/(double)PANGO_SCALE);
            if(!span->url)continue;
            int *ranges=NULL, count=0;pango_layout_line_get_x_ranges(line,begin,end,&ranges,&count);
            for(int r=0;r<count && p->hit_count<ITEM_LIMIT;r++) {
                p->hits=g_realloc_n(p->hits,p->hit_count+1,sizeof *p->hits);
                double left=ranges[2*r]/(double)PANGO_SCALE;
                double right=ranges[2*r+1]/(double)PANGO_SCALE;
                p->hits[p->hit_count++]=(struct hit){x+left,y+logical.y/(double)PANGO_SCALE,
                    MAX(right-left,1),logical.height/(double)PANGO_SCALE,span->url,cb_engine_link_node(span->origin)};
            }
            g_free(ranges);
        }
    } while(p->hit_count<ITEM_LIMIT && pango_layout_iter_next_line(iter));
    pango_layout_iter_free(iter);
}
void cb_engine_add_inline_images(struct cb_page *p, PangoLayout *layout, const struct inline_flow *f,
                              double x, double y, double width) {
    PangoLayoutIter *iter=pango_layout_get_iter(layout);
    guint span_cursor=0;
    do {
        PangoLayoutLine *line=pango_layout_iter_get_line_readonly(iter);
        while(span_cursor<f->spans->len && g_array_index(f->spans,struct span,span_cursor).end<=(guint)line->start_index)span_cursor++;
        for(guint i=span_cursor;i<f->spans->len;i++) {
            const struct span *span=&g_array_index(f->spans,struct span,i);
            if(span->begin>=(guint)(line->start_index+line->length))break;
            if(!span->image && !span->control)continue;
            double w,h;
            if(span->control)cb_engine_control_size(p,span->control,width,&w,&h);
            else cb_engine_image_size(p,span->image,width,&w,&h);
            if(w<=0 || h<=0)continue;
            PangoRectangle position;pango_layout_index_to_pos(layout,(int)span->begin,&position);
            double item_x=x+position.x/(double)PANGO_SCALE;
            double item_y=y+pango_layout_iter_get_baseline(iter)/(double)PANGO_SCALE-h;
            if(span->control){cb_engine_control_item(p,span->control,item_x,item_y,w,h);continue;}
            struct item *item=cb_engine_add_item(p);if(!item)break;
            item->x=item_x;
            item->y=y+pango_layout_iter_get_baseline(iter)/(double)PANGO_SCALE-h;
            item->w=w;item->h=h;item->image=cairo_surface_reference(cb_engine_node_image(p,span->image));
        }
    }while(pango_layout_iter_next_line(iter));
    pango_layout_iter_free(iter);
}

double cb_engine_flush_flow(struct cb_page *p, struct inline_flow *f, double x, double y, double width,
                         const struct style *block) {
    if(!f->text->len)return 0;
    if(!block->pre)while(f->text->len && f->text->str[f->text->len-1]==' ')g_string_truncate(f->text,f->text->len-1);
    if(!f->text->len){g_array_set_size(f->spans,0);return 0;}
    struct item *item=cb_engine_add_item(p);if(!item){g_string_truncate(f->text,0);g_array_set_size(f->spans,0);return 0;}
    item->x=x;item->y=y;item->w=MAX(1,width);
    item->layout=pango_layout_new(p->context);
    pango_layout_set_text(item->layout,f->text->str,(int)f->text->len);
    pango_layout_set_width(item->layout,(int)(CLAMP(width,1,32767)*PANGO_SCALE));
    pango_layout_set_wrap(item->layout,PANGO_WRAP_WORD_CHAR);
    pango_layout_set_height(item->layout,65536*PANGO_SCALE);
    pango_layout_set_ellipsize(item->layout,PANGO_ELLIPSIZE_END);
    pango_layout_set_alignment(item->layout,block->align==1?PANGO_ALIGN_CENTER:block->align==2?PANGO_ALIGN_RIGHT:PANGO_ALIGN_LEFT);
    pango_layout_set_line_spacing(item->layout,(float)block->line_height);
    PangoAttrList *attrs=pango_attr_list_new();
    for(guint i=0;i<f->spans->len;i++) {
        struct span *span=&g_array_index(f->spans,struct span,i);
        guint begin=span->begin,end=MIN(span->end,(guint)f->text->len);
        if(end<=begin)continue;
        struct style *s=&span->style;
        if(span->image || span->control) {
            double image_width,image_height;
            if(span->control)cb_engine_control_size(p,span->control,width,&image_width,&image_height);
            else cb_engine_image_size(p,span->image,width,&image_width,&image_height);
            PangoRectangle rect={0,-(int)(image_height*PANGO_SCALE),(int)(image_width*PANGO_SCALE),(int)(image_height*PANGO_SCALE)};
            cb_engine_add_attr(attrs,pango_attr_shape_new(&rect,&rect),begin,end);
        }
        const char *family=s->family==1?"monospace":s->family==2?"serif":"sans-serif";
        cb_engine_add_attr(attrs,pango_attr_family_new(family),begin,end);
        cb_engine_add_attr(attrs,pango_attr_size_new_absolute((int)(s->font_size*p->zoom*PANGO_SCALE)),begin,end);
        cb_engine_add_attr(attrs,pango_attr_weight_new((PangoWeight)s->weight),begin,end);
        cb_engine_add_attr(attrs,pango_attr_style_new(s->italic?PANGO_STYLE_ITALIC:PANGO_STYLE_NORMAL),begin,end);
        cb_engine_add_attr(attrs,pango_attr_foreground_new((guint16)(s->color.r*65535),(guint16)(s->color.g*65535),(guint16)(s->color.b*65535)),begin,end);
        if(s->color.a<1)cb_engine_add_attr(attrs,pango_attr_foreground_alpha_new((guint16)(s->color.a*65535)),begin,end);
        if(s->underline)cb_engine_add_attr(attrs,pango_attr_underline_new(PANGO_UNDERLINE_SINGLE),begin,end);
        if(s->background.a>0) {
            cb_engine_add_attr(attrs,pango_attr_background_new((guint16)(s->background.r*65535),(guint16)(s->background.g*65535),(guint16)(s->background.b*65535)),begin,end);
            cb_engine_add_attr(attrs,pango_attr_background_alpha_new((guint16)(s->background.a*65535)),begin,end);
        }
    }
    pango_layout_set_attributes(item->layout,attrs);pango_attr_list_unref(attrs);
    int text_width,text_height;pango_layout_get_pixel_size(item->layout,&text_width,&text_height);
    (void)text_width;
    item->h=MAX(text_height,block->font_size*p->zoom);
    cb_engine_add_hits(p,item->layout,f,x,y);
    double height=item->h;
    cb_engine_add_inline_images(p,item->layout,f,x,y,width);
    g_string_truncate(f->text,0);g_array_set_size(f->spans,0);
    return height;
}
double cb_engine_layout_table(struct cb_page *p,struct node *table,double x,double y,double width);

static double layout_core(struct cb_page *p, struct node *n, double x, double y, double available) {
    struct style *s=&n->style;
    if(s->display==D_NONE || p->item_count>=ITEM_LIMIT)return 0;
    double m[4],pad[4];
    for(int i=0;i<4;i++) {
        m[i]=cb_engine_length_value(s->margin[i],available,s->font_size,p->zoom);
        pad[i]=MAX(0,cb_engine_length_value(s->padding[i],available,s->font_size,p->zoom));
    }
    double border=s->border*p->zoom;
    if(n->control) {
        double w,h;cb_engine_control_size(p,n->control,MAX(1,available-m[1]-m[3]),&w,&h);
        cb_engine_control_item(p,n->control,x+m[3],y+m[0],w,h);return MAX(0,m[0]+h+m[2]);
    }
    if(cb_engine_tag_is(n,"img") && cb_engine_node_image(p,n)) {
        double iw,ih;cb_engine_image_size(p,n,MAX(0,available-m[1]-m[3]-pad[1]-pad[3]-2*border),&iw,&ih);
        double outer_w=iw+pad[1]+pad[3]+2*border,outer_h=ih+pad[0]+pad[2]+2*border;
        double remaining=MAX(0,available-outer_w-m[1]-m[3]);
        if(s->margin[1].kind==L_AUTO && s->margin[3].kind==L_AUTO)m[3]=remaining/2;
        else if(s->margin[3].kind==L_AUTO)m[3]=remaining;
        x+=m[3];y+=m[0];
        if(s->background.a>0 || border>0) {
            struct item *box=cb_engine_add_item(p);
            if(box)*box=(struct item){.x=x,.y=y,.w=outer_w,.h=outer_h,.background=s->background,.border_color=s->border_color,.border=border};
        }
        if(iw>0 && ih>0) {
            struct item *image=cb_engine_add_item(p);
            if(image)*image=(struct item){.x=x+pad[3]+border,.y=y+pad[0]+border,.w=iw,.h=ih,.image=cairo_surface_reference(cb_engine_node_image(p,n))};
            const struct node *ancestor=n;while(ancestor && !ancestor->href)ancestor=ancestor->parent;
            if(ancestor && p->hit_count<ITEM_LIMIT) {
                p->hits=g_realloc_n(p->hits,p->hit_count+1,sizeof *p->hits);
                p->hits[p->hit_count++]=(struct hit){x,y,outer_w,outer_h,ancestor->href,ancestor};
            }
        }
        cb_engine_node_box(n,x,y,outer_w,outer_h);return MAX(0,m[0]+outer_h+m[2]);
    }
    double w=MAX(1,available-m[1]-m[3]);
    /* All M0 widths use a border-box constraint, including explicit widths. */
    if(s->width.kind!=L_AUTO)w=CLAMP(cb_engine_length_value(s->width,available,s->font_size,p->zoom)+(s->content_box?pad[1]+pad[3]+2*border:0),1,32767);
    if(s->min_width.kind!=L_AUTO)w=MAX(w,cb_engine_length_value(s->min_width,available,s->font_size,p->zoom));
    if(s->max_width.kind!=L_AUTO)w=MIN(w,MAX(1,cb_engine_length_value(s->max_width,available,s->font_size,p->zoom)));
    double remaining=MAX(0,available-w-m[1]-m[3]);
    if(s->margin[1].kind==L_AUTO && s->margin[3].kind==L_AUTO){m[1]=remaining/2;m[3]=remaining/2;}
    else if(s->margin[3].kind==L_AUTO)m[3]=remaining;
    x+=m[3];y+=m[0];
    size_t box_index=SIZE_MAX;
    if(s->background.a>0 || border>0) {
        struct item *box=cb_engine_add_item(p);
        if(box){box_index=p->item_count-1;*box=(struct item){.x=x,.y=y,.w=w,.background=s->background,.border_color=s->border_color,.border=border};}
    }
    double inner_x=x+pad[3]+border, inner_y=y+pad[0]+border;
    double inner_w=MAX(1,w-pad[1]-pad[3]-2*border);
    double cursor=inner_y;
    if(s->display==D_TABLE) {
        double table_height=cb_engine_layout_table(p,n,inner_x,inner_y,inner_w);
        if(table_height>=0) {
            double h=MAX(table_height+pad[0]+pad[2]+2*border,cb_engine_length_value(s->min_height,available,s->font_size,p->zoom));
            if(box_index!=SIZE_MAX)p->items[box_index].h=h;
            cb_engine_node_box(n,x,y,w,h);return MAX(0,m[0]+h+m[2]);
        }
    }
    struct inline_flow flow=cb_engine_flow_new();
    if(n->text || cb_engine_tag_is(n,"img") || cb_engine_tag_is(n,"input"))cb_engine_collect_inline(p,&flow,n,n->href);
    if(cb_engine_tag_is(n,"li"))cb_engine_flow_append(&flow,"• ",s,NULL,n);
    double flex_height=-1;
    if(s->display==D_GRID || s->display==D_FLEX)
        flex_height=cb_engine_layout_flex(p,n,inner_x,inner_y,inner_w);
    if(flex_height>=0)cursor+=flex_height;
    else {
        for(struct node *c=n->first;c;c=c->next) {
            if(c->style.display==D_NONE || c->style.position==2)continue;
            if(cb_engine_block_display(c->style.display)||s->display==D_FLEX) {
                cursor+=cb_engine_flush_flow(p,&flow,inner_x,cursor,inner_w,s);
                cursor+=cb_engine_layout_node(p,c,inner_x,cursor,inner_w);
                if(s->display==D_FLEX && s->flex_column && c->next)cursor+=s->row_gap*p->zoom;
            } else cb_engine_collect_inline(p,&flow,c,n->href);
        }
    }
    cursor+=cb_engine_flush_flow(p,&flow,inner_x,cursor,inner_w,s);
    cb_engine_flow_free(&flow);
    double h=MAX(cursor-inner_y+pad[0]+pad[2]+2*border,
                 cb_engine_length_value(s->min_height,available,s->font_size,p->zoom));
    if(s->height.kind!=L_AUTO)h=MAX(0,cb_engine_length_value(s->height,available,s->font_size,p->zoom)+(s->content_box?pad[0]+pad[2]+2*border:0));
    if(s->min_height.kind!=L_AUTO)h=MAX(h,cb_engine_length_value(s->min_height,available,s->font_size,p->zoom));
    if(s->max_height.kind!=L_AUTO)h=MIN(h,MAX(0,cb_engine_length_value(s->max_height,available,s->font_size,p->zoom)));
    h=MAX(h,0);
    if(box_index!=SIZE_MAX)p->items[box_index].h=h;
    cb_engine_node_box(n,x,y,w,h);
    return MAX(0,m[0]+h+m[2]);
}
double cb_engine_layout_node(struct cb_page *p,struct node *n,double x,double y,double available) {
    size_t items=p->item_count,hits=p->hit_count;
    double height=layout_core(p,n,x,y,available);
    for(size_t i=items;i<p->item_count;i++)if(!p->items[i].owner)p->items[i].owner=n;
    if(n->style.position==1){
        const struct style *s=&n->style;
        double dx=s->inset[3].kind!=L_AUTO?cb_engine_length_value(s->inset[3],available,s->font_size,p->zoom):-cb_engine_length_value(s->inset[1],available,s->font_size,p->zoom);
        double dy=s->inset[0].kind!=L_AUTO?cb_engine_length_value(s->inset[0],height,s->font_size,p->zoom):-cb_engine_length_value(s->inset[2],height,s->font_size,p->zoom);
        cb_engine_translate(p,n,items,p->item_count,hits,p->hit_count,dx,dy);
    }
    return height;
}
struct table_row {struct node *node,*group;double height,y;size_t box;};
struct table_cell {
    struct node *node;unsigned row,column,rows,columns;
    size_t first_item,last_item,first_hit,last_hit,box;
    double height;
};
struct table_grid {
    struct table_row rows[TABLE_ROW_LIMIT];
    struct table_cell cells[TABLE_CELL_LIMIT];
    uint16_t occupied[TABLE_ROW_LIMIT][TABLE_COLUMN_LIMIT];
    struct node *caption;
    unsigned row_count,column_count,cell_count;
    int invalid;
};
int cb_engine_whitespace_node(const struct node *n) {
    if(!n->text)return 0;
    for(const char *c=n->text;*c;c++)if(!g_ascii_isspace(*c))return 0;
    return 1;
}
void cb_engine_table_rows(struct table_grid *grid,struct node *parent,struct node *group) {
    for(struct node *n=parent->first;n && !grid->invalid;n=n->next) {
        if(n->style.display==D_NONE || cb_engine_whitespace_node(n))continue;
        if(cb_engine_tag_is(n,"tr") && n->style.display==D_ROW) {
            if(grid->row_count>=TABLE_ROW_LIMIT){grid->invalid=1;break;}
            grid->rows[grid->row_count++]=(struct table_row){.node=n,.group=group,.box=SIZE_MAX};
        } else if(cb_engine_in_words("thead tbody tfoot",n->tag) && n->style.display==D_ROWGROUP && !group)cb_engine_table_rows(grid,n,n);
        else if(cb_engine_tag_is(n,"caption") && n->style.display==D_CAPTION && !grid->caption && !group)grid->caption=n;
        else if(cb_engine_tag_is(n,"col"))continue;
        else if(cb_engine_tag_is(n,"colgroup")) {
            for(struct node *c=n->first;c;c=c->next)if(!cb_engine_tag_is(c,"col") && !cb_engine_whitespace_node(c) && c->style.display!=D_NONE){grid->invalid=1;break;}
        } else grid->invalid=1;
    }
}
void cb_engine_table_cells(struct table_grid *g) {
    for(unsigned row=0;row<g->row_count && !g->invalid;row++) {
        unsigned column=0,group_end=row+1;
        while(group_end<g->row_count && g->rows[group_end].group==g->rows[row].group)group_end++;
        for(struct node *n=g->rows[row].node->first;n;n=n->next) {
            if(n->style.display==D_NONE || cb_engine_whitespace_node(n))continue;
            if(!cb_engine_in_words("td th",n->tag) || n->style.display!=D_CELL || g->cell_count>=TABLE_CELL_LIMIT){g->invalid=1;break;}
            unsigned cols=n->colspan,rows=n->rowspan?n->rowspan:group_end-row;
            if(!cols || cols>TABLE_COLUMN_LIMIT || !rows || rows>TABLE_COLUMN_LIMIT){g->invalid=1;break;}
            rows=MIN(rows,group_end-row);
            while(column<TABLE_COLUMN_LIMIT) {
                int free=column+cols<=TABLE_COLUMN_LIMIT;
                for(unsigned c=column;free && c<column+cols;c++)if(g->occupied[row][c])free=0;
                if(free)break;
                column++;
            }
            if(column+cols>TABLE_COLUMN_LIMIT){g->invalid=1;break;}
            struct table_cell *cell=&g->cells[g->cell_count++];
            *cell=(struct table_cell){.node=n,.row=row,.column=column,.rows=rows,.columns=cols,.box=SIZE_MAX};
            for(unsigned r=row;r<row+rows;r++)for(unsigned c=column;c<column+cols;c++)g->occupied[r][c]=(uint16_t)g->cell_count;
            column+=cols;g->column_count=MAX(g->column_count,column);
        }
    }
}
void cb_engine_shift_node_geometry(struct node *n,double dy) {
    if(n->laid_out)n->y+=dy;
    for(struct node *child=n->first;child;child=child->next)cb_engine_shift_node_geometry(child,dy);
}
size_t cb_engine_table_background(struct cb_page *p,const struct node *n,double x,double y,double width) {
    if(n->style.background.a<=0 && n->style.border<=0)return SIZE_MAX;
    struct item *item=cb_engine_add_item(p);if(!item)return SIZE_MAX;
    *item=(struct item){.x=x,.y=y,.w=width,.background=n->style.background,
        .border=n->style.border*p->zoom,.border_color=n->style.border_color};
    return p->item_count-1;
}
double cb_engine_layout_table(struct cb_page *p,struct node *table,double x,double y,double width) {
    struct table_grid *g=g_new0(struct table_grid,1);cb_engine_table_rows(g,table,NULL);cb_engine_table_cells(g);
    if(g->invalid || (g->column_count && width<g->column_count*12*p->zoom)){g_free(g);return -1;}
    double cursor=y;
    if(g->caption)cursor+=cb_engine_layout_node(p,g->caption,x,cursor,width);
    if(!g->column_count){g_free(g);return cursor-y;}
    double gap=table->style.collapse_borders?0:table->style.table_spacing*p->zoom;
    gap=MIN(gap,MAX(0,(width-g->column_count)/(g->column_count+1)));
    double column_width=MAX(1,(width-gap*(g->column_count+1))/g->column_count),origin=cursor+gap;
    struct node *groups[TABLE_ROW_LIMIT];size_t group_boxes[TABLE_ROW_LIMIT];unsigned group_count=0;
    /* Group/row backgrounds precede every cell's content in the display list. */
    for(unsigned r=0;r<g->row_count;r++) {
        struct node *group=g->rows[r].group;
        if(group && (!r || group!=g->rows[r-1].group)) {
            groups[group_count]=group;group_boxes[group_count]=cb_engine_table_background(p,group,x+gap,origin,width-2*gap);group_count++;
        }
        g->rows[r].box=cb_engine_table_background(p,g->rows[r].node,x+gap,origin,width-2*gap);
        const struct style *s=&g->rows[r].node->style;
        g->rows[r].height=MAX(0,MAX(cb_engine_length_value(s->height,width,s->font_size,p->zoom),cb_engine_length_value(s->min_height,width,s->font_size,p->zoom)));
    }
    for(unsigned i=0;i<g->cell_count;i++) {
        struct table_cell *cell=&g->cells[i];struct node *n=cell->node;
        double cell_x=x+gap+cell->column*(column_width+gap),cell_width=cell->columns*column_width+(cell->columns-1)*gap;
        cell->first_item=p->item_count;cell->first_hit=p->hit_count;
        struct style saved=n->style;
        n->style.width=n->style.max_width=(struct length){0,L_AUTO};
        for(int side=0;side<4;side++)n->style.margin[side]=(struct length){0,L_PX};
        cell->height=cb_engine_layout_node(p,n,cell_x,origin,cell_width);n->style=saved;
        cell->last_item=p->item_count;cell->last_hit=p->hit_count;
        if((saved.background.a>0 || saved.border>0) && cell->first_item<cell->last_item)cell->box=cell->first_item;
        if(cell->rows==1)g->rows[cell->row].height=MAX(g->rows[cell->row].height,cell->height);
    }
    for(unsigned i=0;i<g->cell_count;i++) {
        struct table_cell *cell=&g->cells[i];if(cell->rows==1)continue;
        double height=gap*(cell->rows-1);
        for(unsigned r=cell->row;r<cell->row+cell->rows;r++)height+=g->rows[r].height;
        double extra=MAX(0,cell->height-height)/cell->rows;
        for(unsigned r=cell->row;r<cell->row+cell->rows;r++)g->rows[r].height+=extra;
    }
    cursor=origin;
    for(unsigned r=0;r<g->row_count;r++) {
        struct table_row *row=&g->rows[r];row->y=cursor;
        cb_engine_node_box(row->node,x+gap,cursor,width-2*gap,row->height);
        if(row->box!=SIZE_MAX){p->items[row->box].y=cursor;p->items[row->box].h=row->height;}
        cursor+=row->height+gap;
    }
    for(unsigned i=0;i<group_count;i++) {
        double top=-1,bottom=0;
        for(unsigned r=0;r<g->row_count;r++)if(g->rows[r].group==groups[i]){if(top<0)top=g->rows[r].y;bottom=g->rows[r].y+g->rows[r].height;}
        if(top>=0){cb_engine_node_box(groups[i],x+gap,top,width-2*gap,bottom-top);if(group_boxes[i]!=SIZE_MAX){p->items[group_boxes[i]].y=top;p->items[group_boxes[i]].h=bottom-top;}}
    }
    for(unsigned i=0;i<g->cell_count;i++) {
        struct table_cell *cell=&g->cells[i];double target=g->rows[cell->row].y,dy=target-origin;
        double height=gap*(cell->rows-1);for(unsigned r=cell->row;r<cell->row+cell->rows;r++)height+=g->rows[r].height;
        for(size_t j=cell->first_item;j<cell->last_item;j++)p->items[j].y+=dy;
        for(size_t j=cell->first_hit;j<cell->last_hit;j++)p->hits[j].y+=dy;
        cb_engine_shift_node_geometry(cell->node,dy);cell->node->h=height;
        if(cell->box!=SIZE_MAX)p->items[cell->box].h=height;
    }
    g_free(g);return cursor-y;
}

void cb_engine_clear_layout(struct cb_page *p) {
    for(size_t i=0;i<p->item_count;i++) {
        if(p->items[i].layout)g_object_unref(p->items[i].layout);
        if(p->items[i].image)cairo_surface_destroy(p->items[i].image);
    }
    g_free(p->items);p->items=NULL;p->item_count=0;
    g_free(p->hits);p->hits=NULL;p->hit_count=0;
}
void cb_engine_clear_geometry(struct node *n) {
    n->laid_out=0;n->clipped_out=0;
    for(struct node *c=n->first;c;c=c->next)cb_engine_clear_geometry(c);
}
void cb_page_set_environment(struct cb_page *p,int height,int reduced_motion,int strong_contrast) {
    if(!p)return;
    p->viewport_height=CLAMP(height,1,32767);p->reduced_motion=!!reduced_motion;p->strong_contrast=!!strong_contrast;
}

static void layout_positioned(struct cb_page *p,struct node *n){
    if(n->style.display==D_NONE)return;
    if(n->style.position==2){
        const struct node *containing=n->parent;
        while(containing&&containing->style.position==0)containing=containing->parent;
        double x=containing?containing->x:0,y=containing?containing->y:0;
        double width=containing?containing->w:p->width,height=containing?containing->h:p->viewport_height;
        struct style saved=n->style;const struct style *s=&saved;
        double left=cb_engine_length_value(s->inset[3],width,s->font_size,p->zoom),right=cb_engine_length_value(s->inset[1],width,s->font_size,p->zoom);
        double top=cb_engine_length_value(s->inset[0],height,s->font_size,p->zoom),bottom=cb_engine_length_value(s->inset[2],height,s->font_size,p->zoom);
        if(s->width.kind==L_AUTO&&s->inset[3].kind!=L_AUTO&&s->inset[1].kind!=L_AUTO)
            n->style.width=(struct length){MAX(1,width-left-right)/p->zoom,L_PX};
        if(s->height.kind==L_AUTO&&s->inset[0].kind!=L_AUTO&&s->inset[2].kind!=L_AUTO)
            n->style.height=(struct length){MAX(0,height-top-bottom)/p->zoom,L_PX};
        size_t items=p->item_count,hits=p->hit_count;
        cb_engine_layout_node(p,n,x+left,y+top,width);n->style=saved;
        double dx=s->inset[3].kind==L_AUTO&&s->inset[1].kind!=L_AUTO?width-right-n->w:0;
        double dy=s->inset[0].kind==L_AUTO&&s->inset[2].kind!=L_AUTO?height-bottom-n->h:0;
        if(dx||dy)cb_engine_translate(p,n,items,p->item_count,hits,p->hit_count,dx,dy);
    }
    for(struct node *c=n->first;c;c=c->next)layout_positioned(p,c);
}
static int clip_bounds(const struct node *n,double *x,double *y,double *w,double *h,int include_self){
    int clipped=0;
    for(const struct node *c=include_self?n:n?n->parent:NULL;c;c=c->parent){
        if(!c->laid_out||!c->style.overflow)continue;
        double right=MIN(*x+*w,c->x+c->w),bottom=MIN(*y+*h,c->y+c->h);
        *x=MAX(*x,c->x);*y=MAX(*y,c->y);*w=MAX(0,right-*x);*h=MAX(0,bottom-*y);clipped=1;
    }
    return clipped;
}
static void mark_clipped(struct node *n){
    double x=n->x,y=n->y,w=n->w,h=n->h;
    if(n->laid_out&&clip_bounds(n,&x,&y,&w,&h,0)&&(!w||!h))n->clipped_out=1;
    for(struct node *c=n->first;c;c=c->next)mark_clipped(c);
}
static void apply_clips(struct cb_page *p){
    for(size_t i=0;i<p->item_count;i++){
        struct item *item=&p->items[i];
        item->clip_x=item->x;item->clip_y=item->y;item->clip_w=item->w;item->clip_h=item->h;
        item->clipped=clip_bounds(item->owner,&item->clip_x,&item->clip_y,&item->clip_w,&item->clip_h,item->layout||item->image||item->control);
    }
    for(size_t i=0;i<p->hit_count;i++){
        struct hit *hit=&p->hits[i];clip_bounds(hit->node,&hit->x,&hit->y,&hit->w,&hit->h,1);
    }
    mark_clipped(p->root);
}
void cb_page_layout(struct cb_page *p, int width, double zoom) {
    if(!p)return;
    cb_engine_clear_layout(p);cb_engine_clear_geometry(p->root);
    p->width=CLAMP(width,1,32767);p->zoom=isfinite(zoom)?CLAMP(zoom,CB_MIN_EFFECTIVE_ZOOM,CB_MAX_EFFECTIVE_ZOOM):1;
    if(!p->context) {
        PangoFontMap *fontmap=pango_cairo_font_map_get_default();
        p->context=pango_font_map_create_context(fontmap);
        pango_cairo_context_set_resolution(p->context,96);
    }
    struct style defaults={.color={0.12,0.15,0.19,1},.font_size=16,.weight=400,.line_height=1.2};
    cb_engine_rebuild_rules(p);cb_engine_compute_style(p,p->root,&defaults);
    p->height=MAX(1,cb_engine_layout_node(p,p->root,0,0,p->width));
    for(size_t i=0;i<p->hit_count;i++)if(p->hits[i].node && p->hits[i].url) {
        const struct hit *hit=&p->hits[i];struct node *n=(struct node *)hit->node;
        if(!n->laid_out)cb_engine_node_box(n,hit->x,hit->y,hit->w,hit->h);
        else if(n->style.display==D_INLINE)cb_engine_inline_box(n,hit->x,hit->y,hit->w,hit->h);
    }
    layout_positioned(p,p->root);apply_clips(p);
    if(p->focus && (!p->focus->laid_out || p->focus->clipped_out || cb_engine_node_disabled(p->focus)))p->focus=NULL;
}
void cb_engine_source_color(cairo_t *cr, struct color c) { cairo_set_source_rgba(cr,c.r,c.g,c.b,c.a); }
void cb_engine_paint_control(struct cb_page *p,cairo_t *cr,const struct item *item) {
    struct control *c=item->control;const struct node *n=c->node;double z=p->zoom;
    int disabled=cb_engine_node_disabled(n),focused=p->focus==n;
    cairo_save(cr);
    cairo_rectangle(cr,item->x,item->y,item->w,item->h);cairo_clip(cr);
    struct color background=n->style.background.a>0?n->style.background:cb_engine_rgb(disabled?0xe9edf1:cb_engine_editable(c)?0xffffff:0xe7edf3);
    cb_engine_source_color(cr,background);cairo_paint(cr);
    if(c->kind==C_CHECKBOX || c->kind==C_RADIO) {
        double cx=item->x+item->w/2,cy=item->y+item->h/2,r=MIN(item->w,item->h)*0.33;
        cb_engine_source_color(cr,cb_engine_rgb(disabled?0x9aa4af:0x2865aa));
        if(c->kind==C_RADIO){cairo_arc(cr,cx,cy,r,0,2*G_PI);cairo_set_line_width(cr,z);cairo_stroke(cr);if(c->checked){cairo_arc(cr,cx,cy,r*0.6,0,2*G_PI);cairo_fill(cr);}}
        else if(c->checked){cairo_set_line_width(cr,2*z);cairo_move_to(cr,cx-r,cy);cairo_line_to(cr,cx-r*0.2,cy+r*0.7);cairo_line_to(cr,cx+r,cy-r*0.7);cairo_stroke(cr);}
    } else {
        int placeholder=item->placeholder,th=item->text_height;
        PangoLayout *layout=item->layout;PangoRectangle caret=item->caret;
        double inset=6*z,inner=MAX(1,item->w-2*inset-(c->kind==C_SELECT?18*z:0));
        double tx=item->x+inset,ty=item->y+(c->kind==C_TEXTAREA?inset:MAX(0,(item->h-th)/2));
        if(focused && cb_engine_editable(c) && !placeholder) {
            if(c->kind==C_TEXT)tx-=MAX(0,caret.x/(double)PANGO_SCALE-inner+2*z);
            else ty-=MAX(0,(caret.y+caret.height)/(double)PANGO_SCALE-(item->h-2*inset));
        }
        cairo_rectangle(cr,item->x+inset,item->y+2*z,inner,MAX(0,item->h-4*z));cairo_clip(cr);
        if(focused && c->select_all && *c->value){cb_engine_source_color(cr,cb_engine_rgb(0xc8ddf5));cairo_rectangle(cr,item->x+inset,item->y+3*z,inner,item->h-6*z);cairo_fill(cr);}
        cb_engine_source_color(cr,disabled || placeholder?cb_engine_rgb(0x78838f):n->style.color);cairo_move_to(cr,tx,ty);pango_cairo_show_layout(cr,layout);
        if(focused && cb_engine_editable(c) && !c->select_all) {
            cb_engine_source_color(cr,cb_engine_rgb(0x174f89));cairo_set_line_width(cr,MAX(1,z));
            double caret_x=placeholder?item->x+inset:tx+caret.x/(double)PANGO_SCALE;
            double caret_y=placeholder?ty:ty+caret.y/(double)PANGO_SCALE;
            double caret_h=placeholder?n->style.font_size*z:caret.height/(double)PANGO_SCALE;
            cairo_move_to(cr,caret_x,caret_y);cairo_line_to(cr,caret_x,caret_y+caret_h);cairo_stroke(cr);
        }
    }
    cairo_restore(cr);
    cb_engine_source_color(cr,cb_engine_rgb(focused?0x266ac0:disabled?0xc1c8d0:0x788ba0));cairo_set_line_width(cr,focused?2*z:z);
    cairo_rectangle(cr,item->x+z,item->y+z,MAX(0,item->w-2*z),MAX(0,item->h-2*z));cairo_stroke(cr);
    if(c->kind==C_SELECT){cb_engine_source_color(cr,cb_engine_rgb(0x53667a));cairo_move_to(cr,item->x+item->w-15*z,item->y+item->h/2-2*z);cairo_line_to(cr,item->x+item->w-7*z,item->y+item->h/2-2*z);cairo_line_to(cr,item->x+item->w-11*z,item->y+item->h/2+3*z);cairo_close_path(cr);cairo_fill(cr);}
}

void cb_page_paint(struct cb_page *p, cairo_t *cr, double scroll, int viewport_height) {
    if(!p || !cr)return;
    if(!isfinite(scroll))scroll=0;
    cairo_save(cr);
    cairo_rectangle(cr,0,0,p->width,MAX(0,viewport_height));cairo_clip(cr);
    cairo_set_source_rgb(cr,1,1,1);cairo_paint(cr);
    cairo_translate(cr,0,-scroll);
    for(size_t i=0;i<p->item_count;i++) {
        struct item *item=&p->items[i];
        if(item->y+item->h<scroll || item->y>scroll+viewport_height)continue;
        cairo_save(cr);
        if(item->clipped){cairo_rectangle(cr,item->clip_x,item->clip_y,item->clip_w,item->clip_h);cairo_clip(cr);}
        if(item->control)cb_engine_paint_control(p,cr,item);
        else if(item->image) {
            cairo_save(cr);
            cairo_rectangle(cr,item->x,item->y,item->w,item->h);cairo_clip(cr);
            cairo_translate(cr,item->x,item->y);
            cairo_scale(cr,item->w/cairo_image_surface_get_width(item->image),item->h/cairo_image_surface_get_height(item->image));
            cairo_set_source_surface(cr,item->image,0,0);
            cairo_pattern_set_filter(cairo_get_source(cr),CAIRO_FILTER_BILINEAR);
            cairo_pattern_set_extend(cairo_get_source(cr),CAIRO_EXTEND_PAD);cairo_paint(cr);cairo_restore(cr);
        } else if(item->layout) {
            cairo_move_to(cr,item->x,item->y);
            cairo_set_source_rgb(cr,0,0,0);
            pango_cairo_show_layout(cr,item->layout);
        } else {
            if(item->background.a>0){cb_engine_source_color(cr,item->background);cairo_rectangle(cr,item->x,item->y,item->w,item->h);cairo_fill(cr);}
            if(item->border>0){cb_engine_source_color(cr,item->border_color);cairo_set_line_width(cr,item->border);cairo_rectangle(cr,item->x+item->border/2,item->y+item->border/2,MAX(0,item->w-item->border),MAX(0,item->h-item->border));cairo_stroke(cr);}
        }
        cairo_restore(cr);
    }
    if(p->focus && p->focus->href) {
        cb_engine_source_color(cr,cb_engine_rgb(0x2367c5));cairo_set_line_width(cr,1.5*p->zoom);
        for(size_t i=0;i<p->hit_count;i++)if(p->hits[i].node==p->focus) {
            const struct hit *h=&p->hits[i];cairo_rectangle(cr,h->x-2,h->y-1,h->w+4,h->h+2);cairo_stroke(cr);
        }
    }
    cairo_restore(cr);
}
const char *cb_page_title(const struct cb_page *p) { return p?p->title:""; }
double cb_page_height(const struct cb_page *p) { return p?p->height:0; }
size_t cb_page_node_count(const struct cb_page *p) { return p?p->node_count:0; }
const char *cb_page_link_at(const struct cb_page *p, double x, double y) {
    if(!p)return NULL;
    for(size_t i=p->hit_count;i>0;i--) {
        const struct hit *h=&p->hits[i-1];
        if(x>=h->x && x<h->x+h->w && y>=h->y && y<h->y+h->h)return h->url;
    }
    return NULL;
}
int cb_page_find(const struct cb_page *p, const char *query, double after_y, double *found_y) {
    if(!p || !query || !*query || !found_y || !g_utf8_validate(query,-1,NULL))return 0;
    char *needle=g_utf8_casefold(query,-1);
    for(size_t i=0;i<p->item_count;i++) {
        const struct item *item=&p->items[i];if(!item->layout || item->y+item->h<after_y)continue;
        const char *text=pango_layout_get_text(item->layout);
        char *folded=g_utf8_casefold(text,-1), *match=folded;
        while((match=strstr(match,needle))) {
            size_t folded_offset=(size_t)(match-folded), consumed=0;
            const char *original=text;
            while(*original && consumed<folded_offset) {
                const char *next=g_utf8_next_char(original);
                char *one=g_utf8_casefold(original,next-original);
                consumed+=strlen(one);g_free(one);original=next;
            }
            PangoRectangle pos;
            pango_layout_index_to_pos(item->layout,(int)(original-text),&pos);
            double y=item->y+pos.y/(double)PANGO_SCALE;
            if(y>=after_y){*found_y=y;g_free(folded);g_free(needle);return 1;}
            match++;
        }
        g_free(folded);
    }
    g_free(needle);return 0;
}

void cb_engine_extract_text(const struct node *n, GString *out) {
    if(n->hidden || n->style.display==D_NONE || cb_engine_in_words("head script style title template",n->tag))return;
    if(n->text) {
        for(const char *c=n->text;*c;c++) {
            if(g_ascii_isspace(*c)) { if(out->len && !g_ascii_isspace(out->str[out->len-1]))g_string_append_c(out,' '); }
            else g_string_append_c(out,*c);
        }
    }
    if(cb_engine_tag_is(n,"img") && n->alt)g_string_append(out,n->alt);
    for(const struct node *c=n->first;c;c=c->next)cb_engine_extract_text(c,out);
    if((cb_engine_is_block_tag(n->tag) || cb_engine_tag_is(n,"br")) && out->len && out->str[out->len-1]!='\n')g_string_append_c(out,'\n');
}
char *cb_page_text(const struct cb_page *p) {
    if(!p)return strdup("");
    /* Style must also be available to reader mode before the first layout. */
    struct style defaults={.color={0.12,0.15,0.19,1},.font_size=16,.weight=400,.line_height=1.2};
    cb_engine_compute_style((struct cb_page *)p,p->root,&defaults);
    GString *text=g_string_new(NULL);cb_engine_extract_text(p->root,text);
    char *result=strdup(text->str);g_string_free(text,TRUE);return result;
}
