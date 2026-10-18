#!/usr/bin/env python3
"""render_frames.py が描いた観測を、LeRobot v3.0 のデータセットに書き出す。

② データセット化の後半。Python 3.12 + LeRobot 0.6.0 の `.venv-lerobot` で動かす。
形式は `lerobot/libero_plus` に合わせる（notebook の「5. データセットを確かめる」がこれを前提にする）。

| key | 型 | 中身 |
| :--- | :--- | :--- |
| observation.images.front | video 256×256×3（AV1） | agentview を 180° 回転 |
| observation.images.wrist | video 256×256×3（AV1） | robot0_eye_in_hand を 180° 回転 |
| observation.state | float32 (8,) | eef 位置 3 + 軸角 3 + グリッパ指 2 |
| action | float32 (7,) | teleop の actions そのまま |
| task | str | 記録 JSON の language |

来歴（元の npz の相対パスと sha256、選んだ条件、描画の設定、ソフトの版）は
`meta/teleop_provenance.json` に書く。データセットと一緒に Hugging Face へ上がる。

    .venv-lerobot/bin/python dataset/scripts/build_dataset.py --repo-id <hf_user>/<name>
"""

import argparse
import json
import os
import shutil
import sys
import time
from importlib.metadata import version
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
FPS = 20
ROBOT_TYPE = "panda"
CAMERA_PX = 256
FEATURES = {
    "observation.images.front": {"dtype": "video", "shape": (CAMERA_PX, CAMERA_PX, 3),
                                 "names": ["height", "width", "channel"]},
    "observation.images.wrist": {"dtype": "video", "shape": (CAMERA_PX, CAMERA_PX, 3),
                                 "names": ["height", "width", "channel"]},
    "observation.state": {"dtype": "float32", "shape": (8,), "names": [f"state_{i}" for i in range(8)]},
    "action": {"dtype": "float32", "shape": (7,), "names": [f"action_{i}" for i in range(7)]},
}
PROVENANCE = "meta/teleop_provenance.json"


def load_frames(path: Path) -> tuple[dict, dict]:
    with np.load(path, allow_pickle=False) as z:
        header = json.loads(str(z["header"]))
        arrays = {key: z[key.replace(".", "__")] for key in FEATURES}
    return arrays, header


def check_arrays(arrays: dict, length: int, name: str) -> None:
    for key, spec in FEATURES.items():
        a = arrays[key]
        want_dtype = np.uint8 if spec["dtype"] == "video" else np.float32
        if a.shape != (length, *spec["shape"]) or a.dtype != want_dtype:
            raise ValueError(f"{name}: {key} が {a.shape} {a.dtype}（期待 {(length, *spec['shape'])} {want_dtype}）")
        if a.dtype == np.float32 and not np.isfinite(a).all():
            raise ValueError(f"{name}: {key} に NaN / inf がある")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo-id", required=True, help="Hugging Face の dataset repo（<user>/<name>）")
    p.add_argument("--work", type=Path, default=REPO / "dataset/build",
                   help="render_frames.py の出力（manifest.json と frames/）")
    p.add_argument("--root", type=Path, default=None,
                   help="データセットを書く場所。既定は <work>/lerobot/<repo-id>")
    p.add_argument("--overwrite", action="store_true", help="同じ場所にある前回のデータセットを消して作り直す")
    p.add_argument("--image-writer-threads", type=int, default=4)
    args = p.parse_args()

    manifest_path = args.work / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"{manifest_path} が無い。先に render_frames.py を実行する")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    episodes = manifest["episodes"]
    if not episodes:
        raise SystemExit("manifest に episode が無い")
    if manifest["render"]["camera_px"] != CAMERA_PX:
        raise SystemExit(f"camera_px={manifest['render']['camera_px']}（lerobot/libero_plus は {CAMERA_PX}）")

    root = args.root or args.work / "lerobot" / args.repo_id
    if root.exists():
        if not args.overwrite:
            raise SystemExit(f"{root} が既にある。作り直すなら --overwrite")
        if not (root / "meta/info.json").exists():
            raise SystemExit(f"{root} は LeRobot のデータセットに見えないので消さない")
        shutil.rmtree(root)

    os.environ.setdefault("SVT_LOG", "1")  # AV1 エンコーダ（SVT-AV1）の設定表示を episode ごとに出さない
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    dataset = LeRobotDataset.create(repo_id=args.repo_id, fps=FPS, features=FEATURES, root=root,
                                    robot_type=ROBOT_TYPE, use_videos=True,
                                    image_writer_threads=args.image_writer_threads)
    provenance = []
    t_all = time.time()
    try:
        for i, ep in enumerate(episodes):
            t0 = time.time()
            arrays, header = load_frames(args.work / ep["frames"])
            if header["source_sha256"] != ep["source_sha256"]:
                raise ValueError(f"{ep['frames']} は {ep['source_npz']} の描画ではない（sha256 が違う）")
            check_arrays(arrays, ep["length"], ep["source_npz"])
            for t in range(ep["length"]):
                dataset.add_frame({**{key: arrays[key][t] for key in FEATURES}, "task": ep["task"]})
            dataset.save_episode()
            provenance.append({"episode_index": i, **{k: v for k, v in ep.items() if k != "frames"}})
            print(f"[{i + 1}/{len(episodes)}] episode {i}: {ep['length']} frames  {time.time() - t0:.1f}s  "
                  f"{ep['source_npz']}", flush=True)
    finally:
        dataset.finalize()

    (root / PROVENANCE).write_text(json.dumps({
        "description": "teleop の記録（MuJoCo の state 列）から描き直したデータセットの来歴。"
                       "episode_index ごとに元の npz（記録の置き場からの相対パス）と sha256 を持つ",
        "built_with": {"lerobot": version("lerobot"), "python": sys.version.split()[0],
                       "script": "dataset/scripts/build_dataset.py"},
        **{k: manifest[k] for k in ("created_at", "recordings", "selection", "render", "software")},
        "episodes": provenance,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    frames = sum(e["length"] for e in episodes)
    tasks = sorted({e["task"] for e in episodes})
    print(f"[done] {args.repo_id}: {len(episodes)} episodes / {frames} frames / {len(tasks)} tasks  "
          f"{time.time() - t_all:.0f}s")
    print(f"       {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
