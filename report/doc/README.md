# 提出レポート（PDF）

Omnicampus に出す説明資料。下書きは [report.html](report.html)（見た目は [report.css](report.css)）で、ブラウザで確かめてから PDF にする。

## 手順

`report/doc/` で実行する。

```bash
make preview   # report.html をブラウザで開く
make draft     # アカウント名なしの確認用 PDF → build/draft.pdf（各ページの画像は build/pages/）
make pdf       # 提出用 PDF を作る（下を参照）
make open      # build/ を Finder で開く
```

`make pdf` は Omnicampus のアカウント名と YouTube の動画の URL を聞き、
`build/説明資料_PAI最終課題_<アカウント名>.pdf` を作る（ファイル名は課題の指定どおり）。

- アカウント名と URL は `report.html` には書かない。`data-fill` の要素を置き換えた HTML を `build/` に作り、そこから印刷する。
  `build/` は Git 管理外なので、公開リポジトリには入らない。
- コマンドの履歴に残らないよう、引数ではなく入力で受け取る（環境変数 `OMNICAMPUS_ID`・`YOUTUBE_URL` でも渡せる）。
- 動画の URL は、まだなければ空のまま Enter でよい（「提出時に記入」のまま残る）。
- PDF は Chrome のヘッドレス印刷で作る。最後にページ数を表示する。

## 中身

| 節 | 内容 |
| :--- | :--- |
| 1. 概要 | 何をしたか、①〜④ の流れ |
| 2. ゲームコントローラならではの利点 | ① 6 軸を同時に動かせる ② 時間スケールと速さを分けて、実時間より速い教師データを作れる ③ 回転のかかり具合が円の針で分かる |
| 3. 学習と評価の結果 | SmolVLA と π0.5 |
| 4. 今後 | ROS2 で実機へ、接触と汎化 |
| 5. 使ったもの | ライセンスと商標 |

2 の数字は、動画で見せた 1 本（タスク 1147「ボウルをコンロに載せる」）の teleop の記録から出した。
`make figures` を実行すると、図と一緒に数字も表示されるので、本文と食い違っていないか確かめる。

## 図

`figures/` は [scripts/make_figures.py](scripts/make_figures.py) で作り、Git に入れている。元の素材は Git に入れていないので、作り直せるのは手元だけ。

| 図 | 元 |
| :--- | :--- |
| `controller.png` | 動画の操作方法のパーツ（`report/video/build/parts/controller.mp4`） |
| `actions.png` | teleop の記録（`teleop/recordings/libero_goal/normal/t1147/`） |
| `hud.png`・`speed_time.png` | 画面録画（`report/video/assets/teleop/b2.mov` の 9 秒目） |
