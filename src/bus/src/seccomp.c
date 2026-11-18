#include "whitecube_bus.h"
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>

#if defined(__x86_64__)
#  define WC_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#  define WC_ARCH AUDIT_ARCH_AARCH64
#else
#  error "unsupported architecture for seccomp filter"
#endif

int wc_seccomp_apply(int mode)
{
    static const int allowed[] = {
        SYS_read, SYS_write, SYS_recvmsg, SYS_sendmsg, SYS_accept4, SYS_close,
        SYS_epoll_wait, SYS_epoll_pwait, SYS_epoll_ctl, SYS_getsockopt,
        SYS_fcntl, SYS_clock_gettime, SYS_gettimeofday,
        SYS_brk, SYS_mmap, SYS_munmap, SYS_mremap, SYS_madvise,
        SYS_rt_sigreturn, SYS_rt_sigprocmask, SYS_rt_sigaction, SYS_sigaltstack,
        SYS_exit, SYS_exit_group, SYS_futex, SYS_restart_syscall,
        SYS_fstat, SYS_newfstatat, SYS_statx, SYS_unlinkat,
        SYS_getpid, SYS_getuid, SYS_getrandom, SYS_sched_yield,
        SYS_fork, SYS_clone, SYS_execve, SYS_wait4,
        SYS_rt_sigsuspend,
#ifdef SYS_epoll_pwait2
        SYS_epoll_pwait2,
#endif
#ifdef SYS_unlink
        SYS_unlink,
#endif
    };
    const size_t n = sizeof allowed / sizeof allowed[0];
    struct sock_filter f[8 + 2 * 64];
    size_t k = 0;

    f[k++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
                                          (uint32_t)offsetof(struct seccomp_data, arch));
    f[k++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, WC_ARCH, 1, 0);
    f[k++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS);
    f[k++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
                                          (uint32_t)offsetof(struct seccomp_data, nr));
    for (size_t i = 0; i < n; i++) {
        f[k++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (uint32_t)allowed[i], 0, 1);
        f[k++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);
    }
    uint32_t deny = (mode == 2) ? SECCOMP_RET_KILL_PROCESS
                                : (SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA));
    f[k++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, deny);

    struct sock_fprog prog = { .len = (unsigned short)k, .filter = f };
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) return -1;
    return prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog, 0, 0);
}
