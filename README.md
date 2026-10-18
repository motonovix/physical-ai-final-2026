# Physical AI講座 2026 応用編1 最終課題

東京大学 松尾・岩澤研究室 **「Physical AI講座 2026 応用編1」** の最終課題に取り組むためのリポジトリです。

## 講座について

- 主催: 東京大学 松尾・岩澤研究室
- 期間: 2026年9月3日〜10月8日（全6回・オンライン）
- 講座ページ: https://weblab.t.u-tokyo.ac.jp/lecture/course-list/physical-ai/advanced1/

「知能ロボットシステムエンジニアリング」をテーマに、AIロボットのシステムインテグレーションを学ぶ講座です。AIモデルの学習そのものより、MLOps/RobOps・コンピュータアーキテクチャ・ROS2 といった、Physical AI を実運用できるシステムにするためのエンジニアリング基盤を扱います。講義終了後に、約1ヶ月の最終課題があります。

| 回 | テーマ |
|---|---|
| 第1回 | MLOps・RobOps導入 |
| 第2回 | MLOps・RobOps実践 |
| 第3回 | コンピュータアーキテクチャ（メモリ・通信・仮想化・デバイスの管理） |
| 第4回 | ROS2入門(1/2) |
| 第5回 | ROS2入門(2/2) |
| 第6回 | TBA |

## 最終課題

> 課題の詳細が決まったら記入する

- **課題内容**: TODO
- **評価方法・指標**: TODO
- **期間・締切**: TODO
- **提出物**: TODO

## アプローチ

> 方針・手法・実験の結果をここにまとめる

- LIBERO-Plus のシミュレーション環境を人がゲームパッド / キーボードで操作（teleop）し、タスクを解かせる。
  操作・記録の土台は [`teleop/`](teleop/README.md)（[motonovix/parc2026](https://github.com/motonovix/parc2026) の teleop を最小構成で移植）。
- TODO: 解かせるタスクと、その先の構成（ROS2 との接続など）

## セットアップ

macOS（Apple Silicon）・Python 3.10・[uv](https://docs.astral.sh/uv/) を前提にする。
LIBERO-plus の clone と assets、ImageMagick も要る。詳細は [teleop/README.md](teleop/README.md#セットアップ)。

```bash
brew install imagemagick
make -C teleop setup                  # .venv を作り requirements.txt を入れる
make -C teleop test-input             # 入力・状態遷移の試験（窓なし）
make -C teleop smoke DEVICE=keyboard  # env を作って 40 step 回す（窓なし）
make -C teleop teleop                 # ゲームパッドで操作（DEVICE=keyboard でキーボード）
```

## ディレクトリ構成

```text
.
├── README.md
├── requirements.txt        # 実行環境（Python 3.10）
├── requirements-sim.txt    # シミュレータの版の契約（起動時に照合）
└── teleop/                 # LIBERO-Plus を人の手で操作・記録する
    ├── Makefile
    ├── README.md
    ├── scripts/
    ├── tests/
    └── recordings/         # 記録した軌道（Git 管理外）
```

## 注意事項

- 講義資料や配布物そのものは、このリポジトリには含めません。
- 講義の内容は、公開できる範囲で要約・整理して扱います。
