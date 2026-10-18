# teleop で集めた教師データによる SmolVLA の追加学習

**東京大学 松尾・岩澤研究室「Physical AI講座 2026 応用編1」最終課題**

- 提出者: [@motonovix](https://github.com/motonovix)
- 講座ページ: https://weblab.t.u-tokyo.ac.jp/lecture/course-list/physical-ai/advanced1/

## 概要

シミュレーション上のロボットアーム（LIBERO-Plus / robosuite / MuJoCo）で、**人がゲームパッドで操作してタスクを解き、
その軌道を教師データにして VLA モデル SmolVLA を追加学習する。** 学習したモデルはシミュレータで動かし、
成否と動画で評価する。

```text
 ① teleop（Mac）            ② データセット化            ③ 追加学習と評価（Colab）
 人がタスクを解いて記録  ─▶  LeRobot 形式で HF へ  ─▶  SmolVLA を LoRA で追加学習
 teleop/                    （TODO）                    LIBERO-Plus で動かして動画を録る
                                                        notebooks/smolvla_libero_finetune.ipynb
```

| 段階 | 中身 | 状態 |
| :--- | :--- | :--- |
| ① teleop | LIBERO-Plus をゲームパッド / キーボードで操作し、action 列と MuJoCo の state 列を記録する（[teleop/](teleop/README.md)） | 動く |
| ② データセット化 | 記録した state 列から 2 カメラの 256px 画像を描き直し、LeRobot v3.0 形式にして Hugging Face に置く | 未着手 |
| ③ 追加学習と評価 | SmolVLA（`lerobot/smolvla_libero_plus`）を追加学習し、学習前後のモデルを LIBERO-Plus で動かして動画を残す（[notebook](notebooks/smolvla_libero_finetune.ipynb)） | 動く（公開データで確認） |

## 最終課題

> 課題文の要約をここに書く

- **課題内容**: TODO
- **評価方法・指標**: TODO
- **期間・締切**: TODO
- **提出物**: このリポジトリ（コード・README）

## ① teleop で教師データを集める

人がタスクを解くための操作環境。詳細は [teleop/README.md](teleop/README.md)。

| 項目 | 内容 |
| :--- | :--- |
| 対象環境 | LIBERO-Plus の `libero_spatial` / `libero_object` / `libero_goal` / `libero_10`（摂動 variant 込み、各 2,000〜2,500 タスク）と `libero_90` |
| 入力デバイス | Nintendo Switch Pro コントローラ（主）、キーボード、マウス |
| action | OSC_POSE・control_freq 20 Hz の 7 次元（並進 3 + 回転 3 + グリッパ 1）。SmolVLA が出す action と同じ空間 |
| 記録 | MuJoCo の state 列と action 列（`.npz`）＋ 指示文などのメタ情報（`.json`）。画像は持たず、後から好きなカメラで描き直す |
| 成功判定 | BDDL のゴール述語の成立 ＋ 対象外の物体を 1 mm 以上動かしていないこと |

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
同じタスクで評価する。teleop のデータセットができたら `DATASET_REPO` を差し替える。

学習データと評価の観測は次の約束でそろえている（`lerobot/libero_plus` と同じ形式）。

- 画像のキーは `observation.images.front`（`agentview`）と `observation.images.wrist`（`robot0_eye_in_hand`）。
  **env の生画像を 180° 回転**した向きで持つ
- 状態は 8 次元（eef 位置 3 + eef 姿勢の軸角 3 + グリッパ指の位置 2）
- 指示文は LIBERO-Plus のタスクの文をそのまま使う

## 講座の内容との対応

| 講座のテーマ | この課題での取り組み |
| :--- | :--- |
| MLOps・RobOps | データセット・初期重み・LeRobot・LIBERO-Plus の版を revision / tag / commit で固定し、学習と評価を同じ notebook で再現できるようにした。teleop 側はシミュレータの版を**起動時に照合して違えば止める**。記録は state 列なので**決定論的に再生でき**、データの来歴（suite・task・初期状態・入力デバイス）を JSON に残す |
| コンピュータアーキテクチャ（メモリ・デバイスの管理） | 学習（GPU メモリを大きく使う）とシミュレータでの評価（MuJoCo の EGL 描画）を別プロセスに分けた。Colab で描画が CPU に落ちる問題に備え、NVIDIA の EGL を明示し、実際の描画先を表示する。teleop ではゲームパッドを **UI の 1 フレームに 1 回だけ snapshot** して読み、押下の取りこぼしや二重入力を防ぐ |
| ソフトウェアとしての品質 | teleop の入力処理・状態遷移を物理から切り離し、**模擬パッドで 57 件の回帰試験**を回せるようにした |
| ROS2 | 未着手（今後の課題を参照） |

## 動作確認

2026-10-18 に確認した範囲。**Colab の GPU での通し実行はまだしていない。**

| 対象 | 確認内容 | 結果 |
| :--- | :--- | :--- |
| teleop | 入力・状態遷移の回帰試験（`make -C teleop test-input`） | 57 件すべて成功 |
| teleop | 窓なしの経路確認（`make -C teleop smoke`）: `libero_spatial` #0 / `libero_90` #3 | 成功（2 カメラ描画込みで 15〜20 step/s） |
| teleop | 記録の保存 → mp4 への書き出し | 成功 |
| notebook | nbformat の検証と全コードセルの構文 | 成功 |
| notebook | 設定セル＋データセットのセルを `lerobot/libero_plus` で実行 | 成功（50 episode / 4,989 frame を選択） |
| notebook | 学習コマンド（同じオプション）を Mac の CPU で 2 step | 成功（LoRA の checkpoint を保存） |
| notebook | マージのセルをその checkpoint で実行 | 成功（LoRA の重みが残っていないことを確認） |
| notebook | 評価スクリプトで学習前・マージ後のモデルを 12 step 動かして動画を保存 | 成功（向きが正しいことを画像で確認） |

## 結果

> TODO: teleop で集めたデータ、学習条件、学習前後の成功率と動画

| モデル | suite | task | 初期配置 | 結果 | step 数 |
| :--- | :--- | :--- | :--- | :--- | ---: |
| TODO | | | | | |

## 考察と今後の課題

- **② データセット化。** teleop の記録（state 列）から 256px の 2 カメラ画像を描き直し、上の約束どおりの
  LeRobot データセットにして Hugging Face へ置くスクリプトを作る。
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

### 追加学習と評価（Colab）

GPU ランタイム（L4 / A100 推奨、T4 でも LoRA は動く）を選び、Colab の Secrets に `HF_TOKEN` を登録して、
notebook を上から実行する。必要なパッケージは notebook の中で入れる。

## ディレクトリ構成

```text
.
├── README.md
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
