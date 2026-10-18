#!/usr/bin/env python3
"""teleop で録った軌道を mp4 に焼く。

`teleop_libero.py` は画像を保存せず MuJoCo の `states` だけを残す。再生は
決定論的（同じ state から同じ絵になる）なので、**操作したときと同じ映像を
後から任意のカメラ・任意の解像度で作れる。** 録画のために操作中の負荷を
上げる必要がない。

    cd <repo>/teleop/scripts

    MAGICK_HOME=/opt/homebrew/opt/imagemagick \
    DYLD_LIBRARY_PATH=/opt/homebrew/opt/imagemagick/lib \
    <repo>/.venv/bin/python \
        render_recording.py ../recordings/<name>.npz

カメラを並べたいときは `--camera frontview,robot0_eye_in_hand`。
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

from teleop_libero import build_task, make_task_env  # noqa: E402  同じ回避策を通す


def load_recording(npz_path: Path):
    meta_path = npz_path.with_suffix(".json")
    if not meta_path.exists():
        raise SystemExit(f"メタ情報が無い: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    data = np.load(npz_path)
    return meta, data["actions"], data["states"]


def frame_at(env, state, cameras, size, check_goal=False):
    """1 つの state から、指定カメラを横に並べた 1 コマを作る。

    `check_goal` を立てると、その state で BDDL のゴール述語が成立しているかも返す。
    state を復元してから評価するので、記録時と同じ判定になる。
    """
    env.sim.set_state_from_flattened(state)
    env.sim.forward()
    reached = bool(env.check_success()) if check_goal else False
    tiles = []
    for cam in cameras:
        im = env.sim.render(camera_name=cam, height=size, width=size)
        tiles.append(np.flip(im, axis=0)[..., ::-1])
    return np.ascontiguousarray(np.concatenate(tiles, axis=1)), reached


def open_writer(out: Path, fps: int, size, codec: str) -> cv2.VideoWriter:
    """H.264 で開き、駄目なら MPEG-4 Part 2 へ落とす。

    同じ 240 コマ・320x320 で `avc1` 0.18 MB / `mp4v` 0.76 MB / GIF 10.2 MB。
    GIF はフレーム間圧縮を持たないので、背景が固定のこの映像では特に不利になる。
    再生互換の面でも H.264 のほうが素直。
    """
    for tag in ([codec] if codec != "auto" else ["avc1", "mp4v"]):
        writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*tag), fps, size)
        if writer.isOpened():
            if tag != "avc1":
                print(f"[warn] avc1 が使えないので {tag} で書く")
            return writer
    raise SystemExit(f"VideoWriter を開けなかった: {out}")


def write_sheet(tiles, labels, out: Path, cols: int) -> None:
    """コマを格子状に並べた 1 枚の PNG（コンタクトシート）にする。"""
    rows = (len(tiles) + cols - 1) // cols
    th, tw = tiles[0].shape[:2]
    sheet = np.zeros((rows * th, cols * tw, 3), dtype=np.uint8)
    for i, (tile, label) in enumerate(zip(tiles, labels)):
        r, c = divmod(i, cols)
        cv2.putText(tile, label, (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (255, 255, 255), 1, cv2.LINE_AA)
        sheet[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = tile
    cv2.imwrite(str(out), sheet)


def write_gif(frames, out: Path, fps: int) -> None:
    from PIL import Image
    images = [Image.fromarray(f[..., ::-1]) for f in frames]  # BGR -> RGB
    images[0].save(out, save_all=True, append_images=images[1:],
                   duration=max(20, int(1000 / fps)), loop=0, optimize=True)


GREEN = (80, 230, 80)  # BGR


def draw_overlay(frame, text_lines, goal_step=None):
    overlay = frame.copy()
    h = 16 * len(text_lines) + 8
    cv2.rectangle(overlay, (0, 0), (frame.shape[1], h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.5, frame, 0.5, 0, frame)
    for i, text in enumerate(text_lines):
        color = GREEN if text.startswith("GOAL") else (255, 255, 255)
        cv2.putText(frame, text, (6, 16 + i * 16), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, color, 1, cv2.LINE_AA)
    if goal_step is not None:
        # 達成後は緑の枠を出す。文字を読まなくても一目で分かるように。
        cv2.rectangle(frame, (0, 0), (frame.shape[1] - 1, frame.shape[0] - 1),
                      GREEN, 3)
    return frame


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("npz", type=Path, help="teleop_libero.py が書いた .npz")
    p.add_argument("--camera", default="frontview",
                   help="カンマ区切りで複数指定すると横に並べる")
    p.add_argument("--size", type=int, default=512, help="1 カメラあたりの辺の長さ")
    p.add_argument("--fps", type=int, default=0,
                   help="0 なら記録時の control_freq をそのまま使う")
    p.add_argument("--out", type=Path, default=None, help="既定は npz と同じ場所の .mp4")
    p.add_argument("--no-overlay", action="store_true", help="文字を焼き込まない")
    p.add_argument("--format", choices=["mp4", "gif", "png", "sheet"], default="mp4",
                   help="png は連番ディレクトリ、sheet は格子状に並べた 1 枚")
    p.add_argument("--start", type=int, default=0, help="開始 step")
    p.add_argument("--end", type=int, default=None, help="終了 step（含まない）")
    p.add_argument("--every", type=int, default=1, help="何 step ごとに 1 コマ取るか")
    p.add_argument("--sheet-cols", type=int, default=6, help="sheet の列数")
    p.add_argument("--no-goal", action="store_true",
                   help="ゴール判定を評価しない（少し速くなる）")
    p.add_argument("--codec", default="auto",
                   help="mp4 の codec。既定は avc1（H.264）で、駄目なら mp4v へ落とす")
    args = p.parse_args()

    meta, actions, states = load_recording(args.npz)
    cameras = [c.strip() for c in args.camera.split(",") if c.strip()]
    fps = args.fps or int(meta.get("control_freq", 20))
    end = len(states) if args.end is None else min(args.end, len(states))
    indices = list(range(args.start, end, max(1, args.every)))
    if not indices:
        raise SystemExit(f"範囲が空: start={args.start} end={end}")

    suffix = {"mp4": ".mp4", "gif": ".gif", "sheet": ".png", "png": ""}[args.format]
    out = args.out or args.npz.with_suffix(suffix)

    print(f"[in ] {args.npz.name}  steps={len(actions)} "
          f"success={meta.get('success')} exit={meta.get('exit_reason')}")
    print(f"[env] {meta['suite']} #{meta['task_id']}  {meta['language']}")
    print(f"[sel] step {args.start}..{end} を {args.every} 刻み = {len(indices)} コマ")

    task = build_task(meta["suite"], meta["task_id"])
    env = make_task_env(task, int(meta.get("control_freq", 20)), len(states) + 100)
    env.reset()
    with np.load(args.npz, allow_pickle=False) as archive:
        if "model_xml" in archive:
            env.reset_from_xml_string(str(archive["model_xml"]))

    if args.format == "png":
        out.mkdir(parents=True, exist_ok=True)

    writer, collected, labels = None, [], []
    goal_step = None
    try:
        for n, i in enumerate(indices):
            frame, reached = frame_at(env, states[i], cameras, args.size,
                                      check_goal=not args.no_goal)
            if reached and goal_step is None:
                goal_step = i
                print(f"  [goal] step {i} でゴール成立")
            if not args.no_overlay:
                a = actions[i] if i < len(actions) else np.zeros(7)
                lines = [
                    f"step {i}/{len(actions)}  {meta['language'][:60]}",
                    f"dpos={np.round(a[0:3], 3)} drot={np.round(a[3:6], 3)} "
                    f"grip={'CLOSE' if a[6] > 0 else 'open'}",
                ]
                if not args.no_goal:
                    lines.append(f"GOAL REACHED at step {goal_step}"
                                 if goal_step is not None else "goal: not yet")
                draw_overlay(frame, lines, goal_step)

            if args.format == "mp4":
                if writer is None:
                    h, w = frame.shape[:2]
                    writer = open_writer(out, fps, (w, h), args.codec)
                writer.write(frame)
            elif args.format == "png":
                cv2.imwrite(str(out / f"step_{i:06d}.png"), frame)
            else:  # gif / sheet はメモリに溜める
                collected.append(frame)
                labels.append(f"step {i}")

            if n % 200 == 0 and n:
                print(f"  {n}/{len(indices)}")
    finally:
        if writer is not None:
            writer.release()
        env.close()

    if args.format == "gif":
        write_gif(collected, out, fps)
    elif args.format == "sheet":
        write_sheet(collected, labels, out, args.sheet_cols)

    if args.format == "png":
        total = sum(f.stat().st_size for f in out.glob("*.png")) / 1e6
        print(f"[out] {out}/  {len(indices)} PNG  {total:.1f} MB")
    else:
        size_mb = out.stat().st_size / 1e6 if out.exists() else 0.0
        print(f"[out] {out}  {len(indices)} frames "
              f"{'@ %dfps ' % fps if args.format != 'sheet' else ''}{size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
