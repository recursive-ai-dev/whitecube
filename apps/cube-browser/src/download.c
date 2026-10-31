#define _GNU_SOURCE
#include "download.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <unistd.h>
static int fail(char *out,size_t n,const char *message){snprintf(out,n,"%s",message);return -1;}
int cb_download_save_to(int fd,const char *url,const char *disposition,const char *custom_directory,char *path,size_t path_size,char *error,size_t error_size){
    struct stat st;int required=F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_WRITE|F_SEAL_SEAL,seals=fcntl(fd,F_GET_SEALS);
    if(fstat(fd,&st)||!S_ISREG(st.st_mode)||st.st_size<0||st.st_size>CB_DOWNLOAD_LIMIT||seals<0||(seals&required)!=required)return fail(error,error_size,"Invalid completed download");
    char directory[4096],name[160],raw[512];const char *dest=getenv("XDG_DOWNLOAD_DIR"),*home=getenv("HOME");
    if(custom_directory&&*custom_directory){
        if(custom_directory[0]!='/')return fail(error,error_size,"Downloads folder must be an absolute path");
        for(const unsigned char *p=(const unsigned char *)custom_directory;*p;p++)
            if(*p<32||*p==127)return fail(error,error_size,"Downloads folder contains invalid characters");
        dest=custom_directory;
    }
    if((dest&&strlen(dest)>=sizeof directory)||(home&&strlen(home)>sizeof directory-12))return fail(error,error_size,"Downloads path is too long");
    if(dest&&dest[0]=='/')snprintf(directory,sizeof directory,"%s",dest);
    else if(home&&home[0]=='/')snprintf(directory,sizeof directory,"%s/Downloads",home);
    else return fail(error,error_size,"No downloads directory is available");
    size_t directory_length=strlen(directory);
    while(directory_length>1&&directory[directory_length-1]=='/')directory[--directory_length]=0;
    const char *file=disposition?strcasestr(disposition,"filename="):NULL;
    if(file){file+=9;if(*file=='"')file++;snprintf(raw,sizeof raw,"%.*s",(int)strcspn(file,"\";\r\n"),file);}
    else{snprintf(raw,sizeof raw,"%s",url);raw[strcspn(raw,"?#")]=0;file=strrchr(raw,'/');if(file)memmove(raw,file+1,strlen(file+1)+1);}
    size_t count=0;for(const unsigned char *p=(const unsigned char *)raw;*p&&count<128;p++)name[count++]=isalnum(*p)||*p=='.'||*p=='-'||*p=='_'?(char)*p:'_';name[count]=0;
    if(!count||name[0]=='.')strcpy(name,"download.bin");
    if(mkdir(directory,0700)&&errno!=EEXIST)return fail(error,error_size,"Cannot create downloads directory");
    int dir=open(directory,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);if(dir<0)return fail(error,error_size,"Cannot open downloads directory");
    if(fstat(dir,&st)||!S_ISDIR(st.st_mode)||st.st_uid!=getuid()){close(dir);return fail(error,error_size,"Downloads directory must belong to this user");}
    char chosen[192];int output=-1;
    for(unsigned i=0;i<1000;i++){if(i)snprintf(chosen,sizeof chosen,"%s.%u",name,i);else strcpy(chosen,name);output=openat(dir,chosen,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);if(output>=0||errno!=EEXIST)break;}
    if(strlen(directory)+strlen(chosen)+2>path_size){if(output>=0){close(output);unlinkat(dir,chosen,0);}close(dir);return fail(error,error_size,"Downloads path is too long");}
    if(output<0){close(dir);return fail(error,error_size,"Cannot create a new download file");}
    int failed=0;char buffer[65536];off_t offset=0;struct stat source;if(fstat(fd,&source))failed=1;
    while(!failed&&offset<source.st_size){ssize_t n=pread(fd,buffer,sizeof buffer,offset);if(n<0&&errno==EINTR)continue;if(n<=0){failed=1;break;}size_t done=0;while(done<(size_t)n){ssize_t w=write(output,buffer+done,(size_t)n-done);if(w<0&&errno==EINTR)continue;if(w<=0){failed=1;break;}done+=(size_t)w;}offset+=n;}
    if(!failed&&fsync(output))failed=1;
    if(close(output))failed=1;
    if(failed){unlinkat(dir,chosen,0);close(dir);return fail(error,error_size,"Could not save the complete download");}
    close(dir);snprintf(path,path_size,"%s/%s",directory,chosen);return 0;
}
int cb_download_save(int fd,const char *url,const char *disposition,char *path,size_t path_size,char *error,size_t error_size){
    return cb_download_save_to(fd,url,disposition,NULL,path,path_size,error,error_size);
}
