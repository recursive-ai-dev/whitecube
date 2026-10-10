#!/usr/bin/python3 -I
"""Opt-in root test. All firewall writes happen in a new network namespace."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def inside(backend, parent_namespace):
    # Check isolation before importing or invoking the firewall implementation.
    if os.geteuid() != 0 or os.readlink('/proc/self/ns/net') == parent_namespace:
        raise RuntimeError('Refusing firewall tests without a separate root network namespace')
    loader = importlib.machinery.SourceFileLoader('net_switch_kernel_test', str(backend))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    run('/usr/bin/ip', 'link', 'set', 'lo', 'up')
    nft = module.Nft()
    nft.apply('table inet whitecube_test_foreign { chain input { type filter hook input priority 0; policy accept; } }\n')
    foreign = nft.run('list', 'table', 'inet', 'whitecube_test_foreign')
    with tempfile.TemporaryDirectory(prefix='whitecube-net-kernel-') as directory:
        state = module.State(Path(directory) / 'state.json')
        switch = module.Switch(nft=nft, state=state)
        assert switch.change(True).startswith('OFF:')
        assert module.inspect_table(nft.snapshot()) is True
        assert switch.change(True).startswith('OFF:')
        assert state.read() is None
        # Actual IPv4 and IPv6 loopback datagrams must pass the default block.
        families = [(socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')]
        for family, address in families:
            with socket.socket(family, socket.SOCK_DGRAM) as receiver, socket.socket(family, socket.SOCK_DGRAM) as sender:
                receiver.bind((address, 0))
                receiver.settimeout(1)
                sender.sendto(b'whitecube', receiver.getsockname())
                assert receiver.recv(32) == b'whitecube'
        switch.change(True, allow_loopback=False, persist=True)
        assert module.inspect_table(nft.snapshot()) is False
        assert module.persistent_setting(state.read()) is False
        # Both protocols now hit the output drop counter in the real kernel.
        for family, address in families:
            with socket.socket(family, socket.SOCK_DGRAM) as sender:
                try:
                    sender.sendto(b'blocked', (address, 54321))
                except PermissionError:
                    pass
        document = nft.snapshot()
        count = sum(expr['counter']['packets'] for item in document['nftables']
                    if 'rule' in item and item['rule']['chain'] == 'output'
                    for expr in item['rule']['expr'] if 'counter' in expr)
        assert count >= 2, f'Expected IPv4 and IPv6 output drops, observed {count}'
        # Simulate reboot's empty kernel table with the persistence marker retained.
        nft.apply(f'delete table inet {module.TABLE}\n')
        switch.change(True, boot=True)
        assert module.inspect_table(nft.snapshot()) is False
        assert switch.change(False).startswith('ON:')
        assert nft.snapshot() is None and state.read() is None
        assert switch.change(False).startswith('ON:')
        assert nft.run('list', 'table', 'inet', 'whitecube_test_foreign') == foreign
        # A foreign same-name table must be left untouched, even by net-on.
        nft.apply(f'table inet {module.TABLE} {{ comment "foreign owner"; }}\n')
        collision = nft.run('list', 'table', 'inet', module.TABLE)
        try:
            switch.change(False)
        except module.SwitchError:
            pass
        else:
            raise AssertionError('Foreign table was not rejected')
        assert nft.run('list', 'table', 'inet', module.TABLE) == collision
    print('Kernel tests passed: IPv4/IPv6 loopback, drops, persistence, restore, idempotency, foreign firewall preservation.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, default=Path(__file__).with_name('whitecube-net-switch'))
    parser.add_argument('--inside', metavar='PARENT_NETNS')
    arguments = parser.parse_args()
    if arguments.inside:
        inside(arguments.backend, arguments.inside)
        return 0
    if os.geteuid() != 0:
        parser.error('Root is required to create an isolated network namespace; no live firewall is changed')
    parent_namespace = os.readlink('/proc/self/ns/net')
    return subprocess.call(['/usr/bin/unshare', '--net', '--', '/usr/bin/python3', '-I',
                            str(Path(__file__).resolve()), '--backend', str(arguments.backend.resolve()),
                            '--inside', parent_namespace])


if __name__ == '__main__':
    sys.exit(main())
