#ifndef CB_URL_H
#define CB_URL_H
#include <stddef.h>
int cb_url_input(const char *input,const char *search,char *out,size_t cap);
int cb_url_remote(const char *url);
/* Ignore fragments after normalizing URI components. The second argument may
 * be a fragment-only reference, including '#'. Invalid URLs compare false. */
int cb_url_same_document(const char *a,const char *b);
char *cb_html_escape(const char *text);
#endif
