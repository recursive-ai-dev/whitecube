#!/usr/bin/env python3
"""Whitecube command suite. Python standard library only; see wc-help."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

VERSION = "2.0.0"
HOME = Path.home()
SYS = Path("/sys")
PROC = Path("/proc")
# Wrappers live in ../../bin beside lib/wc-suite: ~/.local/bin for a per-user
# install, /usr/bin for the Whitecube package (/usr/lib/wc-suite).
SUITE_DIR = Path(__file__).resolve().parent
BIN = SUITE_DIR.parent.parent / "bin" if SUITE_DIR.parent.name == "lib" else HOME / ".local/bin"
STATE = Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local/state"))) / "wc-suite"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "whitecube/config"
KEYS = Path("/usr/share/whitecube/keys.txt")
COMMANDS = {}
GROUPS = ("Help", "Applications", "Desktop", "Audio", "Network", "System", "Power", "Utilities")


class Error(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def command(name, group, summary, examples="", needs=""):
    def register(handler):
        COMMANDS[name] = dict(group=group, summary=summary, examples=examples,
                              needs=needs, handler=handler)
        return handler
    return register


def executable(name):
    found = locate(name)
    if not found:
        raise Error(f"required command not found: {name}. Run wc-doctor for available tools.", 127)
    return found


def locate(name):
    name = str(name)
    local = BIN / name
    if "/" not in name and local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


def run(argv, *, capture=False, check=True, timeout=20, input=None, **kwargs):
    argv = [str(a) for a in argv]
    argv[0] = executable(argv[0])
    result = subprocess.run(argv, capture_output=capture, text=True, timeout=timeout,
                            input=input, **kwargs)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise Error(f"{argv[0]} failed (exit {result.returncode})" + (f": {detail[:1500]}" if detail else ""))
    return result


def exec_tool(argv):
    argv = [str(a) for a in argv]
    argv[0] = executable(argv[0])
    os.execvpe(argv[0], argv, os.environ.copy())


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise Error(f"state directory must be owned by you and not a symlink: {path}")
    path.chmod(0o700)
    return path


def private_file(path, mode="a"):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_APPEND
    fd = os.open(path, flags, 0o600)
    if os.fstat(fd).st_uid != os.getuid():
        os.close(fd)
        raise Error(f"file must be owned by you: {path}")
    os.fchmod(fd, 0o600)
    if mode == "w":
        os.ftruncate(fd, 0)
    return os.fdopen(fd, mode)


def desktop():
    if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        raise Error("no desktop display in this shell; run this command from a desktop terminal")


def detach(argv, name, *, cwd=None):
    desktop()
    argv = [str(a) for a in argv]
    argv[0] = executable(argv[0])
    logs = private_dir(STATE / "logs")
    log = logs / f"{name}.log"
    if log.is_file() and not log.is_symlink() and log.stat().st_size > 1024 * 1024:
        log.replace(log.with_suffix(".log.old"))
    with private_file(log) as output:
        output.write(f"\n[{dt.datetime.now().isoformat(timespec='seconds')}] launching {argv[0]}\n")
        output.flush()
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                start_new_session=True, cwd=cwd, close_fds=True)
    try:
        status = proc.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        return
    if status:
        raise Error(f"{argv[0]} exited with status {status}; see {log}")


def read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return default


def output(data, as_json=False):
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=True))
    else:
        for key, value in data.items():
            print(f"{key + ':':12} {value}")


def rest(args):
    values = args.args
    return values[1:] if values and values[0] == "--" else values


def percent(value):
    if not re.fullmatch(r"[0-9]{1,3}", value) or int(value) > 100:
        raise argparse.ArgumentTypeError("percentage must be an integer from 0 to 100")
    return int(value)


def bounded_int(low, high):
    def parse(value):
        try:
            n = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer from {low} to {high}") from None
        if not low <= n <= high:
            raise argparse.ArgumentTypeError(f"expected an integer from {low} to {high}")
        return n
    return parse


def duration(value):
    # One or more ordered-independent units, e.g. 1h30m; seconds if no unit.
    if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?[smhd])+|[0-9]+(?:\.[0-9]+)?", value):
        raise argparse.ArgumentTypeError("use a positive duration such as 90, 90s, 5m, or 1h30m")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        seconds = float(value)
    else:
        seconds = sum(float(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
                      for n, u in re.findall(r"([0-9]+(?:\.[0-9]+)?)([smhd])", value))
    if not math.isfinite(seconds) or not 0 < seconds <= 365 * 86400:
        raise argparse.ArgumentTypeError("duration must be positive and at most 365 days")
    return seconds


@command("help", "Help", "Show every command, search help, or read detailed usage",
         "wc-help\nwc-help vol\nwc-help --search audio\nwc-help --all\nwc-help --keys")
def help_command(args):
    if args.keys:
        return show_keys(args)
    if args.command:
        name = args.command.removeprefix("wc-")
        if name in COMMANDS:
            print(parser(name).format_help(), end="")
            return
        for directory in command_dirs():
            path = directory / f"wc-{name}"
            if path.is_file() and os.access(path, os.X_OK):
                print(f"{path.name}: {external_summary(path)}\nExternal command; run {path.name} --help for its own usage.")
                return
        raise Error(f"unknown command: {args.command}; use wc-ls", 2)
    query = (args.search or "").casefold()
    if args.all:
        for name, spec in sorted(COMMANDS.items()):
            if not query or query in f"wc-{name} {spec['summary']} {spec['group']}".casefold():
                print(parser(name).format_help())
        return
    print(f"Whitecube commands {VERSION}\n")
    for group in GROUPS:
        rows = [(name, spec) for name, spec in sorted(COMMANDS.items()) if spec["group"] == group
                and (not query or query in f"wc-{name} {spec['summary']} {group}".casefold())]
        if rows:
            print(group + ":")
            for name, spec in rows:
                print(f"  {'wc-' + name:17} {spec['summary']}")
            print()
    extras = [(name, spec) for name, spec in catalog().items() if spec.get("external")]
    extras = [(n, s) for n, s in extras if not query or query in f"{n} {s['summary']}".casefold()]
    if extras:
        print("Other installed commands:")
        for name, spec in extras:
            print(f"  {name:17} {spec['summary']}")
    print("Details: wc-help COMMAND or wc-COMMAND --help. Key bindings: wc-keys.")


def external_summary(path):
    try:
        with path.open("rb") as stream:
            data = stream.read(8192).decode("utf-8", errors="replace")
        for line in data.splitlines():
            if line.startswith("# desc: "):
                return line[8:]
    except OSError:
        pass
    return "External wc command"


def command_dirs():
    """Where wc-* commands live: the suite's own bin, then the per-user one."""
    return list(dict.fromkeys((BIN, HOME / ".local/bin")))


def catalog():
    data = {f"wc-{n}": {k: s[k] for k in ("summary", "group", "examples", "needs")}
            for n, s in sorted(COMMANDS.items())}
    for directory in command_dirs():
        for path in sorted(directory.glob("wc-*")):
            if path.name not in data and path.is_file() and os.access(path, os.X_OK):
                data[path.name] = dict(summary=external_summary(path), group="External", external=True)
    return data


@command("ls", "Help", "List installed commands, optionally filter or emit JSON", "wc-ls\nwc-ls audio\nwc-ls --json")
def list_commands(args):
    query = args.filter.casefold()
    data = {n: s for n, s in catalog().items() if query in f"{n} {s['summary']} {s['group']}".casefold()}
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for name, spec in sorted(data.items()):
            print(f"{name:19} {spec['summary']}")


@command("keys", "Help", "Show Whitecube keyboard bindings (the former wc-help)", "wc-keys\nwc-keys --plain")
def show_keys(args):
    if not KEYS.is_file():
        raise Error(f"key bindings file not found: {KEYS}")
    if getattr(args, "plain", False) or not sys.stdout.isatty():
        print(KEYS.read_text(), end="")
    else:
        pager = shlex.split(os.environ.get("PAGER", "less"))
        if not pager:
            raise Error("PAGER is empty")
        exec_tool(pager + [str(KEYS)])


@command("completion", "Help", "Print Bash, Zsh, or Fish shell completions", "wc-completion bash\nsource <(wc-completion bash)")
def completion(args):
    names = sorted(n for n in catalog() if re.fullmatch(r"[A-Za-z0-9_.-]+", n))
    # Complete parser options and choices, without executing desktop commands.
    if args.shell == "bash":
        print("_wc_complete() {\n  local cur=${COMP_WORDS[COMP_CWORD]}\n  local choices=''\n  case ${COMP_WORDS[0]##*/} in")
        for name in names:
            options = completion_choices(name)
            print(f"    {shlex.quote(name)}) choices={shlex.quote(' '.join(options))} ;;")
        print("  esac\n  COMPREPLY=()\n  while IFS= read -r item; do COMPREPLY+=(\"$item\"); done < <(compgen -W \"$choices\" -- \"$cur\")\n  if ((${#COMPREPLY[@]} == 0)); then\n    while IFS= read -r item; do COMPREPLY+=(\"$item\"); done < <(compgen -f -- \"$cur\")\n    compopt -o filenames 2>/dev/null\n  fi\n}")
        print("complete -F _wc_complete " + " ".join(shlex.quote(n) for n in names))
    elif args.shell == "zsh":
        print("#compdef " + " ".join(names) + "\n_wc_complete() {\n  local -a choices\n  case ${words[1]:t} in")
        for name in names:
            print(f"    {shlex.quote(name)}) choices=({' '.join(shlex.quote(x) for x in completion_choices(name))}) ;;")
        print("  esac\n  compadd -- $choices\n  _files\n}\ncompdef _wc_complete " + " ".join(shlex.quote(n) for n in names))
    else:
        for name in names:
            for item in completion_choices(name):
                flag = "-l " + shlex.quote(item[2:]) if item.startswith("--") else "-s " + shlex.quote(item[1:]) if item.startswith("-") else "-a " + shlex.quote(item)
                print(f"complete -c {shlex.quote(name)} {flag}")


def completion_choices(name):
    key = name.removeprefix("wc-")
    if key not in COMMANDS:
        return ["--help"]
    choices = []
    for action in parser(key)._actions:
        choices.extend(action.option_strings)
        if action.choices:
            choices.extend(str(x) for x in action.choices)
    if key in ("help", "ls"):
        choices.extend(sorted(n for n in catalog() if re.fullmatch(r"[A-Za-z0-9_.-]+", n)))
    if key in ("vol", "mic"):
        choices.extend(("status", "up", "down", "mute", "unmute", "on", "off"))
    elif key == "bright":
        choices.extend(("status", "up", "down"))
    return list(dict.fromkeys(choices))


APP_TOOLS = {"browser": "cube-browser", "chat": "desktop-chat", "settings": "cube-settings", "lab": "command-center"}


def app(args):
    tool = APP_TOOLS[args.name]
    local = BIN / tool
    detach([local if local.is_file() and os.access(local, os.X_OK) else tool] + rest(args), args.name)


for _name, _desc in (("browser", "Open the Cube browser"), ("chat", "Open Desktop Chat"),
                      ("settings", "Open desktop settings"), ("lab", "Open Ubuntu Lab Command Center")):
    command(_name, "Applications", _desc, f"wc-{_name}\nwc-{_name} -- [application options]", APP_TOOLS[_name])(app)


def terminal_argv(directory=None, title=None, argv=()):
    tool = "whitecube-terminal" if shutil.which("whitecube-terminal") else "foot"
    result = [tool]
    if directory:
        path = Path(directory).expanduser().resolve()
        if not path.is_dir():
            raise Error(f"not a directory: {path}", 2)
        result += ["-D", str(path)]
    if title:
        result += ["-T", title]
    if argv:
        result += ["--", *argv]
    return result


@command("term", "Applications", "Open a terminal here or in a chosen directory",
         "wc-term\nwc-term ~/Projects\nwc-term --exec python3 -m http.server", "whitecube-terminal or foot")
def term(args):
    if args.execute is not None and not args.execute:
        raise Error("--exec requires a command", 2)
    detach(terminal_argv(args.directory or os.getcwd(), args.title, args.execute or []), "term")


@command("binsys", "Applications", "Open the binsys TUI in a terminal", "wc-binsys", "binsys, terminal")
def binsys(args):
    executable("binsys")
    detach(terminal_argv(title="binsys", argv=["binsys", "tui", *rest(args)]), "binsys")


@command("launch", "Applications", "Open the Fuzzel app launcher", "wc-launch", "fuzzel")
def launch(args):
    desktop()
    exec_tool(["fuzzel", *rest(args)])


@command("project", "Applications", "List, find, or pick a project and open a terminal there",
         "wc-project\nwc-project my-project\nwc-project --list\nwc-project --root ~/work", "terminal; fuzzel for interactive picker")
def project(args):
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise Error(f"project directory not found: {root}", 2)
    dirs = sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.casefold())
    if args.list:
        for path in dirs:
            if not args.query or args.query.casefold() in path.name.casefold():
                print(path)
        return
    if args.query:
        selected = root / args.query
        if selected.is_dir() and selected.parent.resolve() == root:
            path = selected
        else:
            matches = [p for p in dirs if args.query.casefold() in p.name.casefold()]
            if len(matches) != 1:
                raise Error("no unique project match; use wc-project --list QUERY", 2)
            path = matches[0]
    else:
        desktop()
        if not dirs:
            raise Error(f"no project directories in {root}")
        # Numbered display labels avoid ambiguity from embedded newlines.
        labels = [f"{i + 1}: {json.dumps(p.name, ensure_ascii=False)}" for i, p in enumerate(dirs)]
        result = run(["fuzzel", "--dmenu", "-p", "project> "], capture=True, check=False,
                     timeout=None, input="\n".join(labels) + "\n")
        if result.returncode or not result.stdout.strip():
            return
        choice = result.stdout.strip()
        if choice not in labels:
            raise Error("select one of the listed projects", 2)
        path = dirs[labels.index(choice)]
    detach(terminal_argv(path), "project")


def require_folder_app():
    """xdg-open exits 0 and does nothing visible when no app handles folders; say so instead."""
    if not shutil.which("xdg-mime"):
        return
    result = run(["xdg-mime", "query", "default", "inode/directory"], capture=True, check=False)
    if result.returncode == 0 and not result.stdout.strip():
        raise Error("no app is set to open folders; set one with: xdg-mime default thunar.desktop inode/directory", 2)


@command("open", "Applications", "Open a file, directory, or URL with its default application",
         "wc-open ~/Downloads\nwc-open https://example.org", "xdg-open")
def open_target(args):
    target = args.target
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
        path = Path(target).expanduser().resolve()
        if not path.exists():
            raise Error(f"path not found: {path}", 2)
        if path.is_dir():
            require_folder_app()
        target = str(path)
    detach(["xdg-open", target], "open")


@command("files", "Applications", "Open a directory in the default file manager", "wc-files\nwc-files ~/Downloads", "xdg-open")
def files(args):
    path = args.directory.expanduser().resolve()
    if not path.is_dir():
        raise Error(f"not a directory: {path}", 2)
    require_folder_app()
    detach(["xdg-open", path], "files")


@command("gparted", "Applications", "Open GParted as root in this Wayland session", "wc-gparted", "sudo, gparted")
def gparted(args):
    desktop()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_dir():
        raise Error("XDG_RUNTIME_DIR must point to this session's runtime directory")
    tool = shutil.which("gparted") or ("/usr/sbin/gparted" if Path("/usr/sbin/gparted").is_file() else "gparted")
    executable(tool)
    exec_tool(["sudo", f"XDG_RUNTIME_DIR={runtime}",
               f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', 'wayland-0')}", "GDK_BACKEND=wayland", tool, *rest(args)])


@command("config", "Desktop", "Edit Whitecube configuration or print its path",
         "wc-config\nwc-config --path\nEDITOR='code --wait' wc-config")
def config(args):
    if args.path:
        print(CONFIG)
        return
    if not CONFIG.is_file():
        raise Error(f"configuration file not found: {CONFIG}")
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano")
    if not editor:
        raise Error("EDITOR is empty")
    exec_tool(editor + [str(CONFIG)])


@command("wall", "Desktop", "Cycle to the next Whitecube wallpaper", "wc-wall", "whitecube-next-wallpaper")
def wall(args):
    desktop()
    exec_tool(["whitecube-next-wallpaper"])


def backlights():
    return [p for p in sorted((SYS / "class/backlight").glob("*")) if p.is_dir()]


@command("bright", "Desktop", "Show or set backlight brightness with a safe minimum",
         "wc-bright\nwc-bright up\nwc-bright 40\nwc-bright --list", "backlight hardware; optional brightnessctl or sudo")
def bright(args):
    devices = backlights()
    if args.list:
        for path in devices:
            print(path.name)
        if not devices:
            print("no backlight device")
        return
    if args.device and not any(p.name == args.device for p in devices):
        raise Error(f"backlight device not found: {args.device}", 2)
    path = next((p for p in devices if p.name == args.device), devices[0] if devices else None)
    if path is None:
        raise Error("no backlight device; external monitor brightness needs its own controls")
    maximum = int(read(path / "max_brightness", "0"))
    current = int(read(path / "brightness", "0"))
    if maximum <= 0:
        raise Error(f"invalid maximum brightness for {path.name}")
    value = round(current * 100 / maximum)
    if args.action == "status":
        print(f"{path.name}: {value}%")
        return
    value = value + args.step if args.action == "up" else value - args.step if args.action == "down" else args.action
    value = max(5, min(100, value))
    raw = max(1, round(maximum * value / 100))
    if shutil.which("brightnessctl"):
        run(["brightnessctl", "-d", path.name, "set", str(raw)], capture=True)
    else:
        try:
            (path / "brightness").write_text(str(raw) + "\n")
        except PermissionError:
            run(["sudo", "-n", "tee", path / "brightness"], capture=True, input=str(raw) + "\n")
    print(f"{path.name}: {round(raw * 100 / maximum)}%")


@command("display", "Desktop", "Show connected displays or open display settings",
         "wc-display\nwc-display --settings", "optional wlr-randr; cube-settings for settings")
def display(args):
    if args.settings:
        return detach([BIN / "cube-settings", "--page", "display"], "display")
    if not args.json and shutil.which("wlr-randr") and os.environ.get("WAYLAND_DISPLAY"):
        return run(["wlr-randr"])
    data = {}
    for path in sorted((SYS / "class/drm").glob("card*-*")):
        if (path / "status").is_file():
            data[path.name] = {"status": read(path / "status"), "modes": read(path / "modes").splitlines()}
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for name, item in data.items():
            print(f"{name}: {item['status']}" + ("; " + ", ".join(item["modes"][:6]) if item["modes"] else ""))
        if not data:
            print("no display connectors reported")


@command("shot", "Desktop", "Capture a Wayland screenshot to a file or clipboard",
         "wc-shot\nwc-shot --region\nwc-shot --copy\nwc-shot ~/Pictures/screen.png", "grim; slurp for regions; wl-copy for clipboard")
def shot(args):
    desktop()
    executable("grim")
    if args.copy:
        executable("wl-copy")
    geometry = None
    if args.region:
        result = run(["slurp"], capture=True, check=False, timeout=None)
        if result.returncode or not result.stdout.strip():
            return
        geometry = result.stdout.strip()
    if args.file:
        path = args.file.expanduser().resolve()
        if not path.parent.is_dir():
            raise Error(f"output directory not found: {path.parent}", 2)
    else:
        pictures = HOME / "Pictures"
        if shutil.which("xdg-user-dir"):
            found = run(["xdg-user-dir", "PICTURES"], capture=True).stdout.strip()
            if found:
                pictures = Path(found)
        directory = pictures / "Screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (dt.datetime.now().strftime("screen-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ".png")
    # Reserve destination before invoking grim so existing files cannot be overwritten.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    captured = False
    try:
        run(["grim", "-t", "png", *(["-g", geometry] if geometry else []), path])
        captured = True
        if args.copy:
            with path.open("rb") as stream:
                run(["wl-copy", "--type", "image/png"], stdin=stream)
    except BaseException:
        if not captured:
            path.unlink(missing_ok=True)
        raise
    print(path)


@command("clip", "Utilities", "Copy, paste, or clear the Wayland clipboard",
         "printf 'hello' | wc-clip copy\nwc-clip copy 'hello world'\nwc-clip paste\nwc-clip clear", "wl-copy, wl-paste")
def clip(args):
    desktop()
    if args.action == "paste":
        if args.text:
            raise Error("paste does not accept text", 2)
        exec_tool(["wl-paste", *(["--no-newline"] if args.no_newline else [])])
    elif args.action == "clear":
        if args.text:
            raise Error("clear does not accept text", 2)
        run(["wl-copy", "--clear"])
    elif args.text:
        run(["wl-copy", "--", " ".join(args.text)])
    else:
        if sys.stdin.isatty():
            raise Error("provide text or pipe input to wc-clip copy", 2)
        exec_tool(["wl-copy"])


def audio_backend(request):
    if request != "auto":
        executable({"pipewire": "wpctl", "alsa": "amixer", "cube": "cube-audio"}[request])
        return request
    # Probe read-only; never repeat a failed write using another backend.
    if shutil.which("wpctl"):
        try:
            if run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture=True, check=False, timeout=3).returncode == 0:
                return "pipewire"
        except subprocess.TimeoutExpired:
            pass
    if shutil.which("amixer"):
        return "alsa"
    if shutil.which("cube-audio"):
        return "cube"
    raise Error("no audio backend available; install amixer (ALSA) or run PipeWire with wpctl", 127)


def alsa_control(args, is_mic):
    base = ["amixer", "-M"] + (["-c", str(args.card)] if args.card is not None else [])
    if args.control:
        return base, args.control
    data = run([*base, "scontrols"], capture=True).stdout
    controls = re.findall(r"Simple mixer control '(.+)',([0-9]+)", data)
    preferred = ("Capture", "Mic", "Internal Mic") if is_mic else ("Master", "PCM", "Speaker", "Headphone")
    for name in preferred:
        match = next((f"{n},{i}" for n, i in controls if n == name), None)
        if match:
            return base, match
    raise Error("no suitable ALSA mixer control; use --control NAME (inspect wc-audio)")


@command("vol", "Audio", "Show or change output volume and mute state",
         "wc-vol\nwc-vol up\nwc-vol 35\nwc-vol mute\nwc-vol --backend alsa --control Master", "wpctl, amixer, or cube-audio")
@command("mic", "Audio", "Show or change microphone level and mute state",
         "wc-mic\nwc-mic mute\nwc-mic unmute\nwc-mic 65", "wpctl, amixer, or cube-audio")
def volume(args):
    if args.backend not in ("auto", "alsa") and (args.card is not None or args.control):
        raise Error("--card and --control apply only to ALSA", 2)
    if args.backend == "auto" and (args.card is not None or args.control):
        args.backend = "alsa"
    backend = audio_backend(args.backend)
    is_mic = args.name == "mic"
    action = args.action
    if backend == "pipewire":
        if args.card is not None or args.control:
            raise Error("--card and --control apply only to ALSA", 2)
        target = "@DEFAULT_AUDIO_SOURCE@" if is_mic else "@DEFAULT_AUDIO_SINK@"
        if action in ("mute", "unmute", "on", "off"):
            setting = {"mute": "toggle", "unmute": "0", "on": "0", "off": "1"}[action]
            run(["wpctl", "set-mute", target, setting])
        elif action != "status":
            setting = f"{args.step}%+" if action == "up" else f"{args.step}%-" if action == "down" else f"{action}%"
            run(["wpctl", "set-volume", "-l", "1.0", target, setting])
        run(["wpctl", "get-volume", target])
    elif backend == "cube":
        if args.card is not None or args.control:
            raise Error("--card and --control apply only to ALSA", 2)
        options = ["--input"] if is_mic else []
        if action in ("mute", "unmute", "on", "off"):
            run(["cube-audio", "mute", *options, {"mute": "toggle", "unmute": "off", "on": "off", "off": "on"}[action]])
        elif action != "status":
            setting = f"+{args.step}" if action == "up" else f"-{args.step}" if action == "down" else str(action)
            run(["cube-audio", "volume", *options, setting])
        run(["cube-audio", "volume", *options])
        run(["cube-audio", "mute", *options])
    else:
        base, control = alsa_control(args, is_mic)
        if action in ("mute", "unmute", "on", "off"):
            if is_mic:
                if action == "mute":
                    state = run([*base, "sget", control], capture=True).stdout
                    setting = "cap" if "[off]" in state else "nocap"
                else:
                    setting = "cap" if action in ("unmute", "on") else "nocap"
            else:
                setting = {"mute": "toggle", "unmute": "unmute", "on": "unmute", "off": "mute"}[action]
            run([*base, "-q", "sset", control, setting])
        elif action != "status":
            setting = f"{args.step}%+" if action == "up" else f"{args.step}%-" if action == "down" else f"{action}%"
            run([*base, "-q", "sset", control, setting])
        run([*base, "sget", control])


@command("audio", "Audio", "Inspect audio devices, mixers, and the selected backend",
         "wc-audio\nwc-audio --backend alsa", "wpctl, amixer, or cube-audio")
def audio(args):
    backend = audio_backend(args.backend)
    print("backend: " + backend, flush=True)
    if backend == "pipewire":
        run(["wpctl", "status"])
    elif backend == "cube":
        run(["cube-audio", "cards"])
        run(["cube-audio", "status"])
    else:
        if shutil.which("aplay"):
            run(["aplay", "-l"])
        run(["amixer", "scontrols"])


@command("net", "Network", "Show addresses, routes, DNS, and optional reachability",
         "wc-net\nwc-net --probe\nwc-net --probe --host 1.1.1.1", "ip; ping for --probe")
def net(args):
    for heading, argv in (("Addresses", ["ip", "-br", "addr"]), ("Routes", ["ip", "route"]),
                          ("IPv6 routes", ["ip", "-6", "route"])):
        print(heading + ":", flush=True)
        run(argv)
    print("DNS:")
    servers = [line for line in read("/etc/resolv.conf").splitlines() if line.split()[:1] == ["nameserver"]]
    print("\n".join(servers) or "no nameservers reported")
    if args.probe:
        result = run(["ping", "-c", "1", "-W", "2", "--", args.host], capture=True, check=False, timeout=5)
        print(f"ICMP {args.host}: {'reachable' if result.returncode == 0 else 'no reply (may be filtered)'}")
        return 0 if result.returncode == 0 else 1


@command("wifi", "Network", "Inspect Wi-Fi, change radio state, or open the native manager",
         "wc-wifi\nwc-wifi on\nwc-wifi off\nwc-wifi manage\nwc-wifi profiles", "cube-net or iw for status; rfkill for radio; sudo cube-net for manager")
def wifi(args):
    if args.action in ("on", "off"):
        run(["rfkill", "unblock" if args.action == "on" else "block", "wifi"])
    elif args.action in ("manage", "scan", "reconnect"):
        executable("cube-net")
        exec_tool(["sudo", executable("cube-net"), args.action])
    elif args.action == "profiles":
        run(["cube-net", "profiles"])
    elif shutil.which("cube-net"):
        run(["cube-net", "status"])
    else:
        interfaces = [p.name for p in (SYS / "class/net").glob("*") if (p / "wireless").is_dir()]
        if not interfaces:
            print("no wireless interfaces found")
        for interface in interfaces:
            print(f"interface: {interface}", flush=True)
            if shutil.which("iw"):
                run(["iw", "dev", interface, "link"])
        if shutil.which("rfkill"):
            run(["rfkill", "list", "wifi"])


@command("bt", "Network", "Inspect Bluetooth, power the controller, or list connected devices",
         "wc-bt\nwc-bt on\nwc-bt off\nwc-bt devices", "bluetoothctl and a running Bluetooth service")
def bluetooth(args):
    argv = {"status": ["show"], "on": ["power", "on"], "off": ["power", "off"],
            "devices": ["devices", "Connected"]}[args.action]
    run(["bluetoothctl", "--timeout", "5", *argv], timeout=8)


def battery_data():
    result = []
    for path in sorted((SYS / "class/power_supply").glob("*")):
        if read(path / "type") != "Battery":
            continue
        result.append(dict(device=path.name, capacity=read(path / "capacity", "unknown"),
                           status=read(path / "status", "unknown"),
                           health=read(path / "health", "unknown")))
    return result


@command("bat", "System", "Show all batteries and their charging state", "wc-bat\nwc-bat --json")
def bat(args):
    batteries = battery_data()
    if args.json:
        print(json.dumps(batteries, indent=2))
    else:
        for item in batteries:
            print(f"{item['device']}: {item['capacity']}% {item['status']} ({item['health']})")
        if not batteries:
            print("no battery")


def temperature_data():
    result = {}
    for path in sorted((SYS / "class/thermal").glob("thermal_zone*")):
        value = read(path / "temp")
        if re.fullmatch(r"-?[0-9]+", value):
            result[f"{path.name} ({read(path / 'type', 'unknown')})"] = int(value) / 1000
    for path in sorted((SYS / "class/hwmon").glob("hwmon*")):
        for sensor in sorted(path.glob("temp*_input")):
            value = read(sensor)
            if re.fullmatch(r"-?[0-9]+", value):
                label = read(sensor.with_name(sensor.name.replace("_input", "_label")), sensor.stem)
                result[f"{path.name}/{read(path / 'name', 'unknown')}/{label}"] = int(value) / 1000
    return result


@command("temp", "System", "Read temperatures from thermal zones and hardware sensors", "wc-temp\nwc-temp --json")
def temp(args):
    data = temperature_data()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for name, value in data.items():
            print(f"{name}: {value:.1f} C")
        if not data:
            print("no temperature sensors reported")


@command("sys", "System", "Show host, uptime, memory, root disk, temperature, and battery",
         "wc-sys\nwc-sys --json")
def system_summary(args):
    uptime = float(read(PROC / "uptime", "0 0").split()[0])
    memory = {}
    for line in read(PROC / "meminfo").splitlines():
        key, _, value = line.partition(":")
        if value.split():
            memory[key] = int(value.split()[0])
    disk = shutil.disk_usage("/")
    data = dict(host=os.uname().nodename, kernel=os.uname().release,
                uptime=str(dt.timedelta(seconds=int(uptime))), load=read(PROC / "loadavg").split()[:3],
                memory_mib=dict(used=round((memory.get("MemTotal", 0) - memory.get("MemAvailable", 0)) / 1024),
                                total=round(memory.get("MemTotal", 0) / 1024)),
                root_disk_gib=dict(used=round(disk.used / 2**30, 1), total=round(disk.total / 2**30, 1)),
                temperatures_c=temperature_data(), batteries=battery_data())
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        output({"host": data["host"], "kernel": data["kernel"], "uptime": data["uptime"],
                "load": " ".join(data["load"]), "memory": f"{data['memory_mib']['used']} / {data['memory_mib']['total']} MiB",
                "root disk": f"{data['root_disk_gib']['used']} / {data['root_disk_gib']['total']} GiB"})
        temperatures = data["temperatures_c"]
        if temperatures:
            print(f"temperature: {max(temperatures.values()):.1f} C max across {len(temperatures)} sensors")
        bat(argparse.Namespace(json=False))


@command("disk", "System", "Show filesystem usage or block devices", "wc-disk\nwc-disk ~/Projects\nwc-disk --devices", "df; lsblk for devices")
def disks(args):
    if args.devices:
        if args.path:
            raise Error("--devices does not accept a filesystem path", 2)
        run(["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS"])
    else:
        argv = ["df", "-h"]
        if args.path:
            path = args.path.expanduser()
            if not path.exists():
                raise Error(f"path not found: {path}", 2)
            argv += ["--", str(path)]
        run(argv)


@command("top", "System", "Show processes sorted by memory or CPU",
         "wc-top\nwc-top --cpu -n 20\nwc-top --interactive", "ps; top for interactive")
def top(args):
    if args.interactive:
        exec_tool(["top"])
    result = run(["ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu" if args.cpu else "--sort=-pmem"], capture=True)
    print("\n".join(result.stdout.splitlines()[:args.count + 1]))


@command("services", "System", "List failed services or inspect a named system/user service",
         "wc-services\nwc-services --user\nwc-services whitecube-network", "systemctl")
def services(args):
    argv = ["systemctl", "--no-pager", *(["--user"] if args.user else [])]
    if args.service:
        argv += ["status", "--", args.service]
    else:
        argv += ["--failed"]
    return run(argv, check=False).returncode


@command("logs", "System", "Read the Whitecube session log or system/user journal",
         "wc-logs\nwc-logs -f\nwc-logs --suite\nwc-logs --service whitecube-network", "journalctl for journal")
def logs(args):
    if args.suite and (args.user or args.follow):
        raise Error("--suite lists log paths; read a listed log with tail -f", 2)
    if args.service or args.journal or args.user:
        argv = ["journalctl", "--no-pager", "-n", str(args.count), *(["--user"] if args.user else [])]
        if args.service:
            argv += ["-u", args.service]
        if args.follow:
            argv += ["-f"]
        exec_tool(argv)
    elif args.suite:
        root = STATE / "logs"
        if not root.is_dir():
            print("no suite launch logs yet")
            return
        for path in sorted(root.glob("*.log")):
            print(path)
        return
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local/state"))) / "whitecube"
        path = root / "session.log"
        if not path.is_file():
            raise Error(f"session log not found: {path}")
        if args.follow:
            exec_tool(["tail", "-n", str(args.count), "-F", "--", str(path)])
        exec_tool(["tail", "-n", str(args.count), "--", str(path)])


@command("check", "System", "Run the existing Whitecube health report", "wc-check\nwc-check -- --json", "cube-check")
def check(args):
    exec_tool(["cube-check", *rest(args)])


DOCTOR_TOOLS = {
    "Applications": ("foot", "whitecube-terminal", "fuzzel", "cube-browser", "desktop-chat", "cube-settings", "command-center", "binsys", "xdg-open", "gparted"),
    "Desktop": ("swaylock", "whitecube-next-wallpaper", "grim", "slurp", "wl-copy", "wl-paste", "wlr-randr", "brightnessctl"),
    "Audio (alternatives)": ("amixer", "wpctl", "cube-audio"),
    "Network": ("ip", "ping", "cube-net", "iw", "rfkill", "bluetoothctl"),
    "System": ("df", "lsblk", "ps", "systemctl", "journalctl", "sudo", "notify-send", "cube-check", "systemd-inhibit"),
}


@command("doctor", "System", "Check the suite, session, hardware, and optional dependencies",
         "wc-doctor\nwc-doctor --json\nwc-doctor --strict")
def doctor(args):
    tools = {group: {tool: locate(tool) for tool in items} for group, items in DOCTOR_TOOLS.items()}
    missing = [name for name in COMMANDS if not os.access(BIN / f"wc-{name}", os.X_OK)]
    info = dict(version=VERSION, python=sys.version.split()[0], suite=str(Path(__file__).resolve()),
                bin=str(BIN), bin_on_path=str(BIN) in os.environ.get("PATH", "").split(os.pathsep),
                wayland_display=os.environ.get("WAYLAND_DISPLAY", ""),
                runtime_directory=os.environ.get("XDG_RUNTIME_DIR", ""),
                notification_bus_configured=bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")),
                desktop_config=CONFIG.is_file(), backlights=[p.name for p in backlights()],
                batteries=battery_data(), missing_wrappers=missing, tools=tools)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        output({"version": VERSION, "python": info["python"], "bin": BIN,
                "PATH": "ok" if info["bin_on_path"] else f"add {BIN} to PATH",
                "display": info["wayland_display"] or "none in this shell",
                "notify bus": "configured" if info["notification_bus_configured"] else "not configured (notify-send may fail)",
                "config": "found" if info["desktop_config"] else "not found",
                "wrappers": "ok" if not missing else ", ".join(missing)})
        print("\nTools (missing optional tools affect only commands that use them):")
        for group, items in tools.items():
            print(group + ":")
            for name, path in items.items():
                print(f"  {name:25} {path or 'not installed'}")
    essential = ("df", "ps", "ip")
    return int(bool(missing) or (args.strict and (not info["bin_on_path"] or any(not locate(t) for t in essential))))


def confirm(message, yes):
    if yes:
        return
    if not sys.stdin.isatty():
        raise Error("confirmation requires a terminal; use --yes to proceed", 2)
    if input(message + " [y/N] ").casefold() not in ("y", "yes"):
        raise Error("cancelled")


@command("lock", "Power", "Lock the desktop and return only after the lock is established",
         "wc-lock", "swaylock")
def lock(args):
    desktop()
    run(["swaylock", "-f", "-c", "ffffff"], timeout=15)


@command("suspend", "Power", "Lock successfully before suspending to RAM",
         "wc-suspend\nwc-suspend --dry-run", "swaylock, systemctl, sudo")
def suspend(args):
    if args.dry_run:
        print("swaylock -f -c ffffff\nsudo systemctl suspend")
        return
    executable("systemctl")
    executable("sudo")
    lock(args)
    run(["sudo", "systemctl", "suspend"], timeout=None)


@command("off", "Power", "Power off cleanly after confirmation",
         "wc-off\nwc-off --yes\nwc-off --dry-run", "sudo, systemctl")
@command("reboot", "Power", "Reboot cleanly after confirmation",
         "wc-reboot\nwc-reboot --yes\nwc-reboot --dry-run", "sudo, systemctl")
def power(args):
    action = "poweroff" if args.name == "off" else "reboot"
    argv = ["sudo", "systemctl", action]
    if args.dry_run:
        print(shlex.join(argv))
        return
    confirm(f"{action.capitalize()} and close all applications?", args.yes)
    run(argv, timeout=None)


@command("logout", "Power", "End your Whitecube session after confirmation",
         "wc-logout\nwc-logout -y\nwc-logout --dry-run", "pkill")
def logout(args):
    argv = ["pkill", "-u", str(os.getuid()), "-x", "whitecube"]
    if args.dry_run:
        print(shlex.join(argv))
        return
    confirm("Quit Whitecube and close all session applications?", args.yes)
    run(argv)


@command("inhibit", "Power", "Keep the system awake while a foreground command runs",
         "wc-inhibit -- sleep 600\nwc-inhibit --reason 'Long build' -- make", "systemd-inhibit and logind")
def inhibit(args):
    values = rest(args)
    if not values:
        raise Error("provide a command after --", 2)
    executable(values[0])
    exec_tool(["systemd-inhibit", "--what=idle:sleep", "--mode=block", "--who=wc-inhibit",
               "--why=" + args.reason, "--", *values])


@command("notify", "Utilities", "Send a desktop notification with title and optional body",
         "wc-notify 'Build done' 'All checks passed'\nwc-notify --urgency critical 'Attention'", "notify-send")
def notify(args):
    run(["notify-send", "--urgency", args.urgency, "--expire-time", str(args.timeout),
         "--", args.title, " ".join(args.body)])


def timer_path(timer_id):
    if not re.fullmatch(r"[a-f0-9]{12}", timer_id):
        raise Error("invalid timer ID; use wc-timer --list", 2)
    return STATE / "timers" / f"{timer_id}.json"


def write_json(path, data):
    private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


@contextlib.contextmanager
def timer_guard():
    private_dir(STATE / "timers")
    with private_file(STATE / "timers/.lock") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def load_timer(path):
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("unsafe record")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("id") != path.stem:
            raise ValueError("invalid identity")
        if data.get("status") not in ("pending", "done", "failed", "cancelled", "interrupted"):
            raise ValueError("invalid status")
        if not isinstance(data.get("message"), str) or not isinstance(data.get("due"), (int, float)) or not math.isfinite(data["due"]):
            raise ValueError("invalid timer details")
        return data
    except (OSError, ValueError, OverflowError):
        raise Error(f"unreadable timer record: {path}") from None


@command("timer", "Utilities", "Start, list, or cancel persistent desktop notification timers",
         "wc-timer 5m 'Tea is ready'\nwc-timer 1h30m 'Take a break'\nwc-timer --list\nwc-timer --cancel ID\nwc-timer --prune", "notify-send for delivery")
def timer(args):
    if args.list or args.cancel or args.prune:
        if args.duration is not None or args.message:
            raise Error("timer management does not accept a duration or message", 2)
        if args.json and not args.list:
            raise Error("--json requires --list", 2)
        with timer_guard():
            if args.cancel:
                path = timer_path(args.cancel)
                if not path.is_file():
                    raise Error("timer ID not found", 2)
                data = load_timer(path)
                if data.get("status") != "pending":
                    raise Error(f"timer is already {data.get('status', 'unknown')}")
                data["status"] = "cancelled"
                write_json(path, data)
                print(f"cancelled {args.cancel}")
                return
            rows = []
            for path in sorted((STATE / "timers").glob("*.json")):
                data = load_timer(path)
                if data.get("status") == "pending" and not timer_alive(data):
                    data["status"] = "interrupted"
                    write_json(path, data)
                if args.prune and data["status"] != "pending":
                    path.unlink()
                rows.append(data)
        if args.prune:
            print(f"removed {sum(d['status'] != 'pending' for d in rows)} finished timer records")
            return
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for data in rows:
                due = dt.datetime.fromtimestamp(data["due"]).isoformat(timespec="seconds")
                message = json.dumps(data["message"], ensure_ascii=True)
                print(f"{data['id']}  {data['status']:9}  {due}  {message}")
            if not rows:
                print("no timers")
        return
    if args.duration is None:
        raise Error("provide a duration or use --list / --cancel / --prune", 2)
    if args.json:
        raise Error("--json requires --list", 2)
    executable("notify-send")
    message = " ".join(args.message) or "time is up"
    if len(message) > 4096:
        raise Error("timer message must be at most 4096 characters", 2)
    timer_id = uuid.uuid4().hex[:12]
    path = timer_path(timer_id)
    now = time.time()
    data = dict(id=timer_id, created=now, due=now + args.duration,
                message=message, status="pending", pid=None,
                boot_id=read(PROC / "sys/kernel/random/boot_id"))
    with timer_guard():
        write_json(path, data)
        try:
            with private_file(STATE / "timers/worker.log") as log:
                proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_timer-worker", timer_id],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        start_new_session=True, close_fds=True)
            data["pid"] = proc.pid
            data["pid_start"] = process_start(proc.pid)
            write_json(path, data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    print(f"timer {timer_id} set for {args.duration:g}s; cancel with wc-timer --cancel {timer_id}")


def process_start(pid):
    # /proc comm may contain spaces or parentheses; fields follow the final ')'.
    stat = read(PROC / str(pid) / "stat")
    fields = stat.rpartition(") ")[2].split()
    return fields[19] if len(fields) > 19 and fields[0] != "Z" else ""


def timer_alive(data):
    pid = data.get("pid")
    return bool(isinstance(pid, int) and pid > 0 and data.get("pid_start")
                and data.get("boot_id") == read(PROC / "sys/kernel/random/boot_id")
                and data["pid_start"] == process_start(pid))


def timer_worker(timer_id):
    path = timer_path(timer_id)
    # Monotonic deadline prevents clock changes from altering a running timer.
    with timer_guard():
        data = load_timer(path)
        remaining = max(0, data["due"] - time.time())
    deadline = time.monotonic() + remaining
    while True:
        with timer_guard():
            if not path.is_file():
                return 0
            data = load_timer(path)
            if data.get("status") != "pending":
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    run(["notify-send", "--", "Timer done", data["message"]], capture=True, timeout=10)
                    data["status"] = "done"
                except (Error, OSError, subprocess.TimeoutExpired) as error:
                    data["status"] = "failed"
                    data["error"] = str(error)
                data["finished"] = time.time()
                write_json(path, data)
                return int(data["status"] == "failed")
        time.sleep(min(remaining, 1))


def action(value):
    if value in ("status", "up", "down", "mute", "unmute", "on", "off"):
        return value
    return percent(value)


def bright_action(value):
    if value in ("status", "up", "down"):
        return value
    return percent(value)


def parser(name):
    spec = COMMANDS[name]
    examples = "Examples:\n  " + spec["examples"].replace("\n", "\n  ") if spec["examples"] else ""
    needs = "\nRequirements: " + spec["needs"] if spec["needs"] else ""
    p = argparse.ArgumentParser(prog=f"wc-{name}", description=spec["summary"],
                                epilog=examples + needs, allow_abbrev=False,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.set_defaults(name=name)
    p.add_argument("--version", action="version", version=f"wc-{name} {VERSION}")
    if name == "help":
        p.add_argument("command", nargs="?", help="command name, with or without wc-")
        p.add_argument("--search", metavar="TEXT", help="filter command names, groups, and descriptions")
        mode = p.add_mutually_exclusive_group()
        mode.add_argument("--all", action="store_true", help="show full help for all suite commands")
        mode.add_argument("--keys", action="store_true", help="show keyboard bindings")
        p.add_argument("--plain", action="store_true", help="do not use a pager for keyboard bindings")
    elif name == "ls":
        p.add_argument("filter", nargs="?", default="", help="filter names, groups, and descriptions")
        p.add_argument("--json", action="store_true")
    elif name == "keys":
        p.add_argument("--plain", action="store_true")
    elif name == "completion":
        p.add_argument("shell", choices=("bash", "zsh", "fish"), nargs="?", default="bash")
    elif name in (*APP_TOOLS, "binsys", "launch", "gparted", "check", "inhibit"):
        if name == "inhibit":
            p.add_argument("--reason", default="Foreground command is running")
        p.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the application; use -- before its options")
    elif name == "term":
        p.add_argument("directory", nargs="?", help="working directory (default: current directory)")
        p.add_argument("--title", help="terminal window title")
        p.add_argument("--exec", dest="execute", nargs=argparse.REMAINDER, help="command to run in the terminal")
    elif name == "project":
        p.add_argument("query", nargs="?", help="project name or substring")
        p.add_argument("--root", type=Path, default=HOME / "Projects")
        p.add_argument("--list", action="store_true", help="list matching directories without opening a terminal")
    elif name == "open":
        p.add_argument("target", help="file, directory, or URL")
    elif name == "files":
        p.add_argument("directory", type=Path, nargs="?", default=Path.cwd())
    elif name == "config":
        p.add_argument("--path", action="store_true", help="print configuration path")
    elif name == "bright":
        p.add_argument("action", type=bright_action, nargs="?", default="status", metavar="status|up|down|0-100")
        p.add_argument("--step", type=bounded_int(1, 100), default=10, help="step percentage (default: 10)")
        p.add_argument("--device", help="device name from --list")
        p.add_argument("--list", action="store_true", help="list backlight devices")
    elif name == "display":
        mode = p.add_mutually_exclusive_group()
        mode.add_argument("--settings", action="store_true")
        mode.add_argument("--json", action="store_true", help="show kernel connector data as JSON")
    elif name == "shot":
        p.add_argument("file", type=Path, nargs="?", help="new PNG file (default: Pictures/Screenshots)")
        p.add_argument("--region", action="store_true", help="select a region with slurp")
        p.add_argument("--copy", action="store_true", help="also copy the image to the clipboard")
    elif name == "clip":
        p.add_argument("action", choices=("copy", "paste", "clear"), nargs="?", default="paste")
        p.add_argument("text", nargs="*")
        p.add_argument("--no-newline", action="store_true", help="omit the extra newline when pasting")
    elif name in ("vol", "mic", "audio"):
        if name != "audio":
            p.add_argument("action", type=action, nargs="?", default="status", metavar="status|up|down|mute|unmute|on|off|0-100")
            p.add_argument("--step", type=bounded_int(1, 100), default=5)
            p.add_argument("--card", type=bounded_int(0, 255), help="ALSA card number")
            p.add_argument("--control", help="ALSA mixer control")
        p.add_argument("--backend", choices=("auto", "pipewire", "alsa", "cube"), default="auto")
    elif name == "net":
        p.add_argument("--probe", action="store_true", help="send one ICMP ping; default report uses local data only")
        p.add_argument("--host", default="1.1.1.1", help="ping target")
    elif name == "wifi":
        p.add_argument("action", choices=("status", "on", "off", "manage", "profiles", "scan", "reconnect"), nargs="?", default="status")
    elif name == "bt":
        p.add_argument("action", choices=("status", "on", "off", "devices"), nargs="?", default="status")
    elif name in ("sys", "bat", "temp", "doctor"):
        p.add_argument("--json", action="store_true")
        if name == "doctor":
            p.add_argument("--strict", action="store_true", help="fail for missing core tools or PATH entry")
    elif name == "disk":
        p.add_argument("path", nargs="?", type=Path)
        p.add_argument("--devices", action="store_true")
    elif name == "top":
        p.add_argument("-n", "--count", type=bounded_int(1, 1000), default=10)
        p.add_argument("--cpu", action="store_true")
        p.add_argument("--interactive", action="store_true")
    elif name == "services":
        p.add_argument("service", nargs="?")
        p.add_argument("--user", action="store_true")
    elif name == "logs":
        mode = p.add_mutually_exclusive_group()
        mode.add_argument("--service", metavar="UNIT")
        mode.add_argument("--journal", action="store_true")
        mode.add_argument("--suite", action="store_true", help="list application launch logs")
        p.add_argument("--user", action="store_true", help="read the user journal")
        p.add_argument("-n", "--count", type=bounded_int(1, 100000), default=50)
        p.add_argument("-f", "--follow", action="store_true")
    elif name in ("off", "reboot", "logout"):
        p.add_argument("-y", "--yes", action="store_true", help="skip the interactive confirmation")
        p.add_argument("--dry-run", action="store_true", help="print the command without executing it")
    elif name == "suspend":
        p.add_argument("--dry-run", action="store_true")
    elif name == "notify":
        p.add_argument("title")
        p.add_argument("body", nargs="*")
        p.add_argument("--urgency", choices=("low", "normal", "critical"), default="normal")
        p.add_argument("--timeout", type=bounded_int(0, 86400000), default=5000, help="notification display time in milliseconds")
    elif name == "timer":
        p.add_argument("duration", nargs="?", type=duration)
        p.add_argument("message", nargs="*")
        mode = p.add_mutually_exclusive_group()
        mode.add_argument("--list", action="store_true")
        mode.add_argument("--cancel", metavar="ID")
        mode.add_argument("--prune", action="store_true", help="remove finished, cancelled, failed, or interrupted records")
        p.add_argument("--json", action="store_true", help="machine-readable --list")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if not argv:
            return help_command(parser("help").parse_args([])) or 0
        name = argv.pop(0).removeprefix("wc-")
        if name == "_timer-worker":
            if len(argv) != 1:
                raise Error("invalid worker arguments", 2)
            return timer_worker(argv[0])
        if name not in COMMANDS:
            raise Error(f"unknown command: {name}; use wc-help", 2)
        if name in (*APP_TOOLS, "binsys", "launch", "gparted", "check") and (not argv or argv[0] not in ("-h", "--help", "--version")):
            # Existing wrappers forwarded application options without requiring --.
            args = argparse.Namespace(name=name, args=argv)
        else:
            args = parser(name).parse_args(argv)
        return COMMANDS[name]["handler"](args) or 0
    except Error as error:
        print(f"wc-{locals().get('name', 'suite')}: {error}", file=sys.stderr)
        return error.code
    except subprocess.TimeoutExpired as error:
        print(f"wc-{locals().get('name', 'suite')}: command timed out after {error.timeout}s", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except (OSError, ValueError) as error:
        print(f"wc-{locals().get('name', 'suite')}: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
