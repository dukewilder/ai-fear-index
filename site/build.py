#!/usr/bin/env python3
"""Render the AI Fear Index site from one data file.

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
NAV_FOR = {"home": "", "fear": "rankings", "org": "rankings", "feed": "feed",
           "rankings": "rankings", "method": "method"}


def esc(value):
    return html.escape(str(value), quote=True)


def qx(quarter):
    """x position of a quarter (0 to 11) on the 350-wide timeline."""
    return round(24 + quarter * 27.45, 1)


def sparkline_svg(values, label):
    w, h = 350, 70
    n = len(values)
    points = []
    for i, v in enumerate(values):
        x = 4 + i * (w - 10) / (n - 1)
        y = h - 8 - (max(0, min(100, v)) / 100) * (h - 16)
        points.append((round(x, 1), round(y, 1)))
    poly = " ".join(f"{x},{y}" for x, y in points)
    ex, ey = points[-1]
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
            f'<line class="sp-base" x1="0" y1="{h - 3}" x2="{w}" y2="{h - 3}"/>'
            f'<polyline class="sp-line" points="{poly}"/>'
            f'<circle class="sp-dot" cx="{ex}" cy="{ey}" r="4.5"/></svg>')


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
    for quarter, height in t.get("funding") or []:
        p.append(f'<rect class="tl-money" x="{qx(quarter) - 8}" y="{86 - height}" width="16" height="{height}"/>')
    for quarter, count in enumerate(t.get("messages") or []):
        for k in range(count):
            p.append(f'<circle class="tl-msg" cx="{qx(quarter)}" cy="{155 - 11 * k}" r="5"/>')
    pts = [(qx(q), round(240 - v * 0.5, 1)) for q, v in t.get("concern") or []]
    if len(pts) > 1:
        p.append('<polyline class="tl-line" points="' + " ".join(f"{x},{y}" for x, y in pts) + '"/>')
    for x, y in pts:
        p.append(f'<circle class="tl-pt" cx="{x}" cy="{y}" r="3.5"/>')
    if pts:
        (fx, fy), (lx, ly) = pts[0], pts[-1]
        if t.get("first_label"):
            p.append(f'<text class="tl-val" x="{fx}" y="{fy - 10}" text-anchor="middle">{esc(t["first_label"])}</text>')
        if t.get("last_label") and len(pts) > 1:
            p.append(f'<text class="tl-val" x="{lx}" y="{ly - 10}" text-anchor="middle">{esc(t["last_label"])}</text>')
    elif not t.get("concern"):
        label(175, 222, "No attention series for this fear yet", "middle")
    return f'<svg viewBox="0 0 350 280" role="img" aria-label="{esc(t["aria"])}">' + "".join(p) + "</svg>"


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


def page_specs(d):
    specs = [{"kind": "home", "route": "", "out": "index.html", "template": "home.html",
              "title": "AI Fear Index", "ctx": {}}]
    for f in d["fear_pages"]:
        specs.append({"kind": "fear", "route": f"fear/{f['slug']}", "out": f"fear/{f['slug']}/index.html",
                      "template": "fear.html", "title": f"{f['name']}, AI Fear Index",
                      "ctx": {"f": f, "timeline": timeline_svg(f["timeline"])}})
    for o in d["org_pages"]:
        specs.append({"kind": "org", "route": f"org/{o['slug']}", "out": f"org/{o['slug']}/index.html",
                      "template": "org.html", "title": f"{o['name']}, AI Fear Index", "ctx": {"o": o}})
    for kind, title in (("feed", "Feed"), ("rankings", "Rankings"), ("method", "How the numbers work")):
        specs.append({"kind": kind, "route": kind, "out": f"{kind}/index.html", "template": f"{kind}.html",
                      "title": f"{title}, AI Fear Index", "ctx": {}})
    for s in specs:
        s["nav"] = NAV_FOR[s["kind"]]
    return specs


def build(data_path, out_dir=None, preview_path=None, base=""):
    d = json.loads(pathlib.Path(data_path).read_text())
    if d.get("built_at") in (None, "", "now"):
        d["built_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    preview = preview_path is not None
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.globals.update(url=make_url(preview, base.rstrip("/")), d=d, chevron=CHEVRON,
                       spark_svg=sparkline_svg(d["index"]["trend"], "Index over the last 90 days"))
    pages = []
    for spec in page_specs(d):
        spec["html"] = env.get_template("pages/" + spec["template"]).render(**spec["ctx"])
        pages.append(spec)
    shell = env.get_template("base.html")
    if preview:
        pathlib.Path(preview_path).write_text(shell.render(pages=pages, preview=True,
                                                           title="AI Fear Index", nav=""))
        return [preview_path]
    written = []
    for page in pages:
        target = pathlib.Path(out_dir) / page["out"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(shell.render(pages=[page], preview=False, title=page["title"], nav=page["nav"]))
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
