#ifndef CP_PLATFORM_H
#define CP_PLATFORM_H
#include <cairo.h>
#include <stdbool.h>
#include <stdint.h>
#include <xkbcommon/xkbcommon.h>
#include <mpv/client.h>
struct cp_window;
struct cp_callbacks {
    void (*draw)(void *,cairo_t *,int,int);
    void (*key)(void *,xkb_keysym_t,const char *,bool,bool,bool);
    void (*click)(void *,double,double,uint32_t);
    void (*scroll)(void *,double);
    void (*tick)(void *);
    void (*paste)(void *,const char *);
    void (*drop)(void *,const char *);
    void (*motion)(void *,double,double);
    void (*release)(void *,double,double,uint32_t);
};
struct cp_window *cp_window_new(struct cp_callbacks cb,void *data);
int cp_window_run(struct cp_window *,double seconds);
void cp_window_dirty(struct cp_window *);
void cp_window_title(struct cp_window *,const char *);
void cp_window_close(struct cp_window *);
void cp_window_move(struct cp_window *);
void cp_window_resize(struct cp_window *,uint32_t edges);
void cp_window_maximize(struct cp_window *);
void cp_window_minimize(struct cp_window *);
void cp_window_paste(struct cp_window *);
void cp_window_copy(struct cp_window *,const char *);
void cp_window_free(struct cp_window *);
bool cp_window_attach_player(struct cp_window *,mpv_handle *,bool software);
void cp_window_detach_player(struct cp_window *);
void cp_window_video(struct cp_window *,cairo_t *,int,int,int,int);
void cp_window_fullscreen(struct cp_window *,bool);
bool cp_window_is_fullscreen(struct cp_window *);
const char *cp_window_renderer(struct cp_window *);
void cp_window_snapshot(struct cp_window *,const char *);
void cp_window_hide_cursor(struct cp_window *,bool);
#endif
