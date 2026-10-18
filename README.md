# teleop で集めた教師データによる SmolVLA の追加学習

## 概要

シミュレーション上のロボットアーム（LIBERO-Plus / robosuite / MuJoCo）で、**人がゲームパッドで操作してタスクを解き、
その軌道を教師データにして VLA モデル SmolVLA を追加学習する。** 学習したモデルはシミュレータで動かし、
成否と動画で評価する。

```text
 ① teleop（Mac）            ② データセット化（Mac）     ③ 追加学習と評価（Colab）
 人がタスクを解いて記録  ─▶  LeRobot 形式で HF へ  ─▶  SmolVLA を LoRA で追加学習
 teleop/                    dataset/                    LIBERO-Plus で動かして動画を録る
                                                        notebooks/smolvla_libero_finetune.ipynb
```

| 段階 | 中身 | 状態 |
| :--- | :--- | :--- |
| ① teleop | LIBERO-Plus をゲームパッド / キーボードで操作し、action 列と MuJoCo の state 列を記録する（[teleop/](teleop/README.md)） | 動く |
| ② データセット化 | 記録した state 列から 2 カメラの 256px 画像を描き直し、LeRobot v3.0 形式にして Hugging Face に置く（[dataset/](dataset/README.md)） | 動く（手元の記録で確認。HF へはまだ上げていない） |
| ③ 追加学習と評価 | SmolVLA（`lerobot/smolvla_libero_plus`）を追加学習し、学習前後のモデルを LIBERO-Plus で動かして動画を残す（[notebook](notebooks/smolvla_libero_finetune.ipynb)） | 動く（公開データで確認） |

## ① teleop で教師データを集める

人がタスクを解くための操作環境。詳細は [teleop/README.md](teleop/README.md)。

| 項目 | 内容 |
| :--- | :--- |
| 対象環境 | LIBERO-Plus の `libero_spatial` / `libero_object` / `libero_goal` / `libero_10`（摂動 variant 込み、各 2,000〜2,500 タスク）と `libero_90` |
| 入力デバイス | Nintendo Switch Pro コントローラ（主）、キーボード、マウス |
| action | OSC_POSE・control_freq 20 Hz の 7 次元（並進 3 + 回転 3 + グリッパ 1）。SmolVLA が出す action と同じ空間 |
| 記録 | MuJoCo の state 列と action 列（`.npz`）＋ 指示文などのメタ情報（`.json`）。画像は持たず、後から好きなカメラで描き直す |
| 成功判定 | BDDL のゴール述語の成立 ＋ 対象外の物体を 1 mm 以上動かしていないこと |

## ② teleop の記録を LeRobot データセットにする

teleop の記録は画像を持たないので、記録した MuJoCo の state 列から学習用の観測を描き直す。
詳細は [dataset/README.md](dataset/README.md)。

```bash
make -C dataset setup   # LeRobot 0.6.0 の .venv-lerobot（Python 3.12）を作る（初回だけ）
make -C dataset all     # 記録を選ぶ → 描き直す → LeRobot v3.0 に書き出す → 読み戻して確かめる
make -C dataset push    # Hugging Face へ上げる（既定 private）
```

| 項目 | 内容 |
| :--- | :--- |
| 使う記録 | 既定は保存メニューで selected を選んだもののうち、success かつ対象外の物体を 1 mm 以上動かしていないもの。`KIND` / `SUITE` / `TASK` で変えられる |
| 観測 | 各 action の直前の state から、`agentview` と `robot0_eye_in_hand` を 256px で描いて 180° 回し、8 次元の状態を作る。全 frame で state を誤差 0 で復元できることを確かめる |
| 形式 | `lerobot/libero_plus` と同じ（AV1 の動画 2 本・state 8 次元・action 7 次元・fps 20）。notebook のデータセットのセルをそのまま通して確かめる |
| 来歴 | episode ごとに元の記録の相対パスと sha256、選んだ条件、描画の設定、シミュレータの版を `meta/teleop_provenance.json` に残す |

描画は teleop と同じ Python 3.10 の環境、書き出しは LeRobot 0.6 の Python 3.12 の環境で動かす
（シミュレータは NumPy 1.26、LeRobot 0.6 は NumPy 2 を要するため）。

## ③ SmolVLA を追加学習して動画で評価する

[notebooks/smolvla_libero_finetune.ipynb](notebooks/smolvla_libero_finetune.ipynb) を Colab で開き、上から順に実行する
（[Colab で開く](https://colab.research.google.com/github/motonovix/physical-ai-final-2026/blob/main/notebooks/smolvla_libero_finetune.ipynb)）。
書き換えるのは最初の「設定」セルだけ。

| セクション | 内容 |
| :--- | :--- |
| 1〜4 | 設定、GPU の確認、LeRobot v0.6.0 の導入、Hugging Face へのログイン |
| 5 | データセットの形式を確かめ、学習に使う episode を選ぶ（指示文で絞り込める） |
| 6〜7 | 初期重み `lerobot/smolvla_libero_plus` を取り、`lerobot-train` で追加学習する。既定は Action Expert への LoRA、`TRAIN_MODE = "full"` で Expert 全体 |
| 8〜9 | LoRA を元の重みへマージして推論用のモデルにする。任意で Hugging Face へアップロード |
| 10〜12 | LIBERO-Plus と assets を入れ、学習前後のモデルを同じタスク・同じ初期配置で動かす。成否の表と mp4（正面カメラ＋手首カメラ）を残す |

既定では、動作確認のために公開データ `lerobot/libero_plus` から `libero_spatial` の 1 タスク分（50 episode）を選んで学習し、
同じタスクで評価する。teleop のデータセット（②）を上げたら `DATASET_REPO` を差し替え、`DATASET_REVISION` と
`DATASET_TASK_FILTER` を `None` にする。

学習データと評価の観測は次の約束でそろえている（`lerobot/libero_plus` と同じ形式）。

- 画像のキーは `observation.images.front`（`agentview`）と `observation.images.wrist`（`robot0_eye_in_hand`）。
  **env の生画像を 180° 回転**した向きで持つ
- 状態は 8 次元（eef 位置 3 + eef 姿勢の軸角 3 + グリッパ指の位置 2）
- 指示文は LIBERO-Plus のタスクの文をそのまま使う

## 考察と今後の課題

- **ROS2 との接続。** 入力（ゲームパッド）と状態（EEF 位置・姿勢・グリッパ）をトピックとして流せば、
  シミュレータと実機で同じ操作系を使える。
- **方策と人の組み合わせ。** teleop には方策の rollout を途中から人が引き継ぐ機能（`--from-recording`）があるので、
  方策が失敗した場面だけ人が直す DAgger 型のデータ収集に広げられる。
- TODO

## セットアップ

### teleop（Mac）

macOS（Apple Silicon）・Python 3.10・[uv](https://docs.astral.sh/uv/) を前提にする。
LIBERO-plus の clone と assets、ImageMagick も要る。詳細は [teleop/README.md](teleop/README.md#セットアップ)。

```bash
brew install imagemagick
make -C teleop setup                  # .venv を作り requirements.txt を入れる
make -C teleop test-input             # 入力・状態遷移の試験（窓なし）
make -C teleop smoke DEVICE=keyboard  # env を作って 40 step 回す（窓なし）
make -C teleop teleop                 # ゲームパッドで操作（DEVICE=keyboard でキーボード）
```

### データセット化（Mac）

teleop の環境に加えて、LeRobot 0.6.0 用の Python 3.12 の環境を作る。詳細は [dataset/README.md](dataset/README.md#使い方)。

```bash
make -C dataset setup                 # .venv-lerobot を作り dataset/requirements.txt を入れる
make -C dataset all                   # 記録 → LeRobot データセット（dataset/build/）
.venv-lerobot/bin/hf auth login       # 書き込み権限つきの token（リポジトリには置かない）
make -C dataset push                  # Hugging Face へ上げる（既定 private）
```

### 追加学習と評価（Colab）

GPU ランタイム（L4 / A100 推奨、T4 でも LoRA は動く）を選び、Colab の Secrets に `HF_TOKEN` を登録して、
notebook を上から実行する。必要なパッケージは notebook の中で入れる。

## ディレクトリ構成

```text
.
├── README.md
├── dataset/                            # teleop の記録を LeRobot データセットにして HF へ上げる
│   ├── Makefile
│   ├── README.md
│   ├── requirements.txt                # LeRobot 0.6.0 の環境（Python 3.12）
│   ├── scripts/
│   └── build/                          # 描画結果と書き出したデータセット（Git 管理外）
├── notebooks/
│   └── smolvla_libero_finetune.ipynb   # SmolVLA の追加学習と動画での評価（Colab）
├── requirements.txt                    # teleop の実行環境（Python 3.10）
├── requirements-sim.txt                # シミュレータの版の契約（起動時に照合）
└── teleop/                             # LIBERO-Plus を人の手で操作・記録する
    ├── Makefile
    ├── README.md
    ├── scripts/
    ├── tests/
    └── recordings/                     # 記録した軌道（Git 管理外）
```

## クレジット・注意事項

- teleop のコードは、提出者が別の非公開リポジトリ（PARC2026 コンペ用）で作ったものを最小構成で移植した。
  notebook も同じリポジトリでの SmolVLA の学習・評価の経験をもとに、このリポジトリ用に書き起こした。
- [LeRobot](https://github.com/huggingface/lerobot)（SmolVLA）、[LIBERO](https://github.com/Lifelong-Robot-Learning/LIBERO)、
  [LIBERO-plus](https://github.com/sylvestf/LIBERO-plus) を使う（このリポジトリには含めない）。
- 講義資料や配布物そのものは、このリポジトリには含めない。講義の内容は、公開できる範囲で要約・整理して扱う。
