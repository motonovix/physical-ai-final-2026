"""レポートの図を作る（figures/ に書き出し、Git に入れる）。

    actions.png       動画の 1 本（ボウルをコンロに載せる）の 6 軸の命令と、同時に動いていた軸の数
    hud.png           画面下の状態表示（INPUT のバー・EEF の値・姿勢ダイヤル）
    speed_time.png    状態表示の SPEED / TIME
    guides.png        正面カメラに重ねた鉛直線などの表示補助（グリッパが机の上と、ボウルの縁の上の 2 コマ）
    controller.png    コントローラの割り当て（動画の操作方法の画面）

元の素材（teleop の記録・画面録画・動画のパーツ）は Git に入れていない。手元で作り直すときだけ要る。
数字（同時に動いた軸の割合など）は標準出力に出すので、本文と食い違っていないか確かめる。
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
FIG = HERE / "figures"
TMP = HERE / "build" / "tmp"
FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"


def frame(src: Path, at: float, out: Path, crop: str | None = None) -> Image.Image:
    vf = ["-vf", f"crop={crop}"] if crop else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at}", "-i", str(src), "-frames:v", "1", *vf, str(out)],
                   check=True)
    return Image.open(out).convert("RGB")


def actions_figure(npz: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    font_manager.fontManager.addfont(FONT)
    plt.rcParams["font.family"] = font_manager.FontProperties(fname=FONT).get_name()
    plt.rcParams["font.size"] = 9
    a = np.load(npz, allow_pickle=True)["actions"]
    t = np.arange(len(a)) / 20
    moving = np.abs(a[:, :6]) > 1e-3
    n = moving.sum(1)
    fig, axes = plt.subplots(2, 1, figsize=(6.4, 3.0), sharex=True, gridspec_kw={"height_ratios": [2.6, 1]})
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    for i, name in enumerate(["x", "y", "z", "roll", "pitch", "yaw"]):
        axes[0].plot(t, a[:, i], lw=1.4, color=colors[i], ls="-" if i < 3 else "--", label=name)
    axes[0].set_ylabel("命令（−1〜1）")
    axes[0].set_ylim(-1.15, 1.15)
    axes[0].legend(ncol=6, fontsize=7.5, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
    axes[0].grid(alpha=0.3)
    axes[1].bar(t, n, width=0.05, color=["#d62728" if k >= 2 else "#aaaaaa" for k in n])
    axes[1].set_ylabel("同時に\n動く軸")
    axes[1].set_yticks([0, 2, 4])
    axes[1].set_xlabel("記録上の時間（秒、20 Hz）")
    fig.tight_layout(pad=0.4)
    fig.savefig(FIG / "actions.png", dpi=200)
    plt.close(fig)
    m = n > 0
    print(f"actions.png: {len(a)} steps = {len(a) / 20:.1f} s, moving {m.sum()} steps, "
          f">=2 axes {np.sum(n >= 2)} ({np.mean(n[m] >= 2):.0%} of moving), "
          f">=3 axes {np.sum(n >= 3)} ({np.mean(n[m] >= 3):.0%}), max {n.max()} axes, "
          f"median |a| {np.median(np.abs(a[:, :6][moving])):.2f}")


def screen_figures(mov: Path, at: float, window: str) -> None:
    shot = frame(mov, at, TMP / "teleop_screen_full.png", window)
    w, h = shot.size
    # 状態帯（下 20%）の左側：INPUT のバー・EEF の値・姿勢ダイヤル
    strip_top = int(h * 0.80)
    shot.crop((0, strip_top, int(w * 0.62), h)).save(FIG / "hud.png")
    # 同じ帯の SPEED / TIME のタイル
    shot.crop((int(w * 0.635), strip_top + int(h * 0.04), int(w * 0.725), h)).save(FIG / "speed_time.png")
    print(f"hud.png / speed_time.png: {mov.name} の {at} 秒")


def guides_figure(mov: Path, times: list[float], window: str) -> None:
    """正面カメラ（窓の左の 1024 px 角）から、グリッパと対象物のまわりを切り出して横に並べる。"""
    ww, wh, wx, wy = map(int, window.split(":"))
    title_bar = 56                                   # 窓の上の題名の帯
    box = (290, 330, 850, 890)                       # 正面カメラの中で切り出す範囲（px）
    gap, size = 16, box[2] - box[0]
    out = Image.new("RGB", (len(times) * size + (len(times) - 1) * gap, size), (255, 255, 255))
    label_font = ImageFont.truetype(FONT.replace("W3", "W6"), 34)
    for i, at in enumerate(times):
        shot = frame(mov, at, TMP / f"guides_{i}.png", f"1024:1024:{wx}:{wy + title_bar}")
        panel = shot.crop(box)
        d = ImageDraw.Draw(panel)
        d.rounded_rectangle((12, 12, 70, 62), radius=8, fill=(255, 255, 255))
        d.text((41, 37), f"({'abcd'[i]})", font=label_font, fill=(20, 20, 20), anchor="mm")
        out.paste(panel, (i * (size + gap), 0))
    out.save(FIG / "guides.png")
    print(f"guides.png: {mov.name} の {', '.join(map(str, times))} 秒")


def controller_figure(part: Path, at: float) -> None:
    shot = frame(part, at, TMP / "controller_full.png")
    shot.crop((0, 150, 1920, 1010)).resize((1440, 645), Image.LANCZOS).save(FIG / "controller.png")
    print(f"controller.png: {part.name} の {at} 秒")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--npz", type=Path, default=None, help="teleop の記録（既定は t1147 の最新）")
    ap.add_argument("--mov", type=Path, default=ROOT / "report/video/assets/teleop/b2.mov", help="画面録画")
    ap.add_argument("--at", type=float, default=9.0, help="画面録画から取るコマの秒")
    ap.add_argument("--window", default="2560:1376:112:76", help="画面録画の窓の範囲（w:h:x:y）")
    ap.add_argument("--guides-at", type=float, nargs="+", default=[5.0, 6.0], help="表示補助の図に使うコマの秒")
    ap.add_argument("--controller", type=Path, default=ROOT / "report/video/build/parts/controller.mp4")
    args = ap.parse_args()
    FIG.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    npz = args.npz or sorted((ROOT / "teleop/recordings/libero_goal/normal/t1147").glob("*.npz"))[-1]
    actions_figure(npz)
    screen_figures(args.mov, args.at, args.window)
    guides_figure(args.mov, args.guides_at, args.window)
    controller_figure(args.controller, 7.5)


if __name__ == "__main__":
    main()
