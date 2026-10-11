#include "js.h"
#include "ipc.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
static unsigned errors,logs;
static void output(void *data,unsigned level,const char *message){(void)data;assert(message);if(level==2)errors++;else logs++;}
static void run(struct cb_js *j,const char *source,const char *expected){char *out=NULL;assert(!cb_js_eval(j,source,strlen(source),"test.js",&out));assert(out&&!strcmp(out,expected));free(out);}
int main(void){
    /* ASan's signal/proc hooks cannot run under the production syscall filter.
     * Normal builds exercise the actual sandbox; only this test has an override. */
#ifndef CB_COMPONENT_SANITIZER
    assert(!cb_renderer_restrict());
#endif
    struct cb_js *j=cb_js_new((struct cb_js_host){.log=output},1);assert(j);
    run(j,"[1,2,3].map(x=>x*x).reduce((a,b)=>a+b,0)","14");
    run(j,"class A{#x=7;get x(){return this.#x}};new A().x","7");
    run(j,"String(2n**64n)","\"18446744073709551616\"");
    run(j,"new Map([['a',1]]).get('a')+new Set([1,1]).size","2");
    run(j,"JSON.stringify({text:'猫',array:[true,null]})","\"{\\\"text\\\":\\\"猫\\\",\\\"array\\\":[true,null]}\"");
    run(j,"/^(?<word>\\p{L}+)$/u.exec('café').groups.word","\"café\"");
    run(j,"new Uint8Array([255,1]).reduce((a,b)=>a+b)","256");
    run(j,"let done=0;Promise.resolve(4).then(v=>done=v);done","0");assert(!cb_js_jobs(j));run(j,"done","4");
    run(j,"typeof std+':'+typeof os+':'+typeof process+':'+typeof SharedArrayBuffer","\"undefined:undefined:undefined:undefined\"");
    run(j,"console.log('bounded console');true","true");assert(logs==1);
    assert(cb_js_eval(j,"throw new Error('expected')",strlen("throw new Error('expected')"),"error.js",NULL));assert(errors);assert(cb_js_memory(j)<32u*1024u*1024u);cb_js_free(j);
    j=cb_js_new((struct cb_js_host){.log=output},0);assert(j);
    assert(cb_js_eval(j,"eval('1')",strlen("eval('1')"),"eval.js",NULL));assert(cb_js_eval(j,"(()=>{}).constructor('return 1')()",strlen("(()=>{}).constructor('return 1')()"),"eval.js",NULL));
    run(j,"1+1","2");assert(cb_js_eval(j,"while(true){}",strlen("while(true){}"),"runaway.js",NULL));assert(cb_js_failed(j));cb_js_free(j);
    j=cb_js_new((struct cb_js_host){.log=output},1);assert(j);assert(!cb_js_eval(j,"function loop(){Promise.resolve().then(loop)}loop()",strlen("function loop(){Promise.resolve().then(loop)}loop()"),"jobs.js",NULL));assert(cb_js_jobs(j));assert(cb_js_failed(j));cb_js_free(j);
    j=cb_js_new((struct cb_js_host){.log=output},1);assert(j);const char *memory="let a=[];for(let i=0;i<10000;i++)a.push(new Uint8Array(1024*1024));";assert(cb_js_eval(j,memory,strlen(memory),"memory.js",NULL));cb_js_free(j);
    puts("bounded QuickJS language, jobs, exceptions, memory and dynamic-code policy passed");
}
