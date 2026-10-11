#define _GNU_SOURCE
#include "platform.h"
#include "player.h"
#include "state.h"
#include "theme.h"
#include <gio/gio.h>
#include <glib/gstdio.h>
#include <pango/pangocairo.h>
#include <xkbcommon/xkbcommon-keysyms.h>
#include <linux/input-event-codes.h>
#include <signal.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

enum Panel { NONE, FILES, SETTINGS, TRACKS, HELP, LOCATION };
enum Action {
    A_NONE, A_MOVE, A_CLOSE, A_MINIMIZE, A_MAXIMIZE, A_OPEN, A_ADD, A_SETTINGS,
    A_PLAY, A_PREV, A_NEXT, A_BACK, A_FORWARD, A_MUTE, A_VOLUME, A_SEEK,
    A_FULL, A_SIDEBAR, A_SHUFFLE, A_REPEAT, A_SPEED, A_TRACKS, A_HELP,
    A_QUEUE, A_RECENT, A_ITEM, A_REMOVE, A_CLEAR, A_RECENT_ITEM,
    A_DISMISS, A_THEME, A_RESUME, A_FORGET, A_SLOWER, A_FASTER,
    A_AUDIO, A_SUB, A_SUBFILE, A_SUBDELAY_MINUS, A_SUBDELAY_PLUS, A_CHAPTER,
    A_HOME, A_MUSIC, A_VIDEOS, A_DOWNLOADS, A_PARENT, A_FILE, A_FILE_OPEN,
    A_INPUT, A_LOCATION, A_HIDDEN, A_SELECT, A_FILE_ADD, A_RESIZE
};
typedef struct { double x,y,w,h; int action,value; char label[96]; } Hit;
typedef struct { char *path,*name; bool dir; } Entry;
typedef struct {
    struct cp_window *window;
    Player player; State state;
    GPtrArray *queue, *entries;
    int current, selected, queue_scroll, file_scroll, file_selected, tracks_scroll;
    char *current_path, *folder;
    enum Panel panel;
    bool recent, add_mode, subtitle_mode, hidden, input_focus, input_select;
    bool fullscreen, dragging, hover_controls, suppress_history, closing;
    int drag_action;
    char input[8192], toast[512];
    size_t input_cursor;
    double mouse_x, mouse_y, last_pointer, toast_until, started, last_save, last_theme, last_snapshot;
    double scale, ui_scale, sub_delay, file_click_time;
    int file_click_index;
    int width, height, nhit, focus;
    Hit hit[1024];
    struct status_theme theme;
    bool strong;
    cairo_t *cr;
    double vw,vh;
    const char *capture, *report;
    double quit_after;
    bool captured, software, silent;
    bool ever_loaded, ever_video;
    double max_position;
} App;
static volatile sig_atomic_t stopping;
static void interrupted(int sig) { (void)sig; stopping=1; }
static double clock_now(void) { return g_get_monotonic_time()/1e6; }
static void dirty(App *a) { cp_window_dirty(a->window); }
static void toast(App *a, const char *text) { g_strlcpy(a->toast,text,sizeof(a->toast)); a->toast_until=clock_now()+4; dirty(a); }
static void act(App *,int,int);
static void open_files(App *,bool,bool);
static void play_index(App *,int);
static void show_panel(App *a,enum Panel panel) { a->panel=panel; a->focus=-1; a->input_focus=false; a->tracks_scroll=0; dirty(a); }
static void ink(App *a,const char *hex,double alpha) {
    unsigned c=strtoul(hex,NULL,16); cairo_set_source_rgba(a->cr,(c>>16)/255.,((c>>8)&255)/255.,(c&255)/255.,alpha);
}
static void rect(App *a,double x,double y,double w,double h,const char *color) {
    ink(a,color,1); cairo_rectangle(a->cr,x,y,w,h); cairo_fill(a->cr);
}
static void rounded(App *a,double x,double y,double w,double h,double r) {
    r=MIN(r,MIN(w,h)/2); cairo_new_sub_path(a->cr);
    cairo_arc(a->cr,x+w-r,y+r,r,-M_PI/2,0); cairo_arc(a->cr,x+w-r,y+h-r,r,0,M_PI/2);
    cairo_arc(a->cr,x+r,y+h-r,r,M_PI/2,M_PI); cairo_arc(a->cr,x+r,y+r,r,M_PI,3*M_PI/2); cairo_close_path(a->cr);
}
static void box(App *a,double x,double y,double w,double h,double r,const char *color) {
    ink(a,color,1); rounded(a,x,y,w,h,r); cairo_fill(a->cr);
}
static void line(App *a,double x1,double y1,double x2,double y2,const char *color,double width) {
    ink(a,color,1); cairo_set_line_width(a->cr,width); cairo_move_to(a->cr,x1,y1); cairo_line_to(a->cr,x2,y2); cairo_stroke(a->cr);
}
static void text(App *a,const char *s,double x,double y,double size,const char *color,double width,bool bold) {
    PangoLayout *layout=pango_cairo_create_layout(a->cr);
    PangoFontDescription *font=pango_font_description_new(); pango_font_description_set_family(font,"Sans");
    pango_font_description_set_absolute_size(font,size*PANGO_SCALE); pango_font_description_set_weight(font,bold?PANGO_WEIGHT_SEMIBOLD:PANGO_WEIGHT_NORMAL);
    pango_layout_set_font_description(layout,font);
    char *valid=g_utf8_make_valid(s?s:"",-1); pango_layout_set_text(layout,valid,-1); g_free(valid);
    if(width>0) { pango_layout_set_width(layout,width*PANGO_SCALE); pango_layout_set_ellipsize(layout,PANGO_ELLIPSIZE_END); }
    pango_layout_set_single_paragraph_mode(layout,TRUE); ink(a,color,1); cairo_move_to(a->cr,x,y); pango_cairo_show_layout(a->cr,layout);
    pango_font_description_free(font); g_object_unref(layout);
}
static void centered(App *a,const char *s,double x,double y,double width,double size,const char *color,bool bold) {
    PangoLayout *l=pango_cairo_create_layout(a->cr); PangoFontDescription *f=pango_font_description_new();
    pango_font_description_set_family(f,"Sans"); pango_font_description_set_absolute_size(f,size*PANGO_SCALE);
    pango_font_description_set_weight(f,bold?PANGO_WEIGHT_SEMIBOLD:PANGO_WEIGHT_NORMAL);
    pango_layout_set_font_description(l,f); pango_layout_set_text(l,s,-1); pango_layout_set_width(l,width*PANGO_SCALE);
    pango_layout_set_alignment(l,PANGO_ALIGN_CENTER); pango_layout_set_ellipsize(l,PANGO_ELLIPSIZE_END);
    ink(a,color,1); cairo_move_to(a->cr,x,y); pango_cairo_show_layout(a->cr,l); pango_font_description_free(f); g_object_unref(l);
}
static bool inside(Hit *h,double x,double y) { return x>=h->x && y>=h->y && x<h->x+h->w && y<h->y+h->h; }
static void target(App *a,int action,int value,double x,double y,double w,double h,const char *label) {
    if(a->nhit >= (int)G_N_ELEMENTS(a->hit)) return;
    Hit *t=&a->hit[a->nhit++]; *t=(Hit){x,y,w,h,action,value,{0}}; g_strlcpy(t->label,label?label:"",sizeof(t->label));
}
static void cube(App *a,double x,double y,double size,const char *color) {
    double k=size/2;
    ink(a,color,1); cairo_set_line_width(a->cr,MAX(1.3,size/40));
    cairo_move_to(a->cr,x+k,y);cairo_line_to(a->cr,x+size,y+k*.52);cairo_line_to(a->cr,x+size,y+k*1.5);
    cairo_line_to(a->cr,x+k,y+size);cairo_line_to(a->cr,x,y+k*1.5);cairo_line_to(a->cr,x,y+k*.52);cairo_close_path(a->cr);
    cairo_move_to(a->cr,x,y+k*.52);cairo_line_to(a->cr,x+k,y+k);cairo_line_to(a->cr,x+size,y+k*.52);
    cairo_move_to(a->cr,x+k,y+k);cairo_line_to(a->cr,x+k,y+size);cairo_stroke(a->cr);
}
static void icon(App *a,const char *name,double x,double y,double size,const char *color) {
    cairo_save(a->cr); cairo_translate(a->cr,x,y); cairo_scale(a->cr,size/24,size/24);
    ink(a,color,1); cairo_set_line_width(a->cr,1.65); cairo_set_line_cap(a->cr,CAIRO_LINE_CAP_ROUND); cairo_set_line_join(a->cr,CAIRO_LINE_JOIN_ROUND);
    if(!strcmp(name,"play")) { cairo_move_to(a->cr,8,5);cairo_line_to(a->cr,19,12);cairo_line_to(a->cr,8,19);cairo_close_path(a->cr);cairo_fill(a->cr); }
    else if(!strcmp(name,"pause")) { cairo_rectangle(a->cr,7,5,3,14);cairo_rectangle(a->cr,14,5,3,14);cairo_fill(a->cr); }
    else if(!strcmp(name,"folder")) { cairo_move_to(a->cr,3,7);cairo_line_to(a->cr,3,5);cairo_line_to(a->cr,10,5);cairo_line_to(a->cr,12,8);cairo_line_to(a->cr,21,8);cairo_line_to(a->cr,21,19);cairo_line_to(a->cr,3,19);cairo_close_path(a->cr);cairo_stroke(a->cr); }
    else if(!strcmp(name,"plus")) { line(a,5,12,19,12,color,1.7);line(a,12,5,12,19,color,1.7); }
    else if(!strcmp(name,"close")) { line(a,6,6,18,18,color,1.7);line(a,6,18,18,6,color,1.7); }
    else if(!strcmp(name,"minus")) line(a,6,12,18,12,color,1.7);
    else if(!strcmp(name,"max")) { cairo_rectangle(a->cr,5,5,14,14);cairo_stroke(a->cr); }
    else if(!strcmp(name,"next") || !strcmp(name,"prev")) {
        if(!strcmp(name,"prev")) { cairo_translate(a->cr,24,0);cairo_scale(a->cr,-1,1); }
        cairo_move_to(a->cr,6,5);cairo_line_to(a->cr,16,12);cairo_line_to(a->cr,6,19);cairo_close_path(a->cr);cairo_fill(a->cr);line(a,18,5,18,19,color,1.8);
    } else if(!strcmp(name,"volume") || !strcmp(name,"mute")) {
        cairo_move_to(a->cr,3,9);cairo_line_to(a->cr,7,9);cairo_line_to(a->cr,12,5);cairo_line_to(a->cr,12,19);cairo_line_to(a->cr,7,15);cairo_line_to(a->cr,3,15);cairo_close_path(a->cr);cairo_stroke(a->cr);
        if(!strcmp(name,"mute")) {line(a,17,9,22,15,color,1.6);line(a,17,15,22,9,color,1.6);}
        else {cairo_arc(a->cr,13,12,5,-.85,.85);cairo_stroke(a->cr);cairo_arc(a->cr,13,12,9,-.8,.8);cairo_stroke(a->cr);}
    } else if(!strcmp(name,"full")) {
        for(int i=0;i<4;i++) { cairo_save(a->cr);cairo_translate(a->cr,12,12);cairo_rotate(a->cr,i*M_PI/2);cairo_move_to(a->cr,-8,-3);cairo_line_to(a->cr,-8,-8);cairo_line_to(a->cr,-3,-8);cairo_stroke(a->cr);cairo_restore(a->cr); }
    } else if(!strcmp(name,"queue")) { for(int i=0;i<3;i++) {line(a,8,6+i*6,20,6+i*6,color,1.6);line(a,3,6+i*6,4,6+i*6,color,1.6);} }
    else if(!strcmp(name,"settings")) {
        for(int i=0;i<3;i++) {double yy=6+i*6,xx=i==1?15:8;line(a,4,yy,20,yy,color,1.5);ink(a,a->theme.background,1);cairo_arc(a->cr,xx,yy,2.4,0,2*M_PI);cairo_fill_preserve(a->cr);ink(a,color,1);cairo_stroke(a->cr);}
    } else if(!strcmp(name,"cc")) { cairo_rectangle(a->cr,2,5,20,14);cairo_stroke(a->cr);text(a,"CC",5,6,10,color,17,true); }
    else if(!strcmp(name,"shuffle")) {
        cairo_move_to(a->cr,3,6);cairo_line_to(a->cr,7,6);cairo_line_to(a->cr,17,18);cairo_line_to(a->cr,21,18);cairo_move_to(a->cr,3,18);cairo_line_to(a->cr,7,18);cairo_line_to(a->cr,17,6);cairo_line_to(a->cr,21,6);cairo_stroke(a->cr);line(a,18,3,21,6,color,1.5);line(a,18,9,21,6,color,1.5);line(a,18,15,21,18,color,1.5);
    } else if(!strcmp(name,"repeat")) {
        cairo_move_to(a->cr,4,10);cairo_line_to(a->cr,4,6);cairo_line_to(a->cr,20,6);cairo_line_to(a->cr,17,3);cairo_move_to(a->cr,20,14);cairo_line_to(a->cr,20,18);cairo_line_to(a->cr,4,18);cairo_line_to(a->cr,7,21);cairo_stroke(a->cr);
    } else if(!strcmp(name,"up")) {line(a,5,15,12,8,color,1.8);line(a,12,8,19,15,color,1.8);}
    else if(!strcmp(name,"check")) {line(a,5,12,10,17,color,1.8);line(a,10,17,20,6,color,1.8);}
    else if(!strcmp(name,"music")) {line(a,10,5,10,17,color,1.6);line(a,10,5,20,3,color,1.6);line(a,20,3,20,15,color,1.6);cairo_arc(a->cr,7,17,3,0,2*M_PI);cairo_arc(a->cr,17,15,3,0,2*M_PI);cairo_fill(a->cr);}
    else if(!strcmp(name,"file")) {cairo_rectangle(a->cr,5,3,14,18);cairo_stroke(a->cr);line(a,8,9,16,9,color,1.4);line(a,8,13,16,13,color,1.4);}
    else if(!strcmp(name,"help")) {cairo_arc(a->cr,12,12,9,0,2*M_PI);cairo_stroke(a->cr);text(a,"?",8,3,15,color,15,true);}
    cairo_restore(a->cr);
}
static void button(App *a,int action,int value,double x,double y,double w,double h,const char *label,const char *glyph,bool active) {
    Hit hit={x,y,w,h,0,0,{0}};
    bool hover=inside(&hit,a->mouse_x,a->mouse_y);
    const char *fg=active?a->theme.background:a->theme.ink;
    if(active || hover) box(a,x,y,w,h,6,active?a->theme.ink:a->theme.hover);
    if(glyph) icon(a,glyph,x+(label&&*label?12:(w-20)/2),y+(h-20)/2,20,fg);
    if(label && *label) {
        if(glyph) text(a,label,x+40,y+(h-17)/2,12,fg,w-48,true);
        else centered(a,label,x,y+(h-17)/2,w,12,fg,true);
    }
    target(a,action,value,x,y,w,h,label&&*label?label:glyph);
}
static void theme_reload(App *a) {
    status_theme_defaults(&a->theme);
    status_theme_color(a->theme.background,"fbfbfb");status_theme_color(a->theme.bar,"f5f5f5");
    char *path=g_build_filename(g_get_user_config_dir(),"whitecube","config",NULL),*data=NULL;
    if(g_file_get_contents(path,&data,NULL,NULL)) {
        char **lines=g_strsplit(data,"\n",-1);
        for(int i=0;lines[i];i++) {
            if(g_str_has_prefix(lines[i],"bg=")) status_theme_color(a->theme.background,lines[i]+3);
            if(g_str_has_prefix(lines[i],"tabbar=")) status_theme_color(a->theme.bar,lines[i]+7);
        } g_strfreev(lines);g_free(data);
    }g_free(path);
    if(a->state.theme==1) {status_theme_color(a->theme.background,"fbfbfb");status_theme_color(a->theme.bar,"f5f5f5");}
    if(a->state.theme==2) {status_theme_color(a->theme.background,"0c1017");status_theme_color(a->theme.bar,"111620");}
    a->ui_scale=1;a->strong=false;
    path=g_build_filename(g_get_user_state_dir(),"whitecube","accessibility.json",NULL);data=NULL;
    if(g_file_get_contents(path,&data,NULL,NULL)) {
        char *p=strstr(data,"\"ui_scale\"");
        if(p && (p=strchr(p,':'))) { double n=g_ascii_strtod(p+1,NULL); if(isfinite(n) && n>=.5 && n<=3) a->ui_scale=n; }
        p=strstr(data,"\"contrast\"");
        if(p && (p=strchr(p,':'))) {
            p++;while(g_ascii_isspace(*p))p++;
            a->strong=g_str_has_prefix(p,"\"strong\"");
        }
        g_free(data);
    }g_free(path);
    status_theme_finish(&a->theme,a->strong);
}
static void remember(App *a) {
    if(a->current_path && a->player.loaded && !a->suppress_history)
        state_remember(&a->state,a->current_path,a->player.position,a->player.duration);
}
static void play_index(App *a,int index) {
    if(index<0 || index>=(int)a->queue->len) return;
    remember(a); a->suppress_history=false;
    Media *m=g_ptr_array_index(a->queue,index);
    a->current=index; a->selected=index;g_free(a->current_path);a->current_path=g_strdup(m->path);
    player_load(&a->player,m->path,state_position(&a->state,m->path));
    char *title=g_strdup_printf("%s — Cube Player",m->title); cp_window_title(a->window,title);g_free(title);
    a->queue_scroll=MAX(0,index-2); a->sub_delay=0;player_number(&a->player,"sub-delay",0);
    dirty(a);
}
static gint path_cmp(gconstpointer x,gconstpointer y) {return g_utf8_collate(*(char *const *)x,*(char *const *)y);}
static int add_media(App *a,const char *input,int depth) {
    if(a->queue->len>=2048) {toast(a,"The queue is full (2,048 items).");return 0;}
    if(depth>3) {toast(a,"This playlist contains too many nested playlists.");return 0;}
    GError *e=NULL;char *path=media_path(input,&e);
    if(!path) {toast(a,e?e->message:"Could not open this item.");g_clear_error(&e);return 0;}
    int before=a->queue->len;
    if(g_file_test(path,G_FILE_TEST_IS_DIR)) {
        GDir *dir=g_dir_open(path,0,&e);
        if(dir) {
            GPtrArray *names=g_ptr_array_new_with_free_func(g_free);const char *name;
            while((name=g_dir_read_name(dir)) && names->len<4096) if(media_supported(name)) g_ptr_array_add(names,g_build_filename(path,name,NULL));
            g_dir_close(dir);g_ptr_array_sort(names,path_cmp);
            for(guint i=0;i<names->len;i++) if(g_file_test(g_ptr_array_index(names,i),G_FILE_TEST_IS_REGULAR)) add_media(a,g_ptr_array_index(names,i),depth+1);
            g_ptr_array_free(names,TRUE);
        } else {toast(a,e->message);g_clear_error(&e);}
    } else if(media_subtitle(path) && a->player.loaded) {
        player_subtitle(&a->player,path);toast(a,"Subtitle file added.");
    } else {
        const char *ext=strrchr(path,'.'); bool playlist=path[0]=='/' && ext && (!g_ascii_strcasecmp(ext,".m3u") || !g_ascii_strcasecmp(ext,".m3u8"));
        char *data=NULL;gsize size=0;
        GStatBuf st; if(playlist && (!g_stat(path,&st) && st.st_size<=2*1024*1024) && g_file_get_contents(path,&data,&size,NULL) && !strstr(data,"#EXT-X-")) {
            char *dir=g_path_get_dirname(path);char **lines=g_strsplit(data,"\n",-1);
            for(int i=0;lines[i] && a->queue->len<2048;i++) {
                char *line=g_strstrip(lines[i]); if(!*line || *line=='#') continue;
                char *scheme=g_uri_parse_scheme(line);
                char *item=scheme || g_path_is_absolute(line)?g_strdup(line):g_build_filename(dir,line,NULL);
                add_media(a,item,depth+1);g_free(item);g_free(scheme);
            }g_strfreev(lines);g_free(dir);
        } else g_ptr_array_add(a->queue,media_new(path));
        g_free(data);
    }
    g_free(path);dirty(a);return (int)a->queue->len-before;
}
static void open_input(App *a,const char *input,bool append) {
    int first=a->queue->len;
    int count=add_media(a,input,0);
    if(count && (!append || a->current<0)) play_index(a,first);
    else if(count) {char buf[96];g_snprintf(buf,sizeof(buf),"Added %d item%s to the queue.",count,count==1?"":"s");toast(a,buf);}
}
static void next(App *a,int direction,bool eof) {
    if(!a->queue->len) return;
    if(eof && a->state.repeat==2) {play_index(a,a->current);return;}
    int index=a->current+direction;
    if(a->state.shuffle && a->queue->len>1) {index=g_random_int_range(0,a->queue->len-1);if(index>=a->current) index++;}
    if(index<0) index=a->queue->len-1;
    if(index>=(int)a->queue->len) {
        if(!eof || a->state.repeat==1) index=0;
        else {a->player.position=a->player.duration;toast(a,"Queue finished.");dirty(a);return;}
    }play_index(a,index);
}
static void entry_free(gpointer p) {Entry *e=p;g_free(e->path);g_free(e->name);g_free(e);}
static gint entry_cmp(gconstpointer x,gconstpointer y) {
    const Entry *a=*(Entry *const *)x,*b=*(Entry *const *)y;
    return a->dir!=b->dir?(a->dir?-1:1):g_utf8_collate(a->name,b->name);
}
static void browse(App *a,const char *path) {
    GError *error=NULL;GDir *dir=g_dir_open(path,0,&error);
    if(!dir) {toast(a,error->message);g_error_free(error);return;}
    char *copy=g_strdup(path);g_free(a->folder);a->folder=copy;g_ptr_array_set_size(a->entries,0);
    const char *name;
    while((name=g_dir_read_name(dir)) && a->entries->len<4096) {
        if(!a->hidden && *name=='.') continue;
        char *full=g_build_filename(path,name,NULL);bool directory=g_file_test(full,G_FILE_TEST_IS_DIR);
        if(!directory && !g_file_test(full,G_FILE_TEST_IS_REGULAR)) {g_free(full);continue;}
        if(!directory && !(a->subtitle_mode?media_subtitle(name):media_supported(name))) {g_free(full);continue;}
        Entry *e=g_new0(Entry,1);e->path=full;e->name=g_filename_display_name(name);e->dir=directory;g_ptr_array_add(a->entries,e);
    }g_dir_close(dir);g_ptr_array_sort(a->entries,entry_cmp);a->file_scroll=0;a->file_selected=0;a->file_click_time=0;a->file_click_index=-1;
    g_strlcpy(a->input,a->folder,sizeof(a->input));a->input_cursor=strlen(a->input);a->input_select=false;a->input_focus=false;dirty(a);
}
static void open_files(App *a,bool append,bool subtitle) {
    show_panel(a,FILES);a->add_mode=append;a->subtitle_mode=subtitle;
    if(!a->folder) {
        const char *dir=g_get_user_special_dir(G_USER_DIRECTORY_VIDEOS);
        browse(a,dir && g_file_test(dir,G_FILE_TEST_IS_DIR)?dir:g_get_home_dir());
    }else browse(a,a->folder);
}
static void file_activate(App *a,bool append) {
    if(a->file_selected<0 || a->file_selected>=(int)a->entries->len) return;
    Entry *e=g_ptr_array_index(a->entries,a->file_selected);
    if(e->dir) {browse(a,e->path);return;}
    char *path=g_strdup(e->path);
    if(a->subtitle_mode) {player_subtitle(&a->player,path);toast(a,"Subtitle file added.");}
    else open_input(a,path,append);
    g_free(path);show_panel(a,NONE);
}

static void input_box(App *a,double x,double y,double w,double h,const char *placeholder) {
    box(a,x,y,w,h,6,a->theme.surface);
    if(a->input_focus) {ink(a,a->theme.ink,1);rounded(a,x+.5,y+.5,w-1,h-1,6);cairo_set_line_width(a->cr,1.3);cairo_stroke(a->cr);}
    if(a->input_select && a->input_focus) box(a,x+9,y+7,w-18,h-14,2,a->theme.hover);
    if(!a->input_focus || !*a->input) text(a,*a->input?a->input:placeholder,x+12,y+10,13,*a->input?a->theme.ink:a->theme.muted,w-24,false);
    if(a->input_focus) {
        a->input_cursor=MIN(a->input_cursor,strlen(a->input));
        PangoLayout *layout=pango_cairo_create_layout(a->cr);
        PangoFontDescription *font=pango_font_description_from_string("Sans");pango_font_description_set_absolute_size(font,13*PANGO_SCALE);
        pango_layout_set_font_description(layout,font);pango_layout_set_text(layout,a->input,-1);
        PangoRectangle cursor;pango_layout_index_to_pos(layout,a->input_cursor,&cursor);
        double cx=cursor.x/(double)PANGO_SCALE,offset=MAX(0,cx-w+28);
        cairo_save(a->cr);cairo_rectangle(a->cr,x+10,y+3,w-20,h-6);cairo_clip(a->cr);
        if(*a->input) {ink(a,a->theme.ink,1);cairo_move_to(a->cr,x+12-offset,y+10);pango_cairo_show_layout(a->cr,layout);}
        if(!a->input_select) line(a,x+12+cx-offset,y+10,x+12+cx-offset,y+h-10,a->theme.ink,1.2);
        cairo_restore(a->cr);pango_font_description_free(font);g_object_unref(layout);
    }
    target(a,A_INPUT,0,x,y,w,h,"File path or media URL");
}
static void sidebar(App *a,double x,double top,double width,double height) {
    rect(a,x,top,width,height,a->theme.bar);line(a,x,top,x,top+height,a->theme.surface,1);
    button(a,A_QUEUE,0,x+16,top+16,93,32,"Queue",NULL,!a->recent);
    button(a,A_RECENT,0,x+113,top+16,93,32,"Recent",NULL,a->recent);
    button(a,A_ADD,0,x+width-52,top+16,32,32,NULL,"plus",false);
    char label[64];int count=a->recent?a->state.recent->len:a->queue->len;
    g_snprintf(label,sizeof(label),"%d ITEM%s",count,count==1?"":"S");text(a,label,x+22,top+66,10,a->theme.muted,width-44,true);
    double y=top+92, bottom=top+height-60;
    int visible=MAX(1,(bottom-y)/62);a->queue_scroll=CLAMP(a->queue_scroll,0,MAX(0,count-visible));
    if(!count) {
        icon(a,a->recent?"play":"queue",x+width/2-15,top+height/2-50,30,a->theme.muted);
        centered(a,a->recent?"Nothing played yet":"Make yourself a queue",x+12,top+height/2,width-24,13,a->theme.ink,true);
        centered(a,a->recent?"Your recent media appears here.":"Add a few files to keep things going.",x+12,top+height/2+26,width-24,11,a->theme.muted,false);
    }
    for(int i=a->queue_scroll;i<count && y+58<=bottom;i++,y+=62) {
        char *tmp=NULL; const char *name,*path;
        if(a->recent) {path=g_ptr_array_index(a->state.recent,i);name=tmp=media_title(path);}
        else {Media *m=g_ptr_array_index(a->queue,i);path=m->path;name=m->title;}
        bool current=!a->recent && i==a->current;
        Hit area={x+12,y,width-24,56,0,0,{0}};
        if(current || inside(&area,a->mouse_x,a->mouse_y)) box(a,x+12,y,width-24,56,6,a->theme.surface);
        box(a,x+22,y+12,30,32,4,current?a->theme.ink:a->theme.hover);
        if(current) icon(a,a->player.paused?"pause":"play",x+28,y+19,18,a->theme.background);
        else {char n[12];g_snprintf(n,sizeof(n),"%02d",i+1);centered(a,n,x+22,y+21,30,10,a->theme.muted,true);}
        text(a,name,x+64,y+9,12,a->theme.ink,width-106,true);
        const char *ext=strrchr(path,'.');char *kind=g_ascii_strup(ext?ext+1:"MEDIA",-1);
        char *detail=g_strdup_printf("%s%s",current?(a->player.paused?"PAUSED · ":a->player.loaded?"PLAYING · ":"SELECTED · "):"",strlen(kind)<10?kind:"STREAM");
        text(a,detail,x+64,y+32,9,a->theme.muted,width-106,false);g_free(kind);g_free(detail);
        target(a,a->recent?A_RECENT_ITEM:A_ITEM,i,x+12,y,width-52,56,name);
        if(!a->recent) button(a,A_REMOVE,i,x+width-40,y+17,24,24,NULL,"close",false);
        g_free(tmp);
    }
    line(a,x+16,bottom+8,x+width-16,bottom+8,a->theme.surface,1);
    button(a,A_ADD,0,x+16,bottom+18,width-90,32,"Add media","plus",false);
    if(!a->recent && count) button(a,A_CLEAR,0,x+width-68,bottom+18,52,32,"Clear",NULL,false);
    if(count>visible) {double h=(bottom-top-92)*visible/count, yy=top+92+(bottom-top-92-h)*a->queue_scroll/MAX(1,count-visible);box(a,x+width-5,yy,2,h,1,a->theme.muted);}
}
static void transport(App *a,double width,double bottom,bool fullscreen) {
    Player *p=&a->player;
    double seek_y=bottom-89, y=bottom-62;
    char pos[32],dur[32];format_time(p->position,pos);format_time(p->duration,dur);
    text(a,pos,26,seek_y-7,11,a->theme.muted,65,false);
    text(a,p->duration>0?dur:"--:--",width-72,seek_y-7,11,a->theme.muted,60,false);
    double sx=88, sw=MAX(40,width-176), ratio=p->duration>0?CLAMP(p->position/p->duration,0,1):0;
    box(a,sx,seek_y,sw,3,1.5,a->theme.hover);box(a,sx,seek_y,MAX(3,sw*ratio),3,1.5,a->theme.ink);
    ink(a,a->theme.ink,1);cairo_arc(a->cr,sx+sw*ratio,seek_y+1.5,4,0,2*M_PI);cairo_fill(a->cr);
    target(a,A_SEEK,0,sx,seek_y-10,sw,24,"Seek");
    for(guint i=0;i<p->chapters->len;i++) {Chapter *c=&g_array_index(p->chapters,Chapter,i);if(p->duration>0) line(a,sx+sw*c->time/p->duration,seek_y-3,sx+sw*c->time/p->duration,seek_y+5,a->theme.muted,1);}
    button(a,A_SHUFFLE,0,20,y,34,36,NULL,"shuffle",a->state.shuffle);
    button(a,A_REPEAT,0,58,y,34,36,NULL,"repeat",a->state.repeat>0);
    if(a->state.repeat==2) text(a,"1",81,y+2,8,a->theme.background,10,true);
    if(width>750) {char rate[24];g_snprintf(rate,sizeof(rate),"%.3gx",p->speed);button(a,A_SPEED,0,100,y,54,36,rate,NULL,false);}
    double center=width/2;
    button(a,A_PREV,0,center-94,y,36,36,NULL,"prev",false);
    button(a,A_BACK,0,center-54,y,32,36,"−10",NULL,false);
    button(a,A_PLAY,0,center-20,y-4,44,44,NULL,p->loaded&&!p->paused?"pause":"play",true);
    button(a,A_FORWARD,0,center+32,y,32,36,"+10",NULL,false);
    button(a,A_NEXT,0,center+68,y,36,36,NULL,"next",false);
    button(a,A_MUTE,0,width-208,y,32,36,NULL,p->mute?"mute":"volume",false);
    double vx=width-168, volw=58;
    box(a,vx,y+17,volw,3,1.5,a->theme.hover);box(a,vx,y+17,MAX(2,volw*p->volume/100),3,1.5,a->theme.ink);
    target(a,A_VOLUME,0,vx,y+6,volw,26,"Volume");
    button(a,A_TRACKS,0,width-100,y,34,36,NULL,"cc",a->panel==TRACKS);
    button(a,A_FULL,0,width-58,y,36,36,NULL,"full",fullscreen);
}
static void panel_base(App *a,double *x,double *y,double *w,double *h,const char *title,const char *subtitle,double desired_h) {
    ink(a,"000000",.38);cairo_paint(a->cr);
    *w=MIN(660,a->vw-40);*h=MIN(desired_h,a->vh-40);*x=(a->vw-*w)/2;*y=(a->vh-*h)/2;
    box(a,*x,*y,*w,*h,12,a->theme.background);
    ink(a,a->theme.muted,.3);rounded(a,*x+.5,*y+.5,*w-1,*h-1,12);cairo_set_line_width(a->cr,1);cairo_stroke(a->cr);
    a->nhit=0;
    text(a,title,*x+28,*y+24,22,a->theme.ink,*w-96,true);
    text(a,subtitle,*x+28,*y+58,12,a->theme.muted,*w-56,false);
    button(a,A_DISMISS,0,*x+*w-52,*y+22,30,30,NULL,"close",false);
}
static void file_panel(App *a) {
    double x,y,w,h;panel_base(a,&x,&y,&w,&h,a->subtitle_mode?"Add subtitles":a->add_mode?"Add to your queue":"Open something good",a->subtitle_mode?"Choose an SRT, ASS, or WebVTT file.":"Choose a file, or browse a folder of media.",620);
    double top=y+90;
    button(a,A_PARENT,0,x+24,top,36,40,NULL,"up",false);input_box(a,x+68,top,w-96,40,"File or folder path");
    double side=128, listx=x+side+22, listtop=top+54, listbottom=y+h-68;
    const char *names[]={"Home","Videos","Music","Downloads"};int actions[]={A_HOME,A_VIDEOS,A_MUSIC,A_DOWNLOADS};
    for(int i=0;i<4;i++) button(a,actions[i],0,x+18,listtop+i*40,side-14,34,names[i],NULL,false);
    if(h>420) button(a,A_HIDDEN,0,x+18,listbottom-36,side-14,32,a->hidden?"Hide dotfiles":"Show dotfiles",NULL,false);
    line(a,x+side+8,listtop,x+side+8,listbottom,a->theme.surface,1);
    int visible=MAX(1,(listbottom-listtop)/40), count=a->entries->len;
    a->file_scroll=CLAMP(a->file_scroll,0,MAX(0,count-visible));
    for(int i=a->file_scroll;i<count && i<a->file_scroll+visible;i++) {
        Entry *e=g_ptr_array_index(a->entries,i);double yy=listtop+(i-a->file_scroll)*40;
        bool selected=i==a->file_selected;
        if(selected) box(a,listx,yy,w-side-46,36,4,a->theme.surface);
        icon(a,e->dir?"folder":a->subtitle_mode?"cc":"file",listx+10,yy+8,20,a->theme.muted);
        text(a,e->name,listx+42,yy+9,12,a->theme.ink,w-side-100,selected);
        target(a,A_FILE,i,listx,yy,w-side-46,36,e->name);
    }
    if(!count) centered(a,"No matching files in this folder.",listx,listtop+40,w-side-46,12,a->theme.muted,false);
    if(count>visible) {double sh=(listbottom-listtop)*visible/count;box(a,x+w-18,listtop+(listbottom-listtop-sh)*a->file_scroll/MAX(1,count-visible),2,sh,1,a->theme.muted);}
    line(a,x+24,y+h-61,x+w-24,y+h-61,a->theme.surface,1);
    text(a,"↑ ↓ select   ·   Enter open   ·   Ctrl+L path",x+28,y+h-37,10,a->theme.muted,w-230,false);
    button(a,A_FILE_OPEN,0,x+w-176,y+h-49,148,34,a->subtitle_mode?"Use subtitles":a->add_mode?"Add to queue":"Open selected",NULL,true);
}
static void settings_panel(App *a) {
    double x,y,w,h;panel_base(a,&x,&y,&w,&h,"Make it yours","A familiar part of your Whitecube desktop.",520);
    double top=y+100, row=MIN(67,(h-152)/5);
    text(a,"Appearance",x+28,top+8,13,a->theme.ink,w-280,true);
    const char *themes[]={"Whitecube","Light","Dark"};
    for(int i=0;i<3;i++) button(a,A_THEME,i,x+w-284+i*84,top,80,34,themes[i],NULL,a->state.theme==i);
    top+=row;text(a,"Remember position",x+28,top+8,13,a->theme.ink,w-220,true);
    button(a,A_RESUME,0,x+w-134,top,106,34,a->state.resume?"On":"Off",NULL,a->state.resume);
    top+=row;text(a,"Playback speed",x+28,top+8,13,a->theme.ink,w-240,true);
    button(a,A_SLOWER,0,x+w-194,top,36,34,"−",NULL,false);
    char speed[32];g_snprintf(speed,sizeof(speed),"%.3g×",a->player.speed);centered(a,speed,x+w-152,top+8,76,13,a->theme.ink,true);
    button(a,A_FASTER,0,x+w-64,top,36,34,"+",NULL,false);
    top+=row;text(a,"Recent media & positions",x+28,top+8,13,a->theme.ink,w-230,true);
    button(a,A_FORGET,0,x+w-174,top,146,34,"Clear history",NULL,false);
    top+=row;text(a,"Keyboard shortcuts",x+28,top+8,13,a->theme.ink,w-210,true);
    button(a,A_HELP,0,x+w-134,top,106,34,"View keys",NULL,false);
    line(a,x+28,y+h-56,x+w-28,y+h-56,a->theme.surface,1);
    text(a,"CUBE PLAYER  1.0.0",x+28,y+h-35,10,a->theme.muted,190,true);
    text(a,cp_window_renderer(a->window),x+w-224,y+h-35,10,a->theme.muted,200,false);
}
static void tracks_panel(App *a) {
    double x,y,w,h;panel_base(a,&x,&y,&w,&h,"Sound & subtitles","Choose a track or jump to a chapter.",590);
    double top=y+94, bottom=y+h-82;int index=0;
    int total=4+a->player.chapters->len;
    for(guint i=0;i<a->player.tracks->len;i++) {Track *t=&g_array_index(a->player.tracks,Track,i);if(!strcmp(t->type,"audio") || !strcmp(t->type,"sub")) total++;}
    a->tracks_scroll=CLAMP(a->tracks_scroll,0,MAX(0,total-(int)((bottom-top)/35)));
    bool sub=false;for(guint i=0;i<a->player.tracks->len;i++) {Track *t=&g_array_index(a->player.tracks,Track,i);if(!strcmp(t->type,"sub") && t->selected) sub=true;}
    for(int group=0;group<3;group++) {
        double yy=top+(index++-a->tracks_scroll)*35;
        if(yy>=top && yy+32<=bottom) text(a,group==0?"AUDIO":group==1?"SUBTITLES":"CHAPTERS",x+28,yy+8,10,a->theme.muted,w-56,true);
        if(group<2) {
            if(group==1) {
                yy=top+(index++-a->tracks_scroll)*35;
                if(yy>=top && yy+32<=bottom) button(a,A_SUB,-1,x+28,yy,w-56,32,"Off",NULL,!sub);
            }
            for(guint i=0;i<a->player.tracks->len;i++) {
                Track *t=&g_array_index(a->player.tracks,Track,i);
                if(strcmp(t->type,group==0?"audio":"sub")) continue;
                yy=top+(index++-a->tracks_scroll)*35;
                if(yy>=top && yy+32<=bottom) button(a,group==0?A_AUDIO:A_SUB,t->id,x+28,yy,w-56,32,t->label,NULL,t->selected);
            }
        } else for(guint i=0;i<a->player.chapters->len;i++) {
            Chapter *c=&g_array_index(a->player.chapters,Chapter,i);yy=top+(index++-a->tracks_scroll)*35;
            if(yy>=top && yy+32<=bottom) {char time[32],name[320];format_time(c->time,time);g_snprintf(name,sizeof(name),"%s   %s",time,c->title);button(a,A_CHAPTER,i,x+28,yy,w-56,32,name,NULL,false);}
        }
    }
    line(a,x+28,y+h-73,x+w-28,y+h-73,a->theme.surface,1);
    button(a,A_SUBFILE,0,x+28,y+h-56,174,34,"Load subtitle file","plus",false);
    if(w>500) {text(a,"Subtitle delay",x+w-254,y+h-48,11,a->theme.muted,105,false);button(a,A_SUBDELAY_MINUS,0,x+w-152,y+h-56,32,34,"−",NULL,false);char delay[24];g_snprintf(delay,sizeof(delay),"%+.1fs",a->sub_delay);centered(a,delay,x+w-120,y+h-48,60,11,a->theme.ink,false);button(a,A_SUBDELAY_PLUS,0,x+w-60,y+h-56,32,34,"+",NULL,false);}
}
static void help_panel(App *a) {
    double x,y,w,h;panel_base(a,&x,&y,&w,&h,"At your fingertips","Everyday controls, without leaving the keyboard.",530);
    const char *keys[]={"Space / K","← / →","↑ / ↓","F / F11","M","N / P","[ / ]","Ctrl+O","Ctrl+Shift+O","Ctrl+L","Ctrl+,","Tab / Shift+Tab","Escape","Ctrl+Q"};
    const char *labels[]={"Play or pause","Seek 5 seconds (Shift: 30)","Adjust volume","Toggle fullscreen","Mute","Next / previous item","Slower / faster","Open media","Add to queue","Open file path or URL","Preferences","Focus controls; Enter activates","Close panel or leave fullscreen","Quit Cube Player"};
    double gap=MIN(28,(h-110)/14);
    for(int i=0;i<14;i++) {text(a,keys[i],x+30,y+92+i*gap,12,a->theme.ink,190,true);text(a,labels[i],x+232,y+92+i*gap,12,a->theme.muted,w-262,false);}
}
static void location_panel(App *a) {
    double x,y,w,h;panel_base(a,&x,&y,&w,&h,"Open a path or link","Local files and direct HTTP(S) media streams.",260);
    input_box(a,x+28,y+100,w-56,42,"/path/to/media or https://…");
    button(a,A_FILE_ADD,0,x+w-252,y+178,100,36,"Add to queue",NULL,false);
    button(a,A_LOCATION,1,x+w-140,y+178,112,36,"Open media",NULL,true);
}
static void draw_app(void *data,cairo_t *cr,int width,int height) {
    App *a=data;a->cr=cr;a->width=width;a->height=height;
    a->scale=MIN(a->ui_scale,MIN(width/640.,height/400.));a->vw=width/a->scale;a->vh=height/a->scale;
    cairo_scale(cr,a->scale,a->scale);a->nhit=0;
    a->fullscreen=cp_window_is_fullscreen(a->window);
    double w=a->vw,h=a->vh;
    bool side=a->state.sidebar && w>=860 && !a->fullscreen;
    double content=side?w-280:w;
    rect(a,0,0,w,h,a->theme.background);
    if(!a->fullscreen) {
        cube(a,24,17,23,a->theme.ink);text(a,"CUBE",61,14,15,a->theme.ink,72,true);text(a,"PLAYER",61,33,8,a->theme.muted,80,true);
        target(a,A_MOVE,0,140,0,MAX(0,w-340),54,"Move window");
        button(a,A_OPEN,0,content-(side?198:310),12,98,32,"Open","folder",false);
        button(a,A_SIDEBAR,0,content-(side?86:202),12,32,32,NULL,"queue",side);
        if(side) {text(a,"YOUR MEDIA",content+20,23,10,a->theme.muted,150,true);}
        button(a,A_SETTINGS,0,w-136,12,32,32,NULL,"settings",a->panel==SETTINGS);
        button(a,A_MINIMIZE,0,w-100,12,28,32,NULL,"minus",false);
        button(a,A_MAXIMIZE,0,w-68,12,28,32,NULL,"max",false);
        button(a,A_CLOSE,0,w-36,12,28,32,NULL,"close",false);
        line(a,0,56,w,56,a->theme.surface,1);
    }
    double vx=a->fullscreen?0:20,vy=a->fullscreen?0:76,vw=a->fullscreen?w:content-40,vh=a->fullscreen?h:MAX(130,h-246);
    bool video=a->player.video && a->player.loaded;
    box(a,vx,vy,vw,vh,a->fullscreen?0:8,video?"000000":a->theme.panel);
    if(video && a->window) {
        cairo_save(cr);cairo_identity_matrix(cr);
        cp_window_video(a->window,cr,lround(vx*a->scale),lround(vy*a->scale),lround(vw*a->scale),lround(vh*a->scale));cairo_restore(cr);
    } else {
        double cx=vx+vw/2,cy=vy+vh/2;
        if(vh>260) {
            double sz=a->player.loaded?74:86;
            for(int i=2;i>=0;i--) {
                const char *color=i==0?a->theme.ink:i==1?a->theme.cube_top:a->theme.cube_side;
                cube(a,cx-sz/2+i*22,cy-116-i*15,sz,color);
            }
        }
        const char *title=a->player.loading?"Opening your media…":a->player.failed?"This one couldn't play":a->player.loaded?"Let it play.":"A space for your media.";
        centered(a,title,vx+20,cy+(vh>260?8:-48),vw-40,MIN(27,vw/19),a->theme.ink,true);
        const char *subtitle=a->player.failed?a->player.error:a->player.loaded?"Sound on. Settle in.":"Drop a video or audio file here, or choose something to play.";
        centered(a,subtitle,vx+24,cy+(vh>260?51:-8),vw-48,12,a->theme.muted,false);
        if(!a->player.loaded && !a->player.loading) button(a,A_OPEN,0,cx-72,cy+(vh>260?90:24),144,38,"Open media","folder",true);
    }
    target(a,A_PLAY,0,vx,vy,vw,MAX(1,vh-(a->player.loaded?0:80)),"Play / pause");
    bool controls=!a->fullscreen || a->player.paused || a->panel!=NONE || a->focus>=0 || clock_now()-a->last_pointer<3;
    if(controls) {
        if(a->fullscreen) rect(a,0,h-122,w,122,a->theme.background);
        else {
            const char *name=a->current_path?(a->player.title[0]?a->player.title:((Media *)g_ptr_array_index(a->queue,MAX(0,a->current)))->title):"Nothing playing yet";
            text(a,name,26,h-155,14,a->theme.ink,content-130,true);
            const char *status=a->player.loading?"OPENING":a->player.loaded?(a->player.paused?"PAUSED":"NOW PLAYING"):"READY WHEN YOU ARE";
            text(a,status,26,h-131,9,a->theme.muted,content-180,true);
            if(a->player.video) {char dims[32];g_snprintf(dims,sizeof(dims),"%d × %d",a->player.width,a->player.height);text(a,dims,content-120,h-151,10,a->theme.muted,100,false);}
        }
        transport(a,content,h-24,a->fullscreen);
        if(!a->fullscreen) {
            line(a,0,h-30,content,h-30,a->theme.surface,1);
            text(a,"WHITE CUBE  /  GOOD COMPANY",24,h-20,8,a->theme.muted,content-190,true);
            button(a,A_HELP,0,content-146,h-26,122,24,"Keyboard shortcuts",NULL,false);
        }
    }
    if(side) sidebar(a,content,57,280,h-57);
    if(!a->fullscreen) target(a,A_RESIZE,0,w-10,h-10,10,10,"Resize window");
    if(a->panel==FILES) file_panel(a);
    else if(a->panel==SETTINGS) settings_panel(a);
    else if(a->panel==TRACKS) tracks_panel(a);
    else if(a->panel==HELP) help_panel(a);
    else if(a->panel==LOCATION) location_panel(a);
    if(a->focus>=0 && a->focus<a->nhit) {
        Hit *t=&a->hit[a->focus];ink(a,a->theme.ink,1);rounded(a,t->x-2,t->y-2,t->w+4,t->h+4,6);cairo_set_line_width(cr,2);cairo_stroke(cr);
    }
    if(a->toast_until>clock_now()) {
        double tw=MIN(w-64,620);box(a,(w-tw)/2,h-80,tw,42,6,a->theme.ink);centered(a,a->toast,(w-tw)/2+12,h-68,tw-24,12,a->theme.background,false);
    }
}

static void speed(App *a,double delta) {
    double v=CLAMP(round((a->player.speed+delta)*4)/4,.25,3);player_number(&a->player,"speed",v);a->player.speed=v;a->state.speed=v;dirty(a);
}
static void volume(App *a,double v) {v=CLAMP(v,0,100);player_number(&a->player,"volume",v);a->player.volume=v;a->state.volume=v;dirty(a);}
static void act(App *a,int action,int value) {
    Player *p=&a->player;a->last_pointer=clock_now();
    switch(action) {
    case A_MOVE:cp_window_move(a->window);break;
    case A_CLOSE:cp_window_close(a->window);break;
    case A_MINIMIZE:cp_window_minimize(a->window);break;
    case A_MAXIMIZE:cp_window_maximize(a->window);break;
    case A_RESIZE:cp_window_resize(a->window,10);break;
    case A_OPEN:open_files(a,false,false);break;
    case A_ADD:open_files(a,true,false);break;
    case A_SETTINGS:show_panel(a,a->panel==SETTINGS?NONE:SETTINGS);break;
    case A_PLAY:
        if(p->loaded) {p->paused=!p->paused;player_flag(p,"pause",p->paused);}
        else if(a->queue->len) play_index(a,a->current<0?0:a->current);
        else open_files(a,false,false);
        break;
    case A_PREV:if(p->position>3) player_seek(p,0,true);else next(a,-1,false);break;
    case A_NEXT:next(a,1,false);break;
    case A_BACK:player_seek(p,-10,false);break;
    case A_FORWARD:player_seek(p,10,false);break;
    case A_MUTE:p->mute=!p->mute;player_flag(p,"mute",p->mute);break;
    case A_FULL:cp_window_fullscreen(a->window,!a->fullscreen);break;
    case A_SIDEBAR:a->state.sidebar=!a->state.sidebar;break;
    case A_SHUFFLE:a->state.shuffle=!a->state.shuffle;toast(a,a->state.shuffle?"Shuffle on.":"Shuffle off.");break;
    case A_REPEAT:a->state.repeat=(a->state.repeat+1)%3;toast(a,a->state.repeat==0?"Repeat off.":a->state.repeat==1?"Repeat queue.":"Repeat current item.");break;
    case A_SPEED:player_number(p,"speed",p->speed>=2?1:p->speed+.25);break;
    case A_SLOWER:speed(a,-.25);break;
    case A_FASTER:speed(a,.25);break;
    case A_TRACKS:show_panel(a,a->panel==TRACKS?NONE:TRACKS);break;
    case A_HELP:show_panel(a,HELP);break;
    case A_QUEUE:a->recent=false;a->queue_scroll=0;break;
    case A_RECENT:a->recent=true;a->queue_scroll=0;break;
    case A_ITEM:play_index(a,value);break;
    case A_RECENT_ITEM:
        if(value>=0 && value<(int)a->state.recent->len) {char *path=g_strdup(g_ptr_array_index(a->state.recent,value));open_input(a,path,false);g_free(path);}break;
    case A_REMOVE:
        if(value>=0 && value<(int)a->queue->len) {
            if(value==a->current) {
                remember(a);const char *stop[]={"stop",NULL};player_command(p,stop);
                p->loaded=false;p->loading=false;g_clear_pointer(&a->current_path,g_free);
                g_ptr_array_remove_index(a->queue,value);a->current=-1;
                if(a->queue->len) play_index(a,MIN(value,(int)a->queue->len-1));
                else {p->position=p->duration=0;p->title[0]=0;cp_window_title(a->window,"Cube Player");}
            } else {g_ptr_array_remove_index(a->queue,value);if(value<a->current) a->current--;}
        }break;
    case A_CLEAR: {
        remember(a);const char *stop[]={"stop",NULL};player_command(p,stop);g_ptr_array_set_size(a->queue,0);
        a->current=-1;g_clear_pointer(&a->current_path,g_free);p->loaded=p->loading=p->video=false;p->position=p->duration=0;p->title[0]=0;
        cp_window_title(a->window,"Cube Player");break;
    }
    case A_DISMISS:show_panel(a,NONE);break;
    case A_THEME:a->state.theme=value;theme_reload(a);break;
    case A_RESUME:a->state.resume=!a->state.resume;break;
    case A_FORGET:state_forget(&a->state);a->suppress_history=true;state_save(&a->state);toast(a,"Recent media and saved positions cleared.");break;
    case A_AUDIO:player_track(p,"audio",value);break;
    case A_SUB:player_track(p,"sub",value);break;
    case A_SUBFILE:if(p->loaded) open_files(a,true,true);else toast(a,"Open media before adding subtitles.");break;
    case A_SUBDELAY_MINUS:case A_SUBDELAY_PLUS:a->sub_delay+=action==A_SUBDELAY_MINUS?-.1:.1;player_number(p,"sub-delay",a->sub_delay);break;
    case A_CHAPTER:if(value>=0 && value<(int)p->chapters->len) {player_seek(p,g_array_index(p->chapters,Chapter,value).time,true);show_panel(a,NONE);}break;
    case A_HOME:browse(a,g_get_home_dir());break;
    case A_VIDEOS:case A_MUSIC:case A_DOWNLOADS: {
        GUserDirectory kind=action==A_VIDEOS?G_USER_DIRECTORY_VIDEOS:action==A_MUSIC?G_USER_DIRECTORY_MUSIC:G_USER_DIRECTORY_DOWNLOAD;
        const char *path=g_get_user_special_dir(kind);if(path && g_file_test(path,G_FILE_TEST_IS_DIR)) browse(a,path);else browse(a,g_get_home_dir());break;
    }
    case A_PARENT:if(a->folder) {char *path=g_path_get_dirname(a->folder);browse(a,path);g_free(path);}break;
    case A_HIDDEN:a->hidden=!a->hidden;browse(a,a->folder);break;
    case A_FILE:
        if(a->file_click_index==value && clock_now()-a->file_click_time<.45 && value>=0 && value<(int)a->entries->len) file_activate(a,a->add_mode);
        else {a->file_selected=value;a->input_focus=false;a->file_click_index=value;a->file_click_time=clock_now();}break;
    case A_FILE_OPEN:file_activate(a,a->add_mode);break;
    case A_INPUT:a->input_focus=true;a->input_select=true;a->input_cursor=strlen(a->input);break;
    case A_LOCATION:
        if(value==0) {show_panel(a,LOCATION);a->input[0]=0;a->input_cursor=0;a->input_select=false;a->input_focus=true;}
        else if(*a->input) {int n=a->queue->len;open_input(a,a->input,false);if((int)a->queue->len>n) show_panel(a,NONE);}
        break;
    case A_FILE_ADD:if(*a->input) {int n=a->queue->len;open_input(a,a->input,true);if((int)a->queue->len>n) show_panel(a,NONE);}break;
    default:break;
    }dirty(a);
}
static Hit *hit_at(App *a,double x,double y) {
    for(int i=a->nhit-1;i>=0;i--) if(inside(&a->hit[i],x,y)) return &a->hit[i];
    return NULL;
}
static void drag_update(App *a,double x) {
    for(int i=0;i<a->nhit;i++) if(a->hit[i].action==a->drag_action) {
        Hit *t=&a->hit[i];double ratio=CLAMP((x-t->x)/t->w,0,1);
        if(t->action==A_VOLUME) volume(a,ratio*100);
        else if(a->player.duration>0 && a->player.seekable) {double pos=ratio*a->player.duration;player_seek(&a->player,pos,true);a->player.position=pos;}
        break;
    }dirty(a);
}
static void click_app(void *data,double px,double py,uint32_t button_code) {
    App *a=data;double x=px/a->scale,y=py/a->scale;a->mouse_x=x;a->mouse_y=y;a->focus=-1;
    if(button_code==BTN_RIGHT) {act(a,A_TRACKS,0);return;}
    if(button_code!=BTN_LEFT) return;
    Hit *h=hit_at(a,x,y);
    if(h) {
        if(h->action==A_SEEK || h->action==A_VOLUME) {a->dragging=true;a->drag_action=h->action;drag_update(a,x);}
        else act(a,h->action,h->value);
    }
}
static void motion_app(void *data,double px,double py) {
    App *a=data;a->mouse_x=px/a->scale;a->mouse_y=py/a->scale;a->last_pointer=clock_now();
    if(a->dragging) drag_update(a,a->mouse_x);
    dirty(a);
}
static void release_app(void *data,double px,double py,uint32_t button_code) {
    (void)py;App *a=data;if(button_code==BTN_LEFT && a->dragging) {drag_update(a,px/a->scale);a->dragging=false;}
}
static void scroll_app(void *data,double amount) {
    App *a=data;int n=amount>0?1:-1;
    if(a->panel==FILES) a->file_scroll+=n*3;
    else if(a->panel==TRACKS) a->tracks_scroll+=n*2;
    else if(a->panel==NONE && a->state.sidebar && a->vw>=860 && a->mouse_x>a->vw-280) a->queue_scroll+=n*2;
    else if(a->panel==NONE) volume(a,a->player.volume-n*5);
    dirty(a);
}
static void insert(App *a,const char *value) {
    if(!g_utf8_validate(value,-1,NULL)) return;
    if(a->input_select) {a->input[0]=0;a->input_cursor=0;}
    a->input_select=false;
    char *line=g_strdup(value);char *end=strpbrk(line,"\r\n");if(end) *end=0;
    size_t len=strlen(a->input),add=strlen(line);
    a->input_cursor=MIN(a->input_cursor,len);
    if(len+add<sizeof(a->input)) {
        memmove(a->input+a->input_cursor+add,a->input+a->input_cursor,len-a->input_cursor+1);
        memcpy(a->input+a->input_cursor,line,add);a->input_cursor+=add;
    }
    else toast(a,"That path or URL is too long.");
    g_free(line);dirty(a);
}
static void paste_app(void *data,const char *value) {App *a=data;if(a->input_focus) insert(a,value);}
static void drop_app(void *data,const char *value) {
    App *a=data;char **lines=g_strsplit(value,"\n",-1);int first=a->queue->len;
    for(int i=0;lines[i];i++) {char *v=lines[i];size_t n=strlen(v);if(n && v[n-1]=='\r') v[n-1]=0;if(*v && *v!='#') add_media(a,v,0);}
    if((int)a->queue->len>first && !a->player.loaded) play_index(a,first);
    else if((int)a->queue->len>first) toast(a,"Dropped media added to the queue.");
    g_strfreev(lines);show_panel(a,NONE);
}
static void submit_input(App *a) {
    if(a->panel==LOCATION) {act(a,A_LOCATION,1);return;}
    GError *e=NULL;char *path=media_path(a->input,&e);
    if(!path) {toast(a,e?e->message:"Could not open this path.");g_clear_error(&e);return;}
    if(g_file_test(path,G_FILE_TEST_IS_DIR)) browse(a,path);
    else {if(a->subtitle_mode) {player_subtitle(&a->player,path);show_panel(a,NONE);}else {int n=a->queue->len;open_input(a,path,a->add_mode);if((int)a->queue->len>n) show_panel(a,NONE);}}
    g_free(path);
}
static void key_app(void *data,xkb_keysym_t key,const char *utf8,bool ctrl,bool shift,bool alt) {
    App *a=data;Player *p=&a->player;(void)alt;a->last_pointer=clock_now();dirty(a);
    if(key==XKB_KEY_Escape) {if(a->panel!=NONE) show_panel(a,NONE);else if(a->fullscreen) cp_window_fullscreen(a->window,false);else a->focus=-1;return;}
    if(ctrl && (key==XKB_KEY_q || key==XKB_KEY_Q)) {act(a,A_CLOSE,0);return;}
    if(ctrl && (key==XKB_KEY_o || key==XKB_KEY_O)) {open_files(a,shift,false);return;}
    if(ctrl && key==XKB_KEY_comma) {act(a,A_SETTINGS,0);return;}
    if(ctrl && (key==XKB_KEY_l || key==XKB_KEY_L)) {
        if(a->panel==FILES) {a->input_focus=true;a->input_select=true;a->input_cursor=strlen(a->input);}
        else act(a,A_LOCATION,0);
        return;
    }
    if(key==XKB_KEY_Tab || key==XKB_KEY_ISO_Left_Tab) {
        bool reverse=shift || key==XKB_KEY_ISO_Left_Tab;
        if(a->nhit) {a->focus=(a->focus+(reverse?-1:1)+a->nhit)%a->nhit;a->input_focus=a->hit[a->focus].action==A_INPUT;}
        return;
    }
    if(a->input_focus) {
        if(ctrl && (key==XKB_KEY_v || key==XKB_KEY_V)) cp_window_paste(a->window);
        else if(ctrl && (key==XKB_KEY_a || key==XKB_KEY_A)) a->input_select=true;
        else if(ctrl && (key==XKB_KEY_c || key==XKB_KEY_C)) cp_window_copy(a->window,a->input);
        else if(ctrl && (key==XKB_KEY_x || key==XKB_KEY_X) && a->input_select) {cp_window_copy(a->window,a->input);a->input[0]=0;a->input_cursor=0;a->input_select=false;}
        else if(key==XKB_KEY_BackSpace) {
            if(a->input_select || ctrl) {a->input[0]=0;a->input_cursor=0;}
            else {
                char *end=g_utf8_find_prev_char(a->input,a->input+a->input_cursor);
                if(end) {size_t start=end-a->input;memmove(end,a->input+a->input_cursor,strlen(a->input+a->input_cursor)+1);a->input_cursor=start;}
            }
            a->input_select=false;
        } else if(key==XKB_KEY_Delete) {
            if(a->input_select) {a->input[0]=0;a->input_cursor=0;}
            else if(a->input_cursor<strlen(a->input)) {char *next=g_utf8_next_char(a->input+a->input_cursor);memmove(a->input+a->input_cursor,next,strlen(next)+1);}
            a->input_select=false;
        } else if(key==XKB_KEY_Left || key==XKB_KEY_Right || key==XKB_KEY_Home || key==XKB_KEY_End) {
            if(key==XKB_KEY_Home || (key==XKB_KEY_Left && a->input_select)) a->input_cursor=0;
            else if(key==XKB_KEY_End || (key==XKB_KEY_Right && a->input_select)) a->input_cursor=strlen(a->input);
            else if(key==XKB_KEY_Left) {char *prev=g_utf8_find_prev_char(a->input,a->input+a->input_cursor);if(prev) a->input_cursor=prev-a->input;}
            else if(a->input_cursor<strlen(a->input)) a->input_cursor=g_utf8_next_char(a->input+a->input_cursor)-a->input;
            a->input_select=false;
        }
        else if(key==XKB_KEY_Return || key==XKB_KEY_KP_Enter) submit_input(a);
        else if(!ctrl && !alt && utf8 && *utf8 && (unsigned char)*utf8>=32) insert(a,utf8);
        return;
    }
    if((key==XKB_KEY_Return || key==XKB_KEY_KP_Enter || key==XKB_KEY_space) && a->focus>=0 && a->focus<a->nhit) {
        Hit h=a->hit[a->focus];act(a,h.action,h.value);return;
    }
    if(a->panel==FILES) {
        if(key==XKB_KEY_Up || key==XKB_KEY_Down) {
            a->file_selected=CLAMP(a->file_selected+(key==XKB_KEY_Down?1:-1),0,MAX(0,(int)a->entries->len-1));
            int visible=MAX(1,(MIN(620,a->vh-40)-212)/40);
            if(a->file_selected<a->file_scroll) a->file_scroll=a->file_selected;
            if(a->file_selected>=a->file_scroll+visible) a->file_scroll=a->file_selected-visible+1;
        }else if(key==XKB_KEY_Return) file_activate(a,a->add_mode);
        else if(key==XKB_KEY_BackSpace) act(a,A_PARENT,0);
        return;
    }
    if(a->panel!=NONE) return;
    switch(key) {
    case XKB_KEY_space:case XKB_KEY_k:act(a,A_PLAY,0);break;
    case XKB_KEY_Left:player_seek(p,shift?-30:-5,false);break;
    case XKB_KEY_Right:player_seek(p,shift?30:5,false);break;
    case XKB_KEY_Up:volume(a,p->volume+5);break;
    case XKB_KEY_Down:volume(a,p->volume-5);break;
    case XKB_KEY_Home:player_seek(p,0,true);break;
    case XKB_KEY_End:player_seek(p,MAX(0,p->duration-1),true);break;
    case XKB_KEY_f:case XKB_KEY_F11:act(a,A_FULL,0);break;
    case XKB_KEY_m:act(a,A_MUTE,0);break;
    case XKB_KEY_n:act(a,A_NEXT,0);break;
    case XKB_KEY_p:act(a,A_PREV,0);break;
    case XKB_KEY_bracketleft:act(a,A_SLOWER,0);break;
    case XKB_KEY_bracketright:act(a,A_FASTER,0);break;
    case XKB_KEY_l:act(a,A_SIDEBAR,0);break;
    case XKB_KEY_s:act(a,A_SHUFFLE,0);break;
    case XKB_KEY_r:act(a,A_REPEAT,0);break;
    case XKB_KEY_v:case XKB_KEY_j:act(a,A_TRACKS,0);break;
    case XKB_KEY_question:case XKB_KEY_F1:act(a,A_HELP,0);break;
    default:break;
    }
}
static void tick_app(void *data) {
    App *a=data;double now=clock_now();
    if(stopping) cp_window_close(a->window);
    player_poll(&a->player);
    if(a->player.changed) {a->state.volume=a->player.volume;a->state.speed=a->player.speed;dirty(a);}
    if(a->player.loaded) {a->ever_loaded=true;a->max_position=MAX(a->max_position,a->player.position);}
    if(a->player.loaded && a->player.video) a->ever_video=true;
    if(a->player.new_file) {a->player.new_file=false;if(!a->suppress_history && a->current_path) state_remember(&a->state,a->current_path,a->player.position,a->player.duration);}
    if(a->player.ended) {
        a->player.ended=false;
        if(a->current_path && !a->suppress_history) state_remember(&a->state,a->current_path,0,a->player.duration);
        next(a,1,true);
    }
    if(now-a->last_save>=5) {remember(a);state_save(&a->state);a->last_save=now;}
    if(now-a->last_theme>=1) {theme_reload(a);a->last_theme=now;dirty(a);}
    if(a->toast_until && now>a->toast_until) {a->toast_until=0;dirty(a);}
    bool visible=now-a->last_pointer<3;
    if(visible!=a->hover_controls) {a->hover_controls=visible;dirty(a);}
    cp_window_hide_cursor(a->window,a->fullscreen && !visible && !a->player.paused && a->panel==NONE && a->focus<0);
    if(a->capture && !a->captured && now-a->started>MAX(.3,a->quit_after>0?a->quit_after-.4:2)) {
        cp_window_snapshot(a->window,a->capture);a->captured=true;
    }
}
static void report(App *a) {
    if(!a->report) return;
    GKeyFile *k=g_key_file_new();
    g_key_file_set_boolean(k,"Playback","ever_loaded",a->ever_loaded);
    g_key_file_set_boolean(k,"Playback","ever_video",a->ever_video);
    g_key_file_set_boolean(k,"Playback","paused",a->player.paused);
    g_key_file_set_boolean(k,"Playback","loaded",a->player.loaded);
    g_key_file_set_boolean(k,"Playback","mute",a->player.mute);
    g_key_file_set_boolean(k,"Playback","fullscreen",a->fullscreen);
    g_key_file_set_double(k,"Playback","position",a->player.position);
    g_key_file_set_double(k,"Playback","max_position",a->max_position);
    g_key_file_set_double(k,"Playback","duration",a->player.duration);
    g_key_file_set_double(k,"Playback","volume",a->player.volume);
    g_key_file_set_double(k,"Playback","speed",a->player.speed);
    g_key_file_set_integer(k,"Playback","queue_count",a->queue->len);
    g_key_file_set_integer(k,"Playback","current",a->current);
    g_key_file_set_integer(k,"Playback","tracks",a->player.tracks->len);
    g_key_file_set_string(k,"Playback","path",a->current_path?a->current_path:"");
    g_key_file_set_string(k,"Playback","title",a->player.title);
    g_key_file_set_string(k,"Playback","error",a->player.error);
    g_key_file_set_string(k,"Playback","renderer",cp_window_renderer(a->window));
    gsize len;char *data=g_key_file_to_data(k,&len,NULL);g_file_set_contents(a->report,data,len,NULL);g_free(data);g_key_file_unref(k);
}
int main(int argc,char **argv) {
    gboolean version=FALSE,software=FALSE,silent=FALSE;char *capture=NULL,*report_path=NULL,*preview=NULL,*theme=NULL;char **files=NULL;double quit_after=0;
    GOptionEntry options[]={
        {"version",0,0,G_OPTION_ARG_NONE,&version,"Show version",NULL},
        {"software",0,0,G_OPTION_ARG_NONE,&software,"Use software video rendering",NULL},
        {"theme",0,0,G_OPTION_ARG_STRING,&theme,"Choose whitecube, light, or dark","THEME"},
        {"capture",0,0,G_OPTION_ARG_FILENAME,&capture,"Save a PNG of the running window","FILE"},
        {"report",0,0,G_OPTION_ARG_FILENAME,&report_path,"Write playback diagnostics on exit","FILE"},
        {"quit-after",0,0,G_OPTION_ARG_DOUBLE,&quit_after,"Close after N seconds (diagnostics)","N"},
        {"silent-test",0,0,G_OPTION_ARG_NONE,&silent,"Use null audio output for diagnostics",NULL},
        {"preview",0,0,G_OPTION_ARG_FILENAME,&preview,"Render the empty interface without a display","PNG"},
        {G_OPTION_REMAINING,0,0,G_OPTION_ARG_FILENAME_ARRAY,&files,"Files, folders, or direct media URLs","MEDIA…"},
        {NULL}
    };
    GOptionContext *context=g_option_context_new("— your Whitecube media player");g_option_context_add_main_entries(context,options,NULL);
    GError *error=NULL;
    if(!g_option_context_parse(context,&argc,&argv,&error)) {g_printerr("Cube Player: %s\n",error->message);g_error_free(error);g_option_context_free(context);return 2;}
    g_option_context_free(context);
    if(version) {puts("Cube Player 1.0.0");return 0;}
    if(!isfinite(quit_after) || quit_after<0) {g_printerr("--quit-after must be a nonnegative number.\n");return 2;}
    App a={.current=-1,.selected=-1,.focus=-1,.scale=1,.ui_scale=1,.capture=capture,.report=report_path,.quit_after=quit_after,.software=software,.silent=silent};
    a.started=a.last_pointer=a.last_save=clock_now();
    state_init(&a.state);a.queue=g_ptr_array_new_with_free_func(media_free);a.entries=g_ptr_array_new_with_free_func(entry_free);
    if(theme) {
        if(!strcmp(theme,"whitecube")) a.state.theme=0;else if(!strcmp(theme,"light")) a.state.theme=1;else if(!strcmp(theme,"dark")) a.state.theme=2;
        else {g_printerr("Unknown theme. Use whitecube, light, or dark.\n");return 2;}
    }
    theme_reload(&a);
    signal(SIGINT,interrupted);signal(SIGTERM,interrupted);signal(SIGPIPE,SIG_IGN);
    int result=0;
    if(!player_init(&a.player,silent)) {g_printerr("Cube Player: could not initialize mpv: %s\n",a.player.error);result=1;goto cleanup;}
    if(preview) {
        cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,1120,740);cairo_t *cr=cairo_create(surface);
        draw_app(&a,cr,1120,740);cairo_destroy(cr);result=cairo_surface_write_to_png(surface,preview)!=CAIRO_STATUS_SUCCESS;cairo_surface_destroy(surface);goto cleanup;
    }
    struct cp_callbacks callbacks={.draw=draw_app,.key=key_app,.click=click_app,.scroll=scroll_app,.tick=tick_app,.paste=paste_app,.drop=drop_app,.motion=motion_app,.release=release_app};
    a.window=cp_window_new(callbacks,&a);
    if(!a.window) {g_printerr("Cube Player: a running Wayland session is required.\n");result=1;goto cleanup;}
    if(!cp_window_attach_player(a.window,a.player.mpv,software)) {g_printerr("Cube Player: no working video renderer.\n");result=1;goto cleanup;}
    player_number(&a.player,"volume",a.state.volume);player_number(&a.player,"speed",a.state.speed);
    for(int i=0;files && files[i];i++) add_media(&a,files[i],0);
    if(a.queue->len) play_index(&a,0);
    result=cp_window_run(a.window,quit_after);
    remember(&a);state_save(&a.state);report(&a);
cleanup:
    cp_window_free(a.window);player_free(&a.player);state_free(&a.state);
    g_ptr_array_free(a.queue,TRUE);g_ptr_array_free(a.entries,TRUE);g_free(a.current_path);g_free(a.folder);
    g_free(capture);g_free(report_path);g_free(preview);g_free(theme);g_strfreev(files);return result;
}
