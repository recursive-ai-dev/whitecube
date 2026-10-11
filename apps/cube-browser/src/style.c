#include "engine-internal.h"

int cb_engine_name_char(char c) { return g_ascii_isalnum(c) || c == '_' || c == '-'; }
unsigned cb_engine_specificity(const char *s) {
    unsigned result = 0;
    int start = 1;
    while (*s) {
        if (*s == '#') { result += 100; start = 0; }
        else if (*s == '.' || *s == ':' || *s == '[') { result += 10; start = 0; }
        else if (start && cb_engine_name_char(*s)) { result++; start = 0; }
        if (g_ascii_isspace(*s) || *s == '>') start = 1;
        s++;
    }
    return result;
}
static const struct node *previous_element(const struct node *n) {
    const struct node *previous=NULL;
    for(const struct node *c=n->parent?n->parent->first:NULL;c && c!=n;c=c->next)
        if(c->tag[0]!='#')previous=c;
    return previous;
}
static int attribute_matches(const struct node *n,const char *source,size_t length) {
    if(length>1024)return 0;
    char *copy=g_strndup(source,length),*s=g_strstrip(copy),*end=s;
    while(cb_engine_name_char(*end))end++;
    char saved=*end;*end=0;
    const char *value=n->attrs?g_hash_table_lookup(n->attrs,s):NULL;
    *end=saved;while(g_ascii_isspace(*end))end++;
    int match=0;
    if(!*end)match=value!=NULL;
    else if(value){
        char op=*end++;if(op!='='){if(*end++!='=')goto done;}
        while(g_ascii_isspace(*end))end++;
        char quote=(*end=='\''||*end=='"')?*end++:0,*expected=end;
        if(quote){while(*end && *end!=quote)end++;if(!*end)goto done;*end++=0;}
        else {while(*end && !g_ascii_isspace(*end))end++;if(*end)*end++=0;}
        while(g_ascii_isspace(*end))end++;
        int insensitive=!strcmp(end,"i");if(*end && !insensitive && strcmp(end,"s"))goto done;
        char *v=insensitive?g_ascii_strdown(value,-1):g_strdup(value);
        char *e=insensitive?g_ascii_strdown(expected,-1):g_strdup(expected);
        switch(op){case '=':match=!strcmp(v,e);break;case '~':match=*e&&cb_engine_in_words(v,e);break;
            case '^':match=*e&&g_str_has_prefix(v,e);break;case '$':match=*e&&g_str_has_suffix(v,e);break;
            case '*':match=*e&&strstr(v,e)!=NULL;break;case '|':match=!strcmp(v,e)||(g_str_has_prefix(v,e)&&v[strlen(e)]=='-');break;}
        g_free(v);g_free(e);
    }
done:g_free(copy);return match;
}
static int compound_depth(const struct node *n,const char *s,size_t len,unsigned depth) {
    if(!n || !len || n->tag[0]=='#' || depth>8)return 0;
    size_t i=0;
    if(s[i]=='*')i++;
    else if(cb_engine_name_char(s[i])){
        size_t begin=i;while(i<len&&cb_engine_name_char(s[i]))i++;
        if(strlen(n->tag)!=i-begin||g_ascii_strncasecmp(n->tag,s+begin,i-begin))return 0;
    }
    while(i<len){
        char kind=s[i++];
        if(kind=='['){
            size_t begin=i;char quote=0;
            while(i<len){if(quote){if(s[i]==quote)quote=0;}else if(s[i]=='\''||s[i]=='"')quote=s[i];else if(s[i]==']')break;i++;}
            if(i==len||!attribute_matches(n,s+begin,i-begin))return 0;
            i++;continue;
        }
        if(kind!='#'&&kind!='.'&&kind!=':')return 0;
        size_t begin=i;while(i<len&&cb_engine_name_char(s[i]))i++;
        if(i==begin||i-begin>=256)return 0;
        char value[256];memcpy(value,s+begin,i-begin);value[i-begin]=0;
        if(kind=='#'&&(!n->id||strcmp(n->id,value)))return 0;
        if(kind=='.'&&(!n->classes||!cb_engine_in_words(n->classes,value)))return 0;
        if(kind!=':')continue;
        if(!strcmp(value,"not")){
            if(i>=len||s[i++]!='(')return 0;
            begin=i;unsigned nesting=1;
            while(i<len&&nesting){if(s[i]=='(')nesting++;if(s[i]==')')nesting--;if(nesting)i++;}
            if(i==len||compound_depth(n,s+begin,i-begin,depth+1))return 0;
            i++;
        }else if(!strcmp(value,"root")){if(!cb_engine_tag_is(n,"html"))return 0;}
        else if(!strcmp(value,"link")||!strcmp(value,"any-link")){if(!n->href)return 0;}
        else if(!strcmp(value,"first-child")){if(!n->parent||previous_element(n))return 0;}
        else if(!strcmp(value,"last-child")){const struct node *next=n->next;while(next&&next->tag[0]=='#')next=next->next;if(next)return 0;}
        else if(!strcmp(value,"checked")){if(!n->control||!n->control->checked)return 0;}
        else if(!strcmp(value,"disabled")){if(!cb_engine_node_disabled(n))return 0;}
        else if(!strcmp(value,"enabled")){if(!n->control||cb_engine_node_disabled(n))return 0;}
        else if(!strcmp(value,"focus")){if(n->page->focus!=n)return 0;}
        else if(!strcmp(value,"nth-child")){
            if(i>=len||s[i++]!='(')return 0;
            begin=i;while(i<len&&s[i]!=')')i++;
            if(i==len||i-begin>32)return 0;
            char number[33];memcpy(number,s+begin,i-begin);number[i-begin]=0;i++;
            unsigned index=1;for(const struct node *c=previous_element(n);c;c=previous_element(c))index++;
            char *number_end=NULL;long wanted=strtol(number,&number_end,10);
            if(!strcmp(number,"odd")){if(!(index&1))return 0;}
            else if(!strcmp(number,"even")){if(index&1)return 0;}
            else if(number_end==number||*number_end||wanted!=(long)index)return 0;
        }else return 0;
    }
    return 1;
}
int cb_engine_compound_matches(const struct node *n,const char *s,size_t len){return compound_depth(n,s,len,0);}
int cb_engine_selector_matches(const struct node *n,const char *s) {
    if(!s||strlen(s)>512)return 0;
    if(!strpbrk(s," >+~\t\r\n"))return cb_engine_compound_matches(n,s,strlen(s));
    char compounds[64][513];char relations[64]={0};unsigned count=0;size_t start=0,i=0;
    unsigned nesting=0;char quote=0;char relation=' ';
    while(1){char c=s[i];
        if(quote){if(c==quote)quote=0;if(!c)return 0;
            i++;continue;}
        if(c=='\''||c=='"'){quote=c;i++;continue;}
        if(c=='['||c=='(')nesting++;
        if(c==']'||c==')'){if(!nesting)return 0;nesting--;}
        if(!c || (!nesting&&(g_ascii_isspace(c)||c=='>'||c=='+'||c=='~'))){
            if(i>start){if(count>=64)return 0;memcpy(compounds[count],s+start,i-start);compounds[count][i-start]=0;relations[count++]=relation;relation=' ';}
            if(!c)break;
            if(c=='>'||c=='+'||c=='~')relation=c;
            start=i+1;
        }
        i++;
    }
    if(!count||nesting||relation!=' '||!cb_engine_compound_matches(n,compounds[count-1],strlen(compounds[count-1])))return 0;
    while(--count){
        char rel=relations[count];n=(rel=='+'||rel=='~')?previous_element(n):n->parent;
        while(n&&!cb_engine_compound_matches(n,compounds[count-1],strlen(compounds[count-1]))){
            if(rel=='>'||rel=='+')return 0;
            n=rel=='~'?previous_element(n):n->parent;
        }
        if(!n)return 0;
    }
    return 1;
}
struct cb_media_env cb_engine_media_environment(const struct cb_page *p) {
    return (struct cb_media_env){.width=p->width/p->zoom,.height=p->viewport_height/p->zoom,
        .reduced_motion=p->reduced_motion,.strong_contrast=p->strong_contrast};
}
/* Strings/escapes are consumed before delimiters, so quoted braces cannot
 * accidentally end a media block or expose rules inside an unknown at-rule. */
char *cb_engine_css_boundary(char *start,char *end,int closing) {
    char quote=0;unsigned parentheses=0,brackets=0,braces=closing?1:0;
    for(char *c=start;c<end;c++) {
        if(*c=='\\'){if(c+1<end)c++;continue;}
        if(quote){if(*c==quote)quote=0;continue;}
        if(*c=='\'' || *c=='"'){quote=*c;continue;}
        if(closing) {
            if(*c=='{')braces++;
            else if(*c=='}' && !--braces)return c;
        } else {
            if(*c=='(')parentheses++;else if(*c==')' && parentheses)parentheses--;
            else if(*c=='[')brackets++;else if(*c==']' && brackets)brackets--;
            else if(!parentheses && !brackets && (*c=='{' || *c==';' || *c=='}'))return c;
        }
    }
    return end;
}
void cb_engine_parse_css_range(struct cb_page *p,char *start,char *end,unsigned depth) {
    char *s=start;
    while(s<end && p->rule_count<RULE_LIMIT) {
        while(s<end && (g_ascii_isspace(*s) || *s==';'))s++;
        if(s>=end)break;
        char *open=cb_engine_css_boundary(s,end,0);
        if(open==end)break;
        if(*open!='{'){s=open+1;continue;}
        char *close=cb_engine_css_boundary(open+1,end,1);
        if(close==end)break;
        *open=0;
        if(*s=='@') {
            char *name=s+1;char *query=name;
            while(g_ascii_isalpha(*query) || *query=='-')query++;
            if((size_t)(query-name)==5 && !g_ascii_strncasecmp(name,"media",5) &&
               depth<MEDIA_DEPTH_LIMIT && strlen(query)<=MEDIA_QUERY_LIMIT) {
                struct cb_media_env env=cb_engine_media_environment(p);
                if(*cb_engine_trim(query) && cb_media_matches(cb_engine_trim(query),&env))cb_engine_parse_css_range(p,open+1,close,depth+1);
            }
        } else {
            *close=0;
            char *save=NULL;
            for(char *selector=strtok_r(s,",",&save);selector && p->rule_count<RULE_LIMIT;
                selector=strtok_r(NULL,",",&save)) {
                selector=cb_engine_trim(selector);
                if(!*selector || strlen(selector)>512 || strlen(open+1)>16384)continue;
                p->rules=g_realloc_n(p->rules,p->rule_count+1,sizeof *p->rules);
                p->rules[p->rule_count]=(struct rule){g_strdup(selector),g_strdup(open+1),cb_engine_specificity(selector),(unsigned)p->rule_count};
                if(strstr(open+1,"--"))p->custom_rules=1;
                p->rule_count++;
            }
        }
        s=close+1;
    }
}
void cb_engine_parse_css_rules(struct cb_page *p,const char *css,size_t len) {
    if(!p || !css || p->rule_count>=RULE_LIMIT)return;
    len=MIN(len,CSS_LIMIT);char *copy=cb_engine_slice(css,len);char quote=0;
    for(char *c=copy;*c;c++) {
        if(*c=='\\'){if(c[1])c++;continue;}
        if(quote){if(*c==quote)quote=0;continue;}
        if(*c=='\'' || *c=='"'){quote=*c;continue;}
        if(c[0]=='/' && c[1]=='*') {
            char *end=strstr(c+2,"*/");if(!end){*c=0;break;}
            memset(c,' ',(size_t)(end+2-c));c=end+1;
        }
    }
    cb_engine_parse_css_range(p,copy,copy+strlen(copy),0);g_free(copy);
}

void cb_engine_rebuild_rules(struct cb_page *p) {
    for(size_t i=0;i<p->rule_count;i++) {
        g_free(p->rules[i].selector);g_free(p->rules[i].declarations);
    }
    g_free(p->rules);p->rules=NULL;p->rule_count=0;p->custom_rules=0;
    for(size_t i=0;i<p->sheet_count && p->rule_count<RULE_LIMIT;i++) {
        struct stylesheet *sheet=&p->sheets[i];
        if(sheet->media){struct cb_media_env env=cb_engine_media_environment(p);if(!cb_media_matches(sheet->media,&env))continue;}
        if(sheet->resource==SIZE_MAX){if(!p->policy||!*p->policy||cb_policy_allows(p->policy,p->base,p->base,CB_POLICY_STYLE,1))cb_engine_parse_css_rules(p,sheet->css,sheet->length);}
        else {
            const struct resource *resource=&p->resources[sheet->resource];
            if(resource->css)cb_engine_parse_css_rules(p,resource->css,resource->css_length);
        }
    }
}
void cb_engine_add_css_media(struct cb_page *p,const char *css,size_t len,const char *media) {
    if(!p || !css || p->sheet_count>=STYLESHEET_LIMIT || p->css_bytes>=CSS_LIMIT)return;
    len=MIN(len,CSS_LIMIT-p->css_bytes);
    if(media && strlen(media)>MEDIA_QUERY_LIMIT)return;
    p->sheets[p->sheet_count++]=(struct stylesheet){cb_engine_slice(css,len),len,SIZE_MAX,media?g_strdup(media):NULL};
    p->css_bytes+=len;
    if(!p->parsing)cb_engine_rebuild_rules(p);
}
void cb_page_add_css(struct cb_page *p,const char *css,size_t len) {cb_engine_add_css_media(p,css,len,NULL);}

int cb_engine_parse_color(const char *s, struct color *out) {
    if (!g_ascii_strcasecmp(s, "transparent")) { *out = (struct color){0}; return 1; }
    if (*s == '#') {
        size_t len = strlen(++s);
        if (len != 3 && len != 4 && len != 6 && len != 8) return 0;
        unsigned values[4] = {0, 0, 0, 255};
        for (size_t i = 0; i < len; i++) if (!g_ascii_isxdigit(s[i])) return 0;
        if (len <= 4) for (size_t i = 0; i < len; i++) values[i] = (unsigned)g_ascii_xdigit_value(s[i]) * 17;
        else for (size_t i = 0; i < len / 2; i++) values[i] = (unsigned)(g_ascii_xdigit_value(s[2*i]) * 16 + g_ascii_xdigit_value(s[2*i+1]));
        *out = (struct color){values[0]/255.0, values[1]/255.0, values[2]/255.0, values[3]/255.0}; return 1;
    }
    static const struct { const char *name; unsigned value; } colors[] = {
        {"black",0x000000}, {"white",0xffffff}, {"red",0xff0000}, {"green",0x008000},
        {"blue",0x0000ff}, {"gray",0x808080}, {"grey",0x808080}, {"silver",0xc0c0c0},
        {"navy",0x000080}, {"teal",0x008080}, {"purple",0x800080}, {"orange",0xffa500},
        {"yellow",0xffff00}, {"lime",0x00ff00}, {"aqua",0x00ffff}, {"fuchsia",0xff00ff},
        {"maroon",0x800000}, {"olive",0x808000}, {"rebeccapurple",0x663399},
        {"whitesmoke",0xf5f5f5}, {"lightgray",0xd3d3d3}, {"darkgray",0xa9a9a9}
    };
    for (size_t i = 0; i < G_N_ELEMENTS(colors); i++) if (!g_ascii_strcasecmp(s, colors[i].name)) {
        *out = cb_engine_rgb(colors[i].value); return 1;
    }
    double r, g, b, a = 1;
    if (sscanf(s, "rgba(%lf , %lf , %lf , %lf)", &r, &g, &b, &a) == 4 ||
        sscanf(s, "rgb(%lf , %lf , %lf)", &r, &g, &b) == 3) {
        if (!isfinite(r) || !isfinite(g) || !isfinite(b) || !isfinite(a)) return 0;
        *out = (struct color){CLAMP(r,0,255)/255, CLAMP(g,0,255)/255, CLAMP(b,0,255)/255, CLAMP(a,0,1)};
        return 1;
    }
    return 0;
}
int cb_engine_parse_length(const char *value, struct length *out) {
    if (!g_ascii_strcasecmp(value, "auto") || !g_ascii_strcasecmp(value, "none")) {
        *out = (struct length){0, L_AUTO}; return 1;
    }
    char *end;
    errno = 0;
    double v = g_ascii_strtod(value, &end);
    if (end == value || errno || !isfinite(v)) return 0;
    while (g_ascii_isspace(*end)) end++;
    enum len_kind kind = L_PX;
    if (!g_ascii_strcasecmp(end, "%")) kind = L_PERCENT;
    else if (!g_ascii_strcasecmp(end, "em")) kind = L_EM;
    else if (!g_ascii_strcasecmp(end, "rem")) v *= 16;
    else if (*end && g_ascii_strcasecmp(end, "px")) return 0;
    *out = (struct length){CLAMP(v, -10000, 10000), kind};
    return 1;
}
double cb_engine_length_value(struct length l, double basis, double font, double zoom) {
    if (l.kind == L_PERCENT) return basis * l.value / 100;
    if (l.kind == L_EM) return font * l.value * zoom;
    if (l.kind == L_PX) return l.value * zoom;
    return 0;
}
enum property {
    P_DISPLAY, P_COLOR, P_BACKGROUND, P_SIZE, P_WEIGHT, P_ITALIC, P_UNDERLINE,
    P_FAMILY, P_ALIGN, P_PRE, P_LINE_HEIGHT, P_BORDER, P_BORDER_COLOR, P_GAP,
    P_DIRECTION, P_WIDTH, P_HEIGHT, P_MAX_WIDTH, P_MIN_HEIGHT, P_TABLE_SPACING, P_COLLAPSE, P_MARGIN, P_PADDING = P_MARGIN + 4,
    P_WRAP = P_PADDING + 4, P_GROW, P_SHRINK, P_BASIS, P_JUSTIFY, P_ALIGN_ITEMS, P_ALIGN_SELF, P_GRID, P_POSITION, P_OVERFLOW, P_MIN_WIDTH, P_MAX_HEIGHT, P_BOX, P_ROW_GAP, P_TOP, P_RIGHT, P_BOTTOM, P_LEFT, P_COUNT
};
void cb_engine_set_sides(struct length *out, uint64_t *scores, uint64_t rank, const char *v, int side) {
    struct length lengths[4]; int count = 0;
    char *copy = g_strdup(v), *save = NULL;
    for (char *part = strtok_r(copy, " \t\r\n", &save); part && count < 4;
         part = strtok_r(NULL, " \t\r\n", &save)) {
        if (!cb_engine_parse_length(part, &lengths[count])) { g_free(copy); return; }
        count++;
    }
    if (count && (side < 0 || count == 1)) for (int i = 0; i < 4; i++) {
        if (side >= 0 && side != i) continue;
        if (rank < scores[i]) continue;
        scores[i] = rank;
        int index = i == 0 ? 0 : i == 1 ? (count > 1 ? 1 : 0) :
                    i == 2 ? (count > 2 ? 2 : 0) : (count > 3 ? 3 : count > 1 ? 1 : 0);
        out[i] = lengths[index];
    }
    g_free(copy);
}
void cb_engine_apply_declaration(struct style *s, const struct style *parent, uint64_t *scores,
                              const char *name, const char *v, uint64_t rank) {
    enum property prop;
    if (!strcmp(name,"display")) prop=P_DISPLAY;
    else if (!strcmp(name,"color")) prop=P_COLOR;
    else if (!strcmp(name,"background") || !strcmp(name,"background-color")) prop=P_BACKGROUND;
    else if (!strcmp(name,"font-size")) prop=P_SIZE;
    else if (!strcmp(name,"font-weight")) prop=P_WEIGHT;
    else if (!strcmp(name,"font-style")) prop=P_ITALIC;
    else if (!strcmp(name,"text-decoration") || !strcmp(name,"text-decoration-line")) prop=P_UNDERLINE;
    else if (!strcmp(name,"font-family")) prop=P_FAMILY;
    else if (!strcmp(name,"text-align")) prop=P_ALIGN;
    else if (!strcmp(name,"white-space")) prop=P_PRE;
    else if (!strcmp(name,"line-height")) prop=P_LINE_HEIGHT;
    else if (!strcmp(name,"border-width") || !strcmp(name,"border")) prop=P_BORDER;
    else if (!strcmp(name,"border-color")) prop=P_BORDER_COLOR;
    else if (!strcmp(name,"gap") || !strcmp(name,"column-gap")) prop=P_GAP;
    else if (!strcmp(name,"flex-direction")) prop=P_DIRECTION;
    else if (!strcmp(name,"width")) prop=P_WIDTH;
    else if (!strcmp(name,"height")) prop=P_HEIGHT;
    else if (!strcmp(name,"max-width")) prop=P_MAX_WIDTH;
    else if (!strcmp(name,"min-height")) prop=P_MIN_HEIGHT;
    else if (!strcmp(name,"border-spacing")) prop=P_TABLE_SPACING;
    else if (!strcmp(name,"border-collapse")) prop=P_COLLAPSE;
    else if (!strcmp(name,"flex-wrap")) prop=P_WRAP;
    else if (!strcmp(name,"flex-grow")) prop=P_GROW;
    else if (!strcmp(name,"flex-shrink")) prop=P_SHRINK;
    else if (!strcmp(name,"flex-basis")) prop=P_BASIS;
    else if (!strcmp(name,"justify-content")) prop=P_JUSTIFY;
    else if (!strcmp(name,"align-items")) prop=P_ALIGN_ITEMS;
    else if (!strcmp(name,"align-self")) prop=P_ALIGN_SELF;
    else if (!strcmp(name,"grid-template-columns")) prop=P_GRID;
    else if (!strcmp(name,"position")) prop=P_POSITION;
    else if (!strcmp(name,"overflow")) prop=P_OVERFLOW;
    else if (!strcmp(name,"min-width")) prop=P_MIN_WIDTH;
    else if (!strcmp(name,"max-height")) prop=P_MAX_HEIGHT;
    else if (!strcmp(name,"box-sizing")) prop=P_BOX;
    else if (!strcmp(name,"row-gap")) prop=P_ROW_GAP;
    else if (!strcmp(name,"top")) prop=P_TOP;
    else if (!strcmp(name,"right")) prop=P_RIGHT;
    else if (!strcmp(name,"bottom")) prop=P_BOTTOM;
    else if (!strcmp(name,"left")) prop=P_LEFT;
    else if (!strcmp(name,"flex")) {
        char first[64]="",second[64]="",third[64]="",extra[2];
        int count=sscanf(v,"%63s %63s %63s %1s",first,second,third,extra);
        if(!strcmp(v,"none")){strcpy(first,"0");strcpy(second,"0");strcpy(third,"auto");}
        else if(!strcmp(v,"auto")){strcpy(first,"1");strcpy(second,"1");strcpy(third,"auto");}
        else {
            if(count<1||count>3)return;
            if(count==1){strcpy(second,"1");strcpy(third,"0px");}
            if(count==2){struct length basis;if(cb_engine_parse_length(second,&basis)&&strpbrk(second,"%abcdefghijklmnopqrstuvwxyz")){strcpy(third,second);strcpy(second,"1");}else strcpy(third,"0px");}
        }
        char *end;double grow=g_ascii_strtod(first,&end);if(*end||end==first||!isfinite(grow)||grow<0)return;
        double shrink=g_ascii_strtod(second,&end);if(*end||end==second||!isfinite(shrink)||shrink<0)return;
        struct length basis;if(!cb_engine_parse_length(third,&basis)||basis.value<0)return;
        cb_engine_apply_declaration(s,parent,scores,"flex-grow",first,rank);
        cb_engine_apply_declaration(s,parent,scores,"flex-shrink",second,rank);
        cb_engine_apply_declaration(s,parent,scores,"flex-basis",third,rank);return;
    }
    else if (!strncmp(name,"margin",6) || !strncmp(name,"padding",7)) {
        int padding = name[0]=='p';
        const char *suffix = name+(padding?7:6);
        int side=-1;
        if (*suffix) {
            if (!strcmp(suffix,"-top")) side=0;
            else if (!strcmp(suffix,"-right")) side=1;
            else if (!strcmp(suffix,"-bottom")) side=2;
            else if (!strcmp(suffix,"-left")) side=3;
            else return;
        }
        cb_engine_set_sides(padding?s->padding:s->margin, scores+(padding?P_PADDING:P_MARGIN), rank, v, side);
        return;
    } else return;
    if (rank < scores[prop]) return;
    struct length l; struct color c; int valid=1;
    switch (prop) {
    case P_DISPLAY:
        if (!strcmp(v,"none")) s->display=D_NONE;
        else if (!strcmp(v,"flex") || !strcmp(v,"inline-flex")) s->display=D_FLEX;
        else if (!strcmp(v,"grid")) s->display=D_GRID;
        else if (!strcmp(v,"table") || !strcmp(v,"inline-table"))s->display=D_TABLE;
        else if (!strcmp(v,"table-row-group") || !strcmp(v,"table-header-group") || !strcmp(v,"table-footer-group"))s->display=D_ROWGROUP;
        else if (!strcmp(v,"table-row"))s->display=D_ROW;
        else if (!strcmp(v,"table-cell"))s->display=D_CELL;
        else if (!strcmp(v,"table-caption"))s->display=D_CAPTION;
        else if (!strcmp(v,"block") || !strcmp(v,"list-item")) s->display=D_BLOCK;
        else if (!strcmp(v,"inline") || !strcmp(v,"inline-block")) s->display=D_INLINE;
        else valid=0;
        break;
    case P_COLOR: valid=cb_engine_parse_color(v,&c); if(valid)s->color=c; break;
    case P_BACKGROUND: valid=cb_engine_parse_color(v,&c); if(valid)s->background=c; break;
    case P_BORDER_COLOR: valid=cb_engine_parse_color(v,&c); if(valid)s->border_color=c; break;
    case P_SIZE:
        if (!strcmp(v,"small")) s->font_size=13;
        else if (!strcmp(v,"medium")) s->font_size=16;
        else if (!strcmp(v,"large")) s->font_size=20;
        else if ((valid=cb_engine_parse_length(v,&l))) s->font_size=CLAMP(cb_engine_length_value(l,parent->font_size,parent->font_size,1),6,160);
        break;
    case P_WEIGHT:
        if(!strcmp(v,"bold") || !strcmp(v,"bolder"))s->weight=700;
        else if(!strcmp(v,"normal"))s->weight=400;
        else { char *end; long w=strtol(v,&end,10); valid=end!=v && !*end && w>=100 && w<=1000; if(valid)s->weight=(int)w; }
        break;
    case P_ITALIC: s->italic=!strcmp(v,"italic") || !strcmp(v,"oblique"); break;
    case P_UNDERLINE: s->underline=strstr(v,"underline")!=NULL; break;
    case P_FAMILY: s->family=strstr(v,"mono")?1:strstr(v,"sans")?0:strstr(v,"serif")?2:0; break;
    case P_ALIGN: s->align=!strcmp(v,"center")?1:!strcmp(v,"right")?2:0; break;
    case P_PRE: s->pre=!strcmp(v,"pre") || !strcmp(v,"pre-wrap") || !strcmp(v,"break-spaces"); break;
    case P_LINE_HEIGHT:
        if (!strcmp(v,"normal")) s->line_height=1.2;
        else if ((valid=cb_engine_parse_length(v,&l))) {
            double h = cb_engine_length_value(l,s->font_size,s->font_size,1);
            if (l.kind==L_PX && !strpbrk(v,"abcdefghijklmnopqrstuvwxyz%")) s->line_height=CLAMP(l.value,0.7,4);
            else s->line_height=CLAMP(h/s->font_size,0.7,4);
        }
        break;
    case P_BORDER:
        if (!strcmp(v,"none")) s->border=0;
        else {
            char *copy=g_strdup(v), *save=NULL; valid=0;
            for (char *part=strtok_r(copy," \t",&save);part;part=strtok_r(NULL," \t",&save)) {
                if(cb_engine_parse_length(part,&l)){s->border=CLAMP(cb_engine_length_value(l,0,s->font_size,1),0,32);valid=1;}
                else if(cb_engine_parse_color(part,&c) && rank>=scores[P_BORDER_COLOR]){s->border_color=c;scores[P_BORDER_COLOR]=rank;}
            }
            g_free(copy);
        }
        break;
    case P_GAP: valid=cb_engine_parse_length(v,&l); if(valid){s->gap=CLAMP(cb_engine_length_value(l,0,s->font_size,1),0,1000);if(!strcmp(name,"gap")&&rank>=scores[P_ROW_GAP]){s->row_gap=s->gap;scores[P_ROW_GAP]=rank;}} break;
    case P_DIRECTION: s->flex_column=!strcmp(v,"column"); break;
    case P_WIDTH: valid=cb_engine_parse_length(v,&l); if(valid)s->width=l; break;
    case P_HEIGHT: valid=cb_engine_parse_length(v,&l); if(valid)s->height=l; break;
    case P_MAX_WIDTH: valid=cb_engine_parse_length(v,&l); if(valid)s->max_width=l; break;
    case P_MIN_HEIGHT: valid=cb_engine_parse_length(v,&l); if(valid)s->min_height=l; break;
    case P_TABLE_SPACING: valid=cb_engine_parse_length(v,&l);if(valid)s->table_spacing=CLAMP(cb_engine_length_value(l,0,s->font_size,1),0,100);break;
    case P_COLLAPSE: valid=!strcmp(v,"collapse") || !strcmp(v,"separate");if(valid)s->collapse_borders=!strcmp(v,"collapse");break;
    case P_WRAP: valid=!strcmp(v,"nowrap")||!strcmp(v,"wrap");if(valid)s->wrap=!strcmp(v,"wrap");break;
    case P_GROW: case P_SHRINK: {
        char *end;double number=g_ascii_strtod(v,&end);valid=end!=v&&!*end&&isfinite(number)&&number>=0&&number<=1000;
        if(valid){if(prop==P_GROW)s->grow=number;else s->shrink=number;}break;
    }
    case P_BASIS: valid=cb_engine_parse_length(v,&l)&&l.value>=0;if(valid)s->basis=l;break;
    case P_JUSTIFY: valid=cb_engine_in_words("flex-start start center flex-end end space-between space-around space-evenly",v);
        if(valid)s->justify=!strcmp(v,"center")?1:cb_engine_in_words("end flex-end",v)?2:!strcmp(v,"space-between")?3:!strcmp(v,"space-around")?4:!strcmp(v,"space-evenly")?5:0;
        break;
    case P_ALIGN_ITEMS: case P_ALIGN_SELF:
        valid=cb_engine_in_words("auto stretch start flex-start center end flex-end",v);
        if(valid){int a=!strcmp(v,"stretch")?0:!strcmp(v,"center")?1:cb_engine_in_words("end flex-end",v)?2:!strcmp(v,"auto")?-1:3;if(prop==P_ALIGN_ITEMS)s->align_items=a;else s->align_self=a;}break;
    case P_GRID: {
        unsigned columns=0;int consumed=0;
        if(sscanf(v,"repeat( %u , 1fr ) %n",&columns,&consumed)!=1 || !consumed || v[consumed]){
            char *copy=g_strdup(v),*save=NULL;columns=0;
            for(char *part=strtok_r(copy," \t",&save);part;part=strtok_r(NULL," \t",&save)){
                if(strcmp(part,"1fr")){columns=0;break;}columns++;
            }g_free(copy);
        }
        valid=columns>0&&columns<=16;if(valid)s->grid_columns=(int)columns;break;
    }
    case P_POSITION: valid=cb_engine_in_words("static relative absolute",v);if(valid)s->position=!strcmp(v,"relative")?1:!strcmp(v,"absolute")?2:0;break;
    case P_OVERFLOW: valid=cb_engine_in_words("visible hidden clip",v);if(valid)s->overflow=strcmp(v,"visible")!=0;break;
    case P_MIN_WIDTH: valid=cb_engine_parse_length(v,&l);if(valid)s->min_width=l;break;
    case P_MAX_HEIGHT: valid=cb_engine_parse_length(v,&l);if(valid)s->max_height=l;break;
    case P_BOX: valid=!strcmp(v,"border-box")||!strcmp(v,"content-box");if(valid)s->content_box=!strcmp(v,"content-box");break;
    case P_ROW_GAP: valid=cb_engine_parse_length(v,&l);if(valid)s->row_gap=CLAMP(cb_engine_length_value(l,0,s->font_size,1),0,1000);break;
    case P_TOP: case P_RIGHT: case P_BOTTOM: case P_LEFT: valid=cb_engine_parse_length(v,&l);if(valid)s->inset[prop-P_TOP]=l;break;
    default: break;
    }
    if (valid) scores[prop]=rank;
}
/* Declarations are split outside strings/functions. Custom names are case-sensitive. */
struct declaration { char *name,*value;uint64_t rank; };
static int next_declaration(char **cursor,struct declaration *out,unsigned spec,unsigned order) {
    char *start=*cursor;if(!*start)return 0;char quote=0;unsigned depth=0;char *c=start;
    for(;*c;c++){
        if(*c=='\\'){if(c[1])c++;continue;}
        if(quote){if(*c==quote)quote=0;continue;}
        if(*c=='\''||*c=='"'){quote=*c;continue;}
        if(*c=='(')depth++;else if(*c==')'&&depth)depth--;else if(*c==';'&&!depth)break;
    }
    if(*c)*c++=0;
    *cursor=c;
    char *colon=strchr(start,':');out->name=out->value=NULL;if(!colon)return 1;*colon++=0;
    out->name=g_strstrip(start);out->value=g_strstrip(colon);
    if(strncmp(out->name,"--",2))for(char *p=out->name;*p;p++)*p=g_ascii_tolower(*p);
    out->rank=((uint64_t)spec<<32)|(order+1u);
    char *important=strrchr(out->value,'!');
    if(important&&!g_ascii_strcasecmp(g_strstrip(important+1),"important")){
        *important=0;out->value=g_strstrip(out->value);out->rank|=UINT64_C(1)<<63;
    }
    return 1;
}
static char *substitute_vars(GHashTable *vars,const char *value,const char **stack,unsigned depth,unsigned *work,int *cycle,int recursive){
    if(depth>=16||strlen(value)>4096||++*work>512)return NULL;
    GString *out=g_string_new(NULL);const char *p=value;char quote=0;
    while(*p && out->len<=4096){
        if(quote){if(*p==quote)quote=0;g_string_append_c(out,*p++);continue;}
        if(*p=='\''||*p=='"'){quote=*p;g_string_append_c(out,*p++);continue;}
        if(strncmp(p,"var(",4)){g_string_append_c(out,*p++);continue;}
        const char *start=p+4,*end=start,*comma=NULL;unsigned nesting=1;
        for(;*end&&nesting;end++){if(*end=='(')nesting++;else if(*end==')')nesting--;else if(*end==','&&nesting==1&&!comma)comma=end;}
        if(nesting)goto invalid;
        char *name=g_strndup(start,(comma?comma:end-1)-start);g_strstrip(name);
        const char *found=vars?g_hash_table_lookup(vars,name):NULL;char *replacement=NULL;
        int seen=0;for(unsigned i=0;i<depth;i++)if(stack[i]&&!strcmp(stack[i],name))seen=1;
        if(seen){*cycle=1;g_free(name);goto invalid;}
        if(found){if(recursive){stack[depth]=name;replacement=substitute_vars(vars,found,stack,depth+1,work,cycle,1);}else replacement=g_strdup(found);}
        if(!replacement&&comma&&!*cycle){stack[depth]=NULL;char *fallback=g_strndup(comma+1,end-comma-2);replacement=substitute_vars(vars,fallback,stack,depth+1,work,cycle,recursive);g_free(fallback);}
        stack[depth]=NULL;g_free(name);if(!replacement)goto invalid;
        g_string_append(out,replacement);g_free(replacement);p=end;
    }
    if(out->len>4096)goto invalid;
    return g_string_free(out,FALSE);
invalid:g_string_free(out,TRUE);return NULL;
}
static void collect_custom(struct node *n,GHashTable *ranks,const char *css,unsigned spec,unsigned order){
    char *copy=g_strdup(css),*cursor=copy;struct declaration d;
    while(next_declaration(&cursor,&d,spec,order)){
        if(!d.name||strncmp(d.name,"--",2)||!d.name[2]||strlen(d.name)>128||strlen(d.value)>4096)continue;
        uint64_t *rank=g_hash_table_lookup(ranks,d.name);if(rank&&*rank>d.rank)continue;
        if(!g_hash_table_contains(n->custom,d.name)&&g_hash_table_size(n->custom)>=64)continue;
        if(!rank){rank=g_new0(uint64_t,1);g_hash_table_insert(ranks,g_strdup(d.name),rank);}*rank=d.rank;
        const char *value=d.value;
        if(!strcmp(value,"inherit")||!strcmp(value,"unset"))value=n->parent&&n->parent->custom?g_hash_table_lookup(n->parent->custom,d.name):NULL;
        if(!value||!strcmp(value,"initial"))g_hash_table_remove(n->custom,d.name);
        else g_hash_table_replace(n->custom,g_strdup(d.name),g_strdup(value));
    }g_free(copy);
}
static void compute_custom(struct cb_page *p,struct node *n){
    if(n->custom)g_hash_table_unref(n->custom);
    n->custom=NULL;
    if(!p->custom_rules&&(!n->inline_css||!strstr(n->inline_css,"--"))&&(!n->parent||!n->parent->custom))return;
    n->custom=g_hash_table_new_full(g_str_hash,g_str_equal,g_free,g_free);
    GHashTableIter it;gpointer key,value;
    if(n->parent&&n->parent->custom){g_hash_table_iter_init(&it,n->parent->custom);while(g_hash_table_iter_next(&it,&key,&value))g_hash_table_insert(n->custom,g_strdup(key),g_strdup(value));}
    GHashTable *ranks=g_hash_table_new_full(g_str_hash,g_str_equal,g_free,g_free);
    for(size_t i=0;i<p->rule_count;i++)if(cb_engine_selector_matches(n,p->rules[i].selector))collect_custom(n,ranks,p->rules[i].declarations,p->rules[i].specificity,p->rules[i].order);
    if(n->inline_css&&(!p->policy||!*p->policy||cb_policy_allows(p->policy,p->base,p->base,CB_POLICY_STYLE_ATTR,1)))collect_custom(n,ranks,n->inline_css,1000,(unsigned)p->rule_count);
    g_hash_table_unref(ranks);
    GHashTable *resolved=g_hash_table_new_full(g_str_hash,g_str_equal,g_free,g_free);
    g_hash_table_iter_init(&it,n->custom);
    while(g_hash_table_iter_next(&it,&key,&value)){
        const char *stack[16]={key};unsigned work=0;int cycle=0;
        char *v=substitute_vars(n->custom,value,stack,1,&work,&cycle,1);
        if(v&&!cycle)g_hash_table_insert(resolved,g_strdup(key),v);else g_free(v);
    }
    g_hash_table_unref(n->custom);n->custom=resolved;
}
void cb_engine_apply_declarations(struct style *s,const struct style *parent,uint64_t *scores,const char *css,unsigned spec,unsigned order,const struct node *node){
    char *copy=g_strdup(css),*cursor=copy;struct declaration d;
    while(next_declaration(&cursor,&d,spec,order)){
        if(!d.name||!strncmp(d.name,"--",2))continue;
        const char *stack[16]={0};unsigned work=0;int cycle=0;
        char *value=strstr(d.value,"var(")?substitute_vars(node->custom,d.value,stack,0,&work,&cycle,0):g_strdup(d.value);
        if(value){cb_engine_apply_declaration(s,parent,scores,d.name,value,d.rank);g_free(value);}
    }g_free(copy);
}
void cb_engine_compute_style(struct cb_page *p, struct node *n, const struct style *parent) {
    struct style *s=&n->style;
    *s=(struct style){.display=D_INLINE,.color=parent->color,.font_size=parent->font_size,
        .weight=parent->weight,.italic=parent->italic,.underline=parent->underline,
        .family=parent->family,.align=parent->align,.pre=parent->pre,.line_height=parent->line_height,
        .border_color=parent->color,.shrink=1,.align_self=-1};
    for(int i=0;i<4;i++){s->margin[i].kind=L_PX;s->padding[i].kind=L_PX;}
    if(cb_engine_tag_is(n,"#text"))return;
    if(cb_engine_is_block_tag(n->tag) || cb_engine_tag_is(n,"#document"))s->display=D_BLOCK;
    if(cb_engine_tag_is(n,"table")){s->display=D_TABLE;s->table_spacing=2;}
    else if(cb_engine_in_words("thead tbody tfoot",n->tag))s->display=D_ROWGROUP;
    else if(cb_engine_tag_is(n,"tr"))s->display=D_ROW;
    else if(cb_engine_in_words("td th",n->tag)){s->display=D_CELL;for(int i=0;i<4;i++)s->padding[i]=(struct length){4,L_PX};}
    else if(cb_engine_tag_is(n,"caption"))s->display=D_CAPTION;
    if(cb_engine_in_words("head script style meta link title template",n->tag))s->display=D_NONE;
    if(cb_engine_tag_is(n,"body") || (cb_engine_tag_is(n,"#document") && !p->has_body))
        for(int i=0;i<4;i++)s->margin[i]=(struct length){8,L_PX};
    if(cb_engine_in_words("p pre blockquote ul ol dl figure",n->tag)) {
        s->margin[0]=s->margin[2]=(struct length){1,L_EM};
    }
    if(cb_engine_in_words("ul ol",n->tag))s->padding[3]=(struct length){28,L_PX};
    if(cb_engine_tag_is(n,"blockquote"))s->margin[3]=s->margin[1]=(struct length){32,L_PX};
    if(n->tag[0]=='h' && n->tag[1]>='1' && n->tag[1]<='6' && !n->tag[2]) {
        static const double sizes[]={32,26,22,19,17,16};
        s->font_size=sizes[n->tag[1]-'1']; s->weight=700;
        s->margin[0]=(struct length){0.67,L_EM}; s->margin[2]=(struct length){0.5,L_EM};
    }
    if(cb_engine_in_words("b strong th",n->tag))s->weight=700;
    if(cb_engine_in_words("em i cite",n->tag))s->italic=1;
    if(cb_engine_in_words("code pre kbd samp",n->tag))s->family=1;
    if(cb_engine_tag_is(n,"pre"))s->pre=1;
    if(cb_engine_tag_is(n,"small"))s->font_size*=0.8;
    if(cb_engine_tag_is(n,"img")){
        if(n->image_width)s->width=(struct length){n->image_width,L_PX};
        if(n->image_height)s->height=(struct length){n->image_height,L_PX};
    }
    if(cb_engine_tag_is(n,"a") && n->href){s->color=cb_engine_rgb(0x245db7);s->underline=1;}
    if(cb_engine_tag_is(n,"hr")){s->border=0.5;s->border_color=cb_engine_rgb(0xc9d2dc);s->margin[0]=s->margin[2]=(struct length){12,L_PX};}
    compute_custom(p,n);
    uint64_t scores[P_COUNT]={0};
    for(size_t i=0;i<p->rule_count;i++)if(cb_engine_selector_matches(n,p->rules[i].selector))
        cb_engine_apply_declarations(s,parent,scores,p->rules[i].declarations,p->rules[i].specificity,p->rules[i].order,n);
    if(n->inline_css&&(!p->policy||!*p->policy||cb_policy_allows(p->policy,p->base,p->base,CB_POLICY_STYLE_ATTR,1)))cb_engine_apply_declarations(s,parent,scores,n->inline_css,1000,(unsigned)p->rule_count,n);
    if(n->hidden || (n->control && n->control->kind==C_HIDDEN))s->display=D_NONE;
    for(struct node *child=n->first;child;child=child->next)cb_engine_compute_style(p,child,s);
}

