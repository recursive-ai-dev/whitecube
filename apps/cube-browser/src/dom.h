#ifndef CB_DOM_H
#define CB_DOM_H
#include "engine.h"
#include "ipc.h"
struct cb_dom;
struct cb_dom *cb_dom_new(struct cb_page *page,int socket,const char *storage,int allow_eval);
void cb_dom_free(struct cb_dom *dom);
void cb_dom_tick(struct cb_dom *dom);
void cb_dom_resource(struct cb_dom *dom);
void cb_dom_response(struct cb_dom *dom,const struct cb_message *message);
int cb_dom_interact(struct cb_dom *dom,unsigned action,double x,double y,const char *text,struct cb_interaction *result);
#define CB_STORAGE_VALUE 16384
struct cb_storage_write {unsigned version,local,operation;char key[1025],value[CB_STORAGE_VALUE+1];};
#endif
