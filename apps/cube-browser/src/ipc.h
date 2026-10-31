#ifndef CB_IPC_H
#define CB_IPC_H
#include <stddef.h>
#include <stdint.h>
/* DOCUMENT.b identifies the body without interpreting image bytes as HTML. */
enum cb_document_kind { CB_DOCUMENT_HTML, CB_DOCUMENT_TEXT, CB_DOCUMENT_PNG,
    CB_DOCUMENT_JPEG, CB_DOCUMENT_SVG };
/* A length-prefixed CBOR array [type, a, b, c, text], one per seqpacket.
 * Optional SCM_RIGHTS carries an anonymous, bounded document or frame. */
enum { CB_FETCH=1, CB_DOCUMENT, CB_ERROR, CB_LAYOUT, CB_FRAME, CB_LINK,
       CB_LINK_RESULT, CB_FIND, CB_FOUND, CB_TEXT, CB_TEXT_RESULT, CB_QUIT,
       CB_RESOURCE_REQUEST, CB_RESOURCE_DATA, CB_RESOURCE_DONE, CB_RESOURCES_END,
       CB_INTERACT, CB_INTERACTION, CB_ANCHOR, CB_ANCHOR_RESULT, CB_POLICY, CB_HTTP_REQUEST, CB_HTTP_META, CB_WEB_INIT, CB_WEB_REQUEST, CB_WEB_RESPONSE,
       CB_WEB_DIRTY, CB_WEB_STATE, CB_CONSOLE, CB_STORAGE_WRITE, CB_HTTP_PROGRESS };
/* INTERACT a=action,b=x,c=document y,text=UTF-8/key name. INTERACTION
 * a=result,b=focus,c=document y or UINT32_MAX,text=URL/notice/copied text.
 * ANCHOR a=0,text=fragment or a=1,text=current same-document URL;
 * RESULT a=found,b=document y. No fds. */
/* Resource catalogue: REQUEST a=index,b=1 CSS/2 image,text=resolved URL;
 * END a=count. DATA a=index,b=kind,text=MIME,fd=sealed body;
 * DONE a=index,b=1 accepted/0 rejected. Indices belong to this renderer only. */
#define CB_MAX_RESOURCES 32
/* LAYOUT a=physical width,b=viewport height,c=scroll,text="zoom\nflags".
 * flags bit0=reduced motion,bit1=strong contrast; legacy zoom alone means0. */
struct cb_message { uint32_t type, a, b, c; char text[8192]; int fd; };
int cb_send(int socket, const struct cb_message *message);
int cb_recv(int socket, struct cb_message *message);
int cb_memfd(const char *name, size_t size);
int cb_blob_fd(const char *data,size_t size);
int cb_document_fd(const char *data, size_t size);
int cb_read_document(int fd, char **data, size_t *size);
void cb_worker_limits(void);
/* Parse and validate an inherited SOCK_SEQPACKET descriptor. */
int cb_ipc_fd(const char *argument);
/* Fail-closed Landlock filesystem policy and seccomp syscall allowlist. */
int cb_renderer_restrict(void);
int cb_network_restrict(const char *ca_bundle);
#endif
