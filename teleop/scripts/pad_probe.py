"""ゲームパッドの軸・ボタンをターミナルにライブ表示する。Ctrl+C で終了。

teleop を通さずに、接続確認・軸番号・ボタン番号を調べるための道具。
実測済みの配置は plans/archive/gamepad_teleop.md に記録してある。
"""
import os
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

pygame.init()
pygame.joystick.init()
if pygame.joystick.get_count() == 0:
    raise SystemExit("コントローラが見つかりません。システム設定 → Bluetooth を確認。")

j = pygame.joystick.Joystick(0)
j.init()
print(f"接続: {j.get_name()}  軸 {j.get_numaxes()} / ボタン {j.get_numbuttons()}")
print("スティックやボタンを操作してください。Ctrl+C で終了。\n")

try:
    while True:
        pygame.event.pump()
        axes = "  ".join(f"a{i}:{j.get_axis(i):+.2f}" for i in range(j.get_numaxes()))
        pressed = [str(i) for i in range(j.get_numbuttons()) if j.get_button(i)]
        print(f"\r{axes}  btn:[{','.join(pressed):12s}]", end="", flush=True)
        time.sleep(0.05)
except KeyboardInterrupt:
    print("\n終了")
