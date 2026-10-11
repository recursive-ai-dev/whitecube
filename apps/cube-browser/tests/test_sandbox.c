#define _GNU_SOURCE
#include "ipc.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static void child(const char *secret,const char *ca,int network) {
    int document=cb_document_fd("inherited document",18);assert(document>=0);
    assert(!(network?cb_network_restrict(ca):cb_renderer_restrict()));
    assert(open(secret,O_RDONLY)<0);
    assert(open(secret,O_WRONLY|O_TRUNC)<0);
    assert(open("/tmp/cube-forbidden-write",O_CREAT|O_WRONLY,0600)<0);
    assert(socket(AF_UNIX,SOCK_STREAM,0)<0);
    int sock=socket(AF_INET,SOCK_STREAM,0);
    assert(network?sock>=0:sock<0);if(sock>=0)close(sock);
    assert(mmap(NULL,4096,PROT_READ|PROT_WRITE|PROT_EXEC,MAP_PRIVATE|MAP_ANONYMOUS,-1,0)==MAP_FAILED);
    char *const argv[]={"true",NULL};assert(execv("/usr/bin/true",argv)<0);
    char *data=NULL;size_t size;assert(!cb_read_document(document,&data,&size));assert(size==18);free(data);close(document);
    if(network){int fd=open(ca,O_RDONLY);assert(fd>=0);close(fd);}
    else assert(open(ca,O_RDONLY)<0);
    _exit(0);
}
int main(void) {
    char secret[]="/tmp/cube-sandbox-secret-XXXXXX",ca[]="/tmp/cube-sandbox-ca-XXXXXX";
    int fd=mkstemp(secret);assert(fd>=0);assert(write(fd,"fake secret",11)==11);close(fd);
    fd=mkstemp(ca);assert(fd>=0);close(fd);
    for(int network=0;network<2;network++){
        pid_t pid=fork();assert(pid>=0);if(!pid)child(secret,ca,network);
        int status;assert(waitpid(pid,&status,0)==pid);assert(WIFEXITED(status)&&WEXITSTATUS(status)==0);
    }
    unlink(secret);unlink(ca);puts("renderer/network filesystem, syscall and descriptor boundaries passed");
}
