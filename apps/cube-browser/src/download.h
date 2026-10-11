#ifndef CB_DOWNLOAD_H
#define CB_DOWNLOAD_H
#include <stddef.h>
#define CB_DOWNLOAD_LIMIT (64u*1024u*1024u)
/* Save a sealed completed transfer into the user's downloads directory without
 * following target symlinks or overwriting an existing file. No automatic open. */
int cb_download_save(int fd,const char *url,const char *disposition,char *path,size_t path_size,char *error,size_t error_size);
#endif
