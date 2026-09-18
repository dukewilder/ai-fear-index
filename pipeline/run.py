"""Run the pipeline: collect, tag, export.

    python -m pipeline.run --mode auto --db state/index.db --out state

auto = hourly sources every run, plus the daily sources on the 06:00 UTC run. Any source
that has never finished a run is added to the next run and reaches back to January 2025,
so the index fills itself in without anyone starting a run by hand.
"""
import argparse
import importlib
import time

from .common import connect, log, now, source_run

HOURLY = ["gdelt", "rss", "fedreg"]
DAILY = ["congress", "openstates", "lda", "fec", "wikipedia"]
MODULES = {"gdelt": "collect_gdelt", "rss": "collect_rss", "fedreg": "collect_fedreg", "congress": "collect_congress",
           "openstates": "collect_openstates", "lda": "collect_lda", "fec": "collect_fec",
           "wikipedia": "collect_wikipedia", "tag": "tag"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "hourly", "daily", "backfill"])
    ap.add_argument("--db", default="state/index.db")
    ap.add_argument("--out", default="state")
    ap.add_argument("--only", default="", help="comma-separated sources to run (for testing)")
    ap.add_argument("--base", default="/ai-fear-index")
    args = ap.parse_args()
    db = connect(args.db)
    done = {r["source"] for r in db.execute("SELECT source FROM status WHERE last_ok IS NOT NULL")}
    mode = args.mode
    if mode == "auto":
        mode = "daily" if now().hour == 6 else "hourly"
    sources = HOURLY + (DAILY if mode in ("daily", "backfill") else [])
    if mode == "hourly":
        sources += [s for s in DAILY if s not in done]  # first run of a source catches it up
    if args.only:
        sources = [s for s in args.only.split(",") if s in MODULES and s != "tag"]
    log(f"mode={mode} sources={sources} first run for: {[s for s in sources if s not in done] or 'none'}")
    started = time.time()
    for name in sources:
        module = importlib.import_module(f"pipeline.{MODULES[name]}")
        with source_run(db, name) as state:
            # a source that has never finished reaches all the way back, whatever the run mode
            module.run(db, state, "backfill" if name not in done else mode)
    if not args.only or "tag" in args.only.split(","):
        from . import tag
        with source_run(db, "tag") as state:
            tag.run(db, state, mode)
    from . import export
    data = export.export(db, args.out, args.base)
    log(f"exported: {data['index']['value']} controlled measures, {len(data['fears'])} fears, "
        f"{len(data['funders'])} funders, {len(data['feed'])} feed items in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
