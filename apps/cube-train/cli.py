#!/usr/bin/python3 -I
"""Headless trainer commands; importing this entry point never imports PyTorch."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine

VERSION = '1.1.0'
COMPARISON_FIELDS = ('id', 'name', 'model_size', 'device', 'status', 'step', 'parameters',
                     'val_loss', 'bits_per_byte', 'tokens_per_second', 'training_seconds', 'corpus_fingerprint')


def comparison_csv(report):
    """Include comparability in every spreadsheet row and neutralize formulas."""
    stream = io.StringIO(newline='')
    fields = (*COMPARISON_FIELDS, 'comparable', 'warnings')
    writer = csv.writer(stream)
    writer.writerow(fields)
    for row in report['runs']:
        values = {**row, 'comparable': report['comparable'], 'warnings': ' | '.join(report['warnings'])}
        def cell(value):
            if value is None:
                return ''
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                return "'" + value
            return value
        writer.writerow([cell(values.get(field)) for field in fields])
    return stream.getvalue()


def comparison_text(report):
    rows = report['runs']
    if not rows:
        return 'No runs to compare.'
    lines = ['RUN                      SIZE    STEPS       BEST VAL    BITS/BYTE    TOKENS/S']
    for row in rows:
        def number(key, digits=3):
            value = row[key]
            return f'{value:,.{digits}f}' if value is not None else '—'
        lines.append(f'{row["id"]:<24} {row["model_size"].upper():>4} {row["step"]:>9,} '
                     f'{number("val_loss"):>14} {number("bits_per_byte"):>12} {number("tokens_per_second", 0):>11}')
    if report['best_run_id']:
        lines.extend(['', 'Lowest comparable validation loss: ' + report['best_run_id']])
    lines.extend(['', *('Warning: ' + warning for warning in report['warnings']), *report.get('notes', [])])
    return '\n'.join(lines)


def _execute(python, script, args, *, isolated=True):
    python = str(Path(python).expanduser())
    try:
        os.execv(python, [python, '-I' if isolated else '-u', str(HERE / 'pipeline' / script), *args])
    except OSError as error:
        raise ValueError(f'Cannot start training Python {python}: {error}. '
                         'Use --doctor --check-runtime to diagnose the selected environment.') from error


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    program = os.environ.get('WHITECUBE_TRAIN_COMMAND') or Path(sys.argv[0]).name
    if args == ['--version']:
        print(f'Whitecube Trainer {VERSION}')
        return 0
    if args[:1] == ['--doctor']:
        import diagnostics
        return diagnostics.main(args[1:])
    trainer = engine.Trainer()
    if args[:1] == ['--compare-runs']:
        parser = argparse.ArgumentParser(prog=program + ' --compare-runs', description='Compare up to 20 saved runs without loading checkpoints.')
        parser.add_argument('run_ids', nargs='*', help='run IDs; omit for the 20 newest runs')
        formats = parser.add_mutually_exclusive_group()
        formats.add_argument('--json', action='store_true', help='print a JSON comparison report')
        formats.add_argument('--csv', action='store_true', help='print a spreadsheet-safe comparison table')
        options = parser.parse_args(args[1:])
        try:
            report = trainer.compare_runs(options.run_ids or None)
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps(report, indent=2, allow_nan=False) if options.json else
              comparison_csv(report) if options.csv else comparison_text(report), end='' if options.csv else '\n')
        return 0
    if args in (['--list-runs'], ['--status']):
        print(trainer.summary() if args[0] == '--list-runs' else trainer.status_text())
        return 0
    if args[:1] == ['--export-notebook']:
        python = trainer.preferences()['python']
        kernel = engine.installed_kernel(python)
        options = ['--python', python]
        if kernel:
            options += ['--kernel-name', kernel['name'], '--kernel-display-name', kernel['display_name']]
        _execute('/usr/bin/python3', 'export_notebook.py', [*args[1:], *options])
    if args[:1] == ['--benchmark']:
        arguments = args[1:]
        if arguments and not arguments[0].startswith('-'):
            arguments = ['--run-dir', *arguments]
        python = '/usr/bin/python3' if any(flag in arguments for flag in ('--help', '-h')) else trainer.preferences()['python']
        _execute(python, 'benchmark.py', arguments)
    if any(flag in args for flag in ('--help', '-h')):
        from training_options import build_parser
        print(f'Whitecube Trainer {VERSION}\n\n'
              f'  {program} --list-runs\n'
              f'  {program} --status\n'
              f'  {program} --doctor [--check-runtime] [--json] [--python PATH] [--device cpu|cuda]\n'
              f'  {program} --compare-runs [RUN_ID ...] [--json | --csv]\n'
              f'  {program} --benchmark RUN --output REPORT.json [--checkpoint best|latest|stepN]\n'
              f'  {program} --export-notebook RUN --output DIR [--checkpoint best|latest|stepN]\n'
              f'                          [--with-corpus] [--reference] [--no-script]\n\n'
              'Training options (uses the Python selected in the desktop):\n')
        build_parser(prog=program).print_help()
        return 0
    _execute(trainer.preferences()['python'], 'train_supervisor.py', args, isolated=False)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f'whitecube-train: {error}', file=sys.stderr)
        raise SystemExit(2)
