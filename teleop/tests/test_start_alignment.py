"""Geometry, stopping bounds, and the recorded episode integration."""
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import numpy as np
import cv2
from test_input_phase1 import app, episode, frames
from start_alignment import StartAlignment


def environment(rotation):
    controller = SimpleNamespace(
        eef_name='eef', ee_pos=np.array([.1, .2, .3]),
        use_delta=True, use_ori=True, impedance_mode='fixed',
        input_max=np.ones(6), input_min=-np.ones(6),
        output_max=np.array([.05]*3 + [.5]*3),
        output_min=-np.array([.05]*3 + [.5]*3))
    return SimpleNamespace(robots=[SimpleNamespace(controller=controller)],
        sim=SimpleNamespace(model=SimpleNamespace(body_name2id=lambda _: 0, site_name2id=lambda _: 0),
                            data=SimpleNamespace(body_xmat=np.array([rotation]), site_xpos=[controller.ee_pos])))


class AlignmentGeometry(unittest.TestCase):
    def test_centered_skips_without_step(self):
        a = StartAlignment(True, 20)
        self.assertIsNone(a.action(environment(a.TARGET), -1))
        self.assertEqual(a.status, 'skipped')
        self.assertEqual(a.steps, 0)

    def test_all_axes_and_half_turn_converge_with_bounded_actions(self):
        for vector in ([.4, 0, 0], [0, -.4, 0], [0, 0, 3.14159265], [.4, -.6, .8]):
            a = StartAlignment(True, 20)
            env = environment(cv2.Rodrigues(np.array(vector, float))[0] @ a.TARGET)
            for _ in range(201):
                action = a.action(env, -1)
                if action is None:
                    break
                self.assertLessEqual(np.linalg.norm(action[3:6]),
                                     StartAlignment.MAX_ROTATION_STEP + 1e-6)
                self.assertEqual(action[6], -1)
                # Ideal world-frame OSC step; frame-local Euler subtraction fails
                # these cases, especially the 180-degree yaw boundary.
                env.sim.data.body_xmat[0] = cv2.Rodrigues(action[3:6]*.5)[0] @ env.sim.data.body_xmat[0]
            self.assertEqual(a.status, 'done')
            np.testing.assert_allclose(env.sim.data.body_xmat[0], a.TARGET, atol=.009)

    def test_holds_initial_position_and_times_out_if_blocked(self):
        a = StartAlignment(True, 20)
        env = environment(cv2.Rodrigues(np.array([.4, 0, 0]))[0] @ a.TARGET)
        a.action(env, -1)
        env.robots[0].controller.ee_pos += .01
        action = a.action(env, -1)
        self.assertTrue(np.all(action[:3] < 0))
        self.assertLessEqual(np.linalg.norm(action[:3]),
                             StartAlignment.MAX_POSITION_STEP + 1e-6)
        for _ in range(200):
            a.action(env, -1)
        self.assertEqual((a.status, a.steps), ('timeout', 200))

    def test_xyz_passthrough_and_release_holds_new_position(self):
        a = StartAlignment(True, 20)
        env = environment(cv2.Rodrigues(np.array([.4, 0, 0]))[0] @ a.TARGET)
        xyz = np.array([.4, -.3, .6])
        np.testing.assert_array_equal(a.action(env, -1, xyz)[:3], xyz)
        env.robots[0].controller.ee_pos += [.02, -.01, .03]
        np.testing.assert_array_equal(a.action(env, -1, np.zeros(3))[:3], np.zeros(3))
        env.robots[0].controller.ee_pos += .001
        self.assertTrue(np.all(a.action(env, -1)[:3] < 0))

    def test_translation_does_not_prevent_alignment_completion(self):
        a = StartAlignment(True, 20)
        env = environment(cv2.Rodrigues(np.array([.4, 0, 0]))[0] @ a.TARGET)
        a.action(env, -1, [.5, 0, 0])
        env.sim.data.body_xmat[0] = a.TARGET
        for _ in range(3):
            env.robots[0].controller.ee_pos += .01
            a.action(env, -1, [.5, 0, 0])
        self.assertEqual(a.status, 'done')

    def test_cancel_and_off_never_access_environment(self):
        for a in (StartAlignment(False, 20), StartAlignment(True, 20)):
            a.cancel()
            self.assertIsNone(a.action(None, -1))


class StubAlignment(StartAlignment):
    instances = []
    finish_after = 3
    finish_status = "done"

    def __init__(self, *args):
        super().__init__(*args)
        self.instances.append(self)

    def action(self, env, gripper, manual_xyz=None):
        if self.steps >= self.finish_after:
            self.status = self.finish_status
            return None
        self.status = 'running'
        self.steps += 1
        return np.r_[manual_xyz if manual_xyz is not None else np.zeros(3), .12, 0., 0., gripper]


class AlignmentEpisode(unittest.TestCase):
    def setUp(self):
        StubAlignment.instances = []
        self.mock = patch.object(app, 'StartAlignment', StubAlignment)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_auto_actions_recorded_then_manual_with_live_gripper(self):
        with patch.object(StubAlignment, 'finish_after', 3):
            result, env, panels, _, ctl = episode(frames({0}) + [{3, 8}]*20, auto_align=True)
        a = StubAlignment.instances[0]
        self.assertEqual((a.status, a.steps), ('done', 3))
        self.assertEqual(len(result[1]), len(result[2]))
        self.assertEqual(len(result[1]), len(env.executed))
        self.assertTrue(all(action[3] == .12 for action in result[1][:3]))
        self.assertTrue(all(action[3] == 0 for action in result[1][3:]))
        self.assertEqual(ctl.gripper, 1)
        self.assertIn('REC', [p[0] for p in panels])
        self.assertNotIn('ALIGN', [p[0] for p in panels])

    def test_x_cancels_alignment_resume_is_manual(self):
        with patch.object(StubAlignment, 'finish_after', 200):
            result, env, panels, _, _ = episode(frames({0}, {2}, {2}), auto_align=True)
        a = StubAlignment.instances[0]
        self.assertEqual(a.status, 'cancelled')
        self.assertGreater(len(result[1]), a.steps)
        self.assertTrue(all(action[3] == 0 for action in result[1][a.steps:]))

    def test_retry_rearms_and_no_motion_before_start(self):
        with patch.object(StubAlignment, 'finish_after', 200):
            _, env, panels, _, _ = episode(frames({0}, {2}, {12}, {0}), auto_align=True)
        self.assertEqual(len(StubAlignment.instances), 2)
        self.assertEqual(StubAlignment.instances[-1].status, 'pending')
        self.assertEqual(panels[-1][0], 'READY')
        self.assertEqual(len(env.resets), 1)

    def test_disturbance_stops_after_first_recorded_step(self):
        result, env, panels, _, _ = episode(frames({0}) + [set()]*12, auto_align=True, disturbance=('cup', .002))
        self.assertEqual(StubAlignment.instances[0].status, 'disturbed')
        self.assertEqual(len(result[1]), 1)
        self.assertEqual(panels[-1][0], 'PAUSE')

    def test_timeout_enters_pause_without_extra_physics(self):
        with patch.object(StubAlignment, 'finish_after', 1), patch.object(StubAlignment, 'finish_status', 'timeout'):
            result, _, panels, _, _ = episode(frames({0}) + [set()]*12, auto_align=True)
        self.assertEqual(len(result[1]), 1)
        self.assertEqual(panels[-1][0], 'PAUSE')

    def test_countdown_cancel_does_not_run_alignment(self):
        _, env, panels, _, _ = episode([set()]*3 + [{0}] + [set()] + [{1}]*10, auto_align=True)
        self.assertFalse(env.executed)
        self.assertEqual(StubAlignment.instances[0].status, 'pending')
        self.assertEqual(panels[-1][0], 'READY')

    def test_ready_menu_toggle_on_then_off_hold_only_once(self):
        _, env, _, _, ctl = episode(frames({12}) + [{0}]*12 + [set()]*3 + [{0}]*12)
        self.assertEqual([a.status for a in StubAlignment.instances], ['off', 'pending', 'off'])
        self.assertFalse(env.executed)
        self.assertEqual(ctl.gripper, -1)

    def test_ready_menu_enables_alignment_and_retry_preserves_setting(self):
        with patch.object(StubAlignment, 'finish_after', 200):
            _, env, panels, _, _ = episode(frames({12}, {0}, {2}, {2}, {12}, {0}))
        self.assertGreater(StubAlignment.instances[1].steps, 0)
        self.assertEqual(StubAlignment.instances[-1].status, 'pending')
        self.assertEqual(len(env.resets), 1)
        self.assertEqual(panels[-1][0], 'READY')

    def test_b_back_never_toggles_alignment(self):
        from interaction import EpisodeInteraction
        ui = EpisodeInteraction()
        self.assertIsNone(ui.gamepad_command(False, {1}))
        self.assertTrue(ui.confirming_exit)
        self.assertEqual(ui.gamepad_command(False, {2}), 'countdown')
        ui.enter('pause')
        self.assertEqual(ui.gamepad_command(False, {1}), 'play')
        ui.enter('play')
        self.assertIsNone(ui.gamepad_command(False, {1}))
        ui.start_countdown(100)
        self.assertEqual(ui.gamepad_command(False, {1}, 101), 'ready')

    def test_xyz_pad_input_recorded_without_cancelling_alignment(self):
        from test_input_phase1 import Drive
        ctl = Drive()
        with patch.object(ctl, 'action', return_value=np.array([.3, -.2, .4, 0., 0., 0., -1.])):
            result, env, _, _, _ = episode(frames({0}) + [set()]*15, auto_align=True, ctl=ctl)
        self.assertGreater(len(env.executed), 3)
        for action in result[1][:3]:
            np.testing.assert_array_equal(action[:3], [.3, -.2, .4])
            np.testing.assert_array_equal(action[3:6], [.12, 0, 0])
        self.assertEqual(len(result[1]), len(result[2]))

    def test_alignment_gripper_close_open_and_toggle_are_recorded(self):
        with patch.object(StubAlignment, 'finish_after', 200):
            result, _, _, _, ctl = episode(frames({0}, {3}, {1}, {8}), auto_align=True)
        grips = [a[6] for a in result[1]]
        changes = [g for i, g in enumerate(grips) if i == 0 or g != grips[i-1]]
        self.assertEqual(changes, [-1, 1, -1, 1])
        self.assertEqual(ctl.gripper, 1)
        self.assertTrue(all(a[3] == .12 for a in result[1]))
        self.assertEqual(len(result[1]), len(result[2]))

    def test_each_rotation_axis_cancels_alignment_and_records_manual_input(self):
        from test_input_phase1 import Drive
        for axis in (3, 4, 5):
            StubAlignment.instances = []
            ctl = Drive()
            action = np.zeros(7)
            action[axis] = .3
            action[6] = -1
            with patch.object(ctl, 'action', return_value=action):
                result, _, _, _, _ = episode(frames({0}) + [set()]*10, auto_align=True, ctl=ctl)
            self.assertEqual(StubAlignment.instances[0].status, 'manual_rotation')
            self.assertEqual(StubAlignment.instances[0].steps, 0)
            self.assertGreater(len(result[1]), 0)
            np.testing.assert_array_equal(result[1][0], action)

    def test_shoulder_cancels_running_alignment_without_pause_or_restart(self):
        with patch.object(StubAlignment, 'finish_after', 200):
            result, _, panels, _, _ = episode(frames({0}, {10}) + [set()]*10, auto_align=True)
        a = StubAlignment.instances[0]
        self.assertGreater(a.steps, 0)
        self.assertEqual(a.status, 'manual_rotation')
        self.assertNotIn('PAUSE', [p[0] for p in panels])
        self.assertTrue(any(v[3] < 0 for v in result[1][a.steps:]))
        self.assertTrue(all(v[3] == 0 for v in result[1][-3:]))
