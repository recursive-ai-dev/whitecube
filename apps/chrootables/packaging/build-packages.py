#!/usr/bin/python3
"""Build coexisting Cube Chrootables Studio DEB, RPM, Arch and AppImage releases."""
from __future__ import annotations

import argparse
from email.parser import Parser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_support import (APPLICATION_ID, EXECUTABLES, MANAGER_ALIAS, PACKAGE, PRIVATE,
                             ROOT, build_metadata, engine_version, file_digest,
                             normalize_tree, require_tool, revision, run_tool,
                             source_archive, stage_payload)

FORMATS = ("deb", "rpm", "arch", "appimage")
DEB_DEPENDS = ("python3 (>= 3.10), python3-pyside6.qtcore (>= 6.8), "
               "python3-pyside6.qtgui (>= 6.8), python3-pyside6.qtwidgets (>= 6.8), "
               "qt6-wayland, foot, sudo, bash, coreutils, util-linux, mount, e2fsprogs, "
               "debootstrap, debian-archive-keyring, ca-certificates, dpkg, findutils, "
               "gpgv, gzip, hostname, iproute2, mawk | gawk, tar, wget, whiptail, xz-utils")
RPM_DEPENDS = ("python3 >= 3.10", "python3-pyside6 >= 6.8", "qt6-qtwayland", "foot", "sudo",
               "bash", "coreutils", "util-linux", "e2fsprogs", "debootstrap", "debian-keyring",
               "ca-certificates", "dpkg", "findutils", "gnupg2", "gzip", "hostname", "iproute",
               "gawk", "tar", "wget", "newt", "xz")
ARCH_DEPENDS = ("python>=3.10", "pyside6>=6.8", "qt6-wayland", "foot", "sudo", "bash", "coreutils",
                "util-linux", "e2fsprogs", "debootstrap", "debian-archive-keyring", "ca-certificates",
                "dpkg", "findutils", "gnupg", "gzip", "inetutils", "iproute2", "gawk", "tar", "wget", "newt", "xz")
APPRUN = '''#!/bin/bash
set -eu
APPDIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Sudo helpers need an extracted payload outside a user's FUSE mount.
if [[ -n "${APPIMAGE:-}" && -z "${CUBE_CHROOTABLES_EXTRACTED:-}" && -z "${APPIMAGE_EXTRACT_AND_RUN:-}" ]]; then
    export CUBE_CHROOTABLES_EXTRACTED=1
    exec "$APPIMAGE" --appimage-extract-and-run "$@"
fi
if ! /usr/bin/python3 -I -c 'import sys, PySide6; assert sys.version_info >= (3, 10); assert PySide6.__version_info__ >= (6, 8)' 2>/dev/null; then
    printf '%s\\n' 'Cube Chrootables Studio needs host Python 3.10+ and PySide6 6.8+.' 'Install your distribution Python/Qt packages; see the bundled installation guide.' >&2
    exit 1
fi
exec "$APPDIR/usr/bin/cube-chrootables-studio" "$@"
'''


def build_deb(work, version, package_revision, epoch):
    tool = require_tool("dpkg-deb")
    stage = stage_payload(work / "payload", epoch)
    payload = sorted(p for p in stage.rglob("*") if p.is_file() and not p.is_symlink())
    control = stage / "DEBIAN"
    control.mkdir()
    (control / "control").write_text(f"""Package: {PACKAGE}
Version: {version}-{package_revision}
Section: devel
Priority: optional
Architecture: all
Maintainer: Whitecube contributors <whitecube@users.noreply.github.com>
Installed-Size: {sum((p.stat().st_size + 1023) // 1024 for p in payload)}
Depends: {DEB_DEPENDS}
Description: Standalone Cube Chrootables Qt workspace studio
 Create Debian image workspaces, open terminals, and manage recovery points.
 Coexists with Whitecube Desktop and the standalone Chrootables application.
""")
    (control / "md5sums").write_text("".join(
        f"{hashlib.md5(p.read_bytes(), usedforsecurity=False).hexdigest()}  {p.relative_to(stage)}\n"
        for p in payload))
    normalize_tree(stage, epoch)
    target = work / f"{PACKAGE}_{version}-{package_revision}_all.deb"
    run_tool([tool, "--root-owner-group", "--uniform-compression", "-Zxz", "-z6", "--threads-max=1",
              "--build", str(stage), str(target)], env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)})
    return target


def build_rpm(work, version, package_revision, epoch):
    tool = require_tool("rpmbuild", os.environ.get("RPMBUILD"))
    for name in ("SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS", "tmp"):
        (work / name).mkdir(parents=True)
    stage = stage_payload(work / "payload", epoch)
    source_archive(stage, work / "SOURCES/payload.tar.gz", epoch)
    dependencies = "\n".join("Requires: " + dependency for dependency in RPM_DEPENDS)
    spec = f"""%global _buildhost reproducible
%global use_source_date_epoch_as_buildtime 1
%global clamp_mtime_to_source_date_epoch 1
%global _binary_payload w6.xzdio
%global _build_id_links none
%global debug_package %{{nil}}
%global __os_install_post %{{nil}}
Name: {PACKAGE}
Version: {version}
Release: {package_revision}
Summary: Standalone Cube Chrootables Qt workspace studio
License: Apache-2.0
BuildArch: noarch
Source0: payload.tar.gz
AutoReqProv: no
{dependencies}

%description
Create Debian image workspaces, open terminals and manage recovery points.
Coexists with Whitecube Desktop and standalone Chrootables.

%prep
%setup -q -c -T
%{{__tar}} -xf "%{{SOURCE0}}"
%build
%install
mkdir -p "%{{buildroot}}"
cp -a usr "%{{buildroot}}/"
%files
%defattr(-,root,root,-)
/usr/lib/{PACKAGE}
/usr/bin/{PACKAGE}
/usr/bin/{MANAGER_ALIAS}
/usr/share/applications/{APPLICATION_ID}.desktop
/usr/share/icons/hicolor/scalable/apps/{APPLICATION_ID}.svg
%dir /usr/share/doc/{PACKAGE}
%license /usr/share/doc/{PACKAGE}/copyright
%doc /usr/share/doc/{PACKAGE}/*.gz
/usr/share/doc/{PACKAGE}/build-provenance.json
/usr/share/man/man1/{PACKAGE}.1.gz
/usr/share/man/man1/{MANAGER_ALIAS}.1.gz
"""
    (work / "SPECS/application.spec").write_text(spec)
    # RPM emits shell snippets that do not quote its temporary paths.
    with tempfile.TemporaryDirectory(prefix="cube-rpm-", dir="/tmp") as alias:
        linked = Path(alias) / "build"
        linked.symlink_to(work, target_is_directory=True)
        run_tool([tool, "--define", f"_topdir {linked}", "--define", f"_tmppath {linked / 'tmp'}",
                  "--define", f"_dbpath {linked / 'rpmdb'}", "--define", "_buildhost reproducible",
                  "-bb", str(linked / "SPECS/application.spec")], cwd=alias,
                 env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "TZ": "UTC", "LC_ALL": "C"})
    packages = list((work / "RPMS").rglob("*.rpm"))
    if len(packages) != 1:
        raise ValueError("RPM build did not produce exactly one package.")
    return packages[0]


def build_arch(work, version, package_revision, epoch):
    zstd = require_tool("zstd")
    stage = stage_payload(work / "payload", epoch)
    size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file() and not p.is_symlink())
    package_info = (f"pkgname = {PACKAGE}\npkgbase = {PACKAGE}\npkgver = {version}-{package_revision}\n"
                    f"pkgdesc = Standalone Cube Chrootables Qt workspace studio\n"
                    f"url = https://github.com/recursive-ai-dev/whitecube\nbuilddate = {epoch}\n"
                    f"packager = Whitecube contributors\nsize = {size}\narch = any\nlicense = Apache-2.0\n"
                    + "".join(f"depend = {dependency}\n" for dependency in ARCH_DEPENDS))
    (stage / ".PKGINFO").write_text(package_info)
    normalize_tree(stage, epoch)
    archive = work / "payload.tar"
    with tarfile.open(archive, "w", format=tarfile.GNU_FORMAT) as output:
        for path in sorted(stage.rglob("*")):
            info = output.gettarinfo(str(path), path.relative_to(stage).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = epoch
            if info.isfile():
                with path.open("rb") as stream:
                    output.addfile(info, stream)
            else:
                output.addfile(info)
    target = work / f"{PACKAGE}-{version}-{package_revision}-any.pkg.tar.zst"
    run_tool([zstd, "-q", "-19", "-T1", "--no-progress", str(archive), "-o", str(target)])
    return target


def build_appimage(work, version, package_revision, epoch):
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise ValueError("The pinned AppImage tooling supports x86_64 builds only.")
    tool = require_tool("appimagetool", os.environ.get("APPIMAGETOOL"))
    runtime = os.environ.get("APPIMAGE_RUNTIME_FILE")
    if not runtime:
        raise ValueError("APPIMAGE_RUNTIME_FILE is required; explicitly fetch the checksum-pinned build tools.")
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("cube_fetch_tools", ROOT / "packaging/fetch-build-tools.py")
    fetch = module_from_spec(spec)
    spec.loader.exec_module(fetch)
    if not fetch.validate_cached_tool(Path(runtime), fetch.TOOLS["runtime-x86_64"][1]):
        raise ValueError("The pinned AppImage runtime is missing.")
    if not fetch.validate_cached_tool(Path(tool), fetch.TOOLS["appimagetool-1.9.1-x86_64.AppImage"][1]):
        raise ValueError("The pinned appimagetool is missing.")
    stage = stage_payload(work / "AppDir", epoch)
    (stage / "AppRun").write_text(APPRUN)
    desktop = (ROOT / f"packaging/{APPLICATION_ID}.desktop").read_text()
    desktop = desktop.replace("Exec=" + PACKAGE, "Exec=AppRun").replace("TryExec=" + PACKAGE, "TryExec=AppRun")
    (stage / (APPLICATION_ID + ".desktop")).write_text(desktop)
    (stage / (APPLICATION_ID + ".svg")).symlink_to(f"usr/share/icons/hicolor/scalable/apps/{APPLICATION_ID}.svg")
    (stage / ".DirIcon").symlink_to(APPLICATION_ID + ".svg")
    normalize_tree(stage, epoch, {"AppRun"})
    target = work / f"Cube-Chrootables-Studio-{version}-{package_revision}-x86_64.AppImage"
    environment = {**os.environ, "ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1", "NO_APPSTREAM": "1", "TZ": "UTC"}
    environment.pop("SOURCE_DATE_EPOCH", None)
    command = [tool, "--no-appstream", "--comp", "zstd", "--runtime-file", str(Path(runtime).resolve())]
    for option in ("-all-root", "-no-xattrs", "-processors", "1", "-mkfs-time", str(epoch), "-all-time", str(epoch)):
        command += ["--mksquashfs-opt", option]
    run_tool([*command, str(stage), str(target)], env=environment)
    return target


def publish(artifacts, output, version, package_revision, epoch, *, force=False):
    """Preflight the complete set, flush staged contents, then rename under one lock."""
    with tempfile.TemporaryDirectory(prefix=".cube-publish-", dir=output) as temporary:
        work = Path(temporary)
        files, entries = {}, []
        for kind, artifact in artifacts:
            if not stat.S_ISREG(artifact.lstat().st_mode):
                raise ValueError("A release artifact must be a regular file.")
            digest = file_digest(artifact)
            artifact.chmod(0o755 if kind == "appimage" else 0o644)
            files[artifact.name] = artifact
            checksum = work / (artifact.name + ".sha256")
            checksum.write_text(f"{digest}  {artifact.name}\n")
            files[checksum.name] = checksum
            entries.append({"filename": artifact.name, "format": kind, "sha256": digest,
                            "bytes": artifact.stat().st_size,
                            "architecture": {"deb": "all", "rpm": "noarch", "arch": "any", "appimage": "x86_64"}[kind]})
        sums = work / "SHA256SUMS"
        sums.write_text("".join(f"{entry['sha256']}  {entry['filename']}\n" for entry in sorted(entries, key=lambda entry: entry['filename'])))
        files[sums.name] = sums
        manifest = work / "release-manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "package": PACKAGE, "version": version,
                                        "engine_version": engine_version(), "revision": package_revision,
                                        "source_revision": revision(), "source_date_epoch": epoch,
                                        "appimage_runtime": "host Python 3.10+ and PySide6 6.8+",
                                        "artifacts": sorted(entries, key=lambda entry: entry['filename'])},
                                       indent=2, sort_keys=True) + "\n")
        files[manifest.name] = manifest
        for source in files.values():
            if source.suffix != ".AppImage":
                source.chmod(0o644)
        descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            for name, source in files.items():
                target = output / name
                try:
                    info = target.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"Refusing to replace a nonregular output: {target}")
                if file_digest(source) != file_digest(target) and not force:
                    raise ValueError(f"{target} already differs; use --force to replace it.")
            for source in files.values():
                with source.open("rb") as stream:
                    os.fsync(stream.fileno())
            for name, source in files.items():
                target = output / name
                if force or not target.exists():
                    os.replace(source, target)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return [output / artifact.name for _, artifact in artifacts]


def verify_artifact(kind, artifact, directory):
    """Exercise the bytes that will ship, rather than only their staging inputs."""
    directory.mkdir()
    if kind == "deb":
        tool = require_tool("dpkg-deb")
        fields = Parser().parsestr(run_tool([tool, "--field", str(artifact)]).stdout)
        if fields.get("Package") != PACKAGE or fields.get("Architecture") != "all":
            raise ValueError("The Debian artifact has an unexpected package identity or architecture.")
        if artifact.name != f"{PACKAGE}_{fields['Version']}_all.deb" or fields.get("Depends") != DEB_DEPENDS:
            raise ValueError("The Debian artifact version or dependencies do not match its release contract.")
        import io
        data = subprocess.run([tool, "--fsys-tarfile", str(artifact)], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            if any(member.uid != 0 or member.gid != 0 for member in archive):
                raise ValueError("Debian archive ownership is not normalized to root.")
        run_tool([tool, "--extract", str(artifact), str(directory)])
    elif kind == "rpm":
        rpm = require_tool("rpm")
        fields = run_tool([rpm, "-qp", "--qf", "%{NAME}\n%{VERSION}-%{RELEASE}\n%{ARCH}\n", str(artifact)]).stdout.splitlines()
        if fields[0] != PACKAGE or fields[2] != "noarch" or artifact.name != f"{PACKAGE}-{fields[1]}.noarch.rpm":
            raise ValueError("The RPM artifact has an unexpected identity, version or architecture.")
        owners = run_tool([rpm, "-qp", "--qf", "[%{FILEUSERNAME} %{FILEGROUPNAME}\n]", str(artifact)]).stdout.splitlines()
        if any(owner != "root root" for owner in owners):
            raise ValueError("RPM archive ownership is not normalized to root.")
        converter, cpio = require_tool("rpm2cpio"), require_tool("cpio")
        converted = subprocess.run([converter, str(artifact)], capture_output=True, check=True)
        extracted = subprocess.run([cpio, "-idm", "--quiet", "--no-absolute-filenames"], cwd=directory,
                                   input=converted.stdout, capture_output=True, umask=0o022)
        if extracted.returncode:
            raise ValueError("RPM extraction failed: " + extracted.stderr.decode(errors="replace")[:2000])
    elif kind == "arch":
        data = subprocess.run([require_tool("zstd"), "-q", "-d", "-c", str(artifact)],
                              capture_output=True, check=True).stdout
        import io
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            members = archive.getmembers()
            if any(member.uid != 0 or member.gid != 0 for member in members):
                raise ValueError("Arch archive ownership is not normalized to root.")
            info = archive.extractfile(".PKGINFO").read().decode()
            fields = dict(line.split(" = ", 1) for line in info.splitlines() if not line.startswith("depend = "))
            depends = [line.removeprefix("depend = ") for line in info.splitlines() if line.startswith("depend = ")]
            if (fields.get("pkgname") != PACKAGE or fields.get("arch") != "any" or depends != list(ARCH_DEPENDS)
                    or artifact.name != f"{PACKAGE}-{fields['pkgver']}-any.pkg.tar.zst"):
                raise ValueError("The Arch artifact metadata does not match its release contract.")
            previous = os.umask(0o022)
            try:
                archive.extractall(directory, filter="data")
            finally:
                os.umask(previous)
    else:
        artifact.chmod(0o755)
        with artifact.open("rb") as stream:
            header = stream.read(20)
        if header[:4] != b"\x7fELF" or int.from_bytes(header[18:20], "little") != 62:
            raise ValueError("The AppImage runtime is not an x86_64 ELF executable.")
        offset = run_tool([str(artifact), "--appimage-offset"]).stdout.strip()
        if not offset.isdigit():
            raise ValueError("The AppImage returned an invalid filesystem offset.")
        # The runtime intentionally extracts directories as private 0700;
        # unsquashfs restores the archived modes for this metadata verification.
        run_tool([require_tool("unsquashfs"), "-no-progress", "-d", str(directory), "-o", offset, str(artifact)], umask=0o022)
        run_tool([str(artifact), "--version"],
                 env={**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1", "QT_QPA_PLATFORM": "offscreen"})
    for path in (directory / "usr").rglob("*"):
        if not path.is_symlink():
            expected = 0o755 if path.is_dir() or path.relative_to(directory).as_posix() in EXECUTABLES else 0o644
            if path.stat().st_mode & 0o777 != expected:
                raise ValueError(f"Unexpected packaged mode {path.stat().st_mode & 0o777:o}, expected {expected:o}: {path}")
        elif not path.resolve().is_relative_to(directory):
            raise ValueError(f"Packaged symlink escapes the payload: {path}")
    run_tool(["/usr/bin/python3", str(ROOT / "packaging/check-installed.py"), "--prefix", str(directory / "usr")])
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="build all four formats (default)")
    selection.add_argument("--format", choices=FORMATS)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--revision", default="1")
    parser.add_argument("--force", action="store_true", help="replace differing release outputs")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 12):
            raise ValueError("Studio artifact builders require Python 3.12+; the application supports Python 3.10+.")
        version, epoch, _ = build_metadata(args.revision)
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".cube-build-", dir=output) as temporary:
            artifacts = []
            for kind in ((args.format,) if args.format else FORMATS):
                work = Path(temporary) / kind
                work.mkdir()
                artifact = globals()["build_" + kind](work, version, args.revision, epoch)
                verify_artifact(kind, artifact, work / "extracted")
                artifacts.append((kind, artifact))
                print(f"Built {kind}: {artifact.name}", flush=True)
            for artifact in publish(artifacts, output, version, args.revision, epoch, force=args.force):
                print(artifact)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Studio package build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
