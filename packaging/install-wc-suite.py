#!/usr/bin/python3 -I
"""install-wc-suite.py DESTDIR: lay the wc command suite out system-wide, for install-tree.sh.

  /usr/bin/wc-*                       one wrapper per command (the per-user template)
  /usr/lib/wc-suite/                  wc_suite.py and README.txt; wrappers find them as ../lib/wc-suite,
                                      exactly like ~/.local/bin and ~/.local/lib/wc-suite per user
  /usr/share/{bash-completion,zsh,fish}   completions for every command

The per-user installer (apps/wc-suite/install.py) stays the way to install into a home directory.
"""
import argparse
import contextlib
import io
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'apps/wc-suite'
sys.path.insert(0, str(SOURCE))
import wc_suite as suite  # noqa: E402


def completion(shell):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        suite.completion(argparse.Namespace(shell=shell))
    return stream.getvalue()


def write(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('destdir', type=Path)
    dest = parser.parse_args().destdir
    for name in ('wc_suite.py', 'README.txt'):
        write(dest / 'usr/lib/wc-suite' / name, (SOURCE / name).read_text(), 0o644)
    for name, spec in sorted(suite.COMMANDS.items()):
        write(dest / f'usr/bin/wc-{name}',
              '#!/bin/sh\n# desc: ' + spec['summary'] + '\n'
              'command -v python3 >/dev/null 2>&1 || { echo "wc-suite requires Python 3" >&2; exit 127; }\n'
              'wc_bin_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
              'PATH="$wc_bin_dir:${PATH:-/usr/bin:/bin}"\nexport PATH\n'
              f'exec python3 "$wc_bin_dir/../lib/wc-suite/wc_suite.py" "{name}" "$@"\n', 0o755)
    bash = completion('bash')
    fish = completion('fish').splitlines()
    for name in suite.COMMANDS:
        write(dest / f'usr/share/bash-completion/completions/wc-{name}', bash, 0o644)
        lines = [line for line in fish if line.startswith(f'complete -c wc-{name} ')]
        write(dest / f'usr/share/fish/vendor_completions.d/wc-{name}.fish', '\n'.join(lines) + '\n', 0o644)
    write(dest / 'usr/share/zsh/vendor-completions/_wc', completion('zsh'), 0o644)
    print(f'wc suite: {len(suite.COMMANDS)} commands with completions')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
