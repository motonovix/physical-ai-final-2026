# LIBERO-Plus テレオペレーション環境

**東京大学 松尾・岩澤研究室「Physical AI講座 2026 応用編1」最終課題**

- 提出者: [@motonovix](https://github.com/motonovix)
- 講座ページ: https://weblab.t.u-tokyo.ac.jp/lecture/course-list/physical-ai/advanced1/

## 概要

シミュレーション上のロボットアーム（LIBERO-Plus / robosuite / MuJoCo）を、**人がゲームパッドやキーボードで操作してタスクを解き、
その軌道を記録・再生できる teleop システム**を構築した。

AI の方策（VLA など）を学習・評価するには、「人が解けること」の確認と、人が解いた教師軌道が欠かせない。
本課題では、そのための土台として、入力デバイスの扱い・状態遷移・記録形式・実行環境の再現性まで含めた
操作環境を一通り作り、テストと動作確認で固めた。

| 項目 | 内容 |
| :--- | :--- |
| 対象環境 | LIBERO-Plus の `libero_spatial` / `libero_object` / `libero_goal` / `libero_10`（摂動 variant 込み、各 2,000〜2,500 タスク）と `libero_90` |
| 入力デバイス | Nintendo Switch Pro コントローラ（主）、キーボード、マウス |
| 制御 | OSC_POSE・control_freq 20 Hz の 7 次元 action（並進 3 + 回転 3 + グリッパ 1）。評価パイプラインと同じ action 空間 |
| 記録 | MuJoCo の state 列と action 列（`.npz`）＋メタ情報（`.json`）。画像は持たず、後から好きなカメラで動画に焼き直す |
| 成功判定 | BDDL のゴール述語の成立 ＋ 対象外の物体を 1 mm 以上動かしていないこと |

## 最終課題

> 課題文の要約をここに書く

- **課題内容**: TODO
- **評価方法・指標**: TODO
- **期間・締切**: TODO
- **提出物**: このリポジトリ（コード・README）

## システム構成

```text
 ┌──────────────┐   1 フレームに 1 回   ┌───────────────────────┐
 │ 入力デバイス  │ ── snapshot を取る ─▶ │ interaction.py         │
 │ pad / key /  │                       │ READY → PLAY ⇄ PAUSE    │
 │ mouse        │                       │ のメニュー判断・状態遷移 │
 └──────────────┘                       └───────────┬───────────┘
                                                    │ PLAY 中だけ action を出す
                                                    ▼
 ┌─────────────────────────────┐  step()  ┌──────────────────────────────┐
 │ teleop_libero.py             │ ───────▶ │ LIBERO-Plus env               │
 │ ・速度ペーサー（TIME SCALE）  │ ◀─────── │ robosuite 1.4 / MuJoCo 3.7    │
 │ ・AUTO ALIGN（開始姿勢補正）  │   obs    │ OSC_POSE・20 Hz               │
 │ ・成功 / 擾乱の判定           │          └──────────────────────────────┘
 └───────┬───────────────┬─────┘
         │               │
         ▼               ▼
 ┌──────────────┐  ┌────────────────────────┐      ┌──────────────────────┐
 │ cv2 の窓      │  │ recordings/             │ ───▶ │ render_recording.py   │
 │ カメラ + HUD  │  │ actions / states (.npz) │      │ state を再生して mp4  │
 │ + 表示補助    │  │ メタ情報 (.json)        │      └──────────────────────┘
 └──────────────┘  └────────────────────────┘
```

## 講座の内容との対応

| 講座のテーマ | この課題での取り組み |
| :--- | :--- |
| MLOps・RobOps | 実行環境を `requirements.txt` で固定し、シミュレータの版（`requirements-sim.txt`）を**起動時に照合して違えば止める**。記録は state 列なので**決定論的に再生でき**、データの来歴（suite・task・初期状態・入力デバイス・速度設定）を JSON に残す |
| コンピュータアーキテクチャ（デバイスの管理） | ゲームパッドを SDL の joystick API で読み、**UI の 1 フレームに 1 回だけ snapshot を取る**ことで、押した瞬間の取りこぼしや二重入力を防ぐ。描画（cv2）と入力（pygame）の役割を分け、macOS の入力監視権限なしで動かす |
| ソフトウェアとしての品質 | 入力処理・状態遷移を物理から切り離し、**模擬パッドで 57 件の回帰試験**を回せるようにした |
| ROS2 | 未着手（今後の課題を参照） |

## 動作確認

macOS（Apple Silicon）・Python 3.10.19 で確認した（2026-10-18）。

| 確認内容 | 結果 |
| :--- | :--- |
| 入力・状態遷移の回帰試験（`make -C teleop test-input`） | 57 件すべて成功 |
| 窓なしの経路確認（`make -C teleop smoke`）: `libero_spatial` task 0 | 40 step 実行・描画・タスク選択画面の生成まで成功。2 カメラ描画込みで 15.4 step/s |
| 同上: `libero_90` task 3 | 成功。20.3 step/s |
| 記録の保存 → mp4 への書き出し（`render_recording.py`） | 30 step の記録から 31 コマの mp4 を生成 |
| タスク一覧・クリア状況の一覧（`make list` / `make clears`） | 成功 |

> ゲームパッドを使った窓での実操作の結果（解いたタスク・所要 step・成功率）は「結果」に書く。

## セットアップと使い方

macOS（Apple Silicon）・Python 3.10・[uv](https://docs.astral.sh/uv/) を前提にする。
LIBERO-plus の clone と assets、ImageMagick も要る。詳細は [teleop/README.md](teleop/README.md#セットアップ)。

```bash
brew install imagemagick
make -C teleop setup                  # .venv を作り requirements.txt を入れる
make -C teleop test-input             # 入力・状態遷移の試験（窓なし）
make -C teleop smoke DEVICE=keyboard  # env を作って 40 step 回す（窓なし）
make -C teleop teleop                 # ゲームパッドで操作（DEVICE=keyboard でキーボード）
```

操作方法・画面の見方・記録の形式は [teleop/README.md](teleop/README.md) にまとめた。

## 結果

> TODO: 実際に解いたタスクと、その記録

| suite | task | 摂動 | 結果 | step 数 | 備考 |
| :--- | :--- | :--- | :--- | ---: | :--- |
| TODO | | | | | |

## 考察と今後の課題

- **ROS2 との接続。** 入力（ゲームパッド）と状態（EEF 位置・姿勢・グリッパ）をトピックとして流せば、
  シミュレータと実機で同じ操作系を使える。
- **方策との組み合わせ。** 方策の rollout を途中から人が引き継ぐ機能（`--from-recording`）はあるので、
  方策が失敗した場面だけ人が直す DAgger 型のデータ収集に広げられる。
- **独自タスクの追加。** タスク選択の枠組み（`SUITE_CYCLE` / `build_task` など）は残してあり、
  独自の BDDL と初期配置を足せば同じ操作系で遊べる。
- TODO

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

## クレジット・注意事項

- teleop のコードは、提出者が別の非公開リポジトリ（PARC2026 コンペ用）で作ったものを最小構成で移植した。
- シミュレーション環境は [LIBERO](https://github.com/Lifelong-Robot-Learning/LIBERO) と
  [LIBERO-plus](https://github.com/sylvestf/LIBERO-plus) を使う（このリポジトリには含めない）。
- 講義資料や配布物そのものは、このリポジトリには含めない。講義の内容は、公開できる範囲で要約・整理して扱う。
