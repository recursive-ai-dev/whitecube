#include "engine-internal.h"
#include "dom.h"
#include <assert.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

static void drain(int socket){
    struct pollfd p={socket,POLLIN,0};
    while(poll(&p,1,0)>0){struct cb_message m;assert(cb_recv(socket,&m)==1);
        if(m.type==CB_CONSOLE&&m.a==2){fprintf(stderr,"DOM exception: %s\n",m.text);assert(0);}
        if(m.fd>=0)close(m.fd);
    }
}
static void fragments(void){
    const char *html="<body><div id=out>original</div><script>"
        "const out=document.getElementById('out');"
        "out.innerHTML='<p id=first>A &amp; B</p><input id=field value=ready>';"
        "if(out.firstChild.textContent!=='A & B'||document.getElementById('field').value!=='ready')throw Error('fragment parse');"
        "if(!out.innerHTML.includes('A &amp; B'))throw Error('fragment serialize');"
        "const old=out.firstChild;out.innerHTML='<span id=next>next</span>';"
        "if(old.isConnected||old.textContent!=='A & B')throw Error('fragment lifetime');"
        "out.firstChild.insertAdjacentHTML('afterend','<b>after</b>');"
        "if(out.lastChild.tagName!=='B')throw Error('adjacent');"
        "const detached=document.createElement('p');detached.className='test';"
        "if(!detached.matches('.test')||detached.closest('p')!==detached)throw Error('detached matches');"
        "let blocked=0;try{out.innerHTML='<div>'.repeat(130)}catch(e){blocked++}"
        "if(!blocked||out.textContent!=='nextafter')throw Error('atomic depth quota');"
        "out.textContent='FRAGMENTS_OK';"
        "</script></body>";
    int sockets[2];assert(!socketpair(AF_UNIX,SOCK_SEQPACKET,0,sockets));
    struct cb_page *page=cb_page_parse(html,strlen(html),"https://example.org/");assert(page);
    struct cb_dom *dom=cb_dom_new(page,sockets[0],"[{},{}]",1);assert(dom);drain(sockets[1]);
    cb_dom_tick(dom);drain(sockets[1]);cb_page_layout(page,640,1);
    char *text=cb_page_text(page);assert(strstr(text,"FRAGMENTS_OK"));free(text);
    cb_dom_free(dom);cb_page_free(page);close(sockets[0]);close(sockets[1]);
}
static void scrolling_images(void){
    const char *html="<body style='margin:0'><div id=near style='height:100px;width:100px'></div>"
        "<div style='height:1000px'></div><img id=far width=100 height=80 data-src='/lazy.png'><p id=out>initial</p><script>"
        "const far=document.getElementById('far'),out=document.getElementById('out');let changes=[],seen=[];"
        "window.addEventListener('scroll',()=>changes.push('scroll:'+scrollY),null);"
        "const media=matchMedia('(min-width:600px)');media.addListener(e=>changes.push('media:'+e.matches));"
        "const observer=new IntersectionObserver((entries,owner)=>{for(const e of entries){"
        "seen.push(e.target.id+':'+e.isIntersecting);"
        "if(e.isIntersecting&&e.target===far){"
        "if(innerWidth!==320||innerHeight!==200||pageYOffset!==1000||far.getBoundingClientRect().top!==100)throw Error('CSS pixel geometry');"
        "far.style.width='50%';if(getComputedStyle(far).width!=='160px'||far.getBoundingClientRect().width!==160)throw Error('resolved replaced width');"
        "for(const key of ['constructor','__proto__','toString'])if(getComputedStyle(far).getPropertyValue(key)!==''||new DOMException('',key).code!==0)throw Error('prototype lookup');"
        "far.src=far.dataset.src;owner.unobserve(far);out.textContent=seen.join(',')+' '+changes.join(',');"
        "}}},{threshold:[0,0.5,1]});"
        "observer.observe(document.getElementById('near'));observer.observe(far);"
        "</script></body>";
    int sockets[2];assert(!socketpair(AF_UNIX,SOCK_SEQPACKET,0,sockets));
    struct cb_page *page=cb_page_parse(html,strlen(html),"https://example.org/");assert(page);
    struct cb_dom *dom=cb_dom_new(page,sockets[0],"[{},{}]",1);assert(dom);drain(sockets[1]);
    cb_page_set_environment(page,400,0,0);cb_page_layout(page,640,1);assert(!cb_dom_viewport(dom,0));drain(sockets[1]);
    assert(!cb_page_resource_count(page));
    cb_page_layout(page,640,2);assert(cb_dom_viewport(dom,2000));drain(sockets[1]);
    assert(cb_page_resource_count(page)==1);int kind;const char *url;
    assert(!cb_page_resource(page,0,&kind,&url)&&kind==CB_RESOURCE_IMAGE&&!strcmp(url,"https://example.org/lazy.png"));
    cb_page_layout(page,640,2);char *text=cb_page_text(page);assert(strstr(text,"near:true,far:false,near:false,far:true scroll:1000,media:false"));free(text);
    assert(!cb_dom_viewport(dom,2000));drain(sockets[1]);assert(cb_page_resource_count(page)==1);
    cb_dom_free(dom);cb_page_free(page);close(sockets[0]);close(sockets[1]);
}
static void observer_records_and_live_metadata(void){
    const char *html="<body style='margin:0'><div id=edge style='height:20px;width:100px;position:absolute;top:400px'></div>"
        "<div id=point style='height:0;width:10px;position:absolute;top:100px'></div><p id=out>initial</p><script>"
        "const out=document.getElementById('out');let callbacks=0;"
        "const second=new IntersectionObserver(()=>{throw Error('drained records delivered')});"
        "const first=new IntersectionObserver(entries=>{"
        "if(entries.length!==1||!entries[0].isIntersecting||entries[0].intersectionRatio!==0)throw Error('edge intersection');"
        "const records=second.takeRecords();if(records.length!==1||!records[0].isIntersecting||records[0].intersectionRatio!==1)throw Error('zero area');"
        "if(second.takeRecords().length)throw Error('records repeated');second.disconnect();first.disconnect();callbacks++;out.textContent='OBSERVER_RECORDS_OK';"
        "});first.observe(document.getElementById('edge'));second.observe(document.getElementById('point'));"
        "window.addEventListener('resize',()=>{if(!callbacks)return;if(callbacks!==1||location.hash!=='#new'||location.search!=='?q=1'||location.port!==''||location.host!=='example.org'||location.origin!=='https://example.org')throw Error('live location');"
        "if(!matchMedia('(resolution:2dppx)').matches||getComputedStyle(document.getElementById('edge')).width!=='100px')throw Error('zoom units');out.textContent='LIVE_METADATA_OK';});"
        "</script></body>";
    int sockets[2];assert(!socketpair(AF_UNIX,SOCK_SEQPACKET,0,sockets));
    struct cb_page *page=cb_page_parse(html,strlen(html),"https://example.org:443/page?q=1#old");assert(page);
    struct cb_dom *dom=cb_dom_new(page,sockets[0],"[{},{}]",1);assert(dom);drain(sockets[1]);
    cb_page_set_environment(page,400,0,0);cb_page_layout(page,640,1);cb_dom_viewport(dom,0);drain(sockets[1]);
    char *text=cb_page_text(page);assert(strstr(text,"OBSERVER_RECORDS_OK"));free(text);
    assert(!cb_page_set_url(page,"https://example.org:443/page?q=1#new"));cb_page_layout(page,640,2);cb_dom_viewport(dom,0);drain(sockets[1]);
    text=cb_page_text(page);assert(strstr(text,"LIVE_METADATA_OK"));free(text);
    cb_dom_free(dom);cb_page_free(page);close(sockets[0]);close(sockets[1]);
}
int main(void){
    observer_records_and_live_metadata();
    fragments();
    scrolling_images();
    const char *html="<body><input id=field><button id=button>Run</button><p id=out>initial</p><script>"
        "const out=document.getElementById('out');"
        "for(let i=0;i<120;i++){let a=document.createElement('div'),b=document.createTextNode('detached '+i);a.append(b);out.append(a);out.removeChild(a);out.append(a);a.textContent='replacement';a.remove();}"
        "const chain=document.createElement('div');out.append(chain);let at=chain,blocked=0;"
        "for(let i=0;i<140;i++){let next=document.createElement('div');try{at.append(next);at=next;}catch(e){blocked++;break;}}"
        "if(!blocked)throw Error('depth bound');try{at.append(chain);throw Error('cycle allowed');}catch(e){if(e.message==='cycle allowed')throw e;}"
        "chain.remove();out.textContent='DOM_READY';"
        "document.getElementById('field').addEventListener('input',()=>document.title='INPUT_OK');"
        "document.getElementById('button').addEventListener('click',e=>{e.preventDefault();out.textContent='CLICK_OK';});"
        "localStorage.setItem('key','value');"
        "fetch('/result').then(r=>r.json()).then(v=>out.textContent=v.message);"
        "</script></body>";
    int sockets[2];assert(!socketpair(AF_UNIX,SOCK_SEQPACKET,0,sockets));
    struct cb_page *page=cb_page_parse(html,strlen(html),"https://example.org/");assert(page);
    struct cb_dom *dom=cb_dom_new(page,sockets[0],"[{},{}]",1);assert(dom);drain(sockets[1]);
    cb_dom_tick(dom);drain(sockets[1]);cb_page_layout(page,640,1);
    char *text=cb_page_text(page);assert(strstr(text,"DOM_READY"));free(text);
    struct cb_interaction interaction;
    assert(!cb_dom_interact(dom,CB_INTERACT_NEXT,0,0,"",&interaction));
    assert(!cb_dom_interact(dom,CB_INTERACT_TEXT,0,0,"猫",&interaction));
    assert(!strcmp(cb_page_title(page),"INPUT_OK"));
    assert(!cb_dom_interact(dom,CB_INTERACT_NEXT,0,0,"",&interaction));
    assert(!cb_dom_interact(dom,CB_INTERACT_KEY,0,0,"enter",&interaction));
    cb_page_layout(page,640,1);text=cb_page_text(page);assert(strstr(text,"CLICK_OK"));free(text);
    const char *json="{\"message\":\"FETCH_OK\"}";
    struct cb_message response={.type=CB_WEB_RESPONSE,.a=1,.b=200,.fd=cb_document_fd(json,strlen(json))};
    strcpy(response.text,"https://example.org/result");assert(response.fd>=0);cb_dom_response(dom,&response);close(response.fd);
    cb_dom_tick(dom);drain(sockets[1]);cb_page_layout(page,640,1);
    text=cb_page_text(page);assert(strstr(text,"FETCH_OK"));free(text);
    cb_dom_free(dom);cb_page_free(page);close(sockets[0]);close(sockets[1]);
    puts("DOM lifetime, depth/cycle bounds, controls, events and async response passed");
}
