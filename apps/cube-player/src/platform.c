#define _GNU_SOURCE
#include "platform.h"
#include <mpv/render.h>
#include <mpv/render_gl.h>
#include <EGL/egl.h>
#include <GLES2/gl2.h>
#include <wayland-egl.h>
#include <stdatomic.h>
#include "xdg-shell-client.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <wayland-cursor.h>

struct buffer {
    struct wl_buffer *wl;
    void *pixels;
    size_t size;
    int w, h;
    bool busy;
    struct buffer *next;
};

struct data_offer {
    struct wl_data_offer *wl;
    bool utf8, plain, uri;
    struct data_offer *next;
};

struct clipboard_write {
    int fd;
    char *text;
    size_t used, size;
    double deadline;
    struct clipboard_write *next;
};

struct cp_window {
    struct cp_callbacks cb;
    void *data;
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct wl_seat *seat;
    uint32_t seat_name, compositor_name, shm_name, wm_name;
    struct wl_surface *surface, *cursor_surface;
    struct xdg_wm_base *wm;
    struct xdg_surface *xdg;
    struct xdg_toplevel *top;
    struct wl_callback *frame;
    struct wl_keyboard *keyboard;
    struct wl_pointer *pointer;
    struct wl_cursor_theme *cursors;
    struct xkb_context *xkb;
    struct xkb_keymap *keymap;
    struct xkb_state *keys;
    struct wl_data_device_manager *data_manager;
    struct wl_data_device *data_device;
    struct data_offer *offers, *selection, *drag;
    struct wl_data_source *source;
    char *clipboard;
    struct clipboard_write *writes;
    int paste_fd;
    char paste[65536];
    bool paste_is_drop;
    size_t paste_size;
    double paste_deadline;
    struct buffer *buffers;
    int w, h, pending_w, pending_h;
    bool configured, dirty, closed, maximized, repeating, fullscreen;
    mpv_render_context *render;
    atomic_bool render_pending;
    EGLDisplay egl;
    EGLContext context;
    EGLSurface egl_surface;
    struct wl_egl_window *egl_window;
    GLuint program, ui_texture, video_texture, video_fbo;
    int video_w, video_h, vx, vy, vw, vh;
    cairo_surface_t *ui;
    char *snapshot;
    bool gpu;
    double x, y;
    uint32_t serial, button_serial, repeat_key, pointer_serial;
    bool cursor_hidden;
    double frame_requested;
    int repeat_rate, repeat_delay;
    double repeat_at;
};

static int cp_memfd(const char *name, size_t size) {
    int fd = memfd_create(name, MFD_CLOEXEC);
    if (fd >= 0 && ftruncate(fd, (off_t)size)) { close(fd); return -1; }
    return fd;
}

static double now(void)
{
    struct timespec time;
    clock_gettime(CLOCK_MONOTONIC, &time);
    return time.tv_sec + time.tv_nsec / 1e9;
}

void cp_window_dirty(struct cp_window *w)
{
    if (w) w->dirty = true;
}

void cp_window_title(struct cp_window *w, const char *title)
{
    if (w && w->top && title) xdg_toplevel_set_title(w->top, title);
}

void cp_window_close(struct cp_window *w)
{
    if (w) w->closed = true;
}

void cp_window_move(struct cp_window *w)
{
    if (w && w->seat && w->button_serial)
        xdg_toplevel_move(w->top, w->seat, w->button_serial);
}

void cp_window_resize(struct cp_window *w, uint32_t edges)
{
    if (w && w->seat && w->button_serial)
        xdg_toplevel_resize(w->top, w->seat, w->button_serial, edges);
}

void cp_window_maximize(struct cp_window *w)
{
    if (!w) return;
    if (w->maximized) xdg_toplevel_unset_maximized(w->top);
    else xdg_toplevel_set_maximized(w->top);
}

void cp_window_minimize(struct cp_window *w)
{
    if (w) xdg_toplevel_set_minimized(w->top);
}

static void buffer_release(void *data, struct wl_buffer *buffer)
{
    (void)buffer;
    ((struct buffer *)data)->busy = false;
}

static const struct wl_buffer_listener buffer_listener = {
    .release = buffer_release,
};

static void buffer_free(struct buffer *buffer)
{
    wl_buffer_destroy(buffer->wl);
    munmap(buffer->pixels, buffer->size);
    free(buffer);
}

static struct buffer *get_buffer(struct cp_window *w)
{
    struct buffer **entry = &w->buffers;
    unsigned count = 0;
    while (*entry) {
        struct buffer *buffer = *entry;
        if (!buffer->busy && (buffer->w != w->w || buffer->h != w->h)) {
            *entry = buffer->next;
            buffer_free(buffer);
            continue;
        }
        if (!buffer->busy) return buffer;
        count++;
        entry = &buffer->next;
    }
    if (count >= 4) return NULL;
    size_t size = (size_t)w->w * w->h * 4;
    if (size > INT32_MAX) return NULL;
    int fd = cp_memfd("cube-window", size);
    if (fd < 0) return NULL;
    struct buffer *buffer = calloc(1, sizeof(*buffer));
    if (!buffer) {
        close(fd);
        return NULL;
    }
    buffer->pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (buffer->pixels == MAP_FAILED) {
        close(fd);
        free(buffer);
        return NULL;
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(w->shm, fd, (int32_t)size);
    close(fd);
    if (pool) {
        buffer->wl = wl_shm_pool_create_buffer(pool, 0, w->w, w->h,
                                               w->w * 4, WL_SHM_FORMAT_ARGB8888);
        wl_shm_pool_destroy(pool);
    }
    if (!buffer->wl) {
        munmap(buffer->pixels, size);
        free(buffer);
        return NULL;
    }
    buffer->size = size;
    buffer->w = w->w;
    buffer->h = w->h;
    wl_buffer_add_listener(buffer->wl, &buffer_listener, buffer);
    buffer->next = w->buffers;
    w->buffers = buffer;
    return buffer;
}

static void frame_done(void *data, struct wl_callback *callback, uint32_t time)
{
    (void)time;
    struct cp_window *w = data;
    wl_callback_destroy(callback);
    w->frame = NULL;
}

static const struct wl_callback_listener frame_listener = {
    .done = frame_done,
};

static void damage(struct wl_surface *surface, int width, int height)
{
    if (wl_surface_get_version(surface) >= WL_SURFACE_DAMAGE_BUFFER_SINCE_VERSION)
        wl_surface_damage_buffer(surface, 0, 0, width, height);
    else
        wl_surface_damage(surface, 0, 0, width, height);
}

void cp_window_hide_cursor(struct cp_window *w, bool hidden) {
    if(!w || !w->pointer || !w->pointer_serial || w->cursor_hidden==hidden) return;
    w->cursor_hidden=hidden;
    if(hidden) {wl_pointer_set_cursor(w->pointer,w->pointer_serial,NULL,0,0);return;}
    if(!w->cursors || !w->cursor_surface) return;
    struct wl_cursor *cursor=wl_cursor_theme_get_cursor(w->cursors,"left_ptr");
    if(!cursor || !cursor->image_count) return;
    struct wl_cursor_image *image=cursor->images[0];
    struct wl_buffer *buffer=wl_cursor_image_get_buffer(image);if(!buffer) return;
    wl_surface_attach(w->cursor_surface,buffer,0,0);damage(w->cursor_surface,image->width,image->height);wl_surface_commit(w->cursor_surface);
    wl_pointer_set_cursor(w->pointer,w->pointer_serial,w->cursor_surface,image->hotspot_x,image->hotspot_y);
}

void cp_window_fullscreen(struct cp_window *w, bool value) {
    if (!w) return;
    if (value) xdg_toplevel_set_fullscreen(w->top, NULL);
    else xdg_toplevel_unset_fullscreen(w->top);
}
bool cp_window_is_fullscreen(struct cp_window *w) { return w && w->fullscreen; }
const char *cp_window_renderer(struct cp_window *w) { return w && w->gpu ? "OpenGL / Wayland" : "Software / Wayland"; }
void cp_window_snapshot(struct cp_window *w, const char *path) {
    if (!w) return;
    free(w->snapshot); w->snapshot = strdup(path); w->dirty = true;
}
static void render_update(void *data) {
    struct cp_window *w = data;
    atomic_store(&w->render_pending, true);
}
static void *get_proc(void *ctx, const char *name) { (void)ctx; return (void *)eglGetProcAddress(name); }
static GLuint shader(GLenum type, const char *source) {
    GLuint id = glCreateShader(type); glShaderSource(id, 1, &source, NULL); glCompileShader(id);
    GLint ok; glGetShaderiv(id, GL_COMPILE_STATUS, &ok);
    if (!ok) { glDeleteShader(id); return 0; } return id;
}
static bool gpu_init(struct cp_window *w) {
    w->egl = eglGetDisplay((EGLNativeDisplayType)w->display);
    if (!w->egl || !eglInitialize(w->egl, NULL, NULL) || !eglBindAPI(EGL_OPENGL_ES_API)) return false;
    EGLint attrs[] = {EGL_SURFACE_TYPE,EGL_WINDOW_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE,8,EGL_GREEN_SIZE,8,EGL_BLUE_SIZE,8,EGL_ALPHA_SIZE,8,EGL_NONE};
    EGLConfig cfg; EGLint count;
    if (!eglChooseConfig(w->egl, attrs, &cfg, 1, &count) || !count) return false;
    EGLint context[] = {EGL_CONTEXT_CLIENT_VERSION,2,EGL_NONE};
    w->context = eglCreateContext(w->egl, cfg, EGL_NO_CONTEXT, context);
    w->egl_window = wl_egl_window_create(w->surface, w->w, w->h);
    if (!w->context || !w->egl_window) return false;
    w->egl_surface = eglCreateWindowSurface(w->egl, cfg, (EGLNativeWindowType)w->egl_window, NULL);
    if (!w->egl_surface || !eglMakeCurrent(w->egl, w->egl_surface, w->egl_surface, w->context)) return false;
    eglSwapInterval(w->egl, 0);
    const char *vert = "attribute vec2 position; attribute vec2 texcoord; varying vec2 uv; void main(){uv=texcoord;gl_Position=vec4(position,0.,1.);}";
    const char *frag = "precision mediump float; varying vec2 uv; uniform sampler2D tex; uniform float cairo; void main(){vec4 c=texture2D(tex,uv);gl_FragColor=mix(c,c.bgra,cairo);}";
    GLuint vs = shader(GL_VERTEX_SHADER, vert), fs = shader(GL_FRAGMENT_SHADER, frag);
    if (!vs || !fs) { if (vs) glDeleteShader(vs); if (fs) glDeleteShader(fs); return false; }
    w->program = glCreateProgram(); glAttachShader(w->program,vs); glAttachShader(w->program,fs);
    glBindAttribLocation(w->program,0,"position"); glBindAttribLocation(w->program,1,"texcoord");
    glLinkProgram(w->program); glDeleteShader(vs); glDeleteShader(fs);
    GLint ok; glGetProgramiv(w->program,GL_LINK_STATUS,&ok); if (!ok) return false;
    glGenTextures(1,&w->ui_texture); glGenTextures(1,&w->video_texture); glGenFramebuffers(1,&w->video_fbo);
    return true;
}
void cp_window_detach_player(struct cp_window *w) {
    if (!w) return;
    if (w->render) { mpv_render_context_free(w->render); w->render = NULL; }
    if (w->context) {
        if (w->ui_texture) glDeleteTextures(1,&w->ui_texture);
        if (w->video_texture) glDeleteTextures(1,&w->video_texture);
        if (w->video_fbo) glDeleteFramebuffers(1,&w->video_fbo);
        if (w->program) glDeleteProgram(w->program);
        eglMakeCurrent(w->egl,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);
        eglDestroyContext(w->egl,w->context);
    }
    if (w->egl_surface) eglDestroySurface(w->egl,w->egl_surface);
    if (w->egl_window) wl_egl_window_destroy(w->egl_window);
    if (w->egl) eglTerminate(w->egl);
    if (w->ui) cairo_surface_destroy(w->ui);
    w->ui = NULL; w->egl = NULL; w->context = NULL; w->egl_surface = NULL;
    w->egl_window = NULL; w->ui_texture = w->video_texture = w->video_fbo = w->program = 0; w->gpu = false;
}
bool cp_window_attach_player(struct cp_window *w, mpv_handle *mpv, bool software) {
    if (!w) return false;
    if (!software && gpu_init(w)) {
        mpv_opengl_init_params gl = {.get_proc_address=get_proc};
        mpv_render_param params[] = {{MPV_RENDER_PARAM_API_TYPE,MPV_RENDER_API_TYPE_OPENGL},
            {MPV_RENDER_PARAM_OPENGL_INIT_PARAMS,&gl},{MPV_RENDER_PARAM_WL_DISPLAY,w->display},{0}};
        if (mpv_render_context_create(&w->render,mpv,params) >= 0) w->gpu = true;
    }
    if (!w->gpu) {
        cp_window_detach_player(w);
        mpv_render_param params[] = {{MPV_RENDER_PARAM_API_TYPE,MPV_RENDER_API_TYPE_SW},{0}};
        if (mpv_render_context_create(&w->render,mpv,params) < 0) return false;
    }
    mpv_render_context_set_update_callback(w->render,render_update,w);
    return true;
}
void cp_window_video(struct cp_window *w, cairo_t *cr, int x, int y, int width, int height) {
    if (!w || !w->render || width < 1 || height < 1) return;
    w->vx=x; w->vy=y; w->vw=width; w->vh=height;
    if (w->gpu) {
        cairo_save(cr); cairo_set_operator(cr,CAIRO_OPERATOR_CLEAR);
        cairo_rectangle(cr,x,y,width,height); cairo_fill(cr); cairo_restore(cr);
    } else {
        cairo_surface_t *s = cairo_image_surface_create(CAIRO_FORMAT_RGB24,width,height);
        if (cairo_surface_status(s) != CAIRO_STATUS_SUCCESS) { cairo_surface_destroy(s); return; }
        int size[] = {width,height}; size_t stride = cairo_image_surface_get_stride(s);
        mpv_render_param params[] = {{MPV_RENDER_PARAM_SW_SIZE,size},{MPV_RENDER_PARAM_SW_FORMAT,"bgr0"},
            {MPV_RENDER_PARAM_SW_STRIDE,&stride},{MPV_RENDER_PARAM_SW_POINTER,cairo_image_surface_get_data(s)},{0}};
        mpv_render_context_render(w->render,params); cairo_surface_mark_dirty(s);
        cairo_set_source_surface(cr,s,x,y); cairo_paint(cr); cairo_surface_destroy(s);
    }
}
static void quad(struct cp_window *w, GLuint texture, int x, int y, int width, int height, bool ui) {
    float left=2.f*x/w->w-1, right=2.f*(x+width)/w->w-1;
    float top=1-2.f*y/w->h, bottom=1-2.f*(y+height)/w->h;
    float a=ui?0:1, b=ui?1:0;
    GLfloat vertices[] = {left,top,0,a,left,bottom,0,b,right,top,1,a,right,bottom,1,b};
    glUseProgram(w->program); glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D,texture);
    glUniform1i(glGetUniformLocation(w->program,"tex"),0);
    glUniform1f(glGetUniformLocation(w->program,"cairo"),ui?1:0);
    glEnableVertexAttribArray(0); glEnableVertexAttribArray(1);
    glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,4*sizeof(float),vertices);
    glVertexAttribPointer(1,2,GL_FLOAT,GL_FALSE,4*sizeof(float),vertices+2);
    glDrawArrays(GL_TRIANGLE_STRIP,0,4);
    glDisableVertexAttribArray(0); glDisableVertexAttribArray(1);
    glUseProgram(0); glBindTexture(GL_TEXTURE_2D,0);
}
static void texture_size(GLuint texture,int width,int height,const void *pixels) {
    glBindTexture(GL_TEXTURE_2D,texture);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA,width,height,0,GL_RGBA,GL_UNSIGNED_BYTE,pixels);
    glBindTexture(GL_TEXTURE_2D,0);
}
static void gpu_draw(struct cp_window *w) {
    wl_egl_window_resize(w->egl_window,w->w,w->h,0,0);
    if (!w->ui || cairo_image_surface_get_width(w->ui)!=w->w || cairo_image_surface_get_height(w->ui)!=w->h) {
        if (w->ui) cairo_surface_destroy(w->ui);
        w->ui=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,w->w,w->h);
    }
    cairo_t *cr=cairo_create(w->ui); w->vw=w->vh=0;
    w->cb.draw(w->data,cr,w->w,w->h); cairo_destroy(cr); cairo_surface_flush(w->ui);
    if (w->vw > 0 && w->vh > 0) {
        if (w->video_w!=w->vw || w->video_h!=w->vh) {
            texture_size(w->video_texture,w->vw,w->vh,NULL); w->video_w=w->vw; w->video_h=w->vh;
            glBindFramebuffer(GL_FRAMEBUFFER,w->video_fbo);
            glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,w->video_texture,0);
            glBindFramebuffer(GL_FRAMEBUFFER,0);
        }
        mpv_opengl_fbo fbo = {.fbo=(int)w->video_fbo,.w=w->vw,.h=w->vh,.internal_format=0};
        int flip=1;
        mpv_render_param params[]={{MPV_RENDER_PARAM_OPENGL_FBO,&fbo},{MPV_RENDER_PARAM_FLIP_Y,&flip},{0}};
        int render_result=mpv_render_context_render(w->render,params);
        if(render_result<0) fprintf(stderr,"Cube Player: video render: %s\n",mpv_error_string(render_result));

    }
    glBindFramebuffer(GL_FRAMEBUFFER,0); glViewport(0,0,w->w,w->h);
    glDisable(GL_SCISSOR_TEST); glDisable(GL_DEPTH_TEST); glDisable(GL_BLEND);
    glClearColor(0,0,0,1); glClear(GL_COLOR_BUFFER_BIT);
    if(w->vw>0 && w->vh>0) quad(w,w->video_texture,w->vx,w->vy,w->vw,w->vh,false);
    texture_size(w->ui_texture,w->w,w->h,cairo_image_surface_get_data(w->ui));
    glEnable(GL_BLEND); glBlendFunc(GL_ONE,GL_ONE_MINUS_SRC_ALPHA);
    quad(w,w->ui_texture,0,0,w->w,w->h,true); glDisable(GL_BLEND);
    if (w->snapshot) {
        size_t len=(size_t)w->w*w->h*4; unsigned char *rgba=malloc(len),*argb=malloc(len);
        if(rgba && argb) {
            glReadPixels(0,0,w->w,w->h,GL_RGBA,GL_UNSIGNED_BYTE,rgba);
            for(int y=0;y<w->h;y++) for(int x=0;x<w->w;x++) {
                size_t a=((size_t)y*w->w+x)*4,b=((size_t)(w->h-1-y)*w->w+x)*4;
                argb[a]=rgba[b+2];argb[a+1]=rgba[b+1];argb[a+2]=rgba[b];argb[a+3]=255;
            }
            cairo_surface_t *s=cairo_image_surface_create_for_data(argb,CAIRO_FORMAT_ARGB32,w->w,w->h,w->w*4);
            if(cairo_surface_write_to_png(s,w->snapshot)) fprintf(stderr,"Cube Player: screenshot could not be saved\n");
            cairo_surface_destroy(s);
        }
        free(rgba);free(argb);free(w->snapshot);w->snapshot=NULL;
    }
    w->frame_requested=now();
    w->frame=wl_surface_frame(w->surface); wl_callback_add_listener(w->frame,&frame_listener,w);
    if (!eglSwapBuffers(w->egl,w->egl_surface)) w->closed=true;
    if(w->render) mpv_render_context_report_swap(w->render);
}

static void draw(struct cp_window *w)
{
    if (!w->configured || !w->dirty || w->frame || w->closed) return;
    if (w->gpu) { w->dirty=false; gpu_draw(w); return; }
    struct buffer *buffer = get_buffer(w);
    if (!buffer) return;
    cairo_surface_t *surface = cairo_image_surface_create_for_data(
        buffer->pixels, CAIRO_FORMAT_ARGB32, w->w, w->h, w->w * 4);
    cairo_t *cr = cairo_create(surface);
    if (cairo_status(cr) != CAIRO_STATUS_SUCCESS) {
        cairo_destroy(cr);
        cairo_surface_destroy(surface);
        w->closed = true;
        return;
    }
    w->dirty = false;
    if (w->cb.draw) w->cb.draw(w->data, cr, w->w, w->h);
    cairo_destroy(cr);
    cairo_surface_flush(surface);
    if (w->snapshot) {
        if(cairo_surface_write_to_png(surface,w->snapshot)) fprintf(stderr,"Cube Player: screenshot could not be saved\n");
        free(w->snapshot); w->snapshot=NULL;
    }
    cairo_surface_destroy(surface);
    w->frame_requested=now();
    w->frame = wl_surface_frame(w->surface);
    if (w->frame) wl_callback_add_listener(w->frame, &frame_listener, w);
    wl_surface_attach(w->surface, buffer->wl, 0, 0);
    damage(w->surface, w->w, w->h);
    wl_surface_commit(w->surface);
    buffer->busy = true;
}

static void keymap(void *data, struct wl_keyboard *keyboard, uint32_t format,
                   int32_t fd, uint32_t size)
{
    (void)keyboard;
    struct cp_window *w = data;
    if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1 || !size || size > 4 * 1024 * 1024) {
        close(fd);
        return;
    }
    char *map = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (map == MAP_FAILED) return;
    if (map[size - 1]) {
        munmap(map, size);
        return;
    }
    struct xkb_keymap *new_map = xkb_keymap_new_from_string(
        w->xkb, map, XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
    munmap(map, size);
    if (!new_map) return;
    struct xkb_state *state = xkb_state_new(new_map);
    if (!state) {
        xkb_keymap_unref(new_map);
        return;
    }
    xkb_state_unref(w->keys);
    xkb_keymap_unref(w->keymap);
    w->keymap = new_map;
    w->keys = state;
    w->repeating = false;
}

static void key_enter(void *data, struct wl_keyboard *keyboard, uint32_t serial,
                      struct wl_surface *surface, struct wl_array *keys)
{
    (void)keyboard;
    (void)surface;
    (void)keys;
    ((struct cp_window *)data)->serial = serial;
}

static void key_leave(void *data, struct wl_keyboard *keyboard, uint32_t serial,
                      struct wl_surface *surface)
{
    (void)keyboard;
    (void)serial;
    (void)surface;
    ((struct cp_window *)data)->repeating = false;
}

static void emit_key(struct cp_window *w, uint32_t keycode)
{
    if (!w->keys || !w->cb.key || keycode > UINT32_MAX - 8) return;
    char text[64] = {0};
    xkb_keysym_t symbol = xkb_state_key_get_one_sym(w->keys, keycode + 8);
    xkb_state_key_get_utf8(w->keys, keycode + 8, text, sizeof(text));
    bool ctrl = xkb_state_mod_name_is_active(w->keys, XKB_MOD_NAME_CTRL,
                                            XKB_STATE_MODS_EFFECTIVE) > 0;
    bool shift = xkb_state_mod_name_is_active(w->keys, XKB_MOD_NAME_SHIFT,
                                             XKB_STATE_MODS_EFFECTIVE) > 0;
    bool alt = xkb_state_mod_name_is_active(w->keys, XKB_MOD_NAME_ALT,
                                           XKB_STATE_MODS_EFFECTIVE) > 0;
    w->cb.key(w->data, symbol, text, ctrl, shift, alt);
}

static void key(void *data, struct wl_keyboard *keyboard, uint32_t serial,
                uint32_t time, uint32_t keycode, uint32_t state)
{
    (void)keyboard;
    (void)time;
    struct cp_window *w = data;
    w->serial = serial;
    if (state == WL_KEYBOARD_KEY_STATE_PRESSED) {
        emit_key(w, keycode);
        if (w->keymap && keycode <= UINT32_MAX - 8 &&
            xkb_keymap_key_repeats(w->keymap, keycode + 8) && w->repeat_rate > 0) {
            w->repeat_key = keycode;
            w->repeating = true;
            w->repeat_at = now() + w->repeat_delay / 1000.;
        }
    } else if (w->repeat_key == keycode) {
        w->repeating = false;
    }
}

static void modifiers(void *data, struct wl_keyboard *keyboard, uint32_t serial,
                       uint32_t depressed, uint32_t latched, uint32_t locked, uint32_t group)
{
    (void)keyboard;
    (void)serial;
    struct cp_window *w = data;
    if (w->keys) xkb_state_update_mask(w->keys, depressed, latched, locked, 0, 0, group);
}

static void repeat_info(void *data, struct wl_keyboard *keyboard, int32_t rate, int32_t delay)
{
    (void)keyboard;
    struct cp_window *w = data;
    w->repeat_rate = rate < 0 ? 0 : rate > 60 ? 60 : rate;
    w->repeat_delay = delay < 50 ? 50 : delay;
    if (!w->repeat_rate) w->repeating = false;
}

static const struct wl_keyboard_listener keyboard_listener = {
    .keymap = keymap, .enter = key_enter, .leave = key_leave,
    .key = key, .modifiers = modifiers, .repeat_info = repeat_info,
};

static void pointer_enter(void *data, struct wl_pointer *pointer, uint32_t serial,
                          struct wl_surface *surface, wl_fixed_t x, wl_fixed_t y)
{
    (void)surface;
    struct cp_window *w = data;
    w->x = wl_fixed_to_double(x);
    w->y = wl_fixed_to_double(y);
    w->pointer_serial=serial;
    if(w->cursor_hidden) {wl_pointer_set_cursor(pointer,serial,NULL,0,0);return;}
    if (!w->cursors || !w->cursor_surface) return;
    struct wl_cursor *cursor = wl_cursor_theme_get_cursor(w->cursors, "left_ptr");
    if (!cursor || !cursor->image_count) return;
    struct wl_cursor_image *image = cursor->images[0];
    struct wl_buffer *buffer = wl_cursor_image_get_buffer(image);
    if (!buffer) return;
    wl_surface_attach(w->cursor_surface, buffer, 0, 0);
    damage(w->cursor_surface, image->width, image->height);
    wl_surface_commit(w->cursor_surface);
    wl_pointer_set_cursor(pointer, serial, w->cursor_surface, image->hotspot_x, image->hotspot_y);
}

static void pointer_leave(void *data, struct wl_pointer *pointer, uint32_t serial,
                          struct wl_surface *surface)
{
    (void)pointer;
    (void)serial;
    (void)surface;
    ((struct cp_window *)data)->button_serial = 0;
    ((struct cp_window *)data)->pointer_serial = 0;
}

static void pointer_motion(void *data, struct wl_pointer *pointer, uint32_t time,
                           wl_fixed_t x, wl_fixed_t y)
{
    (void)pointer;
    (void)time;
    struct cp_window *w = data;
    w->x = wl_fixed_to_double(x);
    w->y = wl_fixed_to_double(y);
    if (w->cb.motion) w->cb.motion(w->data, w->x, w->y);
}

static void pointer_button(void *data, struct wl_pointer *pointer, uint32_t serial,
                           uint32_t time, uint32_t button, uint32_t state)
{
    (void)pointer;
    (void)time;
    struct cp_window *w = data;
    w->serial = serial;
    if (state == WL_POINTER_BUTTON_STATE_PRESSED) {
        w->button_serial = serial;
        if (w->cb.click) w->cb.click(w->data, w->x, w->y, button);
    } else {
        if (w->cb.release) w->cb.release(w->data, w->x, w->y, button);
        w->button_serial = 0;
    }
}

static void pointer_axis(void *data, struct wl_pointer *pointer, uint32_t time,
                         uint32_t axis, wl_fixed_t value)
{
    (void)pointer;
    (void)time;
    struct cp_window *w = data;
    if (axis == WL_POINTER_AXIS_VERTICAL_SCROLL && w->cb.scroll)
        w->cb.scroll(w->data, wl_fixed_to_double(value) * 3);
}

/* Seat v5 can deliver every one of these events even when we only use axis. */
static void pointer_frame(void *data, struct wl_pointer *pointer)
{
    (void)data;
    (void)pointer;
}

static void pointer_axis_source(void *data, struct wl_pointer *pointer, uint32_t source)
{
    (void)data;
    (void)pointer;
    (void)source;
}

static void pointer_axis_stop(void *data, struct wl_pointer *pointer,
                              uint32_t time, uint32_t axis)
{
    (void)data;
    (void)pointer;
    (void)time;
    (void)axis;
}

static void pointer_axis_discrete(void *data, struct wl_pointer *pointer,
                                  uint32_t axis, int32_t discrete)
{
    (void)data;
    (void)pointer;
    (void)axis;
    (void)discrete;
}

static const struct wl_pointer_listener pointer_listener = {
    .enter = pointer_enter, .leave = pointer_leave, .motion = pointer_motion,
    .button = pointer_button, .axis = pointer_axis, .frame = pointer_frame,
    .axis_source = pointer_axis_source, .axis_stop = pointer_axis_stop,
    .axis_discrete = pointer_axis_discrete,
};

static void keyboard_destroy(struct cp_window *w)
{
    if (!w->keyboard) return;
    if (wl_keyboard_get_version(w->keyboard) >= WL_KEYBOARD_RELEASE_SINCE_VERSION)
        wl_keyboard_release(w->keyboard);
    else wl_keyboard_destroy(w->keyboard);
    w->keyboard = NULL;
    w->repeating = false;
}

static void pointer_destroy(struct cp_window *w)
{
    if (!w->pointer) return;
    if (wl_pointer_get_version(w->pointer) >= WL_POINTER_RELEASE_SINCE_VERSION)
        wl_pointer_release(w->pointer);
    else wl_pointer_destroy(w->pointer);
    w->pointer = NULL;
    w->button_serial = 0;
}

static void seat_caps(void *data, struct wl_seat *seat, uint32_t caps)
{
    struct cp_window *w = data;
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !w->keyboard) {
        w->keyboard = wl_seat_get_keyboard(seat);
        if (w->keyboard) wl_keyboard_add_listener(w->keyboard, &keyboard_listener, w);
    }
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !w->pointer) {
        w->pointer = wl_seat_get_pointer(seat);
        if (w->pointer) wl_pointer_add_listener(w->pointer, &pointer_listener, w);
    }
    if (!(caps & WL_SEAT_CAPABILITY_KEYBOARD)) keyboard_destroy(w);
    if (!(caps & WL_SEAT_CAPABILITY_POINTER)) pointer_destroy(w);
}

static void seat_name(void *data, struct wl_seat *seat, const char *name)
{
    (void)data;
    (void)seat;
    (void)name;
}

static const struct wl_seat_listener seat_listener = {
    .capabilities = seat_caps, .name = seat_name,
};

static void ping(void *data, struct xdg_wm_base *base, uint32_t serial)
{
    (void)data;
    xdg_wm_base_pong(base, serial);
}

static const struct xdg_wm_base_listener wm_listener = {.ping = ping};

static void configure(void *data, struct xdg_surface *surface, uint32_t serial)
{
    struct cp_window *w = data;
    xdg_surface_ack_configure(surface, serial);
    if (w->pending_w > 0) w->w = w->pending_w;
    if (w->pending_h > 0) w->h = w->pending_h;
    if (w->w > 4096 || w->h > 4096 || w->w <= 0 || w->h <= 0) {
        fprintf(stderr, "cube-player: output exceeds 4096px software surface limit\n");
        w->closed = true;
        return;
    }
    w->configured = true;
    w->dirty = true;
}

static const struct xdg_surface_listener surface_listener = {.configure = configure};

static void top_configure(void *data, struct xdg_toplevel *top, int32_t width,
                          int32_t height, struct wl_array *states)
{
    (void)top;
    struct cp_window *w = data;
    w->pending_w = width;
    w->pending_h = height;
    w->maximized = false;
    w->fullscreen = false;
    uint32_t *state;
    wl_array_for_each(state, states) {
        if (*state == XDG_TOPLEVEL_STATE_MAXIMIZED) w->maximized = true;
        if (*state == XDG_TOPLEVEL_STATE_FULLSCREEN) w->fullscreen = true;
    }
}

static void top_close(void *data, struct xdg_toplevel *top)
{
    (void)top;
    ((struct cp_window *)data)->closed = true;
}

static void top_bounds(void *data, struct xdg_toplevel *top, int32_t width, int32_t height)
{
    (void)data;
    (void)top;
    (void)width;
    (void)height;
}

static void top_capabilities(void *data, struct xdg_toplevel *top, struct wl_array *capabilities)
{
    (void)data;
    (void)top;
    (void)capabilities;
}

static const struct xdg_toplevel_listener top_listener = {
    .configure = top_configure, .close = top_close,
    .configure_bounds = top_bounds, .wm_capabilities = top_capabilities,
};

static void offer_mime(void *data, struct wl_data_offer *offer, const char *mime)
{
    (void)offer;
    struct data_offer *entry = data;
    if (!strcmp(mime, "text/plain;charset=utf-8")) entry->utf8 = true;
    else if (!strcmp(mime, "text/plain")) entry->plain = true;
    else if (!strcmp(mime, "text/uri-list")) entry->uri = true;
}

/* Data-device version 1 has no action events. */
static const struct wl_data_offer_listener offer_listener = {.offer = offer_mime};

static void offer_free(struct cp_window *w, struct data_offer *entry)
{
    if (!entry) return;
    struct data_offer **link = &w->offers;
    while (*link && *link != entry) link = &(*link)->next;
    if (*link) *link = entry->next;
    if (w->selection == entry) w->selection = NULL;
    if (w->drag == entry) w->drag = NULL;
    wl_data_offer_destroy(entry->wl);
    free(entry);
}

static struct data_offer *offer_find(struct cp_window *w, struct wl_data_offer *offer)
{
    for (struct data_offer *entry = w->offers; entry; entry = entry->next)
        if (entry->wl == offer) return entry;
    return NULL;
}

static void offer(void *data, struct wl_data_device *device, struct wl_data_offer *offer)
{
    (void)device;
    struct cp_window *w = data;
    struct data_offer *entry = w->offers;
    while (entry) {
        struct data_offer *next = entry->next;
        if (entry != w->selection && entry != w->drag) offer_free(w, entry);
        entry = next;
    }
    entry = calloc(1, sizeof(*entry));
    if (!entry) {
        wl_data_offer_destroy(offer);
        return;
    }
    entry->wl = offer;
    entry->next = w->offers;
    w->offers = entry;
    wl_data_offer_add_listener(offer, &offer_listener, entry);
}

static void selection(void *data, struct wl_data_device *device, struct wl_data_offer *offer)
{
    (void)device;
    struct cp_window *w = data;
    struct data_offer *entry = offer_find(w, offer);
    if (w->selection && w->selection != entry && w->selection != w->drag)
        offer_free(w, w->selection);
    w->selection = entry;
}

static void drag_enter(void *data, struct wl_data_device *device, uint32_t serial,
                       struct wl_surface *surface, wl_fixed_t x, wl_fixed_t y,
                       struct wl_data_offer *offer)
{
    (void)device;
    (void)surface;
    (void)x;
    (void)y;
    struct cp_window *w = data;
    if (w->drag && w->drag != w->selection) offer_free(w, w->drag);
    w->drag = offer_find(w, offer);
    if (offer) wl_data_offer_accept(offer, serial, w->drag && w->drag->uri ? "text/uri-list" : NULL);
}

static void drag_leave(void *data, struct wl_data_device *device)
{
    (void)device;
    struct cp_window *w = data;
    if (w->drag != w->selection) offer_free(w, w->drag);
    w->drag = NULL;
}

static void drag_motion(void *data, struct wl_data_device *device,
                        uint32_t time, wl_fixed_t x, wl_fixed_t y)
{
    (void)data;
    (void)device;
    (void)time;
    (void)x;
    (void)y;
}

static void drag_drop(void *data, struct wl_data_device *device)
{
    struct cp_window *w = data;
    int fds[2];
    if (w->drag && w->drag->uri && pipe2(fds, O_CLOEXEC) == 0) {
        if (fcntl(fds[0], F_SETFL, O_NONBLOCK) == 0) {
            if (w->paste_fd >= 0) close(w->paste_fd);
            w->paste_fd = fds[0]; w->paste_size = 0;
            w->paste_deadline = now() + 5; w->paste_is_drop = true;
            wl_data_offer_receive(w->drag->wl, "text/uri-list", fds[1]);
        } else close(fds[0]);
        close(fds[1]);
    }
    drag_leave(data, device);
}

static const struct wl_data_device_listener device_listener = {
    .data_offer = offer, .enter = drag_enter, .leave = drag_leave,
    .motion = drag_motion, .drop = drag_drop, .selection = selection,
};

static void source_target(void *data, struct wl_data_source *source, const char *mime)
{
    (void)data;
    (void)source;
    (void)mime;
}

static void source_send(void *data, struct wl_data_source *source, const char *mime, int32_t fd)
{
    (void)source;
    struct cp_window *w = data;
    unsigned count = 0;
    for (struct clipboard_write *write = w->writes; write; write = write->next) count++;
    int flags = fcntl(fd, F_GETFL);
    if (!w->clipboard || count >= 8 || flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0 ||
        (strcmp(mime, "text/plain;charset=utf-8") && strcmp(mime, "text/plain"))) {
        close(fd);
        return;
    }
    struct clipboard_write *write = calloc(1, sizeof(*write));
    if (!write) {
        close(fd);
        return;
    }
    write->text = strdup(w->clipboard);
    if (!write->text) {
        close(fd);
        free(write);
        return;
    }
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    write->fd = fd;
    write->size = strlen(write->text);
    write->deadline = now() + 5;
    write->next = w->writes;
    w->writes = write;
}

static void source_cancel(void *data, struct wl_data_source *source)
{
    struct cp_window *w = data;
    if (w->source == source) w->source = NULL;
    wl_data_source_destroy(source);
}

static const struct wl_data_source_listener source_listener = {
    .target = source_target, .send = source_send, .cancelled = source_cancel,
};

void cp_window_copy(struct cp_window *w, const char *text)
{
    if (!w || !w->data_device || !w->serial || !text) return;
    char *copy = strndup(text, 4095);
    if (!copy) return;
    struct wl_data_source *source = wl_data_device_manager_create_data_source(w->data_manager);
    if (!source) {
        free(copy);
        return;
    }
    if (w->source) wl_data_source_destroy(w->source);
    free(w->clipboard);
    w->clipboard = copy;
    w->source = source;
    wl_data_source_add_listener(source, &source_listener, w);
    wl_data_source_offer(source, "text/plain;charset=utf-8");
    wl_data_source_offer(source, "text/plain");
    wl_data_device_set_selection(w->data_device, source, w->serial);
}

void cp_window_paste(struct cp_window *w)
{
    if (!w || !w->selection || (!w->selection->utf8 && !w->selection->plain)) return;
    int fds[2];
    /* Only the reader is nonblocking: foreign clients may use blocking writes. */
    if (pipe2(fds, O_CLOEXEC)) return;
    if (fcntl(fds[0], F_SETFL, O_NONBLOCK) < 0) {
        close(fds[0]);
        close(fds[1]);
        return;
    }
    if (w->paste_fd >= 0) close(w->paste_fd);
    w->paste_fd = fds[0];
    w->paste_size = 0;
    w->paste_deadline = now() + 5;
    w->paste_is_drop = false;
    const char *mime = w->selection->utf8 ? "text/plain;charset=utf-8" : "text/plain";
    wl_data_offer_receive(w->selection->wl, mime, fds[1]);
    close(fds[1]);
}

static void clipboard_progress(struct cp_window *w)
{
    struct clipboard_write **link = &w->writes;
    while (*link) {
        struct clipboard_write *transfer = *link;
        ssize_t count = write(transfer->fd, transfer->text + transfer->used,
                              transfer->size - transfer->used);
        if (count > 0) transfer->used += (size_t)count;
        bool failed = count < 0 && errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK;
        if (transfer->used == transfer->size || failed || now() >= transfer->deadline) {
            *link = transfer->next;
            close(transfer->fd);
            free(transfer->text);
            free(transfer);
        } else link = &transfer->next;
    }
    if (w->paste_fd < 0) return;
    ssize_t count = read(w->paste_fd, w->paste + w->paste_size,
                         sizeof(w->paste) - 1 - w->paste_size);
    if (count > 0) w->paste_size += (size_t)count;
    bool failed = count < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR;
    bool timeout = now() >= w->paste_deadline;
    if (!count || w->paste_size == sizeof(w->paste) - 1 || failed || timeout) {
        close(w->paste_fd);
        w->paste_fd = -1;
        w->paste[w->paste_size] = '\0';
        if (!failed && !timeout && w->paste_size < sizeof(w->paste) - 1) {
            if (w->paste_is_drop) { if (w->cb.drop) w->cb.drop(w->data, w->paste); }
            else if (w->cb.paste) w->cb.paste(w->data, w->paste);
        }
    }
}

static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version)
{
    struct cp_window *w = data;
    if (!strcmp(interface, "wl_compositor") && !w->compositor) {
        w->compositor = wl_registry_bind(registry, name, &wl_compositor_interface,
                                         version < 4 ? version : 4);
        w->compositor_name = name;
    } else if (!strcmp(interface, "wl_shm") && !w->shm) {
        w->shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
        w->shm_name = name;
    } else if (!strcmp(interface, "xdg_wm_base") && !w->wm) {
        w->wm = wl_registry_bind(registry, name, &xdg_wm_base_interface, version < 6 ? version : 6);
        w->wm_name = name;
        if (w->wm) xdg_wm_base_add_listener(w->wm, &wm_listener, w);
    } else if (!strcmp(interface, "wl_seat") && !w->seat) {
        w->seat = wl_registry_bind(registry, name, &wl_seat_interface, version < 5 ? version : 5);
        w->seat_name = name;
        if (w->seat) wl_seat_add_listener(w->seat, &seat_listener, w);
    } else if (!strcmp(interface, "wl_data_device_manager") && !w->data_manager) {
        w->data_manager = wl_registry_bind(registry, name, &wl_data_device_manager_interface, 1);
    }
}

static void global_remove(void *data, struct wl_registry *registry, uint32_t name)
{
    (void)registry;
    struct cp_window *w = data;
    if (name == w->seat_name) {
        keyboard_destroy(w);
        pointer_destroy(w);
        w->serial = 0;
    }
    if (name == w->compositor_name || name == w->shm_name || name == w->wm_name)
        w->closed = true;
}

static const struct wl_registry_listener registry_listener = {
    .global = global, .global_remove = global_remove,
};

struct cp_window *cp_window_new(struct cp_callbacks callbacks, void *data)
{
    unsetenv("DISPLAY");
    struct cp_window *w = calloc(1, sizeof(*w));
    if (!w) return NULL;
    w->cb = callbacks;
    w->data = data;
    w->w = 1120;
    w->h = 740;
    w->dirty = true;
    w->paste_fd = -1;
    w->display = wl_display_connect(NULL);
    if (!w->display) {
        free(w);
        return NULL;
    }
    w->xkb = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    w->registry = wl_display_get_registry(w->display);
    if (!w->xkb || !w->registry) goto failure;
    wl_registry_add_listener(w->registry, &registry_listener, w);
    if (wl_display_roundtrip(w->display) < 0 || !w->compositor || !w->shm || !w->wm) goto failure;
    if (w->data_manager && w->seat) {
        w->data_device = wl_data_device_manager_get_data_device(w->data_manager, w->seat);
        if (w->data_device) wl_data_device_add_listener(w->data_device, &device_listener, w);
    }
    w->cursors = wl_cursor_theme_load(NULL, 24, w->shm);
    w->cursor_surface = wl_compositor_create_surface(w->compositor);
    w->surface = wl_compositor_create_surface(w->compositor);
    if (!w->surface) goto failure;
    w->xdg = xdg_wm_base_get_xdg_surface(w->wm, w->surface);
    if (!w->xdg) goto failure;
    xdg_surface_add_listener(w->xdg, &surface_listener, w);
    w->top = xdg_surface_get_toplevel(w->xdg);
    if (!w->top) goto failure;
    xdg_toplevel_add_listener(w->top, &top_listener, w);
    xdg_toplevel_set_app_id(w->top, "cube-player");
    xdg_toplevel_set_title(w->top, "Cube Player");
    xdg_toplevel_set_min_size(w->top, 640, 400);
    wl_surface_commit(w->surface);
    return w;
failure:
    cp_window_free(w);
    return NULL;
}

int cp_window_run(struct cp_window *w, double seconds)
{
    if (!w) return 1;
    double end = seconds > 0 ? now() + seconds : 0;
    while (!w->closed) {
        if (end && now() >= end) break;
        if (w->render && atomic_exchange(&w->render_pending, false)) {
            mpv_render_context_update(w->render); w->dirty = true;
            /* A hidden/minimized window may not get compositor frame callbacks.
             * Acknowledge those video frames so playback can keep advancing. */
            if(w->frame && now()-w->frame_requested>.25) {
                int skip=1;mpv_render_param params[]={{MPV_RENDER_PARAM_SKIP_RENDERING,&skip},{0}};
                mpv_render_context_render(w->render,params);
            }
        }
        if (w->cb.tick) w->cb.tick(w->data);
        if (w->repeating && w->repeat_rate > 0 && now() >= w->repeat_at) {
            emit_key(w, w->repeat_key);
            w->repeat_at = now() + 1. / w->repeat_rate;
        }
        clipboard_progress(w);
        draw(w);
        while (wl_display_prepare_read(w->display) != 0) {
            if (wl_display_dispatch_pending(w->display) < 0) return 1;
        }
        short events = POLLIN;
        if (wl_display_flush(w->display) < 0) {
            if (errno == EAGAIN) events |= POLLOUT;
            else {
                wl_display_cancel_read(w->display);
                return 1;
            }
        }
        struct pollfd fd = {.fd = wl_display_get_fd(w->display), .events = events};
        int count = poll(&fd, 1, 16);
        if (count > 0 && (fd.revents & POLLIN)) {
            if (wl_display_read_events(w->display) < 0) return 1;
        } else wl_display_cancel_read(w->display);
        if ((count < 0 && errno != EINTR) || (fd.revents & (POLLHUP | POLLERR | POLLNVAL))) return 1;
        if (wl_display_dispatch_pending(w->display) < 0) return 1;
    }
    return 0;
}

void cp_window_free(struct cp_window *w)
{
    if (!w) return;
    cp_window_detach_player(w);
    if (w->paste_fd >= 0) close(w->paste_fd);
    while (w->writes) {
        struct clipboard_write *write = w->writes;
        w->writes = write->next;
        close(write->fd);
        free(write->text);
        free(write);
    }
    if (w->source) wl_data_source_destroy(w->source);
    while (w->offers) offer_free(w, w->offers);
    if (w->data_device) wl_data_device_destroy(w->data_device);
    if (w->data_manager) wl_data_device_manager_destroy(w->data_manager);
    if (w->frame) wl_callback_destroy(w->frame);
    while (w->buffers) {
        struct buffer *buffer = w->buffers;
        w->buffers = buffer->next;
        buffer_free(buffer);
    }
    if (w->top) xdg_toplevel_destroy(w->top);
    if (w->xdg) xdg_surface_destroy(w->xdg);
    if (w->surface) wl_surface_destroy(w->surface);
    if (w->cursor_surface) wl_surface_destroy(w->cursor_surface);
    if (w->cursors) wl_cursor_theme_destroy(w->cursors);
    keyboard_destroy(w);
    pointer_destroy(w);
    if (w->seat) {
        if (wl_seat_get_version(w->seat) >= WL_SEAT_RELEASE_SINCE_VERSION) wl_seat_release(w->seat);
        else wl_seat_destroy(w->seat);
    }
    xkb_state_unref(w->keys);
    xkb_keymap_unref(w->keymap);
    xkb_context_unref(w->xkb);
    if (w->wm) xdg_wm_base_destroy(w->wm);
    if (w->shm) wl_shm_destroy(w->shm);
    if (w->compositor) wl_compositor_destroy(w->compositor);
    if (w->registry) wl_registry_destroy(w->registry);
    if (w->display) wl_display_disconnect(w->display);
    free(w->clipboard);
    free(w->snapshot);
    free(w);
}
