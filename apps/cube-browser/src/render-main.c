#define _GNU_SOURCE
#include "engine.h"
#include "ipc.h"
#include "dom.h"
#include "engine-internal.h"
#include <poll.h>
#include <cairo.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

/* Raster runs on a copy-on-write snapshot. Only the parent renderer owns the
 * mutable DOM; only the raster child maps the new frame writable. */
static int frame(int socket,struct cb_page *page,const struct cb_message *request){
    if(request->a<1||request->b<1||request->a>4096||request->b>4096)return -1;
    int w=(int)request->a,h=(int)request->b;double scroll=request->c;
    char *end=NULL;double zoom=strtod(request->text,&end);
    /* Page zoom and interface scale are multiplied by the UI. */
    if(!isfinite(zoom)||zoom<.25||zoom>6)zoom=1;
    unsigned flags=0;
    if(end&&*end=='\n'){
        if(end[1]<'0'||end[1]>'3'||end[2])return -1;
        flags=(unsigned)(end[1]-'0');
    }else if(end&&*end)return -1;
    cb_page_set_environment(page,h,flags&1,flags&2);
    cb_page_layout(page,w,zoom);
    double height=cb_page_height(page);scroll=fmax(0,fmin(scroll,fmax(0,height-h)));
    size_t size=(size_t)w*h*4;int fd=cb_memfd("cube-frame",size);if(fd<0)return -1;
    pid_t parent=getpid(),pid=fork();
    if(pid==0){
        close(socket);prctl(PR_SET_NAME,"cube-rasterd",0,0,0);
        if(prctl(PR_SET_PDEATHSIG,SIGTERM)<0||getppid()!=parent)_exit(1);
        alarm(10);
        void *pixels=mmap(NULL,size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);if(pixels==MAP_FAILED)_exit(1);
        cairo_surface_t *surface=cairo_image_surface_create_for_data(pixels,CAIRO_FORMAT_ARGB32,w,h,w*4);
        cairo_t *cr=cairo_create(surface);cairo_set_source_rgb(cr,1,1,1);cairo_paint(cr);
        cb_page_paint(page,cr,scroll,h);cairo_surface_flush(surface);
        int bad=cairo_status(cr)!=CAIRO_STATUS_SUCCESS||cairo_surface_status(surface)!=CAIRO_STATUS_SUCCESS;
        cairo_destroy(cr);cairo_surface_destroy(surface);munmap(pixels,size);close(fd);_exit(bad);
    }
    if(pid<0){close(fd);return -1;}
    int status;while(waitpid(pid,&status,0)<0)if(errno!=EINTR){close(fd);return -1;}
    if(!WIFEXITED(status)||WEXITSTATUS(status)){close(fd);return -1;}
    if(fcntl(fd,F_ADD_SEALS,F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_WRITE|F_SEAL_SEAL)<0){close(fd);return -1;}
    struct cb_message out={.type=CB_FRAME,.fd=fd,.a=w,.b=h,.c=(uint32_t)fmin(height,10000000)};
    int prefix=snprintf(out.text,sizeof(out.text),"%.0f\n",scroll);
    const char *title=cb_page_title(page);size_t length=strlen(title),space=sizeof(out.text)-(size_t)prefix-1;
    if(length>space){length=space;while(length&&((unsigned char)title[length]&0xc0)==0x80)length--;}
    memcpy(out.text+prefix,title,length);out.text[prefix+length]=0;
    int result=cb_send(socket,&out);close(fd);return result;
}
static int sandbox_check(void){
    if(cb_renderer_restrict())return 1;
    errno=0;int fd=socket(AF_INET,SOCK_STREAM,0);
    if(fd>=0||errno!=EPERM)return 1;
    errno=0;fd=open("/dev/null",O_WRONLY);
    if(fd>=0||errno!=EPERM)return 1;
    char *const args[]={"true",NULL};
    errno=0;execv("/bin/true",args);
    if(errno!=EPERM)return 1;
    fd=open("/dev/null",O_RDONLY);if(fd<0)return 1;close(fd);
    fd=cb_document_fd("ok",2);if(fd<0)return 1;close(fd);
    puts("Renderer restrictions: sockets, exec and writable opens denied; sealed frames allowed.");
    return 0;
}
static int resource_catalog(int socket,struct cb_page *page){
    size_t count=cb_page_resource_count(page);
    if(count>CB_MAX_RESOURCES)return -1;
    for(size_t i=0;i<count;i++){
        int kind;const char *url;
        if(cb_page_resource(page,i,&kind,&url))return -1;
        struct cb_message m={.type=CB_RESOURCE_REQUEST,.fd=-1,.a=i,.b=kind};
        if(snprintf(m.text,sizeof(m.text),"%s",url)>=(int)sizeof(m.text)||cb_send(socket,&m))return -1;
    }
    struct cb_message end={.type=CB_RESOURCES_END,.fd=-1,.a=count};
    return cb_send(socket,&end);
}
int main(int argc,char **argv){
    unsetenv("DISPLAY");cb_worker_limits();prctl(PR_SET_NAME,"cube-renderd",0,0,0);
    if(argc==2&&!strcmp(argv[1],"--sandbox-check"))return sandbox_check();
    if(argc!=3||strcmp(argv[1],"--ipc"))return 2;
    int socket=cb_ipc_fd(argv[2]);if(socket<0)return 2;
    if(cb_renderer_restrict()) {close(socket);return 1;}
    struct cb_page *page=NULL;struct cb_dom *dom=NULL;struct cb_message m;int received=0;char policy[4096]="";char *storage=NULL;int web=0,allow_eval=0,configured=0,images_enabled=1;
    for(;;){
        if(dom){alarm(15);cb_dom_tick(dom);alarm(0);}
        struct pollfd wait={socket,POLLIN,0};int event=poll(&wait,1,dom?15:-1);if(event<0&&errno==EINTR)continue;if(event<0)break;if(!event)continue;
        received=cb_recv(socket,&m);if(received!=1)break;
        alarm(15);int ok=0;
        if(m.fd>=0&&m.type!=CB_DOCUMENT&&m.type!=CB_RESOURCE_DATA&&m.type!=CB_WEB_INIT&&m.type!=CB_WEB_RESPONSE)ok=-1;
        else if(m.type==CB_RESOURCE_DATA&&m.fd<0&&m.c!=1)ok=-1;
        else if(m.type==CB_WEB_INIT&&m.a<=1&&m.b<=1&&m.c<=1){
            char *next=NULL;size_t length=0;if(m.fd<0||cb_read_document(m.fd,&next,&length)||length>600000)ok=-1;
            else{free(storage);storage=next;web=m.a;allow_eval=m.b;images_enabled=!m.c;configured=1;}
        }
        else if(m.type==CB_WEB_RESPONSE&&dom){cb_dom_response(dom,&m);}
        else if(m.type==CB_POLICY&&m.fd<0&&!m.a&&!m.b&&!m.c&&strlen(m.text)<sizeof policy){strcpy(policy,m.text);}
        else if(m.type==CB_DOCUMENT&&m.fd>=0&&m.b<=CB_DOCUMENT_SVG){
            char *html=NULL;size_t size=0;ok=cb_read_document(m.fd,&html,&size);
            if(!ok){const char *mime=m.b==CB_DOCUMENT_PNG?"image/png":m.b==CB_DOCUMENT_JPEG?"image/jpeg":m.b==CB_DOCUMENT_SVG?"image/svg+xml":NULL;
                struct cb_page *next=mime?cb_page_from_image(html,size,mime,m.text):cb_page_parse(html,size,m.text);free(html);if(!next)ok=-1;else{cb_dom_free(dom);dom=NULL;cb_page_free(page);page=next;cb_page_set_images_enabled(page,images_enabled);cb_page_set_policy(page,policy);policy[0]=0;ok=resource_catalog(socket,page);
                if(!ok&&web&&!mime)dom=cb_dom_new(page,socket,storage,allow_eval);
                if(configured&&!dom){struct cb_message state={.type=CB_WEB_STATE,.b=1,.fd=-1};ok=cb_send(socket,&state);}
            }}
        }else if(m.type==CB_RESOURCE_DATA&&page){
            int kind;const char *url;char *bytes=NULL;size_t length=0;
            struct cb_message result={.type=CB_RESOURCE_DONE,.fd=-1,.a=m.a};
            if(m.c==1&&m.fd<0&&m.a<page->resource_count&&m.b==(unsigned)page->resources[m.a].kind){page->resources[m.a].failed=1;}
            else if(!m.c&&m.fd>=0&&m.a<cb_page_resource_count(page)&&!cb_page_resource(page,m.a,&kind,&url)&&m.b==(uint32_t)kind&&
               !cb_read_document(m.fd,&bytes,&length)){
                result.b=cb_page_set_resource(page,m.a,bytes,length,m.text)==0;
            }
            free(bytes);if(m.a<page->resource_count&&!result.b)page->resources[m.a].failed=1;ok=cb_send(socket,&result);cb_dom_resource(dom);
        }else if(m.type==CB_LAYOUT&&page)ok=frame(socket,page,&m);
        else if(m.type==CB_LINK&&page){
            const char *url=cb_page_link_at(page,m.a,m.b);
            struct cb_message r={.type=CB_LINK_RESULT,.fd=-1};if(url)snprintf(r.text,sizeof(r.text),"%s",url);ok=cb_send(socket,&r);
        }else if(m.type==CB_INTERACT&&page){
            struct cb_interaction interaction;
            ok=dom?cb_dom_interact(dom,m.a,m.b,m.c,m.text,&interaction):cb_page_interact(page,m.a,m.b,m.c,m.text,&interaction);
            if(!ok){
                struct cb_message r={.type=CB_INTERACTION,.fd=-1,.a=interaction.kind,.b=interaction.focus,
                    .c=isfinite(interaction.y)&&interaction.y>=0?(uint32_t)fmin(interaction.y,10000000):UINT32_MAX};
                snprintf(r.text,sizeof(r.text),"%s",interaction.text);
                if(interaction.kind==CB_INTERACTION_POST)r.fd=cb_document_fd(interaction.body,strlen(interaction.body));
                ok=interaction.kind==CB_INTERACTION_POST&&r.fd<0?-1:cb_send(socket,&r);if(r.fd>=0)close(r.fd);
            }
        }else if(m.type==CB_ANCHOR&&page&&m.a<=1&&!m.b&&!m.c){
            const char *fragment=m.text;
            if(m.a==1){
                cb_page_set_url(page,m.text);fragment=strchr(m.text,'#');fragment=fragment?fragment+1:"";
            }
            double y=0;int found=cb_page_anchor(page,fragment,&y);
            struct cb_message r={.type=CB_ANCHOR_RESULT,.fd=-1,.a=found>0,
                .b=isfinite(y)&&y>0?(uint32_t)fmin(y,10000000):0};ok=cb_send(socket,&r);
        }else if(m.type==CB_FIND&&page){
            double y=0;int found=cb_page_find(page,m.text,m.a,&y);
            struct cb_message r={.type=CB_FOUND,.fd=-1,.a=found>0,.b=y>0?(uint32_t)y:0};ok=cb_send(socket,&r);
        }else if(m.type==CB_TEXT&&page){
            char *text=cb_page_text(page);struct cb_message r={.type=CB_TEXT_RESULT,.fd=-1};
            if(text){r.fd=cb_document_fd(text,strlen(text));free(text);}
            if(r.fd<0)ok=-1;else{ok=cb_send(socket,&r);close(r.fd);}
        }else if(m.type==CB_QUIT){if(m.fd>=0)close(m.fd);break;}else ok=-1;
        if(m.fd>=0)close(m.fd);
        alarm(0);
        if(ok<0){struct cb_message e={.type=CB_ERROR,.fd=-1};snprintf(e.text,sizeof(e.text),"Rendering failed or the document exceeds this build's limits.");if(cb_send(socket,&e)<0)break;}
    }
    cb_dom_free(dom);free(storage);cb_page_free(page);close(socket);return received<0?1:0;
}
