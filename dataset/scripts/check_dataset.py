#!/usr/bin/env python3
"""書き出したデータセットを読み戻し、元の記録・描画結果と突き合わせる。

`.venv-lerobot`（Python 3.12 + LeRobot 0.6.0）で動かす。確かめること:

1. 形式: notebook の「5. データセットを確かめる」のセルを**そのまま**このデータセットに対して実行する
   （`LeRobotDatasetMetadata` の features・fps の検査と episode の一覧）。加えて robot_type・codec・
   feature の名前が `lerobot/libero_plus` と同じか
2. action: 元の npz の `actions` と完全に一致するか
3. state: render_frames.py が state から作った 8 次元と完全に一致するか
4. 画像: 動画を復号した絵が描画結果に近いか（AV1 の非可逆圧縮ぶんの差だけか）、
   180° 回した絵より明らかに近いか（向きの取り違えがないか）
5. 来歴: `meta/teleop_provenance.json` の sha256 が元の npz と一致するか

    .venv-lerobot/bin/python dataset/scripts/check_dataset.py --repo-id <hf_user>/<name>
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "notebooks/smolvla_libero_finetune.ipynb"
VIDEO_KEYS = ("observation.images.front", "observation.images.wrist")
# AV1（crf 30）の復号誤差の上限。実測は 1 前後（0〜255 の平均絶対誤差）
MAX_IMAGE_MAE = 6.0


def fail(message: str):
    raise SystemExit(f"[NG] {message}")


def recordings_root(text: str, override: Path | None) -> Path:
    if override is not None:
        return override
    if text.startswith("~/"):
        return Path.home() / text[2:]
    path = Path(text)
    return path if path.is_absolute() else REPO / path


def run_notebook_cell(repo_id: str, root: Path) -> None:
    """notebook の設定セルとデータセットのセルを、このデータセットを指すようにして実行する。"""
    import lerobot.datasets.lerobot_dataset as lerobot_dataset

    cells = ["".join(c["source"]) for c in json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
             if c["cell_type"] == "code"]
    settings = next(c for c in cells if "DATASET_REPO = " in c)
    dataset_cell = next(c for c in cells if "LeRobotDatasetMetadata(DATASET_REPO" in c)
    original = lerobot_dataset.LeRobotDatasetMetadata

    def local_metadata(repo, revision=None, **kwargs):  # Hub ではなく手元の root を読む
        return original(repo, root=root, revision=revision, **kwargs)

    scope = {}
    exec(compile(settings, "notebook: 1. 設定", "exec"), scope)
    scope.update(DATASET_REPO=repo_id, DATASET_REVISION=None, DATASET_EPISODES=None, DATASET_TASK_FILTER=None)
    lerobot_dataset.LeRobotDatasetMetadata = local_metadata
    try:
        exec(compile(dataset_cell, "notebook: 5. データセットを確かめる", "exec"), scope)
    finally:
        lerobot_dataset.LeRobotDatasetMetadata = original
    if scope["TRAIN_EPISODES"] is not None or len(scope["selected"]) != scope["meta"].total_episodes:
        fail("notebook のセルが全 episode を選ばなかった")


def check_format(meta, n_episodes: int) -> None:
    want_names = {"observation.state": [f"state_{i}" for i in range(8)],
                  "action": [f"action_{i}" for i in range(7)]}
    problems = []
    if meta.info.codebase_version != "v3.0":
        problems.append(f"codebase_version={meta.info.codebase_version}")
    if meta.robot_type != "panda":
        problems.append(f"robot_type={meta.robot_type}")
    for key in VIDEO_KEYS:
        info = meta.features[key].get("info", {})
        if meta.features[key]["dtype"] != "video" or info.get("video.codec") != "av1" \
                or info.get("video.pix_fmt") != "yuv420p":
            problems.append(f"{key}: {meta.features[key]['dtype']} {info.get('video.codec')} {info.get('video.pix_fmt')}")
    for key, names in want_names.items():
        if meta.features[key]["dtype"] != "float32" or meta.features[key]["names"] != names:
            problems.append(f"{key}: {meta.features[key]['dtype']} {meta.features[key]['names']}")
    if meta.total_episodes != n_episodes:
        problems.append(f"total_episodes={meta.total_episodes}（manifest は {n_episodes}）")
    if problems:
        fail("形式が lerobot/libero_plus と違う: " + "; ".join(problems))


def to_hwc_uint8(tensor) -> np.ndarray:
    return (tensor.permute(1, 2, 0).numpy() * 255.0).round().clip(0, 255).astype(np.uint8)


def mae(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))


def write_sheet(rows, path: Path) -> None:
    from PIL import Image

    Image.fromarray(np.concatenate([np.concatenate(r, axis=1) for r in rows], axis=0)).save(path)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo-id", required=True)
    p.add_argument("--work", type=Path, default=REPO / "dataset/build")
    p.add_argument("--root", type=Path, default=None, help="既定は <work>/lerobot/<repo-id>")
    p.add_argument("--recordings", type=Path, default=None,
                   help="元の記録の置き場。既定は manifest.json に書いた場所")
    p.add_argument("--all-frames", action="store_true",
                   help="全フレームの画像を復号して比べる（既定は各 episode の先頭・中央・末尾）")
    p.add_argument("--sheet", type=Path, default=None,
                   help="episode 0 の先頭・中央・末尾を並べた PNG を書く（向きの目視用）")
    args = p.parse_args()

    root = args.root or args.work / "lerobot" / args.repo_id
    manifest = json.loads((args.work / "manifest.json").read_text(encoding="utf-8"))
    episodes = manifest["episodes"]
    rec_root = recordings_root(manifest["recordings"], args.recordings)

    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 手元のデータセットだけを読む
    from lerobot.datasets.lerobot_dataset import LeRobotDataset, LeRobotDatasetMetadata

    print("[1] notebook の「5. データセットを確かめる」を実行する", flush=True)
    run_notebook_cell(args.repo_id, root)
    meta = LeRobotDatasetMetadata(args.repo_id, root=root)
    check_format(meta, len(episodes))
    print(f"[1] OK  形式は lerobot/libero_plus と同じ（v3.0 / panda / av1 yuv420p / fps {meta.fps}）")

    provenance = json.loads((root / "meta/teleop_provenance.json").read_text(encoding="utf-8"))["episodes"]
    if [e["source_sha256"] for e in provenance] != [e["source_sha256"] for e in episodes]:
        fail("teleop_provenance.json の episode が manifest と一致しない")

    dataset = LeRobotDataset(args.repo_id, root=root)
    data = dataset.hf_dataset.with_format("numpy")
    ep_col, frame_col, task_col = (np.asarray(data[k]) for k in ("episode_index", "frame_index", "task_index"))
    state_col, action_col = np.asarray(data["observation.state"]), np.asarray(data["action"])
    task_by_index = {int(i): t for t, i in zip(dataset.meta.tasks.index, dataset.meta.tasks["task_index"])}

    worst = {"image_mae": 0.0, "rotated_margin": np.inf, "frames_checked": 0}
    sheet_rows = []
    for i, ep in enumerate(episodes):
        with np.load(args.work / ep["frames"], allow_pickle=False) as z:
            frames = {key: z[key.replace(".", "__")] for key in (*VIDEO_KEYS, "observation.state", "action")}
        source = rec_root / ep["source_npz"]
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        if sha != ep["source_sha256"] or provenance[i]["source_sha256"] != sha:
            fail(f"episode {i}: {source} の sha256 が来歴と違う")
        with np.load(source, allow_pickle=False) as z:
            source_actions = np.asarray(z["actions"], dtype=np.float32)

        rows = np.flatnonzero(ep_col == i)
        n = ep["length"]
        if len(rows) != n or not np.array_equal(frame_col[rows], np.arange(n)):
            fail(f"episode {i}: frame が {len(rows)} 個（期待 {n}）か順番が違う")
        if not np.array_equal(action_col[rows], source_actions):
            fail(f"episode {i}: action が元の npz と一致しない")
        if not np.array_equal(state_col[rows], frames["observation.state"]):
            fail(f"episode {i}: state が描画時の値と一致しない")
        if {task_by_index[int(t)] for t in task_col[rows]} != {ep["task"]}:
            fail(f"episode {i}: task が記録の language と違う")

        ends = sorted({0, n // 2, n - 1})
        picks = range(n) if args.all_frames else ends
        for t in picks:
            item = dataset[int(rows[t])]
            if abs(float(item["timestamp"]) - t / dataset.fps) > 1e-4:
                fail(f"episode {i} frame {t}: timestamp={float(item['timestamp'])}")
            for key in VIDEO_KEYS:
                decoded = to_hwc_uint8(item[key])
                expected = frames[key][t]
                err, err_rot = mae(decoded, expected), mae(decoded, np.flip(expected, axis=(0, 1)))
                worst["image_mae"] = max(worst["image_mae"], err)
                worst["rotated_margin"] = min(worst["rotated_margin"], err_rot - err)
                if err > MAX_IMAGE_MAE:
                    fail(f"episode {i} frame {t} {key}: 復号した絵と描画結果の差が大きい（MAE {err:.2f}）")
                if err_rot <= err:
                    fail(f"episode {i} frame {t} {key}: 180° 回した絵のほうが近い（向きの取り違え）")
            worst["frames_checked"] += 1
            if i == 0 and args.sheet is not None and t in ends:
                sheet_rows.append([to_hwc_uint8(item[key]) for key in VIDEO_KEYS])
        print(f"[2-5] episode {i}: {n} frames OK  {ep['source_npz']}", flush=True)

    print(f"[2] OK  action は元の npz と完全一致（{len(action_col)} frames）")
    print("[3] OK  state は描画時の値と完全一致")
    print(f"[4] OK  画像 {worst['frames_checked']} frames × 2 カメラ: 描画結果との MAE 最大 {worst['image_mae']:.2f}、"
          f"180° 回した絵との差は最小でも +{worst['rotated_margin']:.1f}")
    print("[5] OK  来歴の sha256 は元の npz と一致")
    if args.sheet is not None:
        write_sheet(sheet_rows, args.sheet)
        print(f"[sheet] {args.sheet}（左 front・右 wrist、上から先頭・中央・末尾）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
