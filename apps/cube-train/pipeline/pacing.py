"""Optional wall-clock training bursts with interruptible cooling breaks."""
import math
import time


class TrainingPacer:
    def __init__(self, burst_seconds=0, cooldown_seconds=10, *, clock=time.monotonic, sleep=time.sleep):
        self.burst_seconds = float(burst_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        if not math.isfinite(self.burst_seconds) or self.burst_seconds < 0:
            raise ValueError('burst_seconds must be finite and nonnegative')
        if not math.isfinite(self.cooldown_seconds) or self.cooldown_seconds <= 0:
            raise ValueError('cooldown_seconds must be finite and positive')
        self.clock, self.sleep = clock, sleep
        self.reset()

    def reset(self):
        self.next_break = self.clock() + self.burst_seconds

    def after_step(self, should_stop, on_cooling=lambda active: None):
        if should_stop():
            return True
        if not self.burst_seconds or self.clock() < self.next_break:
            return False
        stopped = False
        try:
            on_cooling(True)
            deadline = self.clock() + self.cooldown_seconds
            while self.clock() < deadline:
                if should_stop():
                    stopped = True
                    break
                self.sleep(min(.2, max(0, deadline - self.clock())))
        finally:
            on_cooling(False)
            self.reset()
        return stopped or should_stop()
