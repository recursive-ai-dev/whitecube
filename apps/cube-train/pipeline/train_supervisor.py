"""Keep the GPU health watchdog outside the CUDA training process."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from files import atomic_json


def gpu_temperature():
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5, check=True)
    try:
        readings = [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        if not readings or any(not 0 <= reading <= 150 for reading in readings):
            raise ValueError('Missing or implausible GPU temperature')
        return max(readings)
    except (ValueError, IndexError) as error:
        raise RuntimeError(f"GPU inaccessible: {result.stdout.strip()} {result.stderr.strip()}") from error


def cpu_temperature():
    readings = []
    for sensor in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (sensor / "name").read_text().strip() not in ("coretemp", "k10temp"):
                continue
        except (OSError, ValueError):
            continue
        for path in sensor.glob('temp*_input'):
            try:
                reading = int(path.read_text()) / 1000
                if 0 <= reading <= 150:
                    readings.append(reading)
            except (OSError, ValueError):
                continue
    return max(readings) if readings else None


def reported_run(report_path):
    """A partial, missing, or invalid child report must never prevent shutdown."""
    try:
        with report_path.open() as handle:
            value = handle.read(8193).strip()
        run = Path(value)
        if value and len(value) <= 8192 and run.is_absolute() and run.is_dir():
            return run
    except (OSError, ValueError):
        pass
    return None


def stop_child(child, grace):
    if child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=grace)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            child.kill()
            child.wait(timeout=2)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            print('Training is stuck inside the kernel despite termination; a reboot may be required.',
                  file=sys.stderr, flush=True)


def checked_temperature(value, name, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 150:
        raise RuntimeError(f'{name} returned an invalid temperature: {value!r}')
    return value


def supervise(command, *, max_temp=70, max_cpu_temp=85, probe=gpu_temperature,
              cpu_probe=cpu_temperature, interval=5, grace=10):
    if not command:
        raise ValueError('A training command is required')
    for name, value in {'max_temp': max_temp, 'max_cpu_temp': max_cpu_temp,
                        'interval': interval, 'grace': grace}.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be finite and positive')
    # Fail before preparing/training if the GPU is already inaccessible.
    try:
        initial = checked_temperature(probe(), 'GPU')
        if initial >= max_temp:
            raise RuntimeError(f"GPU is already {initial} C (limit {max_temp} C)")
        initial_cpu = checked_temperature(cpu_probe(), 'CPU', optional=True)
        if initial_cpu is not None and initial_cpu >= max_cpu_temp:
            raise RuntimeError(f"CPU is already {initial_cpu} C (limit {max_cpu_temp} C)")
    except Exception as error:
        print(f"Training was not started: {error}", file=sys.stderr)
        return 2
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'tensegrity'
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="watchdog-", dir=state) as temp:
        report_path = Path(temp) / "active-run.txt"
        env = dict(os.environ, TENSEGRITY_RUN_REPORT=str(report_path))
        child = subprocess.Popen(command, env=env)
        stop_deadline = None
        forwarded_signal = None

        def forward(signum, frame):
            nonlocal stop_deadline, forwarded_signal
            forwarded_signal = signum
            if stop_deadline is None:
                stop_deadline = time.monotonic() + grace
            if child.poll() is None:
                try:
                    child.send_signal(signum)
                except ProcessLookupError:
                    pass

        old_handlers = {s: signal.signal(s, forward) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            while True:
                if stop_deadline is not None and time.monotonic() >= stop_deadline:
                    stop_child(child, .1)
                    return 128 + forwarded_signal
                try:
                    timeout = interval if stop_deadline is None else min(interval, max(.01, stop_deadline - time.monotonic()))
                    return child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    temperature = checked_temperature(probe(), 'GPU')
                    cpu = checked_temperature(cpu_probe(), 'CPU', optional=True)
                    run = reported_run(report_path)
                    if run:
                        try:
                            with (run / "thermal.jsonl").open("a") as log:
                                log.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                                      "gpu_c": temperature, "cpu_c": cpu}) + "\n")
                        except OSError as error:
                            print(f'Watchdog telemetry unavailable: {error}', file=sys.stderr, flush=True)
                    if temperature < max_temp and (cpu is None or cpu < max_cpu_temp):
                        continue
                    reason = (f"GPU temperature {temperature} C reached limit {max_temp} C"
                              if temperature >= max_temp else
                              f"CPU temperature {cpu} C reached limit {max_cpu_temp} C")
                except Exception as error:
                    reason = f"GPU health check failed: {error}"
                print(f"Watchdog: {reason}. Stopping training; retaining saved checkpoints.", file=sys.stderr, flush=True)
                report = {"time": datetime.now(timezone.utc).isoformat(), "reason": reason,
                          "status": "watchdog_stopped", "exit_code": 2}
                run = reported_run(report_path)
                try:
                    if run:
                        atomic_json(run / 'failure.json', report)
                except OSError as error:
                    print(f'Watchdog failure report unavailable: {error}', file=sys.stderr, flush=True)
                finally:
                    stop_child(child, grace)
                return 2
        finally:
            for s, handler in old_handlers.items():
                signal.signal(s, handler)


def main():
    args = sys.argv[1:]
    command = [sys.executable, "-u", str(Path(__file__).with_name("train_folder.py")), *args]
    if any(flag in args for flag in ("--help", "-h", "--prepare-only")) or "--device=cpu" in args:
        os.execv(sys.executable, command)
    if "--device" in args and args[args.index("--device") + 1:][:1] == ["cpu"]:
        os.execv(sys.executable, command)
    limit, cpu_limit = 70, 85
    try:
        if "--max-temp" in args:
            limit = int(args[args.index("--max-temp") + 1])
        for arg in args:
            if arg.startswith("--max-temp="):
                limit = int(arg.split("=", 1)[1])
            elif arg.startswith("--max-cpu-temp="):
                cpu_limit = int(arg.split("=", 1)[1])
        if "--max-cpu-temp" in args:
            cpu_limit = int(args[args.index("--max-cpu-temp") + 1])
    except (ValueError, IndexError):
        os.execv(sys.executable, command)  # Let argparse explain malformed arguments.
    raise SystemExit(supervise(command, max_temp=limit, max_cpu_temp=cpu_limit))


if __name__ == "__main__":
    main()
