#ifndef CB_JS_H
#define CB_JS_H
#include <stddef.h>
/* Replaceable interpreter boundary. Host calls use bounded UTF-8 strings and a
 * JSON result, never engine pointers/handles or QuickJS types. */
struct cb_js;
struct cb_js_host {
    void *data;
    char *(*call)(void *data,unsigned operation,size_t argc,const char *const *argv);
    void (*log)(void *data,unsigned level,const char *message);
};
struct cb_js *cb_js_new(struct cb_js_host host,int allow_eval);
void cb_js_free(struct cb_js *js);
int cb_js_eval(struct cb_js *js,const char *source,size_t length,const char *url,char **json_result);
int cb_js_call(struct cb_js *js,const char *function,const char *arguments_json,char **json_result);
/* Runs at most 256 jobs/100ms. A runaway job queue disables this document's JS. */
int cb_js_jobs(struct cb_js *js);
int cb_js_failed(const struct cb_js *js);
size_t cb_js_memory(struct cb_js *js);
#endif
