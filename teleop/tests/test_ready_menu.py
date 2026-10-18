import unittest
from pathlib import Path

from interaction import EpisodeInteraction, recording_destination, selected_task_dir

class ReadyMenu(unittest.TestCase):
    def test_choose_alignment_then_selection_without_play(self):
        ui = EpisodeInteraction()
        self.assertEqual(ui.visible_menu(True), 0)
        self.assertIsNone(ui.gamepad_command(False, {12}))
        self.assertEqual(ui.gamepad_command(False, {0}), 'toggle_alignment')
        self.assertTrue(ui.stopped)
        self.assertIsNone(ui.gamepad_command(False, {12}))
        self.assertIsNone(ui.gamepad_command(False, {0}))
        self.assertTrue(ui.confirming_exit)
        self.assertEqual(ui.cursor, 1)      # Yes is the default.
        self.assertEqual(ui.gamepad_command(False, {0}), 'discard')
        self.assertTrue(ui.stopped)

    def test_default_start_and_cursor_reset(self):
        ui = EpisodeInteraction()
        self.assertEqual(ui.gamepad_command(False, {0}), 'countdown')
        ui.cursor = 2
        ui.enter('ready')
        self.assertEqual(ui.cursor, 0)
        self.assertEqual(ui.gamepad_command(False, {0,2}), 'countdown')

    def test_x_resumes_from_save_and_exit_confirmation(self):
        for saving in (False, True):
            ui = EpisodeInteraction(phase='pause', saving=saving)
            self.assertEqual(ui.gamepad_command(True, {2}), 'play')
        for phase, op in [('ready', 'countdown'), ('pause', 'play')]:
            ui = EpisodeInteraction(phase=phase, confirming_exit=True)
            self.assertEqual(ui.gamepad_command(False, {2}), op)

    def test_no_and_b_preserve_paused_trajectory(self):
        for button in (0, 1):
            ui = EpisodeInteraction(phase='pause', cursor=2)
            ui.gamepad_command(False, {0})
            self.assertTrue(ui.confirming_exit)
            ui.gamepad_command(False, {11})   # Yes is the default; step up to No.
            self.assertIsNone(ui.gamepad_command(False, {button}))
            self.assertFalse(ui.confirming_exit)
            self.assertEqual(ui.cursor, 2)
            self.assertEqual(ui.phase, 'pause')


class RecordingDestinationTest(unittest.TestCase):
    def test_selected_is_split_per_task(self):
        out = Path("/r")
        self.assertEqual(recording_destination(out, "libero_spatial", 7, "any_task", "selected"),
                         out / "libero_spatial" / "selected" / "t7")

    def test_normal_uses_same_rule(self):
        out = Path("/r")
        self.assertEqual(recording_destination(out, "libero_spatial", 7, "any_task", "save"),
                         out / "libero_spatial" / "normal" / "t7")
        self.assertEqual(selected_task_dir("libero_90", 3, "KITCHEN_SCENE1_xxx"), "t3")
