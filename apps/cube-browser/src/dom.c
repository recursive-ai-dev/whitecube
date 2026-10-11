#include "engine-internal.h"
#include "dom.h"
#include "js.h"
#include "web-bootstrap.h"
#include <time.h>
#include <unistd.h>
struct timer {unsigned id;double due,delay;int interval;};
struct cb_dom {struct cb_page *page;struct cb_js *js;int socket,dirty,loaded,last_state,gesture;struct timer timers[64];unsigned timer_count,fetch_count,fetches[32];size_t text_bytes;};
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static char *number(unsigned n){return g_strdup_printf("%u",n);}
static char *boolean(int value){return g_strdup(value?"true":"false");}
static char *quote(const char *s){if(!s)return g_strdup("null");GString *out=g_string_new("\"");for(const unsigned char *p=(const unsigned char *)s;*p;p++){if(*p=='"'||*p=='\\')g_string_append_printf(out,"\\%c",*p);else if(*p<32)g_string_append_printf(out,"\\u%04x",*p);else g_string_append_c(out,*p);}g_string_append_c(out,'"');return g_string_free(out,FALSE);}
/* Decode fetch text without treating an embedded NUL as end-of-body. */
static char *quote_body(const char *s,size_t size){
    GString *out=g_string_new("\"");
    for(size_t i=0;i<size;){
        unsigned char c=(unsigned char)s[i];
        if(c<32){g_string_append_printf(out,"\\u%04x",c);i++;}
        else if(c=='"'||c=='\\'){g_string_append_printf(out,"\\%c",c);i++;}
        else if(c<128){g_string_append_c(out,c);i++;}
        else{gunichar cp=g_utf8_get_char_validated(s+i,(gssize)(size-i));
            if(cp==(gunichar)-1||cp==(gunichar)-2){g_string_append_unichar(out,0xfffd);i++;}
            else{size_t n=(size_t)(g_utf8_next_char(s+i)-(s+i));g_string_append_len(out,s+i,n);i+=n;}}
        if(out->len>1024*1024-8192){g_string_free(out,TRUE);return NULL;}
    }
    g_string_append_c(out,'"');return g_string_free(out,FALSE);
}
static struct node *node(struct cb_dom *d,const char *id){char *end;unsigned long n=strtoul(id,&end,10);return *id&&!*end&&n&&n<=d->page->nodes->len?g_ptr_array_index(d->page->nodes,n-1):NULL;}
static int attached(struct node *n){unsigned depth=0;while(n&&n->parent&&depth++<=DEPTH_LIMIT)n=n->parent;return n&&n==n->page->root;}
static void detach(struct node *n){struct node *p=n->parent;if(!p)return;struct node **at=&p->first,*previous=NULL;while(*at&&*at!=n){previous=*at;at=&(*at)->next;}if(*at){*at=n->next;if(p->last==n)p->last=previous;}n->next=NULL;n->parent=NULL;}
static void append(struct node *parent,struct node *child,struct node *before){detach(child);child->parent=parent;struct node **at=&parent->first;while(*at&&*at!=before)at=&(*at)->next;child->next=*at;*at=child;if(!child->next)parent->last=child;}
static unsigned subtree_depth(struct node *n){unsigned max=0;for(struct node *c=n->first;c;c=c->next){unsigned v=subtree_depth(c);if(v>max)max=v;}return max+1;}
static void emit(struct cb_dom *d,unsigned type,unsigned a,unsigned b,const char *text,int fd){struct cb_message m={.type=type,.fd=fd,.a=a,.b=b};if(text)snprintf(m.text,sizeof m.text,"%.8191s",text);cb_send(d->socket,&m);if(fd>=0)close(fd);}
static void log_message(void *opaque,unsigned level,const char *text){emit(opaque,CB_CONSOLE,level,0,text,-1);}
static void refresh_controls(struct cb_dom *d){
    cb_engine_init_controls(d->page,d->page->root);
    for(size_t i=0;i<d->page->control_count;i++){struct control *c=&d->page->controls[i];struct node *form=c->node->parent;while(form&&!cb_engine_tag_is(form,"form"))form=form->parent;c->form=c->node->external_form?cb_engine_form_id(d->page->root,c->node->external_form):form;}
}
static int set_attr(struct cb_dom *d,struct node *n,const char *name,const char *value){
    if(n->tag[0]=='#'||!*name||strlen(name)>31||(value&&strlen(value)>ATTR_LIMIT))return -1;
    for(const char *p=name;*p;p++)if(!cb_engine_name_char(*p)&&*p!=':')return -1;
    if(!n->attrs)n->attrs=g_hash_table_new_full(g_str_hash,g_str_equal,g_free,g_free);
    char *key=g_ascii_strdown(name,-1);if(value&&g_hash_table_size(n->attrs)>=64&&!g_hash_table_contains(n->attrs,key)){g_free(key);return -1;}
    if(value)g_hash_table_replace(n->attrs,g_strdup(key),g_strdup(value));else g_hash_table_remove(n->attrs,key);
    char **slot=NULL;
#define ATTR(k,f) if(!strcmp(key,k))slot=&n->f
    ATTR("id",id);ATTR("class",classes);ATTR("style",inline_css);ATTR("alt",alt);ATTR("name",name);ATTR("value",value);ATTR("type",type);ATTR("action",action);ATTR("method",method);ATTR("placeholder",placeholder);ATTR("form",external_form);ATTR("formaction",submit_action);ATTR("formmethod",submit_method);
#undef ATTR
    if(slot){g_free(*slot);*slot=g_strdup(value);}
    if(!strcmp(key,"href")){g_free(n->href);n->href=value&&cb_engine_tag_is(n,"a")?cb_engine_resolve_url(d->page,value):NULL;}
    if(!strcmp(key,"hidden"))n->hidden=value!=NULL;
    if(!strcmp(key,"disabled"))n->disabled=value!=NULL;
    if(!strcmp(key,"readonly"))n->readonly=value!=NULL;
    if(!strcmp(key,"checked")){n->checked=value!=NULL;if(n->control)n->control->checked=n->checked;}
    if(!strcmp(key,"value")&&n->control){g_free(n->control->value);n->control->value=cb_engine_bounded_value(value?value:"",n->control->kind==C_TEXTAREA);n->control->cursor=strlen(n->control->value);}
    if(!strcmp(key,"type")&&n->control&&cb_engine_tag_is(n,"input"))n->control->kind=value&&!g_ascii_strcasecmp(value,"password")?C_PASSWORD:C_TEXT;
    g_free(key);d->dirty=1;return 0;
}
static void query_nodes(struct node *root,const char *selector,int id,int include,GString *out,unsigned *count){
    if(include&&root->tag[0]!='#'&&((id&&root->id&&!strcmp(root->id,selector))||(!id&&cb_engine_selector_matches(root,selector)))){if((*count)++)g_string_append_c(out,',');g_string_append_printf(out,"%u",root->uid);}
    for(struct node *c=root->first;c;c=c->next)query_nodes(c,selector,id,1,out,count);
}
static char *get(struct cb_dom *d,struct node *n,const char *key){
    if(!strcmp(key,"nodeType"))return number(n==d->page->root?9:n->text?3:1);
    if(!strcmp(key,"nodeName")){char *name=g_ascii_strup(n->tag,-1),*out=quote(n->tag[0]=='#'?n->tag:name);g_free(name);return out;}
    if(!strcmp(key,"parentNode"))return number(n->parent?n->parent->uid:0);
    if(!strcmp(key,"nextSibling"))return number(n->next?n->next->uid:0);
    if(!strcmp(key,"childNodes")){GString *out=g_string_new("[");for(struct node *c=n->first;c;c=c->next)g_string_append_printf(out,"%s%u",c==n->first?"":",",c->uid);g_string_append_c(out,']');return g_string_free(out,FALSE);}
    if(!strcmp(key,"textContent")||!strcmp(key,"data")){GString *out=g_string_new(NULL);cb_engine_raw_node_text(n,out);char *text=quote(out->str);g_string_free(out,TRUE);return text;}
    if(!strcmp(key,"value"))return quote(n->control?n->control->kind==C_SELECT&&n->control->selected>=0?n->control->options[n->control->selected].value:n->control->value:n->value?n->value:"");
    if(!strcmp(key,"checked"))return boolean(n->control?n->control->checked:n->checked);
    if(!strcmp(key,"disabled"))return boolean(n->disabled);
    if(!strcmp(key,"hidden"))return boolean(n->hidden);
    if(!strcmp(key,"title")&&n==d->page->root)return quote(d->page->title);
    if(!strcmp(key,"href"))return quote(n->href?n->href:"");
    if(!strcmp(key,"isConnected"))return boolean(attached(n));
    if(!strcmp(key,"activeElement"))return number(d->page->focus?d->page->focus->uid:0);
    return NULL;
}
static char *host(void *opaque,unsigned op,size_t argc,const char *const *argv){
    struct cb_dom *d=opaque;struct cb_page *p=d->page;struct node *n=argc?node(d,argv[0]):NULL;
    if(op==1&&argc==3&&n&&strlen(argv[1])<=1024){GString *out=g_string_new("[");unsigned count=0;query_nodes(n,argv[1],!strcmp(argv[2],"id"),0,out,&count);g_string_append_c(out,']');return g_string_free(out,FALSE);}
    if(op==2&&argc==2&&n)return get(d,n,argv[1]);
    if(op==3&&argc==3&&n){
        const char *key=argv[1],*value=argv[2];size_t length=strlen(value);if(!g_utf8_validate(value,-1,NULL))return NULL;
        if(!strcmp(key,"textContent")||!strcmp(key,"data")){
            if(length>TEXT_LIMIT||d->text_bytes+length>4*TEXT_LIMIT)return NULL;
            d->text_bytes+=length;
            if(n->text){g_free(n->text);n->text=g_strdup(value);}else{while(n->first)detach(n->first);if(length){struct node *c=cb_engine_new_node(p,n,"#text");if(!c)return NULL;c->text=g_strdup(value);}}
            if(n->control&&(n->control->kind==C_BUTTON||n->control->kind==C_SUBMIT)){g_free(n->control->label);n->control->label=g_strdup(value);}
        }else if(!strcmp(key,"value")&&n->control){if(length>VALUE_LIMIT)return NULL;struct control *c=n->control;
            if(c->kind==C_SELECT){for(unsigned i=0;i<c->option_count;i++)if(!strcmp(c->options[i].value,value))c->selected=(int)i;}
            else{g_free(c->value);c->value=cb_engine_bounded_value(value,c->kind==C_TEXTAREA);c->cursor=strlen(c->value);c->select_all=0;}
        }else if(!strcmp(key,"checked")&&n->control)n->control->checked=!strcmp(value,"true");
        else if(!strcmp(key,"title")&&n==p->root){if(length>1024)return NULL;g_free(p->title);p->title=g_strdup(value);}
        else if(set_attr(d,n,key,(!strcmp(key,"disabled")||!strcmp(key,"hidden"))&&!strcmp(value,"false")?NULL:value))return NULL;
        d->dirty=1;return boolean(1);
    }
    if(op==4&&argc>=3&&n){char *name=g_ascii_strdown(argv[2],-1);char *result=NULL;
        if(!strcmp(argv[1],"get"))result=quote(n->attrs?g_hash_table_lookup(n->attrs,name):NULL);
        else if(!strcmp(argv[1],"has"))result=boolean(n->attrs&&g_hash_table_contains(n->attrs,name));
        else if(!strcmp(argv[1],"remove"))result=set_attr(d,n,name,NULL)?NULL:boolean(1);
        else if(!strcmp(argv[1],"set")&&argc==4)result=set_attr(d,n,name,argv[3])?NULL:boolean(1);
        g_free(name);return result;
    }
    if(op==5&&argc==2){const char *tag=argv[0];int text=!strcmp(tag,"#text");if(!text){if(!*tag||strlen(tag)>23)return NULL;for(const char *s=tag;*s;s++)if(!cb_engine_name_char(*s))return NULL;}
        if(strlen(argv[1])>TEXT_LIMIT||d->text_bytes+strlen(argv[1])>4*TEXT_LIMIT)return NULL;
        char *lower=g_ascii_strdown(tag,-1);struct node *made=cb_engine_new_node(p,NULL,lower);g_free(lower);if(!made)return NULL;
        if(text){made->text=g_strdup(argv[1]);d->text_bytes+=strlen(argv[1]);}return number(made->uid);
    }
    if(op==6&&argc==3&&n){struct node *child=node(d,argv[1]),*before=node(d,argv[2]);if(!child||child==p->root||n->text)return NULL;
        if(!strcmp(argv[2],"remove")){if(child->parent!=n)return NULL;detach(child);}
        else {if(before&&before->parent!=n)return NULL;unsigned depth=0;for(struct node *at=n;at;at=at->parent){if(at==child)return NULL;depth++;}if(depth+subtree_depth(child)>DEPTH_LIMIT)return NULL;if(before!=child)append(n,child,before);refresh_controls(d);}
        refresh_controls(d);if(p->focus&&!attached(p->focus))p->focus=NULL;
        d->dirty=1;return boolean(1);
    }
    if(op==7&&argc==1){char *url=cb_engine_resolve_url(p,argv[0]),*out=quote(url);g_free(url);return out;}
    if(op==8&&argc==0)return quote(p->base);
    if(op==9&&argc==4){unsigned id=(unsigned)strtoul(argv[1],NULL,10);for(unsigned i=0;i<d->timer_count;i++)if(d->timers[i].id==id){d->timers[i]=d->timers[--d->timer_count];break;}
        if(!strcmp(argv[0],"clear"))return boolean(1);
        double delay=strtod(argv[2],NULL);if(!isfinite(delay))delay=1;delay=CLAMP(delay,1,60000)/1000.;if(d->timer_count>=64)return NULL;
        d->timers[d->timer_count++]=(struct timer){id,now()+delay,delay,!strcmp(argv[3],"true")};return boolean(1);
    }
    if(op==10&&argc==3){unsigned id=(unsigned)strtoul(argv[0],NULL,10);char target[4096];
        if(d->fetch_count>=32||cb_url_resolve(p->base,argv[1],target))return NULL;
        d->fetches[d->fetch_count++]=id;emit(d,CB_WEB_REQUEST,id,!strcmp(argv[2],"omit")?0:1,target,-1);return boolean(1);
    }
    if(op==11&&argc==4){struct cb_storage_write *write=g_new0(struct cb_storage_write,1);write->version=1;write->local=!strcmp(argv[0],"local");write->operation=!strcmp(argv[1],"set")?0:!strcmp(argv[1],"remove")?1:2;
        if(!cb_engine_remote_url(p->base)||cb_policy_opaque(p->policy)){g_free(write);return NULL;}
        if(strlen(argv[2])>=sizeof write->key||strlen(argv[3])>=sizeof write->value){g_free(write);return NULL;}strcpy(write->key,argv[2]);strcpy(write->value,argv[3]);
        int fd=cb_document_fd((const char *)write,sizeof *write);g_free(write);if(fd<0)return NULL;emit(d,CB_STORAGE_WRITE,0,0,NULL,fd);return boolean(1);
    }
    return NULL;
}
static int dispatch(struct cb_dom *d,struct node *n,const char *type,const char *key){
    if(!d||!n||cb_js_failed(d->js))return 0;
    char *t=quote(type),*k=quote(key?key:"");char *args=g_strdup_printf("[%u,%s,%s]",n->uid,t,k),*result=NULL;g_free(t);g_free(k);
    int rc=cb_js_call(d->js,"__cubeDispatch",args,&result),cancel=!rc&&result&&!strcmp(result,"false");g_free(args);free(result);cb_js_jobs(d->js);return cancel;
}
static void normalize_document(struct cb_page *p){
    struct node *html=NULL,*body=NULL,*head=NULL;for(struct node *c=p->root->first;c;c=c->next)if(cb_engine_tag_is(c,"html")){html=c;break;}
    if(!html){html=cb_engine_new_node(p,NULL,"html");if(!html)return;while(p->root->first)append(html,p->root->first,NULL);append(p->root,html,NULL);}
    for(struct node *c=html->first;c;c=c->next){if(cb_engine_tag_is(c,"body"))body=c;else if(cb_engine_tag_is(c,"head"))head=c;}
    if(!head)head=cb_engine_new_node(p,html,"head");
    if(!body)body=cb_engine_new_node(p,html,"body");
    if(!body||!head)return;
    for(struct node *c=html->first,*next;c;c=next){next=c->next;if(c==head||c==body)continue;append(cb_engine_in_words("title style meta link",c->tag)?head:body,c,NULL);}p->has_body=1;
}
struct cb_dom *cb_dom_new(struct cb_page *page,int socket,const char *storage,int allow_eval){
    if(!page||!page->script_count)return NULL;
    struct cb_dom *d=g_new0(struct cb_dom,1);d->page=page;d->socket=socket;d->last_state=-1;normalize_document(page);refresh_controls(d);
    d->js=cb_js_new((struct cb_js_host){d,host,log_message},allow_eval);if(!d->js){g_free(d);return NULL;}
    if(cb_js_eval(d->js,cb_web_bootstrap,strlen(cb_web_bootstrap),"<web APIs>",NULL)||cb_js_call(d->js,"__cubeInit",storage?storage:"[{},{}]",NULL)){cb_dom_free(d);return NULL;}
    cb_dom_resource(d);return d;
}
void cb_dom_free(struct cb_dom *d){if(d){cb_js_free(d->js);g_free(d);}}
void cb_dom_resource(struct cb_dom *d){
    if(!d||d->loaded||cb_js_failed(d->js))return;
    for(int deferred=0;deferred<2;deferred++)for(size_t i=0;i<d->page->script_count;i++){
        struct page_script *s=&d->page->scripts[i];if(s->done||!!s->defer!=deferred)continue;
        if(s->unsupported){s->done=1;log_message(d,1,"Unsupported script type or script limit exceeded");continue;}
        const char *source=s->source,*url=d->page->base;
        if(s->resource!=SIZE_MAX){struct resource *r=&d->page->resources[s->resource];url=r->url;if(!cb_policy_allows(d->page->policy,d->page->base,url,CB_POLICY_SCRIPT,0)){s->done=1;continue;}if(r->failed){s->done=1;continue;}if(!r->loaded)return;source=r->css;}
        else if(cb_engine_remote_url(url)?!cb_policy_allows(d->page->policy,d->page->base,url,CB_POLICY_SCRIPT,1):!g_str_has_prefix(url,"file:///")){s->done=1;continue;}
        s->done=1;if(source)cb_js_eval(d->js,source,strlen(source),url,NULL);cb_js_jobs(d->js);if(cb_js_failed(d->js))return;
    }
    d->loaded=1;cb_js_call(d->js,"__cubeLoaded","[]",NULL);cb_js_jobs(d->js);d->dirty=1;
}
void cb_dom_tick(struct cb_dom *d){
    if(!d)return;
    cb_dom_resource(d);
    if(!cb_js_failed(d->js))for(unsigned i=0,fired=0;i<d->timer_count&&fired<16;){struct timer t=d->timers[i];if(now()<t.due){i++;continue;}if(t.interval)d->timers[i++].due=now()+t.delay;else d->timers[i]=d->timers[--d->timer_count];
        char args[32];snprintf(args,sizeof args,"[%u]",t.id);cb_js_call(d->js,"__cubeTimer",args,NULL);cb_js_jobs(d->js);fired++;}
    if(d->dirty){emit(d,CB_WEB_DIRTY,0,0,NULL,-1);d->dirty=0;}
    int busy=d->timer_count||d->fetch_count;int state=busy|(d->loaded?2:0)|(cb_js_failed(d->js)?4:0);
    if(state!=d->last_state){emit(d,CB_WEB_STATE,cb_js_failed(d->js)?0:busy,d->loaded||cb_js_failed(d->js),NULL,-1);d->last_state=state;}
}
void cb_dom_response(struct cb_dom *d,const struct cb_message *m){
    if(!d||m->type!=CB_WEB_RESPONSE)return;
    unsigned at=d->fetch_count;for(unsigned i=0;i<d->fetch_count;i++)if(d->fetches[i]==m->a){at=i;break;}if(at==d->fetch_count)return;
    d->fetches[at]=d->fetches[--d->fetch_count];char *data=NULL;size_t size=0;int valid=m->fd>=0&&!cb_read_document(m->fd,&data,&size)&&size<=1024*1024;
    char *body=valid?quote_body(data,size):NULL;if(!body){valid=0;body=g_strdup("\"\"");}
    char *url=quote(m->text);char *args=g_strdup_printf("[%u,%u,%s,%s]",m->a,valid?m->b:0,body,url);g_free(body);g_free(url);free(data);cb_js_call(d->js,"__cubeFetch",args,NULL);g_free(args);cb_js_jobs(d->js);
}
int cb_dom_interact(struct cb_dom *d,unsigned action,double x,double y,const char *text,struct cb_interaction *out){
    struct cb_page *p=d->page;struct node *target=p->focus,*before=p->focus;d->gesture=1;
    if(action==CB_INTERACT_CLICK){target=NULL;unsigned best=0;
        for(guint i=0;i<p->nodes->len;i++){struct node *n=g_ptr_array_index(p->nodes,i);
            if(n->tag[0]=='#'||!attached(n)||!n->laid_out||n->clipped_out||x<n->x||x>=n->x+n->w||y<n->y||y>=n->y+n->h)continue;
            unsigned depth=0;for(struct node *at=n;at;at=at->parent)depth++;if(depth>=best){best=depth;target=n;}}
        for(size_t i=p->hit_count;i>0;i--){struct hit *hit=&p->hits[i-1];if(x>=hit->x&&x<hit->x+hit->w&&y>=hit->y&&y<hit->y+hit->h){target=(struct node *)hit->node;break;}}
    }

    const char *event=action==CB_INTERACT_CLICK?"click":action==CB_INTERACT_KEY?"keydown":action==CB_INTERACT_TEXT?"beforeinput":NULL;
    if(event&&dispatch(d,target,event,text)){memset(out,0,sizeof *out);out->kind=CB_INTERACTION_UPDATE;out->focus=cb_engine_focus_kind(p->focus);out->y=-1;d->gesture=0;return 0;}
    if(action==CB_INTERACT_KEY&&target&&(!strcmp(text,"enter")||!strcmp(text,"space"))&&
       (target->href||(target->control&&!cb_engine_editable(target->control)))&&dispatch(d,target,"click","")){
        memset(out,0,sizeof *out);out->kind=CB_INTERACTION_UPDATE;out->y=-1;d->gesture=0;return 0;
    }
    int rc=cb_page_interact(p,action,x,y,text,out);
    if(!rc&&(out->kind==CB_INTERACTION_SUBMIT||out->kind==CB_INTERACTION_POST)){
        struct control *c=p->focus?p->focus->control:NULL;
        if(c&&c->form&&dispatch(d,c->form,"submit","")){out->kind=CB_INTERACTION_UPDATE;out->text[0]=out->body[0]=0;}
        else if(c)cb_engine_submit_form(p,c,out);
    }
    if(!rc&&out->kind==CB_INTERACTION_NOTICE&&p->focus&&p->focus->control&&p->focus->control->kind==C_BUTTON){out->kind=CB_INTERACTION_UPDATE;out->text[0]=0;}
    if(before!=p->focus){dispatch(d,before,"blur","");dispatch(d,p->focus,"focus","");}
    if(action==CB_INTERACT_TEXT&&out->kind==CB_INTERACTION_UPDATE){dispatch(d,p->focus,"input",text);}
    if(action==CB_INTERACT_CLICK&&p->focus&&p->focus->control&&(p->focus->control->kind==C_CHECKBOX||p->focus->control->kind==C_RADIO)){dispatch(d,p->focus,"input","");dispatch(d,p->focus,"change","");}
    if(action==CB_INTERACT_KEY)dispatch(d,target,"keyup",text);
    d->gesture=0;return rc;
}
