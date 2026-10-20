"""report.html を A4 の PDF にする（Chrome のヘッドレス印刷）。

    --draft  アカウント名を入れずに確認用の build/draft.pdf を作る
    （なし）  Omnicampus のアカウント名と動画の URL を入れて、提出用の
             build/説明資料_PAI最終課題_<アカウント名>.pdf を作る

アカウント名と URL は、環境変数 → local.env（Git 管理外、local.env.example を参照）の順に探し、
どちらにも無ければ入力で聞く。PDF の Omnicampus の欄には OMNICAMPUS_IN_PDF を書く（無ければアカウント名）。
ファイル名はいつもアカウント名を使う。

アカウント名と URL は report.html には書かない。data-fill の要素を置き換えた HTML を build/ に作り、
そこから印刷する（build/ は Git 管理外）。最後にページ数を数え、各ページを build/pages/ に画像で残す。
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
BUILD = HERE / "build"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def local_settings() -> dict[str, str]:
    """local.env（KEY=値 の行）を読む。無ければ空。"""
    path = HERE / "local.env"
    if not path.exists():
        return {}
    settings = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    return settings


def ask(prompt: str, env: str, given: str | None, required: bool) -> str:
    value = given if given is not None else os.environ.get(env)
    if value is None:
        if not sys.stdin.isatty():
            raise SystemExit(f"{prompt}を入力できない（端末から make pdf を実行する。または環境変数 {env}）")
        value = input(f"{prompt}: ")
    value = value.strip()
    if required and not value:
        raise SystemExit(f"{prompt}が空")
    return value


def fill(source: str, values: dict[str, str]) -> str:
    """<span data-fill="名前">…</span> の中身を値で置き換える。値が空ならそのまま残す。"""
    def replace(m: re.Match) -> str:
        value = values.get(m.group(2))
        if not value:
            return m.group(0)
        if value.startswith(("https://", "http://")):
            inner = f'<a href="{html.escape(value)}">{html.escape(value)}</a>'
        else:
            inner = html.escape(value)
        return f"{m.group(1)}{inner}{m.group(3)}"
    return re.sub(r'(<span data-fill="([\w-]+)">).*?(</span>)', replace, source)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--draft", action="store_true", help="アカウント名なしの確認用 PDF を作る")
    args = ap.parse_args()
    if not CHROME.exists():
        raise SystemExit(f"Chrome が無い: {CHROME}")
    BUILD.mkdir(exist_ok=True)
    source = (HERE / "report.html").read_text()
    if args.draft:
        name, filled = "draft", source
    else:
        local = local_settings()
        account = ask("Omnicampus のアカウント名", "OMNICAMPUS_ID",
                      os.environ.get("OMNICAMPUS_ID") or local.get("OMNICAMPUS_ID") or None, required=True)
        if re.search(r'[/\\:*?"<>|]', account):
            raise SystemExit("アカウント名にファイル名に使えない文字がある")
        if "OMNICAMPUS_ID" in os.environ or local.get("OMNICAMPUS_ID"):
            # アカウント名を設定から取ったときは聞かない（URL も設定にあれば使い、無ければ空のまま）
            youtube = os.environ.get("YOUTUBE_URL", local.get("YOUTUBE_URL", ""))
        else:
            youtube = ask("YouTube の動画の URL（まだなら空のまま Enter）", "YOUTUBE_URL", None, required=False)
        shown = os.environ.get("OMNICAMPUS_IN_PDF") or local.get("OMNICAMPUS_IN_PDF") or account
        name = f"説明資料_PAI最終課題_{account}"
        filled = fill(source, {"omnicampus-id": shown, "youtube-url": youtube})
    # build/ から report.css と figures/ を読めるよう、基準の場所を report/doc/ にする
    filled = filled.replace("<head>", '<head>\n<base href="../">', 1)
    page = BUILD / f"{name}.html"
    page.write_text(filled)
    pdf = BUILD / f"{name}.pdf"
    subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--user-data-dir={BUILD / 'chrome'}", f"--print-to-pdf={pdf}", page.as_uri()],
                   check=True, capture_output=True)
    pages = BUILD / "pages"
    shutil.rmtree(pages, ignore_errors=True)
    pages.mkdir()
    count = subprocess.run(
        ["uv", "run", "--quiet", "--with", "pymupdf", "python", "-c",
         "import pymupdf, sys\n"
         "d = pymupdf.open(sys.argv[1])\n"
         "[d[i].get_pixmap(dpi=110).save(f'{sys.argv[2]}/page{i + 1}.png') for i in range(d.page_count)]\n"
         "print(d.page_count)", str(pdf), str(pages)],
        check=True, capture_output=True, text=True).stdout.strip()
    print(f"{pdf.relative_to(HERE)}  {count} ページ（各ページの画像は build/pages/）")


if __name__ == "__main__":
    main()
