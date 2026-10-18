# Dataset — teleop の記録を LeRobot データセットにする

**① teleop の記録（MuJoCo の state 列）から 2 カメラの画像と状態を描き直し、③ の notebook がそのまま学習に使える
LeRobot v3.0 データセットにして Hugging Face に置く。** 形式は `lerobot/libero_plus` と同じにそろえる。

| パス | 内容 |
| :--- | :--- |
| `Makefile` | 入口。`make -C dataset help` で一覧 |
| `scripts/render_frames.py` | 前半。記録を選び、state 列から観測（画像・状態）を描き直す。teleop の `.venv`（Python 3.10） |
| `scripts/build_dataset.py` | 後半。描いた観測を LeRobot v3.0 に書き出し、来歴を添える。`.venv-lerobot`（Python 3.12） |
| `scripts/check_dataset.py` | 書き出したデータセットを読み戻し、元の記録と突き合わせる。notebook のデータセットのセルも通す |
| `scripts/push_dataset.py` | Hugging Face へ上げる（既定 private） |
| `requirements.txt` | `.venv-lerobot` の中身（LeRobot 0.6.0 ほか） |
| `build/` | 途中の描画結果と、書き出したデータセット。**Git 管理外** |

## 流れ

```text
teleop/recordings/<suite>/<selected|normal>/t<task_id>/*.npz + *.json
  │  render_frames.py（teleop の .venv）: 記録を選び、各 step の state から 256px の 2 カメラと 8 次元の状態を作る
  ▼
dataset/build/manifest.json + frames/*.npz
  │  build_dataset.py（.venv-lerobot）: LeRobotDataset.create → add_frame → save_episode → finalize
  ▼
dataset/build/lerobot/<REPO_ID>/          LeRobot v3.0（動画は AV1）＋ meta/teleop_provenance.json
  │  check_dataset.py → push_dataset.py
  ▼
https://huggingface.co/datasets/<REPO_ID>（既定 private）
```

2 段に分けているのは、シミュレータ側（LIBERO-plus / robosuite 1.4 / MuJoCo）が Python 3.10・NumPy 1.26 に
固定してあり（`requirements-sim.txt`）、LeRobot 0.6 が Python 3.12・NumPy 2 を要するため。
間は episode ごとの npz と `manifest.json` で受け渡す。描画結果は元の記録の sha256 で引くので、
2 回目以降は描き直さない。

## 使い方

前提は teleop と同じ（`.venv`・LIBERO-plus・assets・ImageMagick。[teleop/README.md](../teleop/README.md#セットアップ)）。

```bash
make -C dataset setup        # .venv-lerobot を作る（初回だけ）
make -C dataset all          # 描画 → 書き出し → 確認。既定は selected/ のクリアした記録すべて
.venv-lerobot/bin/hf auth login   # 書き込み権限つきの token（リポジトリには置かない）
make -C dataset push         # 確認をもう一度通してから private で上げる
```

上げたら notebook の「1. 設定」を次のように変える。

```python
DATASET_REPO = "motonovix/libero-plus-teleop"   # REPO_ID
DATASET_REVISION = None
DATASET_TASK_FILTER = None    # 全 episode を使う。指示文の一部で絞ってもよい
```

| 変数 | 既定 | 内容 |
| :--- | :--- | :--- |
| `KIND` | `selected` | `selected` = 保存メニューで selected を選んだ記録 / `normal` / `all` = 両方 |
| `SUITE` / `TASK` | 全部 | suite・task id で絞る（`TASK=0,3,10-12`） |
| `RECORDINGS` | `teleop/recordings` | 記録の置き場 |
| `FLAGS` | | `render_frames.py` へそのまま渡す。`--allow-failed`・`--max-disturb-m 0.005`・`--limit 3`・`--sensor-noise off` など |
| `REPO_ID` | `motonovix/libero-plus-teleop` | Hugging Face の dataset repo。手元では `build/lerobot/<REPO_ID>` に書く |
| `PUBLIC` / `DRY_RUN` | | `make push PUBLIC=1` で public、`DRY_RUN=1` で上げずに dataset card だけ作る |

### 選ぶ記録

既定では **success かつ `max_disturb_m` ≤ 1 mm**（teleop の「クリア」と同じ基準）の記録だけを使う。
`max_disturb_m` を持たない古い記録も外す。外した記録と理由は実行時に表示し、`manifest.json` の `skipped` に残す。
episode の順番は suite → task id → 記録日時。

## 中身

`lerobot/libero_plus` と同じ形式（notebook の「5. データセットを確かめる」がこれを前提にする）。

| key | 型 | 中身 |
| :--- | :--- | :--- |
| `observation.images.front` | video 256×256×3（AV1・yuv420p） | `agentview`。**env の生画像を 180° 回転**したもの |
| `observation.images.wrist` | video 256×256×3（AV1・yuv420p） | `robot0_eye_in_hand`。同じく 180° 回転 |
| `observation.state` | float32 (8,) | `robot0_eef_pos` 3 + `quat2axisangle(robot0_eef_quat)` 3 + `robot0_gripper_qpos` 2 |
| `action` | float32 (7,) | 記録の `actions` そのまま |
| `task` | str | 記録 JSON の `language` |

fps 20、robot_type `panda`。feature の名前（`state_0`… / `action_0`…）も `lerobot/libero_plus` と同じ。

- **frame t は action t を入れる直前の state から作る。** teleop は各 action の直前の state と最後の state を
  記録する（`states` は `actions` より 1 つ多い）。最後の state は観測に使わないので、T step の記録は T frame になる。
- **描き方。** 記録した task の BDDL から `OffScreenRenderEnv`（256px・control_freq 20）を作り、`seed(42)` →
  `reset()` → 記録の `model_xml`（固定物の位置込み）で `reset_from_xml_string()` してから、各 state で
  `regenerate_obs_from_state()` を呼ぶ。全 frame で、復元した state と記録の差が 1e-12 以下であることを確かめる
  （実測 0）。
- **XML の assets のパス。** 記録の XML は記録した環境の絶対パス（venv の site-packages など）を持つので、
  この環境の robosuite と LIBERO の assets へ付け替えてから読む。venv を作り直しても、別の場所で記録した軌道でも描ける。
- **Sensor Noise の variant。** env は `step()` / `reset()` の観測の `agentview` にだけノイズを掛け、
  `regenerate_obs_from_state()` は掛けない。評価で方策が見る絵とそろえるため、同じ処理を front に掛ける
  （手首カメラには掛けない。env と同じ）。乱数の seed は記録の sha256 から決めるので、何度描いても同じ絵になる。
  `FLAGS="--sensor-noise off"` で切れる。
- **指示文。** LIBERO-Plus の `task.language` は variant の接尾辞を含む（例 `... on the plate table 12`）。
  notebook の評価も同じ文字列を方策に渡すのでそろっている。`lerobot/libero_plus` は base の指示文（40 種）だけを持つ。

### 来歴

`meta/teleop_provenance.json` に、episode ごとの元の npz（記録の置き場からの相対パス）と **sha256**、
suite・task id・task 名・init index・success・`max_disturb_m`・入力デバイス・記録日時、描画時の state の最大誤差と
ノイズの水準・seed を書く。選んだ条件・描画の設定・シミュレータと LIBERO-plus の版も同じファイルに残す。
データセットと一緒に Hugging Face へ上がる。

## 確かめていること

`make -C dataset check`（`all` と `push` の中でも走る）は次を確かめ、1 つでも外れたら止まる。

1. notebook の「1. 設定」と「5. データセットを確かめる」のセルを、このデータセットを指すようにして**そのまま**実行する。
   加えて v3.0・`panda`・AV1/yuv420p・feature の名前が `lerobot/libero_plus` と同じか
2. action が元の npz の `actions` と完全に一致するか
3. state が描画時に作った値と完全に一致するか
4. 動画を復号した絵と描画結果の平均絶対誤差が 6 以下（0〜255）か、180° 回した絵より明らかに近いか。
   既定は各 episode の先頭・中央・末尾、`check_dataset.py --all-frames` で全 frame
5. 来歴の sha256 が元の npz と一致するか

episode 0 の先頭・中央・末尾は `build/sheet.png` に並べるので、向きは目でも確かめられる。

## 動作確認

2026-10-18 に Mac（Apple Silicon）で確認した範囲。**Hugging Face へはまだ上げていない。**

| 対象 | 確認内容 | 結果 |
| :--- | :--- | :--- |
| 描画 | 提出者が同じ teleop で以前に記録した `libero_spatial` の軌道 5 本（リポジトリには含めない）を `KIND=all` で選ぶ | クリアした 4 本を選び、失敗の 1 本を外した |
| 描画 | 4 本・495 frame の state の復元誤差 | 全 frame で 0 |
| 書き出し・確認 | `make -C dataset all` | 1〜5 すべて成功。全 frame を復号した場合も画像の誤差は最大 3.42、180° 回した絵との差は +39.7 以上 |
| 向き | 描いた frame 0 と `lerobot/libero_plus` の同じ base task の frame 0 を比べる | 180° 回転後の向きで一致（MSE 正面 3,640 / 回さない場合 4,997、手首 3,973 / 10,802）。画像でも確認 |
| Sensor Noise | teleop の保存処理で記録した `libero_spatial` #1374（noise 3）の 40 step | front だけがノイズ付きになり（ノイズなしとの差 5.4）、手首・state・action は一致。ノイズの処理は env の `step()` と画素単位で一致（5 種類すべて） |
| アップロード | `make -C dataset push DRY_RUN=1` | dataset card を作るところまで成功 |
