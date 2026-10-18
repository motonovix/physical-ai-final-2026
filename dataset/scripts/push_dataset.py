#!/usr/bin/env python3
"""build_dataset.py で書いたデータセットを Hugging Face へ上げる。

`.venv-lerobot`（Python 3.12 + LeRobot 0.6.0）で動かす。token は `hf auth login` で保存したものか、
環境変数 `HF_TOKEN` を使う（このスクリプトもリポジトリも token を持たない）。
既定では private の dataset repo に置く。notebook は Colab の Secrets の `HF_TOKEN` で読めるので
private のままで学習できる。

    .venv-lerobot/bin/python dataset/scripts/push_dataset.py --repo-id <hf_user>/<name>
"""

import argparse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GITHUB = "https://github.com/motonovix/physical-ai-final-2026"
DESCRIPTION = (
    "LIBERO-Plus のタスクを人がゲームパッド / キーボードで解いた軌道（teleop）から作ったデータセット。"
    "記録した MuJoCo の state 列から 2 カメラの 256px 画像と 8 次元の状態を描き直し、"
    "`lerobot/libero_plus` と同じ形式にしてある。元の記録との対応は `meta/teleop_provenance.json`。"
    f"作り方は {GITHUB}/tree/main/dataset 。"
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo-id", required=True)
    p.add_argument("--work", type=Path, default=REPO / "dataset/build")
    p.add_argument("--root", type=Path, default=None, help="既定は <work>/lerobot/<repo-id>")
    p.add_argument("--public", action="store_true", help="public の repo にする（既定は private）")
    p.add_argument("--license", default="apache-2.0", help="dataset card に書くライセンス（LeRobot の既定と同じ）")
    p.add_argument("--dry-run", action="store_true",
                   help="上げずに、ログイン中のユーザーと dataset card（<work>/dataset_card.md）だけ確かめる")
    args = p.parse_args()

    root = args.root or args.work / "lerobot" / args.repo_id
    if not (root / "meta/info.json").exists():
        raise SystemExit(f"{root} にデータセットが無い。先に build_dataset.py を実行する")

    from huggingface_hub import whoami
    from huggingface_hub.errors import LocalTokenNotFoundError
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.datasets.utils import create_lerobot_dataset_card

    try:
        user = whoami()["name"]
    except LocalTokenNotFoundError:
        if not args.dry_run:
            raise SystemExit("Hugging Face にログインしていない。`hf auth login` か環境変数 HF_TOKEN を使う")
        user = "未ログイン"
    dataset = LeRobotDataset(args.repo_id, root=root)
    tags = ["LIBERO", "LIBERO-Plus", "teleoperation", "panda"]
    print(f"[push] {root}")
    print(f"       → https://huggingface.co/datasets/{args.repo_id}（{'public' if args.public else 'private'}、"
          f"{user} として）  {dataset.meta.total_episodes} episodes / {dataset.meta.total_frames} frames")
    if args.dry_run:
        card = create_lerobot_dataset_card(tags=tags, dataset_info=dataset.meta.info, license=args.license,
                                           repo_id=args.repo_id, dataset_description=DESCRIPTION, url=GITHUB)
        (args.work / "dataset_card.md").write_text(str(card), encoding="utf-8")
        print(f"[dry-run] 上げなかった。dataset card: {args.work / 'dataset_card.md'}")
        return 0
    dataset.push_to_hub(private=not args.public, license=args.license, tags=tags,
                        dataset_description=DESCRIPTION, url=GITHUB)
    print(f"[done] https://huggingface.co/datasets/{args.repo_id}")
    print("       notebook の設定で DATASET_REPO をこの repo、DATASET_REVISION と DATASET_TASK_FILTER を None にする")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
