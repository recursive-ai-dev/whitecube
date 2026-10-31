#include "version.h"
#define _GNU_SOURCE
#include "ipc.h"
#include "platform.h"
#include "storage.h"
#include "url.h"
#include "home.h"
#include "engine.h"
#include "policy.h"
#include "broker.h"
#include "dom.h"
#include "download.h"
#include "net.h"
#include <linux/landlock.h>
#include <sys/syscall.h>
#include <cairo.h>
#include <pango/pangocairo.h>
#include <glib.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/input-event-codes.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
struct worker {int socket;pid_t pid;struct cb_http_job *job;};
enum resource_state { RESOURCE_EMPTY, RESOURCE_QUEUED, RESOURCE_FETCHING,
                      RESOURCE_APPLYING, RESOURCE_DONE, RESOURCE_FAILED };
struct resource {
    enum resource_state state;
    unsigned kind;bool notified;
    char url[4096];
    struct worker worker;
};
struct web_request {struct worker worker;unsigned state,id,credentials;char url[4096];};
struct browser {
    struct cb_session *session;
    struct web_request web[32];
    bool print_console,scripts_enabled,script_loading,script_busy,bookmarked;double script_settle;
    char console[128][2048];unsigned console_count;
    char network_log[128][1024];unsigned network_count;
    unsigned panel,panel_selected,panel_scroll;bool network_panel;
    unsigned settings_category;bool settings_editing,settings_select_all;
    char settings_text[4096],settings_error[256];size_t settings_cursor;
    struct worker download;unsigned download_state,download_received,download_total;
    char download_path[4096],download_status[256];
    struct cb_window *window;struct cb_config config;struct cb_accessibility acc;
    struct worker net,render;char executable[PATH_MAX],directory[PATH_MAX];
    char url[4096],address[4096],title[512],status[512],find[256];
    char *trail[128];int trail_count,trail_at;bool address_focus,select_all,find_focus,find_visible;
    size_t cursor;double scroll,desired_scroll,zoom,scale,last_preferences,find_y;
    int w,h,view_y,view_h,frame_w,frame_h,layout_w,layout_h;double layout_zoom;uint32_t page_h;
    cairo_surface_t *frame;void *pixels;size_t pixels_size;
    bool busy,layout_pending,relayout,ready,error,history_saved,private_mode,find_wrapping;int http_status;
    struct resource resources[CB_MAX_RESOURCES];
    unsigned resource_count;
    size_t resource_bytes;
    bool catalog_pending;
    double resource_deadline;
    unsigned page_focus,interaction_pending;
    unsigned interaction_head;unsigned char interaction_permissions[128];
    bool anchor_needed,anchor_pending;
    char notice[512];double notice_until;
    char policy[CB_POLICY_LIMIT];bool policy_received;
};
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static void panel_open(struct browser *b,unsigned panel);
static void utf8_copy(char *out,size_t capacity,const char *value){
    if(!capacity)return;
    size_t length=strlen(value);if(length>=capacity){length=capacity-1;
        while(length&&((unsigned char)value[length]&0xc0)==0x80)length--;}
    memcpy(out,value,length);out[length]=0;
}
static void dirty(struct browser*b){cb_window_dirty(b->window);}
static void stop_worker(struct worker*w){
    free(w->job);w->job=NULL;
    if(w->socket>=0){close(w->socket);w->socket=-1;}
    if(w->pid>0){kill(w->pid,SIGKILL);int status;while(waitpid(w->pid,&status,0)<0&&errno==EINTR){}w->pid=0;}
}
static void reset_resources(struct browser *b){
    for(unsigned i=0;i<32;i++){if(b->web[i].state==2)stop_worker(&b->web[i].worker);memset(&b->web[i],0,sizeof b->web[i]);b->web[i].worker.socket=-1;}
    b->script_loading=b->script_busy=false;
    for(unsigned i=0;i<CB_MAX_RESOURCES;i++){
        struct resource *r=&b->resources[i];
        if(r->state==RESOURCE_FETCHING)stop_worker(&r->worker);
        memset(r,0,sizeof(*r));r->worker.socket=-1;
    }
    b->resource_count=0;b->resource_bytes=0;b->catalog_pending=false;
}
static bool resources_loading(const struct browser *b){
    if(b->catalog_pending)return true;
    for(unsigned i=0;i<b->resource_count;i++){
        enum resource_state s=b->resources[i].state;
        if(s==RESOURCE_QUEUED||s==RESOURCE_FETCHING||s==RESOURCE_APPLYING)return true;
    }
    return false;
}
static bool loading(const struct browser *b){return b->busy||resources_loading(b)||b->script_loading;}
static void page_status(struct browser *b){
    if(b->error)return;
    if(*b->notice&&now()<b->notice_until){utf8_copy(b->status,sizeof(b->status),b->notice);return;}
    const char *connection=cb_url_remote(b->url)?
        (g_str_has_prefix(b->url,"https:")?"HTTPS · verified connection":"HTTP · unencrypted connection"):"Local page";
    snprintf(b->status,sizeof(b->status),"%s%s",b->private_mode?"Private history · ":"",connection);
    if(b->http_status>=400)snprintf(b->status,sizeof(b->status),"HTTP %d · The server returned an error page",b->http_status);
    unsigned completed=0,failed=0;
    for(unsigned i=0;i<b->resource_count;i++){
        completed+=b->resources[i].state==RESOURCE_DONE||b->resources[i].state==RESOURCE_FAILED;
        failed+=b->resources[i].state==RESOURCE_FAILED;
    }
    size_t n=strlen(b->status);
    if(b->resource_count&&completed<b->resource_count)
        snprintf(b->status+n,sizeof(b->status)-n," · Loading images/styles %u/%u",completed,b->resource_count);
    else if(failed)snprintf(b->status+n,sizeof(b->status)-n," · %u images/styles unavailable",failed);
}
static void renderer_failed(struct browser *b,const char *message){
    reset_resources(b);stop_worker(&b->render);
    b->ready=false;b->busy=false;b->layout_pending=false;b->relayout=false;b->error=true;
    b->interaction_pending=0;b->page_focus=CB_FOCUS_NONE;b->anchor_needed=b->anchor_pending=false;
    snprintf(b->status,sizeof(b->status),"%s",message);dirty(b);
}
static int start_worker(struct browser*b,struct worker*w,const char*name){
    stop_worker(w);int fd[2];if(socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0,fd))return -1;
    char path[PATH_MAX];if(snprintf(path,sizeof(path),"%s/%s",b->directory,name)>=(int)sizeof(path)){close(fd[0]);close(fd[1]);return -1;}
    pid_t pid=fork();if(pid==0){
        if(fd[1]!=3){if(dup2(fd[1],3)<0)_exit(126);}else fcntl(3,F_SETFD,0);
        int null=open("/dev/null",O_RDWR|O_CLOEXEC);
        if(null>=0){dup2(null,STDIN_FILENO);dup2(null,STDOUT_FILENO);if(null>3)close(null);}
        if(chdir("/"))_exit(126);
        clearenv();setenv("PATH","/usr/bin:/bin",1);setenv("LANG","C.UTF-8",1);setenv("HOME","/nonexistent",1);
        close_range(4,~0U,0);execl(path,name,"--ipc","3",(char*)NULL);_exit(127);
    }
    close(fd[1]);if(pid<0){close(fd[0]);return -1;}w->pid=pid;w->socket=fd[0];return 0;
}
static int network_start(struct browser *b,struct worker *w,struct cb_http_job *job){
    if(!job)return -1;
    if(b->config.https_only&&!g_str_has_prefix(job->request.url,"https://")){free(job);return -1;}
    if(!job->started)job->started=now();
    if(start_worker(b,w,"cube-netd")){free(job);return -1;}w->job=job;
    if(cb_broker_send(job,b->session,w->socket)){stop_worker(w);return -1;}return 0;
}
static int network_read(struct browser *b,struct worker *w,struct cb_message *m){
    int n=cb_recv(w->socket,m);if(n!=1)return -1;if(!w->job)return n;
    char request_url[4096];snprintf(request_url,sizeof request_url,"%s",w->job->request.url);bool was_post=w->job->request.post;
    int rc=cb_broker_receive(w->job,b->session,m);
    if(rc==0)return 0;
    unsigned log_index=b->network_count++%128;snprintf(b->network_log[log_index],sizeof b->network_log[log_index],"%s %u  %.0fms  %.860s%s",
        was_post?"POST":"GET",w->job->meta.status,(now()-w->job->started)*1000,request_url,rc<0?"  (blocked/failed)":"");
    if(rc==2){struct cb_http_job *job=w->job;w->job=NULL;
        job->started=0;
        if(network_start(b,w,job)){m->type=CB_ERROR;strcpy(m->text,"Redirect blocked or worker unavailable");return 1;}return 0;
    }
    if(rc<0){if(m->fd>=0)close(m->fd);m->fd=-1;m->type=CB_ERROR;snprintf(m->text,sizeof m->text,"%s",w->job->error);}
    else if(w==&b->net){strcpy(b->policy,w->job->meta.policy);b->policy_received=true;}
    return 1;
}
static void free_frame(struct browser*b){if(b->frame)cairo_surface_destroy(b->frame);b->frame=NULL;if(b->pixels)munmap(b->pixels,b->pixels_size);b->pixels=NULL;}
static void geometry(struct browser*b,int w,int h){b->scale=fmin(fmax(.5,fmin(3,b->acc.ui_scale)),w/480.);b->w=w;b->h=h;b->view_y=(int)(90*b->scale);b->view_h=h-b->view_y-(int)(28*b->scale);if(b->view_h<1)b->view_h=1;}
static void request_layout(struct browser*b){
    if(b->render.socket<0||!b->ready)return;
    if(b->layout_pending){b->relayout=true;return;}
    struct cb_message m={.type=CB_LAYOUT,.fd=-1,.a=b->w,.b=b->view_h,.c=(uint32_t)fmax(0,b->desired_scroll)};
    snprintf(m.text,sizeof(m.text),"%.4f\n%u",b->zoom*b->scale,
        (b->acc.reduced_motion?1u:0u)|(b->acc.strong_contrast?2u:0u));
    if(cb_send(b->render.socket,&m)){snprintf(b->status,sizeof(b->status),"Renderer disconnected. Reload to try again.");b->busy=false;b->error=true;return;}
    b->layout_pending=true;b->layout_w=b->w;b->layout_h=b->view_h;b->layout_zoom=b->zoom*b->scale;b->relayout=false;
}
static void notice(struct browser *b,const char *message){
    utf8_copy(b->notice,sizeof(b->notice),message);b->notice_until=now()+8;
    utf8_copy(b->status,sizeof(b->status),message);dirty(b);
}
static void interact(struct browser *b,unsigned action,double x,double y,const char *text){
    if(!b->ready||b->render.socket<0||b->interaction_pending>=128)return;
    struct cb_message m={.type=CB_INTERACT,.fd=-1,.a=action,
        .b=(uint32_t)fmax(0,fmin(x,10000000)),.c=(uint32_t)fmax(0,fmin(y,10000000))};
    if(text){if(strlen(text)>=4096){notice(b,"Page input is limited to 4095 UTF-8 bytes at a time.");return;}snprintf(m.text,sizeof(m.text),"%s",text);}
    if(cb_send(b->render.socket,&m)){renderer_failed(b,"Renderer disconnected. Reload to try again.");return;}
    unsigned char permissions=0;
    if(action==CB_INTERACT_CLICK)permissions=3;
    else if(action==CB_INTERACT_KEY&&text){
        if(!strcmp(text,"enter"))permissions=3;
        else if(!strcmp(text,"space"))permissions=2;
        else if(!strcmp(text,"copy")||!strcmp(text,"cut"))permissions=4;
    }
    b->interaction_permissions[(b->interaction_head+b->interaction_pending)%128]=permissions;
    b->interaction_pending++;
}
static void blur_page(struct browser *b){interact(b,CB_INTERACT_BLUR,0,0,NULL);b->page_focus=CB_FOCUS_NONE;}
static void anchor_pump(struct browser *b){
    if(!b->anchor_needed||b->anchor_pending||!b->ready||b->layout_pending||b->relayout||resources_loading(b))return;
    struct cb_message m={.type=CB_ANCHOR,.fd=-1,.a=1};snprintf(m.text,sizeof(m.text),"%s",b->url);
    if(cb_send(b->render.socket,&m)){renderer_failed(b,"Renderer disconnected while locating the anchor.");return;}
    b->anchor_pending=true;b->anchor_needed=false;
}
static int document_kind(struct browser*b,int fd,const char*url,unsigned kind){
    reset_resources(b);stop_worker(&b->render);b->layout_pending=false;b->ready=false;
    b->interaction_pending=0;b->page_focus=CB_FOCUS_NONE;b->anchor_pending=b->anchor_needed=false;
    if(start_worker(b,&b->render,"cube-renderd")){close(fd);return -1;}
    if(*b->policy){struct cb_message policy={.type=CB_POLICY,.fd=-1};snprintf(policy.text,sizeof policy.text,"%s",b->policy);if(cb_send(b->render.socket,&policy)){close(fd);return -1;}}
    b->scripts_enabled=kind<CB_DOCUMENT_PNG&&cb_site_scripts(b->session,url,b->config.javascript)&&strncmp(url,"cube:",5);
    char *init=cb_broker_storage(b->session,url,b->policy,b->scripts_enabled&&!b->error);
    struct cb_message setup={.type=CB_WEB_INIT,.a=b->scripts_enabled&&!b->error&&!cb_policy_opaque(b->policy),.b=cb_policy_eval_allowed(b->policy),.c=b->config.images?0:1,.fd=cb_document_fd(init,strlen(init))};g_free(init);
    if(setup.fd<0||cb_send(b->render.socket,&setup)){if(setup.fd>=0)close(setup.fd);close(fd);return -1;}close(setup.fd);
    b->script_loading=true;b->script_busy=false;b->script_settle=now()+1;
    struct cb_message m={.type=CB_DOCUMENT,.b=kind,.fd=fd};snprintf(m.text,sizeof(m.text),"%s",url);
    int result=cb_send(b->render.socket,&m);close(fd);if(result)return -1;
    b->ready=true;b->desired_scroll=0;b->scroll=0;
    b->catalog_pending=true;b->resource_deadline=now()+20;
    if(!b->error&&strchr(url,'#'))b->anchor_needed=true;
    request_layout(b);return 0;
}
static int document(struct browser*b,int fd,const char*url){return document_kind(b,fd,url,CB_DOCUMENT_HTML);}
static void fail_page(struct browser*b,const char*reason){
    stop_worker(&b->net);b->error=true;b->busy=false;
    snprintf(b->status,sizeof(b->status),"%s",reason);
    char *safe=cb_html_escape(reason),*url=cb_html_escape(b->url);
    char *html=g_strdup_printf("<html><head><title>Unable to open page</title></head><body style='padding:52px;background:#fbfbf8;color:#20251f'><p>CUBE BROWSER</p><h1>This page could not be opened.</h1><p>%s</p><p>%s</p><p>Check the address and connection, then press Ctrl+R to retry.</p><p><a href='cube:home'>Return home</a></p></body></html>",safe,url);
    free(safe);free(url);int fd=cb_document_fd(html,strlen(html));g_free(html);
    if(fd>=0)document(b,fd,b->url);
    dirty(b);
}
static void remember_trail(struct browser*b,const char*url){
    if(b->trail_count&&b->trail_at>=0&&!strcmp(b->trail[b->trail_at],url))return;
    while(b->trail_count>b->trail_at+1)free(b->trail[--b->trail_count]);
    if(b->trail_count==128){free(b->trail[0]);memmove(b->trail,b->trail+1,127*sizeof(char*));b->trail_count--;}
    b->trail[b->trail_count++]=strdup(url);b->trail_at=b->trail_count-1;
}
static int read_local(int fd,char **html,size_t *size){
    struct stat st;
    if(fstat(fd,&st)||!S_ISREG(st.st_mode)||st.st_size<0||st.st_size>2*1024*1024)return -1;
    *size=(size_t)st.st_size;*html=malloc(*size+1);if(!*html)return -1;
    size_t offset=0;
    while(offset<*size){ssize_t n=pread(fd,*html+offset,*size-offset,offset);
        if(n<0&&errno==EINTR)continue;
        if(n<=0){free(*html);*html=NULL;return -1;}
        offset+=(size_t)n;}
    (*html)[*size]=0;return 0;
}
static unsigned local_document_kind(const char *bytes,size_t size,const char *url){
    if(size>=8&&!memcmp(bytes,"\211PNG\r\n\032\n",8))return CB_DOCUMENT_PNG;
    if(size>=3&&(unsigned char)bytes[0]==0xff&&(unsigned char)bytes[1]==0xd8&&(unsigned char)bytes[2]==0xff)return CB_DOCUMENT_JPEG;
    char *lower=g_ascii_strdown(url,-1);unsigned kind=CB_DOCUMENT_HTML;
    if(g_str_has_suffix(lower,".png"))kind=CB_DOCUMENT_PNG;
    else if(g_str_has_suffix(lower,".jpg")||g_str_has_suffix(lower,".jpeg"))kind=CB_DOCUMENT_JPEG;
    else if(g_str_has_suffix(lower,".svg"))kind=CB_DOCUMENT_SVG;
    g_free(lower);return kind;
}
static void navigate_request(struct browser*b,const char*input,bool history,bool from_page,const char *body,bool form){
    char url[4096];if(cb_url_input(input,b->config.search_engine,url,sizeof(url))){snprintf(b->status,sizeof(b->status),"Enter an HTTP(S) address, a local file, or a search.");dirty(b);return;}
    if(from_page&&cb_url_remote(b->url)&&!cb_url_remote(url)&&!(b->error&&!strcmp(url,"cube:home"))){
        snprintf(b->status,sizeof(b->status),"Web pages cannot open local files or browser pages.");dirty(b);return;}
    if(!strcmp(url,"cube:settings")){
        if(!*b->url)navigate_request(b,"cube:home",true,false,NULL,false);
        panel_open(b,1);return;
    }
    if(!strcmp(url,"cube:history-clear"))return;
    if(b->ready&&!b->error&&(strcmp(url,b->url)||(from_page&&strchr(url,'#')))&&cb_url_same_document(b->url,url)){
        blur_page(b);snprintf(b->url,sizeof(b->url),"%s",url);snprintf(b->address,sizeof(b->address),"%s",url);
        b->address_focus=b->find_focus=b->find_visible=false;b->notice[0]=0;
        b->bookmarked=cb_bookmark_contains(url)==1;
        if(history)remember_trail(b,url);
        b->anchor_needed=true;page_status(b);dirty(b);return;
    }
    char source[4096],form_policy[CB_POLICY_LIMIT];snprintf(source,sizeof source,"%s",from_page?b->url:url);snprintf(form_policy,sizeof form_policy,"%s",form?b->policy:"");
    reset_resources(b);stop_worker(&b->net);stop_worker(&b->render);free_frame(b);
    b->interaction_pending=0;b->page_focus=CB_FOCUS_NONE;b->anchor_pending=b->anchor_needed=false;b->notice[0]=0;
    b->ready=false;b->layout_pending=false;b->error=false;b->busy=true;b->history_saved=false;b->http_status=0;b->console_count=0;b->policy[0]=0;b->policy_received=false;
    b->panel=0;
    snprintf(b->url,sizeof(b->url),"%s",url);snprintf(b->address,sizeof(b->address),"%s",url);b->cursor=strlen(b->address);
    b->address_focus=false;b->select_all=false;b->find_visible=false;b->find_focus=false;b->desired_scroll=0;b->find_y=-1;
    snprintf(b->status,sizeof(b->status),"Opening %.500s",url);snprintf(b->title,sizeof(b->title),"Loading…");
    if(history)remember_trail(b,url);
    int fd=-1;unsigned document_type=CB_DOCUMENT_HTML;
    b->bookmarked=cb_bookmark_contains(url)==1;
    if(!strcmp(url,"cube:home"))fd=cb_document_fd(b->private_mode?cb_private_home:cb_home,strlen(b->private_mode?cb_private_home:cb_home));
    else if(!strcmp(url,"cube:about"))fd=cb_document_fd(cb_about,strlen(cb_about));
    else if(!strcmp(url,"cube:bookmarks")){char*h=cb_bookmarks_html();if(h){fd=cb_document_fd(h,strlen(h));free(h);}}
    else if(!strcmp(url,"cube:history")){char*h=b->private_mode?strdup(cb_private_history):cb_history_html();if(h){fd=cb_document_fd(h,strlen(h));free(h);}}
    else if(g_str_has_prefix(url,"file:///")){
        char *path=g_filename_from_uri(url,NULL,NULL);int file=path?open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK):-1;g_free(path);
        char *html=NULL;size_t size=0;
        if(file<0||read_local(file,&html,&size)){
            if(file>=0)close(file);
            fail_page(b,"Cannot read this local document (maximum 2 MiB; symlinks are not followed).");return;}
        close(file);document_type=local_document_kind(html,size,url);fd=cb_document_fd(html,size);free(html);
    }else if(cb_url_remote(url)){
        if(b->config.https_only&&!g_str_has_prefix(url,"https://")){fail_page(b,"HTTPS-only mode blocked this HTTP address. Change it in Settings > Advanced to allow HTTP.");return;}
        struct cb_http_job *job=cb_broker_new(url,source,form?source:NULL,form_policy,form?CB_POLICY_FORM:0,true,body);
        if(network_start(b,&b->net,job)){fail_page(b,"Could not start the network request.");return;}
        dirty(b);return;
    }
    if(fd<0||document_kind(b,fd,url,document_type))fail_page(b,"Could not prepare this document.");
    dirty(b);
}
static void navigate(struct browser*b,const char*input,bool history,bool from_page){navigate_request(b,input,history,from_page,NULL,false);}
static void go(struct browser*b,int direction){int i=b->trail_at+direction;if(i<0||i>=b->trail_count)return;b->trail_at=i;char url[4096];snprintf(url,sizeof(url),"%s",b->trail[i]);navigate(b,url,false,false);}
static void new_window(struct browser*b,bool private_mode){
    pid_t p=fork();if(p==0){if(fork()!=0)_exit(0);setsid();close_range(3,~0U,0);
        if(private_mode)execl(b->executable,"cube-browser","--private","cube:home",(char*)NULL);
        else execl(b->executable,"cube-browser","cube:home",(char*)NULL);
        _exit(127);}
    if(p>0)while(waitpid(p,NULL,0)<0&&errno==EINTR){}
}
static void new_tab(struct browser*b){new_window(b,b->private_mode);}
static void bookmark_toggle(struct browser *b){
    if(b->private_mode){notice(b,"Bookmarks are read-only in a private window.");return;}
    if(!cb_url_remote(b->url)&&!g_str_has_prefix(b->url,"file:///")){notice(b,"Open a website or local document to bookmark it.");return;}
    int exists=cb_bookmark_contains(b->url);
    if(exists<0){notice(b,"Could not read bookmarks.");return;}
    int result=exists?cb_bookmark_remove(b->url,false):cb_bookmark_add(b->url,b->title,false);
    if(result){notice(b,"Could not save bookmark.");return;}
    b->bookmarked=!exists;notice(b,exists?"Bookmark removed":"Page bookmarked · Ctrl+Shift+B opens bookmarks");
}
static void find_next(struct browser*b){if(!*b->find||!b->ready||b->render.socket<0)return;
    struct cb_message m={.type=CB_FIND,.fd=-1,.a=(uint32_t)fmax(0,b->find_y+1)};snprintf(m.text,sizeof(m.text),"%s",b->find);cb_send(b->render.socket,&m);}
static bool worker_readable(const struct worker *w){
    if(w->socket<0)return false;
    struct pollfd p={w->socket,POLLIN,0};return poll(&p,1,0)>0;
}
static void page_navigate(struct browser *b,const char *url){
    if(!strcmp(url,"cube:history-clear")&&!strcmp(b->url,"cube:history")){
        if(b->private_mode){notice(b,"Private windows never access saved history.");return;}
        if(cb_history_clear())notice(b,"Could not clear history.");else navigate(b,"cube:history",false,false);
    }else navigate(b,url,true,true);
}
static void interaction_result(struct browser *b,const struct cb_message *m){
    if((m->fd>=0&&m->a!=CB_INTERACTION_POST)||!b->interaction_pending||m->a>CB_INTERACTION_POST||m->b>CB_FOCUS_CONTROL||
       (m->c!=UINT32_MAX&&m->c>10000000)||strlen(m->text)>=4096){
        renderer_failed(b,"Invalid interaction response. Reload to try again.");return;
    }
    unsigned permissions=b->interaction_permissions[b->interaction_head];
    if((m->a==CB_INTERACTION_NAVIGATE&&!(permissions&1))||
       ((m->a==CB_INTERACTION_SUBMIT||m->a==CB_INTERACTION_POST)&&!(permissions&2))||
       (m->a==CB_INTERACTION_COPY&&!(permissions&4))){
        renderer_failed(b,"Renderer returned an action without a matching user input.");return;
    }
    b->interaction_head=(b->interaction_head+1)%128;b->interaction_pending--;b->page_focus=m->b;
    if(m->a==CB_INTERACTION_NAVIGATE){if(*m->text)page_navigate(b,m->text);return;}
    if(m->a==CB_INTERACTION_SUBMIT||m->a==CB_INTERACTION_POST){
        char url[4096];
        if(!cb_url_remote(b->url)||!cb_url_remote(m->text)||
           cb_url_input(m->text,b->config.search_engine,url,sizeof(url))||
           (!g_ascii_strncasecmp(b->url,"https:",6)&&g_ascii_strncasecmp(url,"https:",6))){
            notice(b,"Form submission refused: a valid HTTP(S) destination without credentials is required; HTTPS cannot downgrade.");return;
        }
        if(!cb_policy_allows(b->policy,b->url,url,CB_POLICY_FORM,0)){notice(b,"Content Security Policy blocked this form submission.");return;}
        /* An explicit submit reloads even if its query matches the current URL. */
        char *body=NULL;size_t size=0;
        if(m->a==CB_INTERACTION_POST&&(m->fd<0||cb_read_document(m->fd,&body,&size)||size>CB_POST_LIMIT||strlen(body)!=size)){
            free(body);renderer_failed(b,"Invalid POST body from renderer.");return;
        }
        if(body)for(size_t i=0;i<size;i++)if((unsigned char)body[i]<32||(unsigned char)body[i]>126){free(body);renderer_failed(b,"Invalid POST encoding.");return;}
        b->ready=false;navigate_request(b,url,true,true,body,true);if(body){memset(body,0,size);free(body);}return;
    }
    if(m->a==CB_INTERACTION_NOTICE)notice(b,m->text);
    else if(m->a==CB_INTERACTION_COPY)cb_window_copy(b->window,m->text);
    else if(m->a==CB_INTERACTION_UPDATE){b->notice[0]=0;page_status(b);}
    if(m->a!=CB_INTERACTION_NONE){
        if(m->b&&m->c!=UINT32_MAX&&!b->address_focus&&!b->find_focus){
            if(m->c<b->desired_scroll||m->c>b->desired_scroll+b->view_h-60*b->scale)
                b->desired_scroll=fmax(0,m->c-40*b->scale);
        }
        request_layout(b);
    }
}
static void resource_catalog_entry(struct browser *b,const struct cb_message *m){
    if(m->fd>=0||!b->catalog_pending||m->a!=b->resource_count||m->a>=CB_MAX_RESOURCES||m->b<1||m->b>3){
        renderer_failed(b,"Invalid page resource list. Reload to try again.");return;
    }
    struct resource *r=&b->resources[b->resource_count++];r->kind=m->b;r->worker.socket=-1;
    if(!cb_url_remote(b->url)||!cb_url_remote(m->text)||
       cb_url_input(m->text,b->config.search_engine,r->url,sizeof(r->url))||
       (!b->scripts_enabled&&m->b==3)||(!b->config.images&&m->b==2)||!cb_policy_allows(b->policy,b->url,r->url,m->b,0))r->state=RESOURCE_FAILED;
    else r->state=RESOURCE_QUEUED;
}
static void resources_pump(struct browser *b){
    if(now()>=b->resource_deadline){
        bool awaiting_renderer=b->catalog_pending,timed_out=false;
        for(unsigned i=0;i<b->resource_count;i++){
            struct resource *r=&b->resources[i];
            awaiting_renderer|=r->state==RESOURCE_APPLYING;
            if(r->state==RESOURCE_QUEUED||r->state==RESOURCE_FETCHING){
                if(r->state==RESOURCE_FETCHING)stop_worker(&r->worker);
                r->state=RESOURCE_FAILED;timed_out=true;
            }
        }
        if(awaiting_renderer){renderer_failed(b,"Page rendering exceeded its time limit. Reload to try again.");return;}
        if(timed_out){page_status(b);dirty(b);}return;
    }
    for(unsigned i=0;i<b->resource_count;i++){
        struct resource *r=&b->resources[i];
        if(r->state!=RESOURCE_FETCHING||!worker_readable(&r->worker))continue;
        struct cb_message m;int received=network_read(b,&r->worker,&m);if(!received&&r->worker.socket>=0)continue;stop_worker(&r->worker);
        r->state=RESOURCE_FAILED;
        if(received==1){
            char *mime=strchr(m.text,'\n');struct stat st;
            if(m.type==CB_DOCUMENT&&m.fd>=0&&m.b==r->kind&&m.a>=200&&m.a<300&&mime&&
               strlen(mime+1)<128&&!fstat(m.fd,&st)&&st.st_size>=0&&st.st_size<=2*1024*1024&&
               (size_t)st.st_size<=16u*1024u*1024u-b->resource_bytes){
                struct cb_message data={.type=CB_RESOURCE_DATA,.fd=m.fd,.a=i,.b=r->kind};
                snprintf(data.text,sizeof(data.text),"%s",mime+1);
                if(b->render.socket>=0&&!cb_send(b->render.socket,&data)){
                    r->state=RESOURCE_APPLYING;b->resource_bytes+=(size_t)st.st_size;
                }
            }
            if(m.fd>=0)close(m.fd);
        }
        page_status(b);dirty(b);
    }
    unsigned active=0;
    bool launch_failed=false;
    for(unsigned i=0;i<b->resource_count;i++)active+=b->resources[i].state==RESOURCE_FETCHING;
    for(unsigned i=0;i<b->resource_count&&active<3;i++){
        struct resource *r=&b->resources[i];if(r->state!=RESOURCE_QUEUED)continue;
        if(b->resource_bytes>=16u*1024u*1024u||network_start(b,&r->worker,cb_broker_new(r->url,b->url,b->url,b->policy,r->kind,false,NULL))){r->state=RESOURCE_FAILED;launch_failed=true;continue;}
        r->state=RESOURCE_FETCHING;active++;

    }
    for(unsigned i=0;i<b->resource_count;i++){struct resource *r=&b->resources[i];
        if(r->kind==3&&r->state==RESOURCE_FAILED&&!r->notified&&b->render.socket>=0){struct cb_message failed={.type=CB_RESOURCE_DATA,.a=i,.b=3,.c=1,.fd=-1};
            if(!cb_send(b->render.socket,&failed)){r->state=RESOURCE_APPLYING;r->notified=true;}}
    }
    if(launch_failed){page_status(b);dirty(b);}
}
static void console_add(struct browser *b,const char *message){
    if(b->print_console)fprintf(stderr,"console: %s\n",message);
    unsigned at=b->console_count++%128;utf8_copy(b->console[at],sizeof b->console[at],message);
}
static void web_request(struct browser *b,const struct cb_message *m){
    if(m->fd>=0||!m->a||m->b>1||m->c||!b->scripts_enabled){renderer_failed(b,"Invalid script request.");return;}
    unsigned slot=32;for(unsigned i=0;i<32;i++){if(b->web[i].state&&b->web[i].id==m->a){renderer_failed(b,"Duplicate script request.");return;}if(!b->web[i].state)slot=i;}
    char url[4096];if(slot==32||!cb_url_remote(b->url)||cb_url_input(m->text,b->config.search_engine,url,sizeof url)||!cb_policy_allows(b->policy,b->url,url,CB_POLICY_CONNECT,0)){
        struct cb_message result={.type=CB_WEB_RESPONSE,.a=m->a,.fd=-1};cb_send(b->render.socket,&result);return;
    }
    b->web[slot]=(struct web_request){.worker={.socket=-1},.state=1,.id=m->a,.credentials=m->b};strcpy(b->web[slot].url,url);
}
static void web_pump(struct browser *b){
    unsigned active=0;
    for(unsigned i=0;i<32;i++){
        struct web_request *r=&b->web[i];if(r->state!=2)continue;
        if(!worker_readable(&r->worker)){active++;continue;}
        struct cb_message m={.fd=-1};int result=network_read(b,&r->worker,&m);if(!result){active++;continue;}
        struct cb_message out={.type=CB_WEB_RESPONSE,.a=r->id,.fd=-1};
        if(result==1&&m.type==CB_DOCUMENT&&m.fd>=0){out.fd=m.fd;out.b=m.a;char *mime=strchr(m.text,'\n');if(mime)*mime=0;snprintf(out.text,sizeof out.text,"%.4095s",m.text);}
        else{if(m.fd>=0)close(m.fd);console_add(b,result==1?m.text:"Fetch worker stopped");}
        stop_worker(&r->worker);r->state=0;
        if(b->render.socket>=0)cb_send(b->render.socket,&out);
        if(out.fd>=0)close(out.fd);
    }
    for(unsigned i=0;i<32&&active<3;i++){struct web_request *r=&b->web[i];if(r->state!=1)continue;
        struct cb_http_job *job=cb_broker_new(r->url,b->url,b->url,b->policy,4,false,NULL);if(job)job->credentials=r->credentials;
        if(network_start(b,&r->worker,job)){struct cb_message out={.type=CB_WEB_RESPONSE,.a=r->id,.fd=-1};cb_send(b->render.socket,&out);r->state=0;}
        else{r->state=2;active++;}
    }
}
static void storage_write(struct browser *b,const struct cb_message *m){
    char *data=NULL;size_t size=0;
    if(m->fd<0||m->a||m->b||m->c||*m->text||cb_read_document(m->fd,&data,&size)||size!=sizeof(struct cb_storage_write)){free(data);renderer_failed(b,"Invalid storage request.");return;}
    if(!cb_url_remote(b->url)||!b->scripts_enabled||b->error||cb_policy_opaque(b->policy)){free(data);console_add(b,"This document cannot access origin storage");return;}
    const struct cb_storage_write *w=(const struct cb_storage_write *)data;
    int valid=w->version==1&&w->local<=1&&w->operation<=2&&memchr(w->key,0,sizeof w->key)&&memchr(w->value,0,sizeof w->value);
    if(!valid){free(data);renderer_failed(b,"Invalid storage payload.");return;}
    int failed=w->operation==2?cb_site_clear(b->session,b->url,w->local):cb_site_set(b->session,b->url,w->local,w->key,w->operation==1?NULL:w->value);
    if(failed)console_add(b,"Origin storage write failed or quota exceeded");
    free(data);
}
static void download_start(struct browser *b){
    if(b->download_state==1){b->panel=3;dirty(b);return;}
    if(!cb_url_remote(b->url)){notice(b,"Downloads require an HTTP(S) page address.");return;}
    struct cb_http_job *job=cb_broker_new(b->url,b->url,NULL,NULL,0,true,NULL);if(job)job->request.download=1;
    b->download_received=b->download_total=0;b->download_path[0]=0;
    if(network_start(b,&b->download,job)){b->download_state=3;strcpy(b->download_status,"Could not start download");}
    else{b->download_state=1;strcpy(b->download_status,"Downloading…");}
    b->panel=3;dirty(b);
}
static void download_cancel(struct browser *b){if(b->download_state==1){stop_worker(&b->download);b->download_state=4;strcpy(b->download_status,"Download cancelled");dirty(b);}}
static void download_pump(struct browser *b){
    if(b->download_state!=1||!worker_readable(&b->download))return;
    struct cb_message m={.fd=-1};int result=network_read(b,&b->download,&m);
    if(!result){if(b->download.job){b->download_received=b->download.job->received;b->download_total=b->download.job->total;}dirty(b);return;}
    b->download_state=3;
    if(result==1&&m.type==CB_DOCUMENT&&m.fd>=0&&m.a>=200&&m.a<300&&b->download.job){
        if(!cb_download_save_to(m.fd,b->download.job->request.url,b->download.job->meta.disposition,b->config.download_directory,b->download_path,sizeof b->download_path,b->download_status,sizeof b->download_status)){
            b->download_state=2;strcpy(b->download_status,"Download saved");}
    }else snprintf(b->download_status,sizeof b->download_status,"%.255s",result==1&&m.type==CB_ERROR?m.text:"Download failed");
    if(m.fd>=0)close(m.fd);
    stop_worker(&b->download);dirty(b);
}
enum setting {
    SET_HOME,SET_SEARCH,SET_ZOOM,SET_USE_HOME,SET_HISTORY,SET_BOOKMARKS,SET_PRIVATE,
    SET_IMAGES,SET_JS,SET_SITE_JS,
    SET_COOKIES,SET_STORAGE,SET_CACHE,SET_SAVE_HISTORY,SET_CLEAR_EXIT,SET_CLEAR,
    SET_DOWNLOAD_DIRECTORY,SET_DOWNLOADS,
    SET_HTTPS_ONLY,SET_RELOAD,SET_RESET,SET_DIAGNOSTICS
};
static const struct {const char *name;unsigned first,count;} settings_categories[]={
    {"General",SET_HOME,7},{"Content",SET_IMAGES,3},{"Privacy",SET_COOKIES,6},
    {"Downloads",SET_DOWNLOAD_DIRECTORY,2},{"Advanced",SET_HTTPS_ONLY,4}
};
static const struct {const char *name,*help;} settings_info[]={
    {"Home page","Enter an HTTP(S), file:/// or cube: address."},
    {"Search engine","Use an HTTP(S) search URL containing exactly one %s."},
    {"Default page zoom","Enter a percentage from 50 to 300. Ctrl+0 resets to 100%."},
    {"Use this page as home","Set the home button and startup page to the current address."},
    {"Browsing history","Open history saved on this device."},
    {"Bookmarks","Open saved bookmarks. Ctrl+D bookmarks the current page."},
    {"New private window","Start a separate window with temporary browsing data."},
    {"Load images","Allow PNG, JPEG and static SVG, including inline images."},
    {"JavaScript by default","Site exceptions take precedence over this default."},
    {"JavaScript for this site","Save a script exception for the current HTTP(S) origin."},
    {"Allow cookies","Third-party cookies are always blocked. Off blocks all cookies."},
    {"Allow site storage","Allow localStorage and sessionStorage. Off retains saved data."},
    {"Use HTTP cache","Keep validated responses in memory. Off discards this cache."},
    {"Save browsing history","Off stops new saved visits; existing history remains."},
    {"Clear browsing data on exit","Clear saved history, cookies, storage and site choices when this window closes."},
    {"Clear browsing data now","Clear history, cookies, storage and site choices; keep bookmarks and downloads."},
    {"Download folder","Enter an absolute folder path, or leave empty for the default Downloads folder."},
    {"Download activity","View progress, cancel a download or copy its saved path."},
    {"HTTPS-only mode","Block HTTP pages, resources, redirects, fetch and downloads."},
    {"Reload configuration from disk","Read your cube-browser/config file and apply validated settings."},
    {"Restore default settings","Reset browser preferences; keep bookmarks and site choices."},
    {"Console and network","Inspect script messages and network requests for this window."}
};
static unsigned setting_id(const struct browser *b){return settings_categories[b->settings_category].first+b->panel_selected;}
static unsigned setting_count(const struct browser *b){return settings_categories[b->settings_category].count;}
static void panel_open(struct browser *b,unsigned panel){
    blur_page(b);b->address_focus=b->find_focus=false;b->panel=panel;
    b->panel_selected=b->panel_scroll=b->settings_category=0;b->settings_editing=false;b->settings_error[0]=0;dirty(b);
}
static void settings_reload_page(struct browser *b){
    unsigned category=b->settings_category,selected=b->panel_selected;
    char url[4096];snprintf(url,sizeof url,"%s",b->url);navigate(b,url,false,false);
    b->panel=1;b->settings_category=category;b->panel_selected=selected;
}
static bool apply_preferences(struct browser *b,const struct cb_config *config,bool reload){
    char error[256];
    if(cb_config_validate(config,error,sizeof error)||(!b->private_mode&&cb_config_save(config,error,sizeof error))){
        snprintf(b->settings_error,sizeof b->settings_error,"%s",error);notice(b,error);return false;
    }
    b->config=*config;b->zoom=config->zoom;cb_session_configure(b->session,config);b->settings_error[0]=0;
    if(config->https_only&&b->download.job&&!g_str_has_prefix(b->download.job->request.url,"https://"))download_cancel(b);
    if(reload)settings_reload_page(b);else request_layout(b);
    notice(b,b->private_mode?"Settings applied to this private window":"Settings saved");return true;
}
static void setting_value(struct browser *b,unsigned id,char *value,size_t capacity){
    const char *v="";bool enabled=false,is_bool=true;
    switch(id){
    case SET_HOME:v=b->config.homepage;is_bool=false;break;
    case SET_SEARCH:v=b->config.search_engine;is_bool=false;break;
    case SET_ZOOM:snprintf(value,capacity,"%.0f%%",b->zoom*100);return;
    case SET_DOWNLOAD_DIRECTORY:v=*b->config.download_directory?b->config.download_directory:"Default Downloads folder";is_bool=false;break;
    case SET_IMAGES:enabled=b->config.images;break;case SET_JS:enabled=b->config.javascript;break;
    case SET_SITE_JS:if(!cb_url_remote(b->url)){v="Open a website first";is_bool=false;}else enabled=cb_site_scripts(b->session,b->url,b->config.javascript);break;
    case SET_COOKIES:enabled=b->config.cookies;break;case SET_STORAGE:enabled=b->config.site_storage;break;
    case SET_CACHE:enabled=b->config.cache;break;case SET_SAVE_HISTORY:enabled=b->config.save_history;break;
    case SET_CLEAR_EXIT:enabled=b->config.clear_on_exit;break;case SET_HTTPS_ONLY:enabled=b->config.https_only;break;
    default:v="Enter to open";is_bool=false;break;
    }
    snprintf(value,capacity,"%s",is_bool?(enabled?"On":"Off"):v);
}
static void settings_edit_commit(struct browser *b){
    struct cb_config next=b->config;unsigned id=setting_id(b);
    char *destination=NULL;size_t capacity=0;
    if(id==SET_HOME){destination=next.homepage;capacity=sizeof next.homepage;}
    else if(id==SET_SEARCH){destination=next.search_engine;capacity=sizeof next.search_engine;}
    else if(id==SET_DOWNLOAD_DIRECTORY){destination=next.download_directory;capacity=sizeof next.download_directory;}
    else if(id==SET_ZOOM){
        char *end;double percent=strtod(b->settings_text,&end);if(*end=='%')end++;
        if(end==b->settings_text||*end||!isfinite(percent)||percent<50||percent>300){snprintf(b->settings_error,sizeof b->settings_error,"Zoom must be a percentage from 50 to 300.");return;}
        next.zoom=percent/100.;
    }
    if(destination){
        if(strlen(b->settings_text)>=capacity){snprintf(b->settings_error,sizeof b->settings_error,"This value is too long.");return;}
        strcpy(destination,b->settings_text);
    }
    if(apply_preferences(b,&next,false))b->settings_editing=false;
}
static void panel_activate(struct browser *b){
    unsigned id=setting_id(b);struct cb_config next=b->config;bool reload=true;
    b->settings_error[0]=0;
    if(id==SET_HOME||id==SET_SEARCH||id==SET_ZOOM||id==SET_DOWNLOAD_DIRECTORY){
        if(id==SET_DOWNLOAD_DIRECTORY)snprintf(b->settings_text,sizeof b->settings_text,"%s",b->config.download_directory);
        else setting_value(b,id,b->settings_text,sizeof b->settings_text);
        b->settings_editing=b->settings_select_all=true;b->settings_cursor=strlen(b->settings_text);
    }else if(id==SET_USE_HOME){snprintf(next.homepage,sizeof next.homepage,"%s",b->url);apply_preferences(b,&next,false);}
    else if(id==SET_HISTORY)navigate(b,"cube:history",true,false);
    else if(id==SET_BOOKMARKS)navigate(b,"cube:bookmarks",true,false);
    else if(id==SET_PRIVATE)new_window(b,true);
    else if(id==SET_SITE_JS){
        if(!cb_url_remote(b->url))snprintf(b->settings_error,sizeof b->settings_error,"Site settings require an HTTP(S) page.");
        else if(cb_site_scripts_set(b->session,b->url,!cb_site_scripts(b->session,b->url,b->config.javascript)))snprintf(b->settings_error,sizeof b->settings_error,"Could not save site setting.");
        else settings_reload_page(b);
    }else if(id==SET_CLEAR){
        download_cancel(b);cb_session_clear(b->session);int failed=!b->private_mode&&cb_history_clear();
        for(int i=0;i<b->trail_count;i++)free(b->trail[i]);
        b->trail_count=0;b->trail_at=-1;b->console_count=b->network_count=0;
        /* Return to a local page so clearing cannot immediately recreate site data. */
        navigate(b,"cube:home",true,false);notice(b,failed?"Site data cleared, but history could not be cleared":"Browsing data cleared");
    }else if(id==SET_DOWNLOADS)panel_open(b,3);
    else if(id==SET_DIAGNOSTICS)panel_open(b,2);
    else if(id==SET_RELOAD){
        char error[256];if(cb_config_load(&next,error,sizeof error))snprintf(b->settings_error,sizeof b->settings_error,"%s",error);
        else apply_preferences(b,&next,true);
    }else if(id==SET_RESET){cb_config_defaults(&next);apply_preferences(b,&next,true);}
    else{
        switch(id){
        case SET_IMAGES:next.images=!next.images;break;case SET_JS:next.javascript=!next.javascript;break;
        case SET_COOKIES:next.cookies=!next.cookies;break;case SET_STORAGE:next.site_storage=!next.site_storage;break;
        case SET_CACHE:next.cache=!next.cache;break;case SET_SAVE_HISTORY:next.save_history=!next.save_history;break;
        case SET_CLEAR_EXIT:next.clear_on_exit=!next.clear_on_exit;reload=false;break;
        case SET_HTTPS_ONLY:next.https_only=!next.https_only;break;default:return;
        }
        apply_preferences(b,&next,reload);
    }
    dirty(b);
}
static void check_worker(struct browser*b,bool network){
    struct worker*w=network?&b->net:&b->render;if(w->socket<0)return;
    struct pollfd p={w->socket,POLLIN,0};if(poll(&p,1,0)<=0)return;
    struct cb_message m;int n=network?network_read(b,w,&m):cb_recv(w->socket,&m);
    if(network&&n==0&&w->socket>=0)return;
    if(n!=1){stop_worker(w);if(network)fail_page(b,"The network worker stopped before returning a response.");else{
        renderer_failed(b,"Renderer stopped. Press Ctrl+R to reload.");}return;}
    if(network&&m.type==CB_POLICY){
        if(m.fd>=0||m.a||m.b||m.c||b->policy_received||strlen(m.text)>=sizeof b->policy){if(m.fd>=0)close(m.fd);fail_page(b,"Invalid response policy from network worker.");return;}
        strcpy(b->policy,m.text);b->policy_received=true;return;
    }
    if(network){
        stop_worker(w);
        if(m.type==CB_ERROR)fail_page(b,m.text);
        else if(m.type==CB_DOCUMENT&&m.fd>=0&&m.a>=200&&m.a<=599&&m.b<=CB_DOCUMENT_SVG&&cb_url_remote(m.text)){
            char checked[4096];if(cb_url_input(m.text,b->config.search_engine,checked,sizeof checked)){close(m.fd);fail_page(b,"Invalid response URL.");return;}
            b->http_status=m.a;
            if(m.b==CB_DOCUMENT_TEXT){char *text=NULL;size_t size;if(cb_read_document(m.fd,&text,&size)==0){char *escaped=cb_html_escape(text);char *html=g_strdup_printf("<html><body><pre>%s</pre></body></html>",escaped);free(text);free(escaped);close(m.fd);m.fd=cb_document_fd(html,strlen(html));g_free(html);}}
            snprintf(b->url,sizeof(b->url),"%.4095s",m.text);
            b->bookmarked=cb_bookmark_contains(b->url)==1;
            if(!b->address_focus)snprintf(b->address,sizeof(b->address),"%s",b->url);
            if(b->trail_at>=0){free(b->trail[b->trail_at]);b->trail[b->trail_at]=strdup(b->url);}
            int fd=m.fd;m.fd=-1;if(fd<0||document_kind(b,fd,b->url,m.b))fail_page(b,"Could not send the response to the renderer.");
        }else fail_page(b,"Invalid response from network worker.");
    }else if(m.type==CB_WEB_REQUEST){web_request(b,&m);}
    else if(m.type==CB_STORAGE_WRITE){storage_write(b,&m);}
    else if(m.type==CB_CONSOLE){if(m.fd<0&&m.a<=2&&!m.b&&!m.c)console_add(b,m.text);else renderer_failed(b,"Invalid console message.");}
    else if(m.type==CB_WEB_STATE){if(m.fd<0&&m.a<=1&&m.b<=1&&!m.c){if(b->script_loading&&m.b)b->script_settle=now()+1;b->script_busy=m.a;b->script_loading=!m.b;}else renderer_failed(b,"Invalid script status.");}
    else if(m.type==CB_WEB_DIRTY){if(m.fd<0&&!m.a&&!m.b&&!m.c)request_layout(b);else renderer_failed(b,"Invalid script repaint request.");}
    else if(m.type==CB_RESOURCE_REQUEST){resource_catalog_entry(b,&m);}
    else if(m.type==CB_RESOURCES_END){
        if(m.fd>=0||!b->catalog_pending||m.a!=b->resource_count){
            renderer_failed(b,"Incomplete page resource list.");
        }else{b->catalog_pending=false;page_status(b);}
    }else if(m.type==CB_RESOURCE_DONE){
        if(m.fd<0&&m.c==0&&m.b<=1&&m.a<b->resource_count&&b->resources[m.a].state==RESOURCE_APPLYING){
            b->resources[m.a].state=m.b==1?RESOURCE_DONE:RESOURCE_FAILED;
            if(m.b==1){b->find_y=-1;request_layout(b);}
            page_status(b);
        }else renderer_failed(b,"Invalid page resource acknowledgement.");
    }else if(m.type==CB_FRAME&&m.fd>=0){
        size_t size=(size_t)m.a*m.b*4;struct stat st;
        int seals=fcntl(m.fd,F_GET_SEALS),required=F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_WRITE|F_SEAL_SEAL;
        char *end=NULL;double frame_scroll=strtod(m.text,&end);
        double maximum=m.c>m.b?(double)m.c-m.b:0;
        if(!b->layout_pending||m.a<1||m.b<1||m.a>4096||m.b>4096||
           m.a!=(uint32_t)b->layout_w||m.b!=(uint32_t)b->layout_h||
           fstat(m.fd,&st)||!S_ISREG(st.st_mode)||(size_t)st.st_size!=size||
           seals<0||(seals&required)!=required||!isfinite(frame_scroll)||frame_scroll<0||
           frame_scroll>maximum+1||end==m.text||!end||*end!='\n'){
            renderer_failed(b,"Invalid frame from renderer.");}
        else{
            void *pixels=mmap(NULL,size,PROT_READ,MAP_SHARED,m.fd,0);
            if(pixels!=MAP_FAILED){free_frame(b);b->pixels=pixels;b->pixels_size=size;b->frame_w=m.a;b->frame_h=m.b;
                b->frame=cairo_image_surface_create_for_data(pixels,CAIRO_FORMAT_ARGB32,m.a,m.b,m.a*4);
                b->page_h=m.c;b->scroll=fmin(frame_scroll,maximum);
                utf8_copy(b->title,sizeof(b->title),end+1);
                cb_window_title(b->window,b->title);
                if(!b->history_saved&&!b->error&&!b->private_mode&&b->config.save_history&&cb_url_remote(b->url)){cb_history_visit(b->url,b->title);b->history_saved=true;}
                page_status(b);
            }else renderer_failed(b,"Could not map the rendered page.");
        }
        b->busy=false;b->layout_pending=false;
        if(b->relayout)request_layout(b);else b->desired_scroll=b->scroll;
    }else if(m.type==CB_INTERACTION){interaction_result(b,&m);
    }else if(m.type==CB_ANCHOR_RESULT){
        if(m.fd>=0||!b->anchor_pending||m.a>1||m.b>10000000||m.c){renderer_failed(b,"Invalid anchor response.");}
        else{
            b->anchor_pending=false;
            if(!b->anchor_needed){
                if(m.a){b->desired_scroll=m.b;request_layout(b);}
                else notice(b,"This page has no matching anchor.");
            }
        }
    }else if(m.type==CB_LINK_RESULT){
        renderer_failed(b,"Renderer returned an unsolicited navigation.");
    }else if(m.type==CB_FOUND){
        if(m.a){b->find_y=m.b;b->desired_scroll=m.b;b->find_wrapping=false;request_layout(b);snprintf(b->status,sizeof(b->status),"Match found · Enter for next");}
        else if(b->find_y>=0&&!b->find_wrapping){b->find_y=-1;b->find_wrapping=true;find_next(b);}
        else{b->find_wrapping=false;snprintf(b->status,sizeof(b->status),"No match for “%.200s”",b->find);}}
    else if(m.type==CB_ERROR){renderer_failed(b,m.text);}
    if(m.fd>=0)close(m.fd);
    dirty(b);
}
static void tick(void*data){struct browser*b=data;check_worker(b,true);
    for(unsigned i=0;i<64&&worker_readable(&b->render);i++)check_worker(b,false);
    resources_pump(b);
    web_pump(b);
    download_pump(b);
    anchor_pump(b);
    if(*b->notice&&now()>=b->notice_until){b->notice[0]=0;page_status(b);dirty(b);}
    if(now()-b->last_preferences>=1){b->last_preferences=now();struct cb_accessibility acc;cb_accessibility_load(&acc);
        if(acc.ui_scale!=b->acc.ui_scale||acc.strong_contrast!=b->acc.strong_contrast||acc.reduced_motion!=b->acc.reduced_motion){b->acc=acc;b->scale=acc.ui_scale;
            geometry(b,b->w,b->h);request_layout(b);dirty(b);}}
}
static void color(cairo_t*c,unsigned rgb){cairo_set_source_rgb(c,(rgb>>16)/255.,((rgb>>8)&255)/255.,(rgb&255)/255.);}
static void box(cairo_t*c,double x,double y,double w,double h,unsigned rgb){color(c,rgb);cairo_rectangle(c,x,y,w,h);cairo_fill(c);}
static void text(cairo_t*c,double x,double y,const char*t,double size,unsigned rgb,double width,bool bold){
    PangoLayout*l=pango_cairo_create_layout(c);PangoFontDescription*f=pango_font_description_new();pango_font_description_set_family(f,"sans-serif");pango_font_description_set_absolute_size(f,size*PANGO_SCALE);pango_font_description_set_weight(f,bold?PANGO_WEIGHT_BOLD:PANGO_WEIGHT_NORMAL);
    pango_layout_set_font_description(l,f);pango_layout_set_text(l,t,-1);if(width>0){pango_layout_set_width(l,width*PANGO_SCALE);pango_layout_set_ellipsize(l,PANGO_ELLIPSIZE_END);}pango_layout_set_single_paragraph_mode(l,TRUE);
    color(c,rgb);cairo_move_to(c,x,y);pango_cairo_show_layout(c,l);g_object_unref(l);pango_font_description_free(f);
}
static void cube(cairo_t*c,double x,double y,double s,unsigned rgb){color(c,rgb);cairo_set_line_width(c,1.4);cairo_move_to(c,x,y+s*.25);cairo_line_to(c,x+s*.5,y);cairo_line_to(c,x+s,y+s*.25);cairo_line_to(c,x+s,y+s*.8);cairo_line_to(c,x+s*.5,y+s);cairo_line_to(c,x,y+s*.8);cairo_close_path(c);cairo_move_to(c,x,y+s*.25);cairo_line_to(c,x+s*.5,y+s*.48);cairo_line_to(c,x+s,y+s*.25);cairo_move_to(c,x+s*.5,y+s*.48);cairo_line_to(c,x+s*.5,y+s);cairo_stroke(c);}
/* Fit settings editing and its help text even with large desktop scaling. */
static double panel_scale(struct browser *b){return fmin(b->scale,fmax(.5,(b->view_h-16)/320.));}
static void panel_geometry(struct browser *b,double *x,double *y,double *w,double *h){double s=panel_scale(b);*w=fmax(220,fmin(680*s,b->w-32*s));*h=fmax(150,fmin(520*s,b->view_h-16*s));*x=fmax(0,b->w-*w-16*s);*y=b->view_y+8*s;}
static unsigned settings_visible(double height,double scale){return (unsigned)fmax(1,floor((height-160*scale)/(44*scale)));}
static void draw_panel(struct browser *b,cairo_t *c){
    if(!b->panel)return;
    double x,y,w,h,s=panel_scale(b);panel_geometry(b,&x,&y,&w,&h);
    box(c,x+4*s,y+4*s,w,h,0x555555);box(c,x,y,w,h,0xf0f0f0);
    const char *title=b->panel==1?"Browser settings":b->panel==2?b->network_panel?"Network activity":"Page console":"Downloads";
    text(c,x+18*s,y+16*s,title,18*s,0x111111,w-36*s,true);
    if(b->panel==1){
        double tab=(w-20*s)/5;
        for(unsigned i=0;i<5;i++){
            if(i==b->settings_category)box(c,x+10*s+i*tab,y+47*s,tab,28*s,0xd5e4db);
            text(c,x+15*s+i*tab,y+55*s,settings_categories[i].name,11*s,0x111111,tab-10*s,i==b->settings_category);
        }
        if(b->settings_editing){
            text(c,x+18*s,y+100*s,settings_info[setting_id(b)].name,14*s,0x111111,w-36*s,true);
            double ix=x+18*s,iy=y+134*s,iw=w-36*s;
            box(c,ix,iy,iw,38*s,0xffffff);
            PangoLayout *layout=pango_cairo_create_layout(c);PangoFontDescription *font=pango_font_description_new();
            pango_font_description_set_family(font,"sans-serif");pango_font_description_set_absolute_size(font,13*s*PANGO_SCALE);
            pango_layout_set_font_description(layout,font);pango_layout_set_text(layout,b->settings_text,-1);
            PangoRectangle caret;pango_layout_get_cursor_pos(layout,(int)b->settings_cursor,&caret,NULL);
            double cx=caret.x/(double)PANGO_SCALE,offset=fmax(0,cx-(iw-24*s));
            cairo_save(c);cairo_rectangle(c,ix+6*s,iy+5*s,iw-12*s,28*s);cairo_clip(c);
            if(b->settings_select_all)box(c,ix+6*s,iy+6*s,iw-12*s,26*s,0xd5e4db);
            color(c,0x111111);cairo_move_to(c,ix+8*s-offset,iy+11*s);pango_cairo_show_layout(c,layout);
            if(!b->settings_select_all)box(c,ix+8*s+cx-offset,iy+9*s,1.2,20*s,0x111111);
            cairo_restore(c);pango_font_description_free(font);g_object_unref(layout);
            text(c,x+18*s,y+188*s,"Ctrl+A select all · Ctrl+V paste · Enter save · Esc cancel",11*s,0x4a4a4a,w-36*s,false);
        }else{
            unsigned visible=settings_visible(h,s),start=b->panel_selected>=visible?b->panel_selected-visible+1:0;
            for(unsigned i=start;i<setting_count(b)&&i<start+visible;i++){
                unsigned id=settings_categories[b->settings_category].first+i;double row=y+84*s+(i-start)*44*s;
                if(i==b->panel_selected)box(c,x+10*s,row,w-20*s,43*s,0xd5e4db);
                text(c,x+18*s,row+5*s,settings_info[id].name,12*s,0x111111,w-36*s,true);
                char value[4096];setting_value(b,id,value,sizeof value);text(c,x+18*s,row+25*s,value,11*s,0x4a4a4a,w-36*s,false);
            }
        }
        if(*b->settings_error)text(c,x+18*s,y+h-78*s,b->settings_error,11*s,0xc8102e,w-36*s,false);
        text(c,x+18*s,y+h-54*s,settings_info[setting_id(b)].help,11*s,0x4a4a4a,w-36*s,false);
        text(c,x+18*s,y+h-26*s,b->private_mode?"This private window only · ← → sections · ↑ ↓ choose · Esc close":"← → sections · ↑ ↓ choose · Enter edit/toggle · Esc close",11*s,0x4a4a4a,w-36*s,false);
    }else if(b->panel==2){
        unsigned count=b->network_panel?b->network_count:b->console_count,rows=(unsigned)fmax(1,floor((h-100*s)/(27*s))),available=count<128?count:128;
        if(!available)text(c,x+18*s,y+62*s,"No messages for this page.",12*s,0x4a4a4a,w-36*s,false);
        for(unsigned i=0;i<rows&&i+b->panel_scroll<available;i++){unsigned at=(count-1-i-b->panel_scroll)%128;const char *line=b->network_panel?b->network_log[at]:b->console[at];text(c,x+18*s,y+56*s+i*27*s,line,11*s,0x111111,w-36*s,false);}
        text(c,x+18*s,y+h-26*s,"Tab console/network · ↑ ↓ scroll · Esc close",11*s,0x4a4a4a,w-36*s,false);
    }else{
        text(c,x+18*s,y+60*s,b->download_state?b->download_status:"No downloads yet. Ctrl+S saves the current URL.",13*s,0x111111,w-36*s,false);
        if(b->download_state==1){char progress[128];snprintf(progress,sizeof progress,"%.1f KiB received%s",b->download_received/1024.,b->download_total?" (known size)":"");text(c,x+18*s,y+91*s,progress,12*s,0x4a4a4a,w-36*s,false);box(c,x+18*s,y+122*s,w-36*s,8*s,0xffffff);box(c,x+18*s,y+122*s,(w-36*s)*(b->download_total?fmin(1,b->download_received/(double)b->download_total):.08),8*s,0x1a1a1a);text(c,x+18*s,y+155*s,"Cancel download (Esc)",13*s,0xc8102e,w-36*s,true);}
        else if(*b->download_path){text(c,x+18*s,y+96*s,b->download_path,11*s,0x4a4a4a,w-36*s,false);text(c,x+18*s,y+130*s,"Ctrl+C copies the saved path",11*s,0x4a4a4a,w-36*s,false);}
        text(c,x+18*s,y+h-26*s,"Change the download folder in Settings > Downloads · Esc close",11*s,0x4a4a4a,w-36*s,false);
    }
}
static void draw(void*data,cairo_t*c,int w,int h){struct browser*b=data;bool changed=b->w!=w||b->h!=h;geometry(b,w,h);if(changed)request_layout(b);
    double s=b->scale;unsigned bg=b->acc.strong_contrast?0xffffff:0xf5f5f5,ink=0x111111,line=b->acc.strong_contrast?0x111111:0x111111;
    box(c,0,0,w,h,bg);box(c,0,0,w,32*s,b->private_mode?0x302640:0x111111);cube(c,14*s,8*s,16*s,0xf2f2f2);
    text(c,40*s,8*s,b->private_mode?"cube / private":"cube / browser",12*s,0xf2f2f2,w-200*s,true);
    text(c,w-106*s,6*s,"−",16*s,0xf2f2f2,25*s,false);text(c,w-70*s,6*s,"□",15*s,0xf2f2f2,25*s,false);text(c,w-34*s,5*s,"×",18*s,0xf2f2f2,25*s,false);
    text(c,17*s,46*s,"‹",27*s,b->trail_at>0?ink:0x9a9a9a,25*s,false);text(c,52*s,46*s,"›",27*s,b->trail_at+1<b->trail_count?ink:0x9a9a9a,25*s,false);
    text(c,90*s,50*s,loading(b)?"×":"↻",22*s,ink,30*s,false);text(c,130*s,52*s,"⌂",20*s,ink,30*s,false);
    double ax=172*s,ay=42*s,aw=w-322*s,ah=36*s;box(c,ax,ay,aw,ah,b->address_focus?0xffffff:0xefefef);
    if(b->address_focus){color(c,0x111111);cairo_set_line_width(c,1.5);cairo_rectangle(c,ax+.5,ay+.5,aw-1,ah-1);cairo_stroke(c);}
    const char*address=b->address;double tx=ax+12*s;
    if(b->select_all&&b->address_focus)box(c,tx,ay+7*s,aw-24*s,22*s,0xc8c8c8);
    if(b->address_focus&&!b->select_all){
        PangoLayout *layout=pango_cairo_create_layout(c);
        PangoFontDescription *font=pango_font_description_new();
        pango_font_description_set_family(font,"sans-serif");pango_font_description_set_absolute_size(font,13*s*PANGO_SCALE);
        pango_layout_set_font_description(layout,font);pango_layout_set_text(layout,address,-1);
        PangoRectangle caret;pango_layout_get_cursor_pos(layout,(int)fmin(b->cursor,strlen(address)),&caret,NULL);
        double cx=caret.x/(double)PANGO_SCALE,offset=fmax(0,cx-(aw-30*s));
        cairo_save(c);cairo_rectangle(c,tx,ay+6*s,fmax(1,aw-24*s),24*s);cairo_clip(c);
        color(c,ink);cairo_move_to(c,tx-offset,ay+10*s);pango_cairo_show_layout(c,layout);
        box(c,tx+cx-offset,ay+9*s,1.2,18*s,0x111111);cairo_restore(c);
        pango_font_description_free(font);g_object_unref(layout);
    }else text(c,tx,ay+10*s,address,13*s,ink,aw-24*s,false);
    text(c,w-142*s,52*s,b->bookmarked?"★":"☆",19*s,b->bookmarked?0x256357:ink,30*s,false);
    text(c,w-96*s,52*s,"+",20*s,ink,25*s,false);text(c,w-60*s,54*s,"Menu",11*s,ink,52*s,false);
    box(c,0,b->view_y-1,w,1,line);
    cairo_save(c);cairo_rectangle(c,0,b->view_y,w,b->view_h);cairo_clip(c);
    box(c,0,b->view_y,w,b->view_h,0xffffff);
    if(b->frame){cairo_set_source_surface(c,b->frame,0,b->view_y);cairo_paint(c);}else text(c,54*s,b->view_y+65*s,b->busy?"Opening your page…":"Enter an address to begin.",24*s,ink,w-100*s,false);
    if(b->page_h>(uint32_t)b->view_h){double thumb=fmax(24,b->view_h*(double)b->view_h/b->page_h),y=b->view_y+(b->view_h-thumb)*b->scroll/(b->page_h-b->view_h);box(c,w-5,y,4,thumb,0x111111);}
    cairo_restore(c);box(c,0,h-28*s,w,1,line);
    text(c,12*s,h-20*s,b->status,11*s,b->error?0xc8102e:0x4a4a4a,w-330*s,false);
    char label[128];snprintf(label,sizeof(label),"JavaScript %s     %.0f%%",b->scripts_enabled?"on":"off",b->zoom*100);text(c,w-275*s,h-20*s,label,11*s,0x4a4a4a,270*s,false);
    if(b->find_visible){box(c,w-360*s,b->view_y,360*s,50*s,0xefefef);text(c,w-345*s,b->view_y+8*s,"Find in page · Enter for next · Esc to close",10*s,0x4a4a4a,335*s,false);text(c,w-345*s,b->view_y+25*s,*b->find?b->find:"Type to find…",13*s,ink,335*s,false);}
    draw_panel(b,c);
}
static void scroll(void*data,double amount){struct browser*b=data;if(b->panel==1){if(!b->settings_editing)b->panel_selected=(b->panel_selected+setting_count(b)+(amount>0?1:-1))%setting_count(b);dirty(b);return;}if(b->panel==2){b->panel_scroll=(unsigned)fmax(0,fmin(127,(double)b->panel_scroll+(amount>0?1:-1)));dirty(b);return;}b->desired_scroll=fmax(0,fmin(b->desired_scroll+amount,fmax(0,(double)b->page_h-b->view_h)));request_layout(b);}
static void click(void*data,double x,double y,uint32_t button){struct browser*b=data;double s=b->scale;
    if(button==BTN_SIDE){go(b,-1);return;}if(button==BTN_EXTRA){go(b,1);return;}
    if(button!=BTN_LEFT)return;
    if(b->panel){s=panel_scale(b);double px,py,pw,ph;panel_geometry(b,&px,&py,&pw,&ph);if(x>=px&&x<px+pw&&y>=py&&y<py+ph){
            if(b->panel==1&&!b->settings_editing){
                if(y>=py+47*s&&y<py+75*s){unsigned category=(unsigned)fmax(0,(x-px-10*s)/((pw-20*s)/5));if(category<5){b->settings_category=category;b->panel_selected=0;b->settings_error[0]=0;dirty(b);}}
                else if(y>=py+84*s&&y<py+ph-76*s){unsigned visible=settings_visible(ph,s),start=b->panel_selected>=visible?b->panel_selected-visible+1:0,selected=start+(unsigned)((y-py-84*s)/(44*s));if(selected<setting_count(b)&&selected<start+visible){b->panel_selected=selected;panel_activate(b);}}
            }
            else if(b->panel==3&&b->download_state==1&&y>=py+140*s&&y<py+190*s)download_cancel(b);
            return;}
        b->panel=0;dirty(b);return;
    }
    if(y<32*s){if(x>b->w-40*s)cb_window_close(b->window);else if(x>b->w-78*s)cb_window_maximize(b->window);else if(x>b->w-114*s)cb_window_minimize(b->window);else cb_window_move(b->window);return;}
    if(x>=b->w-7&&y>=b->h-7){cb_window_resize(b->window,10);return;}
    if(y<b->view_y){
        if(x<42*s)go(b,-1);else if(x<78*s)go(b,1);else if(x<120*s){if(loading(b)){reset_resources(b);stop_worker(&b->net);stop_worker(&b->render);b->busy=false;b->ready=false;b->layout_pending=false;snprintf(b->status,sizeof(b->status),"Loading stopped");}else navigate(b,b->url,false,false);}
        else if(x<166*s)navigate(b,b->config.homepage,true,false);
        else if(x<b->w-150*s){blur_page(b);b->address_focus=true;b->find_focus=false;b->select_all=true;b->cursor=strlen(b->address);}
        else if(x<b->w-114*s)bookmark_toggle(b);
        else if(x<b->w-70*s)new_tab(b);else panel_open(b,1);
        dirty(b);return;
    }
    if(b->find_visible&&x>b->w-360*s&&y<b->view_y+50*s){blur_page(b);b->find_focus=true;b->address_focus=false;return;}
    b->address_focus=false;b->find_focus=false;dirty(b);
    if(b->ready&&y<b->h-28*s)interact(b,CB_INTERACT_CLICK,x,y-b->view_y+b->scroll,NULL);
}
static void insert(struct browser*b,const char*utf8){
    char *dst=b->find_focus?b->find:b->address;size_t cap=b->find_focus?sizeof(b->find):sizeof(b->address),len=strlen(dst),n=strlen(utf8);
    if(!g_utf8_validate(utf8,-1,NULL))return;
    for(const char*p=utf8;*p;p++)if((unsigned char)*p<32||(unsigned char)*p==127)return;
    if(!b->find_focus&&b->select_all){dst[0]=0;len=0;b->cursor=0;b->select_all=false;}
    size_t pos=b->find_focus?len:b->cursor;if(pos>len)pos=len;
    if(len+n>=cap)return;
    memmove(dst+pos+n,dst+pos,len-pos+1);memcpy(dst+pos,utf8,n);b->cursor=pos+n;
    if(b->find_focus){b->find_y=-1;b->find_wrapping=false;}
    dirty(b);
}
static void settings_insert(struct browser *b,const char *value){
    if(!g_utf8_validate(value,-1,NULL))return;
    for(const unsigned char *p=(const unsigned char *)value;*p;p++)if(*p<32||*p==127)return;
    size_t length=strlen(b->settings_text),count=strlen(value),pos=b->settings_cursor;
    if(b->settings_select_all){length=pos=0;}
    if(length+count>=sizeof b->settings_text)return;
    if(b->settings_select_all){b->settings_text[0]=0;b->settings_select_all=false;}
    memmove(b->settings_text+pos+count,b->settings_text+pos,length-pos+1);
    memcpy(b->settings_text+pos,value,count);b->settings_cursor=pos+count;b->settings_error[0]=0;dirty(b);
}
static void settings_edit_key(struct browser *b,xkb_keysym_t sym,const char *utf8,bool ctrl,bool alt){
    xkb_keysym_t lower=xkb_keysym_to_lower(sym);size_t length=strlen(b->settings_text),pos=b->settings_cursor;
    if(ctrl&&lower==XKB_KEY_w)cb_window_close(b->window);
    else if(sym==XKB_KEY_Escape){b->settings_editing=false;b->settings_error[0]=0;}
    else if(sym==XKB_KEY_Return||sym==XKB_KEY_KP_Enter)settings_edit_commit(b);
    else if(ctrl&&lower==XKB_KEY_a)b->settings_select_all=true;
    else if(ctrl&&(lower==XKB_KEY_c||lower==XKB_KEY_x)){
        cb_window_copy(b->window,b->settings_text);
        if(lower==XKB_KEY_x){b->settings_text[0]=0;b->settings_cursor=0;b->settings_select_all=false;}
    }else if(ctrl&&lower==XKB_KEY_v)cb_window_paste(b->window);
    else if(sym==XKB_KEY_BackSpace||sym==XKB_KEY_Delete){
        if(b->settings_select_all){b->settings_text[0]=0;b->settings_cursor=0;b->settings_select_all=false;}
        else if(sym==XKB_KEY_BackSpace&&pos){size_t previous=pos-1;while(previous&&((unsigned char)b->settings_text[previous]&0xc0)==0x80)previous--;memmove(b->settings_text+previous,b->settings_text+pos,length-pos+1);b->settings_cursor=previous;}
        else if(sym==XKB_KEY_Delete&&pos<length){size_t next=pos+1;while(next<length&&((unsigned char)b->settings_text[next]&0xc0)==0x80)next++;memmove(b->settings_text+pos,b->settings_text+next,length-next+1);}
        b->settings_error[0]=0;
    }else if(sym==XKB_KEY_Left||sym==XKB_KEY_Right||sym==XKB_KEY_Home||sym==XKB_KEY_End){
        b->settings_select_all=false;
        if(sym==XKB_KEY_Home)pos=0;else if(sym==XKB_KEY_End)pos=length;
        else if(sym==XKB_KEY_Left&&pos){pos--;while(pos&&((unsigned char)b->settings_text[pos]&0xc0)==0x80)pos--;}
        else if(sym==XKB_KEY_Right&&pos<length){pos++;while(pos<length&&((unsigned char)b->settings_text[pos]&0xc0)==0x80)pos++;}
        b->settings_cursor=pos;
    }else if(!ctrl&&!alt&&*utf8)settings_insert(b,utf8);
    dirty(b);
}
static void paste(void*data,const char*t){struct browser*b=data;if(b->panel==1){if(b->settings_editing)settings_insert(b,t);return;}if(b->address_focus||b->find_focus)insert(b,t);else interact(b,CB_INTERACT_TEXT,0,0,t);}
static void key(void*data,xkb_keysym_t sym,const char*utf8,bool ctrl,bool shift,bool alt){struct browser*b=data;
    xkb_keysym_t lower=xkb_keysym_to_lower(sym);
    if(b->panel==1&&b->settings_editing){settings_edit_key(b,sym,utf8,ctrl,alt);return;}
    if(ctrl&&shift&&lower==XKB_KEY_p){new_window(b,true);return;}
    if(ctrl&&shift&&lower==XKB_KEY_b){navigate(b,"cube:bookmarks",true,false);return;}
    if(ctrl&&lower==XKB_KEY_d){bookmark_toggle(b);return;}
    if(ctrl&&sym==XKB_KEY_comma){panel_open(b,1);return;}
    if(sym==XKB_KEY_F12||(ctrl&&shift&&lower==XKB_KEY_j)){panel_open(b,2);return;}
    if(ctrl&&lower==XKB_KEY_j){panel_open(b,3);return;}
    if(ctrl&&lower==XKB_KEY_s){download_start(b);return;}
    if(b->panel&&!(ctrl&&(lower==XKB_KEY_l||lower==XKB_KEY_w||lower==XKB_KEY_r))){
        if(sym==XKB_KEY_Escape){if(b->panel==3&&b->download_state==1)download_cancel(b);else b->panel=0;}
        else if(b->panel==1){
            if(sym==XKB_KEY_Left||sym==XKB_KEY_Right){b->settings_category=(b->settings_category+(sym==XKB_KEY_Right?1:4))%5;b->panel_selected=0;b->settings_error[0]=0;}
            else if(sym==XKB_KEY_Up||sym==XKB_KEY_ISO_Left_Tab||(sym==XKB_KEY_Tab&&shift))b->panel_selected=(b->panel_selected+setting_count(b)-1)%setting_count(b);
            else if(sym==XKB_KEY_Down||sym==XKB_KEY_Tab)b->panel_selected=(b->panel_selected+1)%setting_count(b);
            else if(sym==XKB_KEY_Return||sym==XKB_KEY_KP_Enter||sym==XKB_KEY_space)panel_activate(b);
        }
        else if(b->panel==2){if(sym==XKB_KEY_Tab){b->network_panel=!b->network_panel;b->panel_scroll=0;}else if(sym==XKB_KEY_Down)b->panel_scroll=(unsigned)fmin(127,b->panel_scroll+1);else if(sym==XKB_KEY_Up&&b->panel_scroll)b->panel_scroll--;}
        else if(b->panel==3&&ctrl&&lower==XKB_KEY_c&&*b->download_path)cb_window_copy(b->window,b->download_path);
        dirty(b);return;
    }
    if(ctrl&&lower==XKB_KEY_l){b->panel=0;blur_page(b);b->address_focus=true;b->find_focus=false;b->select_all=true;b->cursor=strlen(b->address);}
    else if(ctrl&&lower==XKB_KEY_t)new_tab(b);
    else if(ctrl&&lower==XKB_KEY_w)cb_window_close(b->window);
    else if(ctrl&&lower==XKB_KEY_h)navigate(b,"cube:history",true,false);
    else if((ctrl&&lower==XKB_KEY_r)||sym==XKB_KEY_F5)navigate(b,b->url,false,false);
    else if(ctrl&&lower==XKB_KEY_f){blur_page(b);b->find_visible=true;b->find_focus=true;b->address_focus=false;b->find[0]=0;b->find_y=-1;b->find_wrapping=false;}
    else if(sym==XKB_KEY_F3)find_next(b);
    else if(alt&&sym==XKB_KEY_Left)go(b,-1);
    else if(alt&&sym==XKB_KEY_Right)go(b,1);
    else if(ctrl&&(sym==XKB_KEY_plus||sym==XKB_KEY_equal||sym==XKB_KEY_minus||sym==XKB_KEY_0)){
        struct cb_config next=b->config;next.zoom=sym==XKB_KEY_0?1:fmax(.5,fmin(3,b->zoom+(sym==XKB_KEY_minus?-.1:.1)));
        apply_preferences(b,&next,false);
    }else if(sym==XKB_KEY_Escape){blur_page(b);b->address_focus=b->find_focus=b->find_visible=false;snprintf(b->address,sizeof(b->address),"%s",b->url);}
    else if(!ctrl&&!alt&&(sym==XKB_KEY_Tab||sym==XKB_KEY_ISO_Left_Tab)){
        b->address_focus=b->find_focus=b->select_all=false;
        interact(b,shift||sym==XKB_KEY_ISO_Left_Tab?CB_INTERACT_PREVIOUS:CB_INTERACT_NEXT,0,0,NULL);
    }
    else if(ctrl&&lower==XKB_KEY_v)cb_window_paste(b->window);
    else if(b->address_focus&&ctrl&&lower==XKB_KEY_c)cb_window_copy(b->window,b->address);
    else if((b->address_focus||b->find_focus)&&ctrl&&lower==XKB_KEY_a){if(b->address_focus)b->select_all=true;else b->find[0]=0;}
    else if(b->address_focus||b->find_focus){
        char *dst=b->find_focus?b->find:b->address;size_t len=strlen(dst),pos=b->find_focus?len:b->cursor;if(pos>len)pos=len;
        if(sym==XKB_KEY_Return||sym==XKB_KEY_KP_Enter){if(b->find_focus)find_next(b);else navigate(b,b->address,true,false);}
        else if(sym==XKB_KEY_BackSpace||sym==XKB_KEY_Delete){
            if(b->find_focus){b->find_y=-1;b->find_wrapping=false;}
            if(b->address_focus&&b->select_all){dst[0]=0;b->cursor=0;b->select_all=false;}
            else if(sym==XKB_KEY_BackSpace&&pos){size_t p=pos-1;while(p&&((unsigned char)dst[p]&0xc0)==0x80)p--;memmove(dst+p,dst+pos,len-pos+1);b->cursor=p;}
            else if(sym==XKB_KEY_Delete&&pos<len){size_t p=pos+1;while(p<len&&((unsigned char)dst[p]&0xc0)==0x80)p++;memmove(dst+pos,dst+p,len-p+1);}
        }else if(b->address_focus&&(sym==XKB_KEY_Left||sym==XKB_KEY_Right||sym==XKB_KEY_Home||sym==XKB_KEY_End)){
            b->select_all=false;if(sym==XKB_KEY_Home)pos=0;else if(sym==XKB_KEY_End)pos=len;
            else if(sym==XKB_KEY_Left&&pos){pos--;while(pos&&((unsigned char)dst[pos]&0xc0)==0x80)pos--;}
            else if(sym==XKB_KEY_Right&&pos<len){pos++;while(pos<len&&((unsigned char)dst[pos]&0xc0)==0x80)pos++;}
            b->cursor=pos;
        }else if(!ctrl&&!alt&&*utf8)insert(b,utf8);
    }else if(ctrl&&(lower==XKB_KEY_a||lower==XKB_KEY_c||lower==XKB_KEY_x)){
        interact(b,CB_INTERACT_KEY,0,0,lower==XKB_KEY_a?"select-all":lower==XKB_KEY_c?"copy":"cut");
    }else if(sym==XKB_KEY_Return||sym==XKB_KEY_KP_Enter)interact(b,CB_INTERACT_KEY,0,0,"enter");
    else if((b->page_focus>=CB_FOCUS_EDIT||b->interaction_pending)&&
            (sym==XKB_KEY_Left||sym==XKB_KEY_Right||sym==XKB_KEY_Home||sym==XKB_KEY_End||
             sym==XKB_KEY_BackSpace||sym==XKB_KEY_Delete||sym==XKB_KEY_space)){
        const char *name=sym==XKB_KEY_Left?"left":sym==XKB_KEY_Right?"right":sym==XKB_KEY_Home?"home":
            sym==XKB_KEY_End?"end":sym==XKB_KEY_BackSpace?"backspace":sym==XKB_KEY_Delete?"delete":"space";
        interact(b,CB_INTERACT_KEY,0,0,name);
    }else if((b->page_focus>=CB_FOCUS_EDIT||b->interaction_pending)&&(sym==XKB_KEY_Down||sym==XKB_KEY_Up)){
        interact(b,CB_INTERACT_KEY,0,0,sym==XKB_KEY_Down?"down":"up");
    }else if(sym==XKB_KEY_Down||sym==XKB_KEY_Up)scroll(b,sym==XKB_KEY_Down?48:-48);
    else if(sym==XKB_KEY_Page_Down||sym==XKB_KEY_Page_Up||sym==XKB_KEY_space)scroll(b,(sym==XKB_KEY_Page_Up||shift?-1:1)*b->view_h*.85);
    else if(sym==XKB_KEY_Home){b->desired_scroll=0;request_layout(b);}
    else if(sym==XKB_KEY_End){b->desired_scroll=b->page_h;request_layout(b);}
    else if(!ctrl&&!alt&&*utf8)interact(b,CB_INTERACT_TEXT,0,0,utf8);
    dirty(b);
}
static void cleanup(struct browser*b){
    stop_worker(&b->download);reset_resources(b);stop_worker(&b->net);stop_worker(&b->render);
    if(b->config.clear_on_exit&&!b->private_mode){cb_session_clear(b->session);if(cb_history_clear())fprintf(stderr,"cube-browser: could not clear history on exit\n");}
    cb_session_close(b->session);free_frame(b);for(int i=0;i<b->trail_count;i++)free(b->trail[i]);cb_window_free(b->window);
}
static void usage(void){puts("Cube Browser " CB_VERSION " — native interactive-web preview\nUsage: cube-browser [--private] [--console] [--settings] [--] [URL | HTML-or-image-file]\n       cube-browser --render URL OUTPUT.png [WIDTHxHEIGHT]\n       cube-browser --dump URL\n       cube-browser --check\n       cube-browser --quit-after SECONDS [URL]\nCtrl+L address, Ctrl+T desktop tab, Ctrl+H history, Ctrl+F find, Ctrl+R reload.\nCtrl+D bookmark, Ctrl+Shift+B bookmarks, Ctrl+Shift+P private window.\nCtrl+comma settings, F12 diagnostics, Ctrl+S save current URL, Ctrl+J downloads.\nTab/Shift+Tab focus links and controls; Enter activates/submits.\nSupports bounded HTML/CSS, JavaScript/DOM, GET/POST forms, cookies and storage.\nComplete web APIs, modules, file uploads and audio/video remain unsupported.");}
static int health_check(const char *directory){
    int failures=0;char path[PATH_MAX];struct stat st;
    puts("Cube Browser " CB_VERSION " · installation check");
    const char *workers[]={"cube-renderd","cube-netd"};
    for(unsigned i=0;i<2;i++){
        int n=snprintf(path,sizeof path,"%s/%s",directory,workers[i]);
        bool ok=n>0&&(size_t)n<sizeof path&&!stat(path,&st)&&S_ISREG(st.st_mode)&&!access(path,X_OK);
        printf("%s: %s\n",workers[i],ok?"OK":"MISSING or not executable");failures+=!ok;
    }
    bool ca=!stat(CB_CA_BUNDLE,&st)&&S_ISREG(st.st_mode)&&st.st_size>0&&!access(CB_CA_BUNDLE,R_OK);
    printf("HTTPS certificates: %s\n",ca?"OK":"MISSING (install ca-certificates)");failures+=!ca;
    int abi=(int)syscall(SYS_landlock_create_ruleset,NULL,0,LANDLOCK_CREATE_RULESET_VERSION);
    printf("Worker filesystem sandbox: Landlock ABI %d%s\n",abi,abi>=3?" (OK)":" (requires ABI 3 or newer)");failures+=abi<3;
    const char *display=getenv("WAYLAND_DISPLAY"),*runtime=getenv("XDG_RUNTIME_DIR"),*socket=getenv("WAYLAND_SOCKET");
    bool wayland=socket&&*socket;
    if(display&&*display){
        int n=display[0]=='/'?snprintf(path,sizeof path,"%s",display):snprintf(path,sizeof path,"%s/%s",runtime?runtime:"",display);
        wayland=n>0&&(size_t)n<sizeof path&&!stat(path,&st)&&S_ISSOCK(st.st_mode);
    }
    printf("Desktop: %s\n",wayland?"Wayland socket available":"No Wayland socket · use a Wayland session for windows; --render/--dump work headlessly");
    puts("This checks installation prerequisites; run --dump cube:home to exercise the worker sandboxes.");
    return failures?1:0;
}
int main(int argc,char**argv){
    unsetenv("DISPLAY");signal(SIGPIPE,SIG_IGN);umask(077);
    struct browser b={.download={.socket=-1},.scripts_enabled=true,.net={.socket=-1},.render={.socket=-1},.trail_at=-1,.w=1120,.h=800};
    ssize_t n=readlink("/proc/self/exe",b.executable,sizeof(b.executable)-1);if(n<0)return 1;b.executable[n]=0;snprintf(b.directory,sizeof(b.directory),"%s",b.executable);char*slash=strrchr(b.directory,'/');if(!slash)return 1;*slash=0;
    if(argc==2&&(!strcmp(argv[1],"--help")||!strcmp(argv[1],"-h"))){usage();return 0;}
    if(argc==2&&!strcmp(argv[1],"--version")){puts("cube-browser " CB_VERSION " (interactive-web preview)");return 0;}
    if(argc==2&&!strcmp(argv[1],"--check"))return health_check(b.directory);
    char error[256];cb_config_defaults(&b.config);if(cb_config_load(&b.config,error,sizeof(error)))fprintf(stderr,"cube-browser: %s\n",error);
    cb_accessibility_load(&b.acc);b.scale=b.acc.ui_scale;b.zoom=b.config.zoom;
    const char *url=b.config.homepage,*output=NULL;bool dump=false,positional=false,explicit_url=false,open_settings=false;double seconds=0;int width=1120,height=800;
    for(int i=1;i<argc;i++){
        if(positional){if(explicit_url){usage();return 2;}url=argv[i];explicit_url=true;continue;}
        if(!strcmp(argv[i],"--")){positional=true;continue;}
        if(!strcmp(argv[i],"--help")||!strcmp(argv[i],"-h")){usage();return 0;}
        else if(!strcmp(argv[i],"--version")){puts("cube-browser " CB_VERSION " (interactive-web preview)");return 0;}
        else if(!strcmp(argv[i],"--console"))b.print_console=true;
        else if(!strcmp(argv[i],"--private"))b.private_mode=true;
        else if(!strcmp(argv[i],"--settings"))open_settings=true;
        else if(!strcmp(argv[i],"--render")&&i+2<argc){if(output||dump||explicit_url){usage();return 2;}explicit_url=true;url=argv[++i];output=argv[++i];b.private_mode=true;if(i+1<argc&&argv[i+1][0]!='-'){char tail;if(sscanf(argv[++i],"%dx%d%c",&width,&height,&tail)!=2){usage();return 2;}}}
        else if(!strcmp(argv[i],"--dump")&&i+1<argc){if(output||dump||explicit_url){usage();return 2;}explicit_url=true;url=argv[++i];dump=true;b.private_mode=true;}
        else if(!strcmp(argv[i],"--quit-after")&&i+1<argc){char*end;seconds=strtod(argv[++i],&end);if(*end||!isfinite(seconds)||seconds<=0||seconds>3600)return 2;}
        else if(argv[i][0]=='-'){usage();return 2;}else {if(explicit_url){usage();return 2;}url=argv[i];explicit_url=true;}
    }
    char local_url[4096];
    if(explicit_url&&url[0]!='/'&&!strchr(url,':')&&!access(url,F_OK)){
        char *path=g_canonicalize_filename(url,NULL);
        if(strlen(path)>=sizeof local_url){g_free(path);return 2;}
        snprintf(local_url,sizeof local_url,"%s",path);g_free(path);url=local_url;
    }
    if(width<640||height<400||width>4096||height>4096){fprintf(stderr,"Render size must be 640x400 through 4096x4096.\n");return 2;}
    geometry(&b,width,height);
    b.session=cb_session_open(b.private_mode);if(!b.session){fprintf(stderr,"Cannot open browser site storage\n");return 1;}
    cb_session_configure(b.session,&b.config);
    if(output||dump){
        if(open_settings){fprintf(stderr,"--settings requires a browser window.\n");cleanup(&b);return 2;}
        navigate(&b,url,true,false);double deadline=now()+45;
        while((loading(&b)||b.layout_pending||b.relayout||b.anchor_needed||b.anchor_pending||(b.script_busy&&now()<b.script_settle))&&now()<deadline){tick(&b);poll(NULL,0,10);}
        if(!b.frame||loading(&b)||b.layout_pending||b.relayout||b.anchor_needed||b.anchor_pending){fprintf(stderr,"cube-browser: %s (no completed frame)\n",b.status);cleanup(&b);return 1;}
        int result=b.error?1:0;
        if(dump){struct cb_message m={.type=CB_TEXT,.fd=-1};cb_send(b.render.socket,&m);double until=now()+5;bool got=false;
            while(now()<until){struct pollfd fd={b.render.socket,POLLIN,0};if(poll(&fd,1,100)<=0)continue;if(cb_recv(b.render.socket,&m)!=1)break;
                if(m.type==CB_TEXT_RESULT&&m.fd>=0){char *t=NULL;size_t size;if(!cb_read_document(m.fd,&t,&size)){fwrite(t,1,size,stdout);free(t);got=true;}close(m.fd);break;}
                if(m.fd>=0)close(m.fd);
            }if(!got)result=1;
        }else{cairo_surface_t*s=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,width,height);cairo_t*c=cairo_create(s);draw(&b,c,width,height);cairo_destroy(c);if(cairo_surface_write_to_png(s,output)!=CAIRO_STATUS_SUCCESS)result=1;cairo_surface_destroy(s);}
        if(result)fprintf(stderr,"cube-browser: %s\n",b.status);
        cleanup(&b);return result;
    }
    b.window=cb_window_new((struct cb_callbacks){draw,key,click,scroll,tick,paste},&b);
    if(!b.window){fprintf(stderr,"cube-browser: cannot connect to Wayland; run inside a Wayland desktop (including Whitecube), or use --render/--dump. Run --check for installation diagnostics.\n");return 1;}
    navigate(&b,url,true,false);if(open_settings)panel_open(&b,1);int result=cb_window_run(b.window,seconds);cleanup(&b);return result;
}
