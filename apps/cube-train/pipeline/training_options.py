"""Training arguments available without importing NumPy or PyTorch."""
import argparse
import os
import shlex
from pathlib import Path

from model_presets import MODEL_PRESETS

DESCRIPTION = "Train a local 1M/2M/5M/10M Tensegrity model from ~/train-in."
THERMAL_PAUSE_MARGIN = 5       # pause this far below a stop limit
THERMAL_RESUME_MARGIN = 10     # resume once this far below the limit
THERMAL_PAUSE_LIMIT = 15 * 60  # stop if cooling takes longer than this


def default_output_root(home=None):
    return Path(os.environ.get('WHITECUBE_TRAIN_OUT') or Path(home or Path.home()) / 'train-out').expanduser()


def resume_hint(run, steps=None):
    command = [os.environ.get('WHITECUBE_TRAIN_COMMAND') or 'whitecube-train', '--resume', str(run)]
    if steps is not None:
        command += ['--steps', str(steps)]
    return shlex.join(command)


def add_model_size_flags(parser):
    group = parser.add_mutually_exclusive_group()
    for size, preset in MODEL_PRESETS.items():
        group.add_argument(f"--{size}", dest="model_size", action="store_const", const=size,
                           help=f"{preset['parameters']:,} parameters (default: 1m; resume uses saved size)")


def build_parser(prog=None):
    """Build the exact parser used by the training pipeline, without its runtime."""
    home = Path.home()
    parser = argparse.ArgumentParser(prog=prog, description=DESCRIPTION)
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
    parser.add_argument("--run-dir", type=Path, help="New run directory, default $WHITECUBE_TRAIN_OUT/<timestamp> or ~/train-out/<timestamp>")
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
    tuning.add_argument('--eval-iters', type=int, help='Validation batches of eight sequences per evaluation (default 20)')
    tuning.add_argument('--dtype', choices=('auto', 'float32', 'bfloat16', 'float16'),
                        help='Compute precision (default auto; resume preserves the saved actual precision)')
    tuning.add_argument('--seed', type=int, help='Seed for weights, batches and the corpus split (default 1337)')
    tuning.add_argument('--schedule', choices=('cosine', 'wsd'), help='Cosine decay, or warmup-stable-decay (default cosine)')
    tuning.add_argument('--val-fraction', type=float, help='Validation share of documents, 0.01-0.5 (default 0.05)')
    control = parser.add_argument_group('run controls (may change on resume)')
    control.add_argument('--early-stop-patience', type=int, help='Stop after N evaluations without meaningful improvement (0 disables)')
    control.add_argument('--early-stop-min-delta', type=float, help='Minimum absolute validation-loss improvement (default 0; changing it resets patience history)')
    control.add_argument('--log-interval', type=int, help='Write training progress every N steps (default 10)')
    control.add_argument('--keep-snapshots', type=int, help='Keep N step-numbered checkpoint copies (0 disables)')
    control.add_argument('--max-minutes', type=float, help='Save and stop after this many minutes of training (0 disables)')
    control.add_argument('--thermal-pause', action=argparse.BooleanOptionalAction, default=None,
                         help=f'Pause {THERMAL_PAUSE_MARGIN} °C below a stop limit until {THERMAL_RESUME_MARGIN} °C below it')
    return parser
