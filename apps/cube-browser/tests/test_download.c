#define _GNU_SOURCE
#include "download.h"
#include "ipc.h"
#include <assert.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
int main(void){
    char root[]="/tmp/cube-download-XXXXXX";assert(mkdtemp(root));setenv("HOME",root,1);unsetenv("XDG_DOWNLOAD_DIR");
    char path[4096],next[4096],error[256],directory[512];snprintf(directory,sizeof directory,"%s/Downloads",root);
    int fd=cb_document_fd("download data",13);assert(fd>=0);
    assert(!cb_download_save(fd,"https://example.org/file.bin","attachment; filename=../../unsafe",path,sizeof path,error,sizeof error));
    assert(strstr(path,"/Downloads/download.bin"));struct stat st;assert(!stat(path,&st)&&st.st_size==13&&(st.st_mode&0777)==0600);
    assert(!cb_download_save(fd,"https://example.org/download.bin","",next,sizeof next,error,sizeof error));assert(strcmp(path,next));
    int saved=open(path,O_RDONLY);char content[32]={0};assert(read(saved,content,sizeof content)==13&&!strcmp(content,"download data"));close(saved);
    unlink(path);unlink(next);rmdir(directory);assert(!symlink(root,directory));
    assert(cb_download_save(fd,"https://example.org/x","",path,sizeof path,error,sizeof error));unlink(directory);
    char custom[512];snprintf(custom,sizeof custom,"%s/Custom Downloads",root);
    assert(!cb_download_save_to(fd,"https://example.org/file.bin","",custom,path,sizeof path,error,sizeof error));
    assert(!strncmp(path,custom,strlen(custom))&&!stat(path,&st)&&st.st_size==13&&(st.st_mode&0777)==0600);
    assert(!getenv("XDG_DOWNLOAD_DIR")); /* A window's folder never mutates process defaults. */
    assert(cb_download_save_to(fd,"https://example.org/x","","relative/folder",next,sizeof next,error,sizeof error));
    assert(cb_download_save_to(fd,"https://example.org/x","","/tmp/bad\nfolder",next,sizeof next,error,sizeof error));
    unlink(path);rmdir(custom);assert(!symlink(root,custom));
    assert(cb_download_save_to(fd,"https://example.org/x","",custom,path,sizeof path,error,sizeof error));
    strcat(custom,"/");assert(cb_download_save_to(fd,"https://example.org/x","",custom,path,sizeof path,error,sizeof error));
    custom[strlen(custom)-1]=0;unlink(custom);
    int mutable=cb_memfd("mutable",1);assert(cb_download_save(mutable,"https://example.org/x","",path,sizeof path,error,sizeof error));close(mutable);close(fd);rmdir(root);
    puts("download seals, permissions, traversal sanitization, no overwrite and symlink refusal passed");
}
