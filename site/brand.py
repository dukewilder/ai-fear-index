"""The account's fixed images: the avatar, the header, and the plate a brief goes out on.

These are the parts that never change, so they are drawn once and committed rather than built
every run. The card is the same mark at 1600 by 900, which is the size X shows at full width.

    python -m site.brand --out site/static/brand
"""
import argparse
import math
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


# How much room the letters get on each side of the square, as a fraction of the type size.
BADGE_PAD = 0.22
# The letters are not centred on their box but on their weight. "AI" is a triangle with a hole in
# it beside a solid bar, so its ink sits 3.4% right and 3.2% low of the middle of its own bounding
# box, and a box-centred mark reads as sitting low. Measured, not guessed: render AI at 400pt and
# the ink box is 277 by 280 while the centre of mass is at 147.8, 149.0.
#
# Half of that offset, not all of it. Drawn at 0, 50 and 100 per cent and looked at: the whole
# correction overshoots and leaves a visible gap below and to the right, which is the same fault
# the other way round. Half lands even.
BADGE_NUDGE = (-0.017, -0.016)


def badge_side(d, size):
    """The side of the square the letters sit in.

    Square, because a mark that is itself square reads as broken when its box is not. The letters
    measure 191 by 193 at the avatar's size and the box around them was 354 by 315: a third more
    air at the sides than above, which is what made a centred mark look uncentred.
    """
    f = font("barlow-condensed-800", size)
    _, _, iw, ih = ink(d, "AI", f)
    return max(iw, ih) + size * BADGE_PAD * 2


def badge(d, x, y, size, fg=VERMILLION, bg=PAPER):
    """The mark: AI in a square, on its weight rather than its metrics. Returns the square."""
    f = font("barlow-condensed-800", size)
    left, top, iw, ih = ink(d, "AI", f)
    # Rounded once, here, and the letters placed on the rounded box. Rounding each corner of the
    # rectangle separately let a square come out a pixel wider than it was tall whenever it landed
    # on a fraction, which is how the banner's mark measured 134 by 135.
    x0, y0, s = int(round(x)), int(round(y)), int(round(badge_side(d, size)))
    nx, ny = BADGE_NUDGE
    d.rectangle([x0, y0, x0 + s, y0 + s], fill=bg)
    d.text((x0 + (s - iw) / 2 - left + iw * nx, y0 + (s - ih) / 2 - top + ih * ny),
           "AI", font=f, fill=fg)
    return s, s


# The tab icon is not the avatar. The avatar is the mark inset in a field, drawn to be cut to a
# circle at profile size, and using it in a tab stacks three shapes inside sixteen pixels: a red
# border, a cream square, then the letters. At that size it reads as a smudge with a hole in it.
# The tab gets the letters on a solid field and nothing else.
#
# 0.62 of the frame, not more: the 512 goes in the manifest, where Android may mask it to a
# circle, and the inscribed circle is 0.707 of the square. At 0.62 the letters clear the cut.
ICON_IMAGE_HEIGHT = 0.62


def icon(path, size=512, bg=VERMILLION, fg=PAPER):
    """The mark at tab size: AI on a solid field, centred on the ink it actually leaves.

    Centred through compose(), which paints first and measures the result, rather than through
    the square's own arithmetic. That arithmetic is fine at 800 pixels and wrong at 32: the
    nudge rounds away to nothing, the halving lands on a half pixel, and the 32 that shipped sat
    1.5 pixels right of centre, which is 7.5% of its box and plainly visible in a tab.
    """
    d = ImageDraw.Draw(Image.new("RGB", (size, size)))
    want = size * ICON_IMAGE_HEIGHT
    pt = max(1, int(round(size * 1.35)))
    for _ in range(6):   # point size is not cap height; converge on the ink instead of guessing
        _, _, _, ih = ink(d, "AI", font("barlow-condensed-800", pt))
        if not ih or abs(ih - want) <= 0.5:
            break
        pt = max(1, int(round(pt * want / ih)))
    f = font("barlow-condensed-800", pt)
    _, _, iw, ih = ink(d, "AI", f)

    def paint(g):
        left, top, _, _ = ink(g, "AI", f)
        g.text((-left, -top), "AI", font=f, fill=fg)

    # Where the ink should start, worked out here rather than left to compose's rounding. The
    # nudge is a fraction of the ink, so at 16 pixels it is worth a sixth of one and rounds away
    # to nothing; what survives at every size is which way the odd pixel goes when the leftover
    # room will not halve. ceil(x - 0.5) rounds a tie down, which spends that pixel on the side
    # the nudge points away from, so a 9-pixel-tall AI in a 16-pixel frame sits 3 above and 4
    # below rather than the other way round. The shipped 32 had it 1.5 pixels off, 7.5% of its
    # box, because the same arithmetic that is harmless at 800 pixels is not at 32.
    nx, ny = BADGE_NUDGE
    want_x = math.ceil((size - iw) / 2 + iw * nx - 0.5)
    want_y = math.ceil((size - ih) / 2 + ih * ny - 0.5)
    im = compose(size, size, bg, paint,
                 dx=want_x - (size - iw) / 2, dy=want_y - (size - ih) / 2)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)
    return path


def wordmark(d, x, y, size, fill=PAPER, back=VERMILLION):
    """AI in a box, then FEAR REPORT, sitting on one optical baseline. Returns width and height."""
    bw, bh = badge(d, x, y, size, fg=back, bg=fill)
    gap = size * 0.34
    f = font("barlow-condensed-800", size * 1.16)
    left, top, tw, th = ink(d, "FEAR REPORT", f)
    d.text((x + bw + gap - left, y + (bh - th) / 2 - top), "FEAR REPORT", font=f, fill=fill)
    return bw + gap + tw, bh


def wordmark_size(d, size):
    """What wordmark() will occupy, from the same numbers it draws with."""
    side = badge_side(d, size)
    _, _, tw, _ = ink(d, "FEAR REPORT", font("barlow-condensed-800", size * 1.16))
    return side + size * 0.34 + tw, side


def avatar(path, size=800, label=True, bg=VERMILLION, fg=PAPER):
    """Square, shown as a circle, so the whole mark is centred as one block.

    With the name under it, the block is the badge plus the gap plus the name, and that block is
    what gets centred, not the badge alone.
    """
    # Without the name the square carries the whole picture, so it grows. 0.52 leaves an even
    # ring of ground once the circle is cut; at 0.58 the corners crowd the edge and the ring
    # goes thin where they reach, and at 0.44 the mark floats in a field of red.
    mark = int(size * (0.34 if label else 0.52))
    d = ImageDraw.Draw(Image.new("RGB", (size, size)))
    side = badge_side(d, mark)   # the same square the wordmark draws, so the two cannot drift

    f2 = font("barlow-condensed-800", int(size * 0.105))
    track = size * 0.011
    _, l_top, _, lh = ink(d, "FEAR REPORT", f2)
    gap = size * 0.058
    block = side + gap + lh if label else side

    def paint(g):
        x, y = (size - side) / 2, (size - block) / 2
        badge(g, x, y, mark, fg=bg, bg=fg)
        if label:
            spaced_mid(g, size / 2, y + side + gap - l_top, "FEAR REPORT", f2, fg, track)

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


HEAD_SIZES = (100, 92, 84, 76, 68, 60)


def fit_headline(d, headline, room_across, room_down, track=1.6):
    """The largest size at which the whole headline fits the space, and how it breaks.

    The card used to set three lines at one size and drop whatever did not fit, so an eleven word
    headline went out reading "...UNDER ENVIRONMENTAL" and stopping. A smaller headline is still
    the headline. Half of one is a different claim.
    """
    for size in HEAD_SIZES:
        f = font("barlow-condensed-800", size)
        lines = wrap(d, headline.upper(), f, room_across, track)
        step = round(size * 1.04)
        if len(lines) * step <= room_down:
            return f, lines, step
    return f, lines, step  # smaller than this is unreadable; it runs long rather than losing words


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

    y = pad + 24 + mark * 1.62 + 132
    room = (h - pad - 30) - 34 - y   # down to the address, with a line's clearance above it
    if headline:
        f_head, lines, step = fit_headline(d, headline, w - pad * 2, room)
        d.line([(pad, y - 56), (pad + 170, y - 56)], fill=PAPER, width=5)
        for line in lines:
            spaced(d, (pad, y), line, f_head, PAPER, 1.6)
            y += step

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
