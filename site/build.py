#!/usr/bin/env python3
"""Render the AI Fear Report site from one data file.

Production (one folder per page, ready for GitHub Pages):
    python site/build.py --data data/site_data.json --out dist

Preview (every page in one HTML file, hash navigation):
    python site/build.py --data state/site_data.json --preview preview.html

The pipeline writes data/site_data.json in the same shape as placeholder.json,
so the pages never change when real data replaces the placeholders.
"""
import argparse
import datetime as dt
import html
import json
import hashlib
import pathlib
import re
import shutil

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = pathlib.Path(__file__).resolve().parent
CHEVRON = ('<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">'
           '<path d="M10 3 5 8l5 5" fill="none" stroke="currentColor" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"/></svg>')
NAV_FOR = {"home": "", "fear": "", "org": "", "feed": "feed", "method": "method", "states": "states",
           "state": "states", "bills": "bills"}


def esc(value):
    return html.escape(str(value), quote=True)


def qx(quarter):
    """x position of a quarter (0 to 11) on the 350-wide timeline."""
    return round(24 + quarter * 27.45, 1)


def sparkline_svg(series, label):
    """Running total over 13 weekly points, with the numbers on it and a value on hover."""
    w, h = 350, 96
    vals = [v for _, v in series]
    lo, hi = min(vals), max(vals)
    n = len(series)
    pts = []
    for i, (day, v) in enumerate(series):
        x = 8 + i * (w - 16) / (n - 1)
        y = h - 22 - (0.5 if hi == lo else (v - lo) / (hi - lo)) * (h - 46)
        pts.append((round(x, 1), round(y, 1), day, v))
    poly = " ".join(f"{x},{y}" for x, y, _, _ in pts)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">',
           f'<line class="sp-base" x1="0" y1="{h - 20}" x2="{w}" y2="{h - 20}"/>',
           f'<polyline class="sp-line" points="{poly}"/>']
    for i, (x, y, day, v) in enumerate(pts):
        nice = dt.date.fromisoformat(day).strftime("%b %-d")
        out.append(f'<circle class="sp-pt" cx="{x}" cy="{y}" r="7" data-tip="{esc(nice)}: {v:,}"><title>{esc(nice)}: {v:,}</title></circle>')
    fx, fy, fday, fv = pts[0]
    lx, ly, lday, lv = pts[-1]
    out.append(f'<text class="sp-val" x="{fx}" y="{fy - 9}" text-anchor="start">{fv:,}</text>')
    out.append(f'<text class="sp-val sp-val-end" x="{lx}" y="{ly - 9}" text-anchor="end">{lv:,}</text>')
    out.append(f'<circle class="sp-dot" cx="{lx}" cy="{ly}" r="4.5"/>')
    out.append(f'<text class="sp-axis" x="{fx}" y="{h - 5}" text-anchor="start">{esc(dt.date.fromisoformat(fday).strftime("%b %-d"))}</text>')
    out.append(f'<text class="sp-axis" x="{lx}" y="{h - 5}" text-anchor="end">today</text>')
    out.append("</svg>")
    return "".join(out)


def timeline_svg(t):
    p = []

    def label(x, y, text, anchor="start"):
        p.append(f'<text class="tl-lbl" x="{x}" y="{y}" text-anchor="{anchor}">{esc(text)}</text>')

    lanes = t.get("lanes") or ["Money in", "Fear messaging", "Public concern"]
    label(8, 38, lanes[0])
    label(8, 108, lanes[1])
    label(8, 182, lanes[2])
    for i, year in enumerate(t["years"]):
        label(65 + i * 110, 270, year, "middle")
    p.append('<line class="tl-base" x1="10" y1="86" x2="340" y2="86"/>')
    p.append('<line class="tl-base" x1="10" y1="162" x2="340" y2="162"/>')
    p.append('<line class="tl-axis" x1="10" y1="252" x2="340" y2="252"/>')
    for x in (10, 120, 230, 340):
        p.append(f'<line class="tl-axis" x1="{x}" y1="252" x2="{x}" y2="257"/>')
    for ev in t.get("events") or []:
        x = qx(ev["q"])
        for y1, y2 in ((22, 88), (114, 162), (188, 250)):
            p.append(f'<line class="tl-ev" x1="{x}" y1="{y1}" x2="{x}" y2="{y2}"/>')
        p.append(f'<circle class="tl-evc" cx="{x}" cy="12" r="8"/>')
        p.append(f'<text class="tl-evn" x="{x}" y="16" text-anchor="middle">{esc(ev["n"])}</text>')
    quarters = t.get("quarters") or [f"Q{i}" for i in range(12)]
    raw_f, raw_b, raw_w = t.get("funding_raw") or {}, t.get("bills_raw") or {}, t.get("wiki_raw") or {}
    for quarter, height in t.get("funding") or []:
        tip = f"{quarters[quarter]}: {raw_f.get(str(quarter), '')} on lobbying filings naming this fear"
        p.append(f'<rect class="tl-money" x="{qx(quarter) - 8}" y="{86 - height}" width="16" height="{height}" '
                 f'data-tip="{esc(tip)}"><title>{esc(tip)}</title></rect>')
    for quarter, count in enumerate(t.get("messages") or []):
        n = raw_b.get(str(quarter), 0)
        tip = f"{quarters[quarter]}: {n} bill{'' if n == 1 else 's'}"
        for k in range(count):
            p.append(f'<circle class="tl-msg" cx="{qx(quarter)}" cy="{155 - 11 * k}" r="5" data-tip="{esc(tip)}">'
                     f'<title>{esc(tip)}</title></circle>')
    pts = [(qx(q), round(240 - v * 0.5, 1), q) for q, v in t.get("concern") or []]
    if len(pts) > 1:
        p.append('<polyline class="tl-line" points="' + " ".join(f"{x},{y}" for x, y, _ in pts) + '"/>')
    for x, y, q in pts:
        tip = f"{quarters[q]}: {raw_w.get(str(q), '')} Wikipedia views"
        p.append(f'<circle class="tl-pt" cx="{x}" cy="{y}" r="6" data-tip="{esc(tip)}"><title>{esc(tip)}</title></circle>')
    pts = [(x, y) for x, y, _ in pts]
    if pts:
        (fx, fy), (lx, ly) = pts[0], pts[-1]
        if t.get("first_label"):
            fy_label = fy + 20 if fy < 200 else fy - 10  # keep clear of the lane title at the top left
            p.append(f'<text class="tl-val" x="{fx + 14}" y="{fy_label}" text-anchor="middle">{esc(t["first_label"])}</text>')
        if t.get("last_label") and len(pts) > 1:
            p.append(f'<text class="tl-val" x="{lx}" y="{ly - 10}" text-anchor="middle">{esc(t["last_label"])}</text>')
    elif not t.get("concern"):
        label(175, 222, "No attention series for this fear yet", "middle")
    return f'<svg viewBox="0 0 350 280" role="img" aria-label="{esc(t["aria"])}">' + "".join(p) + "</svg>"


# ---------------------------------------------------------------- share cards
CARD_W, CARD_H = 1200, 630


def wrap_text(draw, text, font, width):
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = (line + " " + w).strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def share_card(path, big, label, sub="", kicker="", foot="aifearreport.com", h=None):
    """A 1200 by 630 card in the site's own style: paper, ink, one stamped figure.

    The mark itself goes on it, drawn by the same function that draws the avatar and the banner.
    It used to set the words "AI FEAR REPORT" in a red box in whatever face PIL had lying around,
    which is a card carrying a picture of the name rather than the mark, in the wrong typeface.
    """
    from PIL import Image, ImageDraw
    from brand import font as bfont, ink as bink, wordmark, PAPER, INK, VERMILLION
    CARD_H = h or globals()["CARD_H"]
    im = Image.new("RGB", (CARD_W, CARD_H), PAPER)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, CARD_W - 1, CARD_H - 1], outline=INK, width=3)
    band = 118
    d.line([(0, band), (CARD_W, band)], fill=INK, width=2)
    mark = 44
    wordmark(d, 60, (band - mark * 1.14) / 2, mark, fill=VERMILLION, back=PAPER)

    f_lab = bfont("barlow-condensed-700", 52)
    f_sub = bfont("barlow-500", 30)
    f_foot = bfont("ibm-plex-mono-500", 24)

    def block_for(big_size):
        """The rows and their height at this size for the figure."""
        f = bfont("barlow-condensed-800", big_size)
        rows = [(big, f, INK, 30)]
        rows += [(l, f_lab, INK, 16) for l in wrap_text(d, label, f_lab, CARD_W - 120)[:2]]
        if sub:
            rows[-1] = (*rows[-1][:3], 30)
            rows += [(l, f_sub, "#5D584D", 14) for l in wrap_text(d, sub, f_sub, CARD_W - 120)[:2]]
        hs = [bink(d, t, ff)[3] for t, ff, _, _ in rows]
        return rows, hs, sum(hs) + sum(g for *_, g in rows[:-1])

    # The figure gives way to the words, rather than the block running through the rules at both
    # ends. A two-line name with a two-line line of counts under it overflowed by sixty pixels and
    # clipped the top of the figure against the rule under the mark.
    room = CARD_H - 74 - band - 48
    for big_size in (210 if len(big) <= 5 else 160, 180, 150, 120, 96):
        rows, heights, block = block_for(big_size)
        if block <= room:
            break
    # Measured first, then set as one block in the middle of the space it has. Stacking downwards
    # from a fixed start left the figure high and a third of the card empty under it.
    y = band + max(24, (CARD_H - 74 - band - block) / 2)
    for (text, f, colour, gap), h in zip(rows, heights):
        lx, ty, _, _ = bink(d, text, f)
        d.text((60 - lx, y - ty), text, font=f, fill=colour)
        y += h + gap
    d.line([(60, CARD_H - 74), (CARD_W - 60, CARD_H - 74)], fill=INK, width=2)
    lf, tf, _, hf = bink(d, foot, f_foot)
    d.text((60 - lf, CARD_H - 74 + (74 - hf) / 2 - tf), foot, font=f_foot, fill="#5D584D")
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)


def card_specs(d):
    """Which cards to draw: one per fear and one per state, and the list of every bill. The front
    page has the standing one."""
    if d.get("bills_meta"):
        m = d["bills_meta"]
        yield ("bills", f"{m['total']:,}", "AI bills, resolutions, rules and orders since January 2025",
               f"{m['controlled']:,} would put AI under new government control. By state, fear and control.")
    for f in d["fear_pages"]:
        yield f"fear-{f['slug']}", f["score"], f["name"], \
            f"{f['score']} of 100" + (f" · {f['line']}" if f.get("line") else "")
    for p in d.get("state_pages") or []:
        if p["n"]:
            yield (f"state-{p['slug']}", f"{p['c']:,}", p["name"],
                   f"AI measure{'' if p['c'] == 1 else 's'} that would put AI under new government control, "
                   f"of {p['n']:,} since January 2025")
        else:
            yield f"state-{p['slug']}", "0", p["name"], "No AI measures on file here yet"


# The map card is drawn in the light theme's heat colours, the same five the page uses.
HEAT = ["#EDE9DE", "#EFDCD0", "#DFB09C", "#C76F55", "#B3321F"]


def map_card(path, m, foot="aifearreport.com", h=None):
    """The states page's card: the tile map itself, which is the thing people share it for."""
    from PIL import Image, ImageDraw
    from brand import font as bfont, ink as bink, wordmark, PAPER, INK, VERMILLION
    CARD_H = h or globals()["CARD_H"]
    im = Image.new("RGB", (CARD_W, CARD_H), PAPER)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, CARD_W - 1, CARD_H - 1], outline=INK, width=3)
    band = 118
    d.line([(0, band), (CARD_W, band)], fill=INK, width=2)
    mark = 44
    wordmark(d, 60, (band - mark * 1.14) / 2, mark, fill=VERMILLION, back=PAPER)
    foot_y = CARD_H - 74
    # the grid on the right, as large as the space between the rules allows
    cell, gap = 44, 4
    gx = CARD_W - 60 - 11 * cell - 10 * gap
    gy = band + (foot_y - band - (8 * cell + 7 * gap)) / 2
    f_abbr = bfont("ibm-plex-mono-600", 15)
    for t in m["tiles"]:
        x = gx + t["col"] * (cell + gap)
        y = gy + t["row"] * (cell + gap)
        lv = t["level"]
        d.rectangle([x, y, x + cell, y + cell], fill=HEAT[lv], outline="#CDC6B5")
        lx, ty, w, h = bink(d, t["abbr"], f_abbr)
        d.text((x + (cell - w) / 2 - lx, y + (cell - h) / 2 - ty), t["abbr"], font=f_abbr,
               fill="#FBF9F4" if lv == 4 else INK)
    # the words on the left
    f_head = bfont("barlow-condensed-800", 64)
    f_sub = bfont("barlow-500", 26)
    f_key = bfont("ibm-plex-mono-500", 18)
    left, room = 60, gx - 60 - 40
    y = band + 40
    for line in wrap_text(d, "STATE BY STATE", f_head, room):
        lx, ty, _, h = bink(d, line, f_head)
        d.text((left - lx, y - ty), line, font=f_head, fill=INK)
        y += h + 10
    y += 14
    for line in wrap_text(d, "AI measures that would put AI, the people building it or the people using it "
                             "under new government control", f_sub, room)[:5]:
        lx, ty, _, h = bink(d, line, f_sub)
        d.text((left - lx, y - ty), line, font=f_sub, fill="#33302A")
        y += h + 11
    ky = foot_y - 64
    kx = left
    lx, ty, w, h = bink(d, "0", f_key)
    d.text((kx - lx, ky - ty), "0", font=f_key, fill="#5D584D")
    kx += w + 10
    for c in HEAT:
        d.rectangle([kx, ky - 2, kx + 28, ky + 16], fill=c, outline="#CDC6B5")
        kx += 32
    top = f"{m.get('top', 0):,}"
    lx, ty, w, h = bink(d, top, f_key)
    d.text((kx + 6 - lx, ky - ty), top, font=f_key, fill="#5D584D")
    d.line([(60, foot_y), (CARD_W - 60, foot_y)], fill=INK, width=2)
    f_foot = bfont("ibm-plex-mono-500", 24)
    lf, tf, _, hf = bink(d, foot, f_foot)
    d.text((60 - lf, foot_y + (74 - hf) / 2 - tf), foot, font=f_foot, fill="#5D584D")
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)


def make_url(preview, base):
    routes = {"home": "", "feed": "feed", "method": "method", "states": "states", "bills": "bills"}

    def url(kind, slug=None, anchor=None):
        if kind in ("fear", "org"):
            path = f"{kind}/{slug}"
        elif kind == "state":
            path = f"states/{slug}"
        else:
            path = routes[kind]
        if preview:
            if anchor:
                path = f"{path}/{anchor}" if path else anchor
            return "#/" + path
        return f"{base}/{path + '/' if path else ''}" + (f"#{anchor}" if anchor else "")

    return url


def site_name(d):
    """The site is named in config, so a rename is one line there."""
    return (d.get("site") or {}).get("name") or "AI Fear Report"


# A place page with fewer measures than this is left out of search until it has more. The page is
# still there for the map to link to; it is the search result that would be thin, not the square.
THIN = 3


def count(n, one, many=None):
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def place_head(p):
    """A place page's title: the words people search for, then this place's own numbers.

    Each title carries its own counts, so no two read the same with only the name swapped, which
    is the boilerplate Google says it rewrites. "Measures" rather than "bills" in the count, since a
    state's count includes resolutions and the title is held to the same standard as the page.
    """
    n, c = p["n"], p["c"]
    control = f"{c:,} would add government control" if c else "none would add government control"
    if p.get("kind") == "rules and executive orders":
        return f"Federal AI rules and executive orders: {n:,} since 2025, {control}"
    if p.get("code") == "us":
        return f"AI bills in Congress: {count(n, 'measure')} since 2025, {control}"
    return f"{p['name']} AI bills and laws: {count(n, 'measure')} since 2025, {control}"


def fear_head(f):
    """A fear page's title: the fear, then how many bills cite it and how many filings name it."""
    parts = {x.get("name"): str(x.get("value") or "0") for x in f.get("parts") or []}
    bills, filings = parts.get("Bills", "0"), parts.get("Lobbying filings", "0")
    filed = f"lobbying filing{'' if filings == '1' else 's'} in the past year"
    if bills != "0":
        return f"{f['name']}: {bills} AI bill{'' if bills == '1' else 's'} since 2025, {filings} {filed}"
    if filings != "0":
        return f"{f['name']}: {filings} AI {filed}"
    return f"{f['name']}: the AI bills and the money"


def org_head(o):
    """An organization's title: its name, then the money, worded the way its page words it."""
    if o.get("group") == "election":
        return f"{o['name']}: {o['spent']} raised for AI politics this cycle"
    if " and " in (o.get("breakdown") or ""):  # lobbying and election money both, added together
        return f"{o['name']}: AI lobbying and election money, {o['spent']} in all"
    return f"{o['name']}: {o['spent']} reported on AI lobbying filings, past year"


def page_specs(d):
    name = site_name(d)
    tagline = (d.get("site") or {}).get("tagline")
    specs = [{"kind": "home", "route": "", "out": "index.html", "template": "home.html",
              "head": name, "title": f"{name}: {tagline}" if tagline else name, "ctx": {}}]
    for f in d["fear_pages"]:
        specs.append({"kind": "fear", "route": f"fear/{f['slug']}", "out": f"fear/{f['slug']}/index.html",
                      "template": "fear.html", "head": fear_head(f), "crumb": f["name"],
                      "ctx": {"f": f, "timeline": timeline_svg(f["timeline"])}})
    for o in d["org_pages"]:
        specs.append({"kind": "org", "route": f"org/{o['slug']}", "out": f"org/{o['slug']}/index.html",
                      "template": "org.html", "head": org_head(o), "crumb": o["name"], "ctx": {"o": o}})
    for kind, head, crumb in (("feed", "Feed: AI bills, rules and filings as they arrive", "Feed"),
                              ("method", "How the numbers work", "How the numbers work")):
        specs.append({"kind": kind, "route": kind, "out": f"{kind}/index.html", "template": f"{kind}.html",
                      "head": head, "crumb": crumb, "ctx": {}})
    if d.get("bills"):
        n = len(d["bills"])
        specs.append({"kind": "bills", "route": "bills", "out": "bills/index.html", "template": "bills.html",
                      "head": f"All {n:,} US AI bills since 2025, by state, fear and control", "crumb": "Every bill",
                      "ctx": {"fear_name": {f["slug"]: f["name"] for f in d["bills_meta"]["fears"]},
                              "control_chip": {c["slug"]: c["chip"] for c in d["bills_meta"]["controls"]}}})
    if d.get("state_pages"):
        specs.append({"kind": "states", "route": "states", "out": "states/index.html", "template": "states.html",
                      "head": "AI bills and laws by state: a map of where they would add government control",
                      "crumb": "The states", "ctx": {}})
        for p in d["state_pages"]:
            specs.append({"kind": "state", "route": f"states/{p['slug']}", "out": f"states/{p['slug']}/index.html",
                          "template": "state.html", "head": place_head(p), "crumb": p["name"],
                          "robots": "noindex" if p["n"] < THIN else None, "ctx": {"p": p}})
    for s in specs:
        s.setdefault("title", f"{s['head']} | {name}")
        s["robots"] = s.get("robots") or "max-image-preview:large"
        s["nav"] = NAV_FOR[s["kind"]]
        s["card"] = (f"fear-{s['ctx']['f']['slug']}" if s["kind"] == "fear" else
                     f"state-{s['ctx']['p']['slug']}" if s["kind"] == "state" else
                     s["kind"] if s["kind"] in ("states", "bills") else None)
        s["description"] = None
        if s["kind"] == "fear":
            f = s["ctx"]["f"]
            s["description"] = (f"{f['name']}: how loud the fear is, who lobbies on it, and the bills "
                                f"that cite it. Scores {f['score']} of 100 on the Fear Index. "
                                f"{f.get('line') or ''}").strip()
        elif s["kind"] == "home":
            s["description"] = (f"How fears about AI show up in bills, lobbying and election money. "
                                f"{d['index']['value']} {d['index']['text']}, the government controls "
                                f"they carry and the offices they would hand power to, updated around the clock.")
        elif s["kind"] == "org":
            o = s["ctx"]["o"]
            s["description"] = (f"{o['name']}: money raised for AI politics this cycle and the donors "
                                f"behind it, from FEC filings." if o.get("group") == "election" else
                                f"{o['name']}: federal lobbying filings that name the fears tracked here, "
                                f"the amounts they report, and the bills they list.")
        elif s["kind"] == "feed":
            s["description"] = ("AI bills, rules, lobbying filings, donations and statements as they "
                                "arrive, tagged with the fears they cite and the controls they would impose.")
        elif s["kind"] == "method":
            s["description"] = ("Where every number on this site comes from: the government sources, "
                                "how measures are tagged, and what each label means.")
        elif s["kind"] == "bills":
            m = d["bills_meta"]
            s["description"] = (f"Every AI bill, resolution, rule and order in the US since January 2025, {m['total']:,} "
                                f"in all, by state, the fear it cites, the government control it would create and "
                                f"where it stands. {m['controlled']:,} would add government control.")
        elif s["kind"] == "states":
            s["description"] = ("Every state, DC and Puerto Rico, shaded by the AI measures that would put AI, "
                                "the people building it or the people using it under new government control, "
                                "with a page for each.")
        elif s["kind"] == "state":
            p = s["ctx"]["p"]
            s["description"] = (p["receipt"].rsplit(" http", 1)[0] if p["n"] else
                                f"{p['name']}: no AI measures on file here yet.")
    return specs


def page_url(site_url, route):
    return f"{site_url}/{route}{'/' if route else ''}"


# What changes on every build without the page saying anything new: the times the relative
# "updated 4 minutes ago" labels count from, and the stamps on image addresses. They are taken out
# before a page is fingerprinted, so its date in the sitemap moves only when what it says does.
VOLATILE = [re.compile(r'\sdata-rel="[^"]*"'),
            re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"),
            re.compile(r"\?v=[0-9a-f]{6,}")]


def fingerprint(page):
    text = "\n".join([page["title"], page.get("description") or "", page.get("robots") or "", page["html"]])
    for pattern in VOLATILE:
        text = pattern.sub("", text)
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def dated(pages, record, now, site_url):
    """Each page's last real change, carried from the last build's record.

    A page whose fingerprint matches the record keeps the date it had; one that differs, or is new,
    is dated now and marked pending, for the IndexNow step after publishing to announce. A page that
    has left the site is kept once, marked gone, so its removal is announced too, and then dropped.
    The sitemap used to carry the export time for every page, and a lastmod that says every page
    changed every twenty minutes is one search engines learn to ignore.
    """
    out = {}
    for page in pages:
        loc = page_url(site_url, page["route"])
        old = record.get(loc) or {}
        h = fingerprint(page)
        if old.get("hash") == h and not old.get("gone"):
            entry = dict(old)
        else:
            entry = {"hash": h, "lastmod": now, "sent": old.get("sent"), "pending": old.get("pending") or now}
        entry["index"] = page.get("robots") != "noindex"
        out[loc] = entry
    for loc, old in record.items():
        if loc in out:
            continue
        if not old.get("gone"):
            out[loc] = {"hash": None, "lastmod": now, "sent": old.get("sent"), "pending": now,
                        "gone": True, "index": False}
        elif old.get("pending"):
            out[loc] = old   # announced yet? not until the IndexNow step clears pending
    return out


def structured(page, d, site_url, name, image, logo):
    """What a page is, in the terms search engines read, as one JSON-LD graph.

    The site, its publisher and its dataset are said once, on the front page. They used to ride on
    every fear and organization page too, each claiming to be the website at its own address, which
    is 53 contradictory answers to "what is this site called". Every other page says where it sits,
    as a trail of breadcrumbs, and every page names its wide picture for Discover.
    """
    if not site_url:
        return None
    root = site_url + "/"
    url = page_url(site_url, page["route"])
    webpage = {"@type": "WebPage", "@id": url + "#page", "url": url, "name": page["title"],
               "isPartOf": {"@id": root + "#website"}, "inLanguage": "en-US"}
    if image:
        webpage["primaryImageOfPage"] = {"@type": "ImageObject", "url": image, "width": CARD_W, "height": WIDE_H}
    if page["kind"] == "home":
        org = {"@type": "Organization", "@id": root + "#organization", "name": name, "url": root}
        if logo:
            org["logo"] = {"@type": "ImageObject", "url": logo, "width": 512, "height": 512}
        same = []
        if (d.get("site") or {}).get("x_handle"):
            same.append(f"https://x.com/{d['site']['x_handle']}")
        if (d.get("links") or {}).get("code"):
            same.append(d["links"]["code"])
        if same:
            org["sameAs"] = same
        site = {"@type": "WebSite", "@id": root + "#website", "url": root, "name": name,
                "description": page.get("description") or "", "inLanguage": "en-US",
                "publisher": {"@id": root + "#organization"}}
        graph = [site, org, webpage]
        csv = (d.get("links") or {}).get("csv")
        if csv:
            folder = csv.rsplit("/", 1)[0]
            files = [("measures.csv", "Every AI measure on file since January 2025, with its status, the fears "
                                      "it cites, the controls it would create and the agencies it would hand power to."),
                     ("lobbying.csv", "Federal lobbying filings from the past year that mention AI, with client, "
                                      "registrant, amount, bills and the fears they mention."),
                     ("funders.csv", "Advocacy groups and foundations ranked by what they reported on lobbying "
                                     "filings that mention a tracked fear, past year."),
                     ("election.csv", "Political committees working on AI policy, ranked by the election money "
                                      "they raised this cycle.")]
            graph.append({
                "@type": "Dataset", "@id": root + "#dataset",
                "name": f"{name}: AI fears, the money behind them, and the laws that cite them",
                "description": page.get("description") or "", "url": root, "isAccessibleForFree": True,
                "license": "https://creativecommons.org/publicdomain/zero/1.0/",
                "creator": {"@id": root + "#organization"}, "publisher": {"@id": root + "#organization"},
                "temporalCoverage": "2025-01-01/..",
                "spatialCoverage": {"@type": "Place", "name": "United States"},
                "keywords": ["artificial intelligence", "AI regulation", "AI legislation", "AI policy",
                             "lobbying", "state legislation", "Congress", "AI risk", "data centers",
                             "deepfakes", "super PAC", "FEC"],
                "variableMeasured": ["status of each measure", "fears cited", "government controls",
                                     "agencies given new power", "lobbying amounts reported",
                                     "election money raised"],
                "distribution": [{"@type": "DataDownload", "encodingFormat": "text/csv",
                                  "contentUrl": f"{folder}/{f}", "name": f, "description": text}
                                 for f, text in files],
                "dateModified": (d.get("built_at") or "")[:10] or None})
        return {"@context": "https://schema.org", "@graph": graph}
    trail = [(name, root)]
    if page["kind"] == "state":
        trail.append(("The states", page_url(site_url, "states")))
    trail.append((page.get("crumb") or page["head"], url))
    webpage["breadcrumb"] = {"@id": url + "#breadcrumb"}
    crumbs = {"@type": "BreadcrumbList", "@id": url + "#breadcrumb",
              "itemListElement": [{"@type": "ListItem", "position": i, "name": n, "item": u}
                                  for i, (n, u) in enumerate(trail, 1)]}
    return {"@context": "https://schema.org", "@graph": [webpage, crumbs]}


# The same cards drawn 16:9 for search. Discover asks for an image at least 1200 wide and says 16:9
# works best; the 1200 by 630 ones stay the social cards, the shape X and the rest show whole.
WIDE_H = 675
FONTS = HERE / "static" / "fonts"


def build(data_path, out_dir=None, preview_path=None, base="", pages_path=None):
    d = json.loads(pathlib.Path(data_path).read_text())
    if d.get("built_at") in (None, "", "now"):
        d["built_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    preview = preview_path is not None
    name = site_name(d)
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    series = d["index"].get("series") or [[dt.date.today().isoformat(), v] for v in d["index"]["trend"]]
    env.globals.update(url=make_url(preview, base.rstrip("/")), d=d, chevron=CHEVRON, base=base.rstrip("/"),
                       spark_svg=sparkline_svg(series, d["index"].get("series_label") or "Last 90 days"))
    pages = []
    for spec in page_specs(d):
        spec["html"] = env.get_template("pages/" + spec["template"]).render(**spec["ctx"])
        pages.append(spec)
    shell = env.get_template("base.html")
    if preview:
        pathlib.Path(preview_path).write_text(shell.render(pages=pages, preview=True,
                                                           title=pages[0]["title"], nav="", home=True))
        return [preview_path]
    written = []
    site_url = (d.get("site_url") or "").rstrip("/")
    cards, wide, icons = {}, {}, {}
    try:
        import sys
        sys.path.insert(0, str(HERE))
        from brand import share as share_image

        def stamped(name):
            """The card's address, with a stamp of what is in it.

            The file keeps its name so nothing that already points at it breaks, but the address
            in the page changes the moment the picture does. Without that, every cache between a
            reader and the file is free to keep serving the card that was there first, and the
            first one is the one that gets kept longest.
            """
            if not site_url:
                return None
            body = (pathlib.Path(out_dir) / "og" / name).read_bytes()
            return f"{site_url}/og/{name}?v={hashlib.sha1(body).hexdigest()[:8]}"

        tagline = (d.get("site") or {}).get("tagline") or "Fear of AI, and what it buys."
        (pathlib.Path(out_dir) / "og" / "wide").mkdir(parents=True, exist_ok=True)
        share_image(pathlib.Path(out_dir) / "og" / "share.png", tagline)
        cards["share"] = stamped("share.png")
        share_image(pathlib.Path(out_dir) / "og" / "wide" / "share.png", tagline, h=WIDE_H)
        wide["share"] = stamped("wide/share.png")
        foot = site_url.replace("https://", "") or name
        for card, big, label, sub in card_specs(d):
            share_card(pathlib.Path(out_dir) / "og" / f"{card}.png", big, label, sub, kicker=name.upper(), foot=foot)
            cards[card] = stamped(f"{card}.png")
            share_card(pathlib.Path(out_dir) / "og" / "wide" / f"{card}.png", big, label, sub,
                       kicker=name.upper(), foot=foot, h=WIDE_H)
            wide[card] = stamped(f"wide/{card}.png")
        if d.get("state_map"):
            map_card(pathlib.Path(out_dir) / "og" / "states.png", d["state_map"], foot=foot)
            cards["states"] = stamped("states.png")
            map_card(pathlib.Path(out_dir) / "og" / "wide" / "states.png", d["state_map"], foot=foot, h=WIDE_H)
            wide["states"] = stamped("wide/states.png")
        # The tab, the bookmark, the home screen and the manifest all want a real file, and
        # they want the same mark. 16 because that is what a tab is at 1x, and a browser handed
        # only a 32 downsamples it itself and smears the letters.
        from brand import icon as mark_image
        for px in (16, 32, 180, 512):
            icon_file = pathlib.Path(out_dir) / "icon" / f"{px}.png"
            mark_image(icon_file, size=px)
            # Stamped like the cards, and for a sharper reason: a browser holds a favicon harder
            # than anything else it caches, so a reader who has seen the site once keeps the old
            # tab icon until something changes the address.
            icons[px] = f"/icon/{px}.png?v={hashlib.sha1(icon_file.read_bytes()).hexdigest()[:8]}"
    except Exception as exc:  # cards are a nicety; the pages must still build
        print("share cards skipped:", exc)
    # The type is served from the site itself. It came from Google Fonts, which put a stylesheet on
    # another host in front of every first paint, the one thing holding up the page on a phone, and
    # told Google the address of every reader. The files are the same faces, cut to the Latin set
    # the site uses, under the Open Font License, which travels with them.
    if FONTS.is_dir():
        (pathlib.Path(out_dir) / "fonts").mkdir(parents=True, exist_ok=True)
        for f in sorted(FONTS.iterdir()):
            if f.suffix in (".woff2", ".txt"):
                shutil.copyfile(f, pathlib.Path(out_dir) / "fonts" / f.name)
    logo = f"{site_url}/icon/512.png" if site_url and icons.get(512) else None
    for page in pages:
        target = pathlib.Path(out_dir) / page["out"]
        target.parent.mkdir(parents=True, exist_ok=True)
        route = page["route"]
        target.write_text(shell.render(
            pages=[page], preview=False, title=page["title"], og_title=page["head"], nav=page["nav"],
            home=page["kind"] == "home", og_image=cards.get(page.get("card")) or cards.get("share"),
            icons=icons, robots=page["robots"],
            ld=structured(page, d, site_url, name, wide.get(page.get("card")) or wide.get("share"), logo),
            canonical=page_url(site_url, route) if site_url else None,
            description=page.get("description")))
        written.append(str(target))
    # What GitHub Pages serves for a path that does not exist. It is built last and kept out of
    # the sitemap, so nothing points a crawler at it.
    gone = {"kind": "method", "route": "404", "out": "404.html", "template": "notfound.html",
            "title": f"Page not found | {name}", "head": "Page not found", "ctx": {}, "nav": "", "card": None,
            "description": "That page is not on the AI Fear Report."}
    gone["html"] = env.get_template("pages/notfound.html").render()
    target = pathlib.Path(out_dir) / gone["out"]
    target.write_text(shell.render(pages=[gone], preview=False, title=gone["title"], og_title=gone["head"],
                                   nav="", home=False, og_image=cards.get("share"), icons=icons,
                                   robots="noindex", ld=None, canonical=None, description=gone["description"]))
    written.append(str(target))
    if site_url:
        record = {}
        if pages_path and pathlib.Path(pages_path).exists():
            try:
                record = json.loads(pathlib.Path(pages_path).read_text())
            except ValueError:
                record = {}   # a damaged record costs one round of dates, not the build
        entries = dated(pages, record, d["built_at"], site_url)
        written += write_index_files(out_dir, site_url, pages, d, entries)
        if pages_path:
            pathlib.Path(pages_path).write_text(json.dumps(entries, indent=0, sort_keys=True) + "\n")
    return written


def write_index_files(out_dir, site_url, pages, d, entries=None):
    """robots.txt, a sitemap and the IndexNow key, so a crawler is told what exists rather than guessing.

    Each page in the sitemap carries the date its content last changed, from the record the build
    keeps; a page left out of search is left out of the sitemap too. changefreq and priority are
    gone: Google and Bing both say they ignore them.
    """
    out = pathlib.Path(out_dir)
    entries = entries or {}
    urls = []
    for page in pages:
        if page.get("robots") == "noindex":
            continue
        loc = page_url(site_url, page["route"])
        stamp = (entries.get(loc) or {}).get("lastmod") or d.get("built_at") or ""
        urls.append(f"  <url><loc>{loc}</loc>" + (f"<lastmod>{stamp}</lastmod>" if stamp else "") + "</url>")
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n</urlset>\n")
    (out / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {site_url}/sitemap.xml\n")
    (out / "site.webmanifest").write_text(json.dumps({
        "name": site_name(d), "short_name": site_name(d),
        "description": (d.get("site") or {}).get("tagline") or "",
        "start_url": "/", "display": "standalone",
        "background_color": "#F1EDE3", "theme_color": "#B3321F",
        "icons": [{"src": "/icon/180.png", "sizes": "180x180", "type": "image/png"},
                  {"src": "/icon/512.png", "sizes": "512x512", "type": "image/png"}],
    }, indent=1) + "\n")
    written = [str(out / "sitemap.xml"), str(out / "robots.txt"), str(out / "site.webmanifest")]
    # IndexNow checks that a submission comes from the site by fetching this file from its root.
    key = (d.get("site") or {}).get("indexnow_key")
    if key:
        (out / f"{key}.txt").write_text(key)
        written.append(str(out / f"{key}.txt"))
    return written


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", help="output folder for the production site")
    ap.add_argument("--preview", help="write a single-file preview here instead")
    ap.add_argument("--base", default="", help="URL prefix, for serving under a subpath rather than a domain root")
    ap.add_argument("--pages", help="the record of each page's fingerprint and last change, read and rewritten")
    args = ap.parse_args()
    if not args.out and not args.preview:
        ap.error("give --out or --preview")
    for path in build(args.data, args.out, args.preview, args.base, args.pages):
        print("wrote", path)
