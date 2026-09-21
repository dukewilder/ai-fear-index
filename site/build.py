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

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = pathlib.Path(__file__).resolve().parent
CHEVRON = ('<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">'
           '<path d="M10 3 5 8l5 5" fill="none" stroke="currentColor" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"/></svg>')
NAV_FOR = {"home": "", "fear": "", "org": "", "feed": "feed", "method": "method", "states": "states",
           "state": "states"}


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


def share_card(path, big, label, sub="", kicker="", foot="aifearreport.com"):
    """A 1200 by 630 card in the site's own style: paper, ink, one stamped figure.

    The mark itself goes on it, drawn by the same function that draws the avatar and the banner.
    It used to set the words "AI FEAR REPORT" in a red box in whatever face PIL had lying around,
    which is a card carrying a picture of the name rather than the mark, in the wrong typeface.
    """
    from PIL import Image, ImageDraw
    from brand import font as bfont, ink as bink, wordmark, PAPER, INK, VERMILLION
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
    """Which cards to draw: one per fear and one per state. The front page has the standing one."""
    for f in d["fear_pages"]:
        yield f"fear-{f['slug']}", f["score"], f["name"], \
            f"{f['score']} of 100" + (f" · {f['line']}" if f.get("line") else "")
    for p in d.get("state_pages") or []:
        if p["n"]:
            yield (f"state-{p['slug']}", f"{p['c']:,}", p["name"],
                   f"AI measure{'' if p['c'] == 1 else 's'} that would put AI under new government control, "
                   f"of {p['n']:,} since January 2025")
        else:
            yield f"state-{p['slug']}", "0", p["name"], "No AI measures found here yet"


# The map card is drawn in the light theme's heat colours, the same five the page uses.
HEAT = ["#EDE9DE", "#EFDCD0", "#DFB09C", "#C76F55", "#B3321F"]


def map_card(path, m, foot="aifearreport.com"):
    """The states page's card: the tile map itself, which is the thing people share it for."""
    from PIL import Image, ImageDraw
    from brand import font as bfont, ink as bink, wordmark, PAPER, INK, VERMILLION
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
    routes = {"home": "", "feed": "feed", "method": "method", "states": "states"}

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


def page_specs(d):
    name = site_name(d)
    specs = [{"kind": "home", "route": "", "out": "index.html", "template": "home.html",
              "title": name, "ctx": {}}]
    for f in d["fear_pages"]:
        specs.append({"kind": "fear", "route": f"fear/{f['slug']}", "out": f"fear/{f['slug']}/index.html",
                      "template": "fear.html",
                      "title": f"{f['name']}: the AI bills and the money, {name}",
                      "ctx": {"f": f, "timeline": timeline_svg(f["timeline"])}})
    for o in d["org_pages"]:
        specs.append({"kind": "org", "route": f"org/{o['slug']}", "out": f"org/{o['slug']}/index.html",
                      "template": "org.html",
                      "title": f"{o['name']}: {'election money' if o.get('group') == 'election' else 'AI lobbying'}, {name}",
                      "ctx": {"o": o}})
    for kind, title in (("feed", "Feed: AI bills, rules and filings as they arrive"),
                        ("method", "How the numbers work")):
        specs.append({"kind": kind, "route": kind, "out": f"{kind}/index.html", "template": f"{kind}.html",
                      "title": f"{title}, {name}", "ctx": {}})
    if d.get("state_pages"):
        specs.append({"kind": "states", "route": "states", "out": "states/index.html", "template": "states.html",
                      "title": f"The states: where AI measures would add government control, {name}", "ctx": {}})
        for p in d["state_pages"]:
            specs.append({"kind": "state", "route": f"states/{p['slug']}", "out": f"states/{p['slug']}/index.html",
                          "template": "state.html",
                          "title": f"{p['name']}: AI measures and who they would hand power, {name}",
                          "ctx": {"p": p}})
    for s in specs:
        s["nav"] = NAV_FOR[s["kind"]]
        s["card"] = (f"fear-{s['ctx']['f']['slug']}" if s["kind"] == "fear" else
                     f"state-{s['ctx']['p']['slug']}" if s["kind"] == "state" else
                     "states" if s["kind"] == "states" else None)
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
        elif s["kind"] == "states":
            s["description"] = ("Every state, DC and Puerto Rico, shaded by the AI measures that would put AI, "
                                "the people building it or the people using it under new government control, "
                                "with a page for each.")
        elif s["kind"] == "state":
            p = s["ctx"]["p"]
            s["description"] = (p["receipt"].rsplit(" http", 1)[0] if p["n"] else
                                f"{p['name']}: no AI measures found here since January 2025.")
    return specs


def build(data_path, out_dir=None, preview_path=None, base=""):
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
                                                           title=name, nav="", home=True))
        return [preview_path]
    written = []
    site_url = (d.get("site_url") or "").rstrip("/")
    cards = {}
    icons = {}
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

        share_image(pathlib.Path(out_dir) / "og" / "share.png",
                    (d.get("site") or {}).get("tagline") or "Every fear about AI, and what it buys.")
        cards["share"] = stamped("share.png")
        for card, big, label, sub in card_specs(d):
            share_card(pathlib.Path(out_dir) / "og" / f"{card}.png", big, label, sub,
                       kicker=name.upper(), foot=site_url.replace("https://", "") or name)
            cards[card] = stamped(f"{card}.png")
        if d.get("state_map"):
            map_card(pathlib.Path(out_dir) / "og" / "states.png", d["state_map"],
                     foot=site_url.replace("https://", "") or name)
            cards["states"] = stamped("states.png")
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
    for page in pages:
        target = pathlib.Path(out_dir) / page["out"]
        target.parent.mkdir(parents=True, exist_ok=True)
        route = page["route"]
        target.write_text(shell.render(
            pages=[page], preview=False, title=page["title"], nav=page["nav"],
            home=page["kind"] == "home", og_image=cards.get(page.get("card")) or cards.get("share"),
            icons=icons,
            canonical=f"{site_url}/{route}{'/' if route else ''}" if site_url else None,
            description=page.get("description")))
        written.append(str(target))
    # What GitHub Pages serves for a path that does not exist. It is built last and kept out of
    # the sitemap, so nothing points a crawler at it.
    gone = {"kind": "method", "route": "404", "out": "404.html", "template": "notfound.html",
            "title": f"Page not found, {name}", "ctx": {}, "nav": "", "card": None,
            "description": "That page is not on the AI Fear Report."}
    gone["html"] = env.get_template("pages/notfound.html").render()
    target = pathlib.Path(out_dir) / gone["out"]
    target.write_text(shell.render(pages=[gone], preview=False, title=gone["title"], nav="",
                                   home=False, og_image=cards.get("share"), icons=icons,
                                   canonical=None, description=gone["description"]))
    written.append(str(target))
    if site_url:
        written += write_index_files(out_dir, site_url, pages, d)
    return written


def write_index_files(out_dir, site_url, pages, d):
    """robots.txt and a sitemap, so a crawler is told what exists rather than guessing.

    Every page is worth indexing and none of them change on a schedule a crawler could predict, so
    the sitemap carries one lastmod for the lot: the moment the data was last exported.
    """
    out = pathlib.Path(out_dir)
    stamp = (d.get("built_at") or "")[:10]
    urls = []
    for page in pages:
        route = page["route"]
        loc = f"{site_url}/{route}{'/' if route else ''}"
        urls.append(f"  <url><loc>{loc}</loc>"
                    f"{f'<lastmod>{stamp}</lastmod>' if stamp else ''}"
                    f"<changefreq>{'daily' if page['kind'] in ('home', 'feed', 'states') else 'weekly'}</changefreq>"
                    f"<priority>{'1.0' if page['kind'] == 'home' else '0.7'}</priority></url>")
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
    return [str(out / "sitemap.xml"), str(out / "robots.txt"), str(out / "site.webmanifest")]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", help="output folder for the production site")
    ap.add_argument("--preview", help="write a single-file preview here instead")
    ap.add_argument("--base", default="", help="URL prefix, for serving under a subpath rather than a domain root")
    args = ap.parse_args()
    if not args.out and not args.preview:
        ap.error("give --out or --preview")
    for path in build(args.data, args.out, args.preview, args.base):
        print("wrote", path)
