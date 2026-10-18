"""Characterize production input paths without SDL windows or a physics instance.

The fake pad refreshes its snapshot only on pump(), as SDL does. waitKey changes
physical inputs; event latency therefore remains visible instead of being hidden
by making every getter return the newest physical state.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import teleop_libero as app


class Pad:
    def __init__(self):
        self.physical = set()
        self.sample = set()
        self.pumps = 0

    def pump(self):
        self.pumps += 1
        self.sample = set(self.physical)

    def get_button(self, number):
        return int(number in self.sample)

    def get_axis(self, number):
        return 1.0 if number + 100 in self.sample else 0.0

    def get_numbuttons(self):
        return 15

    def get_numaxes(self):
        return 6


class Drive(app.GamepadDrive):
    def __init__(self):
        app.KeyboardDrive.__init__(self, mode='gamepad')
        self.pad = Pad()
        self._pg = SimpleNamespace(event=SimpleNamespace(pump=self.pad.pump))
        self.signs = dict.fromkeys(self.AXIS_NAMES, 1.0)
        self.yaw_deadzone = app.PAD_YAW_DEADZONE
        self.z_gain = app.PAD_Z_GAIN
        self.xy_gain = app.PAD_XY_GAIN
        self._btn_prev = {}


class Environment:
    def __init__(self):
        self.executed = []
        self.resets = []

    def get_sim_state(self):
        return app.np.array([len(self.executed)])

    def step(self, action):
        self.executed.append(action.copy())
        return {}, 0, False, {}

    def check_success(self):
        return False


def frames(*presses):
    """Hold each supplied set for three UI frames, then release for three."""
    return [set()] * 3 + [frame for press in presses
                          for frame in [set(press)] * 3 + [set()] * 3]


def episode(inputs, debug=True, suite='libero_spatial', key=-1, ctl=None, disturbance=None, auto_align=False):
    ctl = ctl or Drive()
    env = Environment()
    panels = []
    schedule = iter(inputs)
    clock = [100.0]
    out = io.StringIO()
    args = SimpleNamespace(control_freq=20, time_scale=0, max_steps=1000, window_size=100, auto_align_start=auto_align)

    def wait(_):
        try:
            ctl.pad.physical = next(schedule)
            clock[0] += 1.0
        except StopIteration:
            raise KeyboardInterrupt
        return key() if callable(key) else key

    def panel(frame, rpy, info):
        panels.append((info['rec'], info['step'], ctl.gripper))
        return frame

    def reset(env, state):
        env.resets.append(state)
        return {}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(app.time, "monotonic", side_effect=lambda: clock[0]))
        stack.enter_context(patch.dict(os.environ, TELEOP_DEBUG_INPUT='1' if debug else '0'))
        stack.enter_context(contextlib.redirect_stdout(out))
        for name in ('render', 'eef_rpy', 'draw_hud', 'draw_pause_menu', 'draw_ready_menu', 'draw_start_countdown', 'disturb_baseline'):
            stack.enter_context(patch.object(app, name, return_value=None))
        stack.enter_context(patch.object(app, 'draw_panel', side_effect=panel))
        stack.enter_context(patch.object(app, 'reset_to', side_effect=reset))
        stack.enter_context(patch.object(app, 'update_disturb', side_effect=lambda obs, base, worst: disturbance if disturbance is not None else worst))
        stack.enter_context(patch.object(app.cv2, 'imshow'))
        stack.enter_context(patch.object(app.cv2, 'waitKey', side_effect=wait))
        result = app.play_episode(env, dict(suite=suite, task_id=0, language='test'), {}, ctl,
                                  args, [], [], [0, 1, 2], 0)
    logs = [json.loads(line[len('[input] '):]) for line in out.getvalue().splitlines()
            if line.startswith('[input] ')]
    return result, env, panels, logs, ctl


class InputPaths(unittest.TestCase):
    def test_ready_y_b_disabled(self):
        result, env, panels, logs, ctl = episode(frames({3}, {1}, {3, 1}))
        self.assertEqual(result[0], 'interrupt')
        self.assertFalse(env.executed or env.resets)
        self.assertEqual({p[0] for p in panels}, {'READY'})
        self.assertEqual(ctl.gripper, app.GRIPPER_OPEN)

    def test_select_confirm_held_into_ready(self):
        for button in (0, 3):
            ctl = Drive()
            ctl.capture_input()
            ctl.pad.physical = {button}
            self.assertTrue(ctl.select_nav()[2])
            result, env, panels, logs, ctl = episode([{button}] * 12, ctl=ctl)
            self.assertFalse(env.resets or env.executed)
            self.assertEqual(ctl.gripper, app.GRIPPER_OPEN)

    def test_select_aliases_and_back_priority(self):
        for buttons, expected in (({0}, (True, False)), ({3}, (True, False)),
                                  ({1}, (False, True)), ({2}, (False, True)),
                                  ({0, 2}, (False, True))):
            ctl = Drive()
            ctl.capture_input()
            ctl.pad.physical = buttons
            self.assertEqual(ctl.select_nav()[2:], expected)

    def test_ready_a_start_x_pause_and_resume_once(self):
        result, env, panels, logs, _ = episode(frames({0}, {2}, {2}))
        transitions = [e['operation'] for e in logs if e.get('operation') in ('play', 'pause')]
        self.assertEqual(transitions, ['play', 'pause', 'play'])
        self.assertTrue(env.executed)
        self.assertEqual(len(result[1]), len(result[2]))
        paused = [p[1] for p in panels if p[0] == 'PAUSE']
        self.assertEqual(len(set(paused)), 1)

    def test_pause_retry_a_hold_returns_ready_once(self):
        result, env, panels, logs, _ = episode(frames({0}, {2}, {12}, {0})[:-3] + [{0}] * 12)
        self.assertEqual(len(env.resets), 1)
        self.assertEqual(panels[-1][0], 'READY')
        self.assertEqual(len(result[1]), 0)

    def test_pause_y_cannot_save_or_resume(self):
        result, env, panels, logs, _ = episode(frames({0}, {2}, {3}))
        self.assertEqual(result[0], 'interrupt')
        self.assertEqual(panels[-1][0], 'PAUSE')
        self.assertFalse(any(e.get('operation') in ('save', 'selected') for e in logs))

    def test_ready_x_starts(self):
        result, env, panels, logs, _ = episode(frames({2}))
        self.assertEqual(result[0], 'interrupt')
        self.assertTrue(env.executed)

    def test_pause_task_selection_discards(self):
        result, env, panels, logs, _ = episode(frames({0}, {2}, {12}, {12}, {0}, {0}))
        self.assertEqual(result[0], 'discard')
        self.assertTrue(env.executed)

    def test_ready_init_has_explicit_retry_reason(self):
        with patch.object(app, 'INIT_MENU_SUITES', ('custom',)):
            result, env, panels, logs, _ = episode(frames({14}), suite='custom')
        self.assertEqual(env.resets, [1])
        self.assertEqual([e['source'] for e in logs if e.get('source') == 'ready_init'], ['ready_init'])
        self.assertFalse(env.executed)

    def test_keyboard_cannot_operate_gamepad_ready(self):
        for key in (32, ord('p'), ord('0'), 13, 27, ord('g'), ord('=')):
            with self.subTest(key=key):
                result, env, panels, logs, ctl = episode([set()] * 6, key=key)
                self.assertEqual(result[0], 'interrupt')
                self.assertFalse(env.executed or env.resets)
                self.assertEqual(ctl.gripper, app.GRIPPER_OPEN)
                self.assertEqual(ctl.scale, app.DEFAULT_SPEED)

    def test_diagnostics_do_not_change_input_behavior(self):
        seq = frames({0}, {3}, {1}, {2}, {12}, {0})
        off = episode(seq, debug=False)
        on = episode(seq, debug=True)
        self.assertFalse(off[3])
        self.assertEqual(off[0][0], on[0][0])
        self.assertEqual(off[2], on[2])
        app.np.testing.assert_array_equal(off[1].executed, on[1].executed)
        self.assertEqual(off[1].resets, on[1].resets)
        self.assertEqual(off[4].pad.pumps, on[4].pad.pumps)

    def test_play_short_y_during_pacer_wait_is_retained(self):
        class WaitingPacer:
            def __init__(self, *args):
                self.calls = 0
            def reset(self):
                pass
            def ready(self):
                self.calls += 1
                return self.calls >= 20
        with patch.object(app, 'StepPacer', WaitingPacer):
            result, env, panels, logs, ctl = episode(frames({0}, {3}) + [set()] * 30)
        self.assertTrue(env.executed)
        self.assertTrue(any(action[6] == app.GRIPPER_CLOSE for action in env.executed))

    def test_debug_exit_confirmation(self):
        ctl = Drive()
        with patch.dict(os.environ, TELEOP_DEBUG_INPUT='1'), contextlib.redirect_stdout(io.StringIO()), \
             patch.object(app, 'draw_select'), patch.object(app.cv2, 'imshow'), \
             patch.object(app.cv2, 'waitKeyEx', side_effect=lambda _: ctl.pad.physical.add(1) or -1):
            self.assertFalse(app.confirm_exit(ctl))

    def test_success_auto_pause_and_save_both_destinations(self):
        for normal in (False, True):
            seq = frames({0}) + [set()] * 30 + frames({0})
            seq += frames(*([{12}] if normal else []), {0})
            with patch.object(Environment, 'check_success', return_value=True):
                result, env, panels, logs, _ = episode(seq)
            self.assertEqual(result[0], 'save' if normal else 'selected')
            self.assertTrue(result[3])
            self.assertEqual(len(env.executed), 1 + app.SUCCESS_PAUSE_STEPS)
            self.assertEqual(len(result[1]), len(result[2]))

    def test_success_collision_defaults_to_retry_at_warning_threshold(self):
        for distance, cursor in ((0.0, 1), (.001, 1), (.001001, 2)):
            with self.subTest(distance=distance), patch.object(Environment, 'check_success', return_value=True):
                result, env, panels, logs, _ = episode(frames({0}) + [set()] * 30,
                                                       disturbance=('obstacle', distance))
                paused = [e for e in logs if e['event'] == 'observe' and e['phase'] == 'pause']
                self.assertEqual(paused[0]['menu'], cursor)
                self.assertEqual(len(env.executed), 1 + app.SUCCESS_PAUSE_STEPS)
        with patch.object(Environment, 'check_success', return_value=True):
            result, env, panels, _, _ = episode(frames({0}) + [set()] * 30 + frames({0}),
                                               disturbance=('obstacle', .002))
        self.assertEqual(result[0], 'interrupt')
        self.assertEqual(env.resets, [0])
        self.assertEqual(panels[-1][0], 'READY')
        self.assertEqual(len(result[1]), 0)

    def test_shoulders_match_dpad_without_doubling(self):
        for button, trigger, dpad in ((9, None, 13), (10, None, 14),
                                       (None, 4, 12), (None, 5, 11)):
            outputs = []
            for combined in (False, True):
                ctl = Drive()
                ctl.pad.physical = ({button} if button is not None else set()) | ({dpad} if combined else set())
                with patch.object(ctl.pad, 'get_axis', side_effect=lambda i: 1.0 if i == trigger else 0.0):
                    ctl.capture_input()
                    outputs.append(ctl.action())
            ref = Drive()
            ref.pad.physical = {dpad}
            ref.capture_input()
            expected = ref.action()
            if trigger == 5:
                # ZR だけ pitch を PAD_ZR_PITCH_GAIN 倍に緩める（2026-09-18）。十字キー上と同時なら十字キーどおり
                app.np.testing.assert_array_equal(outputs[1], expected)
                expected[4] *= app.PAD_ZR_PITCH_GAIN
                app.np.testing.assert_allclose(outputs[0], expected)
                continue
            app.np.testing.assert_array_equal(outputs[0], expected)
            app.np.testing.assert_array_equal(outputs[0], outputs[1])

    def test_ready_speed_and_play_shoulders_are_separate(self):
        _, _, _, _, ctl = episode(frames({10}, {0}, {10}, {9}))
        self.assertAlmostEqual(ctl.scale, app.DEFAULT_SPEED + 0.1)

    def test_pause_shoulders_change_settings_without_stepping(self):
        result, env, panels, logs, ctl = episode(frames({0}, {2}, {10}, {105}, {104}, {9}))
        self.assertEqual(panels[-1][0], 'PAUSE')
        self.assertEqual(len({p[1] for p in panels if p[0] == 'PAUSE'}), 1)
        self.assertAlmostEqual(ctl.scale, app.DEFAULT_SPEED)
        self.assertEqual(len([e for e in logs if e.get('operation') == 'speed']), 2)
        self.assertEqual(len([e for e in logs if e.get('operation') == 'time_scale']), 2)

    def test_save_submenu_b_back_keeps_pause_and_save_selection(self):
        seq = frames({0}) + [set()] * 30 + frames({0}, {1})[:-3] + [{1}] * 12
        with patch.object(Environment, 'check_success', return_value=True):
            result, env, panels, logs, _ = episode(seq)
        self.assertEqual(result[0], 'interrupt')
        self.assertEqual(panels[-1][0], 'PAUSE')
        self.assertEqual(len(env.executed), 1 + app.SUCCESS_PAUSE_STEPS)
        observed = [e for e in logs if e['event'] == 'observe']
        self.assertEqual(observed[-1]['menu'], 1)

    def test_right_stick_diagonal_controls_height_and_yaw(self):
        ctl = Drive()
        for value in (-.5, .5):
            with patch.object(ctl.pad, 'get_axis', side_effect=lambda i: value if i in (2, 3) else 0.0):
                ctl.capture_input()
                action = ctl.action()
            self.assertLess(action[2] * value, 0)
            self.assertLess(action[5] * value, 0)
        with patch.object(ctl.pad, 'get_axis', return_value=.05):
            ctl.capture_input()
            action = ctl.action()
        self.assertEqual(action[2], 0)
        self.assertEqual(action[5], 0)

    def test_keyboard_and_mouse_pause_resume_and_save(self):
        for ctl in (app.KeyboardDrive(mode='latch'), app.MouseDrag()):
            ctl.pad = Pad()  # Only supplies the test clock's neutral physical input.
            keys = iter([ord('p'), ord('g'), -1, ord('p'), ord('g'), -1,
                         ord('p'), -1, ord('p'), 13])
            result, env, panels, logs, _ = episode([set()] * 10, ctl=ctl, key=lambda: next(keys))
            self.assertEqual(result[0], 'save')
            self.assertTrue(env.executed)
            self.assertTrue(all(action[6] == app.GRIPPER_CLOSE for action in env.executed))
            self.assertEqual(len(result[1]), len(result[2]))

    def test_state_transition_clears_menus(self):
        from interaction import EpisodeInteraction
        state = EpisodeInteraction()
        self.assertTrue(state.stopped)
        for automatic, collided, cursor in ((False, False, 0), (True, False, 1), (True, True, 2)):
            state.enter('pause', automatic=automatic, collided=collided)
            self.assertEqual(state.visible_menu(True), cursor)
            state.saving = True
            state.keyboard_menu = 1
            state.enter('play')
            self.assertFalse(state.stopped)
            self.assertIsNone(state.visible_menu(True))
            self.assertIsNone(state.keyboard_menu)
            self.assertFalse(state.saving)
        state.enter('ready')
        self.assertTrue(state.stopped)
        with self.assertRaises(ValueError):
            state.enter('invalid')

    def test_countdown_ticks_and_cancel_priority(self):
        from interaction import COUNTDOWN_STEP, COUNTDOWN_TICKS, EpisodeInteraction
        ui = EpisodeInteraction()
        ui.start_countdown(10.0)
        deadline = 10.0 + COUNTDOWN_STEP * COUNTDOWN_TICKS
        for tick in range(COUNTDOWN_TICKS):
            now = 10.0 + COUNTDOWN_STEP * tick
            self.assertTrue(ui.stopped)
            self.assertEqual(ui.countdown_remaining(now), COUNTDOWN_TICKS - tick)
            self.assertIsNone(ui.gamepad_command(False, {0, 2, 3}, now))
        self.assertIsNone(ui.gamepad_command(False, set(), deadline - 0.001))
        self.assertEqual(ui.gamepad_command(False, set(), deadline), 'play')
        self.assertEqual(ui.gamepad_command(False, {1}, deadline), 'ready')
        ui.enter('ready')
        self.assertIsNone(ui.countdown_deadline)

    def test_countdown_cancel_never_steps_or_records(self):
        seq = [set()] * 3 + [{0}, set(), {1}] + [set()] * 6
        result, env, panels, logs, _ = episode(seq)
        self.assertFalse(env.executed or env.resets)
        self.assertFalse(result[1] or result[2])
        self.assertEqual(panels[-1][0], 'READY')
        self.assertTrue(any(e.get('phase') == 'countdown' for e in logs))

    def test_countdown_held_gripper_does_not_fire_on_start(self):
        result, env, panels, logs, _ = episode([set()] * 3 + [{0}] + [{3}] * 9)
        commands = [e for e in logs if e['event'] == 'command']
        start = next(e['time'] for e in commands if e['operation'] == 'countdown')
        play = next(e['time'] for e in commands if e['operation'] == 'play')
        self.assertGreaterEqual(play - start, 3.0)
        self.assertTrue(env.executed)
        self.assertTrue(all(a[6] == app.GRIPPER_OPEN for a in env.executed))
        countdown = [e for e in logs if e['event'] == 'observe' and e['phase'] == 'countdown']
        self.assertTrue(all(e['step'] == 0 and e['gripper'] == app.GRIPPER_OPEN for e in countdown))

    def test_ready_pause_l_decreases_speed_zr_increases_time(self):
        for prefix in ([], [{0}, {2}]):
            _, _, _, logs, ctl = episode(frames(*prefix, {104}, {9}, {105}))
            settings = [e for e in logs if e.get('operation') in ('speed', 'time_scale')]
            self.assertEqual([e['operation'] for e in settings], ['time_scale', 'speed', 'time_scale'])
            self.assertAlmostEqual(ctl.scale, app.DEFAULT_SPEED - 0.1)
            self.assertEqual(settings[-1]['value'], 0.0)  # ZL: unlimited -> 1.0; ZR: back to unlimited.

    def test_r3_toggle_once_per_press_and_explicit_buttons_win(self):
        result, env, panels, logs, _ = episode(frames({0}, {8}, {8}, {8, 3}, {8, 1}))
        grip = [e for e in logs if e.get('operation') == 'gripper']
        self.assertEqual([e['source'] for e in grip], ['R3', 'R3', 'Y', 'B'])
        self.assertEqual([e['value'] for e in grip], [1.0, -1.0, 1.0, -1.0])

    def test_r3_disabled_when_stopped_and_hold_does_not_cross_start(self):
        for seq in (frames({8}, {0}, {2}, {8}),
                    [set()] * 3 + [{0}] + [{8}] * 9):
            result, env, panels, logs, _ = episode(seq)
            self.assertFalse(any(e.get('operation') == 'gripper' for e in logs))
            self.assertTrue(all(a[6] == app.GRIPPER_OPEN for a in env.executed))

    def test_menu_contents_and_commands(self):
        from interaction import episode_command, pause_items
        self.assertEqual(len(pause_items(False)), 3)
        self.assertEqual(len(pause_items(True)), 4)
        self.assertEqual(episode_command('pause', False, 0, False, {0})[0], 'play')
        self.assertEqual(episode_command('pause', True, 1, False, {0}), (None, 0, True))
        self.assertEqual(episode_command('pause', True, 0, True, {0})[0], 'selected')
        self.assertEqual(episode_command('pause', True, 1, True, {0})[0], 'save')
        self.assertEqual(episode_command('pause', True, 1, True, {0, 2}), ('play', 0, False))
        for success in (False, True):
            for button in (3,):
                self.assertIsNone(episode_command('pause', success, 0, False, {button})[0])


if __name__ == '__main__':
    unittest.main()
