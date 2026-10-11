#ifndef CB_PLATFORM_H
#define CB_PLATFORM_H
#include <cairo.h>
#include <stdbool.h>
#include <stdint.h>
#include <xkbcommon/xkbcommon.h>
struct cb_window;
struct cb_callbacks {
    void (*draw)(void *,cairo_t *,int,int);
    void (*key)(void *,xkb_keysym_t,const char *,bool,bool,bool);
    void (*click)(void *,double,double,uint32_t);
    void (*scroll)(void *,double);
    void (*tick)(void *);
    void (*paste)(void *,const char *);
};
struct cb_window *cb_window_new(struct cb_callbacks cb,void *data);
int cb_window_run(struct cb_window *,double seconds);
void cb_window_dirty(struct cb_window *);
void cb_window_title(struct cb_window *,const char *);
void cb_window_close(struct cb_window *);
void cb_window_move(struct cb_window *);
void cb_window_resize(struct cb_window *,uint32_t edges);
void cb_window_maximize(struct cb_window *);
void cb_window_minimize(struct cb_window *);
void cb_window_paste(struct cb_window *);
void cb_window_copy(struct cb_window *,const char *);
void cb_window_free(struct cb_window *);
#endif
