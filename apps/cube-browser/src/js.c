#define _POSIX_C_SOURCE 200809L
#include "js.h"
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-parameter"
#include <quickjs.h>
#pragma GCC diagnostic pop
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#define SOURCE_LIMIT (1024u*1024u)
#define MEMORY_LIMIT (32u*1024u*1024u)
struct cb_js {JSRuntime *runtime;JSContext *context;struct cb_js_host host;double deadline,total;int failed,interrupted;unsigned logs,calls;};
static double clock_now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static void log_text(struct cb_js *j,unsigned level,const char *message){if(j->logs++<128&&j->host.log)j->host.log(j->host.data,level,message);}
static int interrupt(JSRuntime *runtime,void *opaque){(void)runtime;struct cb_js *j=opaque;if(j->failed||clock_now()>j->deadline){j->interrupted=1;return 1;}return 0;}
static void report(struct cb_js *j){
    JSValue value=JS_GetException(j->context);const char *text=JS_ToCString(j->context,value);char out[2048];
    snprintf(out,sizeof out,"%.2047s",j->interrupted?"JavaScript execution budget exceeded":text?text:"JavaScript exception (conversion or allocation failed)");
    log_text(j,2,out);if(text)JS_FreeCString(j->context,text);JS_FreeValue(j->context,value);
}
static JSValue console_call(JSContext *ctx,JSValueConst self,int argc,JSValueConst *argv,int level){
    (void)self;struct cb_js *j=JS_GetContextOpaque(ctx);char message[2048]="";size_t used=0;
    for(int i=0;i<argc&&i<16;i++){const char *text=JS_ToCString(ctx,argv[i]);if(!text)return JS_EXCEPTION;size_t n=strlen(text);if(n>sizeof message-used-2)n=sizeof message-used-2;
        if(i&&used<sizeof message-1)message[used++]=' ';
        memcpy(message+used,text,n);used+=n;message[used]=0;JS_FreeCString(ctx,text);if(used>=sizeof message-2)break;}
    log_text(j,(unsigned)level,message);return JS_UNDEFINED;
}
static JSValue host_call(JSContext *ctx,JSValueConst self,int argc,JSValueConst *argv){
    (void)self;struct cb_js *j=JS_GetContextOpaque(ctx);uint32_t operation=0;
    if(argc<1||argc>9||++j->calls>100000||JS_ToUint32(ctx,&operation,argv[0]))return JS_ThrowRangeError(ctx,"Host call budget exceeded");
    const char *args[8]={0};size_t total=0;int ok=1;
    for(int i=1;i<argc;i++){size_t length=0;args[i-1]=JS_ToCStringLen(ctx,&length,argv[i]);if(!args[i-1]){ok=0;break;}total+=length;if(total>65536||strlen(args[i-1])!=length){ok=0;break;}}
    char *result=ok&&j->host.call?j->host.call(j->host.data,operation,(size_t)argc-1,args):NULL;
    for(int i=1;i<argc;i++)if(args[i-1])JS_FreeCString(ctx,args[i-1]);
    if(!result)return JS_ThrowTypeError(ctx,"Unsupported or invalid host operation");
    size_t length=strlen(result);JSValue value=length<=1024*1024?JS_ParseJSON(ctx,result,length,"<host>"):JS_ThrowRangeError(ctx,"Host response too large");free(result);return value;
}
static JSModuleDef *no_module(JSContext *ctx,const char *name,void *opaque){(void)opaque;JS_ThrowReferenceError(ctx,"Modules are unsupported: %.256s",name);return NULL;}
static void rejected(JSContext *ctx,JSValueConst promise,JSValueConst reason,JS_BOOL handled,void *opaque){
    (void)promise;if(handled)return;struct cb_js *j=opaque;const char *text=JS_ToCString(ctx,reason);if(text){char out[2048];snprintf(out,sizeof out,"Unhandled promise rejection: %.2000s",text);log_text(j,2,out);JS_FreeCString(ctx,text);}
}
static double begin(struct cb_js *j){double start=clock_now();j->deadline=start+.1;j->interrupted=0;JS_UpdateStackTop(j->runtime);return start;}
static int finish(struct cb_js *j,double start,JSValue value,char **result){
    int failed=JS_IsException(value);if(failed)report(j);
    else if(result){JSValue json=JS_JSONStringify(j->context,value,JS_UNDEFINED,JS_UNDEFINED);
        if(JS_IsException(json)){report(j);failed=1;}else if(JS_IsUndefined(json))*result=strdup("null");
        else {size_t length=0;const char *text=JS_ToCStringLen(j->context,&length,json);if(text&&length<=SOURCE_LIMIT)*result=strdup(text);else failed=1;if(text)JS_FreeCString(j->context,text);}JS_FreeValue(j->context,json);}
    JS_FreeValue(j->context,value);j->total+=clock_now()-start;
    if(j->interrupted||j->total>2.0){j->failed=1;failed=1;}return failed?-1:0;
}
struct cb_js *cb_js_new(struct cb_js_host host,int allow_eval){
    struct cb_js *j=calloc(1,sizeof *j);if(!j)return NULL;j->host=host;j->runtime=JS_NewRuntime();if(!j->runtime){free(j);return NULL;}
    JS_SetMemoryLimit(j->runtime,MEMORY_LIMIT);JS_SetMaxStackSize(j->runtime,512u*1024u);JS_SetCanBlock(j->runtime,0);
    JS_SetModuleLoaderFunc(j->runtime,NULL,no_module,NULL);JS_SetHostPromiseRejectionTracker(j->runtime,rejected,j);
    j->context=JS_NewContext(j->runtime);if(!j->context){cb_js_free(j);return NULL;}
    JS_SetContextOpaque(j->context,j);j->deadline=clock_now()+.1;JS_SetInterruptHandler(j->runtime,interrupt,j);
    JSValue global=JS_GetGlobalObject(j->context),console=JS_NewObject(j->context);
    const char *names[]={"log","warn","error","info","debug"};for(int i=0;i<5;i++)JS_SetPropertyStr(j->context,console,names[i],JS_NewCFunctionMagic(j->context,console_call,names[i],1,JS_CFUNC_generic_magic,i==2?2:i==1?1:0));
    JS_SetPropertyStr(j->context,global,"console",console);
    JS_DefinePropertyValueStr(j->context,global,"__cube",JS_NewCFunction(j->context,host_call,"__cube",1),0);
    JS_FreeValue(j->context,global);
    const char *base="delete globalThis.SharedArrayBuffer;delete globalThis.Atomics;";
    if(cb_js_eval(j,base,strlen(base),"<runtime>",NULL)){cb_js_free(j);return NULL;}
    if(!allow_eval){const char *lock="(()=>{const blocked=()=>{throw new EvalError('Dynamic code is disabled by policy')};for(const fn of [function(){},function*(){},async function(){},async function*(){}])Object.defineProperty(Object.getPrototypeOf(fn),'constructor',{value:blocked,writable:false,configurable:false});Object.defineProperty(globalThis,'eval',{value:blocked,writable:false,configurable:false});Object.defineProperty(globalThis,'Function',{value:blocked,writable:false,configurable:false});})();";
        if(cb_js_eval(j,lock,strlen(lock),"<policy>",NULL)){cb_js_free(j);return NULL;}}
    return j;
}
void cb_js_free(struct cb_js *j){if(!j)return;if(j->context)JS_FreeContext(j->context);if(j->runtime)JS_FreeRuntime(j->runtime);free(j);}
int cb_js_eval(struct cb_js *j,const char *source,size_t length,const char *url,char **result){
    if(result)*result=NULL;
    if(!j||j->failed||!source||length>SOURCE_LIMIT)return -1;
    char *copy=malloc(length+1);if(!copy)return -1;memcpy(copy,source,length);copy[length]=0;
    double start=begin(j);JSValue value=JS_Eval(j->context,copy,length,url?url:"<script>",JS_EVAL_TYPE_GLOBAL);free(copy);return finish(j,start,value,result);
}
int cb_js_call(struct cb_js *j,const char *function,const char *arguments,char **result){
    if(result)*result=NULL;
    if(!j||j->failed||!arguments||strlen(arguments)>SOURCE_LIMIT)return -1;
    double start=begin(j);JSValue global=JS_GetGlobalObject(j->context),fn=JS_GetPropertyStr(j->context,global,function);
    JSValue args=JS_ParseJSON(j->context,arguments,strlen(arguments),"<event>"),values[8];uint32_t count=0;
    int array=!JS_IsException(args)&&JS_IsArray(j->context,args);
    if(array){JSValue length=JS_GetPropertyStr(j->context,args,"length");JS_ToUint32(j->context,&count,length);JS_FreeValue(j->context,length);}
    JSValue value;
    if(!array||count>8||!JS_IsFunction(j->context,fn))value=JS_ThrowTypeError(j->context,"Invalid callback arguments");
    else{for(unsigned i=0;i<count;i++)values[i]=JS_GetPropertyUint32(j->context,args,i);value=JS_Call(j->context,fn,global,count,values);for(unsigned i=0;i<count;i++)JS_FreeValue(j->context,values[i]);}
    JS_FreeValue(j->context,args);JS_FreeValue(j->context,fn);JS_FreeValue(j->context,global);return finish(j,start,value,result);
}
int cb_js_jobs(struct cb_js *j){
    if(!j||j->failed)return -1;
    double start=begin(j);JSContext *ctx=NULL;unsigned count=0;int rc=0;
    while(JS_IsJobPending(j->runtime)&&count++<256){rc=JS_ExecutePendingJob(j->runtime,&ctx);if(rc<0){report(j);break;}}
    j->total+=clock_now()-start;
    if(JS_IsJobPending(j->runtime)||j->interrupted||j->total>2){j->failed=1;log_text(j,2,"JavaScript job budget exceeded");return -1;}return rc<0?-1:0;
}
int cb_js_failed(const struct cb_js *j){return !j||j->failed;}
size_t cb_js_memory(struct cb_js *j){if(!j)return 0;JSMemoryUsage usage;JS_ComputeMemoryUsage(j->runtime,&usage);return (size_t)usage.memory_used_size;}
