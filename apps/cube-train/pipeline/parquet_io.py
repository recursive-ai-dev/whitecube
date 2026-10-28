"""Parquet text access, including the package's isolated decoder runtime.

The training Python may have its own PyArrow. Otherwise only the decoder child
loads the bundled CPython extension; training environments keep their own ABI
and dependencies. Inspection uses this module without importing NumPy or Torch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

TEXT_FIELDS = ('text', 'content', 'story', 'body', 'document.html')
RUNTIME = Path(__file__).resolve().parent.parent / 'parquet-runtime'
RUNTIME_FORMAT = 'org.whitecube.trainer.parquet-runtime'
_DECODER = None

try:
    if __name__ == '__main__' and sys.argv[1:2] == ['--worker']:
        raise ImportError('Private decoder imports its verified runtime below')
    import pyarrow as pa
    import pyarrow.parquet as pq
    NATIVE_ERROR = None
except (ImportError, OSError) as error:
    pa = pq = None
    NATIVE_ERROR = str(error)


def runtime_command(runtime=RUNTIME):
    runtime = Path(runtime).resolve()
    try:
        manifest = json.loads((runtime / 'runtime.json').read_text())
        python = Path(manifest['python'])
        if manifest.get('format') != RUNTIME_FORMAT or manifest.get('version') != 1:
            raise ValueError('unrecognized runtime manifest')
        if not python.is_absolute() or not python.is_file():
            raise ValueError(f"{python} is unavailable; reinstall the package's Python runtime dependency")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ImportError('Parquet decoder unavailable. Reinstall Whitecube Trainer, or install PyArrow in '
                          f'{sys.executable} with {sys.executable} -m pip install pyarrow. Details: {error}') from error
    return [str(python), '-I', str(Path(__file__).resolve()), '--worker', str(runtime)]


def _checked(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ImportError(f'Parquet decoder could not start: {error}') from error
    if result.returncode:
        raise ValueError('Parquet decoder failed: ' + result.stderr[-2000:].strip())
    return json.loads(result.stdout)


def decoder():
    global _DECODER
    if _DECODER is not None:
        return _DECODER
    system = ['/usr/bin/python3', '-I', str(Path(__file__).resolve()), '--worker-system']
    try:
        info = _checked([*system, 'check'])
        _DECODER = system, 'system decoder', info['version']
    except (ImportError, OSError, ValueError, KeyError):
        command = runtime_command()
        info = _checked([*command, 'check'])
        _DECODER = command, 'packaged decoder', info['version']
    return _DECODER


def support():
    """Check the actual reader import, including fallback, without training imports."""
    if NATIVE_ERROR is None:
        return {'available': True, 'provider': 'training Python', 'version': pa.__version__, 'error': None}
    try:
        _, provider, version = decoder()
        return {'available': True, 'provider': provider, 'version': version, 'error': None}
    except (ImportError, OSError, ValueError, KeyError) as error:
        return {'available': False, 'provider': None, 'version': None, 'error': str(error)}


class _Type:
    def __init__(self, value):
        self.kind = value['kind']
        self.fields = [_Field(item) for item in value.get('fields', [])]
        self.num_fields = len(self.fields)
        self.value_type = _Type(value['value_type']) if 'value_type' in value else None

    def field(self, name):
        if isinstance(name, int):
            return self.fields[name]
        return next((field for field in self.fields if field.name == name), None) or self._missing(name)

    @staticmethod
    def _missing(name):
        raise KeyError(name)


class _Field:
    def __init__(self, value):
        self.name = value['name']
        self.type = _Type(value['type'])


class _Schema(_Type):
    def __init__(self, fields):
        super().__init__({'kind': 'struct', 'fields': fields})
        self.names = [field.name for field in self.fields]

    def __iter__(self):
        return iter(self.fields)


class _Batch:
    def __init__(self, value):
        self.names = value['names']
        self.values = value['columns']
        self.num_rows = len(self.values[0]) if self.values else 0

    def column(self, index):
        return [SimpleNamespace(as_py=lambda value=value: value) for value in self.values[index]]

    def to_pydict(self):
        return dict(zip(self.names, self.values))


class _RemoteParquetFile:
    def __init__(self, path):
        self.path = str(path)
        self.command = decoder()[0]
        info = _checked([*self.command, 'schema', self.path])
        self.schema_arrow = _Schema(info['fields'])
        self.metadata = SimpleNamespace(num_rows=info['rows'])
        self.children = set()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        for process in tuple(self.children):
            self._stop(process)

    def _stop(self, process):
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        if process.stdout:
            process.stdout.close()
        self.children.discard(process)

    def iter_batches(self, *, batch_size=32, columns, use_threads=False, stop_requested=None):
        if not 1 <= len(columns) <= 128 or any(not isinstance(column, str) for column in columns) or not isinstance(batch_size, int) or not 1 <= batch_size <= 65536:
            raise ValueError('Choose Parquet columns and a batch size of 1–65,536 rows.')
        with tempfile.TemporaryFile(mode='w+t') as errors:
            process = subprocess.Popen([*self.command, 'rows', self.path, json.dumps(columns), str(batch_size)],
                                       stdout=subprocess.PIPE, stderr=errors)
            self.children.add(process)
            try:
                pending = bytearray()
                deadline = time.monotonic() + 120
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        if stop_requested and stop_requested():
                            raise InterruptedError('Corpus preparation cancelled')
                        if b'\n' in pending:
                            line, _, rest = pending.partition(b'\n')
                            pending = bytearray(rest)
                            yield _Batch(json.loads(line))
                            continue
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Parquet decoder stalled for two minutes; try a smaller batch size or another file.')
                        if not selector.select(timeout=.1):
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            if pending.strip():
                                yield _Batch(json.loads(pending))
                            break
                        pending.extend(chunk)
                        deadline = time.monotonic() + 120
                if process.wait(timeout=2):
                    errors.seek(0)
                    raise ValueError('Parquet decoder failed: ' + errors.read(2000).strip())
            finally:
                self._stop(process)


def read_batches(source, *, columns, batch_size=32, stop_requested=None):
    if isinstance(source, _RemoteParquetFile):
        yield from source.iter_batches(columns=columns, batch_size=batch_size, stop_requested=stop_requested)
    else:
        for batch in source.iter_batches(columns=columns, batch_size=batch_size, use_threads=False):
            if stop_requested and stop_requested():
                raise InterruptedError('Corpus preparation cancelled')
            yield batch


if NATIVE_ERROR is not None:
    # This small protocol covers typed text/struct projection used by preparation
    # and inspection; it does not load foreign extension modules into the parent.
    pa = SimpleNamespace(types=SimpleNamespace(**{
        'is_' + kind: (lambda typ, kind=kind: typ.kind == kind)
        for kind in ('dictionary', 'struct', 'string', 'large_string')}))
    pq = SimpleNamespace(ParquetFile=_RemoteParquetFile)


def field_type(schema, name):
    parts = [name] if name in schema.names else name.split('.')
    typ = schema.field(parts[0]).type
    for part in parts[1:]:
        if pa.types.is_dictionary(typ):
            typ = typ.value_type
        if not pa.types.is_struct(typ):
            raise KeyError(name)
        typ = typ.field(part).type
    return typ.value_type if pa.types.is_dictionary(typ) else typ


def pick_column(schema, requested=None):
    if requested is not None and (not isinstance(requested, str) or not requested.strip()):
        raise ValueError('Text column must be a nonempty string')
    for name in [requested] if requested is not None else TEXT_FIELDS:
        try:
            typ = field_type(schema, name)
            if pa.types.is_string(typ) or pa.types.is_large_string(typ):
                return name
        except (KeyError, AttributeError, IndexError):
            pass
    raise ValueError(f'No recognized text column. Available fields: {schema.names}; use --column')


def _worker(arguments, system=False):
    manifest = None
    if system:
        mode, *args = arguments
    else:
        runtime, mode, *args = arguments
        manifest = json.loads((Path(runtime) / 'runtime.json').read_text())
        actual = f'{sys.version_info.major}.{sys.version_info.minor}'
        if manifest['python_version'] != actual:
            raise RuntimeError(f'Packaged Parquet decoder requires Python {manifest["python_version"]}; found {actual}. Reinstall its Python dependency.')
        sys.path.insert(0, runtime)
    import pyarrow as arrow
    import pyarrow.parquet as parquet
    if manifest and arrow.__version__ != manifest['pyarrow_version']:
        raise RuntimeError('Packaged PyArrow version does not match its manifest; reinstall Whitecube Trainer.')
    def describe(typ):
        if arrow.types.is_dictionary(typ):
            return {'kind': 'dictionary', 'value_type': describe(typ.value_type)}
        if arrow.types.is_struct(typ):
            return {'kind': 'struct', 'fields': [field(item) for item in typ]}
        return {'kind': 'string' if arrow.types.is_string(typ) else 'large_string' if arrow.types.is_large_string(typ) else 'other'}
    def field(item):
        return {'name': item.name, 'type': describe(item.type)}
    if mode == 'check':
        print(json.dumps({'version': arrow.__version__}))
        return
    with parquet.ParquetFile(args[0]) as source:
        if mode == 'schema':
            print(json.dumps({'fields': [field(item) for item in source.schema_arrow], 'rows': source.metadata.num_rows}))
        elif mode == 'rows':
            selected, batch_rows = json.loads(args[1]), int(args[2])
            if not 1 <= batch_rows <= 65536 or not isinstance(selected, list) or not 1 <= len(selected) <= 128 or any(not isinstance(name, str) for name in selected):
                raise ValueError('Invalid Parquet columns or batch size')
            for batch in source.iter_batches(batch_size=batch_rows, columns=selected, use_threads=False):
                print(json.dumps({'names': batch.schema.names,
                                  'columns': [[value.as_py() for value in batch.column(index)] for index in range(batch.num_columns)]},
                                 ensure_ascii=False, default=lambda value: {'unsupported_scalar_type': type(value).__name__}), flush=True)
        else:
            raise ValueError('Unknown Parquet decoder operation')


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['--worker']:
            _worker(sys.argv[2:])
        elif sys.argv[1:2] == ['--worker-system']:
            _worker(sys.argv[2:], system=True)
        elif sys.argv[1:] == ['--status']:
            print(json.dumps(support()))
        else:
            raise ValueError('Use --status to check Parquet support')
    except (ImportError, OSError, ValueError, RuntimeError, KeyError) as error:
        print(f'Parquet: {error}', file=sys.stderr)
        raise SystemExit(2)
