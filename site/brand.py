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


def compose(w, h, bg, paint, dx=0, dy=0):
    """Paint the content onto a clear layer, then set the layer down so its marks are centred.

    Every other way of centring this predicts where the marks will land and is wrong by a pixel or
    four: a face reports padding as part of its glyph box, a filled rectangle covers both its end
    coordinates, and a fraction of a pixel rounds whichever way it likes. Painting first and
    measuring the result afterwards cannot be wrong about it, because it is looking at the answer.
    """
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    paint(ImageDraw.Draw(layer))
    im = Image.new("RGB", (w, h), bg)
    box = layer.getbbox()
    if box:
        left, top, right, bottom = box
        im.paste(layer, (int(round((w - (right - left)) / 2 - left + dx)),
                         int(round((h - (bottom - top)) / 2 - top + dy))), layer)
    return im


_MEASURED = {}


def measured(text, fnt, track=0.0):
    """Where the marks land when this string is actually drawn, found by drawing it.

    textbbox reports a layout box, which on this face runs several points wider than the ink and
    offset from it, so anything centred on that number is centred on padding. Rendering to a
    scratch bitmap and reading the pixels is the only measurement that agrees with the picture.

    Returns the offset from the draw origin to the first mark, and the size of the marks.
    """
    key = (text, getattr(fnt, "path", ""), getattr(fnt, "size", 0), round(track, 3))
    if key in _MEASURED:
        return _MEASURED[key]
    pad = int(getattr(fnt, "size", 24) * 2) + 8
    scratch = Image.new("L", (int(pad * 2 + sum(fnt.getlength(c) + track for c in text) + 8),
                              pad * 2 + int(getattr(fnt, "size", 24) * 2)), 0)
    sd = ImageDraw.Draw(scratch)
    if track:
        x = float(pad)
        for ch in text:  # letterspaced text is drawn a glyph at a time, so measure it that way
            sd.text((x, pad), ch, font=fnt, fill=255)
            x += sd.textlength(ch, font=fnt) + track
    else:
        sd.text((pad, pad), text, font=fnt, fill=255)  # and plain text in one piece, for the kerning
    box = scratch.getbbox()
    out = (0.0, 0.0, 0.0, 0.0) if not box else \
        (box[0] - pad, box[1] - pad, box[2] - box[0], box[3] - box[1])
    _MEASURED[key] = out
    return out


def width(draw, text, fnt, track=0.0):
    """The width of the marks themselves."""
    return measured(text, fnt, track)[2]


def spaced_mid(draw, cx, y, text, fnt, fill, track=0.0):
    """Draw letterspaced text with its marks centred on cx."""
    left, _, w, _ = measured(text, fnt, track)
    return spaced(draw, (cx - w / 2 - left, y), text, fnt, fill, track)


def text_mid(draw, cx, y, text, fnt, fill):
    """Draw text with its marks centred on cx."""
    left, _, w, _ = measured(text, fnt)
    draw.text((cx - w / 2 - left, y), text, font=fnt, fill=fill)


def ink(d, text, fnt, track=0.0):
    """Where the marks land relative to the draw origin, and how big they are."""
    return measured(text, fnt, track)


def badge(d, x, y, size, fg=VERMILLION, bg=PAPER):
    """The mark: AI in a filled box, the letters centred on their ink rather than their metrics."""
    f = font("barlow-condensed-800", size)
    left, top, iw, ih = ink(d, "AI", f)
    pad = size * 0.30
    bw, bh = iw + pad * 2, ih + pad * 1.5
    d.rectangle([x, y, x + bw, y + bh], fill=bg)
    d.text((x + (bw - iw) / 2 - left, y + (bh - ih) / 2 - top), "AI", font=f, fill=fg)
    return bw, bh


def wordmark(d, x, y, size, fill=PAPER, back=VERMILLION):
    """AI in a box, then FEAR REPORT, sitting on one optical baseline. Returns width and height."""
    bw, bh = badge(d, x, y, size, fg=back, bg=fill)
    gap = size * 0.34
    f = font("barlow-condensed-800", size * 1.16)
    left, top, tw, th = ink(d, "FEAR REPORT", f)
    d.text((x + bw + gap - left, y + (bh - th) / 2 - top), "FEAR REPORT", font=f, fill=fill)
    return bw + gap + tw, bh


def wordmark_size(d, size):
    f = font("barlow-condensed-800", size)
    _, _, iw, ih = ink(d, "AI", f)
    _, _, tw, _ = ink(d, "FEAR REPORT", font("barlow-condensed-800", size * 1.16))
    return iw + size * 0.60 + size * 0.34 + tw, ih + size * 0.45


def avatar(path, size=800, label=True, bg=VERMILLION, fg=PAPER):
    """Square, shown as a circle, so the whole mark is centred as one block.

    With the name under it, the block is the badge plus the gap plus the name, and that block is
    what gets centred, not the badge alone.
    """
    mark = int(size * (0.34 if label else 0.44))
    d = ImageDraw.Draw(Image.new("RGB", (size, size)))
    f = font("barlow-condensed-800", mark)
    left, top, iw, ih = ink(d, "AI", f)
    pad = mark * 0.30
    bw, bh = iw + pad * 2, ih + pad * 1.5

    f2 = font("barlow-condensed-800", int(size * 0.105))
    track = size * 0.011
    _, l_top, _, lh = ink(d, "FEAR REPORT", f2)
    gap = size * 0.058
    block = bh + gap + lh if label else bh

    def paint(g):
        x, y = (size - bw) / 2, (size - block) / 2
        g.rectangle([x, y, x + bw, y + bh], fill=fg)
        g.text((x + (bw - iw) / 2 - left, y + (bh - ih) / 2 - top), "AI", font=f, fill=bg)
        if label:
            spaced_mid(g, size / 2, y + bh + gap - l_top, "FEAR REPORT", f2, fg, track)

    im = compose(size, size, bg, paint)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)
    return path


def header(path, w=1500, h=500, bg=PAPER, fg=VERMILLION):
    """The banner: the mark and the address, nothing else.

    Inverted against everything else, so the profile does not read as one flat orange field once
    the avatar sits on top of it.
    """
    d = ImageDraw.Draw(Image.new("RGB", (w, h)))
    mark = 116
    f_tag = font("barlow-500", 40)
    f_url = font("ibm-plex-mono-600", 34)
    block_w, block_h = wordmark_size(d, mark)
    gap = 46
    _, _, _, uh = ink(d, "aifearreport.com", f_url)
    total = block_h + gap + uh
    _, u_top, _, _ = ink(d, "aifearreport.com", f_url, 3.0)

    def paint(g):
        top = (h - total) / 2
        wordmark(g, (w - block_w) / 2, top, mark, fill=fg, back=bg)
        spaced_mid(g, w / 2, top + block_h + gap - u_top, "aifearreport.com", f_url, fg, 3.0)

    im = compose(w, h, bg, paint)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)
    return path


def share(path, tagline="Every fear about AI, and what it buys.", w=1200, h=630):
    """The picture that stands in for the site wherever a link to it appears.

    The front page's other card stamps the index on it, which is the right thing for a card about
    today and the wrong thing for one that will sit pinned on a profile and under every share of
    the address for as long as the site exists. This one says only what stays true.
    """
    d = ImageDraw.Draw(Image.new("RGB", (w, h)))
    mark = 104
    f_tag = font("barlow-500", 40)
    f_url = font("ibm-plex-mono-600", 28)
    block_w, block_h = wordmark_size(d, mark)
    g1, g2 = 44, 34
    _, _, _, th = ink(d, tagline, f_tag)
    _, _, _, uh = ink(d, "aifearreport.com", f_url)
    _, t_top, _, _ = ink(d, tagline, f_tag)
    _, u_top, _, _ = ink(d, "aifearreport.com", f_url, 2.8)

    def paint(g):
        top = (h - (block_h + g1 + th + g2 + uh)) / 2
        wordmark(g, (w - block_w) / 2, top, mark)
        text_mid(g, w / 2, top + block_h + g1 - t_top, tagline, f_tag, PAPER)
        spaced_mid(g, w / 2, top + block_h + g1 + th + g2 - u_top, "aifearreport.com", f_url, PAPER, 2.8)

    im = compose(w, h, VERMILLION, paint)
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
    _, mark_h = wordmark(d, pad, pad + 24, mark)

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
    print(share(out / "share.png"))
    print(plate(out / "plate.png", "Saturday 19 September 2026"))


if __name__ == "__main__":
    main()
