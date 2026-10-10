"""Keep the GPU health watchdog outside the CUDA training process."""
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def gpu_temperature():
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5, check=True)
    try:
        return int(result.stdout.splitlines()[0].strip())
    except (ValueError, IndexError) as error:
        raise RuntimeError(f"GPU inaccessible: {result.stdout.strip()} {result.stderr.strip()}") from error


def cpu_temperature():
    readings = []
    for sensor in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (sensor / "name").read_text().strip() not in ("coretemp", "k10temp"):
                continue
            readings.extend(int(p.read_text()) / 1000 for p in sensor.glob("temp*_input"))
        except (OSError, ValueError):
            continue
    return max(readings) if readings else None


def supervise(command, *, max_temp=70, max_cpu_temp=85, probe=gpu_temperature,
              cpu_probe=cpu_temperature, interval=5, grace=10):
    # Fail before preparing/training if the GPU is already inaccessible.
    try:
        initial = probe()
        if initial >= max_temp:
            raise RuntimeError(f"GPU is already {initial} C (limit {max_temp} C)")
        initial_cpu = cpu_probe()
        if initial_cpu is not None and initial_cpu >= max_cpu_temp:
            raise RuntimeError(f"CPU is already {initial_cpu} C (limit {max_cpu_temp} C)")
    except Exception as error:
        print(f"Training was not started: {error}", file=sys.stderr)
        return 2
    state = Path.home() / ".local" / "state" / "tensegrity"
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="watchdog-", dir=state) as temp:
        report_path = Path(temp) / "active-run.txt"
        env = dict(os.environ, TENSEGRITY_RUN_REPORT=str(report_path))
        child = subprocess.Popen(command, env=env)

        def forward(signum, frame):
            if child.poll() is None:
                child.send_signal(signum)

        old_handlers = {s: signal.signal(s, forward) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            while True:
                try:
                    return child.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    temperature = probe()
                    cpu = cpu_probe()
                    if report_path.exists():
                        run = Path(report_path.read_text().strip())
                        if run.is_dir():
                            with (run / "thermal.jsonl").open("a") as log:
                                log.write(json.dumps({"time": datetime.now().isoformat(),
                                                      "gpu_c": temperature, "cpu_c": cpu}) + "\n")
                    if temperature < max_temp and (cpu is None or cpu < max_cpu_temp):
                        continue
                    reason = (f"GPU temperature {temperature} C reached limit {max_temp} C"
                              if temperature >= max_temp else
                              f"CPU temperature {cpu} C reached limit {max_cpu_temp} C")
                except Exception as error:
                    reason = f"GPU health check failed: {error}"
                print(f"Watchdog: {reason}. Stopping training; retaining saved checkpoints.", file=sys.stderr, flush=True)
                report = {"time": datetime.now().isoformat(), "reason": reason,
                          "status": "watchdog_stopped", "exit_code": 2}
                if report_path.exists():
                    run = Path(report_path.read_text().strip())
                    if run.is_dir():
                        (run / "failure.json").write_text(json.dumps(report, indent=2) + "\n")
                child.terminate()
                try:
                    child.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    child.kill()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        print("Training is stuck inside the kernel despite termination; a reboot may be required.",
                              file=sys.stderr, flush=True)
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
