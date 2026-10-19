# 提出レポート（PDF）。手順の正本は README.md。
#
#   make preview   report.html をブラウザで開く（下書きを確かめる）
#   make draft     アカウント名なしの確認用 PDF → build/draft.pdf（ページ画像は build/pages/）
#   make pdf       Omnicampus のアカウント名と動画の URL を聞いて、提出用 PDF を作る
#                  → build/説明資料_PAI最終課題_<アカウント名>.pdf（Git 管理外）
#   make open      build/ を Finder で開く
#   make figures   図を作り直す（teleop の記録・画面録画・動画のパーツが手元に要る）

HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
ROOT := $(abspath $(HERE)../..)
PY   ?= $(ROOT)/.venv/bin/python

.PHONY: help preview draft pdf open figures clean

help:
	@echo "make preview  -- report.html をブラウザで開く"
	@echo "make draft    -- アカウント名なしの確認用 PDF（build/draft.pdf）"
	@echo "make pdf      -- アカウント名と動画の URL を聞いて提出用 PDF を作る（build/ に出る）"
	@echo "make open     -- build/ を開く"
	@echo "make figures  -- 図を作り直す"

preview:
	open $(HERE)report.html

draft:
	$(PY) $(HERE)scripts/build_pdf.py --draft

pdf:
	@$(PY) $(HERE)scripts/build_pdf.py

open:
	open $(HERE)build

figures:
	$(PY) $(HERE)scripts/make_figures.py

clean:
	rm -rf $(HERE)build
