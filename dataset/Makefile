# ② teleop の記録 → LeRobot v3.0 データセット → Hugging Face。
# 手順と中身の正本は README.md。
#
#   make setup                         # .venv-lerobot を作る（初回だけ。Python 3.12 + LeRobot 0.6.0）
#   make all                           # frames → dataset → check（既定は selected/ のクリアした記録）
#   make all KIND=normal SUITE=libero_spatial TASK=0,3
#   make push                          # check に通ったデータセットを private で上げる
#   make push DRY_RUN=1                # 上げずに dataset card だけ作る

HERE    := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
ROOT    := $(abspath $(HERE)..)
# LIBERO-plus の clone 先。teleop と同じ環境変数を読む。
LIBERO_PLUS_ROOT ?= $(HOME)/ghq/github.com/sylvestf/LIBERO-plus
export LIBERO_PLUS_ROOT
# 前半（描画）は teleop の .venv（Python 3.10）、後半は .venv-lerobot（Python 3.12）
PY         ?= $(ROOT)/.venv/bin/python
LEROBOT_PY ?= $(ROOT)/.venv-lerobot/bin/python
RUNENV  := MAGICK_HOME=/opt/homebrew/opt/imagemagick \
           DYLD_LIBRARY_PATH=/opt/homebrew/opt/imagemagick/lib
SCRIPTS := $(HERE)scripts

# どの記録を使うか。既定は success かつ max_disturb_m <= 1 mm のもの
RECORDINGS ?= $(ROOT)/teleop/recordings
# selected / normal / all
KIND    ?= selected
# 空なら全部。カンマ区切りで複数（TASK は 10-12 のような範囲も可）
SUITE   ?=
TASK    ?=
# render_frames.py へそのまま渡す（例 FLAGS="--allow-failed --limit 3"）
FLAGS   ?=
# Hugging Face の dataset repo。手元では build/lerobot/<REPO_ID> に書く
REPO_ID ?= motonovix/libero-plus-teleop
WORK    ?= $(HERE)build
# push を public にするなら PUBLIC=1、上げずに確かめるだけなら DRY_RUN=1
PUBLIC  ?=
DRY_RUN ?=

.PHONY: help setup frames dataset check push all clean

help:
	@echo "make setup    -- $(ROOT)/.venv-lerobot を作る（Python 3.12 + LeRobot 0.6.0、初回だけ）"
	@echo "make frames   [KIND=$(KIND)] [SUITE=] [TASK=] [RECORDINGS=] [FLAGS=]"
	@echo "              -- 記録の state 列から 2 カメラの画像と状態を描き直す（teleop の .venv）"
	@echo "make dataset  [REPO_ID=$(REPO_ID)] -- LeRobot v3.0 に書き出す（.venv-lerobot）"
	@echo "make check    -- 読み戻して元の記録と突き合わせ、notebook のデータセットのセルを通す"
	@echo "make all      -- frames → dataset → check"
	@echo "make push     [PUBLIC=1] [DRY_RUN=1] -- check の後で Hugging Face へ上げる（既定 private）"
	@echo "make clean    -- $(WORK) を消す"

setup:
	cd $(ROOT) && uv venv --python 3.12 .venv-lerobot && \
	    uv pip install --python .venv-lerobot/bin/python -r dataset/requirements.txt

frames:
	$(RUNENV) $(PY) $(SCRIPTS)/render_frames.py --recordings $(RECORDINGS) --kind $(KIND) \
	    $(if $(SUITE),--suite $(SUITE)) $(if $(TASK),--task $(TASK)) --out $(WORK) $(FLAGS)

dataset:
	$(LEROBOT_PY) $(SCRIPTS)/build_dataset.py --repo-id $(REPO_ID) --work $(WORK) --overwrite

check:
	$(LEROBOT_PY) $(SCRIPTS)/check_dataset.py --repo-id $(REPO_ID) --work $(WORK) --sheet $(WORK)/sheet.png

all: frames dataset check

push: check
	$(LEROBOT_PY) $(SCRIPTS)/push_dataset.py --repo-id $(REPO_ID) --work $(WORK) \
	    $(if $(PUBLIC),--public) $(if $(DRY_RUN),--dry-run)

clean:
	rm -rf $(WORK)
