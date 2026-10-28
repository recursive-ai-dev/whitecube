#ifndef CP_STATE_H
#define CP_STATE_H
#include <glib.h>
#include <stdbool.h>
typedef struct {
    char *path, *title;
} Media;
typedef struct {
    GKeyFile *settings, *history;
    char *config_path, *history_path;
    double volume, speed;
    bool resume, sidebar, shuffle;
    int repeat, theme;
    GPtrArray *recent;
} State;
void state_init(State *s);
bool state_save(State *s);
void state_free(State *s);
double state_position(State *s, const char *path);
void state_remember(State *s, const char *path, double pos, double duration);
void state_forget(State *s);
char *media_path(const char *input, GError **error);
char *media_title(const char *path);
bool media_supported(const char *path);
bool media_subtitle(const char *path);
bool media_playlist_path(const char *path);
/* Resolved paths, owned by the array. HLS returns an empty array with stream=true. */
GPtrArray *media_playlist_read(const char *path, bool *stream, GError **error);
Media *media_new(const char *path);
void media_free(gpointer media);
void format_time(double seconds, char out[32]);
#endif
