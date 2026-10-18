#!/usr/bin/env python3
"""teleop の記録から、学習に使う観測（2 カメラの画像と 8 次元の状態）を描き直す。

② データセット化の前半。teleop と同じ Python 3.10 の `.venv` で動かす（LIBERO-plus・robosuite・
MuJoCo が要る）。LeRobot 形式への書き出しは後半の `build_dataset.py`（Python 3.12 + LeRobot 0.6）が
受け持つ。LeRobot 0.6 は numpy 2 を要し、teleop のシミュレータは numpy 1.26 に固定してあるので、
1 つの環境にはまとめられない。間は episode ごとの npz と manifest.json で受け渡す。

teleop の記録は画像を持たず、各 action を入れる**直前**の MuJoCo の state と最後の state を持つ
（`states` は action より 1 つ多い）。state から観測を作り直すのは決定論的なので、
操作したときと同じ場面を学習用の解像度・カメラで描ける。

観測は `lerobot/libero_plus` と同じ約束にそろえる（notebook の評価もこの約束で観測を作る）。

- `observation.images.front` = `agentview`、`observation.images.wrist` = `robot0_eye_in_hand`。
  どちらも 256×256 で、env の生画像を 180° 回転（上下・左右とも反転）したもの
- `observation.state` = [eef 位置 3, eef 姿勢（クォータニオン → 軸角）3, グリッパ指の位置 2]
- `action` = 記録の action そのまま。`task` = 記録 JSON の `language`

Sensor Noise の variant は、env が `step()` / `reset()` の観測に掛けるのと同じノイズを
`agentview` に掛ける（`regenerate_obs_from_state` は掛けないため）。評価で方策が見る絵と
そろえるため。乱数の seed は記録の sha256 から決めるので、何度描いても同じ絵になる。

    cd <repo>
    MAGICK_HOME=/opt/homebrew/opt/imagemagick \\
    DYLD_LIBRARY_PATH=/opt/homebrew/opt/imagemagick/lib \\
    .venv/bin/python dataset/scripts/render_frames.py --kind selected --suite libero_spatial

既定では success かつ max_disturb_m <= 1 mm（teleop の「クリア」と同じ基準）の記録だけを選ぶ。
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "teleop/scripts"))

import numpy as np  # noqa: E402

# teleop と同じ回避策（LIBERO_PLUS_ROOT を sys.path へ、torch.load の weights_only）を通す
from teleop_libero import DISTURB_THRESHOLD_M, LIBERO_SRC, build_task, check_assets  # noqa: E402
from sim_runtime import require_local_sim  # noqa: E402
from libero.libero import get_libero_path  # noqa: E402
from libero.libero.envs import OffScreenRenderEnv  # noqa: E402

CAMERAS = {"observation.images.front": "agentview", "observation.images.wrist": "robot0_eye_in_hand"}
CAMERA_PX = 256
CONTROL_FREQ = 20
SEED = 42  # teleop の takeover と同じ。固定物の位置が記録の XML と一致する
STATE_TOLERANCE = 1e-12
# 描き方を変えたら上げる。frames/ の古いキャッシュを使わないようにする
RENDER_VERSION = 1
KINDS = ("selected", "normal")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def display_path(path: Path) -> str:
    """来歴に残すパス。リポジトリ内ならリポジトリからの相対、ホーム以下なら ~ にする。"""
    path = path.resolve()
    for base, label in ((REPO, ""), (Path.home(), "~/")):
        try:
            return label + str(path.relative_to(base))
        except ValueError:
            pass
    return str(path)


def parse_ids(text: str | None):
    if not text:
        return None
    ids = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            ids.update(range(int(lo), int(hi) + 1))
        elif part:
            ids.add(int(part))
    return ids


def find_recordings(root: Path, kinds, suites, task_ids):
    """<suite>/<kind>/t<task_id>/*.json を集める。npz が無いものは飛ばす。"""
    found = []
    for meta_path in sorted(root.glob("*/*/t*/*.json")):
        suite, kind, task_dir = meta_path.relative_to(root).parts[:3]
        if kind not in kinds or (suites and suite not in suites):
            continue
        m = re.fullmatch(r"t(\d+)", task_dir)
        if m is None or (task_ids is not None and int(m.group(1)) not in task_ids):
            continue
        if meta_path.with_suffix(".npz").exists():
            found.append((kind, meta_path))
    return found


def rejection(meta, args) -> str | None:
    """学習に使わない理由。使うなら None。"""
    if not args.allow_failed and not meta.get("success"):
        return "success=false"
    disturb = meta.get("max_disturb_m")
    if args.max_disturb_m >= 0:
        if disturb is None:
            return "max_disturb_m が無い（古い記録）"
        if disturb > args.max_disturb_m:
            return f"max_disturb_m={disturb} > {args.max_disturb_m}"
    if meta.get("control_freq", CONTROL_FREQ) != CONTROL_FREQ:
        return f"control_freq={meta.get('control_freq')}"
    return None


def relocate_assets(xml: str) -> tuple[str, int]:
    """記録の XML にある mesh / texture の絶対パスを、この環境の robosuite と LIBERO の assets へ付け替える。

    XML は記録した環境のパス（例: 別の venv の site-packages）を持つので、そのままでは
    venv を作り直したり別の機械で描いたりすると読めない。LIBERO の postprocess_model_xml と同じ考え方。
    """
    import robosuite
    import xml.etree.ElementTree as ET

    robosuite_dir = Path(robosuite.__file__).parent
    libero_assets = Path(get_libero_path("assets"))
    root = ET.fromstring(xml)
    changed, missing = 0, []
    for elem in root.iter():
        old = elem.get("file")
        if not old or not os.path.isabs(old):
            continue
        parts = Path(old).parts
        new = None
        # .../libero/libero/assets/<rest> → この環境の LIBERO の assets/<rest>
        i = next((k for k in range(2, len(parts)) if parts[k - 2:k + 1] == ("libero", "libero", "assets")), None)
        if i is not None:
            new = libero_assets.joinpath(*parts[i + 1:])
        elif "robosuite" in parts:
            i = max(k for k, p in enumerate(parts) if p == "robosuite")
            new = robosuite_dir.joinpath(*parts[i + 1:])
        if new is None:
            new = Path(old)
        if str(new) != old:
            elem.set("file", str(new))
            changed += 1
        if not new.exists():
            missing.append(str(new))
    if missing:
        raise FileNotFoundError(f"XML が参照する assets が見つからない（{len(missing)} 件）: {missing[:3]}")
    return ET.tostring(root, encoding="unicode"), changed


def sensor_noise(image: np.ndarray, level: int) -> np.ndarray:
    """LIBERO-plus の ControlEnv.step() が agentview に掛けるノイズと同じ処理。"""
    from PIL import Image
    from libero.libero.envs import env_wrapper as w

    pil = Image.fromarray(image)
    if level <= 10:
        out = w.motion_blur(pil, severity=level)
    elif level <= 20:
        out = w.gaussian_blur(pil, severity=level - 10)
    elif level <= 30:
        out = w.zoom_blur(pil, severity=level - 20)
    elif level <= 40:
        out = w.fog(pil, severity=level - 30)
    elif level <= 50:
        out = w.glass_blur(pil, severity=level - 40)
    else:
        raise ValueError(f"未知のノイズ水準: {level}")
    return np.asarray(out).astype(np.uint8)


def upright(image: np.ndarray) -> np.ndarray:
    """env の生画像（上下逆さま）を 180° 回す。notebook の評価と同じ。"""
    return np.ascontiguousarray(np.flip(image, axis=(0, 1)))


def state8(obs) -> np.ndarray:
    from robosuite.utils.transform_utils import quat2axisangle

    return np.concatenate([
        np.asarray(obs["robot0_eef_pos"], dtype=np.float64),
        quat2axisangle(np.asarray(obs["robot0_eef_quat"], dtype=np.float64)),
        np.asarray(obs["robot0_gripper_qpos"], dtype=np.float64),
    ]).astype(np.float32)


def render_episode(npz_path: Path, meta: dict, source_sha: str, args):
    """1 本の記録を描き直す。返り値は (配列の dict, 描画の情報)。"""
    with np.load(npz_path, allow_pickle=False) as z:
        actions = np.asarray(z["actions"], dtype=np.float32)
        states = np.asarray(z["states"], dtype=np.float64)
        model_xml = str(z["model_xml"]) if "model_xml" in z.files else None
    n = len(actions)
    if actions.ndim != 2 or actions.shape[1] != 7 or n == 0:
        raise ValueError(f"actions の形が想定外: {actions.shape}")
    # 通常は action + 1（最後の state 付き）。最後の state が取れなかった記録は action と同数
    if len(states) not in (n, n + 1):
        raise ValueError(f"states {len(states)} 行 / actions {n} 行が対応しない")
    if model_xml is None:
        raise ValueError("model_xml が無い（固定物の位置を復元できない）")

    with contextlib.redirect_stdout(io.StringIO()):  # LIBERO-plus が task 番号を数千件並べて表示するのを隠す
        task = build_task(meta["suite"], int(meta["task_id"]))
    if task["name"] != meta.get("name"):
        raise ValueError(f"task 名が記録と違う: {task['name']} != {meta.get('name')}")

    env = OffScreenRenderEnv(
        bddl_file_name=task["bddl"],
        control_freq=CONTROL_FREQ,
        camera_names=list(CAMERAS.values()),
        camera_heights=args.camera_px,
        camera_widths=args.camera_px,
        horizon=n + 1000,
        ignore_done=True,
    )
    try:
        env.seed(SEED)
        env.reset()
        xml, relocated = relocate_assets(model_xml)
        env.reset_from_xml_string(xml)
        if env.get_sim_state().shape != states[0].shape:
            raise ValueError(f"state の次元が違う: env {env.get_sim_state().shape} / 記録 {states[0].shape}")

        noise = int(getattr(env, "noise", 0) or 0) if args.sensor_noise == "env" else 0
        noise_seed = int(source_sha[:8], 16)
        np.random.seed(noise_seed)

        px = args.camera_px
        front = np.empty((n, px, px, 3), dtype=np.uint8)
        wrist = np.empty((n, px, px, 3), dtype=np.uint8)
        state = np.empty((n, 8), dtype=np.float32)
        worst = 0.0
        for t in range(n):
            obs = env.regenerate_obs_from_state(states[t])
            worst = max(worst, float(np.max(np.abs(env.get_sim_state() - states[t]))))
            raw_front = obs[CAMERAS["observation.images.front"] + "_image"]
            if noise:
                raw_front = sensor_noise(raw_front, noise)
            front[t] = upright(raw_front)
            wrist[t] = upright(obs[CAMERAS["observation.images.wrist"] + "_image"])
            state[t] = state8(obs)
        if worst > STATE_TOLERANCE:
            raise ValueError(f"state を復元できない（最大誤差 {worst:.3g}）")
        goal_at_end = None
        if len(states) == n + 1:
            env.regenerate_obs_from_state(states[n])
            goal_at_end = bool(env.check_success())
    finally:
        env.close()

    arrays = {"observation.images.front": front, "observation.images.wrist": wrist,
              "observation.state": state, "action": actions}
    info = {"max_state_error": worst, "sensor_noise": noise,
            "noise_seed": noise_seed if noise else None,
            "relocated_asset_paths": relocated, "goal_at_end": goal_at_end}
    return arrays, info


def frames_name(source_sha: str, args) -> str:
    noise = "noise" if args.sensor_noise == "env" else "clean"
    return f"{source_sha[:16]}_{args.camera_px}px_{noise}_v{RENDER_VERSION}.npz"


def write_frames(path: Path, arrays: dict, header: dict) -> None:
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez_compressed(tmp, header=np.asarray(json.dumps(header, ensure_ascii=False)),
                        **{k.replace(".", "__"): v for k, v in arrays.items()})
    tmp.replace(path)


def read_header(path: Path) -> dict | None:
    try:
        with np.load(path, allow_pickle=False) as z:
            return json.loads(str(z["header"]))
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recordings", type=Path, default=REPO / "teleop/recordings",
                   help="teleop の記録の置き場（<suite>/<selected|normal>/t<task_id>/*.npz）")
    p.add_argument("--kind", choices=[*KINDS, "all"], default="selected",
                   help="selected = 保存メニューで selected を選んだ記録 / normal / all = 両方")
    p.add_argument("--suite", default="", help="suite で絞る（カンマ区切り）")
    p.add_argument("--task", default="", help="task id で絞る（例 0,3,10-12）")
    p.add_argument("--allow-failed", action="store_true", help="success=false の記録も使う")
    p.add_argument("--max-disturb-m", type=float, default=DISTURB_THRESHOLD_M,
                   help="対象外の物体を動かした量の上限（m）。負の値で判定しない")
    p.add_argument("--limit", type=int, default=0, help="先頭から N 本だけ使う（動作確認用）")
    p.add_argument("--camera-px", type=int, default=CAMERA_PX)
    p.add_argument("--sensor-noise", choices=["env", "off"], default="env",
                   help="env = Sensor Noise の variant では評価と同じノイズを掛ける / off = 掛けない")
    p.add_argument("--out", type=Path, default=REPO / "dataset/build",
                   help="frames/ と manifest.json を書く場所")
    args = p.parse_args()

    root = args.recordings.resolve()
    if not root.is_dir():
        raise SystemExit(f"記録の置き場が無い: {root}")
    kinds = KINDS if args.kind == "all" else (args.kind,)
    suites = {s.strip() for s in args.suite.split(",") if s.strip()}
    task_ids = parse_ids(args.task)

    candidates = find_recordings(root, kinds, suites, task_ids)
    chosen, skipped = [], []
    for kind, meta_path in candidates:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        reason = rejection(meta, args)
        rel = str(meta_path.with_suffix(".npz").relative_to(root))
        if reason:
            skipped.append({"source_npz": rel, "reason": reason})
        else:
            chosen.append((kind, meta_path, meta))
    # suite → task → 記録日時の順に並べる（episode_index の順になる）
    chosen.sort(key=lambda r: (r[2]["suite"], int(r[2]["task_id"]), r[2].get("recorded_at", ""), r[1].name))
    if args.limit:
        chosen = chosen[:args.limit]

    print(f"[select] {display_path(root)}  kind={','.join(kinds)} suite={','.join(sorted(suites)) or 'all'} "
          f"task={args.task or 'all'}")
    print(f"[select] 候補 {len(candidates)} 本 → 使う {len(chosen)} 本 / 外す {len(skipped)} 本")
    for row in skipped[:10]:
        print(f"  [skip] {row['source_npz']}: {row['reason']}")
    if len(skipped) > 10:
        print(f"  [skip] ...ほか {len(skipped) - 10} 本")
    if not chosen:
        hint = "（--kind normal / all も試せる）" if args.kind == "selected" else ""
        raise SystemExit(f"使える記録が無い{hint}")

    versions = require_local_sim()
    if not check_assets():
        return 1
    frames_dir = args.out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    episodes = []
    t_all = time.time()
    for i, (kind, meta_path, meta) in enumerate(chosen):
        npz_path = meta_path.with_suffix(".npz")
        rel = str(npz_path.relative_to(root))
        source_sha = sha256_of(npz_path)
        out = frames_dir / frames_name(source_sha, args)
        header = read_header(out) if out.exists() else None
        t0 = time.time()
        if header is None or header.get("source_sha256") != source_sha:
            arrays, info = render_episode(npz_path, meta, source_sha, args)
            header = {"source_sha256": source_sha, "render_version": RENDER_VERSION,
                      "camera_px": args.camera_px, "frames": len(arrays["action"]), **info}
            write_frames(out, arrays, header)
            status = f"{time.time() - t0:.1f}s"
        else:
            status = "cache"
        print(f"[{i + 1}/{len(chosen)}] {rel}  {header['frames']} frames  {status}"
              f"{'  noise=%d' % header['sensor_noise'] if header['sensor_noise'] else ''}")
        episodes.append({
            "frames": str(out.relative_to(args.out)),
            "length": header["frames"],
            "task": meta["language"],
            "source_npz": rel,
            "source_sha256": source_sha,
            "kind": kind,
            **{k: meta.get(k) for k in ("suite", "task_id", "name", "init_index", "success", "max_disturb_m",
                                        "exit_reason", "device", "steps", "recorded_at")},
            **{k: header[k] for k in ("max_state_error", "sensor_noise", "noise_seed", "goal_at_end")},
        })

    import subprocess
    libero_commit = subprocess.run(["git", "-C", str(LIBERO_SRC), "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True).stdout.strip() or None
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "recordings": display_path(root),
        "selection": {"kind": list(kinds), "suite": sorted(suites) or None, "task": args.task or None,
                      "require_success": not args.allow_failed,
                      "max_disturb_m": args.max_disturb_m if args.max_disturb_m >= 0 else None,
                      "limit": args.limit or None},
        "render": {"cameras": CAMERAS, "camera_px": args.camera_px, "rotate_180": True,
                   "state": "eef_pos(3) + quat2axisangle(eef_quat)(3) + gripper_qpos(2)",
                   "frame_t": "states[t]（action t を入れる直前）から regenerate_obs_from_state",
                   "sensor_noise": args.sensor_noise, "seed": SEED, "control_freq": CONTROL_FREQ,
                   "render_version": RENDER_VERSION},
        "software": {"python": sys.version.split()[0], **versions, "libero_plus": libero_commit},
        "episodes": episodes,
        "skipped": skipped,
    }
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    frames = sum(e["length"] for e in episodes)
    print(f"[done] {len(episodes)} episodes / {frames} frames  {time.time() - t_all:.0f}s → {display_path(manifest_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
