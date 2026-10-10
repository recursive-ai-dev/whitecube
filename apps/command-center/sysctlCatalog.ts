import { SysctlCatalogEntry } from './types';

export const SYSCTL_CATALOG: Record<string, SysctlCatalogEntry> = {
  'net.ipv4.ip_forward': {
    description: 'Enables the kernel to route IPv4 packets between interfaces. Forced to 0 on this machine by the airgap hardening suite.',
    risk: 'high',
  },
  'net.ipv4.conf.all.rp_filter': {
    description: 'Reverse-path filtering for spoofed-source packet protection.',
    risk: 'medium',
  },
  'net.ipv4.tcp_syncookies': {
    description: 'SYN cookie protection against SYN-flood connection exhaustion.',
    risk: 'low',
  },
  'net.ipv4.conf.all.accept_redirects': {
    description: 'Whether ICMP redirects can change this host\'s routing table.',
    risk: 'medium',
  },
  'net.ipv4.conf.all.accept_source_route': {
    description: 'Whether source-routed packets are accepted.',
    risk: 'medium',
  },
  'kernel.dmesg_restrict': {
    description: 'Restricts dmesg access to privileged (CAP_SYSLOG) users.',
    risk: 'low',
  },
  'kernel.kptr_restrict': {
    description: 'Hides kernel pointers from unprivileged (or, at the strictest setting, all) reads, hardening against info leaks.',
    risk: 'low',
  },
  'kernel.yama.ptrace_scope': {
    description: 'Restricts which processes can ptrace which other processes.',
    risk: 'medium',
  },
  'kernel.sysrq': {
    description: 'Controls which magic SysRq key operations are permitted.',
    risk: 'medium',
  },
  'vm.overcommit_memory': {
    description: 'Controls whether the kernel allows memory overcommit; changes OOM-killer behavior system-wide.',
    risk: 'high',
  },
  'vm.mmap_min_addr': {
    description: 'Lowest address a process may mmap. Guarded by this app: cannot be set below 4096.',
    risk: 'high',
  },
  'vm.swappiness': {
    description: 'How aggressively the kernel swaps memory pages to disk.',
    risk: 'low',
  },
  'fs.protected_hardlinks': {
    description: 'Restricts hardlink creation to files you own, closing a privilege-escalation vector.',
    risk: 'medium',
  },
  'fs.protected_symlinks': {
    description: 'Restricts symlink following in world-writable sticky directories.',
    risk: 'medium',
  },
  'fs.suid_dumpable': {
    description: 'Whether setuid programs can produce core dumps.',
    risk: 'medium',
  },
  'fs.file-max': {
    description: 'System-wide limit on the number of open file handles.',
    risk: 'low',
  },
  'net.core.somaxconn': {
    description: 'Maximum length of the pending-connection queue for listening sockets.',
    risk: 'low',
  },
};
