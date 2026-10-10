Four Debian 13 lab containers: audit and implementation constraints

Confirmed request: four Debian 13 (trixie) containers, each with an 8 GiB disk
image. Proposed roles are bash, web, build and clean. 8 GiB is a storage cap,
not a RAM reservation. audit.json records the machine's observed capabilities.

The target has 223.91 GiB available on its root filesystem and 494.65 GiB on
home at audit time. Four images reserve 32 GiB, plus provisioning overhead.
The live workspace has only about 4.7 GiB available, and /mnt/nvme_root is
read-only to this session. No images have been created or bootstrapped here.

Host dependencies

The target already includes unshare, chroot, systemd-run, fallocate, mkfs.ext4,
losetup and mount. It does not include debootstrap, mmdebstrap, systemd-nspawn,
machinectl, bwrap, LXC, Docker, Podman or uidmap. Standard existing container
stores and bootstrap caches were absent. No broad Projects search was made.

A read-only apt simulation against the target's cached package lists and dpkg
status for install --no-install-recommends debootstrap systemd-container found:
  distro-info        1.13
  debootstrap        1.0.141
  systemd-container  257.13-1~deb13u1
These are the only three new packages. Zero upgrades, zero removals, and zero
kernel or firmware packages are proposed. Seventeen packages remain unupgraded.
The required package archives are not cached. Nothing was downloaded. Recheck
the simulation on the booted target before installing; never run autoremove as
part of this setup. A future apt plan can differ from this cached-list result.

The installed 6.12.107+deb13-amd64 kernel enables namespaces, user namespaces,
memory/process cgroups and seccomp. ext4 and loop modules, including ext4's
crc16, mbcache and jbd2 dependencies, are already present. veth and bridge are
configured as modules but their files are absent. This design does not need
either one. No kernel/module restoration, driver change or blacklist modification
is needed. If loop or ext4 is inactive after boot, the controller may load that
already-installed module after checking its protected file in the running kernel's
module tree. Loading uses --use-blacklist and refuses an absent module or a
blacklist conflict instead of restoring or overriding anything.

Storage design

Use four root-owned regular files preallocated to exactly 8,589,934,592 bytes.
Format each as ext4 with no partition table. mkfs.ext4 -m 0 -E nodiscard keeps
the preallocation rather than discarding reserved extents. Filesystem metadata
makes usable capacity slightly less than 8 GiB. Do not auto-grow the files.
Use exclusive creation, reject existing/symlink destinations, and clean up
only temporary files created by the current provisioning operation.

Bootstrap a minimal Debian base into each mounted image with Debian archive
signature checking and verified HTTPS enabled, then install the role packages
inside that image. Bind no writable host directories into the guest by default.
Keep package caches inside the capped image or explicitly account for a shared
provisioning cache outside it. Ephemeral /run, /dev and other RAM-backed mounts
are memory use, not image storage; the image cap is not a cgroup memory limit.

Container launch

The reviewed Debian systemd 257 documentation supports a whole-filesystem image
with --image, separate user IDs with --private-users=pick, and automatic mapped
mounts or ownership adjustment with --private-users-ownership=auto. Use
--as-pid2 for a reaping stub init. Root invokes nspawn; no uidmap package is
needed for this privileged launch path. These operations remain untested here.

Use --settings=no, --register=no and --link-journal=no for direct lab shells,
plus --resolv-conf=bind-host. The initial preference shares the host network.
A saved offline preference or enter --offline uses private-network (loopback
only); omit veth and bridge options in both modes. Do not use systemd-nspawn@
defaults, which enable veth.
The guests share the host's network and ports; select distinct local web ports.
Never move the physical dongle into a guest. Do not silently fall back to
--private-users=no if user namespace setup fails.

Optional --project binds exactly one validated immediate Projects directory
read-only at /project. Otherwise host directories remain unbound. Project
builds may copy sources into the guest or explicitly request --project-mode rw.
Writable binds use norbind,rootidmap; escaping symlinks, hardlinks, special files
and nested mounts are refused. Only the chosen immediate project is writable.
See cube-lab-usage.txt for the reviewed write scope and launch examples.
The bind source is pinned with O_PATH|O_DIRECTORY|O_NOFOLLOW per component and
held open in the root parent while nspawn runs, using /proc/PARENT/fd/FD rather
than a replaceable pathname. norbind excludes nested mount points. systemd 257
passes this source directly to mount(2) before entering user/PID namespaces;
source handling was checked against upstream nspawn-mount.c and mountpoint-util.c:
https://github.com/systemd/systemd/blob/v257/src/nspawn/nspawn-mount.c
https://github.com/systemd/systemd/blob/v257/src/basic/mountpoint-util.c

The image filesystem isolates persistent storage; user/PID/mount namespaces
provide container separation. Host networking deliberately shares the network
namespace. Browser integration should launch an explicit lab entry command in
the existing PTY and keep privileged provisioning outside HTTP endpoints.

Primary reference: Debian's systemd-nspawn manual
https://manpages.debian.org/trixie/systemd-container/systemd-nspawn.1.en.html

Debootstrap's minbase variant installs required packages plus apt, with role
packages added explicitly. Its keyring and forced-signature-check options can
keep archive verification mandatory. Bootstrap after the real target is writable
and its package sources are reachable.

Primary reference: Debian's debootstrap manual
https://manpages.debian.org/trixie/debootstrap/debootstrap.8.en.html

Validation completed: filesystem/package/cache inventory, module dependency
closure, namespace/cgroup kernel configuration, disk space checks, and a real
apt dependency simulation using existing target lists. Image formatting,
bootstrap, namespace launch and browser-to-container operation remain pending
execution on the booted NVMe system.

The upgraded controller also provides named full-image snapshots, atomic
restore with a pre-restore recovery snapshot, and per-lab shared/offline
preferences. Each snapshot reserves another 8 GiB; restore needs 16 GiB plus
512 MiB free temporarily and retains the prior image as an 8 GiB snapshot.
Snapshot operations require the correct booted host, root, an idle role lock,
no attached loop device, protected paths, matching profile/UUID and sufficient
space. Full SHA256 verification precedes restore; partial copies never replace
the current image. No periodic snapshots or automatic deletion are configured.
See cube-lab-usage.txt for exact commands and recovery behavior.

Resource upgrade: each normal lab launch uses the target's existing systemd-run
and systemctl with a dedicated systemd scope and cgroup v2 accounting. Persistent
per-lab CPUQuota and MemoryMax settings default to 2 cores and 2048 MiB; swap is
capped at zero and OOM stops the lab. `cube-lab resources LAB --cpu N
--memory-mib N` changes stopped-lab limits; `status` includes live CPU/RAM/OOM
counters plus persisted last-run outcome. Disk capacity remains exactly 8 GiB.
No new dependency, module, blacklist change or service-mask change was added.
Actual cgroup enforcement and rootidmap launches await the booted target.
