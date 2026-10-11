#include "state.h"
#include <glib/gstdio.h>
#include <math.h>
#include <sys/stat.h>
#include <string.h>

int main(void) {
    GError *error=NULL;char *root=g_dir_make_tmp("cube-player-state-XXXXXX",&error);g_assert_no_error(error);
    g_setenv("XDG_CONFIG_HOME",root,TRUE);g_setenv("XDG_STATE_HOME",root,TRUE);
    State s;state_init(&s);g_assert_cmpfloat(s.volume,==,70);g_assert_true(s.resume);
    char *file=g_build_filename(root,"a film ' $ (é).mp4",NULL);g_assert_true(g_file_set_contents(file,"fixture",-1,NULL));
    char *path=media_path(file,&error);g_assert_no_error(error);g_assert_cmpstr(path,==,file);g_free(path);
    char *uri=g_filename_to_uri(file,NULL,NULL);path=media_path(uri,&error);g_assert_no_error(error);g_assert_cmpstr(path,==,file);g_free(path);g_free(uri);
    path=media_path("file://remote/tmp/file.mp4",&error);g_assert_null(path);g_assert_nonnull(error);g_clear_error(&error);
    path=media_path("https://example.com/film%20one.mp4?token=a",&error);g_assert_no_error(error);g_assert_nonnull(path);g_free(path);
    path=media_path("javascript:alert(1)",&error);g_assert_null(path);g_assert_nonnull(error);g_clear_error(&error);
    path=media_path("/this-file-does-not-exist.mp4",&error);g_assert_null(path);g_assert_nonnull(error);g_clear_error(&error);
    char *title=media_title("https://example.com/film%20one.mp4?secret=not-a-title");g_assert_cmpstr(title,==,"film one.mp4");g_free(title);
    g_assert_true(media_supported("movie.MKV"));g_assert_false(media_supported("movie.txt"));g_assert_true(media_subtitle("sample.SRT"));
    state_remember(&s,file,42.5,120);g_assert_cmpfloat(state_position(&s,file),==,42.5);s.volume=38;s.speed=1.25;s.theme=2;
    g_assert_true(state_save(&s));struct stat st;g_assert_cmpint(stat(s.history_path,&st),==,0);g_assert_cmpint(st.st_mode & 0777,==,0600);
    state_free(&s);state_init(&s);g_assert_cmpfloat(s.volume,==,38);g_assert_cmpfloat(s.speed,==,1.25);g_assert_cmpint(s.theme,==,2);g_assert_cmpfloat(state_position(&s,file),==,42.5);
    state_remember(&s,file,118,120);g_assert_cmpfloat(state_position(&s,file),==,0);g_assert_cmpuint(s.recent->len,==,1);
    state_remember(&s,file,40,120);s.resume=false;g_assert_cmpfloat(state_position(&s,file),==,0);s.resume=true;
    for(int i=0;i<60;i++) {char *name=g_strdup_printf("/fixture/%d.mp4",i);state_remember(&s,name,i*10,900);g_free(name);}
    g_assert_cmpuint(s.recent->len,==,40);g_assert_cmpfloat(state_position(&s,file),==,0);
    state_forget(&s);g_assert_cmpuint(s.recent->len,==,0);g_assert_cmpfloat(state_position(&s,"/fixture/50.mp4"),==,0);
    g_assert_true(state_save(&s));
    char time[32];format_time(3661,time);g_assert_cmpstr(time,==,"1:01:01");format_time(NAN,time);g_assert_cmpstr(time,==,"00:00");
    g_remove(file);g_remove(s.config_path);g_remove(s.history_path);char *dir=g_path_get_dirname(s.config_path);g_rmdir(dir);g_free(dir);g_rmdir(root);
    state_free(&s);g_free(file);g_free(root);g_print("State, resume, URI handling, privacy, and history bounds passed.\n");return 0;
}
