#include "version.h"
#define _POSIX_C_SOURCE 200809L
#include "net.h"
#include "policy.h"
#include "ipc.h"
#include "download.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/error.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>
#include <psa/crypto.h>

/* A small, synchronous fetcher confined to cube-netd. UI/render processes never
 * use this module. HTTP/1.1 only, one connection per request; credentials come from the UI. */
#define HEADER_LIMIT 65536u
#define LINE_LIMIT 16384u
#define READ_SIZE 8192u
/* A compile-time override is used only by the isolated local TLS test build.
 * Production has no environment or request-controlled trust-store override. */
#ifndef CB_CA_BUNDLE
#define CB_CA_BUNDLE "/etc/ssl/certs/ca-certificates.crt"
#endif

struct connection {
    int fd,progress_socket;size_t body_limit,progress,total; bool tls; int64_t deadline;
    mbedtls_ssl_context ssl; mbedtls_ssl_config config;
    mbedtls_x509_crt ca; mbedtls_entropy_context entropy;
    mbedtls_ctr_drbg_context rng;
    struct cb_response *response;
};
struct reader { struct connection *conn; unsigned char data[READ_SIZE]; size_t pos, len; };

static int failure(struct cb_response *r, const char *fmt, ...) {
    va_list args; va_start(args, fmt); vsnprintf(r->error, sizeof r->error, fmt, args); va_end(args); return -1;
}
static int64_t now_ms(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static int wait_fd(int fd, short events, int64_t deadline) {
    for (;;) {
        int64_t left = deadline - now_ms(); if (left <= 0) { errno = ETIMEDOUT; return -1; }
        struct pollfd p = { .fd=fd, .events=events };
        int result = poll(&p, 1, left > INT_MAX ? INT_MAX : (int)left);
        if (result > 0) { if (p.revents & POLLNVAL) { errno=EBADF; return -1; } return 0; }
        if (result == 0) { errno = ETIMEDOUT; return -1; }
        if (errno != EINTR) return -1;
    }
}
/* DNS uses the desktop's explicit resolver list. A bounded UDP A lookup avoids
 * libc's unbounded blocking resolver in the network worker. */
static uint16_t get16(const unsigned char *p) { return (uint16_t)((p[0]<<8)|p[1]); }
static int skip_name(const unsigned char *p, size_t n, size_t *at) {
    for (unsigned labels=0; labels<128 && *at<n; labels++) {
        unsigned v=p[(*at)++]; if (!v)return 0;
        if ((v&0xc0)==0xc0) { if(*at>=n || (((v&63)<<8)|p[*at])>=n)return -1; (*at)++; return 0; }
        if (v>63 || *at+v>n)return -1;
        *at+=v;
    } return -1;
}
static int hosts_lookup(const char *host, struct in_addr *ip) {
    if (inet_pton(AF_INET,host,ip)==1)return 0;
    if (!strcmp(host,"localhost")) { ip->s_addr=htonl(INADDR_LOOPBACK); return 0; }
    FILE *f=fopen("/etc/hosts","r"); if(!f)return -1; char line[2048];
    while(fgets(line,sizeof line,f)) {
        char *hash=strchr(line,'#'); if(hash)*hash='\0'; char *save=NULL, *addr=strtok_r(line," \t\r\n",&save);
        struct in_addr candidate; if(!addr || inet_pton(AF_INET,addr,&candidate)!=1)continue;
        for(char *name=strtok_r(NULL," \t\r\n",&save);name;name=strtok_r(NULL," \t\r\n",&save)) if(!strcasecmp(name,host)) { *ip=candidate; fclose(f); return 0; }
    } fclose(f); return -1;
}
static int resolve(const char *host, struct in_addr *ip, int64_t deadline, struct cb_response *r) {
    if (!hosts_lookup(host,ip))return 0;
    struct in_addr servers[3]; size_t count=0; char line[512], value[128];
    FILE *f=fopen("/run/whitecube-net/resolv.conf","r"); if(!f)f=fopen("/etc/resolv.conf","r");
    if(f) { while(count<3 && fgets(line,sizeof line,f)) if(sscanf(line," nameserver %127s",value)==1 && inet_pton(AF_INET,value,&servers[count])==1)count++; fclose(f); }
    if(!count)return failure(r,"No IPv4 DNS nameserver configured");
    unsigned char request[512]={0}, answer[4096]; uint16_t ident;
    if(getrandom(&ident,sizeof ident,0)!=(ssize_t)sizeof ident)return failure(r,"DNS random source unavailable");
    request[0]=(unsigned char)(ident>>8);request[1]=(unsigned char)ident;request[2]=1;request[5]=1;
    size_t used=12; const char *p=host;
    while(*p) { const char *dot=strchr(p,'.'); size_t n=dot?(size_t)(dot-p):strlen(p); if(!n || n>63 || used+n+6>=sizeof request)return failure(r,"Invalid DNS hostname"); request[used++]=(unsigned char)n;memcpy(request+used,p,n);used+=n; if(!dot)break;p=dot+1; }
    request[used++]=0;request[used++]=0;request[used++]=1;request[used++]=0;request[used++]=1;
    for(size_t i=0;i<count;i++) {
        int fd=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);if(fd<0)continue;
        struct sockaddr_in dest={.sin_family=AF_INET,.sin_port=htons(53),.sin_addr=servers[i]};
        if(connect(fd,(struct sockaddr *)&dest,sizeof dest)<0 || send(fd,request,used,0)!=(ssize_t)used) { close(fd);continue; }
        int64_t until=now_ms()+2000;if(until>deadline)until=deadline;
        if(wait_fd(fd,POLLIN,until)) {close(fd);continue;}
        ssize_t received=recv(fd,answer,sizeof answer,0);close(fd);
        if(received<12 || get16(answer)!=ident || !(answer[2]&0x80))continue;
        if(answer[3]&15)return failure(r,"DNS could not resolve %s (code %u)",host,answer[3]&15);
        if(answer[2]&2)return failure(r,"DNS reply needs TCP retry (not supported yet)");
        size_t at=12, n=(size_t)received; unsigned questions=get16(answer+4), answers=get16(answer+6);
        if(questions!=1 || skip_name(answer,n,&at) || at+4>n)continue;
        at+=4;
        if(at!=used || memcmp(answer+12,request+12,used-12))continue;
        for(unsigned j=0;j<answers;j++) { if(skip_name(answer,n,&at) || at+10>n)break;unsigned type=get16(answer+at),cls=get16(answer+at+2),len=get16(answer+at+8);at+=10;if(at+len>n)break; if(type==1 && cls==1 && len==4) {memcpy(ip,answer+at,4);return 0;}at+=len; }
    }
    return failure(r,"DNS lookup failed or timed out for %s",host);
}
static void connection_init(struct connection *c, struct cb_response *r, int64_t deadline) {
    memset(c,0,sizeof *c);c->fd=-1;c->response=r;c->deadline=deadline;
    mbedtls_ssl_init(&c->ssl);mbedtls_ssl_config_init(&c->config);mbedtls_x509_crt_init(&c->ca);mbedtls_entropy_init(&c->entropy);mbedtls_ctr_drbg_init(&c->rng);
}
static void connection_free(struct connection *c) {
    if(c->fd>=0)close(c->fd);
    mbedtls_ssl_free(&c->ssl);mbedtls_ssl_config_free(&c->config);mbedtls_x509_crt_free(&c->ca);mbedtls_ctr_drbg_free(&c->rng);mbedtls_entropy_free(&c->entropy);
}
static int tls_send(void *ctx, const unsigned char *p, size_t n) {
    struct connection *c=ctx; ssize_t x=send(c->fd,p,n,MSG_NOSIGNAL);
    if(x>=0)return (int)x;
    if(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR)return MBEDTLS_ERR_SSL_WANT_WRITE;
    return MBEDTLS_ERR_NET_SEND_FAILED;
}
static int tls_recv(void *ctx, unsigned char *p, size_t n) {
    struct connection *c=ctx;ssize_t x=recv(c->fd,p,n,0);
    if(x>=0)return (int)x;
    if(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR)return MBEDTLS_ERR_SSL_WANT_READ;
    return MBEDTLS_ERR_NET_RECV_FAILED;
}
static int tls_failure(struct connection *c, int code) {
    char detail[160];mbedtls_strerror(code,detail,sizeof detail);return failure(c->response,"TLS: %s",detail);
}
static int connection_open(struct connection *c, const struct cb_http_url *u) {
    struct in_addr ip;if(resolve(u->host,&ip,c->deadline,c->response))return -1;
    c->fd=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);if(c->fd<0)return failure(c->response,"Socket: %s",strerror(errno));
    struct sockaddr_in addr={.sin_family=AF_INET,.sin_port=htons((uint16_t)u->port),.sin_addr=ip};
    if(connect(c->fd,(struct sockaddr *)&addr,sizeof addr)<0) {
        if(errno!=EINPROGRESS || wait_fd(c->fd,POLLOUT,c->deadline))return failure(c->response,"Connect: %s",strerror(errno));
        int error=0;socklen_t size=sizeof error;if(getsockopt(c->fd,SOL_SOCKET,SO_ERROR,&error,&size)<0 || error)return failure(c->response,"Connect: %s",strerror(error?error:errno));
    }
    if(!u->tls)return 0;
    c->tls=true;int rc;
    if(psa_crypto_init()!=PSA_SUCCESS)return failure(c->response,"TLS crypto initialization failed");
    const unsigned char seed[]="cube-browser-net";
    if((rc=mbedtls_ctr_drbg_seed(&c->rng,mbedtls_entropy_func,&c->entropy,seed,sizeof seed-1)))return tls_failure(c,rc);
    if((rc=mbedtls_x509_crt_parse_file(&c->ca,CB_CA_BUNDLE))<0)return tls_failure(c,rc);
    if((rc=mbedtls_ssl_config_defaults(&c->config,MBEDTLS_SSL_IS_CLIENT,MBEDTLS_SSL_TRANSPORT_STREAM,MBEDTLS_SSL_PRESET_DEFAULT)))return tls_failure(c,rc);
    mbedtls_ssl_conf_authmode(&c->config,MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_ca_chain(&c->config,&c->ca,NULL);
    mbedtls_ssl_conf_rng(&c->config,mbedtls_ctr_drbg_random,&c->rng);
    mbedtls_ssl_conf_min_tls_version(&c->config,MBEDTLS_SSL_VERSION_TLS1_3);
    mbedtls_ssl_conf_max_tls_version(&c->config,MBEDTLS_SSL_VERSION_TLS1_3);
    static const char *alpn[]={"http/1.1",NULL};if((rc=mbedtls_ssl_conf_alpn_protocols(&c->config,alpn)))return tls_failure(c,rc);
    if((rc=mbedtls_ssl_setup(&c->ssl,&c->config)) || (rc=mbedtls_ssl_set_hostname(&c->ssl,u->host)))return tls_failure(c,rc);
    mbedtls_ssl_set_bio(&c->ssl,c,tls_send,tls_recv,NULL);
    while((rc=mbedtls_ssl_handshake(&c->ssl))) {
        if(rc!=MBEDTLS_ERR_SSL_WANT_READ && rc!=MBEDTLS_ERR_SSL_WANT_WRITE)return tls_failure(c,rc);
        if(wait_fd(c->fd,rc==MBEDTLS_ERR_SSL_WANT_READ?POLLIN:POLLOUT,c->deadline))return failure(c->response,"TLS handshake timed out");
    }
    if(mbedtls_ssl_get_verify_result(&c->ssl))return failure(c->response,"TLS certificate verification failed");
    return 0;
}
static int send_request(struct connection *c, const char *p, size_t n) {
    while(n) {
        if(now_ms()>=c->deadline)return failure(c->response,"Request timed out");
        int rc=c->tls?mbedtls_ssl_write(&c->ssl,(const unsigned char *)p,n):tls_send(c,(const unsigned char *)p,n);
        if(rc>0) {p+=rc;n-=(size_t)rc;continue;}
        if(rc==MBEDTLS_ERR_SSL_WANT_WRITE||rc==MBEDTLS_ERR_SSL_WANT_READ) {if(!wait_fd(c->fd,rc==MBEDTLS_ERR_SSL_WANT_WRITE?POLLOUT:POLLIN,c->deadline))continue;return failure(c->response,"Request timed out");}
        return failure(c->response,"Could not send HTTP request");
    }return 0;
}
static int fill(struct reader *r) {
    struct connection *c=r->conn;
    for(;;) {
        if(now_ms()>=c->deadline)return failure(c->response,"Response timed out");
        int rc=c->tls?mbedtls_ssl_read(&c->ssl,r->data,sizeof r->data):tls_recv(c,r->data,sizeof r->data);
        if(rc>0) {r->pos=0;r->len=(size_t)rc;return rc;}
        if(!rc||rc==MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY)return 0;
        if(rc==MBEDTLS_ERR_SSL_WANT_READ||rc==MBEDTLS_ERR_SSL_WANT_WRITE) {if(!wait_fd(c->fd,rc==MBEDTLS_ERR_SSL_WANT_READ?POLLIN:POLLOUT,c->deadline))continue;return failure(c->response,"Response timed out");}
        if(c->tls)return tls_failure(c,rc);
        return failure(c->response,"Connection ended unexpectedly");
    }
}
static int read_line(struct reader *r, char *out, size_t capacity, size_t *budget) {
    size_t used=0;
    for(;;) {
        if(r->pos==r->len) {int rc=fill(r);if(rc<=0)return rc<0?-1:failure(r->conn->response,"Truncated HTTP headers");}
        unsigned char c=r->data[r->pos++];if(!*budget)return failure(r->conn->response,"HTTP headers exceed 64 KiB");(*budget)--;
        if(c=='\n') {if(!used||out[used-1]!='\r')return failure(r->conn->response,"Malformed HTTP line ending");out[used-1]='\0';return 0;}
        if(c==0 || (c<32 && c!='\r' && c!='\t') || used+1>=capacity)return failure(r->conn->response,"Malformed or oversized HTTP header");
        if(used && out[used-1]=='\r')return failure(r->conn->response,"Malformed HTTP carriage return");
        out[used++]=(char)c;
    }
}
static int copy_body(struct reader *reader, size_t count, bool until_eof) {
    struct cb_response *r=reader->conn->response;
    while(count || until_eof) {
        if(reader->pos==reader->len) {int rc=fill(reader);if(rc<0)return -1;if(!rc)return until_eof?0:failure(r,"Truncated HTTP body");}
        size_t n=reader->len-reader->pos;if(!until_eof&&n>count)n=count;
        if(n>reader->conn->body_limit-r->length)return failure(r,"Response exceeds the %zu MiB body limit",reader->conn->body_limit/(1024u*1024u));
        memcpy(r->body+r->length,reader->data+reader->pos,n);r->length+=n;reader->pos+=n;r->body[r->length]='\0';if(!until_eof)count-=n;
        if(reader->conn->progress_socket>=0&&(r->length-reader->conn->progress>=262144||r->length==reader->conn->total)){
            struct cb_message m={.type=CB_HTTP_PROGRESS,.a=r->length,.b=reader->conn->total,.fd=-1};cb_send(reader->conn->progress_socket,&m);reader->conn->progress=r->length;
        }
    }return 0;
}
static int response_read(struct connection *c, char location[4096]) {
    struct reader reader={.conn=c};struct cb_response *r=c->response;char line[LINE_LIMIT];size_t budget=HEADER_LIMIT;bool chunked=false,have_length=false;size_t length=0;
    for(unsigned interim=0;interim<5;interim++) {
        if(read_line(&reader,line,sizeof line,&budget))return -1;
        if(strncmp(line,"HTTP/1.1 ",9)&&strncmp(line,"HTTP/1.0 ",9))return failure(r,"Unsupported HTTP response");
        if(strlen(line)<12||!isdigit((unsigned char)line[9])||!isdigit((unsigned char)line[10])||!isdigit((unsigned char)line[11])||(line[12]&&line[12]!=' '))return failure(r,"Invalid HTTP status");
        r->status=(line[9]-'0')*100+(line[10]-'0')*10+line[11]-'0';if(r->status<100)return failure(r,"Invalid HTTP status");
        chunked=have_length=false;length=0;location[0]='\0';r->content_type[0]='\0';r->policy[0]=0;memset(&r->headers,0,sizeof r->headers);r->headers.version=CB_HTTP_VERSION;
        while(1) {
            if(read_line(&reader,line,sizeof line,&budget))return -1;
            if(!*line)break;
            char *colon=strchr(line,':');if(!colon||colon==line||line[0]==' '||line[0]=='\t')return failure(r,"Malformed HTTP header");*colon='\0';char *value=colon+1;while(*value==' '||*value=='\t')value++;char *end=value+strlen(value);while(end>value&&(end[-1]==' '||end[-1]=='\t'))*--end='\0';
            if(!strcasecmp(line,"content-length")) {
                if(!*value)return failure(r,"Invalid Content-Length");
                size_t parsed=0;for(char *p=value;*p;p++) {if(!isdigit((unsigned char)*p)||parsed>c->body_limit/10u)return failure(r,"Invalid or oversized Content-Length");parsed=parsed*10u+(unsigned)(*p-'0');if(parsed>c->body_limit)return failure(r,"Response exceeds the %zu MiB body limit",c->body_limit/(1024u*1024u));}
                if(have_length&&length!=parsed)return failure(r,"Conflicting Content-Length headers");
                have_length=true;length=parsed;
            } else if(!strcasecmp(line,"transfer-encoding")) {if(chunked||strcasecmp(value,"chunked"))return failure(r,"Unsupported Transfer-Encoding");chunked=true;}
            else if(!strcasecmp(line,"content-encoding")) {if(*value&&strcasecmp(value,"identity"))return failure(r,"Server ignored identity encoding request");}
            else if(!strcasecmp(line,"content-type")) {
                if(strlen(value)>=sizeof r->content_type)return failure(r,"Content-Type exceeds the 127-byte limit");
                for(const unsigned char *p=(const unsigned char *)value;*p;p++)
                    if((*p<32&&*p!='\t')||*p>126)return failure(r,"Content-Type contains invalid characters");
                if(r->content_type[0]&&strcasecmp(r->content_type,value))return failure(r,"Conflicting Content-Type headers");
                memcpy(r->content_type,value,strlen(value)+1);
            }
            else if(!strcasecmp(line,"content-security-policy")) {
                size_t used=strlen(r->policy),extra=strlen(value);
                if(used+extra+2>sizeof r->policy)return failure(r,"Content Security Policy exceeds the supported limit");
                if(used)r->policy[used++]='\n';
                memcpy(r->policy+used,value,extra+1);
            }
            else if(!strcasecmp(line,"set-cookie")) {
                if(r->headers.cookie_count<CB_COOKIE_LINES&&strlen(value)<4096)
                    strcpy(r->headers.cookies[r->headers.cookie_count++],value);
            }
            else if(!strcasecmp(line,"etag")||!strcasecmp(line,"last-modified")||!strcasecmp(line,"cache-control")||
                    !strcasecmp(line,"vary")||!strcasecmp(line,"access-control-allow-origin")||
                    !strcasecmp(line,"access-control-allow-credentials")||!strcasecmp(line,"content-disposition")) {
                char *out=NULL;size_t cap=0;
#define HEADER(name,field) if(!strcasecmp(line,name)){out=r->headers.field;cap=sizeof r->headers.field;}
                HEADER("etag",etag) HEADER("last-modified",modified) HEADER("cache-control",cache_control)
                HEADER("vary",vary) HEADER("access-control-allow-origin",allow_origin)
                HEADER("access-control-allow-credentials",allow_credentials) HEADER("content-disposition",disposition)
#undef HEADER
                if(strlen(value)>=cap)return failure(r,"Response metadata exceeds supported limit");
                if(*out&&(!strcasecmp(line,"cache-control")||!strcasecmp(line,"vary"))){size_t used=strlen(out);if(used+strlen(value)+2>=cap)return failure(r,"Response metadata exceeds supported limit");out[used++]=',';strcpy(out+used,value);}
                else if(*out&&strcmp(out,value))return failure(r,"Conflicting response metadata");
                else strcpy(out,value);
            }
            else if(!strcasecmp(line,"location")) {if(strlen(value)>=4096)return failure(r,"Redirect URL too long");snprintf(location,4096,"%s",value);}
        }
        if(r->status>=200)break;
        if(r->status==101||interim==4)return failure(r,"Unsupported HTTP protocol upgrade or interim responses");
    }
    if(chunked&&have_length)return failure(r,"Ambiguous HTTP message framing");
    if(r->status==204||r->status==304||((r->status==301||r->status==302||r->status==303||r->status==307||r->status==308)&&*location))return 0;
    c->total=have_length?length:0;
    r->body=malloc(c->body_limit+1u);if(!r->body)return failure(r,"Not enough memory for response");r->body[0]='\0';
    if(chunked) {
        for(;;) {
            size_t chunk_budget=1024;if(read_line(&reader,line,1024,&chunk_budget))return -1;
            char *extension=strchr(line,';');if(extension)*extension='\0';if(!*line)return failure(r,"Invalid chunk length");size_t n=0;
            for(char *p=line;*p;p++) {int digit=isdigit((unsigned char)*p)?*p-'0':tolower((unsigned char)*p)>='a'&&tolower((unsigned char)*p)<='f'?tolower((unsigned char)*p)-'a'+10:-1;if(digit<0||n>c->body_limit/16u)return failure(r,"Invalid or oversized chunk length");n=n*16u+(unsigned)digit;if(n>c->body_limit-r->length)return failure(r,"Response exceeds the %zu MiB body limit",c->body_limit/(1024u*1024u));}
            if(!n) {do {if(read_line(&reader,line,sizeof line,&budget))return -1;}while(*line);break;}
            if(copy_body(&reader,n,false))return -1;
            chunk_budget=2;if(read_line(&reader,line,sizeof line,&chunk_budget)||*line)return failure(r,"Invalid chunk ending");
        }return 0;
    }
    return copy_body(&reader,length,!have_length);
}
int cb_fetch_restricted(const char *url,const char *initiator,const char *policy,unsigned kind,struct cb_response *r);
void cb_response_free(struct cb_response *r) {if(r) {free(r->body);memset(r,0,sizeof *r);}}
static int fetch(const char *url, struct cb_response *r, bool resource, bool require_tls, const char *initiator, const char *policy, unsigned kind,const struct cb_http_request *request_options,int progress_socket) {
    if(!r)return -1;
    memset(r,0,sizeof *r);struct cb_http_url u;if(cb_http_parse(url,&u))return failure(r,"Only valid HTTP(S) URLs without credentials are supported");
    char current[4096];if(cb_http_format(&u,current))return failure(r,"URL too long");int64_t deadline=now_ms()+20000;
    for(unsigned redirects=0;redirects<=5;redirects++) {
        snprintf(r->url,sizeof r->url,"%s",current);r->length=0;r->status=0;free(r->body);r->body=NULL;
        if(kind&&initiator&&policy&&!cb_policy_allows(policy,initiator,current,kind,0))return failure(r,"Content Security Policy blocked this request");
        r->policy[0]=0;memset(&r->headers,0,sizeof r->headers);r->headers.version=CB_HTTP_VERSION;
        if(require_tls&&!u.tls)return failure(r,"Mixed content refused: HTTPS documents require HTTPS resources");
        struct connection conn;connection_init(&conn,r,deadline);conn.body_limit=request_options&&request_options->download?CB_DOWNLOAD_LIMIT:CB_MAX_BODY;conn.progress_socket=request_options&&request_options->download?progress_socket:-1;int result=connection_open(&conn,&u);char location[4096]="";
        if(!result) {
            char host[280],request[12288];if(u.port==(u.tls?443u:80u))snprintf(host,sizeof host,"%s",u.host);else snprintf(host,sizeof host,"%s:%u",u.host,u.port);
            const struct cb_http_request *q=request_options;
            int n=snprintf(request,sizeof request,"%s %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: cube-browser/" CB_VERSION_SERIES "\r\nAccept: %s\r\nAccept-Encoding: identity\r\nConnection: close\r\n",q&&q->post?"POST":"GET",u.path,host,resource?"*/*":"text/html,text/plain;q=0.9,*/*;q=0.5");
            if(q&&n>0&&n<(int)sizeof request){
                char origin[320];
                if((q->post||q->kind==CB_POLICY_CONNECT)&&!cb_origin_header(q->initiator,origin))
                    n+=snprintf(request+n,sizeof request-(size_t)n,"Origin: %s\r\n",origin);
                if(n<(int)sizeof request&&*q->cookie)n+=snprintf(request+n,sizeof request-(size_t)n,"Cookie: %s\r\n",q->cookie);
                if(n<(int)sizeof request&&*q->etag)n+=snprintf(request+n,sizeof request-(size_t)n,"If-None-Match: %s\r\n",q->etag);
                if(n<(int)sizeof request&&*q->modified)n+=snprintf(request+n,sizeof request-(size_t)n,"If-Modified-Since: %s\r\n",q->modified);
                if(n<(int)sizeof request&&q->post)n+=snprintf(request+n,sizeof request-(size_t)n,"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: %u\r\n",q->length);
            }
            if(n<0||n+2>=(int)sizeof request)result=failure(r,"Request too large");
            else{memcpy(request+n,"\r\n",2);n+=2;result=send_request(&conn,request,(size_t)n);
                if(!result&&q&&q->post&&q->length)result=send_request(&conn,q->body,q->length);
                if(!result)result=response_read(&conn,location);
            }

        }
        connection_free(&conn);if(result)return -1;
        r->headers.status=r->status;strcpy(r->headers.mime,r->content_type);strcpy(r->headers.policy,r->policy);strcpy(r->headers.location,location);
        if(request_options){if(!r->body)r->body=calloc(1,1);return r->body?0:failure(r,"Response allocation failed");}
        if((r->status==301||r->status==302||r->status==303||r->status==307||r->status==308)&&*location) {
            if(redirects==5)return failure(r,"Too many redirects (limit 5)");
            char next[4096];if(cb_url_resolve(current,location,next))return failure(r,"Invalid redirect URL");struct cb_http_url target;if(cb_http_parse(next,&target))return failure(r,"Invalid redirect URL");
            if(require_tls&&!target.tls)return failure(r,"Mixed content redirect refused: HTTPS documents require HTTPS resources");
            if(u.tls&&!target.tls)return failure(r,"HTTPS to HTTP redirect refused");
            /* RFC 9110 section10.2.2: a redirect lacking a fragment inherits
             * the request-reference fragment. An explicit empty '#' overrides it. */
            if(!strchr(location,'#')&&u.has_fragment){
                target.has_fragment=true;snprintf(target.fragment,sizeof target.fragment,"%s",u.fragment);
                if(cb_http_format(&target,next))return failure(r,"Redirect URL with inherited fragment is too long");
            }
            u=target;snprintf(current,sizeof current,"%s",next);continue;
        }
        if(resource&&(r->status<200||r->status>299))return failure(r,"Resource request failed with HTTP %d",r->status);
        if(!r->body) {r->body=calloc(1,1);if(!r->body)return failure(r,"Not enough memory for response");}
        return 0;
    }return failure(r,"Too many redirects");
}
int cb_fetch(const char *url, struct cb_response *r) {
    return fetch(url,r,false,false,NULL,NULL,0,NULL,-1);
}
int cb_fetch_resource(const char *url, const char *initiator, struct cb_response *r) {
    if(!r)return -1;
    struct cb_http_url document;
    if(cb_http_parse(initiator,&document)) {
        memset(r,0,sizeof *r);
        return failure(r,"Resource initiator must be a valid HTTP(S) URL without credentials");
    }
    return fetch(url,r,true,document.tls,initiator,NULL,0,NULL,-1);
}

int cb_fetch_restricted(const char *url,const char *initiator,const char *policy,unsigned kind,struct cb_response *r) {
    struct cb_http_url document;
    if(cb_http_parse(initiator,&document)){memset(r,0,sizeof *r);return failure(r,"Invalid resource initiator");}
    return fetch(url,r,true,document.tls,initiator,policy,kind,NULL,-1);
}

#ifdef CB_NET_STANDALONE
int main(int argc, char **argv) {
    if(argc==4&&!strcmp(argv[1],"--resolve")) {char url[4096];if(cb_url_resolve(argv[2],argv[3],url))return 1;puts(url);return 0;}
    if(argc!=3||strcmp(argv[1],"--fetch")) {fprintf(stderr,"Usage: %s --fetch URL | --resolve BASE REFERENCE\n",argv[0]);return 2;}
    struct cb_response r;int rc=cb_fetch(argv[2],&r);if(rc)fprintf(stderr,"%s\n",r.error);else {fprintf(stderr,"HTTP %d %s (%zu bytes)\n",r.status,r.content_type,r.length);if(r.length&&fwrite(r.body,1,r.length,stdout)!=r.length)rc=-1;}cb_response_free(&r);return rc?1:0;
}
#endif

int cb_fetch_request_progress(const struct cb_http_request *q,struct cb_response *r,int progress_socket){
    memset(r,0,sizeof *r);if(!cb_http_request_valid(q))return failure(r,"Invalid bounded HTTP request");
    struct cb_http_url document;bool tls=*q->initiator&&!cb_http_parse(q->initiator,&document)&&document.tls;
    return fetch(q->url,r,q->kind!=0,tls,*q->initiator?q->initiator:NULL,*q->initiator?q->policy:NULL,q->kind,q,progress_socket);
}

int cb_fetch_request(const struct cb_http_request *q,struct cb_response *r){return cb_fetch_request_progress(q,r,-1);}
