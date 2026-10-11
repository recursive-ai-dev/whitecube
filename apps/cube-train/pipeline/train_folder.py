"""Train a local 1M/2M/5M/10M Tensegrity model from ~/train-in."""
import argparse
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
import torch

from prepare_folder import prepare_folder
from train_local import train, MODEL_PRESETS, add_model_size_flags
from files import atomic_json
from train_supervisor import cpu_temperature
from pacing import TrainingPacer


class Tee:
    def __init__(self, terminal, log):
        self.terminal, self.log = terminal, log

    def write(self, text):
        self.terminal.write(text)
        self.log.write(text)
        self.log.flush()
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.log.flush()


class PreparationProgress:
    """Surface preparation phases while bounding metadata writes on large corpora."""
    def __init__(self, path, device, *, clock=time.monotonic):
        self.path, self.device, self.clock = path, device, clock
        self.last_phase = None
        self.next_update = 0

    def __call__(self, event):
        phase = event.get('phase', 'reading')
        now = self.clock()
        changed = phase != self.last_phase
        if not changed and now < self.next_update:
            return
        tokens, documents = event.get('tokens', 0), event.get('documents', 0)
        details = {'checking': 'Checking source files', 'reading': 'Reading source documents',
                   'splitting': 'Building training and validation splits', 'publishing': 'Saving prepared corpus',
                   'reusing': 'Copying prepared corpus', 'verifying': 'Verifying prepared corpus'}
        detail = details.get(phase, 'Preparing corpus')
        if documents or tokens:
            detail += f' · {documents:,} documents · {tokens:,} tokens'
        atomic_json(self.path, {'state': 'preparing', 'device': self.device, 'detail': detail,
                               'preparation_phase': phase, 'prepared_tokens': tokens,
                               'prepared_documents': documents})
        if changed:
            print(detail, flush=True)
        self.last_phase, self.next_update = phase, now + .25


THERMAL_PAUSE_MARGIN = 5      # pause this far below a stop limit
THERMAL_RESUME_MARGIN = 10    # resume once this far below the limit
THERMAL_PAUSE_LIMIT = 15 * 60  # stop if cooling takes longer than this


class Monitor:
    def __init__(self, path, max_temp, device='cuda', max_cpu_temp=85, burst_seconds=0, cooldown_seconds=10,
                 max_minutes=0, thermal_pause=False, *, clock=time.monotonic, sleep=time.sleep):
        self.path, self.max_temp = path, max_temp
        self.device, self.max_cpu_temp = device, max_cpu_temp
        self.stopped = False
        self.stop_reason = None
        self.next_check = 0
        self.pacer = TrainingPacer(burst_seconds, cooldown_seconds)
        self.target_steps = None
        self.max_minutes, self.thermal_pause = max_minutes, thermal_pause
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.gpu_reading = self.cpu_reading = None

    def stop(self, reason):
        if not self.stopped:
            self.stopped, self.stop_reason = True, reason

    def request_stop(self, signum, frame):
        self.stop('stop_requested')
        print("Stop requested; finishing the current optimizer step and saving.", flush=True)

    def write_status(self, state, detail=''):
        atomic_json(self.path.parent / 'status.json', {'state': state, 'device': self.device, 'detail': detail})

    def __call__(self, step):
        if self.max_minutes and self.clock() - self.started >= self.max_minutes * 60:
            if not self.stopped:
                print(f'Time limit of {self.max_minutes:g} minutes reached; saving and stopping.', flush=True)
            self.stop('time_limit')
        if self.check_health(step):
            return True
        if self.target_steps is not None and step >= self.target_steps:
            return False
        if self.thermal_pause and self.too_warm(THERMAL_PAUSE_MARGIN) and self.cool_down(step):
            return True
        def cooling(active):
            if active and self.device == 'cuda':
                torch.cuda.synchronize()
            self.write_status('stopped' if self.stopped else 'cooling' if active else 'training',
                              f'Scheduled cooldown: {self.pacer.cooldown_seconds:g} seconds' if active else '')
            if active:
                print(f'Cooling break for {self.pacer.cooldown_seconds:g} seconds; Stop remains available.', flush=True)
        return self.pacer.after_step(lambda: self.check_health(step), cooling)

    def too_warm(self, margin):
        return bool(self.gpu_reading is not None and self.gpu_reading >= self.max_temp - margin
                    or self.cpu_reading is not None and self.cpu_reading >= self.max_cpu_temp - margin)

    def cool_down(self, step):
        """Pause between steps until temperatures fall; the stop limits stay active."""
        if self.device == 'cuda':
            torch.cuda.synchronize()
        readings = ' · '.join(text for text in (
            self.gpu_reading is not None and f'GPU {self.gpu_reading} °C',
            self.cpu_reading is not None and f'CPU {self.cpu_reading} °C') if text)
        print(f'Thermal pause at {readings}; waiting to cool before continuing.', flush=True)
        self.write_status('cooling', f'Thermal pause: {readings}')
        deadline = self.clock() + THERMAL_PAUSE_LIMIT
        try:
            while self.too_warm(THERMAL_RESUME_MARGIN):
                if self.clock() >= deadline:
                    print('Temperatures stayed high for 15 minutes; saving and stopping.', flush=True)
                    self.stop('thermal_timeout')
                if self.stopped:
                    return True
                self.sleep(1)
                self.next_check = 0
                if self.check_health(step):
                    return True
        finally:
            self.write_status('stopped' if self.stopped else 'training')
        print('Cooled; training continues.', flush=True)
        self.pacer.reset()
        return False

    def check_health(self, step):
        if self.stopped:
            return True
        if self.clock() < self.next_check:
            return False
        self.next_check = self.clock() + 5
        record = {"time": datetime.now().isoformat(), "step": step}
        cpu = cpu_temperature()
        record['cpu_temperature_c'] = self.cpu_reading = cpu
        if cpu is not None and cpu >= self.max_cpu_temp:
            print(f'CPU temperature reached {cpu} C; stopping and checkpointing.')
            self.stop('thermal')
        try:
            if self.device != 'cuda':
                with self.path.open('a') as log:
                    log.write(json.dumps(record) + '\n')
                return self.stopped
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total,utilization.gpu",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
            fields = result.stdout.splitlines()[0].split(",")
            record.update(zip(("temperature_c", "memory_used_mib", "memory_total_mib", "utilization_pct"),
                              (int(f.strip()) for f in fields)))
            self.gpu_reading = record['temperature_c']
            if record["temperature_c"] >= self.max_temp:
                print(f"Temperature reached {record['temperature_c']} C; stopping at the configured {self.max_temp} C limit.")
                self.stop('thermal')
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
            record["telemetry_error"] = str(error)
        with self.path.open("a") as log:
            log.write(json.dumps(record) + "\n")
        return self.stopped


# Hyperparameters chosen for a new run and reused when it is resumed.
HYPERPARAMETERS = {'learning_rate': 3e-4, 'min_lr_ratio': .1, 'warmup_steps': None, 'weight_decay': .1,
                   'grad_clip': 1.0, 'eval_interval': 50, 'seed': 1337, 'schedule': 'cosine', 'val_fraction': .05}
# Operational controls; a resume may change them.
CONTROLS = {'early_stop_patience': 0, 'keep_snapshots': 0, 'max_minutes': 0, 'thermal_pause': False}


def read_previous_result(run):
    try:
        value = json.loads((run / 'result.json').read_text(encoding='utf-8'))
        seconds = value.get('total_training_seconds', 0) if isinstance(value, dict) else 0
        return {'total_training_seconds': seconds if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0 else 0}
    except (OSError, ValueError):
        return {'total_training_seconds': 0}


def main():
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    add_model_size_flags(parser)
    parser.add_argument("dataset", nargs="?", type=Path, default=home / "train-in",
                        help='Raw corpus file/folder or an existing prepared data folder')
    parser.add_argument("--max-tokens", type=int, default=20_000_000,
                        help="Corpus cap including validation; not a GPU memory limit (default 20M byte tokens)")
    parser.add_argument("--column", help="Override text field; dotted fields such as document.html supported")
    parser.add_argument('--deduplicate', action='store_true', help='Remove exact duplicate documents before the token cap')
    parser.add_argument('--shuffle-files', action='store_true', help='Shuffle source shards with the split seed before applying the cap')
    parser.add_argument('--min-document-bytes', type=int, default=0, help='Skip documents below this retained UTF-8 byte length (0 disables)')
    parser.add_argument('--max-document-bytes', type=int, default=0, help='Truncate each document at this UTF-8 byte length (0 disables)')
    parser.add_argument('--batch-rows', type=int, default=32, help='Parquet rows decoded per batch (default 32)')
    parser.add_argument("--steps", type=int, help="Total optimizer steps; default one corpus-equivalent token budget")
    parser.add_argument("--schedule-steps", type=int, help="Explicit LR decay horizon; defaults to saved plan on resume")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--grad-accum-steps", type=int, default=4)
    parser.add_argument("--max-temp", type=int, default=70, help="Stop and checkpoint at this GPU temperature (default 70 C)")
    parser.add_argument("--max-cpu-temp", type=int, default=85, help="CPU temperature watchdog limit (default 85 C)")
    parser.add_argument("--cpu-threads", type=int, default=2, help="PyTorch CPU thread limit (default 2)")
    parser.add_argument('--burst-seconds', type=float, default=0, help='Stagger training: work for this many seconds before cooling (0 disables)')
    parser.add_argument('--cooldown-seconds', type=float, default=10, help='Seconds to pause between training bursts (default 10)')
    parser.add_argument('--low-priority', action='store_true', help='Run training with reduced CPU priority and idle disk I/O priority')
    parser.add_argument("--run-dir", type=Path, help="New run directory, default ~/train-out/<timestamp>")
    parser.add_argument('--name', default='', help='Friendly run name (training folder keeps its stable ID)')
    parser.add_argument("--resume", type=Path, help="Resume an existing run directory; input files are not re-parsed")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    tuning = parser.add_argument_group('training plan (new runs; resume reuses the saved values)')
    tuning.add_argument('--learning-rate', type=float, help='Peak learning rate (default 3e-4)')
    tuning.add_argument('--min-lr-ratio', type=float, help='Final learning rate as a fraction of the peak (default 0.1)')
    tuning.add_argument('--warmup-steps', type=int, help='Linear warmup steps (default: a tenth of the plan, up to 50)')
    tuning.add_argument('--weight-decay', type=float, help='AdamW weight decay (default 0.1)')
    tuning.add_argument('--grad-clip', type=float, help='Gradient norm clip; 0 disables (default 1.0)')
    tuning.add_argument('--eval-interval', type=int, help='Validate and checkpoint every N steps (default 50)')
    tuning.add_argument('--seed', type=int, help='Seed for weights, batches and the corpus split (default 1337)')
    tuning.add_argument('--schedule', choices=('cosine', 'wsd'), help='Cosine decay, or warmup-stable-decay (default cosine)')
    tuning.add_argument('--val-fraction', type=float, help='Validation share of documents, 0.01-0.5 (default 0.05)')
    control = parser.add_argument_group('run controls (may change on resume)')
    control.add_argument('--early-stop-patience', type=int, help='Stop after N evaluations without a new best (0 disables)')
    control.add_argument('--keep-snapshots', type=int, help='Keep N step-numbered checkpoint copies (0 disables)')
    control.add_argument('--max-minutes', type=float, help='Save and stop after this many minutes of training (0 disables)')
    control.add_argument('--thermal-pause', action=argparse.BooleanOptionalAction, default=None,
                         help=f'Pause {THERMAL_PAUSE_MARGIN} °C below a stop limit until {THERMAL_RESUME_MARGIN} °C below it')
    args = parser.parse_args()
    if len(args.name.strip()) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in args.name):
        parser.error('Use a run name of up to 80 characters without line breaks')
    if args.resume and (args.run_dir or args.prepare_only):
        parser.error("--resume cannot be combined with --run-dir or --prepare-only")
    if args.schedule_steps is not None and args.schedule_steps < 1:
        parser.error("Schedule steps must be positive")
    if args.max_tokens < 6000 or (args.steps is not None and args.steps < 1):
        parser.error("Use at least 6,000 corpus tokens and positive steps")
    if min(args.min_document_bytes, args.max_document_bytes) < 0 or args.batch_rows < 1:
        parser.error('Document byte limits must be nonnegative; batch rows must be positive')
    if args.max_document_bytes and args.min_document_bytes > args.max_document_bytes:
        parser.error('Minimum document bytes cannot exceed the maximum')
    if args.resume and (args.deduplicate or args.shuffle_files or args.min_document_bytes
                        or args.max_document_bytes or args.batch_rows != 32):
        parser.error('Resume reuses the saved corpus; document selection controls apply to new runs')
    if args.batch_size < 1 or args.grad_accum_steps < 1 or not 50 <= args.max_temp <= 90:
        parser.error("Batch/accumulation must be positive; temperature limit must be 50-90 C")
    if args.cpu_threads < 1 or not 50 <= args.max_cpu_temp <= 95:
        parser.error("CPU threads must be positive; CPU temperature limit must be 50-95 C")
    if not 0 <= args.burst_seconds <= 3600 or not 0 < args.cooldown_seconds <= 600:
        parser.error('Training bursts must be 0-3600 seconds and cooldowns greater than 0, up to 600 seconds')
    checks = {'learning_rate': (1e-6, .1), 'min_lr_ratio': (0, 1), 'weight_decay': (0, 1), 'grad_clip': (0, 100),
              'val_fraction': (.01, .5), 'warmup_steps': (0, 1_000_000), 'eval_interval': (1, 10_000_000),
              'seed': (0, 2**31 - 1), 'early_stop_patience': (0, 1000), 'keep_snapshots': (0, 100), 'max_minutes': (0, 525_600)}
    for key, (low, high) in checks.items():
        value = getattr(args, key)
        if value is not None and not (math.isfinite(value) and low <= value <= high):
            parser.error(f'--{key.replace("_", "-")} must be between {low:g} and {high:g}')
    if args.resume and any(getattr(args, key) is not None for key in ('seed', 'val_fraction')):
        parser.error('--seed and --val-fraction apply to new runs only')
    if args.low_priority:
        os.setpriority(os.PRIO_PROCESS, 0, max(10, os.getpriority(os.PRIO_PROCESS, 0)))
        try:
            subprocess.run(['ionice', '-c', '3', '-p', str(os.getpid())], capture_output=True, check=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as error:
            print(f'Idle disk priority unavailable: {error}', file=sys.stderr)
    torch.set_num_threads(args.cpu_threads)
    torch.set_num_interop_threads(1)
    # This lock is shared with desktop launch/trash and existing CLI installs.
    state = home / '.local' / 'state' / 'tensegrity'
    state.mkdir(parents=True, exist_ok=True)
    with (state / "train.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Another train process is active. Let it finish or stop it first.\n")
        if args.resume:
            run = args.resume.expanduser().resolve()
            config = json.loads((run / "run.json").read_text())
            saved_size = config.get("model_size", "1m")
            if args.model_size is not None and args.model_size != saved_size:
                parser.error(f"--{args.model_size} conflicts with this {saved_size} run; "
                             "resume without a size flag or start a new run")
            args.model_size = saved_size
            data = run / "data"
            if not (data / "train.bin").is_file() or not (data / "val.bin").is_file():
                parser.exit(1, f"No prepared corpus in {run}\n")
            previous_result = json.loads((run / 'result.json').read_text()) if (run / 'result.json').is_file() else {}
            if previous_result.get('completed_steps', 0) > 0 and not (run / 'ckpt_latest.pt').is_file():
                parser.error('Continuing training requires ckpt_latest.pt. Restore it or start a new run.')
            args.batch_size = config["batch_size"]
            args.grad_accum_steps = config["grad_accum_steps"]
            # The optimizer and LR plan are restored from the checkpoint; keep
            # the run record and the remaining settings consistent with it.
            changed = [key for key in ('learning_rate', 'min_lr_ratio', 'warmup_steps', 'weight_decay', 'schedule')
                       if getattr(args, key) is not None and getattr(args, key) != config.get(key, HYPERPARAMETERS[key])]
            if changed:
                parser.error('Resume reuses the saved learning-rate plan and optimizer; remove ' +
                             ', '.join('--' + key.replace('_', '-') for key in changed))
            steps = args.steps if args.steps is not None else config["steps"]
        else:
            args.model_size = args.model_size or "1m"
            run = (args.run_dir or home / "train-out" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")).expanduser().resolve()
            run.mkdir(parents=True, exist_ok=False)
            data = run / "data"
            steps = None
        previous_out, previous_err = sys.stdout, sys.stderr
        if os.environ.get("TENSEGRITY_RUN_REPORT"):
            Path(os.environ["TENSEGRITY_RUN_REPORT"]).write_text(str(run))
        plan = {key: config.get(key, default) if args.resume else default for key, default in HYPERPARAMETERS.items()}
        plan.update({key: getattr(args, key) for key in HYPERPARAMETERS if getattr(args, key) is not None})
        controls = {key: config.get(key, default) if args.resume else default for key, default in CONTROLS.items()}
        controls.update({key: getattr(args, key) for key in CONTROLS if getattr(args, key) is not None})
        with (run / "console.log").open("a") as log:
            sys.stdout, sys.stderr = Tee(previous_out, log), Tee(previous_err, log)
            try:
                monitor = Monitor(run / 'gpu.jsonl', args.max_temp, args.device, args.max_cpu_temp,
                                  args.burst_seconds, args.cooldown_seconds, controls['max_minutes'],
                                  controls['thermal_pause'])
                previous_handlers = {sig: signal.signal(sig, monitor.request_stop)
                                     for sig in (signal.SIGINT, signal.SIGTERM)}
                atomic_json(run / 'status.json', {'state': 'preparing' if not args.resume else 'starting', 'device': args.device})
                print(f"Run directory: {run}", flush=True)
                if not args.resume:
                    corpus = prepare_folder(args.dataset.expanduser(), data, max_tokens=args.max_tokens, column=args.column,
                                            val_fraction=plan['val_fraction'], seed=plan['seed'],
                                            deduplicate=args.deduplicate, shuffle_files=args.shuffle_files,
                                            min_document_bytes=args.min_document_bytes, max_document_bytes=args.max_document_bytes,
                                            batch_rows=args.batch_rows,
                                            stop_requested=lambda: monitor.stopped,
                                            progress_callback=PreparationProgress(run / 'status.json', args.device))
                    steps = args.steps or math.ceil(corpus["train_tokens"] / (args.batch_size * args.grad_accum_steps * 256))
                    config = {"steps": steps, "batch_size": args.batch_size,
                              "grad_accum_steps": args.grad_accum_steps, "max_temp": args.max_temp,
                              "dataset": str(args.dataset.expanduser().resolve()), "max_tokens": corpus.get('max_tokens', args.max_tokens),
                              "block_size": 256, "model_size": args.model_size,
                              "parameters": MODEL_PRESETS[args.model_size]["parameters"], 'display_name': args.name.strip(),
                              'device': args.device, 'max_cpu_temp': args.max_cpu_temp,
                              'created_at': datetime.now().astimezone().isoformat(timespec='seconds')}
                    config.update({key: corpus.get(key, default) for key, default in
                                   {'deduplicate': False, 'shuffle_files': False, 'min_document_bytes': 0,
                                    'max_document_bytes': 0, 'batch_rows': 32, 'column': None}.items()})
                    config['column'] = config['column'] or ''
                    config.update(corpus_split_seed=corpus.get('split_seed'),
                                  corpus_val_fraction=corpus.get('val_fraction'),
                                  reused_corpus=corpus.get('reused_from'))
                    atomic_json(run / 'run.json', config)
                else:
                    config['steps'] = steps
                    config['device'] = args.device
                config.update(plan, **controls)
                config.update(burst_seconds=args.burst_seconds, cooldown_seconds=args.cooldown_seconds,
                              low_priority=args.low_priority, max_temp=args.max_temp, max_cpu_temp=args.max_cpu_temp)
                atomic_json(run / 'run.json', config)
                if args.prepare_only:
                    atomic_json(run / 'status.json', {'state': 'prepared', 'device': args.device})
                    print(f"Prepared. Start training with: train --resume {run}")
                    return
                print(f"Training {args.model_size} model to {steps:,} total steps; batch={args.batch_size}, accumulation={args.grad_accum_steps}, context=256.")
                print(f"Plan: {plan['schedule']} schedule, peak LR {plan['learning_rate']:g}, weight decay {plan['weight_decay']:g}, "
                      f"clip {plan['grad_clip']:g}, evaluate every {plan['eval_interval']:,} steps, seed {plan['seed']}.", flush=True)
                if monitor.stopped:
                    raise InterruptedError('Stopped before training.')
                if monitor.check_health(0):
                    raise InterruptedError('Temperature limit reached before training.')
                monitor.pacer.reset()
                monitor.started = monitor.clock()
                monitor.target_steps = steps
                atomic_json(run / 'status.json', {'state': 'training', 'device': args.device})
                outcome = {}
                started = time.monotonic()
                result = train(data, run, steps=steps, batch_size=args.batch_size,
                               grad_accum_steps=args.grad_accum_steps, device=args.device,
                               resume=bool(args.resume and (run / "ckpt_latest.pt").is_file()), stop_requested=monitor, schedule_steps=args.schedule_steps, model_size=args.model_size,
                               learning_rate=plan['learning_rate'], min_lr_ratio=plan['min_lr_ratio'],
                               warmup_steps=plan['warmup_steps'], weight_decay=plan['weight_decay'],
                               grad_clip=plan['grad_clip'], eval_interval=plan['eval_interval'], seed=plan['seed'],
                               schedule=plan['schedule'], early_stop_patience=controls['early_stop_patience'],
                               keep_snapshots=controls['keep_snapshots'], outcome=outcome)
                previous = read_previous_result(run)
                reason = outcome.get('stop_reason', 'completed')
                if reason == 'stop_requested':
                    reason = monitor.stop_reason or reason
                elapsed = time.monotonic() - started
                atomic_json(run / 'result.json', {
                    "completed_steps": result[0], "best_val_loss": result[1], "stopped_early": result[0] < steps,
                    'stop_reason': reason if result[0] < steps else 'completed',
                    'final_loss': outcome.get('final_loss'), 'final_val_loss': outcome.get('final_val_loss'),
                    'elapsed_seconds': round(elapsed, 3),
                    'total_training_seconds': round(previous.get('total_training_seconds', 0) + elapsed, 3),
                    'peak_vram_mib': outcome.get('peak_vram_mib'),
                    'finished_at': datetime.now().astimezone().isoformat(timespec='seconds')})
                atomic_json(run / 'status.json', {'state': 'stopped' if result[0] < steps else 'completed', 'device': args.device})
                print(f"Checkpoint: {run / 'ckpt_latest.pt'}")
                print(f"Continue with: train --resume {run} --steps {max(steps, result[0]) + 1000}")
            except InterruptedError as error:
                atomic_json(run / 'status.json', {'state': 'stopped', 'detail': str(error)})
                print(str(error), flush=True)
                raise SystemExit(130)
            except Exception as error:
                atomic_json(run / 'status.json', {'state': 'failed', 'detail': str(error)})
                traceback.print_exc()
                raise SystemExit(1)
            finally:
                sys.stdout, sys.stderr = previous_out, previous_err
                for sig, handler in locals().get('previous_handlers', {}).items():
                    signal.signal(sig, handler)


if __name__ == "__main__":
    main()
