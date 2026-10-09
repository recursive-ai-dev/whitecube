#!/usr/bin/env python3
"""Emit a C regression harness around the compositor's actual focus/workspace helpers.
Wayland seat and configure requests are stubbed; no display socket is required.
"""
from pathlib import Path
source = Path(__file__).with_name('whitecube.c').read_text()
def function(name):
    start = source.index('static ', source.rfind('\n', 0, source.index(name + '(')) + 1)
    start = source.rfind('\n', 0, start) + 1
    brace = source.index('{', source.index(name + '(', start))
    # Forward declarations may precede implementations.
    if ';' in source[source.index(name + '(', start):brace]:
        start = source.index('static ', brace + 1)  # replaced below by line pattern
    import re
    match = re.search(r'^static [^\n]+\b' + name + r'\([^;]*?\) \{', source, re.M)
    start, brace = match.start(), source.index('{', match.start())
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]
print(r'''
#include <stddef.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>
#define WORKSPACES 4
typedef unsigned xkb_keysym_t;
#define XKB_KEY_Left 1
#define XKB_KEY_Right 2
#define XKB_KEY_Up 3
#define XKB_KEY_Down 4
struct wl_list { struct wl_list *prev, *next; };
static void wl_list_init(struct wl_list *n) { n->prev=n->next=n; }
static void wl_list_insert(struct wl_list *at, struct wl_list *n) { n->next=at->next; n->prev=at; at->next->prev=n; at->next=n; }
static bool wl_list_empty(struct wl_list *n) { return n->next==n; }
#define wl_container_of(ptr,sample,member) ((__typeof__(sample))((char *)(ptr)-offsetof(__typeof__(*(sample)), member)))
#define wl_list_for_each(pos,head,member) for (pos=wl_container_of((head)->next,pos,member); &pos->member!=(head); pos=wl_container_of(pos->member.next,pos,member))
struct wc_toplevel;
struct fake_base { void *surface; };
struct fake_xdg { char *app_id; bool activated; struct fake_base *base; };
struct wlr_keyboard { int *keycodes; int num_keycodes; int modifiers; };
struct wc_server { struct wl_list toplevels; unsigned workspace; struct wc_toplevel *focused,*normal,*saved_focus[4],*saved_normal[4]; bool desktop_visible,locked; void *seat; struct { struct wc_toplevel *toplevel; } drag; };
struct wc_toplevel { struct wl_list link; struct wc_server *server; struct fake_xdg *xdg_toplevel; unsigned workspace; bool mapped,hidden,fullscreen,maximized; int layout_x,layout_y,layout_w,layout_h; };
static const char *g_scripts_app_id="cube-scripts";
static unsigned keyboard_clears=0, pointer_clears=0;
static void wc_retile(struct wc_server *s) {}
static void wc_lock_focus(struct wc_server *s) {}
static void wc_cancel_drag(struct wc_server *s) { s->drag.toplevel = NULL; }
static void wlr_xdg_toplevel_set_activated(struct fake_xdg *x,bool active) { x->activated=active; }
static struct wlr_keyboard *wlr_seat_get_keyboard(void *seat) { return NULL; }
static void wlr_seat_keyboard_notify_enter(void *seat,void *surface,int *keycodes,int count,int *mods) {}
static void wlr_seat_keyboard_clear_focus(void *seat) { keyboard_clears++; }
static void wlr_seat_pointer_clear_focus(void *seat) { pointer_clears++; }
''')
for name in ['is_scripts', 'wc_find_app', 'wc_focus', 'wc_focus_next', 'wc_focus_previous', 'wc_adjust_window', 'wc_focus_nth', 'wc_focus_available', 'forget_toplevel', 'wc_workspace']:
    print(function(name))
print(r'''
static void add(struct wc_server *s,struct wc_toplevel *t,struct fake_xdg *x,unsigned workspace) {
    *t=(struct wc_toplevel){.server=s,.xdg_toplevel=x,.workspace=workspace,.mapped=true}; wl_list_insert(&s->toplevels,&t->link);
}
int main(void) {
    struct wc_server s={0}; wl_list_init(&s.toplevels);
    struct wc_toplevel coding,panel,play,hidden;
    struct fake_xdg xc={.app_id="editor"},xs={.app_id="cube-scripts"},xp={.app_id="game"},xh={.app_id="cube-scripts"};
    add(&s,&coding,&xc,0); add(&s,&panel,&xs,0); add(&s,&play,&xp,1); add(&s,&hidden,&xh,1); hidden.hidden=true;
    wc_focus(&coding); wc_focus(&panel); panel.fullscreen=true;
    assert(s.normal==&coding && s.focused==&panel);
    wc_focus_previous(&s); assert(s.focused==&coding);
    wc_focus_next(&s); assert(s.focused==&panel);
    wc_workspace(&s,1,false);
    assert(s.focused==&play && s.normal==&play && !xs.activated && xp.activated);
    assert(wc_find_app(&s,"cube-scripts")==&hidden);
    wc_focus_next(&s); assert(s.focused==&play); // hidden panel and other-workspace apps excluded
    wc_workspace(&s,0,false);
    assert(s.focused==&panel && s.normal==&coding && panel.fullscreen);
    wc_workspace(&s,2,true); // Move fullscreen panel, retain the coding normal window
    assert(panel.workspace==2 && s.workspace==0 && s.focused==&coding && s.normal==&coding);
    wc_workspace(&s,2,false); assert(s.focused==&panel && panel.fullscreen && s.normal==NULL);
    panel.hidden=true; wc_focus_available(&s); assert(!s.focused && keyboard_clears);
    wc_workspace(&s,3,false); assert(!s.focused && !s.normal);
    wc_focus_nth(&s,0); assert(!s.focused);
    wc_workspace(&s,1,false); wc_focus_nth(&s,0); assert(s.focused==&hidden && !hidden.hidden);
    hidden.mapped=false; forget_toplevel(&hidden); wc_focus_available(&s); assert(s.focused==&play);
    wc_adjust_window(&s,XKB_KEY_Left,true); assert(play.layout_w==975);
    wc_adjust_window(&s,XKB_KEY_Right,false); assert(play.layout_x==25 && play.layout_w==975);
    wc_adjust_window(&s,XKB_KEY_Right,false); assert(play.layout_x==25); // bounded by available width
    play.fullscreen=true; wc_adjust_window(&s,XKB_KEY_Left,true); assert(play.layout_w==975); play.fullscreen=false;
    for(int j=0;j<100;j++) wc_adjust_window(&s,XKB_KEY_Left,true);
    assert(play.layout_w==100 && play.layout_x>=0);
    for(unsigned i=0;i<4;i++) assert(s.saved_focus[i]!=&hidden && s.saved_normal[i]!=&hidden);
    wc_workspace(&s,9,false); assert(s.workspace==1);
    assert(pointer_clears>=6);
    puts("workspaces: focus isolation, reverse keyboard navigation, bounded panel placement/resize, hidden panels, fullscreen moves and lifecycle references passed");
    return 0;
}
''')
