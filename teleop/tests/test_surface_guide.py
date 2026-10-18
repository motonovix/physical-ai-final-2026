"""surface_guide の表示専用レイ問い合わせ。小さな MJCF で内側判定を確かめる。"""
import unittest
from types import SimpleNamespace

import mujoco
import numpy as np

from surface_guide import enclosing_top, surface_below

XML = """
<mujoco>
  <worldbody>
    <geom name="table" type="box" size="1 1 0.02" pos="0 0 0.98"/>
    <body name="bottle" pos="0.2 0 1.10">
      <geom type="cylinder" size="0.03 0.10"/>
    </body>
    <body name="gripper0_hand" pos="0.2 0 1.15">
      <geom type="box" size="0.01 0.01 0.01"/>
    </body>
  </worldbody>
</mujoco>
"""


def _sim():
    model = mujoco.MjModel.from_xml_string(XML)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    return SimpleNamespace(model=model, data=data)


class EnclosingTopTest(unittest.TestCase):
    def test_inside_bottle_returns_its_top(self):
        sim = _sim()
        top = enclosing_top(sim, [0.2, 0.0, 1.12])      # ボトル（z 1.00〜1.20）の中
        self.assertIsNotNone(top)
        self.assertAlmostEqual(top, 1.20, places=4)

    def test_outside_returns_none(self):
        sim = _sim()
        self.assertIsNone(enclosing_top(sim, [0.5, 0.0, 1.12]))   # ボトルの横、机の上
        self.assertIsNone(enclosing_top(sim, [0.2, 0.0, 1.30]))   # ボトルの真上（外側）

    def test_surface_below_still_finds_table(self):
        sim = _sim()
        self.assertAlmostEqual(surface_below(_sim(), [0.5, 0.0, 1.5]), 1.00, places=4)
        self.assertAlmostEqual(surface_below(sim, [0.2, 0.0, 1.5]), 1.20, places=4)   # ボトル上面
