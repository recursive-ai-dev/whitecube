"""End-to-end smoke test; run with the configured PyTorch Python environment."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import engine
from train_supervisor import cpu_temperature


class TrainingJobTests(unittest.TestCase):
    def test_persistent_worker_cooling_stop_checkpoint_resume_and_preview(self):
        with tempfile.TemporaryDirectory(prefix='whitecube-training-job-') as temp:
            root = Path(temp)
            corpus = root / 'corpus.jsonl'
            corpus.write_text('\n'.join(json.dumps({'text': f'Document {i}. ' + 'A cube learned from local stories. ' * 30}) for i in range(40)))
            env = {'HOME': str(root), 'WHITECUBE_TRAIN_OUT': str(root / 'train-out'),
                   'XDG_STATE_HOME': str(root / 'state'), 'XDG_CONFIG_HOME': str(root / 'config')}
            with patch.dict(os.environ, env):
                trainer = engine.Trainer(home=root)
                values = {**trainer.preferences(), 'python': sys.executable, 'dataset': str(corpus), 'name': 'Cooling smoke',
                          'model_size': '2m', 'device': 'cpu', 'cpu_threads': 1, 'batch_size': 1, 'grad_accum_steps': 1,
                          'steps': 100, 'max_tokens': 30_000, 'max_cpu_temp': 95,
                          'stagger_training': True, 'burst_seconds': 1, 'cooldown_seconds': 10, 'low_priority': True}
                def cool_before_training():
                    deadline = time.monotonic() + 60
                    while (cpu_temperature() or 0) >= 85:
                        if time.monotonic() >= deadline:
                            self.fail('CPU did not cool below 85 C before the smoke run')
                        time.sleep(.5)
                cool_before_training()
                job = trainer.start(values)
                run = Path(job['run'])
                def wait_for(check, seconds=60):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if check(): return
                        saved = engine.read_json(job['path'])
                        if saved.get('state') == 'failed':
                            self.fail(engine.tail_text(job['log']))
                        time.sleep(.05)
                    self.fail('Timed out: ' + engine.tail_text(job['log']))
                try:
                    wait_for(lambda: engine.read_json(run / 'status.json').get('state') == 'cooling')
                    reopened = engine.Trainer(home=root)
                    active = reopened.active_job()
                    self.assertIsNotNone(active)
                    child_pid = int(Path(f'/proc/{active["pid"]}/task/{active["pid"]}/children').read_text().split()[0])
                    self.assertGreaterEqual(os.getpriority(os.PRIO_PROCESS, child_pid), 10)
                    disk_priority = subprocess.run(['ionice', '-p', str(child_pid)], capture_output=True, text=True, check=True)
                    self.assertIn('idle', disk_priority.stdout)
                    stopped_at = time.monotonic()
                    reopened.stop(active)
                    wait_for(lambda: not engine.job_running(engine.read_json(job['path'])), 30)
                    self.assertEqual(engine.read_json(job['path'])['state'], 'stopped')
                    self.assertLess(time.monotonic() - stopped_at, 10)
                    result = engine.read_json(run / 'result.json')
                    self.assertGreater(result['completed_steps'], 0)
                    self.assertLess(result['completed_steps'], 100)
                    self.assertTrue((run / 'ckpt_best.pt').is_file())
                    self.assertTrue((run / 'ckpt_latest.pt').is_file())
                    completed = result['completed_steps']
                    cool_before_training()
                    job = reopened.start({**values, 'steps': completed + 1}, resume_id=run.name)
                    wait_for(lambda: engine.read_json(job['path']).get('state') == 'completed' and not engine.job_running(engine.read_json(job['path'])), 45)
                    self.assertEqual(engine.read_json(job['path'])['state'], 'completed', engine.tail_text(job['log']))
                    self.assertEqual(engine.read_json(run / 'result.json')['completed_steps'], completed + 1)
                    self.assertEqual(engine.read_json(run / 'local_config.json')['n_embd'], 224)
                    self.assertNotIn('Cooling break', engine.tail_text(job['log']))
                    preview = subprocess.run([sys.executable, str(engine.PIPELINE / 'preview.py'),
                                              '--checkpoint', str(run / 'ckpt_best.pt'), '--prompt', 'Once upon a time'],
                                             capture_output=True, text=True, timeout=45)
                    self.assertEqual(preview.returncode, 0, preview.stderr)
                    self.assertLessEqual(len(preview.stdout.rstrip('\n')), 120)
                    print(f'PASS: persistent 2M job, nice 10/idle I/O, cooling Stop, saved checkpoints, resume to {completed + 1} steps, CPU preview')
                finally:
                    active = trainer.active_job()
                    if active:
                        trainer.stop(active)
                        deadline = time.monotonic() + 30
                        while trainer.active_job() and time.monotonic() < deadline: time.sleep(.1)
                    for child in trainer.children + reopened.children:
                        child.wait(timeout=5)


if __name__ == '__main__': unittest.main()
