#include "engine-internal.h"

char *cb_engine_decode(const char *s, size_t len) {
    GString *out = g_string_sized_new(len);
    for (size_t i = 0; i < len; i++) {
        if (s[i] == '&') {
            size_t end = i + 1;
            while (end < len && end - i < 32 && s[end] != ';' &&
                   !g_ascii_isspace(s[end]) && s[end] != '<') end++;
            if (end < len && s[end] == ';') {
                gunichar cp = 0;
                char entity[33];
                size_t n = end - i - 1;
                memcpy(entity, s + i + 1, n); entity[n] = 0;
                if (entity[0] == '#') {
                    char *tail;
                    const char *digits = entity + 1;
                    int base = 10;
                    if (*digits == 'x' || *digits == 'X') { base = 16; digits++; }
                    errno = 0;
                    unsigned long v = strtoul(digits, &tail, base);
                    if (tail != digits && !*tail) {
                        cp = !errno && v <= 0x10ffff && !(v >= 0xd800 && v <= 0xdfff)
                             && v != 0 ? (gunichar)v : 0xfffd;
                    }
                } else {
                    static const struct { const char *name; gunichar cp; } entities[] = {
                        {"amp", '&'}, {"lt", '<'}, {"gt", '>'}, {"quot", '"'},
                        {"apos", '\''}, {"nbsp", 0xa0}, {"copy", 0xa9}, {"reg", 0xae},
                        {"ndash", 0x2013}, {"mdash", 0x2014}, {"hellip", 0x2026},
                        {"lsquo", 0x2018}, {"rsquo", 0x2019}, {"ldquo", 0x201c},
                        {"rdquo", 0x201d}, {"bull", 0x2022}, {"trade", 0x2122},
                        {"euro", 0x20ac}, {"times", 0xd7}, {"divide", 0xf7}
                    };
                    for (size_t e = 0; e < G_N_ELEMENTS(entities); e++)
                        if (!strcmp(entity, entities[e].name)) { cp = entities[e].cp; break; }
                }
                if (cp) { g_string_append_unichar(out, cp); i = end; continue; }
            }
        }
        if (!s[i]) g_string_append_unichar(out, 0xfffd);
        else g_string_append_c(out, s[i]);
    }
    char *valid = g_utf8_make_valid(out->str, out->len);
    g_string_free(out, TRUE);
    return valid;
}

struct node *cb_engine_new_node(struct cb_page *p, struct node *parent, const char *tag) {
    if (p->node_count >= NODE_LIMIT) {p->parse_incomplete=1;return NULL;}
    struct node *n = g_new0(struct node, 1);
    n->page = p;
    n->uid=(unsigned)p->node_count+1;
    if(!p->nodes)p->nodes=g_ptr_array_new();
    g_ptr_array_add(p->nodes,n);
    g_strlcpy(n->tag, tag, sizeof n->tag);
    n->parent = parent;
    n->resource = SIZE_MAX;
    n->maxlength = VALUE_LIMIT;n->colspan=1;n->rowspan=1;
    if (parent) {
        if (parent->last) parent->last->next = n;
        else parent->first = n;
        parent->last = n;
    }
    p->node_count++;
    return n;
}
int cb_engine_is_block_tag(const char *tag) {
    return cb_engine_in_words("html body div p section article aside main nav header footer h1 h2 h3 h4 h5 h6 ul ol li pre blockquote form fieldset legend table thead tbody tfoot tr td th dl dt dd figure figcaption hr address details summary", tag);
}
int cb_engine_is_void(const char *tag) {
    return cb_engine_in_words("area base br col embed hr img input link meta param source track wbr", tag);
}
char *cb_engine_resolve_url(struct cb_page *p, const char *href) {
    if (!href || !*href) return NULL;
    char *clean = g_strdup(href);
    g_strstrip(clean);
    for (char *s = clean; *s; s++) if ((unsigned char)*s < 32) { g_free(clean); return NULL; }
    const char *scheme = g_uri_peek_scheme(clean);
    if (scheme && g_ascii_strcasecmp(scheme, "http") && g_ascii_strcasecmp(scheme, "https") &&
        g_ascii_strcasecmp(scheme, "file") && g_ascii_strcasecmp(scheme, "about") &&
        g_ascii_strcasecmp(scheme, "cube")) { g_free(clean); return NULL; }
    char *url = g_uri_resolve_relative(p->base, clean, G_URI_FLAGS_PARSE_RELAXED, NULL);
    if (!url && scheme) url = g_strdup(clean);
    g_free(clean);
    return url;
}

/* Subset selectors: compound tag/id/class and descendant or child combinators. */
int cb_engine_remote_url(const char *url) {
    const char *scheme=g_uri_peek_scheme(url);
    return scheme && (!g_ascii_strcasecmp(scheme,"http") || !g_ascii_strcasecmp(scheme,"https"));
}
size_t cb_engine_discover_resource(struct cb_page *p, int kind, const char *value) {
    if(!cb_engine_remote_url(p->base))return SIZE_MAX;
    char *url=cb_engine_resolve_url(p,value);
    if(!url || !cb_engine_remote_url(url)){g_free(url);return SIZE_MAX;}
    /* URL fragments do not identify distinct HTTP fetches. */
    char *fragment=strchr(url,'#');if(fragment)*fragment=0;
    for(size_t i=0;i<p->resource_count;i++) {
        if(p->resources[i].kind==kind && !strcmp(p->resources[i].url,url)){g_free(url);return i;}
    }
    if(p->resource_count>=RESOURCE_LIMIT){g_free(url);return SIZE_MAX;}
    size_t index=p->resource_count++;
    p->resources[index]=(struct resource){.kind=kind,.url=url};
    return index;
}
size_t cb_page_resource_count(const struct cb_page *p) {return p?p->resource_count:0;}
int cb_page_resource(const struct cb_page *p, size_t index, int *kind, const char **url) {
    if(!p || index>=p->resource_count || !kind || !url)return -1;
    *kind=p->resources[index].kind;*url=p->resources[index].url;return 0;
}
int cb_engine_mime_is(const char *mime, const char *expected) {
    if(!mime)return 0;
    while(g_ascii_isspace(*mime))mime++;
    size_t length=strcspn(mime,"; \t\r\n");
    return strlen(expected)==length && !g_ascii_strncasecmp(mime,expected,length);
}
int cb_page_set_resource(struct cb_page *p, size_t index, const void *bytes, size_t length, const char *mime) {
    if(!p || index>=p->resource_count || !bytes)return -1;
    struct resource *resource=&p->resources[index];
    if(resource->kind==CB_RESOURCE_SCRIPT){
        if(length>1024*1024||memchr(bytes,0,length)||(!cb_engine_mime_is(mime,"text/javascript")&&!cb_engine_mime_is(mime,"application/javascript")))return -1;
        g_free(resource->css);resource->css=g_utf8_make_valid(bytes,length);resource->css_length=strlen(resource->css);resource->loaded=1;return 0;
    }
    if(resource->kind==CB_RESOURCE_CSS) {
        if(!cb_engine_mime_is(mime,"text/css") || length>CSS_LIMIT ||
           length>CSS_LIMIT-(p->css_bytes-resource->css_length) || memchr(bytes,0,length))return -1;
        char *css=g_utf8_make_valid(bytes,length);
        size_t valid_length=strlen(css);
        if(valid_length>CSS_LIMIT-(p->css_bytes-resource->css_length)){g_free(css);return -1;}
        p->css_bytes=p->css_bytes-resource->css_length+valid_length;
        g_free(resource->css);resource->css=css;resource->css_length=valid_length;
        cb_engine_rebuild_rules(p);return 0;
    }
    if(resource->kind==CB_RESOURCE_IMAGE) {
        const char *type=cb_engine_mime_is(mime,"image/png")?"image/png":cb_engine_mime_is(mime,"image/jpeg")?"image/jpeg":cb_engine_mime_is(mime,"image/svg+xml")?"image/svg+xml":NULL;
        if(!type)return -1;
        char error[128];
        cairo_surface_t *image=cb_image_decode(bytes,length,type,error,sizeof error);
        if(!image)return -1;
        size_t pixels=(size_t)cairo_image_surface_get_width(image)*cairo_image_surface_get_height(image);
        size_t previous=resource->image?(size_t)cairo_image_surface_get_width(resource->image)*cairo_image_surface_get_height(resource->image):0;
        if(pixels>16u*1024u*1024u-(p->image_pixels-previous)){cairo_surface_destroy(image);return -1;}
        p->image_pixels=p->image_pixels-previous+pixels;
        if(resource->image)cairo_surface_destroy(resource->image);
        resource->image=image;return 0;
    }
    return -1;
}
/* Avoid print-only fetches; dimension/preference conditions are retained even
 * when inactive so resizing never changes the immutable resource catalog. */
int cb_engine_screen_media(const char *media) {
    if(!media || !*media)return 1;
    if(strlen(media)>MEDIA_QUERY_LIMIT)return 0;
    char *copy=g_ascii_strdown(media,-1),*save=NULL;int possible=0;
    for(char *part=strtok_r(copy,",",&save);part;part=strtok_r(NULL,",",&save)) {
        part=cb_engine_trim(part);
        if(!strncmp(part,"only",4) && g_ascii_isspace(part[4]))part=cb_engine_trim(part+4);
        if(strncmp(part,"print",5) || (part[5] && !g_ascii_isspace(part[5]))) {possible=1;break;}
    }
    g_free(copy);return possible;
}

unsigned cb_engine_image_dimension(const char *value, size_t length) {
    if(!length || length>12)return 0;
    char number[13];memcpy(number,value,length);number[length]=0;
    char *end;errno=0;unsigned long result=strtoul(number,&end,10);
    if(errno || end==number || *end || result>16384)return 0;
    return (unsigned)result;
}

unsigned cb_engine_parse_span(const char *value,size_t length,int row) {
    if(!length || length>12)return TABLE_COLUMN_LIMIT+1;
    char number[13];memcpy(number,value,length);number[length]=0;char *start=g_strstrip(number),*end;
    if(*start=='-')return 1;
    errno=0;unsigned long v=strtoul(start,&end,10);
    if(errno)return TABLE_COLUMN_LIMIT+1;
    if(end==start || *end)return 1;
    if(!v)return row?0:1;
    return (unsigned)MIN(v,TABLE_COLUMN_LIMIT+1);
}

unsigned cb_engine_parse_maxlength(const char *value,size_t length) {
    if(!length || length>32)return VALUE_LIMIT;
    char number[33];memcpy(number,value,length);number[length]=0;char *s=g_strstrip(number);
    if(!*s || *s=='-')return VALUE_LIMIT;
    char *end;errno=0;unsigned long long limit=strtoull(s,&end,10);
    if(errno || end==s || *end)return VALUE_LIMIT;
    return (unsigned)MIN(limit,VALUE_LIMIT);
}

void cb_engine_init_controls(struct cb_page *p, struct node *n);

struct cb_page *cb_page_parse(const char *html, size_t len, const char *base_url) {
    if (!html) return NULL;
    struct cb_page *p = g_new0(struct cb_page, 1);
    p->base = g_strdup(base_url && *base_url ? base_url : "about:blank");
    p->title = g_strdup("Untitled");
    p->zoom = 1;p->width=1024;p->viewport_height=800;
    p->parsing = 1;
    p->parse_incomplete=len>HTML_LIMIT || memchr(html,0,MIN(len,HTML_LIMIT))!=NULL;
    p->root = cb_engine_new_node(p, NULL, "#document");
    struct node *stack[DEPTH_LIMIT + 1] = {p->root};
    unsigned depth = 0, overflow = 0;
    char *input = cb_engine_slice(html, MIN(len, HTML_LIMIT));
    char *s = input;
    while (*s && p->node_count < NODE_LIMIT) {
        if (*s != '<') {
            char *end = strchr(s, '<'); if (!end) end = s + strlen(s);
            if (!overflow && end > s) {
                struct node *n = cb_engine_new_node(p, stack[depth], "#text");
                if (n) n->text = cb_engine_decode(s, (size_t)(end - s));
            }
            s = end; continue;
        }
        if (!strncmp(s, "<!--", 4)) {
            char *end = strstr(s + 4, "-->"); s = end ? end + 3 : s + strlen(s); continue;
        }
        if (s[1] == '!' || s[1] == '?') {
            char *end = strchr(s + 2, '>'); s = end ? end + 1 : s + strlen(s); continue;
        }
        char *start = s++;
        int closing = *s == '/'; if (closing) s++;
        if (!g_ascii_isalpha(*s)) {
            if (!overflow) { struct node *n = cb_engine_new_node(p, stack[depth], "#text"); if (n) n->text = g_strdup("<"); }
            s = start + 1; continue;
        }
        char tag[24]; size_t ti = 0;
        while (cb_engine_name_char(*s) || *s == ':') { if (ti < sizeof tag - 1) tag[ti++] = g_ascii_tolower(*s); s++; }
        tag[ti] = 0;
        if (closing) {
            char *end = strchr(s, '>'); s = end ? end + 1 : s + strlen(s);
            if (overflow) { overflow--; continue; }
            for (unsigned d = depth; d > 0; d--) if (cb_engine_tag_is(stack[d], tag)) { depth = d - 1; break; }
            continue;
        }
        struct node scratch = {.maxlength=VALUE_LIMIT,.colspan=1,.rowspan=1};
        int self_closing = 0;
        while (*s && *s != '>') {
            while (g_ascii_isspace(*s)) s++;
            if (*s == '>') break;
            if (*s == '/' && s[1] == '>') { self_closing = 1; s++; break; }
            char name[32]; size_t ni = 0;
            while (*s && !g_ascii_isspace(*s) && *s != '=' && *s != '>' && *s != '/') {
                if (ni < sizeof name - 1) name[ni++] = g_ascii_tolower(*s);
                s++;
            }
            name[ni] = 0;
            if (!ni) { if (*s && *s != '>') s++; continue; }
            while (g_ascii_isspace(*s)) s++;
            const char *value = ""; size_t vn = 0;
            if (*s == '=') {
                s++; while (g_ascii_isspace(*s)) s++;
                char quote = (*s == '\'' || *s == '"') ? *s++ : 0;
                value = s;
                if (quote) while (*s && *s != quote) s++;
                else while (*s && !g_ascii_isspace(*s) && *s != '>') s++;
                if((size_t)(s-value)>ATTR_LIMIT)p->parse_incomplete=1;
                vn = MIN((size_t)(s - value), ATTR_LIMIT);
                if (quote && *s) s++;
            }
            char **target = NULL;
            if (!strcmp(name, "id")) target = &scratch.id;
            else if (!strcmp(name, "class")) target = &scratch.classes;
            else if (!strcmp(name, "style")) target = &scratch.inline_css;
            else if (!strcmp(name, "href")) target = &scratch.href;
            else if (!strcmp(name, "alt")) target = &scratch.alt;
            else if (!strcmp(name, "src")) target = &scratch.src;
            else if (!strcmp(name, "rel")) target = &scratch.rel;
            else if (!strcmp(name, "media")) target = &scratch.media;
            else if (!strcmp(name, "name")) target = &scratch.name;
            else if (!strcmp(name, "value")) target = &scratch.value;
            else if (!strcmp(name, "type")) target = &scratch.type;
            else if (!strcmp(name, "action")) target = &scratch.action;
            else if (!strcmp(name, "method")) target = &scratch.method;
            else if (!strcmp(name, "placeholder")) target = &scratch.placeholder;
            else if (!strcmp(name, "form")) target = &scratch.external_form;
            else if (!strcmp(name, "formaction")) target = &scratch.submit_action;
            else if (!strcmp(name, "formmethod")) target = &scratch.submit_method;
            else if (!strcmp(name, "maxlength")) scratch.maxlength=cb_engine_parse_maxlength(value,vn);
            else if (!strcmp(name, "readonly")) scratch.readonly=1;
            else if (!strcmp(name, "checked")) scratch.checked=1;
            else if (!strcmp(name, "selected")) scratch.selected=1;
            else if (!strcmp(name, "multiple")) scratch.multiple=1;
            else if (!strcmp(name, "tabindex")) scratch.tab_skip=vn && value[0]=='-';
            else if (!strcmp(name,"colspan")) scratch.colspan=cb_engine_parse_span(value,vn,0);
            else if (!strcmp(name,"rowspan")) scratch.rowspan=cb_engine_parse_span(value,vn,1);
            else if (!strcmp(name, "width")) scratch.image_width=cb_engine_image_dimension(value,vn);
            else if (!strcmp(name, "height")) scratch.image_height=cb_engine_image_dimension(value,vn);
            else if (!strcmp(name, "disabled")) scratch.disabled=1;
            else if (!strcmp(name, "hidden")) scratch.hidden = 1;
            if (target && !*target) *target = cb_engine_decode(value, vn);
            if (!scratch.attrs) scratch.attrs=g_hash_table_new_full(g_str_hash,g_str_equal,g_free,g_free);
            if (g_hash_table_size(scratch.attrs)<64 && !g_hash_table_contains(scratch.attrs,name))
                g_hash_table_insert(scratch.attrs,g_strdup(name),cb_engine_decode(value,vn));
        }
        if (*s == '>') s++;
        int raw = !strcmp(tag, "script") || !strcmp(tag, "style") || !strcmp(tag, "title");
        if (raw) {
            char closer[32]; snprintf(closer, sizeof closer, "</%s", tag);
            const char *end = cb_engine_find_ci(s, closer);
            while (end && cb_engine_name_char(end[strlen(closer)])) end = cb_engine_find_ci(end + 2, closer);
            if (!end) end = s + strlen(s);
            if (!overflow && !strcmp(tag, "style") && !scratch.disabled && cb_engine_screen_media(scratch.media)) cb_engine_add_css_media(p,s,(size_t)(end-s),scratch.media);
            if(!overflow&&p->script_count<64&&!strcmp(tag,"script")){
                struct node *n=cb_engine_new_node(p,stack[depth],tag);
                if(n){n->attrs=scratch.attrs;scratch.attrs=NULL;struct node *text=cb_engine_new_node(p,n,"#text");
                    size_t length=(size_t)(end-s);if(text)text->text=g_utf8_make_valid(s,length);
                    const char *type=n->attrs?g_hash_table_lookup(n->attrs,"type"):NULL;
                    struct page_script *script=&p->scripts[p->script_count++];script->node=n;script->resource=SIZE_MAX;
                    script->unsupported=(type&&*type&&g_ascii_strcasecmp(type,"text/javascript")&&g_ascii_strcasecmp(type,"application/javascript"))||length>1024*1024-p->script_bytes;
                    script->defer=n->attrs&&g_hash_table_contains(n->attrs,"defer");
                    if(n->attrs&&g_hash_table_contains(n->attrs,"async"))script->defer=1;
                    if(!script->unsupported&&scratch.src)script->resource=cb_engine_discover_resource(p,CB_RESOURCE_SCRIPT,scratch.src);
                    else if(!script->unsupported){script->source=g_utf8_make_valid(s,length);p->script_bytes+=length;}
                    if(scratch.src&&script->resource==SIZE_MAX)script->unsupported=1;
                }
            }
            if (!overflow && !strcmp(tag, "title")) {
                g_free(p->title); p->title = cb_engine_decode(s, MIN((size_t)(end - s), (size_t)1024)); g_strstrip(p->title);
            }
            s = (char *)end;
            if (*s) { char *finish = strchr(s, '>'); s = finish ? finish + 1 : s + strlen(s); }
        } else if (overflow || depth >= DEPTH_LIMIT) {
            p->parse_incomplete=1;
            if (!cb_engine_is_void(tag) && !self_closing) overflow++;
        } else {
            /* Implied end tags also close intervening inline descendants. */
            for (unsigned d=depth;d>0;d--) {
                const char *open=stack[d]->tag;
                if ((!strcmp(open,"p") && cb_engine_is_block_tag(tag)) ||
                    (!strcmp(open,"li") && !strcmp(tag,"li")) ||
                    (cb_engine_in_words("dt dd",open) && cb_engine_in_words("dt dd",tag)) ||
                    (!strcmp(open,"option") && cb_engine_in_words("option optgroup",tag)) ||
                    (cb_engine_in_words("h1 h2 h3 h4 h5 h6",open) && cb_engine_in_words("h1 h2 h3 h4 h5 h6",tag))) {depth=d-1;break;}
                if (cb_engine_in_words("table ul ol dl select body",open)) break;
            }
            if(!strcmp(tag,"tr") || !strcmp(tag,"td") || !strcmp(tag,"th")) {
                for(unsigned d=depth;d>0;d--) {
                    if(cb_engine_tag_is(stack[d],"table"))break;
                    if((!strcmp(tag,"tr") && cb_engine_tag_is(stack[d],"tr")) ||
                       ((!strcmp(tag,"td") || !strcmp(tag,"th")) && (cb_engine_tag_is(stack[d],"td") || cb_engine_tag_is(stack[d],"th")))){depth=d-1;break;}
                    if(strcmp(tag,"tr") && cb_engine_tag_is(stack[d],"tr"))break;
                }
            }
            struct node *n = cb_engine_new_node(p, stack[depth], tag);
            if (n) {
                n->attrs=scratch.attrs;scratch.attrs=NULL;
                n->id = scratch.id; scratch.id = NULL;
                n->classes = scratch.classes; scratch.classes = NULL;
                n->inline_css = scratch.inline_css; scratch.inline_css = NULL;
                n->alt = scratch.alt; scratch.alt = NULL;
                n->hidden = scratch.hidden;n->disabled=scratch.disabled;
                n->readonly=scratch.readonly;n->checked=scratch.checked;n->selected=scratch.selected;
                n->multiple=scratch.multiple;n->tab_skip=scratch.tab_skip;n->maxlength=scratch.maxlength;
                n->name=scratch.name;scratch.name=NULL;n->value=scratch.value;scratch.value=NULL;
                n->type=scratch.type;scratch.type=NULL;n->action=scratch.action;scratch.action=NULL;
                n->method=scratch.method;scratch.method=NULL;n->placeholder=scratch.placeholder;scratch.placeholder=NULL;
                n->external_form=scratch.external_form;scratch.external_form=NULL;
                n->submit_action=scratch.submit_action;scratch.submit_action=NULL;
                n->submit_method=scratch.submit_method;scratch.submit_method=NULL;
                n->image_width=scratch.image_width;n->image_height=scratch.image_height;
                n->colspan=scratch.colspan;n->rowspan=scratch.rowspan;
                if(!strcmp(tag,"img"))n->resource=cb_engine_discover_resource(p,CB_RESOURCE_IMAGE,scratch.src);
                if(!strcmp(tag,"link") && scratch.rel && !scratch.disabled && p->sheet_count<STYLESHEET_LIMIT) {
                    char *rel=g_ascii_strdown(scratch.rel,-1);
                    int screen=cb_engine_screen_media(scratch.media);
                    if(screen && cb_engine_in_words(rel,"stylesheet") && !cb_engine_in_words(rel,"alternate")) {
                        size_t resource=cb_engine_discover_resource(p,CB_RESOURCE_CSS,scratch.href);
                        if(resource!=SIZE_MAX)p->sheets[p->sheet_count++]=(struct stylesheet){NULL,0,resource,scratch.media?g_strdup(scratch.media):NULL};
                    }
                    g_free(rel);
                }
                if (!strcmp(tag, "a")) n->href = cb_engine_resolve_url(p, scratch.href);
                if (!strcmp(tag, "body")) p->has_body = 1;
                if(!strcmp(tag,"textarea")) {
                    const char *end=cb_engine_find_ci(s,"</textarea");
                    while(end && cb_engine_name_char(end[10]))end=cb_engine_find_ci(end+2,"</textarea");
                    if(!end)end=s+strlen(s);
                    struct node *child=cb_engine_new_node(p,n,"#text");
                    if(child)child->text=cb_engine_decode(s,(size_t)(end-s));
                    s=(char *)end;
                    if(*s){char *finish=strchr(s,'>');s=finish?finish+1:s+strlen(s);}
                } else if (!cb_engine_is_void(tag) && !self_closing) stack[++depth] = n;
            }
        }
        g_free(scratch.id); g_free(scratch.classes); g_free(scratch.inline_css);
        g_free(scratch.href); g_free(scratch.alt);
        g_free(scratch.src);g_free(scratch.rel);g_free(scratch.media);
        g_free(scratch.name);g_free(scratch.value);g_free(scratch.type);g_free(scratch.action);
        g_free(scratch.method);g_free(scratch.placeholder);g_free(scratch.external_form);
        g_free(scratch.submit_action);g_free(scratch.submit_method);
        if(scratch.attrs)g_hash_table_unref(scratch.attrs);
    }
    if(*s && p->node_count>=NODE_LIMIT)p->parse_incomplete=1;
    g_free(input);
    p->parsing=0;cb_engine_rebuild_rules(p);cb_engine_init_controls(p,p->root);
    return p;
}

void cb_engine_free_node(struct node *n) {
    struct node *child=n->first;
    while(child){struct node *next=child->next;cb_engine_free_node(child);child=next;}
    if(n->attrs)g_hash_table_unref(n->attrs);
    if(n->custom)g_hash_table_unref(n->custom);
    g_free(n->name);g_free(n->value);g_free(n->type);g_free(n->action);g_free(n->method);
    g_free(n->placeholder);g_free(n->external_form);g_free(n->submit_action);g_free(n->submit_method);
    g_free(n->text);g_free(n->id);g_free(n->classes);g_free(n->inline_css);g_free(n->href);g_free(n->alt);g_free(n);
}
void cb_page_free(struct cb_page *p) {
    if(!p)return;
    cb_engine_clear_layout(p);
    for(size_t i=0;i<p->control_count;i++) {
        struct control *c=&p->controls[i];g_free(c->value);g_free(c->initial);g_free(c->label);
        for(unsigned j=0;j<c->option_count;j++){g_free(c->options[j].label);g_free(c->options[j].value);}
    }
    if(p->nodes){for(guint i=0;i<p->nodes->len;i++){struct node *n=g_ptr_array_index(p->nodes,i);n->first=NULL;cb_engine_free_node(n);}g_ptr_array_free(p->nodes,TRUE);}
    for(size_t i=0;i<p->script_count;i++)g_free(p->scripts[i].source);
    for(size_t i=0;i<p->rule_count;i++){g_free(p->rules[i].selector);g_free(p->rules[i].declarations);}
    g_free(p->rules);g_free(p->base);g_free(p->title);g_free(p->policy);
    for(size_t i=0;i<p->sheet_count;i++){g_free(p->sheets[i].css);g_free(p->sheets[i].media);}
    for(size_t i=0;i<p->resource_count;i++) {
        g_free(p->resources[i].url);g_free(p->resources[i].css);
        if(p->resources[i].image)cairo_surface_destroy(p->resources[i].image);
    }
    if(p->context)g_object_unref(p->context);
    g_free(p);
}

int cb_page_set_url(struct cb_page *p,const char *url) {
    if(!p || !url || strlen(url)>4095 || !g_utf8_validate(url,-1,NULL))return -1;
    for(const char *c=url;*c;c++)if((unsigned char)*c<32 || *c=='\\')return -1;
    GUriFlags flags=G_URI_FLAGS_ENCODED|G_URI_FLAGS_SCHEME_NORMALIZE;
    GUri *old=g_uri_parse(p->base,flags,NULL),*next=g_uri_parse(url,flags,NULL);
    if(!old || !next){if(old)g_uri_unref(old);if(next)g_uri_unref(next);return -1;}
    const char *oh=g_uri_get_host(old),*nh=g_uri_get_host(next);
    int same=!g_uri_get_userinfo(next) && !g_ascii_strcasecmp(g_uri_get_scheme(old),g_uri_get_scheme(next)) &&
        ((!oh&&!nh)||(oh&&nh&&!g_ascii_strcasecmp(oh,nh))) && g_uri_get_port(old)==g_uri_get_port(next) &&
        !g_strcmp0(g_uri_get_path(old),g_uri_get_path(next)) && !g_strcmp0(g_uri_get_query(old),g_uri_get_query(next));
    g_uri_unref(old);g_uri_unref(next);if(!same)return -1;
    g_free(p->base);p->base=g_strdup(url);return 0;
}

void cb_page_set_policy(struct cb_page *p,const char *policy){if(!p)return;g_free(p->policy);p->policy=g_strndup(policy?policy:"",CB_POLICY_LIMIT-1);cb_engine_rebuild_rules(p);}
