#include "player.h"
#include <string.h>
#include <stdio.h>
#include <math.h>
#include <stdlib.h>

static mpv_node *member(mpv_node *n, const char *key) {
    if (n && n->format == MPV_FORMAT_NODE_MAP) {
        for (int i = 0; i < n->u.list->num; i++)
            if (!strcmp(n->u.list->keys[i], key)) return &n->u.list->values[i];
    }
    return NULL;
}
static const char *str(mpv_node *n) { return n && n->format == MPV_FORMAT_STRING ? n->u.string : ""; }
static double num(mpv_node *n) {
    if (!n) return 0;
    if (n->format == MPV_FORMAT_DOUBLE) return n->u.double_;
    if (n->format == MPV_FORMAT_INT64) return n->u.int64;
    if (n->format == MPV_FORMAT_FLAG) return n->u.flag;
    return 0;
}
bool player_init(Player *p, bool silent) {
    memset(p, 0, sizeof(*p)); p->volume = 70; p->speed = 1;
    p->tracks = g_array_new(FALSE, FALSE, sizeof(Track));
    p->chapters = g_array_new(FALSE, FALSE, sizeof(Chapter));
    p->mpv = mpv_create();
    if (!p->mpv) return false;
    const char *opts[][2] = {{"vo","libmpv"},{"config","no"},{"terminal","no"},
        {"input-default-bindings","no"},{"input-vo-keyboard","no"},{"osc","no"},
        {"load-scripts","no"},{"ytdl","no"},{"idle","yes"},{"keep-open","no"},
        {"hwdec","auto-safe"},{"scale","bilinear"},{"cscale","bilinear"},{"dscale","bilinear"},{"osd-level","0"},{"audio-display","no"},
        {"resume-playback","no"},{"sub-auto","fuzzy"},{"volume-max","100"}};
    for (guint i = 0; i < G_N_ELEMENTS(opts); i++) mpv_set_option_string(p->mpv, opts[i][0], opts[i][1]);
    if (silent) {
        mpv_set_option_string(p->mpv, "ao", "null");
        mpv_set_option_string(p->mpv, "hwdec", "no");
        mpv_set_option_string(p->mpv, "gpu-hwdec-interop", "no");
    }
    int rc = mpv_initialize(p->mpv);
    if (rc < 0) { g_snprintf(p->error, sizeof(p->error), "%s", mpv_error_string(rc)); return false; }
    mpv_request_log_messages(p->mpv, getenv("CUBE_PLAYER_DEBUG") ? "debug" : "error");
    const char *doubles[] = {"time-pos","duration","volume","speed"};
    const char *flags[] = {"pause","mute","seekable"};
    const char *strings[] = {"media-title","video-codec"};
    for (guint i = 0; i < G_N_ELEMENTS(doubles); i++) mpv_observe_property(p->mpv, 0, doubles[i], MPV_FORMAT_DOUBLE);
    for (guint i = 0; i < G_N_ELEMENTS(flags); i++) mpv_observe_property(p->mpv, 0, flags[i], MPV_FORMAT_FLAG);
    for (guint i = 0; i < G_N_ELEMENTS(strings); i++) mpv_observe_property(p->mpv, 0, strings[i], MPV_FORMAT_STRING);
    mpv_observe_property(p->mpv, 0, "track-list", MPV_FORMAT_NODE);
    mpv_observe_property(p->mpv, 0, "chapter-list", MPV_FORMAT_NODE);
    mpv_observe_property(p->mpv, 0, "video-params", MPV_FORMAT_NODE);
    return true;
}
void player_free(Player *p) {
    if (p->mpv) mpv_terminate_destroy(p->mpv);
    if (p->tracks) g_array_free(p->tracks, TRUE);
    if (p->chapters) g_array_free(p->chapters, TRUE);
}
void player_command(Player *p, const char **args) {
    if (!p->mpv) return;
    int r = mpv_command_async(p->mpv, 1, args);
    if (r < 0) g_snprintf(p->error, sizeof(p->error), "%s", mpv_error_string(r));
}
void player_number(Player *p, const char *key, double value) {
    if (p->mpv && isfinite(value)) mpv_set_property_async(p->mpv, 2, key, MPV_FORMAT_DOUBLE, &value);
}
void player_flag(Player *p, const char *key, bool value) {
    int flag = value;
    if (p->mpv) mpv_set_property_async(p->mpv, 2, key, MPV_FORMAT_FLAG, &flag);
}
void player_text(Player *p, const char *key, const char *value) {
    if (p->mpv) mpv_set_property_async(p->mpv, 2, key, MPV_FORMAT_STRING, &value);
}
void player_load(Player *p, const char *path, double start) {
    p->loaded = false; p->loading = true; p->video = false; p->ended = false; p->failed = false;
    p->position = p->duration = 0; p->error[0] = 0; p->title[0] = 0;
    p->width = p->height = 0;
    g_array_set_size(p->tracks, 0); g_array_set_size(p->chapters, 0);
    char options[96];
    g_snprintf(options, sizeof(options), "start=%.3f", MAX(0, start));
    const char *args[] = {"loadfile", path, "replace", "-1", options, NULL};
    player_command(p, args); player_flag(p, "pause", false); p->paused = false;
}
void player_seek(Player *p, double seconds, bool absolute) {
    if (!isfinite(seconds) || !p->seekable) return;
    char value[64]; g_snprintf(value, sizeof(value), "%.3f", seconds);
    const char *args[] = {"seek", value, absolute ? "absolute+exact" : "relative+exact", NULL}; player_command(p, args);
}
void player_track(Player *p, const char *type, int id) {
    char value[32]; g_snprintf(value, sizeof(value), "%d", id);
    player_text(p, !strcmp(type, "sub") ? "sid" : "aid", id < 0 ? "no" : value);
}
void player_subtitle(Player *p, const char *path) {
    const char *args[] = {"sub-add", path, "select", NULL}; player_command(p, args);
}
static void tracks(Player *p, mpv_node *n) {
    g_array_set_size(p->tracks, 0);
    if (!n || n->format != MPV_FORMAT_NODE_ARRAY) return;
    for (int i = 0; i < n->u.list->num && i < 256; i++) {
        mpv_node *item = &n->u.list->values[i];
        Track t = {.id = num(member(item,"id")), .selected = num(member(item,"selected"))};
        g_strlcpy(t.type, str(member(item,"type")), sizeof(t.type));
        const char *title = str(member(item,"title")), *lang = str(member(item,"lang")), *codec = str(member(item,"codec"));
        g_snprintf(t.label, sizeof(t.label), "%d  %s%s%s%s%s", t.id, *title ? title : !strcmp(t.type,"sub") ? "Subtitles" : "Audio", *lang ? " · " : "", lang, *codec ? " · " : "", codec);
        g_array_append_val(p->tracks, t);
    }
}
void player_poll(Player *p) {
    p->changed = false;
    for (int count = 0; count < 256; count++) {
        mpv_event *e = mpv_wait_event(p->mpv, 0);
        if (e->event_id == MPV_EVENT_NONE) break;
        p->changed = true;
        if (e->event_id == MPV_EVENT_LOG_MESSAGE) {
            mpv_event_log_message *log=e->data;
            fprintf(stderr,"[%s] %s",log->prefix,log->text);
        }
        if (e->event_id == MPV_EVENT_FILE_LOADED) { p->loaded = true; p->loading = false; p->new_file = true; }
        else if (e->event_id == MPV_EVENT_END_FILE) {
            mpv_event_end_file *end = e->data;
            p->loaded = false; p->loading = false;
            if (end->reason == MPV_END_FILE_REASON_EOF) p->ended = true;
            if (end->reason == MPV_END_FILE_REASON_ERROR) {
                p->failed = true;
                g_snprintf(p->error, sizeof(p->error), "Could not play this media: %s", mpv_error_string(end->error));
            }
        } else if (e->event_id == MPV_EVENT_COMMAND_REPLY && e->error < 0) {
            g_snprintf(p->error, sizeof(p->error), "Action failed: %s", mpv_error_string(e->error));
        } else if (e->event_id == MPV_EVENT_PROPERTY_CHANGE) {
            mpv_event_property *v = e->data;
            if (!v->data) continue;
            if (v->format == MPV_FORMAT_DOUBLE) {
                double d = *(double *)v->data; if (!isfinite(d)) continue;
                if (!strcmp(v->name,"time-pos")) p->position = d;
                else if (!strcmp(v->name,"duration")) p->duration = d;
                else if (!strcmp(v->name,"volume")) p->volume = d;
                else if (!strcmp(v->name,"speed")) p->speed = d;
            } else if (v->format == MPV_FORMAT_FLAG) {
                bool b = *(int *)v->data;
                if (!strcmp(v->name,"pause")) p->paused = b;
                else if (!strcmp(v->name,"mute")) p->mute = b;
                else if (!strcmp(v->name,"seekable")) p->seekable = b;
            } else if (v->format == MPV_FORMAT_STRING) {
                const char *s = *(char **)v->data;
                if (!strcmp(v->name,"media-title")) g_strlcpy(p->title, s, sizeof(p->title));
                else if (!strcmp(v->name,"video-codec")) g_strlcpy(p->codec, s, sizeof(p->codec));
            } else if (v->format == MPV_FORMAT_NODE) {
                mpv_node *n = v->data;
                if (!strcmp(v->name,"track-list")) tracks(p, n);
                else if (!strcmp(v->name,"video-params")) {
                    p->width = num(member(n,"w")); p->height = num(member(n,"h"));
                    p->video = p->width > 0 && p->height > 0;
                } else if (!strcmp(v->name,"chapter-list")) {
                    g_array_set_size(p->chapters, 0);
                    if (n->format == MPV_FORMAT_NODE_ARRAY) for (int i = 0; i < MIN(n->u.list->num, 512); i++) {
                        mpv_node *item = &n->u.list->values[i]; Chapter c = {.time = num(member(item,"time"))};
                        g_snprintf(c.title, sizeof(c.title), "%s", str(member(item,"title")));
                        if (!*c.title) g_snprintf(c.title, sizeof(c.title), "Chapter %d", i + 1);
                        g_array_append_val(p->chapters, c);
                    }
                }
            }
        }
    }
}
