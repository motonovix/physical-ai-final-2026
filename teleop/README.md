# Teleop — LIBERO-Plus を人の手で操作する

**Mac のキーボード / マウス / ゲームパッドで LIBERO-Plus の env を直接操作し、タスクを解いて軌道を記録する。**

[motonovix/parc2026](https://github.com/motonovix/parc2026) の `data_studio/teleop`（2026-10-18 時点）から、
操作・表示・記録の本体だけを移植した。parc2026 固有のタスク表（逆方向タスク・候補群・careful 収集・
運営 kit の Track 3 など）は外し、LIBERO / LIBERO-Plus の標準 suite だけで動く最小構成にしてある。

| パス | 内容 |
| :--- | :--- |
| `Makefile` | 起動の入口。`make help` で一覧 |
| `scripts/teleop_libero.py` | 本体（入力・タスク選択画面・READY/PLAY/PAUSE・記録） |
| `scripts/interaction.py` | 入力 snapshot・メニュー判断・episode の状態遷移。物理処理から独立 |
| `scripts/display_guides.py` / `surface_guide.py` | ゴール領域・グリッパの鉛直線などの表示補助（表示だけ、記録には入らない） |
| `scripts/start_alignment.py` | 開始時に姿勢 3 軸を 0 へ合わせる AUTO ALIGN |
| `scripts/step_pacer.py` | シミュレーション速度（TIME SCALE）の調整 |
| `scripts/render_recording.py` | 記録を mp4 などに焼き直す |
| `scripts/pad_probe.py` | ゲームパッドの軸・ボタン番号のライブ表示 |
| `scripts/sim_runtime.py` | 起動時にシミュレータの版（`requirements-sim.txt`）を照合する |
| `tests/` | 入力・状態遷移の回帰試験（`make test-input`） |
| `recordings/` | 記録した軌道（`.npz` + `.json`）。**Git 管理外** |

## セットアップ

1. ImageMagick を入れる（LIBERO の `env_wrapper` が `wand` 経由で読む）。

   ```bash
   brew install imagemagick
   ```

2. [LIBERO-plus](https://github.com/sylvestf/LIBERO-plus) を clone し、README に従って assets を
   `libero/libero/assets` に置く。既定の場所は `~/ghq/github.com/sylvestf/LIBERO-plus` で、
   別の場所なら環境変数 `LIBERO_PLUS_ROOT` で渡す。LIBERO 本体は pip で入れず、
   `teleop_libero.py` が `sys.path` に足して読む。パスの設定は `~/.libero/config.yaml`。

3. リポジトリ直下に Python 3.10 の `.venv` を作る（[uv](https://docs.astral.sh/uv/) を使う）。

   ```bash
   make -C teleop setup
   ```

   シミュレータの版は `requirements-sim.txt`（MuJoCo 3.7.0 / robosuite 1.4.0 / NumPy 1.26.4 /
   gym 0.25.2 / bddl 3.6.0）に固定してあり、起動時に照合して違えば止まる。

4. 確認する。

   ```bash
   make -C teleop test-input           # 模擬padで入力・状態遷移を試験（窓なし・物理なし）
   make -C teleop smoke DEVICE=keyboard # 窓を出さずに env 作成〜40 step〜描画まで通す
   ```

## 実行

```bash
cd teleop

make teleop                     # gamepad + 手首カメラ。タスク選択画面から
make teleop DEVICE=keyboard     # キーボードで操作
make teleop SUITE=libero_90 TASK=3   # task を直接開始
make teleop TASK=42 FLIP=z      # z 軸を反転して開始
make list FILTER=Camera         # task 一覧（名前・摂動カテゴリで絞る）
make clears                     # base task ごとのクリア状況と npz を一覧
make render REC=libero_spatial/normal/t0/<name>.npz
make probe                      # ゲームパッドの軸・ボタン番号をライブ表示
```

素の `python3` では動かない。直接叩くなら `.venv` の python と ImageMagick の環境変数を使う
（**無いと `MagickWand shared library not found` で import 段階から落ちる**）。

```bash
cd teleop/scripts
MAGICK_HOME=/opt/homebrew/opt/imagemagick \
DYLD_LIBRARY_PATH=/opt/homebrew/opt/imagemagick/lib \
../../.venv/bin/python teleop_libero.py --device keyboard --suite libero_spatial --task-id 0
```

## suite とタスク選択

| suite | 件数 | 選択画面 |
| :--- | ---: | :--- |
| `libero_spatial` / `libero_object` / `libero_goal` / `libero_10` | 各 2,000〜2,500 | suite → base task（10 種）→ 摂動カテゴリ → 難易度 → variant |
| `libero_90` | 90 | suite → タスク → そのまま READY |

LIBERO-Plus の 4 suite は、元の 10 タスクに 7 種の摂動（Background Textures / Camera Viewpoints /
Language Instructions / Light Conditions / Objects Layout / Robot Initial States / Sensor Noise）を
掛けた variant の集まり。摂動カテゴリと難易度は LIBERO-plus の
`libero/libero/benchmark/task_classification.json` から引く。**名前の接尾辞から推測しない**
（base 名に `_table_1` を含む task があり、素朴な部分一致では `_tb_` variant と取り違える）。

`--task-id` を渡すと選択画面を飛ばして直接 READY に入る。`--init-index` は同じ task の中で
物体配置を変える（多くの task で 50 通り）。

独自のタスクを足すときは、`teleop_libero.py` の `SUITE_CYCLE`（選択画面に並べる suite）・
`get_task_suite` / `build_task`（task の作り方）・`INIT_MENU_SUITES`（タスクの次に INIT を選ぶ段へ進む）・
`DIRECT_SUITES`（タスクを選んだらそのまま READY）を触る。

## READY → PLAY ⇄ PAUSE

READY は記録前、PLAY だけがロボットを動かして記録する。PAUSE は記録を保持した停止。

| 状態 | A | X | Y | B |
| :--- | :--- | :--- | :--- | :--- |
| タスク選択 | 決定 | 戻る | 決定 | 戻る |
| READY | メニュー（開始 / AUTO ALIGN / タスク選択へ）を実行 | 0.75 秒 × 3 のカウント後に PLAY | 無効 | タスク選択へ（確認あり） |
| PLAY | 未使用 | PAUSE へ | グリッパを閉じる | グリッパを開く |
| PAUSE | 選択項目を実行 | PLAY 再開 | 無効 | 保存先選択 → PAUSE、PAUSE → PLAY |

- ゴール述語が成立すると 20 step 後に自動で PAUSE し、保存メニュー（selected / normal）を出す。
- READY / PAUSE では R/L で速度 ±、ZR/ZL で時間スケール 速く/遅く。初期値は SPEED 1.3・TIME 0.6。
- **AUTO ALIGN**（既定 ON）は開始時に roll / pitch / yaw を 0 へ実際に動かして揃える。補正の action / state も軌跡に入る。
  READY メニューで ON/OFF、起動時から OFF なら `--no-auto-align-start`。

## 操作

### ゲームパッド（Nintendo Switch Pro コントローラ）

**倒し量がそのまま速度になる連続入力。** 読み取りは pygame（SDL）の joystick API。
事前に Mac と Bluetooth でペアリングしておく（未接続だと起動時に止まる）。
gamepad を使っている間はキーボード入力は無効。

```text
左スティック: 前後（x）/ 左右（y）    右スティック: 上下（z）/ yaw（横は25%を超えたら反応）
十字キー: pitch（上下）/ roll（左右）    ZR/ZL/L/R でも同じ（上=ZR 下=ZL 左=L 右=R、ZR は 0.7 倍）
グリッパ（PLAYのみ）: Y=閉じる / B=開く / 右スティック押し込み（R3）=開閉トグル
READY: 十字キー上下=選択 A=決定 X=カウント後に開始 B=タスク選択へ（確認あり）
READY/PAUSE: R/L=速度 +/−、ZR/ZL=時間スケール 速く/遅く
PLAY: X=PAUSE
PAUSE: 十字キー上下=選択 A=決定 X=再開 B=戻る（保存先選択→PAUSE、PAUSE→PLAY）
```

軸の向きが逆なら起動時に `--pad-flip y,yaw`（Makefile なら `FLIP=y,yaw`）。
軸・ボタン番号は `make probe` で確かめられる（A/B/X/Y=0..3、R3=8、L=9、R=10、十字キー=11..14、ZL/ZR=axis 4/5）。

### キーボード

**減衰式。** キーを押すとフル速度で動き、離すと約 1.1 秒で止まる。ちょん押しは小さく、
押しっぱなしはキーリピートの数で速くなる。`space` で即停止。

```text
w/s: +x/-x   a/d: +y/-y   r/f: +z/-z
i/k: pitch   j/l: yaw     u/o: roll
space: 即停止  g: グリッパ開閉  -/=: 速度
p: PLAY と停止（READY/PAUSE）のトグル
enter: 保存して SELECT へ    esc: 破棄して SELECT へ
```

マウス（`--device mouse`）も残してあるが、macOS では OpenCV がホイールを配送しないなど未検証の部分がある。

## 画面

カメラ映像の下に状態帯が付く。INPUT（今入れている action）と EEF（実際の位置 [m] と姿勢 [度]）、
ROLL / PITCH / YAW の姿勢ダイヤル、SPEED・STEP・GRIP を並べる。映像側には SUCCESS（ゴール成立）、
`Collided: <物体名>`（対象外物体の擾乱）、REC / PAUSE / READY を出す。

表示補助（ゴール領域の枠、グリッパ中心から支持面への鉛直線、把持線、横からの仮想カメラ `gripper_side`）は
BDDL の `:goal`（On / In）から作る。表示だけで、state・XML・保存物には入らない。

カメラは `--camera frontview,robot0_eye_in_hand` のようにカンマ区切りで横に並べる。
`--small-camera` で右端に半分サイズのカメラを縦に積める。

## 成功の判定

- **SUCCESS** はゴール述語（BDDL の `:goal`）の成立。
- **擾乱**は、対象外の可動物体（BDDL の `obj_of_interest` 以外）が初期位置から L1 距離で 1mm を超えて動いたか。
- 両方を満たしたもの（ゴール成立かつ擾乱 ≤ 1mm）を**クリア**と数える。選択画面の右列の `CLEAR <step>` は
  その最短 step、`rec N` は記録はあるがクリアなし、`sel N` は selected に保存した本数。

## 記録される形式

`recordings/<suite>/<selected|normal>/t<task_id>/<suite>_t<task>_i<init>_<timestamp>.{npz,json}` の 2 本。
`esc` で明示的に捨てたとき以外は保存する（`--max-steps` 到達、Ctrl-C、env の異常終了を含む）。

| キー | 形 | 内容 |
| :--- | :--- | :--- |
| `actions` | `(T, 7)` float32 | 実行した OSC_POSE delta（xyz 3 + 回転 3 + グリッパ 1、すべて [-1, 1]、control_freq=20） |
| `states` | `(T+1, S)` float64 | 各 action の**直前**の MuJoCo state（flatten 済み）＋最終状態 |
| `model_xml` | str | env の MJCF（再生用） |

**画像は保存しない。** 再生は決定論的なので、`states` から好きなカメラ・解像度で焼き直せる。
JSON 側に suite / task_id / name / language / bddl / init_index / device / control_freq / time_scale /
camera / horizon / exit_reason / success / steps / max_disturb_m / max_disturb_object / start_alignment を残す。

## 動画にする

```bash
make render REC=libero_spatial/normal/t0/<name>.npz CAMERA=frontview,robot0_eye_in_hand
```

出力は npz と同じ場所に `.mp4`（既定は H.264 / `avc1`、記録時の control_freq で等速）。
`FLAGS="--format sheet"` で格子状の 1 枚、`--format gif` / `png` も選べる。左上に step と入力した
action を焼き込み、ゴールが成立した step からは緑の枠を出す（擾乱の判定は入っていない）。

## 録画の途中から引き継ぐ

既存の録画（teleop の録画、または方策 rollout を同じ形式に変換した npz）の step k の状態から、人が続きを操作する。

```bash
make teleop FLAGS="--from-recording <path>.npz --takeover-step 55"
```

- suite / task_id / init_index は同名 json から取る。`--takeover-step` 省略時は末尾。
- `--takeover-replay` を付けると開始前に `actions[k:]` を再生して録画の残りと一致することを確かめる。
- 保存は通常形式で、`actions` / `states` は録画の `[:k]` + 人の操作。json に `takeover` が入る。

## オプション

| オプション | 既定 | 内容 |
| :--- | :--- | :--- |
| `--device` | `keyboard`（Makefile は `gamepad`） | `keyboard` / `mouse` / `gamepad` |
| `--suite` | `libero_spatial` | `libero_spatial` / `libero_object` / `libero_goal` / `libero_10` / `libero_90` |
| `--task-id` | （なし） | 指定すると選択画面を飛ばして直接始める |
| `--init-index` | `0` | その task の init state の番号 |
| `--camera` | `frontview`（Makefile は `frontview,robot0_eye_in_hand`） | カンマ区切りで横に並べる |
| `--small-camera` | （なし） | 右端に半分サイズで縦に積むカメラ |
| `--window-size` | `640` | 1 カメラあたりの表示解像度 |
| `--time-scale` | `0.6` | 実時間に対するシミュレーション速度。0=無制限 |
| `--max-steps` | `6000` | 記録する最大 step 数 |
| `--pad-flip` | （なし） | ゲームパッドで符号を反転する軸（例 `y,yaw`） |
| `--pad-xy-gain` / `--pad-z-gain` | `1.5` / `2.0` | 左スティック（x/y）・右スティック縦（z）の倍率 |
| `--pad-yaw-deadzone` | `0.25` | 右スティック横（yaw）の遊び |
| `--no-auto-align-start` | — | 開始時の AUTO ALIGN を OFF |
| `--smoke N` | — | 窓を出さずに N step だけ回して経路を確認 |
| `--out` | `teleop/recordings` | 保存先 |

## 注意点

- **時間制限を外している。** robosuite の既定 horizon は 1000 step で、超えると `step()` が落ちる。
  人の操作には短すぎるので `ignore_done=True` にし、`--max-steps + 100` を horizon に渡している。
  評価側は評価側の horizon で打ち切るので、長い軌道はそのまま再生できないことがある。
- **`torch.load` の既定値を戻している。** LIBERO の init states は `weights_only=False` でないと読めないので、
  ロード時だけ既定値を差し替える。
- **Sensor Noise variant は観測ノイズを切って操作する。** 観測画像のノイズ計算で 1 step が約 5 倍重くなるため。
  teleop は観測画像を使わないので、物理・記録には影響しない。
- cv2 と pygame が同じ libSDL2 を二重に同梱していて、起動時に「implemented in both」警告が大量に出る。
  実害はないので Makefile で stderr から濾している。
