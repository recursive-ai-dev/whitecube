#define _GNU_SOURCE
#include "ipc.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/seccomp.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static int allow_path(int rules,const char *path) {
    int fd=open(path,O_PATH|O_CLOEXEC);
    if(fd<0)return errno==ENOENT?0:-1;
    struct stat st;
    if(fstat(fd,&st)){close(fd);return -1;}
    struct landlock_path_beneath_attr entry={.parent_fd=fd,
        .allowed_access=LANDLOCK_ACCESS_FS_READ_FILE|(S_ISDIR(st.st_mode)?LANDLOCK_ACCESS_FS_READ_DIR:0)};
    int rc=(int)syscall(SYS_landlock_add_rule,rules,LANDLOCK_RULE_PATH_BENEATH,&entry,0);
    close(fd);return rc;
}
static int filesystem(int network,const char *ca_bundle) {
    int abi=(int)syscall(SYS_landlock_create_ruleset,NULL,0,LANDLOCK_CREATE_RULESET_VERSION);
    /* ABI 3 closes truncate paths; an unavailable boundary is a launch failure. */
    if(abi<3){errno=ENOTSUP;return -1;}
    struct landlock_ruleset_attr rules={.handled_access_fs=(UINT64_C(1)<<15)-1};
    if(abi>=5)rules.handled_access_fs|=LANDLOCK_ACCESS_FS_IOCTL_DEV;
    if(abi>=6)rules.scoped=LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET|LANDLOCK_SCOPE_SIGNAL;
    int fd=(int)syscall(SYS_landlock_create_ruleset,&rules,sizeof rules,0);
    if(fd<0)return -1;
    const char *fonts[]={"/etc/fonts","/usr/share/fonts","/usr/local/share/fonts",
        "/usr/share/fontconfig","/var/cache/fontconfig","/usr/lib","/lib","/dev/null","/dev/urandom"};
    const char *net[]={"/etc/hosts","/etc/resolv.conf","/run/whitecube-net/resolv.conf","/dev/null","/dev/urandom"};
    const char **paths=network?net:fonts;
    size_t count=network?sizeof net/sizeof *net:sizeof fonts/sizeof *fonts;
    for(size_t i=0;i<count;i++)if(allow_path(fd,paths[i])){close(fd);return -1;}
    if(network&&ca_bundle&&allow_path(fd,ca_bundle)){close(fd);return -1;}
    int result=(int)syscall(SYS_landlock_restrict_self,fd,0);close(fd);return result;
}
static int syscall_filter(int network) {
#if defined(__x86_64__)
    const unsigned arch=AUDIT_ARCH_X86_64;
#elif defined(__aarch64__)
    const unsigned arch=AUDIT_ARCH_AARCH64;
#else
    errno=ENOTSUP;return -1;
#endif
    struct sock_filter code[512];unsigned count=0;
#define ADD(x) do {code[count++]=(struct sock_filter)x;} while(0)
#define STMT(op,k) ADD(BPF_STMT(op,k))
#define JUMP(op,k,t,f) ADD(BPF_JUMP(op,k,t,f))
#define ALLOW(nr) do {JUMP(BPF_JMP|BPF_JEQ|BPF_K,(nr),0,1);STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);} while(0)
    STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,arch));
    JUMP(BPF_JMP|BPF_JEQ|BPF_K,arch,1,0);STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS);
    STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr));
    /* libc retries clone with inspectable arguments when clone3 is unavailable. */
#ifdef SYS_clone3
    JUMP(BPF_JMP|BPF_JEQ|BPF_K,SYS_clone3,0,1);
    STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|ENOSYS);
#endif
    /* No executable anonymous memory or writable file opens. Landlock confines
     * read opens; sealed anonymous memfds remain writable until publication. */
    const int memory_calls[]={SYS_mmap,SYS_mprotect};
    for(unsigned i=0;i<sizeof memory_calls/sizeof *memory_calls;i++){
        JUMP(BPF_JMP|BPF_JEQ|BPF_K,memory_calls[i],0,4);
        STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[2]));
        JUMP(BPF_JMP|BPF_JSET|BPF_K,PROT_EXEC,0,1);
        STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    }
#define READ_OPEN(nr,arg) do { \
    JUMP(BPF_JMP|BPF_JEQ|BPF_K,nr,0,4); \
    STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[arg])); \
    JUMP(BPF_JMP|BPF_JSET|BPF_K,O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND,0,1); \
    STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW); \
} while(0)
    READ_OPEN(SYS_openat,2);
#ifdef SYS_open
    READ_OPEN(SYS_open,1);
#endif
    JUMP(BPF_JMP|BPF_JEQ|BPF_K,SYS_prctl,0,5);
    STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0]));
    JUMP(BPF_JMP|BPF_JEQ|BPF_K,PR_SET_NAME,2,0);
    JUMP(BPF_JMP|BPF_JEQ|BPF_K,PR_SET_PDEATHSIG,1,0);
    STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    if(network){
        JUMP(BPF_JMP|BPF_JEQ|BPF_K,SYS_socket,0,4);
        STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0]));
        JUMP(BPF_JMP|BPF_JEQ|BPF_K,AF_INET,1,0);
        STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
        ALLOW(SYS_connect);ALLOW(SYS_sendto);ALLOW(SYS_recvfrom);ALLOW(SYS_shutdown);
        ALLOW(SYS_getsockname);ALLOW(SYS_getpeername);ALLOW(SYS_setsockopt);
    }
    const int allowed[]={SYS_read,SYS_write,SYS_readv,SYS_writev,SYS_pread64,SYS_pwrite64,
        SYS_close,SYS_close_range,SYS_lseek,SYS_fstat,SYS_newfstatat,SYS_statx,
        SYS_getcwd,SYS_getdents64,SYS_fcntl,SYS_dup,SYS_dup3,SYS_ftruncate,SYS_memfd_create,
        SYS_recvmsg,SYS_sendmsg,SYS_getsockopt,SYS_poll,SYS_ppoll,
        SYS_brk,SYS_munmap,SYS_mremap,SYS_madvise,SYS_futex,SYS_set_robust_list,SYS_rseq,
        SYS_rt_sigaction,SYS_rt_sigprocmask,SYS_rt_sigreturn,SYS_sigaltstack,SYS_restart_syscall,
        SYS_clock_gettime,SYS_clock_nanosleep,SYS_nanosleep,SYS_gettimeofday,
        SYS_getpid,SYS_getppid,SYS_gettid,SYS_getuid,SYS_geteuid,SYS_getgid,SYS_getegid,
        SYS_getrandom,SYS_sched_getaffinity,SYS_sched_yield,SYS_uname,
        SYS_prlimit64,SYS_getrlimit,SYS_setitimer,SYS_getitimer,SYS_exit,SYS_exit_group,
#ifdef SYS_alarm
        SYS_alarm,SYS_time,SYS_stat,SYS_lstat,SYS_access,SYS_dup2,
#endif
        SYS_faccessat,SYS_faccessat2};
    for(unsigned i=0;i<sizeof allowed/sizeof *allowed;i++)ALLOW(allowed[i]);
    if(!network){
        /* Raster children inherit the same policy. No exec or sockets are allowed. */
        ALLOW(SYS_clone);ALLOW(SYS_wait4);ALLOW(SYS_waitid);
#ifdef SYS_fork
        ALLOW(SYS_fork);
#endif
    }
    STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    struct sock_fprog program={.len=(unsigned short)count,.filter=code};
    return prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&program);
#undef READ_OPEN
#undef ALLOW
#undef JUMP
#undef STMT
#undef ADD
}
static int restrict_worker(int network,const char *ca_bundle) {
    if(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0))return -1;
    struct __user_cap_header_struct header={.version=_LINUX_CAPABILITY_VERSION_3};
    struct __user_cap_data_struct data[2]={{0},{0}};
    if(syscall(SYS_capset,&header,data))return -1;
    if(prctl(PR_CAP_AMBIENT,PR_CAP_AMBIENT_CLEAR_ALL,0,0,0)&&errno!=EINVAL)return -1;
    if(filesystem(network,ca_bundle))return -1;
    return syscall_filter(network);
}
int cb_renderer_restrict(void){return restrict_worker(0,NULL);}
int cb_network_restrict(const char *ca_bundle){return restrict_worker(1,ca_bundle);}
