"""Pull chat, seller speech and lot state out of sampled eBay Live frames.

    uv run python tools/extract_frames.py ~/Desktop/ebaylive --out /tmp/ebay

WHY OCR RATHER THAN READING THE FRAMES. 218 frames at three regions each is 654
crops. Reading them by eye is the accurate way and it does not finish; cropping
to the regions that matter, binarising, and running tesseract is the way that
does. The output is a draft transcript for a human to correct, never a dataset
on its own — OCR on text composited over live video is wrong often enough that
shipping it unchecked would poison the corpus the whole eval rests on.

THE BINARISATION IS THE TRICK. Chat and captions are white text over a moving
video feed, so tesseract sees the video as much as the text. Thresholding at
luma > 170 throws the video away and keeps the glyphs, which takes the chat
region from unreadable to clean.

THREE REGIONS, THREE DIFFERENT KINDS OF EVIDENCE:

    chat     what viewers typed — the triage corpus
    caption  **what the seller SAID** — eBay Live carries live captions, and
             this is the thing the Whatnot observation could not capture at all.
             It is what makes "did the seller answer that question?" answerable.
    lot      lot number, price, who is winning, seconds left — the auction
             mechanics, for Suite E

A NOTE ON SAMPLING, because it bit us once already. Frames are 5 s apart and
eBay Live shows only THREE chat messages at a time. A message is therefore
missed whenever four or more arrive inside one 5 s window. At the pace observed
on Whatnot (0.15 msg/s) that is rare, but it is a **censoring floor, not zero** —
any message-rate computed from this is a lower bound. The same mistake in the
other direction was corrected once before in the Whatnot analysis.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# x, y, w, h in the 3584x1758 source. Measured off a frame, not guessed.
REGIONS = {
    "chat": (1265, 845, 1075, 400),
    "caption": (1330, 245, 900, 150),   # narrowed: the wider box ate the Items/Wallet rail
    "lot": (1270, 1400, 1060, 210),
}
# Chat is WHITE text over video, so a bright threshold isolates the glyphs.
# The caption is the opposite — DARK text inside a light bubble — so the same
# threshold kept the bubble and deleted every word. Found by reading the output
# rather than by reasoning about it, which is the only way this kind of bug
# surfaces. The lot panel has its own solid background and needs nothing.
THRESHOLD = {"chat": 170, "caption": None, "lot": None}

# A username line: no spaces, and the handle alphabet eBay actually issues.
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._\-]{2,30}$")
# Lines that are mostly punctuation or stray glyphs are video bleed, not text.
NOISE = re.compile(r"^[^A-Za-z0-9]*$")


@dataclass
class Frame:
    idx: int
    chat: list[tuple[str, str]] = field(default_factory=list)   # (author, text)
    caption: str = ""
    lot: dict[str, str] = field(default_factory=dict)


def _crop_ocr(png: Path, region: str, tmp: Path) -> str:
    x, y, w, h = REGIONS[region]
    thr = THRESHOLD[region]
    vf = f"crop={w}:{h}:{x}:{y}"
    if thr is not None:
        vf += f",format=gray,lut=y='if(gt(val,{thr}),255,0)'"
    out = tmp / f"{png.stem}_{region}.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(png),
                    "-vf", vf, str(out)], check=True)
    r = subprocess.run(["tesseract", str(out), "-", "--psm", "6"],
                       capture_output=True, text=True)
    out.unlink(missing_ok=True)
    return r.stdout


def clean(raw: str) -> list[str]:
    """Drop video bleed. Deliberately conservative — a wrongly dropped line is
    a message lost from the corpus, a wrongly kept one is visible to the human
    correcting the draft."""
    keep = []
    for line in raw.splitlines():
        s = line.strip()
        if len(s) < 4 or NOISE.match(s):
            continue
        letters = sum(c.isalnum() or c.isspace() for c in s)
        if letters / len(s) < 0.72:            # mostly glyph soup
            continue
        words = s.split()
        # Video bleed reliably OCRs as scattered one- and two-character tokens.
        # Real chat does not look like that.
        if len(words) > 2 and sum(len(w) <= 2 for w in words) / len(words) > 0.5:
            continue
        if sum(c.isalpha() for c in s) < 4:
            continue
        keep.append(s)
    return keep


VOWELS = set("aeiouAEIOU")

# Short words that legitimately carry no vowel, so the token filter below does
# not eat them.
_NO_VOWEL_OK = {"my", "by", "hm", "mm", "tv", "ok", "vs", "th", "ty"}


def clean_caption(s: str) -> str:
    """Strip video bleed token by token.

    The caption bubble is translucent, so its edges pick up whatever is moving
    behind it and tesseract renders that as short vowel-less debris — `Bie`,
    `Sst`, `))`, `~3`. That debris differs frame to frame, which is what
    defeats the overlap merge: two captions of the same speech do not share a
    boundary once each has different rubbish glued to it. Removing the tokens
    first is what makes the merge find the real overlap.
    """
    out = []
    for w in s.split():
        core = "".join(c for c in w if c.isalnum() or c in "'\u2019")
        if not core:
            continue
        if core.isdigit():
            out.append(core)
            continue
        low = core.lower()
        if not any(c in VOWELS for c in core) and low not in _NO_VOWEL_OK:
            continue
        if len(core) <= 2 and low not in _NO_VOWEL_OK | {"a", "i", "is", "it",
                                                          "in", "of", "on", "or",
                                                          "to", "up", "us", "we",
                                                          "so", "no", "do", "go",
                                                          "he", "me", "be", "at",
                                                          "an", "as", "if", "am"}:
            continue
        out.append(core)
    return " ".join(out)


def plausible(s: str) -> bool:
    """Does this read like something a person typed?

    Video bleed survives the line filter often enough to be annoying, and it is
    recognisable: too few real words, a vowel ratio no language has, or runs of
    isolated characters. Tuned to be *stricter* than `clean` because the cost
    here is asymmetric in the other direction — a dropped line is still in
    chat_raw.tsv, an admitted one costs a human a keypress and their attention.
    """
    words = s.split()
    real = [w for w in words if len(w) >= 3 and any(c in VOWELS for c in w)]
    if len(real) < 2:
        return False
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 10:
        return False
    vr = sum(c in VOWELS for c in letters) / len(letters)
    if not 0.22 <= vr <= 0.62:            # no natural English sits outside this
        return False
    if sum(len(w) <= 2 for w in words) / len(words) > 0.4:
        return False
    return True


def parse_chat(lines: list[str]) -> list[tuple[str, str]]:
    """Pair usernames with the message under them.

    eBay Live renders the handle on its own line above the text, so order is
    the only structure available and it is enough. A message with no handle
    above it is kept with an empty author rather than dropped — the text is
    what the corpus needs.
    """
    out: list[tuple[str, str]] = []
    author = ""
    for s in lines:
        if USERNAME.match(s) and " " not in s:
            author = s
            continue
        out.append((author, s))
        author = ""
    return out


LOT_NO = re.compile(r"#(\d+)")
PRICE = re.compile(r"\$\s?([\d,]+\.?\d*)")
WINNING = re.compile(r"([A-Za-z0-9._\-]+)\s+is winning", re.I)
CLOCK = re.compile(r"(\d{1,2}):(\d{2})")


def parse_lot(raw: str) -> dict[str, str]:
    d: dict[str, str] = {}
    if m := LOT_NO.search(raw):
        d["lot"] = m.group(1)
    if m := PRICE.search(raw):
        d["price"] = m.group(1).replace(",", "")
    if m := WINNING.search(raw):
        d["winner"] = m.group(1)
    if m := CLOCK.search(raw):
        d["clock"] = f"{int(m.group(1))}:{m.group(2)}"
    return d


def _merge_overlap(acc: str, new: str, min_overlap: int = 8) -> str:
    """Append `new` to `acc`, collapsing the longest shared boundary.

    OCR of the same words across frames is not identical, so this compares on a
    lowercase alphanumeric projection and splices on the raw text. Longest
    overlap first: a short accidental match would splice in the wrong place and
    silently delete a clause.
    """
    if not acc:
        return new
    a, b = acc.lower(), new.lower()
    for n in range(min(len(a), len(b)), min_overlap - 1, -1):
        if a[-n:] == b[:n]:
            return acc + new[n:]
    return acc + " " + new


def do_frame(png: Path, tmp: Path) -> Frame:
    idx = int(re.sub(r"\D", "", png.stem) or 0)
    f = Frame(idx=idx)
    f.chat = parse_chat(clean(_crop_ocr(png, "chat", tmp)))
    f.caption = clean_caption(" ".join(clean(_crop_ocr(png, "caption", tmp))))
    f.lot = parse_lot(_crop_ocr(png, "lot", tmp))
    return f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default="/tmp/ebay_extract")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    for exe in ("ffmpeg", "tesseract"):
        if shutil.which(exe) is None:
            print(f"need {exe} on PATH", file=sys.stderr)
            return 1

    src = Path(a.folder).expanduser()
    pngs = sorted(src.glob("*.png"))[: a.limit or None]
    if not pngs:
        print(f"no frames in {src}", file=sys.stderr)
        return 1
    outdir = Path(a.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = outdir / "_crops"
    tmp.mkdir(exist_ok=True)

    print(f"{len(pngs)} frames from {src}")
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        frames = list(pool.map(lambda p: do_frame(p, tmp), pngs))
    frames.sort(key=lambda f: f.idx)

    # --- chat, deduped, first occurrence wins ----------------------------
    # Dedup has to cope with PARTIAL reads. A message fades and slides as it
    # scrolls out, so successive frames yield "Got any sealed promo cards or
    # banned cards buddy?", then "Got any seale 'or banned cards buddy?", then
    # "Got any seal romo cards". An exact key treats all three as new messages
    # and the corpus fills with fragments of one question. So: containment on
    # the normalised form, plus a similarity check, keeping the LONGEST reading
    # — the fullest view of the message is the one closest to what was typed.
    chat_rows: list[dict] = []
    keys: list[str] = []
    for f in frames:
        for author, text in f.chat:
            k = re.sub(r"[^a-z0-9]", "", text.lower())
            if len(k) < 4:
                continue
            hit = -1
            for j, prev in enumerate(keys):
                if k in prev or prev in k or SequenceMatcher(None, k, prev).ratio() > 0.72:
                    hit = j
                    break
            if hit >= 0:
                if len(text) > len(chat_rows[hit]["text"]):
                    chat_rows[hit]["text"] = text
                    chat_rows[hit]["author"] = author or chat_rows[hit]["author"]
                    keys[hit] = k
                continue
            keys.append(k)
            chat_rows.append({"frame": f.idx, "t": f.idx * 5,
                              "author": author, "text": text})
    (outdir / "chat_raw.tsv").write_text(
        "frame\tsec\tauthor\ttext\n" + "\n".join(
            f"{r['frame']}\t{r['t']}\t{r['author']}\t{r['text']}" for r in chat_rows),
        encoding="utf-8")
    # Paste-ready for tools/label.html — filtered harder than the TSV.
    # Two files on purpose: chat_raw.tsv keeps everything OCR produced so a
    # human can rescue a real message the filter threw away, while this one is
    # tuned for someone about to label a hundred rows and who should not spend
    # half of them pressing skip.
    (outdir / "chat_messages.txt").write_text(
        "\n".join(r["text"] for r in chat_rows if plausible(r["text"])) + "\n",
        encoding="utf-8")

    # --- seller speech ----------------------------------------------------
    # The caption is a ROLLING two-line window, not a feed: consecutive frames
    # repeat most of the previous text with a little new on the end. Deduping
    # whole captions keeps all the repetition; the fix is an overlap merge —
    # find the longest suffix of what we already have that prefixes the new
    # caption, and append only the remainder. That turns 40 overlapping
    # snapshots into one continuous transcript of what the seller said.
    caps: list[str] = []
    transcript = ""
    for f in frames:
        c = " ".join(f.caption.split())
        if len(c) < 8:
            continue
        merged = _merge_overlap(transcript, c)
        new = merged[len(transcript):].strip()
        if len(new) >= 4:
            caps.append(f"[{f.idx * 5 // 60:02d}:{f.idx * 5 % 60:02d}] {new}")
            transcript = merged
    (outdir / "seller_speech.txt").write_text("\n".join(caps) + "\n", encoding="utf-8")
    (outdir / "seller_transcript.txt").write_text(transcript + "\n", encoding="utf-8")

    # --- lot timeline -----------------------------------------------------
    rows = ["frame\tsec\tlot\tprice\twinner\tclock"]
    for f in frames:
        d = f.lot
        if d:
            rows.append(f"{f.idx}\t{f.idx*5}\t{d.get('lot','')}\t{d.get('price','')}"
                        f"\t{d.get('winner','')}\t{d.get('clock','')}")
    (outdir / "lots_raw.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    shutil.rmtree(tmp, ignore_errors=True)
    lots = {f.lot.get("lot") for f in frames if f.lot.get("lot")}
    print(f"\n  chat     {len(chat_rows)} unique messages -> chat_messages.txt")
    print(f"  speech   {len(caps)} caption changes      -> seller_speech.txt")
    print(f"  lots     {len(lots)} distinct lot numbers -> lots_raw.tsv")
    print(f"\n  in {outdir}")
    print("\n  OCR output is a DRAFT. Read it against the frames before it becomes data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
