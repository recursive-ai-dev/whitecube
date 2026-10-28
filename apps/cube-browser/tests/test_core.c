#define _GNU_SOURCE
#include "ipc.h"
#include "url.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
int main(void){
    int f[2];assert(!socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0,f));
    const char *html="<h1>Bounded document</h1>";int fd=cb_document_fd(html,strlen(html));assert(fd>=0);
    struct cb_message m={.type=CB_DOCUMENT,.a=4096,.b=257,.c=0xffffffff,.fd=fd};strcpy(m.text,"https://example.com/é");
    assert(!cb_send(f[0],&m));close(fd);struct cb_message got;assert(cb_recv(f[1],&got)==1);
    assert(got.type==m.type&&got.a==m.a&&got.b==m.b&&got.c==m.c&&!strcmp(got.text,m.text)&&got.fd>=0);
    char *body=NULL;size_t size;assert(!cb_read_document(got.fd,&body,&size)&&size==strlen(html)&&!strcmp(body,html));free(body);
    assert(write(got.fd,"x",1)<0&&errno==EPERM);assert(fcntl(got.fd,F_GETFD)&FD_CLOEXEC);close(got.fd);
    unsigned char malformed[]={0,0,0,1,0x80};assert(send(f[0],malformed,sizeof(malformed),0)==sizeof(malformed));assert(cb_recv(f[1],&got)<0&&errno==EPROTO);
    close(f[0]);assert(cb_recv(f[1],&got)==0);close(f[1]);
    char url[4096];const char *search="https://duckduckgo.com/html/?q=%s";
    assert(!cb_url_input("cube:start",search,url,sizeof(url))&&!strcmp(url,"cube:home"));
    assert(!cb_url_input("cube:bookmarks",search,url,sizeof(url))&&!strcmp(url,"cube:bookmarks"));
    assert(!cb_url_input("example.com",search,url,sizeof(url))&&!strcmp(url,"https://example.com"));
    assert(!cb_url_input(" localhost:8123/a ",search,url,sizeof(url))&&!strcmp(url,"http://localhost:8123/a"));
    assert(!cb_url_input("hello & world",search,url,sizeof(url))&&!strcmp(url,"https://duckduckgo.com/html/?q=hello%20%26%20world"));
    assert(cb_url_input("javascript:alert(1)",search,url,sizeof(url))<0);
    assert(cb_url_input("https://user:password@example.com",search,url,sizeof(url))<0);
    assert(cb_url_input("https://example.com/\rInjected",search,url,sizeof(url))<0);
    assert(cb_url_input("data:text/html,<h1>x</h1>",search,url,sizeof(url))<0);
    assert(cb_url_input("https://",search,url,sizeof(url))<0);
    assert(cb_url_input("https://example.com:/",search,url,sizeof(url))<0);
    assert(cb_url_input("https://example.com:0/",search,url,sizeof(url))<0);
    assert(cb_url_input("https://example.com:65536/",search,url,sizeof(url))<0);
    assert(cb_url_input("https://example.com%2Fevil/",search,url,sizeof(url))<0);
    assert(cb_url_input("https://example.com/a\\b",search,url,sizeof(url))<0);
    assert(cb_url_input("https://example.com/\n",search,url,sizeof(url))<0);
    assert(cb_url_input("https://example.com/%zz",search,url,sizeof(url))<0);
    assert(!cb_url_input("https://bücher.example/été",search,url,sizeof(url))&&!strcmp(url,"https://xn--bcher-kva.example/%C3%A9t%C3%A9"));
    assert(cb_url_same_document("https://EXAMPLE.org:443/a/../page?q=1#old","https://example.org/page?q=1#new"));
    assert(cb_url_same_document("https://example.org","https://example.org/#top"));
    assert(cb_url_same_document("https://example.org/p?q#old","#"));
    assert(cb_url_same_document("https://example.org/p?q#old","#another"));
    assert(cb_url_same_document("https://example.org/p","https://example.org/p"));
    assert(cb_url_same_document("file:///tmp/a.html#one","file:///tmp/a.html#two"));
    assert(cb_url_same_document("cube:home#one","cube:home#two"));
    assert(!cb_url_same_document("https://example.org/p?q=1","https://example.org/p?q=2#x"));
    assert(!cb_url_same_document("https://example.org/p","https://example.org/p?#x"));
    assert(!cb_url_same_document("https://example.org/p","http://example.org/p#x"));
    assert(!cb_url_same_document("https://example.org/p","https://other.example/p#x"));
    assert(!cb_url_same_document("https://example.org/p","https://example.org:444/p#x"));
    assert(!cb_url_same_document("https://example.org/p","https://user@example.org/p#x"));
    assert(!cb_url_same_document("https://example.org/p","https://example.org:/p#x"));
    assert(!cb_url_same_document("javascript:alert(1)","javascript:alert(1)#x"));
    assert(!cb_url_same_document("https://example.org/p",NULL));
    char *escaped=cb_html_escape("<a x=\"'&\">");assert(!strcmp(escaped,"&lt;a x=&quot;&apos;&amp;&quot;&gt;"));free(escaped);
    puts("IPC framing, sealed descriptors and address parsing passed");return 0;
}
