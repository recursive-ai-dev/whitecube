#!/usr/bin/env python3
"""Build a standalone Debian archive without sudo or changing the desktop."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
BINARIES = ("cube-browser", "cube-renderd", "cube-netd")


def run(args, **kwargs):
    print("+ " + " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def copy(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    dest.chmod(0o755 if os.access(source, os.X_OK) else 0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build-deb")
    parser.add_argument("--skip-tests", action="store_true", help="use only after separately running the test suite")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 4))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    version = (ROOT / "VERSION").read_text().strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        parser.error("VERSION must contain a three-part numeric release")
    # Avoid silently linking Homebrew's desktop stack or inheriting development
    # library paths. Dependencies come from the Debian host plus pinned .deps.
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    env["PKG_CONFIG_PATH"] = str(ROOT / ".deps/prefix/lib/pkgconfig")
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PKG_CONFIG_LIBDIR", "CFLAGS", "CPPFLAGS", "LDFLAGS", "CC", "CXX"):
        env.pop(name, None)
    env["CFLAGS"] = shlex.join([f"-ffile-prefix-map={ROOT}=.", f"-fdebug-prefix-map={ROOT}=."])
    if "SOURCE_DATE_EPOCH" not in env:
        git = shutil.which("git", path=env["PATH"])
        result = subprocess.run([git, "log", "-1", "--format=%ct", "--", "."], cwd=ROOT, text=True, capture_output=True) if git else None
        epoch = result.stdout.strip() if result and result.returncode == 0 else ""
        env["SOURCE_DATE_EPOCH"] = epoch if epoch.isdecimal() else "1791504000"
    if not env["SOURCE_DATE_EPOCH"].isdecimal():
        parser.error("SOURCE_DATE_EPOCH must be a nonnegative integer")
    for required in ("meson", "ninja", "dpkg-deb", "dpkg-shlibdeps", "readelf", "strip", "desktop-file-validate"):
        if not shutil.which(required, path=env["PATH"]):
            parser.error(f"missing build tool: {required}")
    for required in (".deps/prefix/lib/libcairo.so.2", ".deps/lib/libmbedtls.a", ".deps/lib/libquickjs-core.a",
                     ".deps/prefix/lib/libsqlite3.a", ".deps/prefix/lib/libjpeg.a"):
        if not (ROOT / required).is_file():
            parser.error(f"missing {required}; run the four scripts/bootstrap-*.sh scripts first")
    build = args.build_dir.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    setup = ["meson", "setup"]
    if (build / "build.ninja").exists():
        setup += ["--reconfigure"]
    setup += [build, ROOT, "--prefix=/usr", "--bindir=lib/cube-browser", "--buildtype=release",
              "-Dprivate_libdir=$ORIGIN/lib", "-Db_lto=true"]
    run(["python3", ROOT / "scripts/version.py", "--check"], cwd=ROOT, env=env)
    run(setup, cwd=ROOT, env=env)
    run(["meson", "compile", "-C", build, "-j", str(args.jobs)], env=env)
    if not args.skip_tests:
        run(["meson", "test", "-C", build, "--no-suite=integration", "--num-processes=1", "--print-errorlogs"], env=env)
    arch = subprocess.check_output(["dpkg", "--print-architecture"], env=env, text=True).strip()
    archive = output / f"cube-browser_{version}_{arch}.deb"
    with tempfile.TemporaryDirectory(prefix="cube-browser-deb-") as temp:
        work = Path(temp)
        stage = work / "package"
        run(["meson", "install", "--no-rebuild", "-C", build, "--destdir", stage], env=env)
        libdir = stage / "usr/lib/cube-browser/lib"
        libdir.mkdir(parents=True)
        cairo = (ROOT / ".deps/prefix/lib/libcairo.so.2").resolve()
        copy(cairo, libdir / cairo.name)
        (libdir / "libcairo.so.2").symlink_to(cairo.name)
        copy(ROOT / "packaging/cube-browser", stage / "usr/bin/cube-browser")
        copy(ROOT / "packaging/cube-browser.desktop", stage / "usr/share/applications/cube-browser.desktop")
        copy(ROOT / "cube-browser.svg", stage / "usr/share/icons/hicolor/scalable/apps/cube-browser.svg")
        docs = stage / "usr/share/doc/cube-browser"
        for name in ("README.md", "COMPATIBILITY.md", "RELEASE.md", "VALIDATION.md"):
            copy(ROOT / name, docs / name)
        copy(ROOT / "packaging/README.md", docs / "README.Debian")
        copy(ROOT / "packaging/copyright", docs / "copyright")
        copy(ROOT / "packaging/COPYING", docs / "COPYING")
        copy(ROOT / "packaging/NOTICE", docs / "NOTICE")
        licenses = docs / "licenses"
        for source, name in (
            (ROOT / ".deps/licenses/mbedtls-LICENSE", "mbedtls-LICENSE"),
            (ROOT / ".deps/licenses/quickjs-LICENSE", "quickjs-LICENSE"),
            (ROOT / ".deps/prefix/share/licenses/libjpeg-turbo/copyright", "libjpeg-turbo-copyright"),
            (ROOT / "third_party/nanosvg/LICENSE.txt", "nanosvg-LICENSE"),
        ):
            copy(source, licenses / name)
        for name in ("COPYING", "COPYING-LGPL-2.1", "COPYING-MPL-1.1"):
            copy(ROOT / ".deps/cairo-1.18.4" / name, licenses / name)
        (licenses / "sqlite-PUBLIC-DOMAIN").write_text(
            "SQLite 3.53.4 (https://sqlite.org/2026/sqlite-amalgamation-3530400.zip)\n\n"
            "All of the code and documentation in SQLite has been dedicated to the\n"
            "public domain by the authors. All code authors, and representatives of\n"
            "the companies they work for, have signed affidavits dedicating their\n"
            "contributions to the public domain and originals of those signed\n"
            "affidavits are stored in a firesafe at the main offices of Hwaci.\n\n"
            "See https://sqlite.org/copyright.html.\n")
        executables = [stage / "usr/lib/cube-browser" / name for name in BINARIES]
        elfs = executables + [libdir / cairo.name]
        for binary in elfs:
            run(["strip", "--strip-unneeded", binary], env=env)
        for binary in executables[:2]:
            dynamic = subprocess.check_output(["readelf", "-d", binary], env=env, text=True)
            paths = [line for line in dynamic.splitlines() if "RUNPATH" in line or "RPATH" in line]
            if len(paths) != 1 or "[$ORIGIN/lib]" not in paths[0]:
                raise SystemExit(f"unexpected packaged runtime path for {binary}: {paths}")
        run([ROOT / "scripts/check-links.sh", *elfs], env=env)
        run(["desktop-file-validate", stage / "usr/share/applications/cube-browser.desktop"], env=env)
        # Declare the private Cairo as belonging to this archive, then remove
        # self-dependencies. All other dependencies use the host's symbols data.
        control = stage / "DEBIAN"
        control.mkdir()
        debian = work / "debian"
        debian.mkdir()
        (debian / "control").write_text("Source: cube-browser\n\nPackage: cube-browser\nArchitecture: any\nDescription: Native Wayland web browser\n")
        (debian / "shlibs.local").write_text(f"libcairo 2 cube-browser (= {version})\n")
        command = ["dpkg-shlibdeps", "-O", "-xcube-browser", "-l" + str(libdir), "-S" + str(stage)]
        command += ["-e" + str(binary) for binary in elfs]
        dependencies = subprocess.check_output(command, cwd=work, env=env, text=True)
        found = next((line.split("=", 1)[1] for line in dependencies.splitlines() if line.startswith("shlibs:Depends=")), None)
        if not found:
            raise SystemExit("dpkg-shlibdeps did not generate dependencies")
        installed_size = (sum(p.stat().st_size for p in stage.rglob("*") if p.is_file() and not p.is_symlink()) + 1023) // 1024
        (control / "control").write_text(
            f"Package: cube-browser\nVersion: {version}\nArchitecture: {arch}\n"
            "Section: web\nPriority: optional\nMaintainer: Whitecube contributors <maintainers@whitecube.local>\n"
            f"Installed-Size: {installed_size}\nDepends: {found}, ca-certificates\n"
            "Recommends: fonts-dejavu-core\n"
            "Description: Native Wayland browser with an independent web engine\n"
            " Cube Browser includes sandboxed network and rendering workers, a bounded\n"
            " JavaScript runtime, local history, bookmarks and private browsing.\n"
            " Requires a native Wayland session and Linux Landlock ABI 3 or newer.\n")
        sums = []
        for path in sorted(stage.rglob("*")):
            if path.is_file() and not path.is_symlink() and control not in path.parents:
                sums.append(f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(stage)}\n")
        (control / "md5sums").write_text("".join(sums))
        epoch = int(env["SOURCE_DATE_EPOCH"])
        for path in sorted(stage.rglob("*"), reverse=True):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644)
            os.utime(path, (epoch, epoch), follow_symlinks=False)
        os.utime(stage, (epoch, epoch))
        # Publish only a complete archive; interrupted rebuilds retain the last
        # valid release instead of leaving a truncated .deb at its final path.
        with tempfile.NamedTemporaryFile(prefix=f".{archive.name}.", suffix=".tmp", dir=output, delete=False) as partial:
            candidate = Path(partial.name)
        try:
            run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", stage, candidate], env=env)
            candidate.chmod(0o644)
            candidate.replace(archive)
        finally:
            candidate.unlink(missing_ok=True)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".deb.sha256").write_text(f"{checksum}  {archive.name}\n")
    print(f"Built {archive}\nSHA-256 {checksum}")


if __name__ == "__main__":
    main()
