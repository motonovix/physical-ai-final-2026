"""最終課題の動画の素材をそろえ、パーツを組んで書き出す。

    collect  parc2026 の元動画を assets/parc2026/ へ写す（SSD をつないで一度だけ）
    parts    assets/ の素材からパーツを build/parts/ に作る（画面録画は clips.json の screens）
    norm     assets/teleop/ の録画を、パーツと同じ形式にそろえて build/parts/ に置く
    sheet    パーツのコマを並べた確認用の画像を build/sheet/ に作る
    final    timeline.txt の順にパーツをつなぎ、build/final.mp4 に書き出す
    thumbnail  YouTube のサムネイルを build/thumbnail.jpg に作る（clips.json の thumbnail）

パーツはすべて 1920x1080・30fps・H.264 / AAC 48 kHz ステレオ（無音の音声つき）にそろえる。
手順と構成の正本は ../README.md。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

HERE = Path(__file__).resolve().parents[1]
ASSETS = HERE / "assets"
BUILD = HERE / "build"
PARTS = BUILD / "parts"
TMP = BUILD / "tmp"

W, H, FPS = 1920, 1080, 30
BG = (16, 18, 23)
FG = (240, 240, 240)
SUB = (150, 156, 166)
OK = (40, 150, 85)
OK_TEXT = (90, 200, 125)          # 暗い背景の上で読める緑
NG = (190, 55, 55)
FONT_DIR = Path("/System/Library/Fonts")
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
ROOT = HERE.parents[1]

VCODEC = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
          "-pix_fmt", "yuv420p", "-r", str(FPS)]
ACODEC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
SILENCE = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]


# ---------------------------------------------------------------- ffmpeg

def ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def duration(path: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)], text=True)
    return float(out.strip())


def video_duration(path: Path) -> float:
    """映像のストリームの長さ（コマ数 / fps）。"""
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                                   "-show_entries", "stream=nb_read_packets,r_frame_rate", "-of", "csv=p=0",
                                   str(path)], text=True).strip().split(",")
    num, den = map(int, out[0].split("/"))
    return int(out[1]) * den / num


def has_audio(path: Path) -> bool:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a",
                                   "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], text=True)
    return bool(out.strip())


def compose(out: Path, bg: Path, videos: list[tuple], badges: list[tuple], total: float,
            fade_in: float = 0.0, fade_out: float = 0.0) -> None:
    """背景の上に動画と札を重ねる。

    videos: (path, speed, (x, y), (w, h)[, 前処理])。最後のコマで止めたまま total まで伸ばす。
        前処理は trim・crop など、速さを変える前に掛ける filter（省略可）。元の音声は使わない。
    badges: (png, (x, y), 出す時刻 s)。時刻を (出す s, 消す s) にすると、その間だけ出す。
    fade_in / fade_out: 頭を黒から浮かび上がらせる秒数 / 終わりを黒へ沈める秒数。
    """
    t = f"{total:.3f}"
    args = ["-loop", "1", "-framerate", FPS, "-t", t, "-i", bg]
    for path, *_ in videos:
        args += ["-i", path]
    for png, *_ in badges:
        args += ["-loop", "1", "-framerate", FPS, "-t", t, "-i", png]
    args += SILENCE
    chains, last = [], "0:v"
    for i, (_, speed, (x, y), (w, h), *pre) in enumerate(videos, 1):
        head = f"{pre[0]}," if pre else ""
        chains.append(f"[{i}:v]{head}setpts=(PTS-STARTPTS)/{speed},fps={FPS},scale={w}:{h}:flags=lanczos,"
                      f"tpad=stop_mode=clone:stop_duration={t}[v{i}]")
        chains.append(f"[{last}][v{i}]overlay={x}:{y}[o{i}]")
        last = f"o{i}"
    first = 1 + len(videos)
    for j, (_, (x, y), when) in enumerate(badges):
        cond = f"between(t,{when[0]:.3f},{when[1]:.3f})" if isinstance(when, tuple) else f"gte(t,{when:.3f})"
        chains.append(f"[{last}][{first + j}:v]overlay={x}:{y}:enable='{cond}'[b{j}]")
        last = f"b{j}"
    fade = f"fade=t=in:st=0:d={fade_in:.3f}," if fade_in else ""
    if fade_out:
        fade += f"fade=t=out:st={total - fade_out:.3f}:d={fade_out:.3f},"
    chains.append(f"[{last}]{fade}format=yuv420p[vout]")
    audio = first + len(badges)
    ffmpeg([*args, "-filter_complex", ";".join(chains), "-map", "[vout]", "-map", f"{audio}:a",
            "-t", t, *VCODEC, *ACODEC, "-movflags", "+faststart", out])
    print(f"wrote {out.relative_to(HERE)}  {total:.1f} s")


# ---------------------------------------------------------------- 文字

def font(weight: int, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / f"ヒラギノ角ゴシック W{weight}.ttc"), size)


NO_HEAD = set("、。，．）」』】・ー")   # 行の頭に来させない文字


def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """英単語は切らずに、日本語は文字で折り返す。「\n」はそこで改行する。"""
    lines = []
    for para in text.split("\n"):
        units = re.findall(r"[!-~]+|\s+|.", para)   # 英数字の並び・空白・それ以外の 1 文字
        cur = ""
        for u in units:
            trial = cur + u
            if f.getlength(trial.strip()) <= width or not cur.strip() or u.isspace() or u in NO_HEAD:
                cur = trial
            else:
                lines.append(cur.strip())
                cur = u
        lines.append(cur.strip())
    return lines


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def header(d: ImageDraw.ImageDraw, title: str, ja: str = "", en: str = "", note: str = "") -> None:
    d.text((80, 56), title, font=font(6, 52), fill=FG)
    if ja:
        d.text((80, 136), ja, font=font(3, 34), fill=FG)
    if en:
        d.text((80, 186), en, font=font(3, 24), fill=SUB)
    if note:
        d.text((W - 80, 186), note, font=font(3, 26), fill=SUB, anchor="ra")


def badge(path: Path, text: str, color: tuple, size: int = 44) -> tuple[Path, int, int]:
    f = font(6, size)
    pad_x, pad_y = size * 0.7, size * 0.35
    w = int(f.getlength(text) + 2 * pad_x)
    h = int(size + 2 * pad_y)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 4, fill=(*color, 235))
    d.text((w / 2, h / 2), text, font=f, fill=(255, 255, 255), anchor="mm")
    img.save(path)
    return path, w, h


def caption(path: Path, text: str, max_w: int, size: int = 38) -> tuple[Path, int, int]:
    """画面録画に重ねるテロップ。半透明の黒い帯に白い文字、max_w を超えたら折り返す。"""
    f = font(6, size)
    pad_x, pad_y, gap = 36, 18, int(size * 0.45)
    lines = wrap(text, f, max_w - 2 * pad_x)
    w = int(max(f.getlength(line) for line in lines) + 2 * pad_x)
    h = int(len(lines) * size + (len(lines) - 1) * gap + 2 * pad_y)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=14, fill=(8, 10, 14, 215))
    for i, line in enumerate(lines):
        d.text((w / 2, pad_y + i * (size + gap)), line, font=f, fill=(255, 255, 255), anchor="ma")
    img.save(path)
    return path, w, h


def result_text(ep: dict, seconds: float) -> tuple[str, tuple]:
    if ep["success"] and not ep["collision"]:
        return f"成功  {seconds:.1f} 秒", OK
    if ep["success"]:
        return f"成功（ほかの物も動いた）  {seconds:.1f} 秒", OK
    return f"失敗  {seconds:.0f} 秒で打ち切り", NG


# ---------------------------------------------------------------- collect

def collect(args: argparse.Namespace) -> None:
    clips = json.loads((HERE / "clips.json").read_text())
    want = {"vladrop_pre": {clips["compare"]["task"]},
            "vladrop_post": {clips["compare"]["task"], *clips["successes"]}}
    for name, set_dir in (("vladrop_pre", args.pre), ("vladrop_post", args.post)):
        dst = ASSETS / "parc2026" / name
        dst.mkdir(parents=True, exist_ok=True)
        paths = sorted(Path(set_dir).glob("runs/*/t*-e000/episode.json"))
        if not paths:
            raise SystemExit(f"{set_dir} に runs/*/t*-e000/episode.json が無い")
        eps = {json.loads(p.read_text())["task_id"]: p for p in paths}
        rows = [json.loads(p.read_text()) for p in paths]
        summary = {"episodes": len(rows),
                   "success": sum(r["success"] for r in rows),
                   "safe_success": sum(r["safe_success"] for r in rows)}
        (dst / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
        for task in sorted(want[name]):
            ep = json.loads(eps[task].read_text())
            shutil.copy2(eps[task].parents[1] / ep["video"], dst / f"t{task:03d}.mp4")
            keep = ("task_id", "instruction", "success", "collision", "safe_success", "collision_step")
            (dst / f"t{task:03d}.json").write_text(
                json.dumps({k: ep.get(k) for k in keep}, ensure_ascii=False, indent=1) + "\n")
        print(f"{name}: {summary}  tasks={sorted(want[name])}")
    dst = ASSETS / "parc2026" / "smolvla"
    dst.mkdir(parents=True, exist_ok=True)
    src = Path(args.smolvla)
    shutil.copy2(src / "rollout.mp4", dst / "rollout.mp4")
    res = json.loads((src / "result.json").read_text())
    keep = ("task", "init", "seed", "steps", "first_goal_step", "success", "max_disturb_m",
            "n_action_steps", "control_freq", "language")
    (dst / "result.json").write_text(json.dumps({k: res.get(k) for k in keep}, ensure_ascii=False, indent=1) + "\n")
    print(f"smolvla: success={res['success']} steps={res['steps']}")


# ---------------------------------------------------------------- parts

def part_smolvla(clips: dict) -> None:
    src = ASSETS / "parc2026" / "smolvla"
    res = json.loads((src / "result.json").read_text())
    video = src / "rollout.mp4"
    d = duration(video)
    img, dr = canvas()
    header(dr, f"SmolVLA を人の teleop {clips['smolvla']['episodes']} 本で追加学習（LoRA）",
           clips["smolvla"]["caption"], res["language"], note="学習に使った初期配置での 1 試行・等倍速")
    x, y, w, h = 240, 250, 1440, 720
    for cx, label in ((x + w // 4, "正面カメラ"), (x + 3 * w // 4, "手首カメラ")):
        dr.text((cx, y + h + 34), label, font=font(3, 30), fill=SUB, anchor="mm")
    bg = TMP / "smolvla_bg.png"
    img.save(bg)
    text = f"成功  {res['first_goal_step'] / res['control_freq']:.1f} 秒でゴール" if res["success"] else "失敗"
    png, bw, bh = badge(TMP / "smolvla_badge.png", text, OK if res["success"] else NG)
    compose(PARTS / "smolvla.mp4", bg, [(video, 1.0, (x, y), (w, h))],
            [(png, (x + (w - bw) // 2, y + h - bh - 30), d)], d + 2.0)


def part_compare(clips: dict) -> None:
    task, speed = clips["compare"]["task"], clips["compare"]["speed"]
    pre, post = (ASSETS / "parc2026" / s / f"t{task:03d}" for s in ("vladrop_pre", "vladrop_post"))
    ep_pre, ep_post = (json.loads(p.with_suffix(".json").read_text()) for p in (pre, post))
    d_pre, d_post = duration(pre.with_suffix(".mp4")), duration(post.with_suffix(".mp4"))
    total = max(d_pre, d_post) / speed + 2.0
    img, dr = canvas()
    header(dr, "追加学習の前と後（同じタスク・同じ初期配置）", clips["captions"][str(task)],
           ep_post["instruction"], note=f"π0.5・{speed:g} 倍速")
    size, gap = 720, 80
    xs = [(W - 2 * size - gap) // 2, (W + gap) // 2]
    y = 250
    for x, label in zip(xs, ("追加学習の前", "追加学習の後")):
        dr.text((x + size // 2, y + size + 40), label, font=font(6, 36), fill=FG, anchor="mm")
    bg = TMP / "compare_bg.png"
    img.save(bg)
    videos, badges = [], []
    for x, path, ep, d, tag in ((xs[0], pre, ep_pre, d_pre, "pre"), (xs[1], post, ep_post, d_post, "post")):
        videos.append((path.with_suffix(".mp4"), speed, (x, y), (size, size)))
        text, color = result_text(ep, d)
        png, bw, bh = badge(TMP / f"compare_{tag}.png", text, color, size=40)
        badges.append((png, (x + (size - bw) // 2, y + size - bh - 24), d / speed))
    compose(PARTS / "compare.mp4", bg, videos, badges, total)


def part_successes(clips: dict) -> None:
    tasks = clips["successes"]
    n, gap = len(tasks), 40
    size = min(420, (W - 160 - (n - 1) * gap) // n)
    x0 = (W - n * size - (n - 1) * gap) // 2
    y = 330
    img, dr = canvas()
    header(dr, "追加学習の後：解けたタスクの例", "π0.5 を、逆向きのタスクの教師データで追加学習",
           note="等倍速・各 1 試行")
    videos, badges = [], []
    for i, task in enumerate(tasks):
        path = ASSETS / "parc2026" / "vladrop_post" / f"t{task:03d}"
        ep = json.loads(path.with_suffix(".json").read_text())
        d = duration(path.with_suffix(".mp4"))
        x = x0 + i * (size + gap)
        ty = y + size + 28
        for line in wrap(clips["captions"][str(task)], font(6, 28), size):
            dr.text((x, ty), line, font=font(6, 28), fill=FG)
            ty += 40
        ty += 8
        for line in wrap(ep["instruction"], font(3, 20), size):
            dr.text((x, ty), line, font=font(3, 20), fill=SUB)
            ty += 28
        videos.append((path.with_suffix(".mp4"), 1.0, (x, y), (size, size)))
        text, color = result_text(ep, d)
        png, bw, bh = badge(TMP / f"success_{task:03d}.png", text, color, size=30)
        badges.append((png, (x + (size - bw) // 2, y + size - bh - 16), d))
    bg = TMP / "successes_bg.png"
    img.save(bg)
    total = max(t0 for *_, t0 in badges) + 2.0
    compose(PARTS / "successes.mp4", bg, videos, badges, total)


def part_results() -> None:
    pre, post = (json.loads((ASSETS / "parc2026" / s / "summary.json").read_text())
                 for s in ("vladrop_pre", "vladrop_post"))
    img, dr = canvas()
    header(dr, "結果：逆向きの 16 タスク（各 1 試行）",
           "元の LIBERO のタスクを逆向きにした 8 種 × 指示文 2 通り（原文・言い換え）", note="π0.5")
    for cx, label, s, color in ((W // 4 + 80, "追加学習の前", pre, NG), (3 * W // 4 - 80, "追加学習の後", post, OK)):
        dr.text((cx, 380), label, font=font(6, 48), fill=FG, anchor="mm")
        dr.text((cx, 560), f"{s['success']} / {s['episodes']}", font=font(6, 180), fill=color, anchor="mm")
        dr.text((cx, 720), f"ほかの物を動かさずに成功：{s['safe_success']} タスク", font=font(3, 36), fill=SUB, anchor="mm")
    dr.text((W // 2, 960), "成功：BDDL のゴール条件が成り立つ。ほかの物：対象外の物体が 1 mm 以上動いたかどうか",
            font=font(3, 28), fill=SUB, anchor="mm")
    bg = TMP / "results_bg.png"
    img.save(bg)
    compose(PARTS / "results.mp4", bg, [], [], 5.0)


def window_box(path: Path, start: float, end: float) -> tuple[int, int, int, int]:
    """録画のうち、黒い余白を除いた窓の範囲（w, h, x, y）。範囲内で一番多く出た値を使う。"""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(path),
                          "-vf", "cropdetect=limit=16:round=2:reset=0", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    boxes = [line.rsplit("crop=", 1)[1].strip() for line in out.splitlines() if "crop=" in line]
    if not boxes:
        raise SystemExit(f"{path.name}: 窓の範囲が分からない")
    w, h, x, y = map(int, max(set(boxes), key=boxes.count).split(":"))
    return w, h, x, y


def part_screen(name: str, cfg: dict) -> None:
    """自分で撮った画面録画を、窓だけ切り出して見出しの下に置く。元の音声は外す。"""
    src = ASSETS / "teleop" / cfg["src"]
    start, end = cfg.get("start", 0.0), cfg.get("end") or duration(src)
    speed, hold = cfg.get("speed", 1.0), cfg.get("hold", 0.5)
    cw, ch, cx, cy = window_box(src, start, end)
    top, bottom, side = 170, 1050, 40
    scale = min((W - 2 * side) / cw, (bottom - top) / ch)
    w, h = int(cw * scale) // 2 * 2, int(ch * scale) // 2 * 2
    x, y = (W - w) // 2, top + (bottom - top - h) // 2
    img, dr = canvas()
    dr.text((80, 40), cfg["title"], font=font(6, 46), fill=FG)
    if cfg.get("sub"):
        dr.text((80, 104), cfg["sub"], font=font(3, 30), fill=SUB)
    note = cfg.get("note") or (f"{speed:g} 倍速" if speed != 1 else "等倍速")
    dr.text((W - 80, 48), note, font=font(3, 28), fill=SUB, anchor="ra")
    bg = TMP / f"{name}_bg.png"
    img.save(bg)
    total = (end - start) / speed + hold
    badges = []
    for i, cap in enumerate(cfg.get("captions", [])):
        png, bw, bh = caption(TMP / f"{name}_cap{i}.png", cap["text"], min(w - 80, 1500))
        badges.append((png, ((W - bw) // 2, y + h - bh - 28), (cap["from"], cap.get("to", total))))
    pre = f"trim=start={start:.3f}:end={end:.3f},crop={cw}:{ch}:{cx}:{cy}"
    compose(PARTS / f"{name}.mp4", bg, [(src, speed, (x, y), (w, h), pre)], badges, total)


def frame_of(src: Path, at: float, out: Path) -> Image.Image:
    """画面録画の at 秒のコマを、窓だけ切り出して取る。"""
    cw, ch, cx, cy = window_box(src, max(at - 1.0, 0.0), at + 1.0)
    ffmpeg(["-ss", f"{at:.3f}", "-i", src, "-frames:v", "1", "-vf", f"crop={cw}:{ch}:{cx}:{cy}", out])
    return Image.open(out).convert("RGB")


def cover(img: Image.Image) -> Image.Image:
    """画面いっぱいに広げ、はみ出た分を切る。"""
    scale = max(W / img.width, H / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    x, y = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((x, y, x + W, y + H))


def part_title(cfg: dict, name: str = "title") -> None:
    """題名の画面（締めの画面にも使う）。背景に teleop の画面録画のコマをぼかして暗く敷く。

    sub（副題）・url・foot（小さな添え書き）は、あるものだけ書く。
    """
    bg_cfg = cfg.get("bg")
    if bg_cfg and (ASSETS / "teleop" / bg_cfg["src"]).exists():
        shot = frame_of(ASSETS / "teleop" / bg_cfg["src"], bg_cfg.get("at", 0.0), TMP / f"{name}_shot.png")
        img = cover(shot).filter(ImageFilter.GaussianBlur(14))
        img = ImageEnhance.Brightness(img).enhance(0.32)
    else:
        img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    lead, main = cfg["main"]
    d.text((W // 2, 400), lead, font=font(6, 64), fill=FG, anchor="mm")
    d.text((W // 2, 535), main, font=font(8, 136), fill=FG, anchor="mm")
    d.line((W // 2 - 360, 650, W // 2 + 360, 650), fill=SUB, width=2)
    y = 712
    if cfg.get("sub"):
        d.text((W // 2, y), cfg["sub"], font=font(3, 38), fill=FG, anchor="mm")
        y += 70
    if cfg.get("url"):
        d.text((W // 2, y), cfg["url"], font=font(6, 44), fill=(92, 170, 220), anchor="mm")
        y += 80
    for line in (cfg.get("foot") or "").split("\n"):     # 添え書きは改行で複数行にできる
        if line:
            d.text((W // 2, y), line, font=font(3, 30), fill=SUB, anchor="mm")
            y += 44
    bg = TMP / f"{name}_bg.png"
    img.save(bg)
    compose(PARTS / f"{name}.mp4", bg, [], [], cfg.get("seconds", 2.0),
            fade_in=cfg.get("fade_in", 0.3), fade_out=cfg.get("fade_out", 0.0))


def part_overview(cfg: dict, name: str = "overview") -> None:
    """流れの図。見出しの後、四角を左から順に出す。四角の上の印は mark（既定は ①②…）。"""
    img, dr = canvas()
    size = 50
    while font(6, size).getlength(cfg["lead"]) > W - 200 and size > 34:
        size -= 2
    dr.text((W // 2, 215), cfg["lead"], font=font(6, size), fill=FG, anchor="mm")
    steps = cfg["steps"]
    n, box_w, gap, box_h, top = len(steps), cfg.get("box_w", 380), cfg.get("gap", 80), 330, 400
    x0 = (W - n * box_w - (n - 1) * gap) // 2
    accent = (92, 170, 220)
    badges = []
    for i, step in enumerate(steps):
        x = x0 + i * (box_w + gap)
        layer = Image.new("RGBA", (box_w + gap, box_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        bx = gap if i else 0          # 2 つ目からは左の矢印ごと出す
        dashed = step.get("dashed", False)                # 点線の四角：まだやっていないこと
        line_c = (110, 116, 128) if dashed else SUB
        box_c = (110, 116, 128) if dashed else accent
        name_c = (175, 180, 190) if dashed else FG
        if i:
            ay = box_h // 2
            if dashed:
                for ax in range(10, gap - 30, 14):
                    d.line((ax, ay, min(ax + 7, gap - 30), ay), fill=line_c, width=5)
            else:
                d.line((10, ay, gap - 22, ay), fill=line_c, width=5)
            d.polygon([(gap - 10, ay), (gap - 30, ay - 14), (gap - 30, ay + 14)], fill=line_c)
        if dashed:
            d.rounded_rectangle((bx, 0, bx + box_w - 1, box_h - 1), radius=22, fill=(22, 25, 31, 255))
            for k in range(0, box_w, 22):                 # 上下の辺
                d.line((bx + k, 1, bx + min(k + 12, box_w - 1), 1), fill=box_c, width=3)
                d.line((bx + k, box_h - 2, bx + min(k + 12, box_w - 1), box_h - 2), fill=box_c, width=3)
            for k in range(0, box_h, 22):                 # 左右の辺
                d.line((bx + 1, k, bx + 1, min(k + 12, box_h - 1)), fill=box_c, width=3)
                d.line((bx + box_w - 2, k, bx + box_w - 2, min(k + 12, box_h - 1)), fill=box_c, width=3)
        else:
            d.rounded_rectangle((bx, 0, bx + box_w - 1, box_h - 1), radius=22, fill=(30, 34, 42, 255),
                                outline=box_c, width=3)
        cx = bx + box_w // 2
        mark = step.get("mark") or "①②③④⑤⑥"[i]
        d.text((cx, 62), mark, font=font(6, 54 if len(mark) == 1 else 34), fill=box_c, anchor="mm")
        size = 40
        while font(6, size).getlength(step["name"]) > box_w - 40 and size > 26:
            size -= 2
        d.text((cx, 130), step["name"], font=font(6, size), fill=name_c, anchor="mm")
        ty = 190
        for note in step.get("notes", []):
            size = 28
            while font(3, size).getlength(note) > box_w - 30 and size > 20:
                size -= 1
            d.text((cx, ty), note, font=font(3, size), fill=SUB, anchor="mm")
            ty += 38
        if step.get("result"):
            size = 32
            while font(6, size).getlength(step["result"]) > box_w - 30 and size > 20:
                size -= 1
            d.text((cx, box_h - 44), step["result"], font=font(6, size),
                   fill=(175, 180, 190) if dashed else OK_TEXT, anchor="mm")
        png = TMP / f"{name}_{i}.png"
        layer.save(png)
        badges.append((png, (x - bx, top), step.get("at", 0.4 + 0.6 * i)))
    if cfg.get("foot"):
        dr.text((W // 2, top + box_h + 130), cfg["foot"], font=font(3, 34), fill=SUB, anchor="mm")
    bg = TMP / f"{name}_bg.png"
    img.save(bg)
    compose(PARTS / f"{name}.mp4", bg, [], badges, cfg.get("seconds", 6.0))


NOTEBOOK_CSS = """
body { margin: 0; background: #1f1f1f; color: #e3e3e3; font-family: "Hiragino Sans", sans-serif; }
.bar { height: 52px; display: flex; align-items: center; padding: 0 22px; background: #2b2b2b;
       border-bottom: 1px solid #3c3c3c; font-size: 21px; }
.bar .dot { width: 26px; height: 26px; border-radius: 6px; background: #f9ab00; margin-right: 14px; }
.cells { padding: 18px 26px; }
.md { font-size: 24px; font-weight: 600; margin: 10px 4px 8px; }
.cell { display: flex; margin: 0 0 14px; }
.run { width: 44px; flex: none; color: #9aa0a6; font: 20px Menlo, monospace; padding-top: 14px; }
.code { flex: 1; background: #2a2a2a; border: 1px solid #3c3c3c; border-radius: 6px; padding: 12px 18px; }
.code pre { margin: 0; font: 18px/1.45 Menlo, "Hiragino Sans", monospace; white-space: pre; }
.more { color: #8a8f98; }
"""


def notebook_lines(nb: dict, spec: dict) -> str:
    """セル spec["cell"] の、from を含む行から to を含む行までを取り出す（抜粋なら末尾に … を足す）。"""
    lines = "".join(nb["cells"][spec["cell"]]["source"]).splitlines()
    start = next(i for i, line in enumerate(lines) if spec["from"] in line)
    end = next(i for i, line in enumerate(lines) if i >= start and spec["to"] in line)
    picked = lines[start:end + 1]
    if spec.get("more"):
        indent = len(picked[-1]) - len(picked[-1].lstrip())
        picked.append(" " * indent + "...")
    return "\n".join(picked)


def part_code(cfg: dict) -> None:
    """notebook のセルを抜粋し、Colab 風の画面にして見出しの下に置く（Chrome で描く）。"""
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import PythonLexer

    nb = json.loads((ROOT / cfg["notebook"]).read_text())
    fmt = HtmlFormatter(style="github-dark", nowrap=True)
    body = []
    for spec in cfg["cells"]:
        cell = nb["cells"][spec["cell"]]
        if cell["cell_type"] == "markdown":
            heading = "".join(cell["source"]).splitlines()[0].lstrip("# ")
            body.append(f'<div class="md">{heading}</div>')
        else:
            code = highlight(notebook_lines(nb, spec), PythonLexer(), fmt)
            body.append(f'<div class="cell"><div class="run">[ ]</div><div class="code"><pre>{code}</pre></div></div>')
    top, bottom, side = 150, 955, 60       # 枠の下（955〜1080）にテロップを出す
    pw, ph = W - 2 * side, bottom - top
    html = (f"<!doctype html><meta charset='utf-8'><style>{NOTEBOOK_CSS}{fmt.get_style_defs('.code')}"
            f".code {{ background: #2a2a2a; }}</style>"
            f"<div class='bar'><div class='dot'></div>{cfg['doc_title']}</div><div class='cells'>{''.join(body)}</div>")
    page = TMP / "code.html"
    page.write_text(html)
    shot = TMP / "code_shot.png"
    subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={TMP / 'chrome'}",
                    "--force-device-scale-factor=2", f"--window-size={pw},{ph}", f"--screenshot={shot}", page.as_uri()],
                   check=True, capture_output=True)
    panel = Image.open(shot).convert("RGB").resize((pw, ph), Image.LANCZOS)
    img, dr = canvas()
    dr.text((80, 40), cfg["title"], font=font(6, 46), fill=FG)
    if cfg.get("note"):
        dr.text((W - 80, 48), cfg["note"], font=font(3, 28), fill=SUB, anchor="ra")
    img.paste(panel, (side, top))
    bg = TMP / "code_bg.png"
    img.save(bg)
    total = cfg.get("seconds", 6.5)
    badges = []
    for i, cap in enumerate(cfg.get("captions", [])):
        png, bw, bh = caption(TMP / f"code_cap{i}.png", cap["text"], 1500)
        badges.append((png, ((W - bw) // 2, bottom + (H - bottom - bh) // 2), (cap["from"], cap.get("to", total))))
    compose(PARTS / "code.mp4", bg, [], badges, total)


def callout(d: ImageDraw.ImageDraw, label: dict, point: tuple, color: tuple, dot: bool = True) -> None:
    """吹き出し：at に見出し（色）と説明（白、text は文字列か行のリスト）を書き、point へ線を引く。"""
    ax, ay = label["at"]
    left = label["side"] == "left"
    anchor = ("r" if left else "l") + "s"
    d.text((ax, ay), label["head"], font=font(6, 32), fill=color, anchor=anchor)
    lines = label["text"] if isinstance(label["text"], list) else [label["text"]]
    for i, line in enumerate(lines):
        d.text((ax, ay + 40 + 38 * i), line, font=font(3, 28), fill=FG, anchor=anchor)
    sx, sy = ax + (14 if left else -14), ay - 10
    tx, ty = point
    d.line((sx, sy, tx, ty), fill=(0, 0, 0, 200), width=8)
    d.line((sx, sy, tx, ty), fill=color, width=4)
    if dot:
        d.ellipse((tx - 11, ty - 11, tx + 11, ty + 11), fill=color, outline=(255, 255, 255), width=3)


def part_controller(cfg: dict) -> None:
    """コントローラの写真に、操作の割り当てを色分けして書き込む。

    写真は crop（元画像の px）で切り出して置く。注釈は groups ごとの層にして、at 秒で順に出す。
    target は元画像の px か、肩のボタンの名前（写真に写らないので、上に四角で描き足す）。
    """
    src = ASSETS / "photo" / cfg["src"]
    photo = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    cx0, cy0, cx1, cy1 = cfg["crop"]
    px, py, pw = cfg["photo_xy"][0], cfg["photo_xy"][1], cfg["photo_w"]
    scale = pw / (cx1 - cx0)
    ph = round((cy1 - cy0) * scale)
    photo = photo.crop((cx0, cy0, cx1, cy1)).resize((pw, ph), Image.LANCZOS)
    img, dr = canvas()
    dr.text((80, 40), cfg["title"], font=font(6, 46), fill=FG)
    dr.text((80, 104), cfg["sub"], font=font(3, 30), fill=SUB)
    img.paste(photo, (px, py))
    bg = TMP / "controller_bg.png"
    img.save(bg)

    def to_frame(pt):
        return px + (pt[0] - cx0) * scale, py + (pt[1] - cy0) * scale

    boxes = {name: tuple(b) for name, b in cfg["shoulders"].items()}     # 名前 -> 画面上の (x0, y0, x1, y1)
    legend_font = font(6, 30)
    legend_w = [44 + legend_font.getlength(g["legend"]) for g in cfg["groups"]]
    lx = (W - sum(legend_w) - 60 * (len(legend_w) - 1)) / 2
    badges = []
    for gi, group in enumerate(cfg["groups"]):
        color = tuple(group["color"])
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for name in group.get("draw_shoulders", []):
            x0, y0, x1, y1 = boxes[name]
            d.rounded_rectangle(boxes[name], radius=12, fill=(30, 34, 42, 255), outline=color, width=3)
            d.text(((x0 + x1) / 2, (y0 + y1) / 2), name, font=font(6, 26), fill=FG, anchor="mm")
        if group.get("shoulder_note"):
            note = group["shoulder_note"]
            d.text((note["at"][0], note["at"][1]), note["text"], font=font(3, 24), fill=SUB, anchor="mm")
        for label in group["labels"]:
            target = label["target"]
            if isinstance(target, str):                    # 肩のボタン：四角の内側の端へ
                x0, y0, x1, y1 = boxes[target]
                point = ((x0 if label["side"] == "left" else x1), (y0 + y1) / 2)
                callout(d, label, point, color, dot=False)
            else:
                callout(d, label, to_frame(target), color)
        x = lx + sum(legend_w[:gi]) + 60 * gi
        d.rounded_rectangle((x, cfg["legend_y"] - 15, x + 30, cfg["legend_y"] + 15), radius=6, fill=color)
        d.text((x + 44, cfg["legend_y"]), group["legend"], font=legend_font, fill=FG, anchor="lm")
        png = TMP / f"controller_{gi}.png"
        layer.save(png)
        badges.append((png, (0, 0), group["at"]))
    compose(PARTS / "controller.mp4", bg, [], badges, cfg.get("seconds", 8.0))


def part_photo(cfg: dict) -> None:
    """機材の写真を見出しの下に置く。HEIC は sips で JPEG にしてから読む。"""
    src = ASSETS / "photo" / cfg["src"]
    if src.suffix.lower() in {".heic", ".heif"}:
        jpg = TMP / f"{src.stem}.jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(jpg)], check=True,
                       stdout=subprocess.DEVNULL)
        src = jpg
    photo = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    top, bottom, side = 170, 1050, 40
    if "photo_w" in cfg:                          # 吹き出しを置くときは、写真の位置と幅を決め打ちする
        scale = cfg["photo_w"] / photo.width
    else:
        scale = min((W - 2 * side) / photo.width, (bottom - top) / photo.height)
    pw, ph = round(photo.width * scale), round(photo.height * scale)
    photo = photo.resize((pw, ph), Image.LANCZOS)
    px, py = cfg.get("photo_xy", ((W - pw) // 2, top + (bottom - top - ph) // 2))
    img, dr = canvas()
    dr.text((80, 40), cfg["title"], font=font(6, 46), fill=FG)
    if cfg.get("sub"):
        dr.text((80, 104), cfg["sub"], font=font(3, 30), fill=SUB)
    img.paste(photo, (px, py))
    bg = TMP / "photo_bg.png"
    img.save(bg)
    badges = []
    if cfg.get("callouts"):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for c in cfg["callouts"]:
            callout(d, c, (px + c["target"][0] * scale, py + c["target"][1] * scale), tuple(c["color"]))
        png = TMP / "photo_callouts.png"
        layer.save(png)
        badges.append((png, (0, 0), cfg.get("callouts_at", 0.4)))
    compose(PARTS / "photo.mp4", bg, [], badges, cfg.get("seconds", 3.0))


def part_credits(cfg: dict) -> None:
    """クレジット・ライセンスの画面。columns を左右に並べ、下に foot を添える。"""
    img, dr = canvas()
    dr.text((W // 2, 80), cfg["title"], font=font(6, 50), fill=FG, anchor="mm")
    columns = cfg["columns"]
    gap, side = 60, 100
    col_w = (W - 2 * side - gap * (len(columns) - 1)) // len(columns)
    item_font, head_font = font(3, 26), font(6, 32)
    accent = (92, 170, 220)
    for i, col in enumerate(columns):
        x = side + i * (col_w + gap)
        dr.text((x, 210), col["head"], font=head_font, fill=accent)
        dr.line((x, 262, x + col_w, 262), fill=(60, 66, 78), width=2)
        y = 290
        for item in col["items"]:
            for j, line in enumerate(wrap(item, item_font, col_w - 30)):
                if j == 0:
                    dr.text((x, y), "・", font=item_font, fill=SUB)
                dr.text((x + 30, y), line, font=item_font, fill=FG)
                y += 38
            y += 16
    y = H - 150
    for line in wrap(cfg.get("foot", ""), font(3, 22), W - 2 * side):
        dr.text((W // 2, y), line, font=font(3, 22), fill=SUB, anchor="mm")
        y += 34
    bg = TMP / "credits_bg.png"
    img.save(bg)
    compose(PARTS / "credits.mp4", bg, [], [], cfg.get("seconds", 7.0), fade_in=0.4, fade_out=0.8)


def fit(text: str, weight: int, size: int, width: int) -> ImageFont.FreeTypeFont:
    """width に収まるまで文字を小さくする。"""
    while font(weight, size).getlength(text) > width and size > 20:
        size -= 2
    return font(weight, size)


def thumbnail(args: argparse.Namespace) -> None:
    """YouTube のサムネイル（1920x1080 の JPEG）。左に題名、右に機材の写真、写真の上に teleop の画面を重ねる。"""
    cfg = json.loads((HERE / "clips.json").read_text())["thumbnail"]
    img, d = canvas()
    # 右：機材の写真。左の端は背景の色へぼかして溶かす
    p = cfg["photo"]
    photo = ImageOps.exif_transpose(Image.open(ASSETS / "photo" / p["src"])).convert("RGB").crop(p["crop"])
    photo = photo.resize((round(photo.width * H / photo.height), H), Image.LANCZOS)
    photo = ImageEnhance.Brightness(photo).enhance(p.get("brightness", 0.9))
    fade = p.get("fade", 320)
    mask = Image.new("L", photo.size, 255)
    md = ImageDraw.Draw(mask)
    for x in range(fade):
        md.line((x, 0, x, H), fill=round(255 * (x / fade) ** 1.5))
    img.paste(photo, (W - photo.width, 0), mask)
    # 写真の上に teleop の画面（正面のカメラ）を角丸の枠で重ねる
    s = cfg.get("screen")
    if s and (ASSETS / "teleop" / s["src"]).exists():
        shot = frame_of(ASSETS / "teleop" / s["src"], s["at"], TMP / "thumbnail_shot.png")
        fx0, fy0, fx1, fy1 = s["crop"]                      # 窓に対する割合
        shot = shot.crop((round(shot.width * fx0), round(shot.height * fy0),
                          round(shot.width * fx1), round(shot.height * fy1)))
        sw = s["w"]
        shot = shot.resize((sw, round(shot.height * sw / shot.width)), Image.LANCZOS)
        sx, sy = s["xy"]
        round_mask = Image.new("L", shot.size, 0)
        ImageDraw.Draw(round_mask).rounded_rectangle((0, 0, *shot.size), radius=24, fill=255)
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((sx + 10, sy + 14, sx + shot.width + 10, sy + shot.height + 14),
                                                 radius=24, fill=(0, 0, 0, 170))
        img.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), shadow.filter(ImageFilter.GaussianBlur(14)))
        img.paste(shot, (sx, sy), round_mask)
        d.rounded_rectangle((sx - 3, sy - 3, sx + shot.width + 3, sy + shot.height + 3), radius=26,
                            outline=FG, width=5)
    # 左：題名。縁取りで写真に重なっても読めるようにする
    x, y, width = 90, cfg.get("top", 150), cfg.get("text_w", 980)
    for line in cfg["lines"]:
        f = fit(line, 8, cfg.get("size", 104), width)
        d.text((x, y), line, font=f, fill=FG, stroke_width=8, stroke_fill=BG)
        y += round(f.size * 1.28)
    y += 10
    f = fit(cfg["main"], 8, cfg.get("main_size", 190), width)
    d.text((x, y), cfg["main"], font=f, fill=tuple(cfg.get("main_color", (255, 205, 50))),
           stroke_width=10, stroke_fill=BG)
    y += round(f.size * 1.45)
    if cfg.get("badge"):
        f = font(6, cfg.get("badge_size", 50))
        pad_x, pad_y = 34, 20
        bw, bh = f.getlength(cfg["badge"]) + 2 * pad_x, f.size + 2 * pad_y
        d.rounded_rectangle((x, y, x + bw, y + bh), radius=bh // 4, fill=OK)
        d.text((x + bw / 2, y + bh / 2), cfg["badge"], font=f, fill=(255, 255, 255), anchor="mm")
    out = BUILD / "thumbnail.jpg"
    img.save(out, quality=92)
    print(f"wrote {out.relative_to(HERE)}  {out.stat().st_size / 1e6:.2f} MB")


def parts(args: argparse.Namespace) -> None:
    clips = json.loads((HERE / "clips.json").read_text())
    builders = {"smolvla": lambda: part_smolvla(clips), "compare": lambda: part_compare(clips),
                "successes": lambda: part_successes(clips), "results": part_results}
    if "title" in clips:
        builders["title"] = lambda: part_title(clips["title"])
    for name in ("overview", "training", "recap"):
        if name in clips:
            builders[name] = lambda name=name: part_overview(clips[name], name)
    if "credits" in clips:
        builders["credits"] = lambda: part_credits(clips["credits"])
    if "outro" in clips:
        builders["outro"] = lambda: part_title(clips["outro"], "outro")
    if "controller" in clips and (ASSETS / "photo" / clips["controller"]["src"]).exists():
        builders["controller"] = lambda: part_controller(clips["controller"])
    if "code" in clips:
        builders["code"] = lambda: part_code(clips["code"])
    if "photo" in clips:
        if (ASSETS / "photo" / clips["photo"]["src"]).exists():
            builders["photo"] = lambda: part_photo(clips["photo"])
        else:
            print(f"skip photo: assets/photo/{clips['photo']['src']} が無い")
    for name, cfg in clips.get("screens", {}).items():
        if (ASSETS / "teleop" / cfg["src"]).exists():
            builders[name] = lambda name=name, cfg=cfg: part_screen(name, cfg)
        else:
            print(f"skip {name}: assets/teleop/{cfg['src']} が無い")
    unknown = set(args.only or []) - set(builders)
    if unknown:
        raise SystemExit(f"知らないパーツ: {', '.join(sorted(unknown))}（選べるもの: {', '.join(builders)}）")
    for name in args.only or builders:
        builders[name]()


# ---------------------------------------------------------------- norm / sheet / final

def norm(args: argparse.Namespace) -> None:
    """自分で撮った録画を 1920x1080・30fps に収め（余白は背景色）、音声が無ければ無音を足す。"""
    srcs = sorted(p for p in (ASSETS / "teleop").iterdir() if p.suffix.lower() in {".mov", ".mp4", ".m4v"})
    if not srcs:
        raise SystemExit(f"{(ASSETS / 'teleop').relative_to(HERE)}/ に録画（.mov / .mp4）が無い")
    color = "0x%02x%02x%02x" % BG
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={color},fps={FPS},format=yuv420p")
    for src in srcs:
        out = PARTS / f"teleop_{src.stem}.mp4"
        if out.exists() and out.stat().st_mtime > src.stat().st_mtime and not args.force:
            continue
        if has_audio(src):
            io = ["-i", src, "-map", "0:v:0", "-map", "0:a:0"]
        else:
            io = ["-i", src, *SILENCE, "-map", "0:v:0", "-map", "1:a", "-shortest"]
        ffmpeg([*io, "-vf", vf, *VCODEC, *ACODEC, "-movflags", "+faststart", out])
        print(f"wrote {out.relative_to(HERE)}  {duration(out):.1f} s")


def sheet(args: argparse.Namespace) -> None:
    out_dir = BUILD / "sheet"
    out_dir.mkdir(parents=True, exist_ok=True)
    for part in sorted(PARTS.glob("*.mp4")):
        d = duration(part)
        out = out_dir / f"{part.stem}.png"
        ffmpeg(["-i", part, "-vf", f"fps=6/{d:.3f},scale=640:-2,tile=3x2", "-frames:v", "1", out])
        print(f"wrote {out.relative_to(HERE)}")


NARRATION_EXT = (".m4a", ".wav", ".aiff", ".mp3", ".caf")


def narration_audio(name: str, cfg: dict) -> Path | None:
    """パーツ name のナレーション。assets/narration/<name>.* があればそれ（自分の声）、無ければ台本を読み上げる。"""
    for ext in NARRATION_EXT:
        own = ASSETS / "narration" / f"{name}{ext}"
        if own.exists():
            return own
    text = cfg.get("lines", {}).get(name)
    if not text:
        return None
    spoken = text
    for word, reading in cfg.get("readings", {}).items():   # 読み上げで読み違える語を、読み方に置き換える
        spoken = spoken.replace(word, reading)
    engine = cfg.get("engine", "say")
    settings = cfg.get(engine, {})
    out = BUILD / "narration" / f"{name}.{engine}.{'aiff' if engine == 'say' else 'wav'}"
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = out.with_suffix(".txt")
    key = json.dumps([engine, settings, spoken], ensure_ascii=False)
    if out.exists() and stamp.exists() and stamp.read_text() == key:     # 台本・設定が変わったときだけ作り直す
        return out
    if engine == "say":
        cmd = ["say", "-v", settings.get("voice", "Kyoko"), "-o", str(out)]
        if settings.get("rate"):
            cmd += ["-r", str(settings["rate"])]
        subprocess.run([*cmd, spoken], check=True)
    elif engine == "aivisspeech":
        out.write_bytes(aivisspeech(spoken, settings))
    else:
        raise SystemExit(f"知らない読み上げ: {engine}（say か aivisspeech）")
    stamp.write_text(key)
    return out


def aivisspeech(text: str, settings: dict) -> bytes:
    """AivisSpeech Engine（VOICEVOX 互換の API）で読み上げ、WAV を返す。アプリを起動しておく。"""
    base = settings.get("url", "http://127.0.0.1:10101")
    speaker = settings.get("speaker") or aivisspeech_styles(base)[0][0]
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"{base}/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": speaker}),
                method="POST"), timeout=120) as r:
            query = json.load(r)
    except OSError as e:
        raise SystemExit(f"AivisSpeech に届かない（{base}）。アプリを起動してから作り直す: {e}")
    for field in ("speedScale", "intonationScale", "tempoDynamicsScale", "volumeScale",
                  "prePhonemeLength", "postPhonemeLength"):
        if field in settings:
            query[field] = settings[field]
    req = urllib.request.Request(f"{base}/synthesis?speaker={speaker}", method="POST",
                                 data=json.dumps(query).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def aivisspeech_styles(base: str) -> list[tuple[int, str]]:
    """入っている声と話し方の一覧（style ID, 「話者 / 話し方」）。"""
    try:
        with urllib.request.urlopen(f"{base}/speakers", timeout=10) as r:
            speakers = json.load(r)
    except OSError as e:
        raise SystemExit(f"AivisSpeech に届かない（{base}）。アプリを起動してから作り直す: {e}")
    return [(style["id"], f"{sp['name']} / {style['name']}") for sp in speakers for style in sp["styles"]]


def voices(args: argparse.Namespace) -> None:
    narration = json.loads((HERE / "clips.json").read_text()).get("narration", {})
    base = narration.get("aivisspeech", {}).get("url", "http://127.0.0.1:10101")
    for style_id, label in aivisspeech_styles(base):
        print(f"{style_id:>12}  {label}")


def narrate(part: Path, audio: Path, cfg: dict) -> tuple[Path, float]:
    """パーツにナレーションを重ねる。ナレーションが長ければ、最後のコマで止めたまま延ばす。"""
    lead, tail = cfg.get("lead", 0.4), cfg.get("tail", 0.6)
    d_part, d_audio = duration(part), duration(audio)
    total = max(d_part, lead + d_audio + tail)
    out = BUILD / "narrated" / part.name
    out.parent.mkdir(parents=True, exist_ok=True)
    # 頭の無音は adelay ではなく、無音のデータをつないで作る。adelay だと mp4 の編集リスト
    # （開始の遅れ）として記録されることがあり、つなぐときにその分が捨てられて音声が前へずれた。
    ffmpeg(["-i", part, "-i", audio, "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={total - d_part + 0.1:.3f}[v];"
            f"[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,asetpts=N/SR/TB[n];"
            f"anullsrc=r=48000:cl=stereo,atrim=0:{lead:.3f},aformat=sample_fmts=fltp[s];"
            f"[s][n]concat=n=2:v=0:a=1,apad,atrim=0:{total:.3f}[a]",
            "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", *VCODEC, *ACODEC, out])
    return out, total - d_part


def final(args: argparse.Namespace) -> None:
    names = [line.split("#", 1)[0].strip() for line in (HERE / "timeline.txt").read_text().splitlines()]
    names = [n for n in names if n]
    missing = [n for n in names if not (PARTS / n).exists()]
    if missing:
        raise SystemExit(f"build/parts/ に無い: {', '.join(missing)}（make parts / make norm を先に）")
    narration = {} if args.no_narration else json.loads((HERE / "clips.json").read_text()).get("narration", {})
    files = []
    for n in names:
        part = PARTS / n
        audio = narration_audio(Path(n).stem, narration) if narration else None
        if audio:
            part, extra = narrate(part, audio, narration)
            src = "自分の声" if audio.parent == ASSETS / "narration" else f"読み上げ（{narration.get('engine', 'say')}）"
            print(f"  {n:<16} {src}  {duration(audio):5.1f} s" + (f"  （{extra:.1f} s 延ばした）" if extra > 0 else ""))
        files.append(part)
    # 映像と音声を別々につなぐ（concat フィルタ）。各パーツの音声は、映像の長さにぴったり合わせてから
    # つなぐ。AAC はフレーム単位なので音声が映像より数十 ms 短く、そのままだと継ぎ目ごとにずれがたまる。
    inputs, chains = [], []
    for i, f in enumerate(files):
        inputs += ["-i", f]
        v = video_duration(f)
        chains.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}];"
                      f"[{i}:a]aresample=48000,asetpts=PTS-STARTPTS,apad,atrim=0:{v:.6f}[a{i}]")
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(files)))
    graph = ";".join(chains) + f";{joined}concat=n={len(files)}:v=1:a=1[vout][aout]"
    out = BUILD / "final.mp4"
    ffmpeg([*inputs, "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
            *VCODEC, *ACODEC, "-movflags", "+faststart", out])
    print(f"wrote {out.relative_to(HERE)}  {duration(out):.1f} s  ({len(names)} parts)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--pre", required=True, help="VLADrop 追加学習前の結果フォルダ")
    c.add_argument("--post", required=True, help="VLADrop 追加学習後の結果フォルダ")
    c.add_argument("--smolvla", required=True, help="SmolVLA の 1 試行のフォルダ")
    p = sub.add_parser("parts")
    p.add_argument("--only", nargs="*", help="作るパーツの名前（既定は全部。clips.json の screens も含む）")
    n = sub.add_parser("norm")
    n.add_argument("--force", action="store_true", help="作り済みでも作り直す")
    sub.add_parser("sheet")
    sub.add_parser("voices", help="AivisSpeech に入っている声と話し方（style ID）を並べる")
    f = sub.add_parser("final")
    f.add_argument("--no-narration", action="store_true", help="ナレーションを入れずにつなぐ")
    sub.add_parser("thumbnail", help="YouTube のサムネイルを build/thumbnail.jpg に作る")
    args = ap.parse_args()
    for d in (PARTS, TMP):
        d.mkdir(parents=True, exist_ok=True)
    {"collect": collect, "parts": parts, "norm": norm, "sheet": sheet, "final": final, "voices": voices,
     "thumbnail": thumbnail}[args.cmd](args)


if __name__ == "__main__":
    main()
