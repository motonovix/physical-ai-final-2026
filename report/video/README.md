# 動画の作り方

提出物の 1 つ、YouTube 動画の素材づくりと書き出しのメモ。
素材の動画と書き出した動画は重いので Git に入れない（`assets/`・`build/`・`local.mk` は `.gitignore` 済み）。

## 構成（案）

前半は Mac で teleop を動かしている様子の画面録画、後半は parc2026 で追加学習したモデルがタスクを解く動画。

| 区間 | 中身 | 素材 | パーツ（`build/parts/`） |
| :--- | :--- | :--- | :--- |
| 前半 | Mac で teleop を動かし、Pro コントローラで LIBERO-Plus のタスクを解く様子 | 自分で画面録画する（[前半を録る](#前半teleop-の画面を録る)） | `teleop_<名前>.mp4`（未） |
| 後半 1 | SmolVLA を人の teleop 18 本で LoRA 追加学習し、赤いマグの課題を解く（正面＋手首カメラ、等倍速、7.7 秒） | parc2026（2026-09-08） | `smolvla.mp4` |
| 後半 2 | VLADrop の追加学習の前と後を、同じ問題・同じ初期配置で横に並べる（2 倍速、9.5 秒） | parc2026（2026-09-20） | `compare.mp4` |
| 後半 3 | 追加学習の後に解けた 4 問を並べる（等倍速、11.6 秒） | 同上 | `successes.mp4` |
| 後半 4 | 16 問の成否：学習前 0/16 → 学習後 13/16（ほかの物を動かさずに成功 8 問）（5 秒） | 同上 | `results.mp4` |

並びは [timeline.txt](timeline.txt)、使う問題と日本語の字幕は [clips.json](clips.json) で変える。
後半だけで約 34 秒。ナレーションの間を取るなら、パーツの長さ（止めておく秒数）を延ばす。

### 話すときに押さえる事実

学習と評価は、すべて別の非公開リポジトリ（parc2026、PARC2026 コンペ用）で行った。後半はそのとき撮った動画を使う。
このリポジトリの teleop・データセット化・notebook は、parc2026 で実際に使ったコードを提出用にまとめたもので、
このリポジトリのコードで学習し直してはいない。

- **SmolVLA（後半 1）**
  - 初期重み `lerobot/smolvla_libero_plus` に、人が teleop で解いた 18 本（赤いマグの課題、1,942 frame）で LoRA（r16）を 1,000 step 当てた。
    このリポジトリの ①→②→③ と同じ流れ。
  - 評価は、学習に使った初期配置での 1 試行。未知の配置への汎化を示すものではない。
  - 同じ重みを、シミュレータの版などの条件を変えて動かした別の 2 試行は、時間内にゴールに届いていない。
- **VLADrop（後半 2〜4）**
  - π0.5 系の公開モデル（LIBERO-Plus で学習済み、言語モデルの層を 9 つ落とした軽量版）が起点。
    これを、元の LIBERO の課題を逆向きにした課題の教師データ（415 episode）で 600 step 追加学習した。
  - 評価は、逆向きの課題 8 種 × 指示文 2 通り（原文・言い換え）の 16 問、各 1 試行。学習と評価の初期配置が重なる。
  - 成功は BDDL のゴール条件の成立。「ほかの物を動かさずに」は、対象外の物体が 1 mm 以上動いていないこと。
- **話す前に確かめること**
  - VLADrop の教師データ 415 本を、人の teleop からどう作ったか。parc2026 の記録で確かめる。
  - 起点のモデルの出典。YouTube の概要欄に書く。

## 前半：teleop の画面を録る

Mac で teleop を動かしている様子を、画面録画で動画にする。学習用のデータを集めるための操作ではない。

1. teleop を起動する（`make -C teleop teleop`、[teleop/README.md](../../teleop/README.md)）。
2. `⌘⇧5` で「選択部分を収録」を選び、teleop の窓だけを囲んで収録する。
   声を同時に入れるなら、「オプション」でマイクを選ぶ。
3. 撮った `.mov` を `assets/teleop/<名前>.mov` に置き、`make norm` で形式をそろえる。

画面録画がカクつくなど画質が足りないときは、teleop の記録（`.npz`）から同じ動きを描き直せる
（`make -C teleop render REC=<suite>/normal/<dir>/<name>.npz FLAGS="--size 720"`。画面のガイド表示は入らない）。

### 映り込みに気をつける

- 集中モードで通知を止める。
- ターミナル（プロンプトにユーザー名やパスが出る）、Finder、メニューバーを映さない。「選択部分を収録」で窓だけを撮る。
- Omnicampus の ID を含め、名前につながる情報を映さない。

## 手順

`report/video/` で実行する。Pillow を使うので、teleop の `.venv`（`make -C teleop setup`）が要る。

```bash
cp local.mk.example local.mk   # 元動画の場所を書く（初回だけ。local.mk は Git 管理外）
make collect                   # parc2026 の元動画を assets/parc2026/ へ写す（SSD をつないで、初回だけ）
make parts                     # 後半のパーツを作る（PART=compare で 1 つだけ）
make norm                      # assets/teleop/ の録画を 1920x1080・30fps にそろえる
make sheet                     # パーツのコマを並べた確認用の画像（build/sheet/）
make final                     # timeline.txt の順につなぐ → build/final.mp4
make open                      # 見る
```

`make collect` の後は SSD が無くても作り直せる。

### 書き出しの形式

YouTube の推奨に合わせ、すべてのパーツを同じ形式にそろえてからつなぐ。

| 項目 | 値 |
| :--- | :--- |
| 映像 | 1920x1080・30fps・H.264 High・CRF 18・yuv420p |
| 音声 | AAC・48 kHz・ステレオ・192 kbps（音の無いパーツには無音を入れる） |
| その他 | `+faststart`（先頭から再生を始められる） |

元動画は 256px（SmolVLA は 512x256）なので、拡大すると少し粗い。

### 文字の入れ方

この Mac の ffmpeg には文字を描く機能（drawtext・subtitles）が入っていない。
見出しや「成功」の札は Pillow（ヒラギノ角ゴシック）で画像にして重ねている（[scripts/build_parts.py](scripts/build_parts.py)）。

## 仕上げ

- **ナレーション・BGM：** iMovie などで `build/final.mp4` に重ねるのが早い。仮の読み上げは `say -v Kyoko -o narration.aiff "..."` で作れる。
- **アップロード：** YouTube へは自分のアカウントで上げる。公開範囲（限定公開など）は課題の指示に合わせる。

## ファイル

| パス | 中身 |
| :--- | :--- |
| `Makefile` | collect / parts / norm / sheet / final |
| `scripts/build_parts.py` | 素材を写す・パーツを組む・録画をそろえる・つなぐ |
| `clips.json` | 後半で使う問題の番号と日本語の字幕 |
| `timeline.txt` | パーツの並び |
| `local.mk.example` | 元動画の場所の書き方（写して `local.mk` にする） |
| `assets/` | 素材（Git 管理外）。`teleop/` に自分の録画、`parc2026/` に写した元動画 |
| `build/` | パーツ・確認用の画像・書き出し（Git 管理外） |
