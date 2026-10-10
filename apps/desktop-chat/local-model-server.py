#!/usr/bin/env python3
"""Desktop Chat's private loopback server, using the user's training runtime.

Each model ID identifies an exact checkpoint. No training file is changed.
The existing tg-serve process and its port are independent of this server.
"""
import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from http.server import ThreadingHTTPServer


def checkpoint_entries(directory):
    if not directory.is_dir():
        return []
    entries = []
    for run in directory.iterdir():
        if not run.is_dir() or run.is_symlink():
            continue
        for checkpoint in run.glob('ckpt_*.pt'):
            if not checkpoint.is_file() or checkpoint.is_symlink():
                continue
            try:
                stat = checkpoint.stat()
            except FileNotFoundError:
                continue
            entries.append({'id': f'{run.name}/{checkpoint.name}', 'checkpoint': str(checkpoint),
                            'mtime': stat.st_mtime, 'signature': (stat.st_mtime_ns, stat.st_size, stat.st_ino),
                            'step': None, 'val_loss': None, 'parameters': None})
    return sorted(entries, key=lambda item: item['id'], reverse=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module-dir', required=True)
    parser.add_argument('--runs-dir', required=True)
    args = parser.parse_args()
    sys.path.insert(0, args.module_dir)
    backend = importlib.import_module('tensegrity_serve')
    backend.torch.set_num_threads(min(4, os.cpu_count() or 1))

    class CheckpointStore(backend.ModelStore):
        def runs(self):
            return checkpoint_entries(self.runs_dir)

        def resolve(self, name):
            for item in self.runs():
                if item['id'] == name:
                    return item
            raise LookupError('Checkpoint is unavailable. Refresh the Desktop Chat model list.')

        def get(self, name):
            run = self.resolve(name)
            signature = (run['id'], run['signature'])
            with self._load_lock:
                if getattr(self, '_signature', None) != signature:
                    # Keep only the selected weights warm. A replaced latest/best
                    # checkpoint is reloaded on the next message.
                    self._cache.clear()
                    self._loaded = self._load(run)
                    self._signature = signature
                model, cfg, step, loss = self._loaded
            return run['id'], model, cfg, step, loss

    backend.Handler.store = CheckpointStore(Path(args.runs_dir).expanduser(), 'cpu', 'ckpt_best.pt',
        {'temperature': 0.85, 'top_k': 40, 'max_tokens': 120, 'format': 'raw', 'verbose': False})
    server = ThreadingHTTPServer(('127.0.0.1', 0), backend.Handler)
    server.daemon_threads = True
    print('WHITECUBE_READY ' + json.dumps({'port': server.server_port}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
