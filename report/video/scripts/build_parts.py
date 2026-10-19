"""最終課題の動画の素材をそろえ、パーツを組んで書き出す。

    collect  parc2026 の元動画を assets/parc2026/ へ写す（SSD をつないで一度だけ）
    parts    assets/ の素材からパーツを build/parts/ に作る（画面録画は clips.json の screens）
    norm     assets/teleop/ の録画を、パーツと同じ形式にそろえて build/parts/ に置く
    sheet    パーツのコマを並べた確認用の画像を build/sheet/ に作る
    final    timeline.txt の順にパーツをつなぎ、build/final.mp4 に書き出す

パーツはすべて 1920x1080・30fps・H.264 / AAC 48 kHz ステレオ（無音の音声つき）にそろえる。
手順と構成の正本は ../README.md。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
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
NG = (190, 55, 55)
FONT_DIR = Path("/System/Library/Fonts")

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


def has_audio(path: Path) -> bool:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a",
                                   "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], text=True)
    return bool(out.strip())


def compose(out: Path, bg: Path, videos: list[tuple], badges: list[tuple], total: float,
            fade_in: float = 0.0) -> None:
    """背景の上に動画と札を重ねる。

    videos: (path, speed, (x, y), (w, h)[, 前処理])。最後のコマで止めたまま total まで伸ばす。
        前処理は trim・crop など、速さを変える前に掛ける filter（省略可）。元の音声は使わない。
    badges: (png, (x, y), 出す時刻 s)。
    fade_in: 頭を黒から浮かび上がらせる秒数。
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
    for j, (_, (x, y), t0) in enumerate(badges):
        chains.append(f"[{last}][{first + j}:v]overlay={x}:{y}:enable='gte(t,{t0:.3f})'[b{j}]")
        last = f"b{j}"
    fade = f"fade=t=in:st=0:d={fade_in:.3f}," if fade_in else ""
    chains.append(f"[{last}]{fade}format=yuv420p[vout]")
    audio = first + len(badges)
    ffmpeg([*args, "-filter_complex", ";".join(chains), "-map", "[vout]", "-map", f"{audio}:a",
            "-t", t, *VCODEC, *ACODEC, "-movflags", "+faststart", out])
    print(f"wrote {out.relative_to(HERE)}  {total:.1f} s")


# ---------------------------------------------------------------- 文字

def font(weight: int, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / f"ヒラギノ角ゴシック W{weight}.ttc"), size)


def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """英文は単語、日本語は文字で折り返す。"""
    units, sep = (text.split(" "), " ") if text.isascii() else (list(text), "")
    lines, cur = [], ""
    for u in units:
        trial = f"{cur}{sep}{u}" if cur else u
        if f.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = u
    return [*lines, cur] if cur else lines


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
    header(dr, "追加学習の前と後（同じ問題・同じ初期配置）", clips["captions"][str(task)],
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
    header(dr, "追加学習の後：解けた問題の例", "π0.5 を、逆向きの課題の教師データで追加学習",
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
    header(dr, "結果：逆向きの課題 16 問（各 1 試行）",
           "元の LIBERO の課題を逆向きにした 8 種 × 指示文 2 通り（原文・言い換え）", note="π0.5")
    for cx, label, s, color in ((W // 4 + 80, "追加学習の前", pre, NG), (3 * W // 4 - 80, "追加学習の後", post, OK)):
        dr.text((cx, 380), label, font=font(6, 48), fill=FG, anchor="mm")
        dr.text((cx, 560), f"{s['success']} / {s['episodes']}", font=font(6, 180), fill=color, anchor="mm")
        dr.text((cx, 720), f"ほかの物を動かさずに成功：{s['safe_success']} 問", font=font(3, 36), fill=SUB, anchor="mm")
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
    pre = f"trim=start={start:.3f}:end={end:.3f},crop={cw}:{ch}:{cx}:{cy}"
    compose(PARTS / f"{name}.mp4", bg, [(src, speed, (x, y), (w, h), pre)], [], (end - start) / speed + hold)


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


def part_title(cfg: dict) -> None:
    """題名の画面。背景に teleop の画面録画のコマをぼかして暗く敷く。"""
    bg_cfg = cfg.get("bg")
    if bg_cfg and (ASSETS / "teleop" / bg_cfg["src"]).exists():
        shot = frame_of(ASSETS / "teleop" / bg_cfg["src"], bg_cfg.get("at", 0.0), TMP / "title_shot.png")
        img = cover(shot).filter(ImageFilter.GaussianBlur(14))
        img = ImageEnhance.Brightness(img).enhance(0.32)
    else:
        img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    lead, main = cfg["main"]
    d.text((W // 2, 400), lead, font=font(6, 64), fill=FG, anchor="mm")
    d.text((W // 2, 535), main, font=font(8, 136), fill=FG, anchor="mm")
    d.line((W // 2 - 360, 650, W // 2 + 360, 650), fill=SUB, width=2)
    d.text((W // 2, 712), cfg["sub"], font=font(3, 38), fill=FG, anchor="mm")
    bg = TMP / "title_bg.png"
    img.save(bg)
    compose(PARTS / "title.mp4", bg, [], [], cfg.get("seconds", 2.0), fade_in=0.3)


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
    scale = min((W - 2 * side) / photo.width, (bottom - top) / photo.height)
    photo = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.LANCZOS)
    img, dr = canvas()
    dr.text((80, 40), cfg["title"], font=font(6, 46), fill=FG)
    if cfg.get("sub"):
        dr.text((80, 104), cfg["sub"], font=font(3, 30), fill=SUB)
    img.paste(photo, ((W - photo.width) // 2, top + (bottom - top - photo.height) // 2))
    bg = TMP / "photo_bg.png"
    img.save(bg)
    compose(PARTS / "photo.mp4", bg, [], [], cfg.get("seconds", 3.0))


def parts(args: argparse.Namespace) -> None:
    clips = json.loads((HERE / "clips.json").read_text())
    builders = {"smolvla": lambda: part_smolvla(clips), "compare": lambda: part_compare(clips),
                "successes": lambda: part_successes(clips), "results": part_results}
    if "title" in clips:
        builders["title"] = lambda: part_title(clips["title"])
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


def final(args: argparse.Namespace) -> None:
    names = [line.split("#", 1)[0].strip() for line in (HERE / "timeline.txt").read_text().splitlines()]
    names = [n for n in names if n]
    missing = [n for n in names if not (PARTS / n).exists()]
    if missing:
        raise SystemExit(f"build/parts/ に無い: {', '.join(missing)}（make parts / make norm を先に）")
    lst = TMP / "concat.txt"
    lst.write_text("".join(f"file '{PARTS / n}'\n" for n in names))
    out = BUILD / "final.mp4"
    ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, *VCODEC, *ACODEC, "-movflags", "+faststart", out])
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
    sub.add_parser("final")
    args = ap.parse_args()
    for d in (PARTS, TMP):
        d.mkdir(parents=True, exist_ok=True)
    {"collect": collect, "parts": parts, "norm": norm, "sheet": sheet, "final": final}[args.cmd](args)


if __name__ == "__main__":
    main()
