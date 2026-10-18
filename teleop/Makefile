# LIBERO-Plus teleop の長い起動コマンドを 1 行にする。
# 操作方法・オプションの正本は README.md。
#
#   make setup                      # リポジトリ直下に .venv を作る（初回だけ）
#   make teleop                     # gamepad + 手首カメラ。タスク選択画面から
#   make teleop TASK=42 FLIP=z      # task 42 を直接開始、z 軸を反転
#   make clears                     # base task ごとのクリア状況と npz を一覧
#   make list FILTER=Camera
#   make render REC=<name>.npz
#   make probe                      # 軸・ボタン番号のライブ表示
#   make smoke                      # 窓を出さずに経路確認

HERE    := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
ROOT    := $(abspath $(HERE)..)
# LIBERO-plus の clone 先。teleop_libero.py も同じ環境変数を読む。
LIBERO_PLUS_ROOT ?= $(HOME)/ghq/github.com/sylvestf/LIBERO-plus
export LIBERO_PLUS_ROOT
PY      := $(ROOT)/.venv/bin/python
RUNENV  := MAGICK_HOME=/opt/homebrew/opt/imagemagick \
           DYLD_LIBRARY_PATH=/opt/homebrew/opt/imagemagick/lib
SCRIPTS := $(HERE)scripts
# cv2 と pygame が同じ libSDL2 を二重同梱していて objc が起動時に
# 「implemented in both」警告を大量に出す。実害はない（cv2=窓 / pygame=pad で
# SDL の使い所が重ならない）ので、この警告行だけ stderr から濾す。
SHELL := /bin/bash
QUIET_SDL = 2> >(grep -v "implemented in both" >&2)

# よく変える引数。それ以外は FLAGS でそのまま渡す（例 FLAGS="--max-steps 3000"）
DEVICE ?= gamepad
SUITE  ?= libero_spatial
# TASK 未指定ならタスク選択画面から始まる。番号を渡すと直接プレイ画面へ
TASK   ?=
TIME_SCALE ?= 0.6
INIT   ?= 0
CAMERA ?= frontview,robot0_eye_in_hand
# FLIP=z → --pad-flip z
FLIP   ?=
FLAGS  ?=
# list の絞り込み文字列（名前でも摂動カテゴリでも可）
FILTER ?=

TELEOP = cd $(SCRIPTS) && $(RUNENV) $(PY) teleop_libero.py \
         --device $(DEVICE) --suite $(SUITE) \
         $(if $(TASK),--task-id $(TASK)) \
         --init-index $(INIT) --time-scale $(TIME_SCALE) --camera $(CAMERA) \
         $(if $(FLIP),--pad-flip $(FLIP)) $(FLAGS) $(QUIET_SDL)

.PHONY: help setup teleop smoke list clears render probe test-input

help:
	@echo "make setup   -- $(ROOT)/.venv を作り requirements.txt を入れる（初回だけ）"
	@echo "make teleop  [DEVICE=$(DEVICE)] [SUITE=$(SUITE)] [TASK=$(TASK)] [INIT=$(INIT)]"
	@echo "             [CAMERA=$(CAMERA)] [TIME_SCALE=$(TIME_SCALE), 0:無制限] [FLIP=] [FLAGS=]"
	@echo "             SUITE は libero_spatial / libero_object / libero_goal / libero_10 / libero_90"
	@echo "make smoke   -- 窓を出さずに経路確認（--smoke 40）"
	@echo "make list    [FILTER=] -- task 一覧。名前・摂動カテゴリで絞れる"
	@echo "make clears  -- base task ごとのクリア状況と npz の一覧"
	@echo "make render  REC=<suite>/normal/<dir>/<name>.npz [CAMERA=] [FLAGS=] -- 記録を mp4 に焼く"
	@echo "make probe   -- ゲームパッドの軸・ボタン番号をライブ表示"
	@echo "make test-input -- 模擬padで入力・状態遷移を試験（窓なし）"

setup:
	cd $(ROOT) && uv venv --python 3.10 .venv && \
	    uv pip install --python .venv/bin/python -r requirements.txt

teleop:
	$(TELEOP)

smoke:
	$(TELEOP) --smoke 40

list:
	cd $(SCRIPTS) && $(RUNENV) $(PY) teleop_libero.py \
	    --suite $(SUITE) --list-tasks $(FILTER)

clears:
	cd $(SCRIPTS) && $(RUNENV) $(PY) teleop_libero.py --list-clears 2>/dev/null \
	    | grep -v "using task orders"

render:
ifndef REC
	$(error REC=<recordings/ 内の相対パス> を指定する（例 make render REC=libero_spatial/normal/t0/<name>.npz）)
endif
	cd $(SCRIPTS) && $(RUNENV) $(PY) render_recording.py \
	    ../recordings/$(REC) --camera $(CAMERA) $(FLAGS)

probe:
	$(PY) $(SCRIPTS)/pad_probe.py

# 実際の入力処理へ模擬padを渡す。窓・物理環境・録画ファイルは作らない。
test-input:
	cd $(SCRIPTS) && $(RUNENV) $(PY) -m unittest discover -s ../tests -v
