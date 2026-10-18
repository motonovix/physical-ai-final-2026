"""Gamepad snapshots and menu decisions, independent of SDL / rendering / physics."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class PadFrame:
    buttons: tuple
    axes: tuple
    pressed: frozenset

    def get_button(self, index):
        return self.buttons[index] if index < len(self.buttons) else False

    def get_axis(self, index):
        return self.axes[index] if index < len(self.axes) else 0.0


class PadInput:
    def __init__(self):
        self.previous = None

    def capture(self, pad):
        buttons = tuple(bool(pad.get_button(i)) for i in range(pad.get_numbuttons()))
        axes = tuple(pad.get_axis(i) for i in range(pad.get_numaxes()))
        held = frozenset(i for i, down in enumerate(buttons) if down)
        pressed = held - self.previous if self.previous is not None else frozenset()
        self.previous = held
        return PadFrame(buttons, axes, pressed)


def pause_items(success, saving=False):
    if saving:
        return ('Save trajectory (selected)', 'Save trajectory (normal)')
    return ('Resume PLAY',) + (('Save trajectory...',) if success else ()) + (
        'Retry from READY (discard)', 'Task selection (discard)')


def episode_command(phase, success, cursor, saving, pressed):
    """Return operation, cursor, saving. X starts/resumes, A confirms, B backs out of menus."""
    if phase == 'ready':
        cursor = (cursor + int(12 in pressed) - int(11 in pressed)) % 3
        operation = ('countdown' if 2 in pressed else 'confirm_exit' if 1 in pressed else
                     ('countdown', 'toggle_alignment', 'confirm_exit')[cursor] if 0 in pressed
                     else None)
        return operation, cursor, False
    if phase == 'play':
        return ('pause' if 2 in pressed else None), 0, False
    if 2 in pressed:
        return 'play', 0, False
    if 1 in pressed:
        return (None, 1, False) if saving else ('play', 0, False)
    cursor = (cursor + int(12 in pressed) - int(11 in pressed)) % len(pause_items(success, saving))
    if 0 not in pressed:
        return None, cursor, saving
    if saving:
        return ('selected' if cursor == 0 else 'save'), cursor, saving
    if cursor == 0:
        return 'play', 0, False
    if success and cursor == 1:
        return None, 0, True
    return ('retry' if cursor == (2 if success else 1) else 'confirm_exit'), cursor, False


# START 直前のカウントダウン。3 -> 2 -> 1 -> START を 1 目盛り 0.75 秒で刻む
# （2026-09-13 ユーザー指定。実時間 1 秒だと待ちが長い）。
COUNTDOWN_TICKS = 3
COUNTDOWN_STEP = 0.75


@dataclass
class EpisodeInteraction:
    """One owner for phase and its menus; physics and recordings stay outside.

    Gamepad always shows the pause menu; keyboard_menu preserves the existing
    keyboard/mouse modal menu. Every phase transition resets both menu variants.
    """
    phase: str = 'ready'
    cursor: int = 0
    saving: bool = False
    keyboard_menu: int | None = None
    countdown_deadline: float | None = None
    confirming_exit: bool = False
    return_cursor: int = 0

    @property
    def stopped(self):
        return self.phase != 'play'

    def enter(self, phase, *, automatic=False, collided=False):
        if phase not in ('ready', 'countdown', 'play', 'pause'):
            raise ValueError(f'Unknown episode phase: {phase}')
        self.phase = phase
        self.cursor = (2 if collided else 1) if phase == 'pause' and automatic else 0
        self.saving = False
        self.keyboard_menu = None
        self.countdown_deadline = None
        self.confirming_exit = False

    def start_countdown(self, now):
        self.enter('countdown')
        self.countdown_deadline = now + COUNTDOWN_STEP * COUNTDOWN_TICKS

    def countdown_remaining(self, now):
        return max(0, math.ceil((self.countdown_deadline - now) / COUNTDOWN_STEP))

    def gamepad_command(self, success, pressed, now=None):
        if self.confirming_exit:
            if 2 in pressed:
                self.confirming_exit = False
                return 'countdown' if self.phase == 'ready' else 'play'
            if 1 in pressed:
                self.confirming_exit = False
                self.cursor = self.return_cursor
                return None
            self.cursor = (self.cursor + int(12 in pressed) - int(11 in pressed)) % 2
            if 0 in pressed:
                if self.cursor == 1:
                    return 'discard'
                self.confirming_exit = False
                self.cursor = self.return_cursor
            return None
        if self.phase == 'countdown':
            if 1 in pressed:
                return 'ready'
            return 'play' if now >= self.countdown_deadline else None

        operation, self.cursor, self.saving = episode_command(
            self.phase, success, self.cursor, self.saving, pressed)
        if operation == 'confirm_exit':
            self.return_cursor = self.cursor
            self.cursor = 1  # Yes is the default (2026-09-13, user request).
            self.confirming_exit = True
            return None
        return operation

    def visible_menu(self, gamepad):
        if gamepad:
            return self.cursor if self.phase in ('ready', 'pause') else None
        return self.keyboard_menu


def selected_task_dir(suite, task_id, name=None) -> str:
    """selected/ の下でタスクごとに分けるディレクトリ名。normal/ も同じ規則で `t<task_id>`。"""
    return f"t{task_id}"


def recording_destination(out_dir, suite, task_id, name, reason):
    """保存先ディレクトリ。selected / normal ともタスク別サブディレクトリ（normal は 2026-09-16 夕方から）。"""
    kind = "selected" if reason == "selected" else "normal"
    return out_dir / suite / kind / selected_task_dir(suite, task_id, name)
