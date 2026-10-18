"""Wall-clock pacing only; simulation dt and action magnitude stay unchanged."""
import math
import time


class StepPacer:
    def __init__(self, control_freq, time_scale, clock=time.monotonic):
        if control_freq <= 0 or not math.isfinite(time_scale) or time_scale < 0:
            raise ValueError('control_freq must be positive; time_scale must be finite and >= 0')
        self.interval = 1 / (control_freq * time_scale) if time_scale else 0
        self.clock = clock
        self.deadline = 0

    def reset(self):
        self.deadline = 0

    def ready(self):
        now = self.clock()
        if now < self.deadline:
            return False
        # Never catch up with a burst after rendering stalls or pauses.
        self.deadline = now + self.interval
        return True


def adjust_time_scale(value, direction):
    """Move through practical slow-motion presets; 0 (unlimited) is fastest."""
    presets = tuple(i / 10 for i in range(1, 11))
    if direction < 0:
        return next((x for x in reversed(presets) if value == 0 or x < value), presets[0])
    return next((x for x in presets if x > value), 0.0) if value else 0.0
