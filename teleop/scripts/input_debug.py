"""Opt-in input observation; never pumps SDL or changes the controller's edge history."""
import hashlib
import json
import os
from pathlib import Path
import time


def enabled():
    return os.environ.get('TELEOP_DEBUG_INPUT', '').lower() in ('1', 'true', 'yes')


def startup(script):
    if enabled():
        path = Path(script).resolve()
        print('[input] ' + json.dumps(dict(event='startup', pid=os.getpid(), script=str(path),
              sha256=hashlib.sha256(path.read_bytes()).hexdigest()), sort_keys=True), flush=True)


class InputTrace:
    def __init__(self):
        self.enabled = enabled()
        self.previous = None
        self.pending = None
        self.commands = []
        self.sequence = 0

    def emit(self, event, **data):
        if self.enabled:
            print('[input] ' + json.dumps(dict(event=event, pid=os.getpid(), seq=self.sequence,
                  time=round(time.monotonic(), 6), **data), sort_keys=True), flush=True)

    def finish(self):
        if self.pending is not None:
            self.emit('decision', **self.pending, commands=list(self.commands),
                      result='commands_observed' if self.commands else 'no_command_observed')
            self.pending = None
        self.commands = []

    def observe(self, ctl, **context):
        if not self.enabled:
            return
        self.finish()
        pad = getattr(ctl, 'pad', None)
        # Read only. The production code retains sole ownership of event.pump().
        buttons = [i for i in range(pad.get_numbuttons()) if pad.get_button(i)] if pad else []
        triggers = [i for i in (4, 5) if i < pad.get_numaxes() and pad.get_axis(i) > .5] if pad else []
        current = dict(context, buttons=buttons, triggers=triggers, gripper=float(ctl.gripper), scale=float(ctl.scale))
        old = self.previous or dict(buttons=[], triggers=[])
        pressed = sorted(set(buttons)-set(old['buttons']))
        released = sorted(set(old['buttons'])-set(buttons))
        if current != self.previous:
            self.sequence += 1
            self.emit('observe', **current, pressed=pressed, released=released)
            # No-command reports are about observed edges, not proof that SDL delivered every edge.
            if pressed or released or triggers != old['triggers']:
                self.pending = dict(context, pressed=pressed, released=released, buttons=buttons, triggers=triggers)
        self.previous = current

    def command(self, operation, **data):
        if self.enabled:
            self.commands.append(operation)
            self.emit('command', operation=operation, **data)


def trace_for(ctl):
    if not hasattr(ctl, '_input_trace'):
        ctl._input_trace = InputTrace()
    return ctl._input_trace
