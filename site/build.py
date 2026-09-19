#!/usr/bin/env python3
"""Render the AI Fear Report site from one data file.

Production (one folder per page, ready for GitHub Pages):
    python site/build.py --data data/site_data.json --out dist --base /ai-fear-index

Preview (every page in one HTML file, hash navigation):
    python site/build.py --data state/site_data.json --preview preview.html

The pipeline writes data/site_data.json in the same shape as placeholder.json,
so the pages never change when real data replaces the placeholders.
"""
import argparse
import datetime as dt
import html
import json
import pathlib

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = pathlib.Path(__file__).resolve().parent
CHEVRON = ('<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">'
           '<path d="M10 3 5 8l5 5" fill="none" stroke="currentColor" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"/></svg>')
NAV_FOR = {"home": "", "fear": "", "org": "rankings", "feed": "feed",
           "rankings": "rankings", "method": "method"}


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
        tip = f"{quarters[quarter]}: {raw_b.get(str(quarter), 0)} bills"
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


def share_card(path, big, label, sub="", kicker="AI FEAR REPORT", foot="aifearreport"):
    """A 1200 by 630 card in the site's own style: paper, ink, one stamped figure."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (CARD_W, CARD_H), "#F1EDE3")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, CARD_W - 1, CARD_H - 1], outline="#141210", width=3)
    d.line([(0, 104), (CARD_W, 104)], fill="#141210", width=2)
    f_kick = ImageFont.load_default(size=28)
    f_big = ImageFont.load_default(size=200 if len(big) <= 5 else 150)
    f_lab = ImageFont.load_default(size=44)
    f_sub = ImageFont.load_default(size=28)
    f_foot = ImageFont.load_default(size=24)
    kb = d.textbbox((0, 0), kicker, font=f_kick)
    d.rectangle([60, 38, 60 + (kb[2] - kb[0]) + 30, 38 + (kb[3] - kb[1]) + 24], fill="#B3321F")
    d.text((75, 44), kicker, font=f_kick, fill="#F1EDE3")
    bb = d.textbbox((0, 0), big, font=f_big)
    top = 150
    d.text((52, top - bb[1]), big, font=f_big, fill="#141210")
    y = top + (bb[3] - bb[1]) + 26
    for line in wrap_text(d, label, f_lab, CARD_W - 120)[:2]:
        d.text((60, y), line, font=f_lab, fill="#141210")
        y += 54
    if sub:
        y += 10
        for line in wrap_text(d, sub, f_sub, CARD_W - 120)[:2]:
            d.text((60, y), line, font=f_sub, fill="#5D584D")
            y += 38
    d.line([(60, CARD_H - 70), (CARD_W - 60, CARD_H - 70)], fill="#141210", width=2)
    d.text((60, CARD_H - 56), foot, font=f_foot, fill="#5D584D")
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)


def card_specs(d):
    """Which cards to draw: one for the front page, one per fear, one for the rankings."""
    ix = d["index"]
    yield "home", ix["value"], ix["text"] + (f", in {ix['where']}" if ix.get("where") else ""), \
        f"{ix.get('second', '')} {ix.get('second_text', '')}".strip()
    for f in d["fear_pages"]:
        yield f"fear-{f['slug']}", f["score"], f["name"], \
            f"{f['score']} of 100" + (f" · {f['line']}" if f.get("line") else "")
    top = d["beneficiaries"][:3]
    yield "rankings", d.get("beneficiaries_total", str(len(d["beneficiaries"]))), \
        "agencies and officials the bills would hand new power over AI", \
        " · ".join(f"{b['name']} {b['score']}" for b in top)


def make_url(preview, base):
    routes = {"home": "", "feed": "feed", "rankings": "rankings", "method": "method"}

    def url(kind, slug=None, anchor=None):
        if kind in ("fear", "org"):
            path = f"{kind}/{slug}"
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
                      "template": "fear.html", "title": f"{f['name']}, {name}",
                      "ctx": {"f": f, "timeline": timeline_svg(f["timeline"])}})
    for o in d["org_pages"]:
        specs.append({"kind": "org", "route": f"org/{o['slug']}", "out": f"org/{o['slug']}/index.html",
                      "template": "org.html", "title": f"{o['name']}, {name}", "ctx": {"o": o}})
    for kind, title in (("feed", "Feed"), ("rankings", "Rankings"), ("method", "How the numbers work")):
        specs.append({"kind": kind, "route": kind, "out": f"{kind}/index.html", "template": f"{kind}.html",
                      "title": f"{title}, {name}", "ctx": {}})
    for s in specs:
        s["nav"] = NAV_FOR[s["kind"]]
        s["card"] = {"home": "home", "rankings": "rankings"}.get(s["kind"]) or \
            (f"fear-{s['ctx']['f']['slug']}" if s["kind"] == "fear" else None)
        s["description"] = None
        if s["kind"] == "fear":
            f = s["ctx"]["f"]
            s["description"] = f"{f['name']} scores {f['score']} on the Fear Index. {f.get('line') or ''}".strip()
        elif s["kind"] == "home":
            s["description"] = (f"{d['index']['value']} {d['index']['text']}. {d['index']['second']} "
                                f"{d['index']['second_text']}.")
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
    env.globals.update(url=make_url(preview, base.rstrip("/")), d=d, chevron=CHEVRON,
                       spark_svg=sparkline_svg(series, d["index"].get("series_label") or "Last 90 days"))
    pages = []
    for spec in page_specs(d):
        spec["html"] = env.get_template("pages/" + spec["template"]).render(**spec["ctx"])
        pages.append(spec)
    shell = env.get_template("base.html")
    if preview:
        pathlib.Path(preview_path).write_text(shell.render(pages=pages, preview=True,
                                                           title=name, nav=""))
        return [preview_path]
    written = []
    site_url = (d.get("site_url") or "").rstrip("/")
    cards = {}
    try:
        for card, big, label, sub in card_specs(d):
            share_card(pathlib.Path(out_dir) / "og" / f"{card}.png", big, label, sub,
                       kicker=name.upper(), foot=site_url.replace("https://", "") or name)
            cards[card] = f"{site_url}/og/{card}.png" if site_url else None
    except Exception as exc:  # cards are a nicety; the pages must still build
        print("share cards skipped:", exc)
    for page in pages:
        target = pathlib.Path(out_dir) / page["out"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(shell.render(pages=[page], preview=False, title=page["title"], nav=page["nav"],
                                       og_image=cards.get(page.get("card")), description=page.get("description")))
        written.append(str(target))
    return written


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", help="output folder for the production site")
    ap.add_argument("--preview", help="write a single-file preview here instead")
    ap.add_argument("--base", default="", help="URL prefix, e.g. /ai-fear-index")
    args = ap.parse_args()
    if not args.out and not args.preview:
        ap.error("give --out or --preview")
    for path in build(args.data, args.out, args.preview, args.base):
        print("wrote", path)
