#!/usr/bin/env python3
"""LIBERO-Plus の env を Mac のキーボード / マウス / ゲームパッドで直接操作し、軌道を記録する。

robosuite 標準の `collect_demonstration.py` は pynput（キーボード）か hidapi
（SpaceMouse）に依存し、macOS では「入力監視」権限も要る。ここでは描画窓を
自前の cv2 ウィンドウにして、`cv2.waitKey` と `cv2.setMouseCallback` だけで
入力を取る。追加の依存も OS 権限も要らない。

アクション空間は評価パイプラインと同じ OSC_POSE / control_freq=20 の 7 次元
（xyz 3 + 回転 3 + グリッパ 1、すべて [-1, 1]）なので、ここで録った軌道は
そのまま教師データの単位になる。

    cd <repo>/teleop/scripts
    MAGICK_HOME=/opt/homebrew/opt/imagemagick \
    DYLD_LIBRARY_PATH=/opt/homebrew/opt/imagemagick/lib \
    <repo>/.venv/bin/python \
        teleop_libero.py --device keyboard --task-id 0

LIBERO-plus の場所は環境変数 `LIBERO_PLUS_ROOT` で渡す（既定は
`~/ghq/github.com/sylvestf/LIBERO-plus`）。
`--smoke` を付けると窓を出さずに経路だけ確認する（CI 代わりのローカル検証）。

移植元は motonovix/parc2026 の data_studio/teleop（2026-10-18 時点）。
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from step_pacer import StepPacer, adjust_time_scale
from interaction import EpisodeInteraction, PadInput, pause_items, recording_destination
from start_alignment import StartAlignment
from input_debug import startup as input_debug_startup, trace_for

LIBERO_SRC = Path(os.environ.get(
    "LIBERO_PLUS_ROOT", Path.home() / "ghq/github.com/sylvestf/LIBERO-plus"))
sys.path.insert(0, str(LIBERO_SRC))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402

# kit の setup.sh は init_states 読み込みに weights_only=False を当てるが、その
# sed -i が macOS の BSD sed では効かず未適用のまま残る。kit 側を書き換えると
# 採点環境との差分になるので、ここでロード時だけ既定値を戻す。
_TORCH_LOAD = torch.load


def _torch_load_compat(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _TORCH_LOAD(*args, **kwargs)


torch.load = _torch_load_compat

from libero.libero import benchmark as libero_benchmark  # noqa: E402
from libero.libero.envs import OffScreenRenderEnv  # noqa: E402

WINDOW = "LIBERO teleop"
REPO = Path(__file__).resolve().parents[2]
# render_recording.py などがこのモジュールを import する。スクリプト実行時に二重に読まない。
sys.modules.setdefault("teleop_libero", sys.modules[__name__])
# タスクを選んだあと INIT を選ぶ段へ進む suite。独自タスクを足すときに使う（今は空）。
INIT_MENU_SUITES = ()
DEFAULT_OUT = REPO / "teleop/recordings"

# 並進・回転の 1 step あたりの既定量。OSC_POSE の [-1, 1] に対する割合。
SPEED_BASE_GAIN = 1.4  # displayed SPEED 1.0 equals the former 1.4
# PLAY 開始時の SPEED。R/L でその場で上下できる（2026-09-13 ユーザー指定）。
DEFAULT_SPEED = 1.3
POS_STEP = 0.35
ROT_STEP = 0.35

GRIPPER_OPEN = -1.0
GRIPPER_CLOSE = 1.0
# グリッパが切り替わった直後に HUD を光らせる秒数（2026-09-13 ユーザー要望）。
GRIP_FLASH_S = 0.7
SUCCESS_PAUSE_STEPS = 20  # ゴール成立後に自動で一時停止するまでの step 数（2026-09-26 に一度 10 にしたが、ユーザー判断で 20 に戻した）

# マウスの画面移動量をアクションへ変換する係数。
MOUSE_POS_GAIN = 0.012
MOUSE_ROT_GAIN = 0.010
MOUSE_WHEEL_GAIN = 0.35

# 「画面を右へ / 下へ動かす / 3 軸目」を、それぞれどの world 軸へ流すか。
# 画面上の見え方はカメラ姿勢で変わるので、**正しい向きは動かしてみないと決まらない。**
# 実行中に x / y / z キーで符号を反転、t キーで横縦を入れ替えられる。決まった値は
# 終了時に表示されるので、それを `--mouse-map` に渡すと次から再現できる。
#
# 3 軸目の入力は r / f キーか shift + 上下ドラッグを使う。**ホイールは macOS では
# 届かない**（2026-08-16 実測。OpenCV の GUI バックエンドが COCOA で、scroll を
# マウスコールバックへ配送しない）。ハンドラは残してあるが当てにしない。
MOUSE_MAP = "-y,-x,+z"

# 受け取ったマウスイベントの種別名。届いているかの確認にだけ使う。
EVENT_NAMES = {
    cv2.EVENT_MOUSEMOVE: "move", cv2.EVENT_LBUTTONDOWN: "L down",
    cv2.EVENT_LBUTTONUP: "L up", cv2.EVENT_RBUTTONDOWN: "R down",
    cv2.EVENT_RBUTTONUP: "R up", cv2.EVENT_MBUTTONDOWN: "M down",
    cv2.EVENT_MOUSEWHEEL: "wheel", cv2.EVENT_MOUSEHWHEEL: "h-wheel",
}


def parse_mouse_map(text: str):
    """`-y,-x,+z` を (軸番号, 符号) の 3 組に開く。"""
    tokens = [t.strip() for t in text.split(",") if t.strip()]
    if len(tokens) != 3:
        raise SystemExit(f"--mouse-map は 3 つ必要（例 -y,-x,+z）: {text}")
    axes, signs = [], []
    for t in tokens:
        sign, name = (t[0], t[1:]) if t[0] in "+-" else ("+", t)
        if name not in ("x", "y", "z"):
            raise SystemExit(f"軸は x / y / z のいずれか: {t}")
        axes.append("xyz".index(name))
        signs.append(1.0 if sign == "+" else -1.0)
    return axes, signs


# 押した直後にフル速度を保つ時間と、その後の減衰の時定数（秒）。
# cv2.waitKey は OS のキーリピートに従うので、押しっぱなしでも最初の 1 回の
# あと 0.5 秒ほど次のイベントが来ない。HOLD_SEC はその空白を埋めるための窓で、
# macOS の「リピート入力認識までの時間」より短くすると押し始めに一度つんのめる。
HOLD_SEC = 0.45
DECAY_TAU = 0.20
DECAY_FLOOR = 0.03

# ちょん押しを小さく、押しっぱなしを速くするための立ち上げ。
# タップとホールドは、キーリピートが届くまで区別できない（最初の 0.5 秒は
# どちらも「イベント 1 回」に見える）。そこで経過時間ではなく**受け取った
# キーイベントの数**で速度を上げる。1 回だけならタップ、リピートが積み上がれば
# ホールド、と後から自然に分かれる。
TAP_GAIN = 0.18
RAMP_EVENTS = 6


class KeyboardDrive:
    """キーで並進・回転の速度を与える。

    `decay`（既定）は、キーイベントから HOLD_SEC はフル、その後 DECAY_TAU の
    時定数で 0 へ落とす。手を離せば勝手に止まり、押し続ければリピートが窓を
    延ばし続ける。`latch` は押した方向を保持し続け、停止キーでのみ 0 に戻す。
    """

    def __init__(self, mode: str = "decay", hold_sec: float = HOLD_SEC,
                 decay_tau: float = DECAY_TAU, tap_gain: float = TAP_GAIN,
                 ramp_events: int = RAMP_EVENTS):
        self.mode = mode
        self.hold_sec = hold_sec
        self.decay_tau = decay_tau
        self.tap_gain = tap_gain
        self.ramp_events = ramp_events
        self.dpos = np.zeros(3)
        self.drot = np.zeros(3)
        self.gripper = GRIPPER_OPEN
        # READY / PAUSE 中はグリッパを固定する（誤操作で記録中の状態を変えない。
        # 2026-08-19 ユーザー要望）。main 側が PLAY 中だけ True にする。
        self.gripper_live = True
        self.scale = DEFAULT_SPEED
        self.quit = None  # "save" | "discard"
        self._t_event = -1e9
        self._gain = 0.0
        self._events = 0
        self._held = None  # 立ち上げ中のキー。変えるとタップからやり直す

    @property
    def HELP(self):
        return [
            "w/s: +x/-x   a/d: +y/-y   r/f: +z/-z",
            "i/k: pitch   j/l: yaw     u/o: roll",
            f"space: stop  g: gripper   -/=: speed  [{self.mode}]",
            "p: pause/play  enter: save  esc: discard & select",
        ]

    def _decay_gain(self) -> float:
        """最後のキーイベントからの経過時間で 1.0 -> 0.0 へ落とす係数。"""
        if self.mode != "decay":
            return 1.0
        age = time.monotonic() - self._t_event
        if age <= self.hold_sec:
            return 1.0
        gain = math.exp(-(age - self.hold_sec) / self.decay_tau)
        return 0.0 if gain < DECAY_FLOOR else gain

    def _ramp_gain(self) -> float:
        """受け取ったキーイベント数で tap_gain -> 1.0 へ上げる係数。"""
        if self._events <= 0:
            return 0.0
        grown = (self._events - 1) / max(1, self.ramp_events)
        return min(1.0, self.tap_gain + (1.0 - self.tap_gain) * grown)

    def _press(self, held) -> None:
        if held != self._held:
            self._events = 0
            self._held = held
        self._events += 1
        self._t_event = time.monotonic()

    def _release(self) -> None:
        self.dpos[:] = 0.0
        self.drot[:] = 0.0
        self._events = 0
        self._held = None
        self._t_event = -1e9

    def handle_key(self, key: int) -> None:
        if key == -1:
            return
        ch = chr(key & 0xFF) if 0 <= (key & 0xFF) < 128 else ""

        pos_map = {"w": (0, +1), "s": (0, -1), "a": (1, +1),
                   "d": (1, -1), "r": (2, +1), "f": (2, -1)}
        rot_map = {"u": (0, +1), "o": (0, -1), "i": (1, +1),
                   "k": (1, -1), "j": (2, +1), "l": (2, -1)}

        if ch in pos_map:
            axis, sign = pos_map[ch]
            self.dpos[:] = 0.0
            self.dpos[axis] = sign * POS_STEP
            self.drot[:] = 0.0
            self._press(ch)
        elif ch in rot_map:
            axis, sign = rot_map[ch]
            self.drot[:] = 0.0
            self.drot[axis] = sign * ROT_STEP
            self.dpos[:] = 0.0
            self._press(ch)
        elif ch == " ":
            self._release()
        elif ch == "g" and self.gripper_live:
            self.gripper = (GRIPPER_CLOSE if self.gripper == GRIPPER_OPEN
                            else GRIPPER_OPEN)
        elif ch in ("-", "_"):
            self.scale = max(0.1, self.scale - 0.1)
        elif ch in ("=", "+"):
            self.scale = min(2.0, self.scale + 0.1)
        elif key in (13, 10):
            self.quit = "save"
        elif key == 27:
            self.quit = "discard"

    def action(self) -> np.ndarray:
        self._gain = self._decay_gain() * self._ramp_gain()
        if self._gain == 0.0:
            self._release()
        a = np.zeros(7)
        a[0:3] = self.dpos * self.scale * SPEED_BASE_GAIN * self._gain
        a[3:6] = self.drot * self.scale * SPEED_BASE_GAIN * self._gain
        a[6] = self.gripper
        return np.clip(a, -1.0, 1.0)

    def status(self) -> str:
        return (f"pos={np.round(self.dpos * self._gain, 3)} "
                f"rot={np.round(self.drot * self._gain, 3)} "
                f"ramp={self._ramp_gain():.2f} "
                f"grip={'CLOSE' if self.gripper > 0 else 'open'} "
                f"scale={self.scale:.1f}")


class MouseDrag(KeyboardDrive):
    """描画窓の上でマウスを引いて操作する。

    イベントで溜まった移動量を 1 step で消費するので、手を止めればロボットも
    止まる。減衰も latch も要らない。キー操作（gripper / speed / 終了）は
    そのまま併用できる。
    """

    AXIS_NAME = "xyz"

    def __init__(self, mapping: str = MOUSE_MAP):
        # マウスは移動量そのものが速度なので、キーの減衰・latch は効かせない。
        super().__init__(mode="mouse")
        self._pending = np.zeros(6)
        self._last = None
        self._drag = None
        self._seen = set()
        # 横移動・縦移動・ホイールを、それぞれどの world 軸へどの符号で流すか。
        self.axes, self.signs = parse_mouse_map(mapping)

    @property
    def HELP(self):
        return [
            "left drag: move   r/f or shift+drag: 3rd axis  (wheel is dead)",
            "right drag: pitch/yaw   middle click or g: gripper",
            "x/y/z: flip axis   t: swap drag axes   -/=: speed",
            "p: pause/play  enter: save  esc: discard & select",
        ]

    def map_str(self) -> str:
        """今の割り当てを `--mouse-map` にそのまま渡せる形で返す。"""
        return ",".join(f"{'+' if s > 0 else '-'}{self.AXIS_NAME[a]}"
                        for a, s in zip(self.axes, self.signs))

    def on_mouse(self, event, x, y, flags, param) -> None:
        # 届いたイベント種別を初回だけ出す。macOS の Cocoa バックエンドが
        # ホイールを配送するかは実測しないと分からないので、その確認に使う。
        if event not in self._seen:
            self._seen.add(event)
            print(f"[mouse] event {event} ({EVENT_NAMES.get(event, '?')}) を受信")
        if event == cv2.EVENT_LBUTTONDOWN:
            self._drag, self._last = "pos", (x, y)
        elif event == cv2.EVENT_RBUTTONDOWN:
            self._drag, self._last = "rot", (x, y)
        elif event in (cv2.EVENT_LBUTTONUP, cv2.EVENT_RBUTTONUP):
            self._drag, self._last = None, None
        elif event == cv2.EVENT_MBUTTONDOWN and self.gripper_live:
            self.gripper = (GRIPPER_CLOSE if self.gripper == GRIPPER_OPEN
                            else GRIPPER_OPEN)
        elif event == cv2.EVENT_MOUSEWHEEL:
            self._pending[self.axes[2]] += (np.sign(flags) * self.signs[2]
                                            * MOUSE_WHEEL_GAIN)
        elif event == cv2.EVENT_MOUSEMOVE and self._drag and self._last:
            dx, dy = x - self._last[0], y - self._last[1]
            self._last = (x, y)
            if self._drag == "pos" and flags & cv2.EVENT_FLAG_SHIFTKEY:
                # shift を押しながら上下に引くと 3 軸目。ホイールが届かない
                # 環境でも、マウスだけで z を動かせるようにしておく。
                self._pending[self.axes[2]] += self.signs[2] * -dy * MOUSE_POS_GAIN
            elif self._drag == "pos":
                self._pending[self.axes[0]] += self.signs[0] * dx * MOUSE_POS_GAIN
                self._pending[self.axes[1]] += self.signs[1] * dy * MOUSE_POS_GAIN
            else:
                self._pending[5] += self.signs[0] * dx * MOUSE_ROT_GAIN
                self._pending[4] += self.signs[1] * dy * MOUSE_ROT_GAIN

    def handle_key(self, key: int) -> None:
        # 方向キーは使わず、gripper / speed / 軸の校正 / 終了だけ拾う。
        if key == -1:
            return
        ch = chr(key & 0xFF) if 0 <= (key & 0xFF) < 128 else ""
        if ch in ("r", "f"):
            # ホイールが届かない環境用の 3 軸目。1 押しがホイール 1 ノッチと同じ。
            self._pending[self.axes[2]] += (self.signs[2] * MOUSE_WHEEL_GAIN
                                            * (1 if ch == "r" else -1))
        elif ch in self.AXIS_NAME:
            # 動かしてみて向きが逆なら、その場で符号を反転する。再起動は要らない。
            slot = next(i for i, a in enumerate(self.axes)
                        if a == self.AXIS_NAME.index(ch))
            self.signs[slot] *= -1
            print(f"[map] {self.map_str()}")
        elif ch == "t":
            self.axes[0], self.axes[1] = self.axes[1], self.axes[0]
            print(f"[map] {self.map_str()}")
        elif ch == "g" and self.gripper_live:
            self.gripper = (GRIPPER_CLOSE if self.gripper == GRIPPER_OPEN
                            else GRIPPER_OPEN)
        elif ch in ("-", "_"):
            self.scale = max(0.1, self.scale - 0.1)
        elif ch in ("=", "+"):
            self.scale = min(2.0, self.scale + 0.1)
        elif key in (13, 10):
            self.quit = "save"
        elif key == 27:
            self.quit = "discard"

    def action(self) -> np.ndarray:
        a = np.zeros(7)
        a[0:6] = self._pending * self.scale * SPEED_BASE_GAIN
        a[6] = self.gripper
        self._pending[:] = 0.0
        return np.clip(a, -1.0, 1.0)

    def status(self) -> str:
        h, v, w = self.map_str().split(",")
        return (f"drag={self._drag or '-'} "
                f"map: right={h} down={v} wheel={w} "
                f"grip={'CLOSE' if self.gripper > 0 else 'open'} "
                f"scale={self.scale:.1f}")


# スティックの遊び。2026-08-17 の実測で中心ドリフトの報告は無かったので小さめ。
PAD_DEADZONE = 0.10
# 左スティックの中立ずれによる机上のドリフトを抑える。
PAD_XY_DEADZONE = 0.20
# 右スティックは斜めでz/yawを同時に出す。縦10%、横25%から独立に適用。
# 実機で調整する初期値（2026-09-12）。--pad-yaw-deadzoneで変更可能。
PAD_YAW_DEADZONE = 0.25
# z（右スティック縦）だけの倍率。上下移動は他の軸より距離を稼ぎたい場面が多い
# というユーザーの感触（2026-08-17）。フル倒しで 0.35×2=0.7、clip の 1.0 には収まる。
PAD_Z_GAIN = 2.0
# 左スティック（x/y）の倍率。キーボードのフル速度と同じでは遅いという感触
# （2026-08-17）。clip の 1.0 まで上げるなら 2.8 程度が実質の上限。
PAD_XY_GAIN = 1.5
# Pro コンの ZL/ZR はハード的に 2 値。pygame の axis は初回入力まで 0、
# 以後は離し=-1 / 押し=+1 を返すので、0.5 超を「押している」とみなす。
PAD_TRIGGER_ON = 0.5
# ZRだけピッチ回転を緩める（2026-09-18ユーザー指定）。
PAD_ZR_PITCH_GAIN = 0.7


class GamepadDrive(KeyboardDrive):
    """Nintendo Switch Pro コントローラ（pygame joystick）で操作する。

    倒し量がそのまま速度になる連続入力なので、キーの減衰・ラッチ・イベント数の
    立ち上げはどれも要らない。描画とキー入力（保存 / 破棄 / 符号反転）は従来どおり
    cv2 の窓で行い、pygame は joystick の読み取りだけに使う。

    axis / button 番号は 2026-08-17 に実機で確認した並び
    （plans/archive/gamepad_teleop.md）。他のコントローラでは並びが違う可能性がある。
    """

    AXIS_NAMES = ("x", "y", "z", "yaw")
    BTN_A, BTN_B, BTN_X, BTN_Y = 0, 1, 2, 3
    BTN_R3 = 8  # Right stick click, measured 2026-09-12.
    # 実機で確認済み（2026-09-12）。
    BTN_L, BTN_R = 9, 10
    DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT = 11, 12, 13, 14

    def __init__(self, flip: str = "", yaw_deadzone: float = PAD_YAW_DEADZONE,
                 z_gain: float = PAD_Z_GAIN, xy_gain: float = PAD_XY_GAIN):
        super().__init__(mode="gamepad")
        self.yaw_deadzone = yaw_deadzone
        self.z_gain = z_gain
        self.xy_gain = xy_gain
        # pygame の窓は出さない。フォーカスは cv2 の窓にあるので、
        # 背景でも joystick イベントを受け取れるようにしておく。
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        import pygame
        self._pg = pygame
        pygame.init()
        pygame.joystick.init()
        if pygame.joystick.get_count() == 0:
            raise SystemExit(
                "ゲームパッドが見つからない。システム設定 → Bluetooth で "
                "Pro Controller が Connected になっているか確認する")
        try:
            self.pad = pygame.joystick.Joystick(0)
            self.pad.init()
        except pygame.error as exc:
            pygame.quit()
            raise SystemExit(
                f"[pad] コントローラの初期化に失敗しました: {exc}\n"
                "Pro Controller をBluetoothで一度切断して再接続してください。"
                "USB接続の場合はケーブルを抜き差ししてください。\n"
                "接続後に同じmakeコマンドを再実行してください。"
                "キーボードで進める場合は DEVICE=keyboard を付けてください。"
            ) from None
        self.signs = {name: 1.0 for name in self.AXIS_NAMES}
        for name in [t.strip() for t in flip.split(",") if t.strip()]:
            if name not in self.signs:
                raise SystemExit(
                    f"--pad-flip は {'/'.join(self.AXIS_NAMES)} のカンマ区切り: {name}")
            self.signs[name] = -1.0
        self._btn_prev = {}
        self.capture_input()  # Initial held buttons are not new presses.
        print(f"[pad] {self.pad.get_name()}  axes={self.pad.get_numaxes()} "
              f"buttons={self.pad.get_numbuttons()}")

    @property
    def HELP(self):
        return [
            f"L-stick: x(vert)/y(horz)  R-stick: z/yaw together (yaw deadzone {self.yaw_deadzone:.0%})",
            "pitch/roll: dpad or ZR(up) ZL(dn) L(left) R(right)   grip: Y=close B=open R3=toggle",
            "PLAY -- X: pause    PAUSE -- A: confirm  B: back  X: resume  Y: disabled",
            "READY -- A: confirm  X: start  B: back; READY/PAUSE -- R/L: speed; ZR/ZL: time",
            "(keyboard is disabled while a gamepad is in use)",
        ]

    def map_str(self) -> str:
        """反転している軸を `--pad-flip` にそのまま渡せる形で返す。"""
        return ",".join(n for n in self.AXIS_NAMES if self.signs[n] < 0)

    def capture_input(self):
        self._pg.event.pump()
        if not hasattr(self, '_input_reader'):
            self._input_reader = PadInput()
        self.frame = self._input_reader.capture(self.pad)
        return self.frame

    def _axis(self, i: int, deadzone: float = PAD_DEADZONE) -> float:
        # 遊びを除いた残りをフルレンジへ引き直す。境界でいきなり deadzone ぶんの
        # 速度が出ないように（yaw の広い遊びでは特に効く）。
        v = self.frame.get_axis(i)
        if abs(v) <= deadzone:
            return 0.0
        return math.copysign((abs(v) - deadzone) / (1.0 - deadzone), v)

    def _rising(self, name: str, now: bool) -> bool:
        was = self._btn_prev.get(name, False)
        self._btn_prev[name] = now
        return now and not was

    def _toggle_gripper(self) -> None:
        self.gripper = (GRIPPER_CLOSE if self.gripper == GRIPPER_OPEN
                        else GRIPPER_OPEN)

    def _poll(self) -> None:
        # Input is captured once per UI frame, independently of the physics pacer.
        s = self.signs
        # スティックは上・左が負なので、既定で「上=+x（前）/ 左=+y / 上=+z」に
        # なるよう符号を反す。向きが逆なら実行中に x/y/z/c キーで直す。
        self.dpos[0] = s["x"] * -self._axis(1, PAD_XY_DEADZONE) * POS_STEP * self.xy_gain
        self.dpos[1] = s["y"] * -self._axis(0, PAD_XY_DEADZONE) * POS_STEP * self.xy_gain
        self.dpos[2] = s["z"] * -self._axis(3) * POS_STEP * self.z_gain
        self.drot[2] = s["yaw"] * -self._axis(2, self.yaw_deadzone) * ROT_STEP
        # 十字キーに加えて **L/ZL/R/ZR でも pitch/roll を出せる**（2026-09-12 ユーザー決定）。
        # 対応は十字キーの向きに合わせる: 上=ZR / 下=ZL / 左=L / 右=R。
        # 同時に押されたら足し合わせるのではなく、どちらかが押されていれば 1 とする
        # （両方押しで 2 倍になるのを避ける）。
        up = (bool(self.frame.get_button(self.DPAD_UP))
              or self.frame.get_axis(5) > PAD_TRIGGER_ON)          # ZR
        down = (bool(self.frame.get_button(self.DPAD_DOWN))
                or self.frame.get_axis(4) > PAD_TRIGGER_ON)        # ZL
        left = (bool(self.frame.get_button(self.DPAD_LEFT))
                or bool(self.frame.get_button(self.BTN_L)))        # L
        right = (bool(self.frame.get_button(self.DPAD_RIGHT))
                 or bool(self.frame.get_button(self.BTN_R)))       # R
        pitch = int(up) - int(down)
        if pitch > 0 and not self.frame.get_button(self.DPAD_UP):
            pitch *= PAD_ZR_PITCH_GAIN
        roll = int(left) - int(right)
        self.drot[1] = pitch * ROT_STEP
        self.drot[0] = roll * ROT_STEP
        # PLAY: Y closes / B opens / R3 toggles. Explicit Y/B wins over a simultaneous toggle.
        if self.gripper_live:
            close = self.BTN_Y in self.frame.pressed
            opened = self.BTN_B in self.frame.pressed
            if close or opened:
                self.gripper = GRIPPER_OPEN if opened else GRIPPER_CLOSE
                trace_for(self).command("gripper", source="B" if opened else "Y", value=self.gripper)
            elif self.BTN_R3 in self.frame.pressed:
                self._toggle_gripper()
                trace_for(self).command("gripper", source="R3", value=self.gripper)

    def battery(self) -> str:
        """SDL が返す電池状態（full / medium / low / empty / wired / unknown）。"""
        try:
            return self.pad.get_power_level()
        except Exception:
            return ""

    def select_nav(self):
        """Task selection: Y/A confirm, B/X back; one snapshot per UI frame."""
        pressed = self.capture_input().pressed
        move = int(self.DPAD_DOWN in pressed) - int(self.DPAD_UP in pressed)
        page = 10 * (int(self.DPAD_RIGHT in pressed) - int(self.DPAD_LEFT in pressed))
        back = bool(pressed & {self.BTN_B, self.BTN_X})
        enter = bool(pressed & {self.BTN_A, self.BTN_Y}) and not back
        return move, page, enter, back

    def handle_key(self, key: int) -> None:
        # 方向はコントローラで入れるので、キーは符号反転と共通操作だけ拾う。
        if key == -1:
            return
        ch = chr(key & 0xFF) if 0 <= (key & 0xFF) < 128 else ""
        flips = {"x": "x", "y": "y", "z": "z", "c": "yaw"}
        if ch in flips:
            self.signs[flips[ch]] *= -1
            print(f"[map] flip: {self.map_str() or '(既定どおり)'}")
        elif ch == "g" and self.gripper_live:
            self._toggle_gripper()
        elif ch in ("-", "_"):
            self.scale = max(0.1, self.scale - 0.1)
        elif ch in ("=", "+"):
            self.scale = min(2.0, self.scale + 0.1)
        elif key in (13, 10):
            self.quit = "save"
        elif key == 27:
            self.quit = "discard"

    def action(self) -> np.ndarray:
        self._poll()
        a = np.zeros(7)
        a[0:3] = self.dpos * self.scale * SPEED_BASE_GAIN
        a[3:6] = self.drot * self.scale * SPEED_BASE_GAIN
        a[6] = self.gripper
        return np.clip(a, -1.0, 1.0)

    def status(self) -> str:
        flipped = self.map_str()
        return (f"pos={np.round(self.dpos * self.scale * SPEED_BASE_GAIN, 2)} "
                f"rot={np.round(self.drot * self.scale * SPEED_BASE_GAIN, 2)} "
                f"grip={'CLOSE' if self.gripper > 0 else 'open'} "
                f"scale={self.scale:.1f}"
                + (f" flip={flipped}" if flipped else ""))


def get_task_suite(suite_name):
    return libero_benchmark.get_benchmark_dict()[suite_name](task_order_index=0)


def build_task(suite_name: str, task_id: int):
    suite = get_task_suite(suite_name)
    task = suite.get_task(task_id)
    if suite_name == "libero_90":
        # LIBERO-Plus の get_task_init_states は摂動 variant の名前しか扱わず、素の名前で落ちる
        from libero.libero import get_libero_path
        bddl = suite.get_task_bddl_file_path(task_id)
        init_states = torch.load(os.path.join(get_libero_path("init_states"), task.problem_folder, task.init_states_file), weights_only=False)
    else:
        bddl = suite.get_task_bddl_file_path(task_id)
        init_states = suite.get_task_init_states(task_id)
    return {
        "suite": suite_name,
        "task_id": task_id,
        "num_tasks": suite.get_num_tasks(),
        "name": task.name,
        "language": task.language,
        "bddl": str(bddl),
        "init_states": np.asarray(init_states),
    }


# 摂動カテゴリと難易度の正本。**名前の接尾辞から推測しない。**
# base 名に `_table_1` を含む task があり、素朴な部分一致では `_tb_` variant と
# 取り違える（実際に task 0 は名前が `..._table_1` だが分類は Background Textures）。
CLASSIFICATION = LIBERO_SRC / "libero/libero/benchmark/task_classification.json"


def load_classification(suite_name: str) -> dict:
    if not CLASSIFICATION.exists():
        return {}
    rows = json.loads(CLASSIFICATION.read_text(encoding="utf-8")).get(suite_name, [])
    return {r["name"]: (r.get("category") or "?", r.get("difficulty_level"))
            for r in rows}


def list_tasks(suite_name: str, substr: str) -> int:
    """suite の task を一覧する。2,402 件あるので名前かカテゴリで絞れるようにしてある。"""
    suite = get_task_suite(suite_name)
    labels = load_classification(suite_name)
    needle = substr.lower()

    rows, counts = [], {}
    for task_id in range(suite.get_num_tasks()):
        name = suite.get_task(task_id).name
        category, level = labels.get(name, ("未分類", None))
        if needle and needle not in f"{name} {category}".lower():
            continue
        rows.append((task_id, category, level, name))
        counts[category] = counts.get(category, 0) + 1

    total = suite.get_num_tasks()
    print(f"[suite] {suite_name}  {len(rows)}/{total} 件"
          f"{f'（「{substr}」で絞り込み）' if substr else ''}")
    if not labels:
        print(f"[warn] 分類が読めない: {CLASSIFICATION}")
    for task_id, category, level, name in rows:
        lv = f"L{level}" if level is not None else "L?"
        print(f"  {task_id:>5}  {category:<22} {lv:<3}  {name}")
    if not rows:
        print("  該当なし。`--list-tasks` を引数なしで叩くと全件出る")
        return 0
    print("[内訳] " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


# variant 名の接尾辞。**カテゴリ判定には使わない**（正本は classification）。
# base 名候補の抽出（頻度上位 10）にだけ使う。
_VARIANT_SUFFIXES = tuple(re.compile(p) for p in (
    r"_noise_\d+$", r"_initstate_\d+$", r"_view_\d+_\d+_\d+_\d+_\d+$",
    r"_language_\d+$", r"_light_\d+$", r"_add_\d+$", r"_table_\d+$",
    r"_tb_\d+$"))


def _strip_variant(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        for pat in _VARIANT_SUFFIXES:
            name = pat.sub("", name)
    return name


def build_suite_index(suite_name: str) -> dict:
    """SELECT 画面用に suite の全 task を base task（10 種）へ振った索引を作る。

    base は「接尾辞を剥がした名前の頻度上位 10」への最長前方一致で決める。
    この方法で全 4 suite の 10,030 件が 100% カバーできることは 2026-08-19 に
    確認済み。接尾辞剥がし**だけ**だと Objects Layout が
    物体名を接尾辞に含んで 187 種に化けるので、必ず前方一致まで行う。
    """
    suite = get_task_suite(suite_name)
    labels = load_classification(suite_name)

    rows = []
    for task_id in range(suite.get_num_tasks()):
        t = suite.get_task(task_id)
        category, level = labels.get(t.name, ("未分類", None))
        rows.append(dict(task_id=task_id, name=t.name, language=t.language,
                         category=category, level=level, suffix="", label=getattr(t, "label", None)))

    if suite_name in INIT_MENU_SUITES or suite_name in DIRECT_SUITES:
        # libero_90 は 90 本すべてが別タスク（摂動 variant なし）なので 1 task = 1 base
        return dict(suite=suite_name, num_tasks=len(rows),
                    bases=[dict(name=r["name"], rows=[r]) for r in rows])

    freq = {}
    for r in rows:
        b = _strip_variant(r["name"])
        freq[b] = freq.get(b, 0) + 1
    top = sorted(freq, key=freq.get, reverse=True)[:10]
    bases = sorted((dict(name=b, rows=[]) for b in top), key=lambda b: b["name"])

    unmatched = 0
    for r in rows:
        cands = [b for b in bases if r["name"].startswith(b["name"])]
        if not cands:
            unmatched += 1
            continue
        b = max(cands, key=lambda x: len(x["name"]))
        r["suffix"] = r["name"][len(b["name"]):].lstrip("_")
        b["rows"].append(r)
    if unmatched:
        print(f"[index] {suite_name}: base に振れない task が {unmatched} 件")
    return dict(suite=suite_name, num_tasks=suite.get_num_tasks(), bases=bases)


def list_clears(out_dir: Path) -> int:
    """base task ごとのクリア状況と npz を一覧する（正本は recordings/*.json）。

    CLEAR = kit 基準（success かつ max_disturb_m <= 1mm）。clear? = disturb
    未計測の旧記録の成功（kit 基準では未確認。確定と書かない）。
    """
    recs = load_recordings(out_dir)
    if not recs:
        print(f"[clears] 記録がない: {out_dir}")
        return 0
    suites = sorted({r["suite"] for rl in recs.values() for r in rl
                     if r["suite"]})
    for suite_name in suites:
        index = build_suite_index(suite_name)
        print(f"[clears] {suite_name}")
        for b in index["bases"]:
            rows = [r for r in b["rows"] if r["name"] in recs]
            if not rows:
                continue
            n_rec, best, n_maybe, n_sel = clear_summary(b["rows"], recs)
            head = _clear_text(n_rec, best, n_maybe, n_sel)
            if not head.startswith("rec"):
                head += f"  rec {n_rec}"
            print(f"  {b['name']}  [{head}]")
            entries = []
            for r in rows:
                for rec in recs[r["name"]]:
                    entries.append((r, rec))
            entries.sort(key=lambda e: (not e[1]["kit_clear"],
                                        not e[1]["maybe_clear"],
                                        e[1]["steps"] or 10**9))
            for r, rec in entries:
                mark = ("CLEAR " if rec["kit_clear"] else
                        "clear?" if rec["maybe_clear"] else
                        "fail  " if not rec["success"] else "collid")
                dist = ("--" if rec["disturb"] is None
                        else f"{rec['disturb'] * 1000:.1f}mm")
                print(f"      {mark} {rec['steps']:>5} steps  disturb {dist:>7}"
                      f"  t{r['task_id']:<5} {rec['npz']}")
    return 0


def make_env(bddl: str, control_freq: int, horizon: int) -> OffScreenRenderEnv:
    """人が操作する前提で horizon を外した env を作る。

    robosuite の既定 horizon は 1000 step で、超えると `self.done` が立ち、次の
    `step()` が `executing action in terminated episode` で落ちる
    （robosuite/environments/base.py の 375 行目と 429 行目）。このループは実測
    30 step/s 前後なので、既定のままだと**30 秒ほどで必ず落ちる**。人の操作には短すぎる。

    horizon は終了判定にしか使われず、記録する action と state には影響しない。
    ただし評価側は評価側の horizon で打ち切るので、**そこを超える長さの軌道は
    評価環境ではそのまま再生できない**ことに注意する。
    """
    if sys.platform == "darwin":
        from sim_runtime import require_local_sim
        require_local_sim()
    return OffScreenRenderEnv(
        bddl_file_name=bddl,
        camera_heights=128,
        camera_widths=128,
        control_freq=control_freq,
        horizon=horizon,
        ignore_done=True,
    )


def make_task_env(task, control_freq, horizon):
    env = make_env(task["bddl"], control_freq, horizon)
    # Sensor Noise variant は毎 step の観測画像にノイズ関数を掛ける（noise 48 で 1 step 107ms、素の task は 22ms。
    # 2026-09-23 実測）。teleop は観測画像を記録にも表示にも使わないので切る。物理・state・XML には影響しない。
    # 教師化の観測生成（prepare_material）は別処理で seed 付きにノイズを掛け直す。
    if getattr(env, "noise", 0):
        print(f"[env] Sensor Noise variant (noise={env.noise}): 観測ノイズを切って操作する（記録には影響しない）")
        env.noise = 0
    from display_guides import GuideEnv   # ゴール領域・グリッパの鉛直線などの表示補助（表示だけ、記録には入らない）
    return GuideEnv(env)


def reset_to(env, init_state: np.ndarray, settle_steps: int = 10):
    """評価パイプライン（pipeline/rollout.py）と同じ手順で初期状態に入れる。"""
    if hasattr(env, "restore_start"):
        return env.restore_start(init_state)
    env.reset()
    env.sim.set_state_from_flattened(init_state)
    env.sim.forward()
    obs = None
    for _ in range(settle_steps):
        obs, _, _, _ = env.step(np.zeros(7))
    return obs


# ------------------------------------------------------------- takeover
# 録画（方策 rollout を replay_bridge で変換した npz、または通常の teleop 録画）の
# step k から人が続きを操作する。
BRANCH_KEYS = ("gripper_current_action", "qacc_warmstart", "controller_initial_joint")


def load_takeover(npz_path: Path, step):
    """npz + 同名 json を読み、引き継ぎに要る配列をそろえる。分岐 key が無い録画は近似復元（branch_exact=False）。"""
    npz_path = Path(npz_path).resolve(strict=True)
    with np.load(npz_path, allow_pickle=False) as z:
        rec = {k: z[k] for k in z.files}
    meta_path = npz_path.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    actions = np.asarray(rec["actions"], dtype=np.float32)
    states = np.asarray(rec["states"], dtype=np.float64)
    n = len(actions)
    if states.shape[0] != n + 1:
        raise ValueError(f"states must be actions+1 ({states.shape[0]} vs {n + 1})")
    k = n if step is None else int(step)
    if not 0 <= k <= n:
        raise ValueError(f"--takeover-step {k} is outside 0..{n}")
    exact = all(key in rec for key in BRANCH_KEYS)
    import hashlib
    return {
        "npz": npz_path, "sha256": hashlib.sha256(npz_path.read_bytes()).hexdigest(), "meta": meta,
        "actions": actions, "states": states, "n": n, "k": k, "branch_exact": exact,
        "model_xml": str(rec["model_xml"]) if "model_xml" in rec else None,
        "grip": np.asarray(rec["gripper_current_action"], dtype=np.float64) if exact else None,
        "warm": np.asarray(rec["qacc_warmstart"], dtype=np.float64) if exact else None,
        "initial_joint": np.asarray(rec["controller_initial_joint"], dtype=np.float64) if exact else None,
        "suite": meta.get("suite"), "task_id": meta.get("task_id"), "init_index": meta.get("init_index", 0),
    }


def takeover_reset(env, rec, k):
    """状態 states[k] へ分岐する（replay_bridge.branch_reset と同じ手順）。settle は入れない。"""
    sim = env.sim
    sim.reset()
    sim.set_state_from_flattened(rec["states"][k])
    sim.forward()
    robot = env.robots[0]
    c = robot.controller
    c.update(force=True)
    ids = robot._ref_joint_gripper_actuator_indexes
    if rec["branch_exact"]:
        c.update_initial_joints(rec["initial_joint"].copy())
        c.reset_goal()
        robot.gripper.current_action = rec["grip"][k].copy()
        sim.data.qacc_warmstart[:] = rec["warm"][k]
    else:
        # 近似（trajectory editor の BranchSession と同じ）: 積分指令を指の開きから逆算、warmstart 0
        c.update_initial_joints(sim.data.qpos[c.qpos_index].copy())
        c.reset_goal()
        ranges = sim.model.actuator_ctrlrange[ids]
        q = sim.data.qpos[robot._ref_gripper_joint_pos_indexes]
        robot.gripper.current_action = np.clip(2 * (q - ranges.mean(axis=1)) / (ranges[:, 1] - ranges[:, 0]), -1, 1)
        sim.data.qacc_warmstart[:] = 0
    sim.data.ctrl[ids] = sim.data.qpos[robot._ref_gripper_joint_pos_indexes]
    raw = env.env
    raw.timestep = 0
    raw.cur_time = 0
    raw.done = False
    raw._update_observables(force=True)
    return raw._get_observations(force_update=True)


def fixed_body_mismatch(env, xml: str) -> float:
    """録画 XML の body pos/quat と、いま作った model との最大差。固定物（wine_rack 等）が reset ごとに動く variant を見つける。"""
    import xml.etree.ElementTree as ET
    model = env.sim.model
    worst = 0.0
    for body in ET.fromstring(xml).iter("body"):
        name = body.get("name")
        if not name or name not in model.body_names:
            continue
        idx = model.body_name2id(name)
        for attr, cur in (("pos", model.body_pos[idx]), ("quat", model.body_quat[idx])):
            val = body.get(attr)
            if val:
                worst = max(worst, float(np.max(np.abs(np.array(val.split(), float) - cur))))
    return worst


def prepare_takeover_env(env, rec):
    """READY（states[0]）で対象外物体の基準位置を控えてから states[k] へ入る。

    model は kit / replay_bridge と同じく seed → reset で作る（C1 と同じ経路。seed 42 で固定物の位置も
    録画 XML と一致し、分岐からの再生誤差 0.0）。seed なしの reset は固定物（cabinet / stove）が数 mm ずれる。
    録画 XML から `reset_from_xml_string` で作り直すと、位置が一致していても接触以降の物理が録画と
    ずれる（2026-09-23 実測 6.1）ので、XML は照合と警告にだけ使う。
    """
    env.seed(int(rec["meta"].get("seed", 42)))
    env.reset()
    rec["model_source"] = "seeded_reset"
    if rec["model_xml"]:
        gap = fixed_body_mismatch(env, rec["model_xml"])
        rec["fixed_body_mismatch_m"] = gap
        if gap > 1e-6:
            print(f"[takeover] 固定物の位置が録画 XML と {gap:.2e} m ずれている（seed が違うか、reset ごとに動く variant）。"
                  "物理は保存 state から続くが、見た目の固定物が録画と違い得る", file=sys.stderr)
    if env.sim.get_state().flatten().shape != rec["states"][0].shape:
        raise ValueError("録画の state 次元がこの環境と合わない（model_xml / suite の不一致）")
    obs0 = env.regenerate_obs_from_state(rec["states"][0])
    rec["baseline"] = disturb_baseline(env, obs0)
    return takeover_reset(env, rec, rec["k"])


def takeover_check(env, rec, k=None):
    """states[k] から actions[k:] を流して録画の残りと比べる（引き継ぎ点の物理が元と同じことの根拠）。"""
    k = rec["k"] if k is None else k
    takeover_reset(env, rec, k)
    worst = 0.0
    for i in range(k, rec["n"]):
        env.step(rec["actions"][i])
        worst = max(worst, float(np.max(np.abs(env.sim.get_state().flatten() - rec["states"][i + 1]))))
    takeover_reset(env, rec, rec["k"])
    return worst


def takeover_meta(rec):
    return {"source_npz": str(rec["npz"]), "source_sha256": rec["sha256"], "takeover_step": rec["k"],
            "prefix_steps": rec["k"], "source_steps": rec["n"], "branch_exact": rec["branch_exact"],
            "model_source": rec.get("model_source"), "fixed_body_mismatch_m": rec.get("fixed_body_mismatch_m"),
            "source_meta": {key: rec["meta"].get(key) for key in ("suite", "task_id", "init_index", "name", "success", "source")}}


def draw_hud(frame, lines):
    overlay = frame.copy()
    h = 18 * len(lines) + 10
    cv2.rectangle(overlay, (0, 0), (frame.shape[1], h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    for i, text in enumerate(lines):
        cv2.putText(frame, text, (8, 18 + i * 18), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (255, 255, 255), 1, cv2.LINE_AA)
    return frame


def draw_start_countdown(frame, remaining, video_h):
    """Visible countdown without blocking input or advancing the simulation."""
    text = str(remaining) if remaining else "START"
    width, height = 300, 156
    x, y = (frame.shape[1] - width) // 2, (video_h - height) // 2
    overlay = frame.copy()
    cv2.rectangle(overlay, (x, y), (x + width, y + height), (0, 0, 0), -1)
    cv2.addWeighted(overlay, .8, frame, .2, 0, frame)
    size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 2.0, 3)[0]
    cv2.putText(frame, text, (x + (width-size[0])//2, y+87),
                cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 3, cv2.LINE_AA)
    if remaining:
        cv2.putText(frame, "Get ready   B: cancel", (x+50, y+132),
                    cv2.FONT_HERSHEY_SIMPLEX, .55, (220, 220, 220), 1, cv2.LINE_AA)
    return frame


PAUSE_MENU_ITEMS = ("Retry from READY (discard)",
                    "Cancel")


def pause_menu_items(success):
    if success:
        return ("Save trajectory (selected)", "Save trajectory (normal)",
                "Retry from READY (discard)", "Back to PAUSE")
    return PAUSE_MENU_ITEMS


def draw_ready_menu(frame, cursor, auto_align, speed, time_scale, init_index,
                    init_count, has_init, video_h, *, gamepad=True):
    """READY uses the same modal presentation as PAUSE; simulation remains stopped."""
    items = ("Start PLAY", f"AUTO ALIGN: {'ON' if auto_align else 'OFF'}",
             "Task selection")
    w = min(540, frame.shape[1] - 20)
    h = 260
    x, y = (frame.shape[1] - w) // 2, max(0, (video_h - h) // 2)
    box = frame[y:y+h, x:x+w]
    box[:] = (box * .25).astype(box.dtype)
    cv2.rectangle(frame, (x, y), (x+w, y+h), INK_MUTED, 1, cv2.LINE_AA)
    cv2.putText(frame, "READY MENU", (x+20, y+30), FONT, .55, INK, 1, cv2.LINE_AA)
    for i, item in enumerate(items):
        yy = y+66+i*34
        if i == cursor:
            cv2.rectangle(frame, (x+10, yy-22), (x+w-10, yy+8), TRACK, -1)
        cv2.putText(frame, ("> " if i == cursor else "  ")+item,
                    (x+20, yy), FONT, .5, INK if i == cursor else INK_MUTED, 1, cv2.LINE_AA)
    lines = (["Dpad: choose   A: confirm   B: back   X: start",
              f"R/L: SPEED {speed:.1f}   ZR/ZL: TIME SCALE {time_scale:.1f}"]
             if gamepad else ["W/S: choose   Enter: confirm   Esc: task selection",
                              f"TIME SCALE {time_scale:.1f}: [ / ]"])
    lines.append((f"INIT {init_index}/{init_count-1}: " +
                  ("Dpad left/right" if gamepad else ", / .")) if has_init else "")
    for i, text in enumerate(lines):
        cv2.putText(frame, text, (x+15, y+185+i*23), FONT, .38, INK, 1, cv2.LINE_AA)
    return frame


def draw_pause_menu(frame, cursor: int, success=False, video_h=None,
                    disturb=("", 0.0), *, gamepad=False, saving=False, confirming_exit=False):
    """PAUSE menu overlay. Gamepad defaults to Resume; keyboard uses legacy items.

    frame は draw_panel 済みでもよい（映像部の中央 = 上から video_h/2 に置く）。

    disturb=(対象外物体名, 移動量m)。1mm を超えていれば警告 2 行をメニュー内へ足す。
    保存自体は衝突していても通るので、kit 基準で不成功であることと、
    INIT が進まないことを選ぶ前に見せる（2026-09-09 ユーザー要望）。
    """
    if video_h is None:
        video_h = frame.shape[0] - PANEL_H
    items = (("No - stay here", "Yes - discard and select") if confirming_exit else
             pause_items(success, saving) if gamepad else pause_menu_items(success))
    name, dist = disturb
    warn = []
    if dist > DISTURB_THRESHOLD_M:
        warn = [f"Collided: {name} {dist * 1000:.2f}mm"
                f" (> {DISTURB_THRESHOLD_M * 1000:.1f}mm)",
                "kit criterion: FAIL - INIT will not advance"]
    w, row_h, warn_h = 480, 34, 21
    h = row_h * len(items) + (102 if gamepad else 82) + warn_h * len(warn)
    x0 = (frame.shape[1] - w) // 2
    y0 = (video_h - h) // 2
    box = frame[y0:y0 + h, x0:x0 + w]
    box[:] = (box * 0.25).astype(box.dtype)  # 地を落として浮かせる
    cv2.rectangle(frame, (x0, y0), (x0 + w, y0 + h), INK_MUTED, 1, cv2.LINE_AA)
    cv2.putText(frame, "Return to task selection?" if confirming_exit else "PAUSE MENU", (x0 + 20, y0 + 30), FONT, 0.55, INK, 1,
                cv2.LINE_AA)
    for i, line in enumerate(warn):
        cv2.putText(frame, line, (x0 + 20, y0 + 52 + i * warn_h), FONT, 0.42,
                    COLLIDE, 1, cv2.LINE_AA)
    top = y0 + 58 + warn_h * len(warn)
    for i, item in enumerate(items):
        y = top + i * row_h
        if i == cursor:
            cv2.rectangle(frame, (x0 + 10, y - 22), (x0 + w - 10, y + 8),
                          TRACK, -1)
            cv2.putText(frame, ">", (x0 + 20, y), FONT, 0.5, ACCENT, 1,
                        cv2.LINE_AA)
        color = INK if i == cursor else INK_MUTED
        cv2.putText(frame, item, (x0 + 40, y), FONT, 0.5, color, 1, cv2.LINE_AA)
    if gamepad:
        cv2.putText(frame, "Dpad: choose   A: confirm   B: back   X: play",
                    (x0 + 15, y0 + h - 32), FONT, 0.38, INK, 1, cv2.LINE_AA)
        cv2.putText(frame, "Only Yes discards the current trajectory" if confirming_exit else "R/L: speed +/-   ZR/ZL: time faster/slower",
                    (x0 + 15, y0 + h - 12), FONT, 0.38, INK, 1, cv2.LINE_AA)
    return frame


# 状態帯（カメラ映像の下に足す）の高さ。映像そのものには何も描かない。
PANEL_H = 148
# 配色はダッシュボードの定石で最小に: 無彩色の地とインク + アクセント 1 色 +
# 成功状態にだけ緑。BGR。
PANEL_BG = (24, 24, 24)
INK = (235, 235, 235)
INK_MUTED = (150, 150, 150)
TRACK = (55, 55, 55)
ACCENT = (0, 200, 255)  # アンバー
GOOD = (90, 210, 120)   # 緑（success 専用）
BAD = (60, 60, 230)     # 赤（REC と電池残量警告）
COLLIDE = (40, 90, 240)  # オレンジ寄りの赤（擾乱 = Collided 表示専用）
FONT = cv2.FONT_HERSHEY_SIMPLEX

# SELECT 画面に並べる suite（表示順）。
SUITE_CYCLE = ("libero_spatial", "libero_object", "libero_goal", "libero_10", "libero_90")
DIRECT_SUITES = ("libero_90",)   # タスクを選んだらそのまま READY へ（category / difficulty / INIT の段階を飛ばす。INIT は READY で切替）

# 対象外物体の擾乱判定。kit の pipeline/rollout.py と同じ基準
# （obs の *_pos を L1 距離で比較、閾値 1mm、obj_of_interest は除外）。
DISTURB_THRESHOLD_M = 0.001


def disturb_baseline(env, obs):
    """対象外の可動物体の初期位置を控える。キーの選び方は kit と同一。"""
    targets = set(getattr(env.env, "obj_of_interest", []) or [])
    return {k[:-4]: np.asarray(obs[k]).copy() for k in obs
            if k.endswith("_pos") and not k.startswith("robot0")
            and not k.endswith("_to_robot0_eef_pos") and k[:-4] not in targets}


def update_disturb(obs, baseline, worst):
    """対象外物体の最大移動量（L1）を kit と同じ式で更新する。worst=(name, m)。"""
    for name, p0 in baseline.items():
        cur = obs.get(name + "_pos")
        if cur is None:
            continue
        d = float(np.sum(np.abs(np.asarray(cur) - p0)))
        if d > worst[1]:
            worst = (name, d)
    return worst


def eef_rpy(env):
    """エンドエフェクタの傾き（world 基準の ZYX オイラー角、rad）。

    roll はグリッパが真下を向く初期姿勢で 0 になるよう π ずらしてある
    （素の値だと中立で ±180° に張り付き、針が左右へ飛んで読めない）。
    """
    bid = env.sim.model.body_name2id("robot0_right_hand")
    m = np.array(env.sim.data.body_xmat[bid]).reshape(3, 3)
    pitch = -math.asin(min(1.0, max(-1.0, m[2, 0])))
    roll = math.atan2(m[2, 1], m[2, 2])
    yaw = math.atan2(m[1, 0], m[0, 0])
    roll = (roll % (2.0 * math.pi)) - math.pi  # wrap(roll - π)
    return roll, pitch, yaw


def wrap_text(text, max_width, scale):
    """OpenCV の実描画幅で折り返す。長い単語も文字単位に分割する。"""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if cv2.getTextSize(candidate, FONT, scale, 1)[0][0] <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            for char in word:
                if line and cv2.getTextSize(line + char, FONT, scale, 1)[0][0] > max_width:
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return lines


def draw_panel(frame, rpy, info):
    """映像の下の状態帯。ダッシュボード風に横一列で並べて縦に繋ぐ。

    最上段にタスク名。左から: 姿勢ダイヤル 3 つ（実際の傾き）/ STEP・SPEED の
    タイル（小ラベル + 大きい数値）/ 指令速度のバーメーター 6 本（action の値、
    中央がゼロ）。下段に REC 表示・EEF 位置・電池インジケータ。SUCCESS と
    Collided（対象外物体の擾乱）は状態帯ではなく映像の左下へ重ねる。ダイヤルは
    「今どう傾いているか」、メーターは「今どう動かそうとしているか」で別物。
    レイアウトは横 2 カメラ（幅 1280）向けで、1 カメラだと右側が切れる
    （cv2 が安全にクリップするだけで落ちはしない）。

    info のキー: step / max_steps / success / action / scale /
    eef（(3,) か None）/ battery（str、空なら非表示）/
    rec（"REC" | "PAUSE"）/ disturb（(name, m)）
    """
    w = frame.shape[1]
    panel = np.full((PANEL_H, w, 3), PANEL_BG[0], dtype=np.uint8)

    # 電池インジケータはパネル右端。高さは表の最下段（他の表示）に合わせる。
    level = info.get("battery", "")
    if level:
        bw_, bh_ = 36, 14
        bx, by_ = w - bw_ - 28, 118
        cv2.rectangle(panel, (bx, by_), (bx + bw_, by_ + bh_), INK_MUTED, 1)
        cv2.rectangle(panel, (bx + bw_ + 1, by_ + 4),
                      (bx + bw_ + 4, by_ + bh_ - 4), INK_MUTED, -1)  # 端子
        segs = {"full": 4, "max": 4, "wired": 4,
                "medium": 2, "low": 1, "empty": 0}.get(level, -1)
        seg_color = BAD if level in ("low", "empty") else INK
        if segs < 0:
            cv2.putText(panel, "?", (bx + 14, by_ + 12), FONT, 0.42,
                        INK_MUTED, 1, cv2.LINE_AA)
        for s in range(max(0, segs)):
            sx = bx + 2 + s * 9
            cv2.rectangle(panel, (sx, by_ + 2), (sx + 7, by_ + bh_ - 2),
                          seg_color, -1)

    # 並びは左から: INPUT x/y/z → EEF x/y/z → INPUT roll/pitch/yaw → EEF 同 →
    # 姿勢ダイヤル → SPEED → STEP → SUITE & TASK（2026-08-17 ユーザー指定）。
    # INPUT は action（指令速度、中央ゼロの短いバー）、EEF は実際の位置 m と姿勢 度。
    action = info["action"]
    eef = info.get("eef")
    deg = [math.degrees(a) for a in rpy]
    bar_w = 96
    rows_y = (66, 94, 122)

    def bar(x, by, v):
        cv2.rectangle(panel, (x, by), (x + bar_w, by + 9), TRACK, -1)
        mid = x + bar_w // 2
        cv2.line(panel, (mid, by - 1), (mid, by + 10), (90, 90, 90), 1)
        px = int(mid + float(v) * (bar_w // 2))
        if px != mid:
            cv2.rectangle(panel, (min(mid, px), by), (max(mid, px), by + 9),
                          ACCENT, -1)

    cv2.putText(panel, "INPUT", (40, 52), FONT, 0.36, INK_MUTED, 1, cv2.LINE_AA)
    cv2.putText(panel, "EEF", (166, 52), FONT, 0.36, INK_MUTED, 1, cv2.LINE_AA)
    cv2.putText(panel, "INPUT", (306, 52), FONT, 0.36, INK_MUTED, 1, cv2.LINE_AA)
    cv2.putText(panel, "EEF", (450, 52), FONT, 0.36, INK_MUTED, 1, cv2.LINE_AA)
    for i, nm in enumerate(("x", "y", "z")):
        by = rows_y[i]
        cv2.putText(panel, nm, (14, by + 9), FONT, 0.36, INK_MUTED, 1,
                    cv2.LINE_AA)
        bar(36, by, action[i])
        val = f"{eef[i]:+.3f}" if eef is not None else "--"
        cv2.putText(panel, val, (160, by + 9), FONT, 0.4, INK, 1, cv2.LINE_AA)
    for i, nm in enumerate(("roll", "pitch", "yaw")):
        by = rows_y[i]
        cv2.putText(panel, nm, (246, by + 9), FONT, 0.36, INK_MUTED, 1,
                    cv2.LINE_AA)
        # Match the attitude dials without modifying the action sent to the robot.
        direction = -1 if nm in ("roll", "yaw") else 1
        bar(302, by, direction * action[3 + i])
        cv2.putText(panel, f"{deg[i]:+.0f}", (450, by + 9), FONT, 0.4, INK, 1,
                    cv2.LINE_AA)

    cv2.line(panel, (520, 42), (520, PANEL_H - 10), (60, 60, 60), 1, cv2.LINE_AA)

    # 姿勢ダイヤル。針は上が 0°。度数は左の EEF 列にある。
    r, cy = 38, 82  # Fill the existing dial strip; keep labels inside the 148px panel.
    for i, (name, ang) in enumerate(zip(("ROLL", "PITCH", "YAW"), rpy)):
        cx = 566 + i * 92  # 仕切り線 520..796 の間で左右のマージンが等しくなる位置
        cv2.circle(panel, (cx, cy), r, (70, 70, 70), 1, cv2.LINE_AA)
        cv2.line(panel, (cx, cy - r), (cx, cy - r + 5), INK_MUTED, 1, cv2.LINE_AA)
        # Display only: R (negative roll) and stick-right (negative yaw) swing right.
        # ZR (positive pitch) already swings right. EEF angles/actions stay unchanged.
        direction = -1 if name in ("ROLL", "YAW") else 1
        dx = direction * math.sin(ang) * (r - 4)
        dy = -math.cos(ang) * (r - 4)
        cv2.line(panel, (cx, cy), (int(round(cx + dx)), int(round(cy + dy))),
                 ACCENT, 2, cv2.LINE_AA)
        cv2.circle(panel, (cx, cy), 3, ACCENT, -1, cv2.LINE_AA)
        label_w = cv2.getTextSize(name, FONT, 0.35, 1)[0][0]
        cv2.putText(panel, name, (cx - label_w // 2, cy + r + 18), FONT, 0.35,
                    INK_MUTED, 1, cv2.LINE_AA)

    cv2.line(panel, (796, 42), (796, PANEL_H - 10), (60, 60, 60), 1, cv2.LINE_AA)

    # SPEED / STEP タイル（小さい muted ラベルの下に大きい数値）。
    def tile(x, label, value, color=INK, small=""):
        cv2.putText(panel, label, (x, 76), FONT, 0.42, INK_MUTED, 1, cv2.LINE_AA)
        cv2.putText(panel, value, (x, 120), FONT, 0.9, color, 2, cv2.LINE_AA)
        if small:
            (tw, _), _ = cv2.getTextSize(value, FONT, 0.9, 2)
            cv2.putText(panel, small, (x + tw + 8, 120), FONT, 0.45,
                        INK_MUTED, 1, cv2.LINE_AA)

    # GRIP は「いま開いているか閉じているか」と「直前に変更操作が届いたか」を
    # 見るためのもの。入力がすっぽ抜けたときに、押したのに何も変わらないことを
    # 目で確かめられるようにする（2026-09-13 ユーザー要望）。
    if w >= 1180:
        closed = info.get("gripper", GRIPPER_OPEN) == GRIPPER_CLOSE
        tile(1086, "GRIP", "CLOSE" if closed else "OPEN",
             color=ACCENT if info.get("grip_fresh") else INK)
    tile(826, "SPEED", f"x{info['scale']:.1f}")
    time_scale = info.get("time_scale", 0)
    time_label = f"TIME x{time_scale:g}" if time_scale else "TIME unlimited"
    cv2.putText(panel, time_label, (826, 140), FONT, 0.38, INK, 1, cv2.LINE_AA)
    # STEP は達成後に緑へ（達成 step で自動一時停止するので、その数字が残る）
    tile(956, "STEP", f"{info['step']}",
         color=GOOD if info["success"] else INK, small=f"/{info['max_steps']}")

    # 指示の行数に合わせてヘッダーを伸ばし、メーターとの重なりを防ぐ。
    task_lines = wrap_text(info.get("task", ""), w - 24, 0.58)
    extra = 24 * (len(task_lines) - 1)
    if extra:
        panel = np.concatenate([panel[:34],
                                np.full((extra, w, 3), PANEL_BG[0], dtype=np.uint8),
                                panel[34:]], axis=0)
    for i, line in enumerate(task_lines):
        cv2.putText(panel, line, (12, 24 + 24 * i), FONT, 0.58, INK, 1,
                    cv2.LINE_AA)
    out = np.concatenate([frame, panel], axis=0)
    h = frame.shape[0]

    # SUCCESS / Collided は映像の左下、REC / PAUSE は映像の右下に重ねる
    # （どちらも黒縁取りつき）。
    def stamp(text, x, ypos, color, size=0.9):
        cv2.putText(out, text, (x, ypos), FONT, size, (0, 0, 0), 5, cv2.LINE_AA)
        cv2.putText(out, text, (x, ypos), FONT, size, color, 2, cv2.LINE_AA)

    name, dist = info.get("disturb", ("", 0.0))
    collided = dist > DISTURB_THRESHOLD_M
    if info["success"]:
        # SUCCESS は映像全体の中央に大きく出す。
        (tw, th), _ = cv2.getTextSize("SUCCESS", FONT, 2.2, 5)
        cv2.putText(out, "SUCCESS", ((w - tw) // 2, (h + th) // 2), FONT, 2.2,
                    (0, 0, 0), 10, cv2.LINE_AA)
        cv2.putText(out, "SUCCESS", ((w - tw) // 2, (h + th) // 2), FONT, 2.2,
                    GOOD, 5, cv2.LINE_AA)
    if collided:
        stamp(f"Collided: {name}", 14, h - 18, COLLIDE)

    rec = info.get("rec", "")
    if rec:
        rec_color = {"REC": BAD, "PAUSE": ACCENT}.get(rec, INK)
        (tw, _), _ = cv2.getTextSize(rec, FONT, 0.8, 2)
        rx = w - tw - 22
        stamp(rec, rx, h - 18, rec_color, size=0.8)
        if rec == "REC":
            cv2.circle(out, (rx - 16, h - 26), 7, (0, 0, 0), -1, cv2.LINE_AA)
            cv2.circle(out, (rx - 16, h - 26), 5, BAD, -1, cv2.LINE_AA)
    if info.get("aligning") and info.get("align_visible", True):
        (tw, _), _ = cv2.getTextSize("ALIGN", FONT, 0.8, 2)
        stamp("ALIGN", w - tw - 22, h - 54, ACCENT, size=0.8)
    if info.get("grip_fresh"):
        text = ("GRIP CLOSE" if info.get("gripper") == GRIPPER_CLOSE
                else "GRIP OPEN")
        (tw, th), _ = cv2.getTextSize(text, FONT, 1.1, 3)
        stamp(text, (w - tw) // 2, h - 24, ACCENT, size=1.1)
    return out


def render(env, cameras, size: int, small=()):
    """指定カメラを横に並べた 1 コマを作る。

    small に渡したカメラは半分のサイズで縦に積み、右端の列にする（カメラ名を
    左上に焼き込む）。カメラを増やすとその分だけ描画コストが乗る。640 での実測は
    1 枚 34.8 / 2 枚 27.2 / 3 枚 22.8 step/s、**2 枚 + small 2 枚で 21 step/s 前後**。
    control_freq=20 は割らないが、さらに増やすなら `--window-size` を下げる。
    """
    overlay = getattr(env, "draw_goal_overlay", None)  # GuideEnv: 判定領域を表示だけに描く
    virtual = getattr(env, "virtual_camera", lambda n: n)  # GuideEnv: gripper_side などの仮想カメラ
    if not small:
        small = getattr(env, "default_small", ())
        if small:
            # 既定の小窓を足すときは主カメラを縮めて、全体の幅を主カメラ 2 枚分（1280）に収める。
            # 幅 1600 だと WINDOW_AUTOSIZE の窓が画面に収まらず縮小表示される。
            size = int(round(size * 2 / 2.5))
    tiles = []
    for cam in cameras:
        im = env.sim.render(camera_name=virtual(cam), height=size, width=size)
        im = np.ascontiguousarray(np.flip(im, axis=0)[..., ::-1])  # robosuite の上下反転と RGB->BGR
        if overlay:
            overlay(im, cam)
        tiles.append(im)
    if small:
        half = size // 2
        minis = []
        for cam in small:
            im = env.sim.render(camera_name=virtual(cam), height=half, width=half)
            im = np.ascontiguousarray(np.flip(im, axis=0)[..., ::-1])
            if overlay:
                overlay(im, cam)
            cv2.putText(im, cam, (6, 16), FONT, 0.38, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(im, cam, (6, 16), FONT, 0.38, (235, 235, 235), 1,
                        cv2.LINE_AA)
            minis.append(im)
        col = np.concatenate(minis, axis=0)
        if col.shape[0] < size:  # 枚数が半端なら下を地の色で埋める
            pad = np.full((size - col.shape[0], half, 3), PANEL_BG[0],
                          dtype=np.uint8)
            col = np.concatenate([col, pad], axis=0)
        tiles.append(col[:size])
    return np.ascontiguousarray(np.concatenate(tiles, axis=1))


def recording_model_xml(env):
    """Include current fixed-body poses at full precision, not XML's rounded poses."""
    import xml.etree.ElementTree as ET
    model = env.sim.model
    root = ET.fromstring(model.get_xml())
    for body in root.iter("body"):
        name = body.get("name")
        if name:
            idx = model.body_name2id(name)
            body.set("pos", " ".join(format(float(x), ".17g") for x in model.body_pos[idx]))
            body.set("quat", " ".join(format(float(x), ".17g") for x in model.body_quat[idx]))
    return ET.tostring(root, encoding="unicode")


def save_recording(out_dir: Path, meta: dict, actions, states, success: bool, *, model_xml: str | None = None):
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{meta['suite']}_t{meta['task_id']}_i{meta['init_index']}_{stamp}"
    npz = out_dir / f"{stem}.npz"
    # Fixed fixture poses live in the model, not in the flattened simulator state.
    # Embed XML in the same archive so moving the JSON/NPZ pair preserves it.
    extra = {"model_xml": np.asarray(model_xml)} if model_xml is not None else {}
    np.savez_compressed(
        npz,
        actions=np.asarray(actions, dtype=np.float32),
        states=np.asarray(states, dtype=np.float64),
        **extra,
    )
    meta = dict(meta, success=bool(success), steps=len(actions),
                recorded_at=stamp, npz=npz.name)
    if model_xml is not None:
        meta["model_snapshot"] = {"format": "mujoco_xml", "npz_key": "model_xml"}
    (out_dir / f"{stem}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return npz


# ------------------------------------------------------------- SELECT screen

def load_recordings(out_dir: Path) -> dict:
    """recordings/*.json を task 名 -> [記録] に束ねる。

    正本は npz + json のペアで、台帳は持たない（ここで毎回読み直す。数十件なら
    一瞬）。kit_clear は skill 7 節の基準（success かつ max_disturb_m <= 1mm）。
    disturb 未計測の旧記録（2026-08-17 18:02 より前）は成功でも kit 基準では
    未確認なので maybe_clear に分ける。**クリア確定と書けるのは kit_clear だけ。**
    """
    recs = {}
    for j in sorted([*out_dir.glob("*.json"), *out_dir.glob("normal/*.json"),
                     *out_dir.glob("selected/*.json"),
                     *out_dir.glob("*/normal/*.json"),
                     *out_dir.glob("*/selected/*.json"),
                     # 2026-09-16 から selected / normal はタスク別サブディレクトリ
                     *out_dir.glob("*/selected/*/*.json"),
                     *out_dir.glob("*/normal/*/*.json")]):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = d.get("name")
        if not name:
            continue
        disturb = d.get("max_disturb_m")
        success = bool(d.get("success"))
        recs.setdefault(name, []).append(dict(
            suite=d.get("suite"), task_id=d.get("task_id"),
            init_index=d.get("init_index"),   # INIT 段の集計に使う
            steps=d.get("steps"), success=success, disturb=disturb,
            npz=str(j.with_suffix(".npz").relative_to(out_dir)),
            kit_clear=success and disturb is not None
                      and disturb <= DISTURB_THRESHOLD_M,
            maybe_clear=success and disturb is None,
            # 保存メニューで selected を選んだもの。教師の候補として溜めた本数を
            # SELECT 画面に出すために使う（2026-09-13 ユーザー要望）。
            selected=d.get("exit_reason") == "selected" or "selected" in j.relative_to(out_dir).parts))
    return recs


def clear_summary(rows, recs):
    """variant 行の集合に対する記録の集計。

    返り値 (n_rec, best, n_maybe, n_sel)。best は kit 基準クリアの最短
    (steps, npz)、無ければ None。n_maybe は disturb 未計測の成功数、
    n_sel は selected/ に保存した本数（成否は問わない）。
    """
    n_rec = n_maybe = n_sel = 0
    best = None
    for r in rows:
        for rec in recs.get(r["name"], ()):
            n_rec += 1
            n_sel += bool(rec["selected"])
            if rec["kit_clear"] and rec["steps"]:
                if best is None or rec["steps"] < best[0]:
                    best = (rec["steps"], rec["npz"])
            elif rec["maybe_clear"]:
                n_maybe += 1
    return n_rec, best, n_maybe, n_sel


def _clear_text(n_rec, best, n_maybe, n_sel=0) -> str:
    """一覧の右列に出す短い表記。CLEAR は kit 基準の最短 step。

    selected/ に溜めた本数があれば頭に `sel N` を付ける。teleop で何本
    確保できたかを画面で数えるため（2026-09-13 ユーザー要望）。
    """
    head = f"sel {n_sel}  " if n_sel else ""
    if best is not None:
        return f"{head}CLEAR {best[0]}"
    if n_maybe:
        return f"{head}clr? x{n_maybe}"
    if n_rec:
        return f"{head}rec {n_rec}"
    return head.strip()


SELECT_W, SELECT_H = 1080, 720
SELECT_ALL = "ALL"
# variant 行に出すカテゴリの略記（cv2.putText は ASCII のみ）。
CAT_ABBR = {"Background Textures": "BG", "Camera Viewpoints": "CAM",
            "Language Instructions": "LANG", "Light Conditions": "LIGHT",
            "Objects Layout": "LAYOUT", "Robot Initial States": "INIT",
            "Sensor Noise": "NOISE", "未分類": "?"}
def perturbation_type_label(category: str, name: str) -> str:
    """Display subtype only; category and task selection remain authoritative."""
    prefix = CAT_ABBR.get(category, '?')
    if category == 'Objects Layout':
        kind = 'add' if re.search(r'_add_\d+$', name) else 'moved' if re.search(r'_level\d+_sample\d+$', name) else '?'
    elif category == 'Sensor Noise':
        match = re.search(r'_noise_(\d+)$', name)
        number = int(match[1]) if match else 0
        kind = ('motion', 'gaussian', 'zoom', 'fog', 'glass')[(number - 1) // 10] if 1 <= number <= 50 else '?'
    elif category == 'Camera Viewpoints':
        match = re.search(r'_view_(-?\d+)_(-?\d+)_(-?\d+)_(-?\d+)_(-?\d+)_initstate_', name)
        values = tuple(map(int, match.groups())) if match else None
        flags = tuple(i for i, (a, b) in enumerate(zip(values, (0, 0, 100, 0, 0))) if a != b) if values else None
        kind = {(): 'base', (0,): 'orbit-H', (0, 1): 'orbit-HV', (1,): 'orbit-V', (2,): 'distance', (3,): 'direction', (4,): 'direction', (3, 4): 'direction'}.get(flags, '?')
    elif category == 'Robot Initial States':
        kind = 'pose'
    else:
        return prefix
    return f'{prefix}:{kind}'


SELECT_STAGES = ("suite", "base task", "category", "difficulty", "variant")
SELECT_HELP = ("up/down or dpad: move   left/right: +/-10   "
               "enter/A: select   backspace/B: back   esc: quit")


def _fmt_level(level) -> str:
    return f"L{level}" if level is not None else "L?"


def select_options(cache: dict, stage: int, suite_name: str, base, category,
                   level, recs=None):
    """SELECT 画面の現段階の選択肢を (表示行, 値) のリスト 2 本で返す。

    表示行は (本文, 件数, クリア列) の 3 組。絞り込みは「段階の列」として
    並べてあり、新しいフィルタ（録画済み除外や成功率など）は段階を 1 つ
    挟むだけで足せる。recs は load_recordings() の結果（クリア情報の右列）。
    """
    recs = recs or {}
    if stage == 0:
        labels = [(s, str(cache[s]["num_tasks"]) if s in cache else "", "")
                  for s in SUITE_CYCLE]
        return labels + [("Exit", "", "")], list(SUITE_CYCLE) + ["Exit"]
    if stage == 1:
        bases = cache[suite_name]["bases"]
        if suite_name in INIT_MENU_SUITES or suite_name in DIRECT_SUITES:
            def _head(r):
                if r.get("label"):
                    return r["label"]   # 独自 suite の短い ID など
                m = re.match(r"^((?:KITCHEN|LIVING_ROOM|STUDY)_SCENE\d+)_", r["name"])   # libero_90: ID、scene 名、指示文の並び
                return f"#{r['task_id']:<3} {m.group(1):<19}" if m else f"#{r['task_id']}"
            return [(f"{_head(b['rows'][0])}  {b['rows'][0]['language']}", "",
                     _clear_text(*clear_summary(b["rows"], recs))) for b in bases], bases
        labels = [(b["name"], str(len(b["rows"])),
                   _clear_text(*clear_summary(b["rows"], recs)))
                  for b in bases]
        return labels, bases
    rows = base["rows"]
    if suite_name in INIT_MENU_SUITES and stage == 2:
        # INIT ごとの記録（sel N / CLEAR）。base の全記録を init_index で振り分ける
        indices = list(range(base["init_count"]))
        by_init = {}
        for r in base["rows"]:
            for rec in recs.get(r["name"], ()):
                by_init.setdefault(rec.get("init_index"), []).append(rec)
        labels = []
        for i in indices:
            sub = by_init.get(i, [])
            fake = {i: sub}   # clear_summary は rows × recs の形なので 1 行に束ねる
            labels.append((f"INIT={i}", str(len(sub)) if sub else "",
                           _clear_text(*clear_summary([{"name": i}], fake))))
        return labels, indices
    if stage == 2:
        cats = sorted({r["category"] for r in rows})
        opts = [SELECT_ALL] + cats
        labels = [(c, str(len(sub)), _clear_text(*clear_summary(sub, recs)))
                  for c in opts
                  for sub in [[r for r in rows
                               if c == SELECT_ALL or r["category"] == c]]]
        return labels, opts
    rows = [r for r in rows if category == SELECT_ALL or r["category"] == category]
    if stage == 3:
        lvs = sorted({r["level"] for r in rows},
                     key=lambda v: (v is None, v if v is not None else 0))
        opts = [SELECT_ALL] + lvs
        labels = []
        for v in opts:
            sub = [r for r in rows if v == SELECT_ALL or r["level"] == v]
            labels.append((_fmt_level(v) if v != SELECT_ALL else SELECT_ALL,
                           str(len(sub)),
                           _clear_text(*clear_summary(sub, recs))))
        return labels, opts
    rows = [r for r in rows if level == SELECT_ALL or r["level"] == level]
    labels = [(f"{r['task_id']:>5}  {_fmt_level(r['level'])} "
               f"{perturbation_type_label(r['category'], r['name'])} {r['suffix'] or '(base)'}",
               "", _clear_text(*clear_summary([r], recs))) for r in rows]
    return labels, rows


def _fit_text(text: str, max_w: int, scale: float) -> str:
    """右列（件数 / クリア情報）に重ならないよう、本文を幅で切って末尾に '..' を付ける。"""
    if cv2.getTextSize(text, FONT, scale, 1)[0][0] <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cv2.getTextSize(text[:mid] + "..", FONT, scale, 1)[0][0] <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ".."


def draw_select(crumbs: str, labels, cursor: int, detail: str = "",
                message: str = "", detail2: str = "",
                title: str = "SELECT TASK", help_text: str = SELECT_HELP):
    """SELECT 画面の 1 フレームを描く（headless でも呼べる）。

    labels は (本文, 件数, クリア列) の 3 組。クリア列（CLEAR <最短step> /
    clr? / rec）は kit 基準クリアだけ緑にする。
    """
    detail_lines = wrap_text(detail, SELECT_W - 48, 0.45)
    extra = 24 * (len(detail_lines) - 1)
    height = SELECT_H + extra
    img = np.full((height, SELECT_W, 3), PANEL_BG[0], dtype=np.uint8)
    cv2.putText(img, title, (24, 44), FONT, 0.9, INK, 2, cv2.LINE_AA)
    cv2.putText(img, crumbs, (24, 76), FONT, 0.5, ACCENT, 1, cv2.LINE_AA)
    if message:
        cv2.putText(img, message, (24, SELECT_H // 2), FONT, 0.7, INK, 1,
                    cv2.LINE_AA)
    top_y, row_h, view = 112, 27, 20
    first = max(0, min(cursor - view // 2, len(labels) - view))
    # 本文の幅は画面内で揃える: どこかの行に右列（クリア情報 / 件数）があれば全行その手前で切る
    shown = labels[first:first + view]
    limit = (SELECT_W - 340 if any(l[2] for l in shown) else
             SELECT_W - 110 if any(l[1] for l in shown) else SELECT_W - 40) - 30
    for i, (label, count, clear) in enumerate(labels[first:first + view]):
        j = first + i
        y = top_y + i * row_h
        if j == cursor:
            cv2.rectangle(img, (16, y - 19), (SELECT_W - 16, y + 7), TRACK, -1)
        color = INK if j == cursor else INK_MUTED
        cv2.putText(img, _fit_text(label, limit, 0.48), (30, y), FONT, 0.48, color, 1, cv2.LINE_AA)
        if clear:
            c_color = GOOD if clear.startswith("CLEAR") else color
            cv2.putText(img, clear, (SELECT_W - 330, y), FONT, 0.48, c_color, 1,
                        cv2.LINE_AA)
        if count:
            cv2.putText(img, count, (SELECT_W - 100, y), FONT, 0.48, color, 1,
                        cv2.LINE_AA)
    if labels and len(labels) > view:
        cv2.putText(img, f"{cursor + 1}/{len(labels)}", (SELECT_W - 100, 76),
                    FONT, 0.5, INK_MUTED, 1, cv2.LINE_AA)
    if detail or detail2:
        cv2.line(img, (16, SELECT_H - 92), (SELECT_W - 16, SELECT_H - 92),
                 (60, 60, 60), 1, cv2.LINE_AA)
    if detail:
        for i, line in enumerate(detail_lines):
            cv2.putText(img, line, (24, SELECT_H - 68 + 24 * i), FONT, 0.45,
                        INK, 1, cv2.LINE_AA)
    if detail2:
        cv2.putText(img, detail2[:110], (24, height - 44), FONT, 0.45,
                    GOOD if "CLEAR" in detail2 else INK_MUTED, 1, cv2.LINE_AA)
    cv2.putText(img, help_text, (24, height - 16), FONT, 0.42, INK_MUTED, 1,
                cv2.LINE_AA)
    return img


def _select_crumbs(stage: int, suite_name: str, base, category, level) -> str:
    if suite_name in INIT_MENU_SUITES and stage == 2 and base is not None:
        return f"[INIT]  {suite_name} > TASK={base['rows'][0]['task_id']}"
    parts = []
    if stage > 0:
        parts.append(suite_name)
    if stage > 1 and base is not None:
        parts.append(base["name"][:48])
    if stage > 2:
        parts.append(category)
    if stage > 3:
        parts.append(_fmt_level(level) if level != SELECT_ALL else SELECT_ALL)
    crumb = " > ".join(parts)
    return f"[{SELECT_STAGES[stage]}]  {crumb}" if crumb else \
        f"[{SELECT_STAGES[stage]}]"


def confirm_exit(ctl):
    """Exit確認。A/Bの押しっぱなしを引き継がない。

    既定は Yes（2026-09-13 ユーザー指定）。ここへ来るのは task selection で
    自分から抜けようとしたときなので、そのまま A で閉じられるようにする。
    """
    cursor = 1
    trace = trace_for(ctl)
    is_pad = isinstance(ctl, GamepadDrive)
    if is_pad:
        ctl.capture_input()
    try:
        while True:
            img = draw_select("Exit teleop?", [("No - return", "", ""),
                                               ("Yes - exit", "", "")], cursor,
                              "Close teleop and return to the terminal.",
                              title="EXIT TELEOP?",
                              help_text=("up/down: choose   A/Y: confirm   B/X: cancel" if is_pad else
                                         "up/down: choose   enter: confirm   esc: cancel"))
            cv2.imshow(WINDOW, img)
            key = cv2.waitKeyEx(30)
            if is_pad:
                key = -1
            move = -1 if key in (63232, ord("w")) else 1 if key in (63233, ord("s")) else 0
            enter, back = key in (13, 10), key in (27, 8, 127, ord("b"))
            if is_pad:
                g_move, _, g_enter, g_back = ctl.select_nav()
                move += g_move
                enter |= g_enter
                back |= g_back
            trace.observe(ctl, screen="exit_confirm", cursor=cursor)
            if move:
                trace.command("exit_cursor", move=move)
                cursor = (cursor + move) % 2
            if back:
                trace.command("exit_cancel")
                return False
            if enter:
                trace.command("exit_confirm", cursor=cursor)
                return cursor == 1
    finally:
        trace.finish()
        if is_pad:
            ctl.capture_input()


def select_screen(ctl, cache: dict, suite_name: str, out_dir: Path,
                  memory: dict = None, resume: bool = False, init_index: int = 0):
    """タスク選択画面。決定で (suite, task_id, init_indexまたはNone) を返し、esc は None（終了）。

    suite -> base task -> 摂動カテゴリ -> 難易度 -> variant の
    段階式。suite の索引は選んだときに 1 回だけ作ってキャッシュする。
    クリア情報（右列と下段の npz 名）は recordings/*.json を入るたびに読み直す
    ので、保存して戻ってくればすぐ映る。

    memory には決定時の絞り込み状態（suite / 段階 / カーソル / base /
    カテゴリ / 難易度）を書き戻す。resume=True ならそこから再開する
    （READY から戻ったとき用。保存後はトップから。2026-08-19 ユーザー指定）。
    """
    if suite_name not in SUITE_CYCLE:
        suite_name = SUITE_CYCLE[0]
    trace = trace_for(ctl)
    if isinstance(ctl, GamepadDrive):
        ctl.capture_input()
    recs = load_recordings(out_dir)
    stage = 0
    cur = [SUITE_CYCLE.index(suite_name), 0, 0, 0, 0]
    base, category, level = None, SELECT_ALL, SELECT_ALL
    if resume and memory:
        suite_name = memory.get("suite", suite_name)
        if suite_name not in cache:
            cache[suite_name] = build_suite_index(suite_name)
        stage = memory.get("stage", 0)
        cur = list(memory.get("cur", cur))
        base = memory.get("base")
        category = memory.get("category", SELECT_ALL)
        level = memory.get("level", SELECT_ALL)

    while True:
        labels, values = select_options(cache, stage, suite_name, base,
                                        category, level, recs)
        cursor = min(cur[stage], max(0, len(labels) - 1))
        cur[stage] = cursor
        detail = detail2 = ""
        if suite_name in INIT_MENU_SUITES and stage == 2 and values:
            detail = base["rows"][0]["language"]
            detail2 = f"INIT={values[cursor]}  /  {len(values)} initial states"
        elif stage == 4 and values:
            detail = values[cursor]["language"]
            n_rec, best, n_maybe, _ = clear_summary([values[cursor]], recs)
            if best is not None:
                detail2 = f"CLEAR {best[0]} steps  {best[1]}"
            elif n_rec:
                detail2 = f"rec {n_rec}（kit 基準クリアなし）"
        elif stage == 1 and values and isinstance(values[cursor], dict):
            if suite_name in INIT_MENU_SUITES:
                detail = values[cursor]["rows"][0]["language"]
            n_rec, best, n_maybe, _ = clear_summary(values[cursor]["rows"], recs)
            if best is not None:
                detail2 = f"CLEAR {best[0]} steps  {best[1]}"
        elif stage == 1 and values:
            detail = f"{values[cursor]}: {labels[cursor][1]} tasks"
        img = draw_select(_select_crumbs(stage, suite_name, base, category,
                                         level), labels, cursor, detail,
                          detail2=detail2,
                          **({"help_text": "dpad: choose   A/Y: confirm   B/X: back"}
                             if isinstance(ctl, GamepadDrive) else {}))
        cv2.imshow(WINDOW, img)
        key = cv2.waitKeyEx(30)
        if isinstance(ctl, GamepadDrive):
            key = -1        # gamepad のときはキーボードを見ない（2026-09-12）

        move = page = 0
        enter = back = False
        # macOS の cv2 は矢印を 63232..63235 で返す（waitKeyEx）。
        if key in (63232, ord("w")):
            move -= 1
        elif key in (63233, ord("s")):
            move += 1
        elif key in (63234, ord("a")):
            page -= 10
        elif key in (63235, ord("d")):
            page += 10
        elif key in (13, 10):
            enter = True
        elif key in (8, 127, ord("b")):
            back = True
        elif key == 27:
            if confirm_exit(ctl):
                return None
            continue
        if isinstance(ctl, GamepadDrive):
            g_move, g_page, g_enter, g_back = ctl.select_nav()
            move += g_move
            page += g_page
            enter |= g_enter
            back |= g_back

        trace.observe(ctl, screen="select", stage=stage, cursor=cursor)
        if move or page or back or enter:
            trace.command("select_navigation", move=move, page=page, back=back, enter=enter)
        if move and labels:
            cur[stage] = (cursor + move) % len(labels)  # 上下は循環
        if page and labels:
            cur[stage] = min(max(cursor + page, 0), len(labels) - 1)
        if back and stage > 0:
            stage -= 1
            continue
        if not (enter and labels):
            continue

        value = values[cursor]
        if stage == 0:
            if value == "Exit":
                if confirm_exit(ctl):
                    return None
                continue
            if value not in cache:
                cv2.imshow(WINDOW, draw_select(
                    f"[{SELECT_STAGES[0]}]", labels, cursor,
                    message=f"loading {value} ..."))
                cv2.waitKey(1)
                cache[value] = build_suite_index(value)
            suite_name = value
            base, category, level = None, SELECT_ALL, SELECT_ALL
            cur[1:] = [0, 0, 0, 0]
            stage = 1
        elif stage == 1:
            if suite_name in DIRECT_SUITES:
                if memory is not None:
                    memory.update(suite=suite_name, stage=stage, cur=list(cur),
                                  base=value, category=SELECT_ALL, level=SELECT_ALL)
                return suite_name, value["rows"][0]["task_id"], None
            if suite_name in INIT_MENU_SUITES:
                base = dict(value)
                base["init_count"] = len(build_task(suite_name, value["rows"][0]["task_id"])["init_states"])
                cur[2] = min(max(init_index, 0), base["init_count"] - 1)
                stage = 2
                continue
            base = value
            category, level = SELECT_ALL, SELECT_ALL
            cur[2:] = [0, 0, 0]
            stage = 2
        elif stage == 2:
            if suite_name in INIT_MENU_SUITES:
                if memory is not None:
                    memory.update(suite=suite_name, stage=stage, cur=list(cur),
                                  base=base, category=SELECT_ALL, level=SELECT_ALL)
                return suite_name, base["rows"][0]["task_id"], value
            category = value
            level = SELECT_ALL
            cur[3:] = [0, 0]
            stage = 3
        elif stage == 3:
            level = value
            cur[4] = 0
            stage = 4
        else:
            if memory is not None:
                memory.update(suite=suite_name, stage=stage, cur=list(cur),
                              base=base, category=category, level=level)
            return suite_name, value["task_id"], None



def play_episode(env, task, obs, ctl, args, cameras, small_cams,
                 init_states, init_index, takeover=None):
    """Operate and record READY -> PLAY <-> PAUSE.

    Gamepad: READY A starts / X returns; PLAY Y closes / B opens / X pauses.
    PAUSE always shows its success-dependent menu; A confirms and X resumes.
    Keyboard/mouse keep their existing controls. Stopped phases never step physics.
    Returns (reason, actions, pre_action_states, success, worst, init_index).
    """
    ctl.gripper = GRIPPER_OPEN  # New episode: never inherit the previous close command.
    pacer = StepPacer(args.control_freq, args.time_scale)
    print(f"[time] scale={args.time_scale:g} (0=unlimited), simulation={args.control_freq}Hz")
    lang = task["language"]
    hud_task = f"{task['suite']} #{task['task_id']}: {lang}"
    if takeover is not None:
        hud_task = f"TAKEOVER {takeover['k']}/{takeover['n']} " + hud_task

    def fresh_episode():
        """(actions, states, step, baseline, worst, gripper) を新しい episode の開始値にそろえる。"""
        if takeover is None:
            return [], [], 0, disturb_baseline(env, obs), ("", 0.0), GRIPPER_OPEN
        k = takeover["k"]
        # prefix は録画の値をそのまま持ち、基準位置は READY（prefix で動いた分を見落とさない）
        worst0 = update_disturb(obs, takeover["baseline"], ("", 0.0))
        grip = GRIPPER_CLOSE if k > 0 and takeover["actions"][k - 1][6] > 0 else GRIPPER_OPEN
        return ([np.asarray(a, dtype=float) for a in takeover["actions"][:k]],
                [np.asarray(s, dtype=float) for s in takeover["states"][:k]], k, takeover["baseline"], worst0, grip)

    actions, states, step, baseline, worst, ctl.gripper = fresh_episode()
    success = False
    success_pause_step = None
    reason = "max_steps"
    disturb_warned = False
    ui = EpisodeInteraction()
    start_banner_until = 0.0
    alignment = StartAlignment(getattr(args, "auto_align_start", True), args.control_freq)
    grip_prev, grip_changed_at = ctl.gripper, -1e9
    last_action = np.zeros(7)
    ctl.quit = None
    is_pad = isinstance(ctl, GamepadDrive)
    trace = trace_for(ctl)
    if is_pad:
        ctl.capture_input()

    def retry(source):
        nonlocal obs, baseline, worst, disturb_warned, actions, states
        nonlocal step, success, success_pause_step, alignment
        trace.command("retry", source=source, phase=ui.phase, menu=ui.visible_menu(is_pad), step=step, init_index=init_index)
        if takeover is None:
            print(f"[retry] {len(actions)} step を捨てて初期状態からやり直す")
            obs = reset_to(env, init_states[init_index])
        else:
            print(f"[retry] {len(actions) - takeover['k']} step を捨てて引き継ぎ点 step {takeover['k']} からやり直す")
            obs = takeover_reset(env, takeover, takeover["k"])
        actions, states, step, baseline, worst, ctl.gripper = fresh_episode()
        disturb_warned = False
        success = False
        success_pause_step = None
        ui.enter("ready")
        alignment = StartAlignment(getattr(args, "auto_align_start", True), args.control_freq)
        if is_pad:
            ctl.capture_input()

    try:
        while step < args.max_steps:
            ctl.gripper_live = not ui.stopped
            if is_pad:
                ctl.capture_input()
                pad_action = ctl.action()  # retain gripper edges even while pacer waits
            stopped = ui.stopped
            if stopped:
                pacer.reset()
            # 停止中も入力を消費して状態帯へ映す（env は進めない）。
            # メニュー中は入力表示を止めて、モーダルであることを分からせる。
            if ui.phase == "play" and alignment.active:
                shown = last_action
            elif ui.keyboard_menu is not None:
                shown = np.zeros(7)
            else:
                shown = pad_action if is_pad else (ctl.action() if stopped else last_action)
            if ctl.gripper != grip_prev:
                grip_prev, grip_changed_at = ctl.gripper, time.monotonic()
            frame = draw_panel(
                render(env, cameras, args.window_size, small_cams),
                eef_rpy(env), dict(
                    step=step, max_steps=args.max_steps, success=success,
                    action=shown, scale=ctl.scale, time_scale=args.time_scale,
                    eef=obs.get("robot0_eef_pos"),
                    battery=ctl.battery() if is_pad else "",
                    gripper=ctl.gripper,
                    grip_fresh=time.monotonic() - grip_changed_at < GRIP_FLASH_S,
                    rec={"ready": "READY", "countdown": "READY", "pause": "PAUSE"}.get(ui.phase, "REC"),
                    aligning=ui.phase == "play" and alignment.active,
                    align_visible=int(time.monotonic() * 2) % 2 == 0,
                    disturb=worst, task=(f"{task['suite']} #{task['task_id']} INIT={init_index}/{len(init_states)-1}: {lang}"
                                         if task["suite"] in INIT_MENU_SUITES else hud_task), suite=task["suite"],
                    task_id=task["task_id"]))
            # 走行中は HUD を出さない（映像と状態帯だけ）。停止中だけ表示する
            # （2026-08-17 ユーザー要望）。
            if is_pad and ui.confirming_exit:
                draw_pause_menu(frame, ui.cursor, video_h=args.window_size,
                                gamepad=True, confirming_exit=True)
            elif is_pad and ui.phase == "countdown":
                draw_start_countdown(frame, ui.countdown_remaining(time.monotonic()), args.window_size)
            elif is_pad and ui.phase == "play" and time.monotonic() < start_banner_until:
                draw_start_countdown(frame, 0, args.window_size)
            elif ui.phase == "play" and alignment.active:
                draw_hud(frame, ["XYZ + grip: manual / rotation input: end ALIGN",
                                 "X: stop & pause" if is_pad else "p: stop & pause"])
            elif is_pad and ui.phase == "pause":
                draw_pause_menu(frame, ui.cursor, success, args.window_size, worst,
                                gamepad=True, saving=ui.saving)
            elif ui.keyboard_menu is not None:
                draw_pause_menu(frame, ui.keyboard_menu, success, args.window_size, worst)
            elif ui.phase == "ready":
                draw_ready_menu(frame, ui.cursor, alignment.active, ctl.scale,
                                args.time_scale, init_index, len(init_states),
                                task["suite"] in INIT_MENU_SUITES,
                                args.window_size, gamepad=is_pad)
            elif ui.phase == "pause":
                resume = "space/p"
                extra = "b: menu"
                enter_action = "menu" if success else "save"
                draw_hud(frame, [
                    f"PAUSED - {resume}: play   0: retry   enter: {enter_action}   "
                    f"esc: discard & select   {extra}   (gripper locked)",
                    *ctl.HELP,
                    *(["SUCCESS: enter/b opens save menu (selected / normal / retry / back)"]
                      if success else []),
                ])
            cv2.imshow(WINDOW, frame)
            key = cv2.waitKey(30 if stopped else 1)
            # Pump window events, but ignore all keyboard controls in gamepad mode.
            if is_pad:
                key = -1

            trace.observe(ctl, screen="episode", phase=ui.phase,
                          menu=ui.visible_menu(is_pad), stopped=stopped,
                          success=success, step=step, init_index=init_index)

            if is_pad:
                pressed = ctl.frame.pressed
                zl = ctl._rising("time_slower", ctl.frame.get_axis(4) > PAD_TRIGGER_ON)
                zr = ctl._rising("scale_dn", ctl.frame.get_axis(5) > PAD_TRIGGER_ON)
                op = ui.gamepad_command(success, pressed, time.monotonic())
                if op:
                    trace.command(op, phase=ui.phase, menu=ui.cursor)
                if op in ("save", "selected", "discard"):
                    reason = op
                    break
                if op == "retry":
                    retry("pause_menu")
                    continue
                if op == "toggle_alignment":
                    args.auto_align_start = not getattr(args, "auto_align_start", True)
                    alignment = StartAlignment(args.auto_align_start, args.control_freq)
                    print(f"[align] AUTO ALIGN {'ON' if args.auto_align_start else 'OFF'}")
                    continue
                if op == "countdown":
                    ui.start_countdown(time.monotonic())
                    continue
                if op in ("ready", "play", "pause"):
                    if op == "play" and ui.phase == "countdown":
                        start_banner_until = time.monotonic() + .5
                    if op == "pause":
                        alignment.cancel()
                    ui.enter(op)
                    continue
                if ui.confirming_exit:
                    continue
                if ui.phase in ("ready", "pause"):
                    time_direction = int(zr) - int(zl)
                    scale_direction = int(ctl.BTN_R in pressed) - int(ctl.BTN_L in pressed)
                    if time_direction:
                        args.time_scale = adjust_time_scale(args.time_scale, time_direction)
                        pacer = StepPacer(args.control_freq, args.time_scale)
                        trace.command("time_scale", value=args.time_scale)
                    if scale_direction:
                        ctl.scale = min(2.0, max(0.1, ctl.scale + .1 * scale_direction))
                        trace.command("speed", value=ctl.scale)
                if ui.phase == "ready":
                    direction = int(ctl.DPAD_RIGHT in pressed) - int(ctl.DPAD_LEFT in pressed)
                    if direction and task["suite"] in INIT_MENU_SUITES and takeover is None:
                        init_index = (init_index + direction) % len(init_states)
                        retry("ready_init")
                        continue
            else:
                # Keyboard/mouse retain their existing pause and save behavior.
                if ui.keyboard_menu is not None:
                    move = int(key == ord("s")) - int(key == ord("w"))
                    if move:
                        ui.keyboard_menu = (ui.keyboard_menu + move) % len(pause_menu_items(success))
                    if key in (27, 8, 127, ord("b")):
                        ui.keyboard_menu = None
                    elif key in (13, 10):
                        if success and ui.keyboard_menu in (0, 1):
                            reason = "selected" if ui.keyboard_menu == 0 else "save"
                            break
                        if ui.keyboard_menu == (2 if success else 0):
                            ui.keyboard_menu = None
                            retry("pause_menu")
                            continue
                        ui.keyboard_menu = None
                    continue
                if ui.phase == "ready":
                    ui.cursor = (ui.cursor + int(key == ord("s")) - int(key == ord("w"))) % 3
                    if key in (13, 10):
                        if ui.cursor == 2:
                            reason = "discard"
                            break
                        if ui.cursor == 1:
                            args.auto_align_start = not getattr(args, "auto_align_start", True)
                            alignment = StartAlignment(args.auto_align_start, args.control_freq)
                        else:
                            ui.enter("play")
                        continue
                    direction = int(key == ord("]")) - int(key == ord("["))
                    if direction:
                        args.time_scale = adjust_time_scale(args.time_scale, direction)
                        pacer = StepPacer(args.control_freq, args.time_scale)
                        print(f"[time] scale={args.time_scale:g} (0=unlimited)")
                    direction = int(key == ord(".")) - int(key == ord(","))
                    if direction and task["suite"] in INIT_MENU_SUITES and takeover is None:
                        init_index = (init_index + direction) % len(init_states)
                        retry("ready_init")
                        last_action = np.zeros(7)
                        continue
                toggled = key == ord("p") or (stopped and key == 32)
                if ui.phase == "pause":
                    if key == ord("b") or (success and key in (13, 10)):
                        ui.keyboard_menu = 0
                        continue
                    if key == ord("0"):
                        retry("keyboard_0")
                        continue
                ctl.handle_key(key)
                if ctl.quit == "save" and ui.phase == "ready":
                    ctl.quit = None
                if ctl.quit is not None:
                    reason = ctl.quit
                    break
                if toggled:
                    if ui.phase == "play":
                        alignment.cancel()
                    ui.enter("pause" if ui.phase == "play" else "play")
                    continue
            if stopped:
                continue

            manual_action = pad_action if is_pad else ctl.action()
            if alignment.active and np.any(manual_action[3:6] != 0):
                alignment.cancel("manual_rotation")
                trace.command("alignment_cancel", reason="manual_rotation", step=step,
                              rot=[float(v) for v in manual_action[3:6]])
                # 何も触っていないのに解除される報告があるので、どの軸がいくつ
                # 出ていたかを残す（2026-09-13）。roll=L/R・pitch=ZL/ZR と十字、
                # yaw=右スティック横。
                print("[align] 手動回転入力で補正を終了し、手動PLAYへ移行"
                      f"  roll={manual_action[3]:+.3f} pitch={manual_action[4]:+.3f}"
                      f" yaw={manual_action[5]:+.3f}")
            if not pacer.ready():
                continue
            if alignment.active:
                action = alignment.action(env, ctl.gripper, manual_action[:3])
                if action is None:
                    print(f"[align] {alignment.status}: {alignment.steps} steps")
                    if alignment.status == "timeout":
                        ui.enter("pause")
                        continue
                    action = manual_action  # Keep XYZ moving through the handoff.
            else:
                action = manual_action
            states.append(env.get_sim_state())
            obs, _, _, _ = env.step(action)
            actions.append(action)
            last_action = action
            worst = update_disturb(obs, baseline, worst)
            if worst[1] > DISTURB_THRESHOLD_M and not disturb_warned:
                disturb_warned = True
                print(f"[disturb] 対象外の {worst[0]} が "
                      f"{worst[1] * 1000:.1f}mm 動いた（kit 基準では不成功）")
            step += 1
            if alignment.active and worst[1] > DISTURB_THRESHOLD_M:
                alignment.cancel("disturbed")
                ui.enter("pause")
                print("[align] 対象外物体の移動を検知したため補正を止めてPAUSE")
            if not success and bool(env.check_success()):
                success = True
                success_pause_step = step + SUCCESS_PAUSE_STEPS
                print(f"[success] step {step} でゴール成立。"
                      f"{SUCCESS_PAUSE_STEPS} step 後（step {success_pause_step}）に自動で一時停止する")
            if success_pause_step is not None and step >= success_pause_step:
                success_pause_step = None  # 再開後は自動停止を繰り返さない
                alignment.cancel("success_pause")
                trace.command("auto_pause", before=ui.phase, after="pause", step=step)
                ui.enter("pause", automatic=is_pad, collided=worst[1] > DISTURB_THRESHOLD_M)
                print(f"[pause] step {step}: ゴール成立から{SUCCESS_PAUSE_STEPS} step進めて一時停止した"
                      + ("（A: メニュー決定 / X: 再開）" if is_pad else "（enter/b: 保存メニュー / p: 再開）"))
    except KeyboardInterrupt:
        reason = "interrupt"
    except Exception as exc:  # env が落ちても、それまでの記録は残す
        reason = "error"
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        trace.emit("episode_end", reason=reason, phase=ui.phase, menu=ui.visible_menu(is_pad), step=step)
        trace.finish()
        args.start_alignment_result = alignment.summary()
        ctl.gripper_live = True  # 次のエピソード・他経路に影響を残さない
    return reason, actions, states, success, worst, init_index


def check_assets(assets=None):
    """未マウントはシミュレータを起動する前に案内する。"""
    assets = assets or LIBERO_SRC / "libero/libero/assets"
    target = assets.resolve()
    if assets.is_dir():
        return True
    if len(target.parts) > 2 and target.parts[1] == "Volumes":
        volume = Path(*target.parts[:3])
        if not volume.is_mount():
            print(f"[assets] SSD {volume.name} が未マウントです。"
                  "SSD を接続・マウントしてから、もう一度起動してください。"
                  f"\n[assets] 必要な場所: {target}", file=sys.stderr)
            return False
    print(f"[assets] assets が見つかりません: {target}\n"
          "SSD の接続と assets の配置を確認してください。", file=sys.stderr)
    return False


def main() -> int:
    input_debug_startup(__file__)
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", default="libero_spatial", choices=SUITE_CYCLE)
    p.add_argument("--task-id", type=int, default=None,
                   help="指定すると SELECT 画面を飛ばして直接始める。"
                        "省略時はタスク選択画面から")
    p.add_argument("--init-index", type=int, default=0)
    p.add_argument("--device", choices=["keyboard", "mouse", "gamepad"],
                   default="keyboard")
    p.add_argument("--mouse-map", default=MOUSE_MAP,
                   help="マウスの 右/下/ホイール を world 軸へ割り当てる（例 -y,-x,+z）")
    p.add_argument("--pad-flip", default="",
                   help="ゲームパッドで符号を反転する軸（例 y,yaw）。実行中の x/y/z/c "
                        "キーで合わせた結果が終了時に表示される")
    p.add_argument("--pad-yaw-deadzone", type=float, default=PAD_YAW_DEADZONE,
                   help="右スティック横（yaw）の遊び。この倒し量までは z だけが効く")
    p.add_argument("--pad-z-gain", type=float, default=PAD_Z_GAIN,
                   help="z（右スティック縦）だけに掛かる倍率")
    p.add_argument("--pad-xy-gain", type=float, default=PAD_XY_GAIN,
                   help="左スティック（x/y）に掛かる倍率。実質上限は 2.8 程度")
    p.add_argument("--hold-mode", choices=["decay", "latch"], default="decay",
                   help="キーの効き方。decay=離すと減速して止まる / latch=space まで保持")
    p.add_argument("--hold-sec", type=float, default=HOLD_SEC,
                   help="キーイベント後にフル速度を保つ秒数。OS のキーリピート待ちを埋める")
    p.add_argument("--decay-tau", type=float, default=DECAY_TAU,
                   help="その後の減衰の時定数（秒）")
    p.add_argument("--tap-gain", type=float, default=TAP_GAIN,
                   help="ちょん押し 1 回のときの速度比。小さいほど微調整が効く")
    p.add_argument("--ramp-events", type=int, default=RAMP_EVENTS,
                   help="キーリピート何回でフル速度に達するか")
    p.add_argument("--camera", default="frontview",
                   help="描画に使うカメラ。カンマ区切りで複数指定すると横に並べる。"
                        "frontview / agentview / robot0_eye_in_hand")
    p.add_argument("--small-camera", default="",
                   help="右端に半分サイズで縦に積むカメラ（カンマ区切り。"
                        "例 birdview,robot0_robotview）。既定は無効")
    p.add_argument("--window-size", type=int, default=640)
    p.add_argument("--control-freq", type=int, default=20)
    p.add_argument("--time-scale", type=float, default=0.6,
                   help="実時間に対するシミュレーション速度。0=従来の無制限、1=等速、0.5=半速")
    p.add_argument("--auto-align-start", action=argparse.BooleanOptionalAction, default=True,
                   help="開始時に姿勢3軸をHUDの0へ合わせる（補正も記録、既定ON）")
    p.add_argument("--max-steps", type=int, default=6000,
                   help="記録する最大 step 数。30 step/s 前後なので 6000 で約 3 分")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--smoke", type=int, default=0, metavar="N",
                   help="窓を出さずに N step だけダミー操作して経路を確認する")
    p.add_argument("--list-tasks", nargs="?", const="", metavar="SUBSTR",
                   help="suite の task を一覧する。文字列を渡すと名前で絞る")
    p.add_argument("--list-clears", action="store_true",
                   help="base task ごとのクリア状況と npz を一覧する")
    p.add_argument("--from-recording", type=Path, default=None, metavar="NPZ",
                   help="録画（replay_bridge 変換 npz か teleop 録画）の途中から引き継ぐ。suite/task は同名 json から取る")
    p.add_argument("--takeover-step", type=int, default=None, metavar="K",
                   help="--from-recording の何 step 目の状態から始めるか（既定は末尾）")
    p.add_argument("--takeover-replay", action="store_true",
                   help="開始前に actions[k:] を再生して録画の残りと一致することを確かめる")
    args = p.parse_args()
    if args.takeover_step is not None and args.from_recording is None:
        p.error("--takeover-step needs --from-recording")
    if args.from_recording is not None and args.auto_align_start:
        args.auto_align_start = False   # 引き継ぎ点の姿勢を勝手に回さない
        print("[align] --from-recording では AUTO ALIGN を OFF にする")
    if not math.isfinite(args.time_scale) or args.time_scale < 0 or args.control_freq <= 0:
        p.error("--time-scale must be finite and >= 0; --control-freq must be > 0")

    if args.list_tasks is not None:
        return list_tasks(args.suite, args.list_tasks)
    if args.list_clears:
        return list_clears(args.out)

    if not check_assets():
        return 1

    cameras = [c.strip() for c in args.camera.split(",") if c.strip()]
    small_cams = [c.strip() for c in args.small_camera.split(",") if c.strip()]

    takeover = None
    if args.from_recording is not None:
        takeover = load_takeover(args.from_recording, args.takeover_step)
        if takeover["suite"] is None or takeover["task_id"] is None:
            p.error("録画の json に suite / task_id が無い")
        print(f"[takeover] {takeover['npz'].name}: step {takeover['k']}/{takeover['n']} "
              f"branch_exact={takeover['branch_exact']}")

    if args.smoke and takeover is not None:
        task = build_task(takeover["suite"], takeover["task_id"])
        env = make_task_env(task, args.control_freq, args.max_steps + 100)
        obs = prepare_takeover_env(env, takeover)
        err = takeover_check(env, takeover)
        for i in range(args.smoke):
            a = np.zeros(7); a[6] = GRIPPER_OPEN
            obs, _, _, _ = env.step(a)
        frame = render(env, cameras, args.window_size, small_cams)
        print(f"[smoke] takeover step {takeover['k']}/{takeover['n']} replay_max_state_error={err:.3g} "
              f"+{args.smoke} steps, frame={frame.shape}, success={env.check_success()}")
        env.close()
        return 0 if err < 1e-9 or not takeover["branch_exact"] else 1

    if args.smoke:
        task = build_task(args.suite, args.task_id or 0)
        init_states = task["init_states"]
        print(f"[task] {task['suite']} #{task['task_id']}  {task['name']}")
        env = make_task_env(task, args.control_freq, args.max_steps + 100)
        obs = reset_to(env, init_states[min(args.init_index,
                                            len(init_states) - 1)])
        # gamepad は初期化と polling 経路も一緒に通す（コントローラ未接続なら
        # ここで落ちる）。スティックが中立なら action は動きに影響しない。
        pad = (GamepadDrive(args.pad_flip, args.pad_yaw_deadzone,
                            args.pad_z_gain, args.pad_xy_gain)
               if args.device == "gamepad" else None)
        t0 = time.time()
        for i in range(args.smoke):
            a = np.zeros(7)
            a[2] = -0.3 if i < args.smoke // 2 else 0.3
            a[6] = GRIPPER_OPEN
            if pad is not None:
                pad.action()
            env.step(a)
            frame = render(env, cameras, args.window_size, small_cams)
        frame = draw_panel(frame, eef_rpy(env), dict(  # 帯の経路も通す
            step=args.smoke, max_steps=args.smoke,
            success=bool(env.check_success()), action=np.zeros(7), scale=1.0,
            eef=obs.get("robot0_eef_pos"), battery="", rec="REC",
            disturb=("", 0.0)))
        dt = time.time() - t0
        # SELECT 画面の経路（索引構築 + 記録スキャン + 描画）も 1 フレーム通す。
        cache = {args.suite: build_suite_index(args.suite)}
        labels, values = select_options(cache, 1, args.suite, None,
                                        SELECT_ALL, SELECT_ALL,
                                        load_recordings(args.out))
        sel_img = draw_select(_select_crumbs(1, args.suite, None, SELECT_ALL,
                                             SELECT_ALL), labels, 0)
        print(f"[smoke] {args.smoke} steps in {dt:.1f}s "
              f"({args.smoke / dt:.1f} step/s incl. render), "
              f"frame={frame.shape}, success={env.check_success()}, "
              f"select frame={sel_img.shape} bases={len(values)}")
        env.close()
        return 0

    if args.device == "gamepad":
        ctl = GamepadDrive(args.pad_flip, args.pad_yaw_deadzone,
                           args.pad_z_gain, args.pad_xy_gain)
    elif args.device == "mouse":
        ctl = MouseDrag(args.mouse_map)
    else:
        ctl = KeyboardDrive(args.hold_mode, args.hold_sec, args.decay_tau,
                            args.tap_gain, args.ramp_events)

    cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
    if isinstance(ctl, MouseDrag):
        cv2.setMouseCallback(WINDOW, ctl.on_mouse)

    # SELECT 画面（suite -> base task -> カテゴリ -> 難易度 -> variant）と
    # PLAY 画面（PAUSE で開始、PLAY/PAUSE の 2 状態）を行き来する。
    # 保存後は同じタスクのREADYへ。破棄でSELECTへ戻る。
    cache = {}
    pending = ((args.suite, args.task_id)
               if args.task_id is not None else None)
    if takeover is not None:
        pending = (takeover["suite"], takeover["task_id"])
        args.init_index = int(takeover["init_index"] or 0)
    sel_memory = {}   # 決定時の絞り込み状態。捨てて戻ったときはここから再開
    resume = False    # 保存して戻ったときはトップ（suite 選択）から
    try:
        while True:
            if pending is not None:
                suite_name, task_id = pending
                pending = None
            else:
                sel = select_screen(ctl, cache, args.suite, args.out,
                                    sel_memory, resume, args.init_index)
                if sel is None:
                    break
                suite_name, task_id, selected_init = sel
                if selected_init is not None:
                    args.init_index = selected_init
            args.suite = suite_name  # 次に SELECT へ戻ったときの初期 suite

            task = build_task(suite_name, task_id)
            init_states = task["init_states"]
            init_index = args.init_index
            if not 0 <= init_index < len(init_states):
                print(f"[init] index {init_index} は範囲外なので 0 を使う")
                init_index = 0
            print(f"[task] {task['suite']} #{task['task_id']}  {task['name']}")
            print(f"[lang] {task['language']}")
            print(f"[init] {len(init_states)} states / index {init_index}")
            env = make_task_env(task, args.control_freq,
                           args.max_steps + 100)
            if takeover is not None:
                obs = prepare_takeover_env(env, takeover)
                if args.takeover_replay:
                    err = takeover_check(env, takeover)
                    print(f"[takeover] replay from step {takeover['k']}: max state error {err:.3g}")
                    if takeover["branch_exact"] and err > 1e-9:
                        print("[takeover] 録画の残りを再現できない（環境か録画が違う）。中止", file=sys.stderr)
                        env.close()
                        return 1
                    obs = takeover_reset(env, takeover, takeover["k"])
            else:
                obs = reset_to(env, init_states[init_index])

            reason, actions, states, success, worst, init_index = play_episode(
                env, task, obs, ctl, args, cameras, small_cams,
                init_states, init_index, takeover=takeover)

            # esc で明示的に捨てたとき以外は保存する。長い操作を事故で失う
            # ほうが高くつく。
            if reason != "discard" and actions:
                try:
                    states.append(env.get_sim_state())
                except Exception:
                    # 最終状態が取れなくても、action と直前までの state は残す
                    states = states[:len(actions)]
                meta = {k: task[k] for k in ("suite", "task_id", "name",
                                             "language", "bddl")}
                meta.update(init_index=init_index, device=args.device,
                            control_freq=args.control_freq,
                            time_scale=args.time_scale,
                            camera=args.camera, horizon=args.max_steps + 100,
                            exit_reason=reason,
                            max_disturb_m=round(worst[1], 6),
                            max_disturb_object=worst[0])
                if args.auto_align_start:
                    meta["start_alignment"] = args.start_alignment_result
                if takeover is not None:
                    meta["takeover"] = takeover_meta(takeover)
                destination = recording_destination(args.out, task["suite"], task["task_id"], task.get("name"), reason)
                path = save_recording(destination, meta, actions, states, success,
                                      model_xml=recording_model_xml(env))
                print(f"[save] {path}  steps={len(actions)} "
                      f"success={success} reason={reason}")
            else:
                print(f"[discard] 保存しなかった（reason={reason} "
                      f"steps={len(actions)} success={success}）")
            env.close()
            if reason in ("interrupt", "error"):
                break
            if takeover is not None:
                if reason == "discard":
                    break   # 引き継ぎでは SELECT へ戻らず終了
                pending = (suite_name, task_id)
                print(f"[ready] 同じ録画の step {takeover['k']} から次のプレイを準備します")
                if isinstance(ctl, GamepadDrive):
                    ctl.capture_input()
                continue
            if reason in ("save", "selected") and actions:
                pending = (suite_name, task_id)
                args.init_index = init_index
                if suite_name in INIT_MENU_SUITES and success and worst[1] <= DISTURB_THRESHOLD_M:
                    args.init_index = (init_index + 1) % len(init_states)
                if isinstance(ctl, GamepadDrive):
                    ctl.capture_input()
                print(f"[ready] 同じタスク・INIT={args.init_index}で次のプレイを準備します")
            # READYのB/escで前回の選択位置へ戻れる。
            resume = reason == "discard"
    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
        if isinstance(ctl, GamepadDrive):
            ctl._pg.quit()

    if isinstance(ctl, GamepadDrive):
        if ctl.map_str():
            print(f"[map] 次からはこれを渡す: --pad-flip {ctl.map_str()}")
    elif isinstance(ctl, MouseDrag) and ctl.map_str() != MOUSE_MAP:
        print(f"[map] 次からはこれを渡す: --mouse-map {ctl.map_str()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
