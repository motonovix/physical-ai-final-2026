"""Align the hand to the HUD's zero using recorded OSC delta actions."""
import cv2
import numpy as np


class StartAlignment:
    TARGET = np.diag([1., -1., -1.])  # HUD roll=0 means physical roll=pi.
    ANGLE_TOL = np.deg2rad(.5)
    POSITION_TOL = .001
    # Per-step caps on the proportional command, as a fraction of the OSC action
    # range. They set how fast the alignment runs while the error is still large;
    # the approach near the target stays proportional either way. Doubled from
    # .15 / .12 on 2026-09-13 at the user's request to shorten the wait.
    MAX_POSITION_STEP = .30
    MAX_ROTATION_STEP = .24

    def __init__(self, enabled, control_freq):
        self.status = 'pending' if enabled else 'off'
        self.steps = 0
        self.limit = int(control_freq * 10)
        self.target_pos = None
        self.settled = 0
        self.was_moving = False

    @property
    def active(self):
        return self.status in ('pending', 'running')

    def cancel(self, reason='cancelled'):
        if self.active:
            self.status = reason

    def summary(self):
        return dict(status=self.status, steps=self.steps, target_hud_degrees=[0, 0, 0])

    def action(self, env, gripper, manual_xyz=None):
        """None means no automatic step; caller handles completion / timeout.

        World-frame rotation increments apply equally to hand body and controller
        site, even though their local frames differ. Pass manual XYZ through at
        normal speed; when released, hold the new position instead of returning
        to the starting point. Never edit INIT or controller settings.
        """
        if not self.active:
            return None
        controller = env.robots[0].controller
        site = env.sim.model.site_name2id(controller.eef_name)
        position = np.array(env.sim.data.site_xpos[site])
        if self.target_pos is None:
            if not (controller.use_delta and controller.use_ori
                    and controller.impedance_mode == 'fixed'
                    and np.allclose(controller.input_max, 1)
                    and np.allclose(controller.input_min, -1)
                    and np.allclose(controller.output_min, -controller.output_max)):
                raise ValueError('Start alignment requires symmetric delta OSC_POSE')
            self.target_pos = position.copy()
        bid = env.sim.model.body_name2id('robot0_right_hand')
        rotation = np.array(env.sim.data.body_xmat[bid]).reshape(3, 3)
        error = cv2.Rodrigues(self.TARGET @ rotation.T)[0].ravel()
        xyz = np.zeros(3) if manual_xyz is None else np.asarray(manual_xyz)
        moving = bool(np.any(xyz != 0))
        if moving or self.was_moving:
            self.target_pos = position.copy()
        self.was_moving = moving
        position_error = self.target_pos - position
        centered = (np.linalg.norm(error) <= self.ANGLE_TOL
                    and np.linalg.norm(position_error) <= self.POSITION_TOL)
        self.settled = self.settled + 1 if centered else 0
        if centered and (self.steps == 0 or self.settled >= 3):
            self.status = 'skipped' if self.steps == 0 else 'done'
            return None
        if self.steps >= self.limit:
            self.status = 'timeout'
            return None
        delta = np.r_[position_error, error] / controller.output_max
        # Bound vector lengths, so diagonal motion is no faster than one axis.
        for part, maximum in ((slice(0, 3), self.MAX_POSITION_STEP),
                              (slice(3, 6), self.MAX_ROTATION_STEP)):
            norm = np.linalg.norm(delta[part])
            if norm > maximum:
                delta[part] *= maximum / norm
        if moving:
            delta[:3] = xyz
        self.status = 'running'
        self.steps += 1
        return np.r_[delta, gripper]
