#define main cube_player_entry_point
#include "../src/main.c"
#undef main

int main(void) {
    char *root=g_dir_make_tmp("cube-player-ui-XXXXXX",NULL);g_assert_nonnull(root);
    g_setenv("XDG_CONFIG_HOME",root,TRUE);g_setenv("XDG_STATE_HOME",root,TRUE);
    App a={.current=-1,.focus=-1,.scale=1,.ui_scale=1};
    state_init(&a.state);a.queue=g_ptr_array_new_with_free_func(media_free);a.entries=g_ptr_array_new_with_free_func(entry_free);
    a.player.tracks=g_array_new(FALSE,FALSE,sizeof(Track));a.player.chapters=g_array_new(FALSE,FALSE,sizeof(Chapter));a.player.volume=70;a.player.speed=1;
    theme_reload(&a);browse(&a,root);
    int sizes[][2]={{640,400},{800,450},{1120,740},{1920,1080}};
    g_mkdir_with_parents("validation",0700);
    for(int scaled=0;scaled<2;scaled++) for(guint i=0;i<G_N_ELEMENTS(sizes);i++) for(int panel=NONE;panel<=LOCATION;panel++) {
        a.ui_scale=scaled?1.6:1;a.panel=panel;
        cairo_surface_t *surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,sizes[i][0],sizes[i][1]);
        cairo_t *cr=cairo_create(surface);draw_app(&a,cr,sizes[i][0],sizes[i][1]);
        g_assert_cmpint(cairo_status(cr),==,CAIRO_STATUS_SUCCESS);
        for(int j=0;j<a.nhit;j++) {
            Hit *hit=&a.hit[j];
            if(hit->x<0 || hit->y<0 || hit->x+hit->w>a.vw+.01 || hit->y+hit->h>a.vh+.01) {
                g_error("Control outside viewport: %s panel=%d scale=%d viewport=%dx%d hit=%.1f %.1f %.1f %.1f",hit->label,panel,scaled,sizes[i][0],sizes[i][1],hit->x,hit->y,hit->w,hit->h);
            }
        }
        if(!scaled && i==0) {char name[128];g_snprintf(name,sizeof(name),"validation/compact-panel-%d.png",panel);g_assert_cmpint(cairo_surface_write_to_png(surface,name),==,CAIRO_STATUS_SUCCESS);}
        cairo_destroy(cr);cairo_surface_destroy(surface);
    }
    // Follow actual Whitecube colour and accessibility preferences.
    char *dir=g_build_filename(root,"whitecube",NULL);g_mkdir_with_parents(dir,0700);
    char *config=g_build_filename(dir,"config",NULL),*access=g_build_filename(dir,"accessibility.json",NULL);
    g_file_set_contents(config,"bg=142536\ntabbar=213243\n",-1,NULL);
    g_file_set_contents(access,"{\"ui_scale\": 1.4, \"contrast\": \"strong\"}",-1,NULL);
    theme_reload(&a);g_assert_true(a.strong);g_assert_cmpfloat(a.ui_scale,==,1.4);g_assert_cmpstr(a.theme.background,==,"000000");
    g_file_set_contents(access,"{\"ui_scale\": 1, \"contrast\": \"normal\"}",-1,NULL);
    theme_reload(&a);g_assert_cmpstr(a.theme.background,==,"142536");
    a.state.theme=1;theme_reload(&a);g_assert_cmpstr(a.theme.background,==,"fbfbfb");
    char *media=g_build_filename(root,"a ' strange; name.mp4",NULL);g_file_set_contents(media,"test",-1,NULL);
    char *list=g_build_filename(root,"list.m3u",NULL);g_file_set_contents(list,"#EXTM3U\na ' strange; name.mp4\n",-1,NULL);
    g_assert_cmpint(add_media(&a,list,0),==,1);g_assert_cmpstr(((Media *)g_ptr_array_index(a.queue,0))->path,==,media);
    // BOM, relative paths and a PLS file are expanded into the application's queue.
    g_file_set_contents(list,"\xef\xbb\xbf#EXTM3U\n./a ' strange; name.mp4\n",-1,NULL);
    g_assert_cmpint(add_media(&a,list,0),==,1);
    char *pls=g_build_filename(root,"list.pls",NULL);
    g_file_set_contents(pls,"[playlist]\nFile2=https://example.com/radio.mp3\nFile1=list.m3u\nVersion=2\n",-1,NULL);
    g_assert_cmpint(add_media(&a,pls,0),==,2);
    g_assert_cmpstr(((Media *)g_ptr_array_index(a.queue,2))->path,==,media);
    g_assert_cmpstr(((Media *)g_ptr_array_index(a.queue,3))->path,==,"https://example.com/radio.mp3");
    // HLS remains one media item; mpv must receive the complete manifest.
    g_file_set_contents(list,"#EXTM3U\n#EXT-X-TARGETDURATION:10\nsegment.ts\n",-1,NULL);
    g_assert_cmpint(add_media(&a,list,0),==,1);
    g_assert_cmpstr(((Media *)g_ptr_array_index(a.queue,4))->path,==,list);
    g_remove(pls);g_free(pls);
    // Caret movement and deletion preserve UTF-8 boundaries inside paths.
    show_panel(&a,LOCATION);a.input_focus=true;a.input[0]=0;a.input_cursor=0;
    insert(&a,"aéz");key_app(&a,XKB_KEY_Home,"",false,false,false);key_app(&a,XKB_KEY_Right,"",false,false,false);
    key_app(&a,XKB_KEY_Delete,"",false,false,false);g_assert_cmpstr(a.input,==,"az");
    key_app(&a,XKB_KEY_End,"",false,false,false);key_app(&a,XKB_KEY_BackSpace,"",false,false,false);g_assert_cmpstr(a.input,==,"a");
    key_app(&a,XKB_KEY_Home,"",false,false,false);insert(&a,"ø");g_assert_cmpstr(a.input,==,"øa");
    key_app(&a,XKB_KEY_a,"",true,false,false);insert(&a,"replacement.mkv");g_assert_cmpstr(a.input,==,"replacement.mkv");
    // A self-reference stops at the nesting bound rather than growing forever.
    g_file_set_contents(list,"list.m3u\n",-1,NULL);g_assert_cmpint(add_media(&a,list,0),==,0);g_assert_cmpuint(a.queue->len,==,5);
    // A cycle skips only its own branch; repeated valid entries remain intentional.
    g_file_set_contents(list,"./list.m3u\na ' strange; name.mp4\na ' strange; name.mp4\n",-1,NULL);
    g_assert_cmpint(add_media(&a,list,0),==,2);g_assert_cmpuint(a.queue->len,==,7);
    // VLC-style XSPF locations use URI escaping and share cycle/nesting bounds.
    char *xspf=g_build_filename(root,"list.xspf",NULL);
    g_file_set_contents(xspf,"<playlist xmlns=\"http://xspf.org/ns/0/\"><trackList><track><location>a%20%27%20strange%3B%20name.mp4</location></track><track><location>list.xspf</location></track></trackList></playlist>",-1,NULL);
    g_assert_cmpint(add_media(&a,xspf,0),==,1);g_assert_cmpuint(a.queue->len,==,8);
    g_assert_cmpstr(((Media *)g_ptr_array_index(a.queue,7))->path,==,media);
    g_remove(xspf);g_free(xspf);
    g_remove(config);g_remove(access);g_rmdir(dir);g_remove(media);g_remove(list);g_rmdir(root);
    g_free(config);g_free(access);g_free(dir);g_free(media);g_free(list);g_free(root);
    state_free(&a.state);g_ptr_array_free(a.queue,TRUE);g_ptr_array_free(a.entries,TRUE);g_array_free(a.player.tracks,TRUE);g_array_free(a.player.chapters,TRUE);g_free(a.folder);
    puts("48 responsive panel layouts, Whitecube preferences, literal media paths, and nested playlists passed.");return 0;
}
