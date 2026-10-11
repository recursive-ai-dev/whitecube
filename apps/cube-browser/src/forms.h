#ifndef CB_FORMS_H
#define CB_FORMS_H
#include <stddef.h>

#define CB_FORM_MAX_PAIRS 128u
#define CB_FORM_MAX_NAME 1024u
#define CB_FORM_MAX_VALUE 4096u
#define CB_FORM_MAX_URL 4095u

struct cb_form_pair { const char *name, *value; };

/* Serialize already-selected successful controls, preserving order/duplicates.
 * UTF-8 application/x-www-form-urlencoded, GET only. No I/O. Returns 0 on
 * success, -1 with a bounded error on failure; failure leaves out empty. */
int cb_form_get_url(const char *document_url, const char *action,
                    const struct cb_form_pair *pairs, size_t count,
                    char *out, size_t out_size, char *error, size_t error_size);
int cb_form_post(const char *document_url,const char *action,const struct cb_form_pair *pairs,size_t count,char *out,size_t out_size,char *body,size_t body_size,char *error,size_t error_size);
#endif
