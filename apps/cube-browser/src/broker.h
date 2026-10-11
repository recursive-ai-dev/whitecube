#ifndef CB_BROKER_H
#define CB_BROKER_H
#include "session.h"
/* Storage is disclosed only to an enabled HTTP(S), non-opaque document. */
char *cb_broker_storage(struct cb_session *session,const char *url,const char *policy,bool enabled);
#include "ipc.h"
struct cb_http_job {
    struct cb_http_request request;
    struct cb_http_metadata meta;
    char site[4096], error[256];
    unsigned received,total;double started;
    unsigned redirects,credentials; /* 0 omit, 1 same-origin, 2 cookie policy */
    bool top,have_meta,cross_site;
};
struct cb_http_job *cb_broker_new(const char *url,const char *site,const char *initiator,const char *policy,unsigned kind,bool top,const char *body);
int cb_broker_send(struct cb_http_job *job,struct cb_session *session,int socket);
/* 0 metadata consumed, 1 final legacy DOCUMENT, 2 redirect needs new worker, -1 failure. */
int cb_broker_receive(struct cb_http_job *job,struct cb_session *session,struct cb_message *message);
#endif
