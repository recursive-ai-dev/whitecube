#include "engine-internal.h"

char *cb_engine_bounded_value(const char *text, int multiline) {
    GString *out=g_string_new(NULL);
    if(text)for(const char *c=text;*c && out->len<VALUE_LIMIT;c++) {
        unsigned char ch=(unsigned char)*c;
        if(ch=='\r'){if(c[1]=='\n')continue;if(multiline)g_string_append_c(out,'\n');continue;}
        if(ch=='\n'){if(multiline)g_string_append_c(out,'\n');continue;}
        if(ch=='\t'){g_string_append_c(out,multiline?'\t':' ');continue;}
        if(ch<32 || ch==127)continue;
        g_string_append_c(out,*c);
    }
    const char *end=NULL;
    if(!g_utf8_validate(out->str,out->len,&end))g_string_truncate(out,(gsize)(end-out->str));
    return g_string_free(out,FALSE);
}
void cb_engine_raw_node_text(const struct node *n,GString *out) {
    if(out->len>=VALUE_LIMIT || n->hidden)return;
    if(n->text)g_string_append_len(out,n->text,MIN(strlen(n->text),VALUE_LIMIT-out->len));
    for(const struct node *c=n->first;c;c=c->next)cb_engine_raw_node_text(c,out);
}
char *cb_engine_node_text(const struct node *n,int multiline) {
    GString *text=g_string_new(NULL);cb_engine_raw_node_text(n,text);
    char *result=cb_engine_bounded_value(text->str,multiline);g_string_free(text,TRUE);return result;
}
int cb_engine_node_disabled(const struct node *n) {
    if(n->disabled)return 1;
    for(const struct node *parent=n->parent;parent;parent=parent->parent) {
        if(cb_engine_tag_is(parent,"optgroup") && parent->disabled)return 1;
        if(cb_engine_tag_is(parent,"fieldset") && parent->disabled) {
            const struct node *legend=parent->first;
            while(legend && !cb_engine_tag_is(legend,"legend"))legend=legend->next;
            const struct node *ancestor=n;
            while(ancestor && ancestor!=parent && ancestor!=legend)ancestor=ancestor->parent;
            if(!legend || ancestor!=legend)return 1;
        }
    }
    return 0;
}
struct node *cb_engine_form_id(struct node *n,const char *id) {
    if(n->id && !strcmp(n->id,id))return n;
    for(struct node *child=n->first;child;child=child->next){struct node *found=cb_engine_form_id(child,id);if(found)return found;}
    return NULL;
}
size_t cb_engine_raw_text_length(const struct node *n) {
    size_t length=n->text?strlen(n->text):0;
    for(const struct node *child=n->first;child && length<=VALUE_LIMIT;child=child->next)length+=cb_engine_raw_text_length(child);
    return MIN(length,VALUE_LIMIT+1u);
}
char *cb_engine_exact_value(const char *text) {
    char *copy=g_strndup(text?text:"",VALUE_LIMIT);const char *end=NULL;
    if(!g_utf8_validate(copy,-1,&end))copy[end-copy]=0;
    return copy;
}
char *cb_engine_initial_value(const char *text,enum control_kind kind) {
    char *result=cb_engine_exact_value(text);
    if(kind==C_TEXT || kind==C_TEXTAREA) {
        char *write=result;
        for(const char *read=result;*read;read++) {
            if(*read=='\r'){if(kind==C_TEXTAREA && read[1]!='\n')*write++='\n';continue;}
            if(*read=='\n' && kind==C_TEXT)continue;
            *write++=*read;
        }
        *write=0;
    }
    return result;
}

void cb_engine_init_options(struct control *c,struct node *n) {
    if(cb_engine_tag_is(n,"option")) {
        if(c->option_count>=OPTION_LIMIT){c->options_overflow=1;return;}
        struct option *o=&c->options[c->option_count];o->node=n;o->label=cb_engine_node_text(n,1);
        GString *collapsed=g_string_new(NULL);int whitespace=0;
        for(const char *s=o->label;*s;s++) {
            if(g_ascii_isspace(*s)){whitespace=collapsed->len>0;continue;}
            if(whitespace){g_string_append_c(collapsed,' ');whitespace=0;}g_string_append_c(collapsed,*s);
        }
        g_free(o->label);o->label=g_string_free(collapsed,FALSE);
        o->value=cb_engine_exact_value(n->value?n->value:o->label);
        o->incomplete=n->value?strlen(n->value)>VALUE_LIMIT:cb_engine_raw_text_length(n)>VALUE_LIMIT;
        if(n->selected)c->selected=(int)c->option_count;
        c->option_count++;return;
    }
    for(struct node *child=n->first;child;child=child->next)cb_engine_init_options(c,child);
}
void cb_engine_init_controls(struct cb_page *p,struct node *n) {
    if(!n->control && cb_engine_in_words("input button textarea select",n->tag) && p->control_count>=CONTROL_LIMIT)p->control_overflow=1;
    if(!n->control && cb_engine_in_words("input button textarea select",n->tag) && p->control_count<CONTROL_LIMIT) {
        struct control *c=&p->controls[p->control_count++];c->node=n;n->control=c;
        struct node *form=n->parent;while(form && !cb_engine_tag_is(form,"form"))form=form->parent;
        c->form=n->external_form?cb_engine_form_id(p->root,n->external_form):form;
        if(c->form && !cb_engine_tag_is(c->form,"form"))c->form=NULL;
        c->maxlength=n->maxlength;
        const char *type=n->type?n->type:"text";
        if(cb_engine_tag_is(n,"textarea"))c->kind=C_TEXTAREA;
        else if(cb_engine_tag_is(n,"select"))c->kind=n->multiple?C_UNSUPPORTED:C_SELECT;
        else if(cb_engine_tag_is(n,"button"))c->kind=n->type && !g_ascii_strcasecmp(type,"reset")?C_RESET:
            n->type && !g_ascii_strcasecmp(type,"button")?C_BUTTON:C_SUBMIT;
        else if(!g_ascii_strcasecmp(type,"hidden"))c->kind=C_HIDDEN;
        else if(!g_ascii_strcasecmp(type,"checkbox"))c->kind=C_CHECKBOX;
        else if(!g_ascii_strcasecmp(type,"radio"))c->kind=C_RADIO;
        else if(!g_ascii_strcasecmp(type,"submit"))c->kind=C_SUBMIT;
        else if(!g_ascii_strcasecmp(type,"reset"))c->kind=C_RESET;
        else if(!g_ascii_strcasecmp(type,"button"))c->kind=C_BUTTON;
        else if(!g_ascii_strcasecmp(type,"password"))c->kind=C_PASSWORD;
        else if(!g_ascii_strcasecmp(type,"file"))c->kind=C_FILE;
        else if(!g_ascii_strcasecmp(type,"text") || !g_ascii_strcasecmp(type,"search") ||
                !g_ascii_strcasecmp(type,"email") || !g_ascii_strcasecmp(type,"url") || !g_ascii_strcasecmp(type,"tel"))c->kind=C_TEXT;
        else c->kind=C_UNSUPPORTED;
        if(c->kind==C_TEXTAREA) {
            GString *text=g_string_new(NULL);cb_engine_raw_node_text(n,text);c->value=cb_engine_initial_value(text->str,C_TEXTAREA);g_string_free(text,TRUE);
            c->incomplete=cb_engine_raw_text_length(n)>VALUE_LIMIT;
        }
        else if(c->kind==C_FILE)c->value=g_strdup("");
        else {
            c->value=cb_engine_initial_value(n->value?n->value:(c->kind==C_CHECKBOX || c->kind==C_RADIO)?"on":
                c->kind==C_SUBMIT && cb_engine_tag_is(n,"input")?"Submit":c->kind==C_RESET && cb_engine_tag_is(n,"input")?"Reset":"",c->kind);
            c->incomplete=n->value && strlen(n->value)>VALUE_LIMIT;
        }
        c->initial_incomplete=c->incomplete;
        c->initial=g_strdup(c->value);c->cursor=strlen(c->value);
        c->checked=c->initial_checked=n->checked;
        if(c->kind==C_RADIO && c->checked && n->name && *n->name) {
            for(size_t i=0;i+1<p->control_count;i++) {
                struct control *other=&p->controls[i];
                if(other->kind==C_RADIO && other->form==c->form && other->node->name && !strcmp(other->node->name,n->name))
                    other->checked=other->initial_checked=0;
            }
        }
        if(c->kind==C_SELECT) {
            c->selected=-1;cb_engine_init_options(c,n);
            if(c->selected<0)for(unsigned i=0;i<c->option_count;i++)if(!cb_engine_node_disabled(c->options[i].node)){c->selected=(int)i;break;}
            c->initial_selected=c->selected;
        }
        if(cb_engine_tag_is(n,"button"))c->label=cb_engine_node_text(n,0);
        else if(c->kind==C_PASSWORD)c->label=g_strdup("Password");
        else if(c->kind==C_FILE)c->label=g_strdup("File upload unsupported");
        else if(c->kind==C_UNSUPPORTED)c->label=g_strdup("Unsupported control");
        else c->label=g_strdup(c->value);
        if((c->kind==C_SUBMIT || c->kind==C_RESET) && !*c->label){g_free(c->label);c->label=g_strdup(c->kind==C_SUBMIT?"Submit":"Reset");}
    }
    for(struct node *child=n->first;child;child=child->next)cb_engine_init_controls(p,child);
}

unsigned cb_engine_focus_kind(const struct node *n) {
    if(!n)return CB_FOCUS_NONE;
    if(n->control)return cb_engine_editable(n->control)?CB_FOCUS_EDIT:CB_FOCUS_CONTROL;
    return n->href?CB_FOCUS_LINK:CB_FOCUS_NONE;
}
void cb_engine_interaction_focus(const struct cb_page *p,struct cb_interaction *out) {
    out->focus=cb_engine_focus_kind(p->focus);out->y=p->focus?p->focus->y:-1;
}
void cb_engine_notice(struct cb_interaction *out,const char *message) {
    out->kind=CB_INTERACTION_NOTICE;g_strlcpy(out->text,message,sizeof out->text);
}
int cb_engine_focusable(const struct node *n) {
    return n->laid_out && !n->clipped_out && !n->tab_skip && !cb_engine_node_disabled(n) && n->w>0 && n->h>0 &&
        (n->href || (n->control && n->control->kind!=C_HIDDEN));
}
void cb_engine_focus_nodes(struct node *n,struct node **nodes,size_t *count) {
    if(cb_engine_focusable(n))nodes[(*count)++]=n;
    for(struct node *child=n->first;child;child=child->next)cb_engine_focus_nodes(child,nodes,count);
}
void cb_engine_submit_form(struct cb_page *p,struct control *source,struct cb_interaction *out) {
    if(p->control_overflow || p->parse_incomplete){cb_engine_notice(out,"This document exceeds the supported parsing or form-control limits; no submission was sent.");return;}
    struct node *form=source->form;
    if(!form){cb_engine_notice(out,"This control has no supported form owner.");return;}
    struct control *submitter=source->kind==C_SUBMIT?source:NULL;
    if(!submitter)for(size_t i=0;i<p->control_count;i++) {
        struct control *c=&p->controls[i];
        if(c->form==form && c->kind==C_SUBMIT){submitter=c;break;}
    }
    if(submitter && cb_engine_node_disabled(submitter->node)){cb_engine_notice(out,"The default submit button is disabled; no submission was sent.");return;}
    if(!submitter && source->kind!=C_SUBMIT) {
        size_t blocking=0;for(size_t i=0;i<p->control_count;i++)if(p->controls[i].form==form && p->controls[i].kind==C_TEXT)blocking++;
        if(blocking>1){cb_engine_notice(out,"This form has multiple text fields and no submit button.");return;}
    }
    const char *method=submitter && submitter->node->submit_method?submitter->node->submit_method:form->method;
    int post=method&&!g_ascii_strcasecmp(method,"post");
    if(method&&*method&&g_ascii_strcasecmp(method,"get")&&!post){cb_engine_notice(out,"This form method is unsupported.");return;}
    const char *enctype=form->attrs?g_hash_table_lookup(form->attrs,"enctype"):NULL;
    if(post&&enctype&&*enctype&&g_ascii_strcasecmp(enctype,"application/x-www-form-urlencoded")){cb_engine_notice(out,"This form encoding is unsupported.");return;}
    struct cb_form_pair pairs[CONTROL_LIMIT];size_t count=0;
    for(size_t i=0;i<p->control_count;i++) {
        struct control *c=&p->controls[i];const struct node *n=c->node;
        if(c->form!=form || cb_engine_node_disabled(n))continue;
        if(c->kind==C_FILE||(c->kind==C_PASSWORD&&!post)){cb_engine_notice(out,"File uploads are unsupported; password forms require POST.");return;}
        if(c->kind==C_UNSUPPORTED || c->options_overflow){cb_engine_notice(out,"This form contains an unsupported control or too many options; no submission was sent.");return;}
        if(!n->name || !*n->name)continue;
        if((c->kind==C_CHECKBOX || c->kind==C_RADIO) && !c->checked)continue;
        if(c->kind==C_RESET || c->kind==C_BUTTON || (c->kind==C_SUBMIT && c!=submitter))continue;
        const char *value=c->value;int incomplete=c->incomplete;
        if(c->kind==C_SELECT){if(c->selected<0 || cb_engine_node_disabled(c->options[c->selected].node))continue;value=c->options[c->selected].value;incomplete=c->options[c->selected].incomplete;}
        if(incomplete){cb_engine_notice(out,"A form value exceeds the supported length; no submission was sent.");return;}
        pairs[count++]=(struct cb_form_pair){n->name,value};
    }
    const char *action=submitter && submitter->node->submit_action?submitter->node->submit_action:form->action;
    char error[256];
    int failed=post?cb_form_post(p->base,action,pairs,count,out->text,sizeof out->text,out->body,sizeof out->body,error,sizeof error):
        cb_form_get_url(p->base,action,pairs,count,out->text,sizeof out->text,error,sizeof error);
    if(failed){cb_engine_notice(out,error);return;}
    out->kind=post?CB_INTERACTION_POST:CB_INTERACTION_SUBMIT;
}
void cb_engine_activate_control(struct cb_page *p,struct control *c,struct cb_interaction *out) {
    if(cb_engine_node_disabled(c->node))return;
    out->kind=CB_INTERACTION_UPDATE;
    if(c->kind==C_SUBMIT){cb_engine_submit_form(p,c,out);return;}
    if(c->kind==C_FILE){cb_engine_notice(out,"File uploads are not supported in this build.");return;}
    if(c->kind==C_UNSUPPORTED || c->kind==C_BUTTON){cb_engine_notice(out,"This control requires a feature that is not supported.");return;}
    if(c->kind==C_RESET) {
        for(size_t i=0;i<p->control_count;i++) {
            struct control *other=&p->controls[i];if(!c->form || other->form!=c->form)continue;
            g_free(other->value);other->value=g_strdup(other->initial);other->cursor=strlen(other->value);
            other->checked=other->initial_checked;other->selected=other->initial_selected;other->select_all=0;other->incomplete=other->initial_incomplete;
        }
    } else if(c->kind==C_CHECKBOX)c->checked=!c->checked;
    else if(c->kind==C_RADIO) {
        if(c->node->name && *c->node->name)for(size_t i=0;i<p->control_count;i++) {
            struct control *other=&p->controls[i];
            if(other->kind==C_RADIO && other->form==c->form && other->node->name && !strcmp(other->node->name,c->node->name))other->checked=0;
        }
        c->checked=1;
    }
}
void cb_engine_navigate_link(const struct node *n,struct cb_interaction *out) {
    if(strlen(n->href)>=sizeof out->text){cb_engine_notice(out,"This link address exceeds the supported length.");return;}
    out->kind=CB_INTERACTION_NAVIGATE;g_strlcpy(out->text,n->href,sizeof out->text);
}
void cb_engine_insert_value(struct control *c,const char *text,struct cb_interaction *out) {
    if(c->node->readonly){cb_engine_notice(out,"This field is read-only.");return;}
    char *insert=cb_engine_bounded_value(text,c->kind==C_TEXTAREA);
    size_t length=strlen(c->value),addition=strlen(insert),position=c->select_all?0:c->cursor;
    size_t retained=c->select_all?0:length;
    if(retained+addition>VALUE_LIMIT || (size_t)g_utf8_strlen(c->select_all?"":c->value,-1)+(size_t)g_utf8_strlen(insert,-1)>c->maxlength) {
        g_free(insert);cb_engine_notice(out,"This field has reached its length limit.");return;
    }
    char *result=g_malloc(retained+addition+1);
    memcpy(result,c->value,position);memcpy(result+position,insert,addition);
    if(!c->select_all)memcpy(result+position+addition,c->value+position,length-position+1);
    else result[addition]=0;
    if(c->select_all)c->incomplete=0;
    g_free(insert);g_free(c->value);c->value=result;c->cursor=position+addition;c->select_all=0;out->kind=CB_INTERACTION_UPDATE;
}
void cb_engine_vertical_cursor(struct cb_page *p,struct control *c,int direction) {
    PangoLayout *layout=pango_layout_new(p->context);PangoFontDescription *font=pango_font_description_new();
    pango_font_description_set_family(font,"sans-serif");pango_font_description_set_absolute_size(font,c->node->style.font_size*p->zoom*PANGO_SCALE);
    pango_layout_set_font_description(layout,font);pango_font_description_free(font);pango_layout_set_text(layout,c->value,-1);
    pango_layout_set_width(layout,(int)(MAX(1,c->node->w-12*p->zoom)*PANGO_SCALE));pango_layout_set_wrap(layout,PANGO_WRAP_WORD_CHAR);
    int line,x;pango_layout_index_to_line_x(layout,(int)c->cursor,FALSE,&line,&x);
    line=CLAMP(line+direction,0,pango_layout_get_line_count(layout)-1);
    PangoLayoutLine *target=pango_layout_get_line_readonly(layout,line);int index=0,trailing=0;
    pango_layout_line_x_to_index(target,x,&index,&trailing);
    const char *point=c->value+CLAMP(index,0,(int)strlen(c->value));
    while(trailing-- && *point)point=g_utf8_next_char(point);
    c->cursor=(size_t)(point-c->value);c->select_all=0;g_object_unref(layout);
}
int cb_page_interact(struct cb_page *p,unsigned action,double x,double y,const char *text,struct cb_interaction *out) {
    if(!p || !out || action<CB_INTERACT_CLICK || action>CB_INTERACT_BLUR)return -1;
    memset(out,0,sizeof *out);out->y=-1;
    if(!text)text="";
    if(strnlen(text,4096)>VALUE_LIMIT || !g_utf8_validate(text,-1,NULL))return -1;
    if(p->focus && (!p->focus->laid_out || p->focus->clipped_out || cb_engine_node_disabled(p->focus)))p->focus=NULL;
    if(action==CB_INTERACT_BLUR){p->focus=NULL;out->kind=CB_INTERACTION_UPDATE;}
    else if(action==CB_INTERACT_NEXT || action==CB_INTERACT_PREVIOUS) {
        struct node **nodes=g_new(struct node *,p->node_count);size_t count=0;cb_engine_focus_nodes(p->root,nodes,&count);
        size_t current=count;for(size_t i=0;i<count;i++)if(nodes[i]==p->focus){current=i;break;}
        if(count) {
            size_t next=action==CB_INTERACT_NEXT?(current==count?0:(current+1)%count):(current==count?count-1:(current+count-1)%count);
            p->focus=nodes[next];
        }else p->focus=NULL;
        g_free(nodes);out->kind=CB_INTERACTION_UPDATE;
    } else if(action==CB_INTERACT_CLICK) {
        if(!isfinite(x) || !isfinite(y))return -1;
        p->focus=NULL;out->kind=CB_INTERACTION_UPDATE;
        for(size_t i=p->hit_count;i>0;i--) {
            const struct hit *hit=&p->hits[i-1];
            if(x<hit->x || x>=hit->x+hit->w || y<hit->y || y>=hit->y+hit->h || !hit->node)continue;
            if(cb_engine_node_disabled(hit->node))break;
            p->focus=(struct node *)hit->node;
            if(p->focus->control) {
                struct control *c=p->focus->control;c->select_all=0;c->cursor=strlen(c->value);
                cb_engine_activate_control(p,c,out);
            }else cb_engine_navigate_link(p->focus,out);
            break;
        }
    } else if(p->focus) {
        struct node *n=p->focus;struct control *c=n->control;
        if(action==CB_INTERACT_TEXT) {
            if(cb_engine_editable(c))cb_engine_insert_value(c,text,out);
            else if(c && c->kind==C_FILE)cb_engine_notice(out,"File uploads are not supported.");
        } else if(action==CB_INTERACT_KEY) {
            if(!strcmp(text,"escape")){p->focus=NULL;out->kind=CB_INTERACTION_UPDATE;}
            else if(!strcmp(text,"enter")) {
                if(n->href)cb_engine_navigate_link(n,out);
                else if(c && c->kind==C_TEXTAREA)cb_engine_insert_value(c,"\n",out);
                else if(c && (c->kind==C_TEXT||c->kind==C_PASSWORD))cb_engine_submit_form(p,c,out);
                else if(c)cb_engine_activate_control(p,c,out);
            }else if(c && c->kind==C_SELECT) {
                int direction=!strcmp(text,"up") || !strcmp(text,"left")?-1:!strcmp(text,"down") || !strcmp(text,"right") || !strcmp(text,"space")?1:0;
                int next=c->selected;
                if(!strcmp(text,"home")){next=-1;direction=1;}else if(!strcmp(text,"end")){next=(int)c->option_count;direction=-1;}
                if(direction) {
                    do{next+=direction;}while(next>=0 && next<(int)c->option_count && cb_engine_node_disabled(c->options[next].node));
                    if(next>=0 && next<(int)c->option_count)c->selected=next;
                    out->kind=CB_INTERACTION_UPDATE;
                }
            }else if(cb_engine_editable(c)) {
                size_t length=strlen(c->value);out->kind=CB_INTERACTION_UPDATE;
                if(!strcmp(text,"select-all"))c->select_all=1;
                else if(!strcmp(text,"copy") || !strcmp(text,"cut")) {
                    if(c->select_all && *c->value && c->kind!=C_PASSWORD) {
                        out->kind=CB_INTERACTION_COPY;g_strlcpy(out->text,c->value,sizeof out->text);
                        if(!strcmp(text,"cut")) {
                            if(n->readonly)cb_engine_notice(out,"This field is read-only.");
                            else{c->value[0]=0;c->cursor=0;c->select_all=0;c->incomplete=0;}
                        }
                    }
                }else if(!strcmp(text,"left")){c->cursor=c->select_all?0:c->cursor?(size_t)(g_utf8_find_prev_char(c->value,c->value+c->cursor)-c->value):0;c->select_all=0;}
                else if(!strcmp(text,"right")){c->cursor=c->select_all?length:c->cursor<length?(size_t)(g_utf8_next_char(c->value+c->cursor)-c->value):length;c->select_all=0;}
                else if(!strcmp(text,"home")){c->cursor=0;c->select_all=0;}
                else if(!strcmp(text,"end")){c->cursor=length;c->select_all=0;}
                else if((!strcmp(text,"up") || !strcmp(text,"down")) && c->kind==C_TEXTAREA)cb_engine_vertical_cursor(p,c,!strcmp(text,"up")?-1:1);
                else if(!strcmp(text,"space"))cb_engine_insert_value(c," ",out);
                else if(!strcmp(text,"backspace") || !strcmp(text,"delete")) {
                    if(n->readonly)cb_engine_notice(out,"This field is read-only.");
                    else if(c->select_all){c->value[0]=0;c->cursor=0;c->select_all=0;c->incomplete=0;}
                    else if(!strcmp(text,"backspace") && c->cursor){size_t prev=(size_t)(g_utf8_find_prev_char(c->value,c->value+c->cursor)-c->value);memmove(c->value+prev,c->value+c->cursor,length-c->cursor+1);c->cursor=prev;}
                    else if(!strcmp(text,"delete") && c->cursor<length){size_t next=(size_t)(g_utf8_next_char(c->value+c->cursor)-c->value);memmove(c->value+c->cursor,c->value+next,length-next+1);}
                }
            }else if(c && !strcmp(text,"space"))cb_engine_activate_control(p,c,out);
        }
    }
    for(size_t i=0;i<p->item_count;i++)if(p->items[i].control)cb_engine_prepare_control_layout(p,&p->items[i]);
    cb_engine_interaction_focus(p,out);return 0;
}
const struct node *cb_engine_find_anchor(const struct node *n,const char *name) {
    if(n->laid_out && ((n->id && !strcmp(n->id,name)) || (cb_engine_tag_is(n,"a") && n->name && !strcmp(n->name,name))))return n;
    for(const struct node *child=n->first;child;child=child->next){const struct node *found=cb_engine_find_anchor(child,name);if(found)return found;}
    return NULL;
}
int cb_page_anchor(const struct cb_page *p,const char *fragment,double *y) {
    if(!p || !fragment || !y)return 0;
    if(*fragment=='#')fragment++;
    if(!*fragment){*y=0;return 1;}
    char *name=g_uri_unescape_string(fragment,NULL);if(!name || !g_utf8_validate(name,-1,NULL)){g_free(name);return 0;}
    const struct node *n=cb_engine_find_anchor(p->root,name);g_free(name);
    if(!n)return 0;
    *y=n->y;return 1;
}
