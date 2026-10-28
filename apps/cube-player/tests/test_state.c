#include "state.h"
#include <glib/gstdio.h>
#include <math.h>
#include <sys/stat.h>
#include <string.h>

static void test_playlists(const char *root) {
    char *list = g_build_filename(root, "list.m3u", NULL);
    char *pls = g_build_filename(root, "radio.pls", NULL);
    char *expected = g_build_filename(root, "film é.mp4", NULL);
    GError *error = NULL; bool stream = false;
    g_assert_true(media_playlist_path(list));
    g_assert_false(media_playlist_path("https://example.com/stream.m3u8"));
    g_assert_true(g_file_set_contents(list, "\xef\xbb\xbf#EXTM3U\r\n#EXTINF:100,Film\r\n./film é.mp4\r\nhttps://example.com/song.mp3?x=1\r\n", -1, NULL));
    GPtrArray *items = media_playlist_read(list, &stream, &error);
    g_assert_no_error(error); g_assert_false(stream); g_assert_cmpuint(items->len, ==, 2);
    g_assert_cmpstr(g_ptr_array_index(items, 0), ==, expected);
    g_assert_cmpstr(g_ptr_array_index(items, 1), ==, "https://example.com/song.mp3?x=1");
    g_ptr_array_free(items, TRUE);
    // File10 must follow File2 even when the source file lists it first.
    g_assert_true(g_file_set_contents(pls, "[playlist]\nFile10=https://example.com/tenth.mp3\nFile2=./film é.mp4\nTitle2=Title is not a path\nNumberOfEntries=999999999\nVersion=2\n", -1, NULL));
    items = media_playlist_read(pls, &stream, &error);
    g_assert_no_error(error); g_assert_cmpuint(items->len, ==, 2);
    g_assert_cmpstr(g_ptr_array_index(items, 0), ==, expected);
    g_assert_cmpstr(g_ptr_array_index(items, 1), ==, "https://example.com/tenth.mp3");
    g_ptr_array_free(items, TRUE);
    // HLS manifests stay intact for mpv's segment/variant handling.
    g_file_set_contents(list, "#EXTM3U\n#EXT-X-TARGETDURATION:10\nsegment.ts\n", -1, NULL);
    items = media_playlist_read(list, &stream, &error);
    g_assert_no_error(error); g_assert_true(stream); g_assert_cmpuint(items->len, ==, 0);
    g_ptr_array_free(items, TRUE);
    g_file_set_contents(pls, "[unrelated]\nFile1=film.mp4\n", -1, NULL);
    items = media_playlist_read(pls, &stream, &error);
    g_assert_null(items); g_assert_nonnull(error); g_clear_error(&error);
    const char embedded_nul[] = "film.mp4\0hidden.mp4";
    g_file_set_contents(list, embedded_nul, sizeof(embedded_nul) - 1, NULL);
    items = media_playlist_read(list, &stream, &error);
    g_assert_null(items); g_assert_nonnull(error); g_clear_error(&error);
    g_file_set_contents(list, "\xff\xfe", 2, NULL);
    items = media_playlist_read(list, &stream, &error);
    g_assert_null(items); g_assert_nonnull(error); g_clear_error(&error);
    char *large = g_strnfill(2 * 1024 * 1024 + 1, 'a');
    g_file_set_contents(list, large, -1, NULL); g_free(large);
    items = media_playlist_read(list, &stream, &error);
    g_assert_null(items); g_assert_nonnull(error); g_clear_error(&error);
    GString *many = g_string_new(NULL);
    for (int i = 0; i < 2049; i++) g_string_append(many, "film.mp4\n");
    g_file_set_contents(list, many->str, many->len, NULL); g_string_free(many, TRUE);
    items = media_playlist_read(list, &stream, &error);
    g_assert_null(items); g_assert_nonnull(error); g_clear_error(&error);
    g_remove(list); g_remove(pls); g_free(list); g_free(pls); g_free(expected);
}

static void test_xspf(const char *root) {
    char *list = g_build_filename(root, "list.xspf", NULL);
    char *expected = g_build_filename(root, "sub", "film é.mp4", NULL);
    char *uri = g_filename_to_uri(expected, NULL, NULL);
    bool stream = true; GError *error = NULL;
    g_assert_true(media_supported("Playlist.XSPF"));
    g_assert_true(media_playlist_path(list));
    const char *xml = "<?xml version=\"1.0\"?><playlist xmlns=\"http://xspf.org/ns/0/\" version=\"1\">"
        "<title>Not a media URL</title><trackList xml:base=\"sub/\">"
        "<track><location>film%20%C3%A9.mp4</location><location>ignored.mp4</location></track>"
        "<track><location>spotify:unsupported</location><location>https://example.com/radio?a=1&amp;b=2</location></track>"
        "<track><location><![CDATA[https://example.com/another.mp3?x=1&y=2]]></location></track>"
        "<track><extension><location>ignored.mp4</location></extension></track>"
        "</trackList></playlist>";
    g_assert_true(g_file_set_contents(list, xml, -1, NULL));
    GPtrArray *items = media_playlist_read(list, &stream, &error);
    g_assert_no_error(error); g_assert_false(stream); g_assert_cmpuint(items->len, ==, 3);
    g_assert_cmpstr(g_ptr_array_index(items, 0), ==, uri);
    g_assert_cmpstr(g_ptr_array_index(items, 1), ==, "https://example.com/radio?a=1&b=2");
    g_assert_cmpstr(g_ptr_array_index(items, 2), ==, "https://example.com/another.mp3?x=1&y=2");
    g_ptr_array_free(items, TRUE);
    const char *invalid[] = {"<html/>", "<playlist><trackList>",
        "<playlist><trackList><track><location>bad<extra/>value</location></track></trackList></playlist>",
        "<!DOCTYPE playlist [<!ENTITY remote SYSTEM 'file:///etc/passwd'>]><playlist><trackList><track><location>&remote;</location></track></trackList></playlist>"};
    for (guint i = 0; i < G_N_ELEMENTS(invalid); i++) {
        g_file_set_contents(list, invalid[i], -1, NULL);
        items = media_playlist_read(list, &stream, &error);
        g_assert_null(items); g_assert_nonnull(error); g_clear_error(&error);
    }
    GString *many = g_string_new("<playlist><trackList>");
    for (int i = 0; i < 2049; i++) g_string_append(many, "<track><location>film.mp4</location></track>");
    g_string_append(many, "</trackList></playlist>");
    g_file_set_contents(list, many->str, many->len, NULL); g_string_free(many, TRUE);
    items = media_playlist_read(list, &stream, &error);
    g_assert_null(items); g_assert_nonnull(error); g_clear_error(&error);
    many = g_string_new("<playlist>");
    for (int i = 0; i < 65; i++) g_string_append(many, "<extension>");
    g_file_set_contents(list, many->str, many->len, NULL); g_string_free(many, TRUE);
    items = media_playlist_read(list, &stream, &error);
    g_assert_null(items); g_assert_nonnull(error); g_clear_error(&error);
    g_remove(list); g_free(list); g_free(expected); g_free(uri);
}

int main(void) {
    GError *error=NULL;char *root=g_dir_make_tmp("cube-player-state-XXXXXX",&error);g_assert_no_error(error);
    g_setenv("XDG_CONFIG_HOME",root,TRUE);g_setenv("XDG_STATE_HOME",root,TRUE);
    test_playlists(root);
    test_xspf(root);
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
    state_free(&s);g_free(file);g_free(root);g_print("State, resume, URI handling, privacy, bounded M3U/PLS/XSPF parsing, and history bounds passed.\n");return 0;
}
