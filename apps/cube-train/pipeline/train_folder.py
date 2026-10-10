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

    def flush(self):
        self.terminal.flush()
        self.log.flush()


class Monitor:
    def __init__(self, path, max_temp, device='cuda', max_cpu_temp=85, burst_seconds=0, cooldown_seconds=10):
        self.path, self.max_temp = path, max_temp
        self.device, self.max_cpu_temp = device, max_cpu_temp
        self.stopped = False
        self.next_check = 0
        self.pacer = TrainingPacer(burst_seconds, cooldown_seconds)
        self.target_steps = None

    def request_stop(self, signum, frame):
        self.stopped = True
        print("Stop requested; finishing the current optimizer step and saving.", flush=True)

    def __call__(self, step):
        if self.check_health(step):
            return True
        if self.target_steps is not None and step >= self.target_steps:
            return False
        def cooling(active):
            atomic_json(self.path.parent / 'status.json', {
                'state': 'stopped' if self.stopped else 'cooling' if active else 'training',
                'device': self.device,
                'detail': f'Scheduled cooldown: {self.pacer.cooldown_seconds:g} seconds' if active else '',
            })
            if active:
                print(f'Cooling break for {self.pacer.cooldown_seconds:g} seconds; Stop remains available.', flush=True)
        return self.pacer.after_step(lambda: self.check_health(step), cooling)

    def check_health(self, step):
        if self.stopped:
            return True
        if time.monotonic() < self.next_check:
            return False
        self.next_check = time.monotonic() + 5
        record = {"time": datetime.now().isoformat(), "step": step}
        cpu = cpu_temperature()
        record['cpu_temperature_c'] = cpu
        if cpu is not None and cpu >= self.max_cpu_temp:
            print(f'CPU temperature reached {cpu} C; stopping and checkpointing.')
            self.stopped = True
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
            if record["temperature_c"] >= self.max_temp:
                print(f"Temperature reached {record['temperature_c']} C; stopping at the configured {self.max_temp} C limit.")
                self.stopped = True
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
            record["telemetry_error"] = str(error)
        with self.path.open("a") as log:
            log.write(json.dumps(record) + "\n")
        return self.stopped


def main():
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    add_model_size_flags(parser)
    parser.add_argument("dataset", nargs="?", type=Path, default=home / "train-in")
    parser.add_argument("--max-tokens", type=int, default=20_000_000,
                        help="Corpus cap including validation; not a GPU memory limit (default 20M byte tokens)")
    parser.add_argument("--column", help="Override text field; dotted fields such as document.html supported")
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
    args = parser.parse_args()
    if len(args.name.strip()) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in args.name):
        parser.error('Use a run name of up to 80 characters without line breaks')
    if args.resume and (args.run_dir or args.prepare_only):
        parser.error("--resume cannot be combined with --run-dir or --prepare-only")
    if args.schedule_steps is not None and args.schedule_steps < 1:
        parser.error("Schedule steps must be positive")
    if args.max_tokens < 6000 or (args.steps is not None and args.steps < 1):
        parser.error("Use at least 6,000 corpus tokens and positive steps")
    if args.batch_size < 1 or args.grad_accum_steps < 1 or not 50 <= args.max_temp <= 90:
        parser.error("Batch/accumulation must be positive; temperature limit must be 50-90 C")
    if args.cpu_threads < 1 or not 50 <= args.max_cpu_temp <= 95:
        parser.error("CPU threads must be positive; CPU temperature limit must be 50-95 C")
    if not 0 <= args.burst_seconds <= 3600 or not 0 < args.cooldown_seconds <= 600:
        parser.error('Training bursts must be 0-3600 seconds and cooldowns greater than 0, up to 600 seconds')
    if args.low_priority:
        os.setpriority(os.PRIO_PROCESS, 0, max(10, os.getpriority(os.PRIO_PROCESS, 0)))
        try:
            subprocess.run(['ionice', '-c', '3', '-p', str(os.getpid())], capture_output=True, check=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as error:
            print(f'Idle disk priority unavailable: {error}', file=sys.stderr)
    torch.set_num_threads(args.cpu_threads)
    torch.set_num_interop_threads(1)
    state = home / ".local" / "state" / "tensegrity"
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
        with (run / "console.log").open("a") as log:
            sys.stdout, sys.stderr = Tee(previous_out, log), Tee(previous_err, log)
            try:
                monitor = Monitor(run / 'gpu.jsonl', args.max_temp, args.device, args.max_cpu_temp,
                                  args.burst_seconds, args.cooldown_seconds)
                signal.signal(signal.SIGINT, monitor.request_stop)
                signal.signal(signal.SIGTERM, monitor.request_stop)
                atomic_json(run / 'status.json', {'state': 'preparing' if not args.resume else 'starting', 'device': args.device})
                print(f"Run directory: {run}", flush=True)
                if not args.resume:
                    corpus = prepare_folder(args.dataset.expanduser(), data, max_tokens=args.max_tokens, column=args.column,
                                            stop_requested=lambda: monitor.stopped)
                    steps = args.steps or math.ceil(corpus["train_tokens"] / (args.batch_size * args.grad_accum_steps * 256))
                    config = {"steps": steps, "batch_size": args.batch_size,
                              "grad_accum_steps": args.grad_accum_steps, "max_temp": args.max_temp,
                              "dataset": str(args.dataset.expanduser().resolve()), "max_tokens": args.max_tokens,
                              "block_size": 256, "model_size": args.model_size,
                              "parameters": MODEL_PRESETS[args.model_size]["parameters"], 'display_name': args.name.strip(),
                              'device': args.device, 'max_cpu_temp': args.max_cpu_temp}
                    atomic_json(run / 'run.json', config)
                else:
                    config['steps'] = steps
                    config['device'] = args.device
                config.update(burst_seconds=args.burst_seconds, cooldown_seconds=args.cooldown_seconds,
                              low_priority=args.low_priority, max_temp=args.max_temp, max_cpu_temp=args.max_cpu_temp)
                atomic_json(run / 'run.json', config)
                if args.prepare_only:
                    atomic_json(run / 'status.json', {'state': 'prepared', 'device': args.device})
                    print(f"Prepared. Start training with: train --resume {run}")
                    return
                print(f"Training {args.model_size} model to {steps:,} total steps; batch={args.batch_size}, accumulation={args.grad_accum_steps}, context=256.")
                if monitor.stopped:
                    raise InterruptedError('Stopped before training.')
                if monitor.check_health(0):
                    raise InterruptedError('Temperature limit reached before training.')
                monitor.pacer.reset()
                monitor.target_steps = steps
                atomic_json(run / 'status.json', {'state': 'training', 'device': args.device})
                result = train(data, run, steps=steps, batch_size=args.batch_size,
                               grad_accum_steps=args.grad_accum_steps, device=args.device,
                               resume=bool(args.resume and (run / "ckpt_latest.pt").is_file()), stop_requested=monitor, schedule_steps=args.schedule_steps, model_size=args.model_size)
                atomic_json(run / 'result.json', {"completed_steps": result[0], "best_val_loss": result[1],
                                                 "stopped_early": result[0] < steps})
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


if __name__ == "__main__":
    main()
