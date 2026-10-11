#define _GNU_SOURCE
#include "ipc.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>

static int valid_utf8(const unsigned char *text,size_t size){
    for(size_t at=0;at<size;){
        unsigned first=text[at++],count;uint32_t point,minimum;
        if(first<0x80)continue;
        if(first>=0xc2&&first<=0xdf){count=1;point=first&31;minimum=0x80;}
        else if(first>=0xe0&&first<=0xef){count=2;point=first&15;minimum=0x800;}
        else if(first>=0xf0&&first<=0xf4){count=3;point=first&7;minimum=0x10000;}
        else return 0;
        if(count>size-at)return 0;
        while(count--){unsigned next=text[at++];if((next&0xc0)!=0x80)return 0;point=(point<<6)|(next&63);}
        if(point<minimum||point>0x10ffff||(point>=0xd800&&point<=0xdfff))return 0;
    }
    return 1;
}
static size_t integer(unsigned char *p, unsigned major, uint32_t n) {
    if(n<24){p[0]=(major<<5)|n; return 1;}
    if(n<=255){p[0]=(major<<5)|24; p[1]=n; return 2;}
    if(n<=65535){p[0]=(major<<5)|25;p[1]=n>>8;p[2]=n;return 3;}
    p[0]=(major<<5)|26;for(int i=0;i<4;i++)p[i+1]=n>>(24-i*8);return 5;
}
static int decode(const unsigned char **p,const unsigned char *end,unsigned major,uint32_t *v){
    if(*p>=end||(**p>>5)!=major)return -1;
    unsigned n=*(*p)++ &31; if(n<24){*v=n;return 0;}
    unsigned bytes=n==24?1:n==25?2:n==26?4:0;
    if(!bytes||(size_t)(end-*p)<bytes)return -1;
    *v=0;while(bytes--)*v=(*v<<8)|*(*p)++;return 0;
}
int cb_send(int s,const struct cb_message *m){
    unsigned char buf[8256]; size_t n=4, len=strnlen(m->text,sizeof(m->text));
    if(len==sizeof(m->text)){errno=EMSGSIZE;return -1;}
    if(!valid_utf8((const unsigned char *)m->text,len)){errno=EILSEQ;return -1;}
    buf[n++]=0x85;n+=integer(buf+n,0,m->type);n+=integer(buf+n,0,m->a);
    n+=integer(buf+n,0,m->b);n+=integer(buf+n,0,m->c);n+=integer(buf+n,3,len);
    memcpy(buf+n,m->text,len);n+=len;uint32_t size=n-4;
    for(int i=0;i<4;i++)buf[i]=size>>(24-i*8);
    struct iovec io={buf,n}; union {struct cmsghdr align; char b[CMSG_SPACE(sizeof(int))];} control={0};
    struct msghdr msg={.msg_iov=&io,.msg_iovlen=1};
    if(m->fd>=0){msg.msg_control=control.b;msg.msg_controllen=sizeof(control.b);
        struct cmsghdr *c=CMSG_FIRSTHDR(&msg);c->cmsg_level=SOL_SOCKET;c->cmsg_type=SCM_RIGHTS;
        c->cmsg_len=CMSG_LEN(sizeof(int));memcpy(CMSG_DATA(c),&m->fd,sizeof(int));}
    ssize_t r;do{r=sendmsg(s,&msg,MSG_NOSIGNAL);}while(r<0&&errno==EINTR);
    return r==(ssize_t)n?0:-1;
}
int cb_recv(int s,struct cb_message *m){
    unsigned char buf[8256];union {struct cmsghdr align;char b[CMSG_SPACE(sizeof(int)*8)];} control;
    struct iovec io={buf,sizeof(buf)};struct msghdr msg={.msg_iov=&io,.msg_iovlen=1,.msg_control=control.b,.msg_controllen=sizeof(control.b)};
    memset(m,0,sizeof(*m));m->fd=-1;
    ssize_t n;do{n=recvmsg(s,&msg,MSG_CMSG_CLOEXEC);}while(n<0&&errno==EINTR);
    if(n<0)return -1;
    int count=0;
    for(struct cmsghdr *c=CMSG_FIRSTHDR(&msg);c;c=CMSG_NXTHDR(&msg,c)){
        if(c->cmsg_level==SOL_SOCKET&&c->cmsg_type==SCM_RIGHTS){
            if(c->cmsg_len<CMSG_LEN(0))goto bad;
            size_t bytes=c->cmsg_len-CMSG_LEN(0);
            for(size_t i=0;i+sizeof(int)<=bytes;i+=sizeof(int)){int fd;memcpy(&fd,(char*)CMSG_DATA(c)+i,sizeof(fd));
                if(count++==0)m->fd=fd;else close(fd);}
        }
    }
    if(n==0){if(count)goto bad;return 0;}
    if(n<5||(msg.msg_flags&(MSG_TRUNC|MSG_CTRUNC))||count>1)goto bad;
    uint32_t len=0;for(int i=0;i<4;i++)len=(len<<8)|buf[i];
    if(len!=(uint32_t)n-4||buf[4]!=0x85)goto bad;
    const unsigned char *p=buf+5,*end=buf+n;
    if(decode(&p,end,0,&m->type)||decode(&p,end,0,&m->a)||decode(&p,end,0,&m->b)||decode(&p,end,0,&m->c)||decode(&p,end,3,&len))goto bad;
    if(len>=sizeof(m->text)||(size_t)(end-p)!=len||memchr(p,0,len)||!valid_utf8(p,len))goto bad;
    memcpy(m->text,p,len);m->text[len]=0;return 1;
bad:if(m->fd>=0)close(m->fd);m->fd=-1;errno=EPROTO;return -1;
}
int cb_memfd(const char *name,size_t size){
    if(size>128u*1024u*1024u){errno=EFBIG;return -1;}
    int fd=memfd_create(name,MFD_CLOEXEC|MFD_ALLOW_SEALING);
    if(fd<0)return -1;
    if(ftruncate(fd,size)){close(fd);return -1;}return fd;
}
int cb_blob_fd(const char *data,size_t size){
    if(size>64u*1024u*1024u){errno=EFBIG;return -1;}
    int fd=cb_memfd("cube-document",size);if(fd<0)return -1;
    size_t done=0;while(done<size){ssize_t n=write(fd,data+done,size-done);if(n<0&&errno==EINTR)continue;if(n<=0){close(fd);return -1;}done+=n;}
    if(fcntl(fd,F_ADD_SEALS,F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_WRITE|F_SEAL_SEAL)<0){close(fd);return -1;}
    lseek(fd,0,SEEK_SET);return fd;
}
int cb_document_fd(const char *data,size_t size){if(size>2u*1024u*1024u){errno=EFBIG;return -1;}return cb_blob_fd(data,size);}
int cb_read_document(int fd,char **data,size_t *size){
    if(!data||!size){errno=EINVAL;return -1;}
    *data=NULL;*size=0;
    struct stat st;if(fstat(fd,&st))return -1;
    if(!S_ISREG(st.st_mode)||st.st_size<0||st.st_size>2*1024*1024){errno=EFBIG;return -1;}
    int seals=fcntl(fd,F_GET_SEALS);
    int required=F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_WRITE|F_SEAL_SEAL;
    if(seals<0||(seals&required)!=required){errno=EPERM;return -1;}
    *size=st.st_size;*data=malloc(*size+1);if(!*data)return -1;
    size_t done=0;while(done<*size){ssize_t n=pread(fd,*data+done,*size-done,done);if(n<0&&errno==EINTR)continue;if(n<=0){free(*data);*data=NULL;return -1;}done+=n;}
    (*data)[*size]=0;return 0;
}
void cb_worker_limits(void){
    prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0);prctl(PR_SET_DUMPABLE,0,0,0,0);
    struct rlimit mem={512u*1024u*1024u,512u*1024u*1024u};setrlimit(RLIMIT_AS,&mem);
    struct rlimit core={0,0};setrlimit(RLIMIT_CORE,&core);
    struct rlimit files={64,64};setrlimit(RLIMIT_NOFILE,&files);
}
int cb_ipc_fd(const char *argument){
    if(!argument||!*argument){errno=EINVAL;return -1;}
    char *end=NULL;errno=0;long value=strtol(argument,&end,10);
    if(errno||*end||value<0||value>INT_MAX){errno=EINVAL;return -1;}
    int type=0;socklen_t size=sizeof type;
    if(getsockopt((int)value,SOL_SOCKET,SO_TYPE,&type,&size)<0)return -1;
    if(type!=SOCK_SEQPACKET){errno=EPROTOTYPE;return -1;}
    return (int)value;
}

