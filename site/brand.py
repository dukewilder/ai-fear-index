"""The account's fixed images: the avatar, the header, and the plate a brief goes out on.

These are the parts that never change, so they are drawn once and committed rather than built
every run. The card is the same mark at 1600 by 900, which is the size X shows at full width.

    python -m site.brand --out site/static/brand
"""
import argparse
import pathlib

from PIL import Image, ImageDraw, ImageFont

FONTS = pathlib.Path(__file__).resolve().parent / "fonts"
PAPER, INK, VERMILLION = "#F1EDE3", "#141210", "#B3321F"


def font(name, size):
    return ImageFont.truetype(str(FONTS / f"{name}.ttf"), size)


def spaced(draw, xy, text, fnt, fill, track=0.0):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + track
    return x


def width(draw, text, fnt, track=0.0):
    return sum(draw.textlength(c, font=fnt) for c in text) + track * max(0, len(text) - 1)


def wrap(draw, text, fnt, room, track=0.0):
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = (line + " " + word).strip()
        if width(draw, trial, fnt, track) <= room or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def badge(d, x, y, size, fg=VERMILLION, bg=PAPER):
    """The mark: AI in a filled box. It is the whole avatar, so it has to read at 48 pixels."""
    f = font("barlow-condensed-800", size)
    w = d.textlength("AI", font=f)
    pad_x, pad_y = size * 0.30, size * 0.12
    box = [x, y, x + w + pad_x * 2, y + size * 1.18]
    d.rectangle(box, fill=bg)
    d.text((x + pad_x, y - size * 0.11 + pad_y), "AI", font=f, fill=fg)
    return box[2] - box[0], box[3] - box[1]


def wordmark(d, x, y, size, fill=PAPER, boxed=True):
    """AI in a box, then FEAR REPORT. Returns the width drawn."""
    bw = 0
    if boxed:
        bw, _ = badge(d, x, y, size, fg=VERMILLION, bg=fill)
        bw += size * 0.34
    f = font("barlow-condensed-800", size * 1.16)
    d.text((x + bw, y - size * 0.15), "FEAR REPORT", font=f, fill=fill)
    return bw + d.textlength("FEAR REPORT", font=f)


def avatar(path, size=400, label=False):
    """Square, shown as a circle, so the mark sits centred with room on every side.

    At the size a timeline actually shows it, a word under the mark is a grey smudge, so the
    default is the mark alone, as large as the circle allows.
    """
    im = Image.new("RGB", (size, size), VERMILLION)
    d = ImageDraw.Draw(im)
    mark = int(size * (0.355 if label else 0.46))
    f = font("barlow-condensed-800", mark)
    pad_x, pad_y = mark * 0.30, mark * 0.12
    bw, bh = d.textlength("AI", font=f) + pad_x * 2, mark * 1.18
    x = (size - bw) / 2
    y = (size - bh) / 2 - (size * 0.075 if label else 0)
    d.rectangle([x, y, x + bw, y + bh], fill=PAPER)
    d.text((x + pad_x, y - mark * 0.11 + pad_y), "AI", font=f, fill=VERMILLION)
    if label:
        f2 = font("barlow-condensed-800", int(size * 0.105))
        lw = width(d, "FEAR REPORT", f2, size * 0.012)
        spaced(d, ((size - lw) / 2, y + bh + size * 0.055), "FEAR REPORT", f2, PAPER, size * 0.011)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)
    return path


def header(path, w=1500, h=500):
    """The banner: the mark and the address, nothing else.

    The avatar sits over the lower left and a phone crops the sides, so it all lives in the
    middle band and nothing goes near that corner.
    """
    im = Image.new("RGB", (w, h), VERMILLION)
    d = ImageDraw.Draw(im)
    mark = 116
    f_mark = font("barlow-condensed-800", mark * 1.16)
    block = (d.textlength("AI", font=font("barlow-condensed-800", mark)) + mark * 0.60
             + mark * 0.34 + d.textlength("FEAR REPORT", font=f_mark))
    x = (w - block) / 2
    top = 150
    wordmark(d, x, top, mark)
    f_url = font("ibm-plex-mono-600", 34)
    uw = width(d, "aifearreport.com", f_url, 3.0)
    spaced(d, ((w - uw) / 2, top + mark * 1.62), "aifearreport.com", f_url, PAPER, 3.0)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)
    return path


def plate(path, date, headline="", w=1600, h=900):
    """The image a brief goes out on: the mark, the date, one line, the address.

    The post carries the entries. This carries the masthead, so the account is recognisable in a
    feed at thumbnail size, which a page of small type is not.
    """
    im = Image.new("RGB", (w, h), VERMILLION)
    d = ImageDraw.Draw(im)
    pad = 92

    mark = 120
    wordmark(d, pad, pad + 24, mark)

    f_date = font("ibm-plex-mono-600", 30)
    spaced(d, (pad + 4, pad + 24 + mark * 1.62), date.upper(), f_date, PAPER, 4.0)

    if headline:
        f_head = font("barlow-condensed-800", 100)
        lines = wrap(d, headline.upper(), f_head, w - pad * 2, 1.6)[:3]
        y = pad + 24 + mark * 1.62 + 132
        d.line([(pad, y - 56), (pad + 170, y - 56)], fill=PAPER, width=5)
        for line in lines:
            spaced(d, (pad, y), line, f_head, PAPER, 1.6)
            y += 104

    f_url = font("ibm-plex-mono-600", 34)
    url = "aifearreport.com"
    uw = width(d, url, f_url, 3.0)
    spaced(d, (pad, h - pad - 30), url, f_url, PAPER, 3.0)
    f_h = font("ibm-plex-mono-500", 30)
    handle = "@aiFearReport"
    hw = width(d, handle, f_h, 2.4)
    spaced(d, (w - pad - hw, h - pad - 28), handle, f_h, PAPER, 2.4)

    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/static/brand")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    print(avatar(out / "avatar.png", size=800, label=True))
    print(header(out / "header.png"))
    print(plate(out / "plate.png", "Saturday 19 September 2026"))


if __name__ == "__main__":
    main()
