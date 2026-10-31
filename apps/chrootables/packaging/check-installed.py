#!/usr/bin/python3
"""Check an installed or extracted Studio payload using temporary read-only data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

PACKAGE = "cube-chrootables-studio"
MANAGER = "cube-chrootables-manager"


def check(prefix):
    prefix = prefix.resolve()
    application = prefix / "lib" / PACKAGE
    desktop, manager = prefix / "bin" / PACKAGE, prefix / "bin" / MANAGER
    for path in (desktop, manager):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"Missing Studio executable: {path}")
        if not path.resolve().is_relative_to(application):
            raise RuntimeError(f"Studio executable escapes its private application: {path}")
    if desktop.read_text().splitlines()[0] != "#!/usr/bin/python3 -I":
        raise RuntimeError("Studio launcher does not select isolated distribution Python.")
    docs = prefix / "share/doc" / PACKAGE
    provenance = json.loads((docs / "build-provenance.json").read_text())
    for relative, expected in provenance["files_sha256"].items():
        path = prefix.parent / relative
        if not path.resolve().is_relative_to(prefix):
            raise RuntimeError("Invalid provenance path.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Payload checksum differs: {relative}")
    with tempfile.TemporaryDirectory(prefix="cube-studio-check-") as temporary:
        directory = Path(temporary)
        store = directory / "absent-store"
        environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "CHROOTABLE_AUTO_INSTALL": "0",
                       "CHROOTABLE_HOME": str(store), "XDG_CONFIG_HOME": str(directory / "config")}

        def run(*command, **options):
            try:
                return subprocess.run(list(map(str, command)), env=environment,
                                      capture_output=True, text=True, timeout=30, **options)
            except subprocess.CalledProcessError as error:
                raise RuntimeError(f"{Path(command[0]).name} failed: {error.stdout}\n{error.stderr}") from error

        app_version = run(desktop, "--version", check=True).stdout.strip()
        engine_version = run(manager, "--version", check=True).stdout.strip().split()[-1]
        if app_version != provenance["application_version"] or engine_version != provenance["engine_version"]:
            raise RuntimeError("Installed application/engine versions differ from their provenance.")
        capabilities = json.loads(run(manager, "--capabilities", "--json", check=True).stdout)
        if capabilities.get("schema_version") != 1 or not capabilities.get("profiles"):
            raise RuntimeError("Invalid manager capabilities.")
        doctor = run(manager, "--doctor", "--json")
        if doctor.returncode not in (0, 1) or json.loads(doctor.stdout).get("schema_version") != 1:
            raise RuntimeError("Installed manager diagnostics failed.")
        broker = run("/usr/bin/python3", "-I", "-S", application / "chrootables/broker.py",
                     "--store", store, input='{"id":1,"action":"inventory"}\n{"id":2,"action":"doctor"}\n', check=True)
        results = {message["id"]: message for line in broker.stdout.splitlines()
                   if (message := json.loads(line)).get("event") == "result"}
        if not results[1]["ok"] or results[1]["data"]["images"]:
            raise RuntimeError("Installed helper failed to inspect an empty store.")
        if not results[2]["ok"] or results[2]["data"].get("schema_version") != 1:
            raise RuntimeError("Installed helper diagnostics failed.")
        screenshot = directory / "desktop.png"
        run(desktop, "--demo", "--store", store, "--screenshot", screenshot, check=True)
        if not screenshot.is_file() or screenshot.stat().st_size == 0:
            raise RuntimeError("Studio demo did not render.")
        if store.exists():
            raise RuntimeError("Read-only checks created a store.")
        print(f"Studio {app_version}, engine {engine_version}: checksums, CLI, diagnostics, helper and Qt demo passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path("/usr"))
    args = parser.parse_args()
    try:
        check(args.prefix)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Studio installed check failed: {error}\n")
