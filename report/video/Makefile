# 最終課題の動画（YouTube）の素材をそろえて書き出す。手順と構成の正本は README.md。
#
#   make collect     # parc2026 の元動画を assets/parc2026/ へ写す（SSD をつないで、初回だけ）
#   make parts       # パーツを build/parts/ に作る（PART=compare で 1 つだけ。画面録画は clips.json の screens）
#   make norm        # assets/teleop/ に置いた録画を build/parts/teleop_*.mp4 にそろえる
#   make sheet       # パーツのコマを並べた確認用の画像を build/sheet/ に作る
#   make final       # timeline.txt の順につなぎ、build/final.mp4 に書き出す

HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
ROOT := $(abspath $(HERE)../..)
# 元動画の置き場所（VLADROP_PRE / VLADROP_POST / SMOLVLA_RUN）。local.mk.example を写して書く
-include $(HERE)local.mk
# Pillow が入っていればよい。teleop の .venv を使う
PY     ?= $(ROOT)/.venv/bin/python
SCRIPT := $(HERE)scripts/build_parts.py
PART   ?=

.PHONY: help collect parts norm sheet final voices open clean

help:
	@echo "make collect  -- parc2026 の元動画を assets/parc2026/ へ写す（local.mk に場所を書く）"
	@echo "make parts    [PART=teleop_b など] -- パーツを作る（画面録画は clips.json の screens）"
	@echo "make norm     -- assets/teleop/ の録画を 1920x1080・30fps にそろえる"
	@echo "make sheet    -- パーツのコマを並べた確認用の画像を作る"
	@echo "make final    [NARRATION=0] -- timeline.txt の順につなぎ、ナレーションを重ねて build/final.mp4 に書き出す"
	@echo "make voices   -- AivisSpeech に入っている声と style ID を並べる（アプリを起動しておく）"
	@echo "make open     -- build/final.mp4 を開く"

collect:
ifeq ($(and $(VLADROP_PRE),$(VLADROP_POST),$(SMOLVLA_RUN)),)
	$(error local.mk に VLADROP_PRE / VLADROP_POST / SMOLVLA_RUN を書く（local.mk.example を参照）)
endif
	$(PY) $(SCRIPT) collect --pre "$(VLADROP_PRE)" --post "$(VLADROP_POST)" --smolvla "$(SMOLVLA_RUN)"

parts:
	$(PY) $(SCRIPT) parts $(if $(PART),--only $(PART))

norm:
	$(PY) $(SCRIPT) norm

sheet:
	$(PY) $(SCRIPT) sheet

final:
	$(PY) $(SCRIPT) final $(if $(filter 0,$(NARRATION)),--no-narration)

voices:
	$(PY) $(SCRIPT) voices

open:
	open $(HERE)build/final.mp4

clean:
	rm -rf $(HERE)build
