#ifndef CP_PLAYER_H
#define CP_PLAYER_H
#include <mpv/client.h>
#include <glib.h>
#include <stdbool.h>
typedef struct { int id; char type[16], label[256]; bool selected; } Track;
typedef struct { double time; char title[256]; } Chapter;
typedef struct {
    mpv_handle *mpv;
    double position, duration, volume, speed;
    bool paused, mute, loaded, loading, video, seekable;
    bool ended, failed, changed, new_file;
    char title[1024], error[512], codec[128];
    int width, height;
    GArray *tracks, *chapters;
} Player;
bool player_init(Player *p, bool silent);
void player_free(Player *p);
void player_poll(Player *p);
void player_command(Player *p, const char **args);
void player_number(Player *p, const char *key, double number);
void player_flag(Player *p, const char *key, bool value);
void player_text(Player *p, const char *key, const char *value);
void player_load(Player *p, const char *path, double start);
void player_seek(Player *p, double seconds, bool absolute);
void player_track(Player *p, const char *type, int id);
void player_subtitle(Player *p, const char *path);
#endif
