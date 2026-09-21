"""Once a week, a list for a person: the labels most likely to be wrong, and the groups the lists miss.

Every check in this pipeline is code or a model, and both have been wrong in ways only a reader
caught: a sandbox read as a license, an auditor registry read as one, a nonprofit whose filings
warn of uncontrolled superintelligence ranked as industry because nobody had put it on the list.
This does not decide anything. It gathers the few things worth a human minute into one GitHub issue
on Mondays, which GitHub emails to the owner, and keeps a copy on the data branch.

    python -m pipeline.review --db state/index.db --dry-run
"""
import argparse
import collections
import datetime as dt
import os
import re

import requests

from .common import (Entities, config, connect, fear_keywords, fears_mentioned, iso, kv_get, kv_set,
                     measures_in_scope, name_key, now)

RARE = ("license-to-build", "training-caps", "open-model-limits", "id-age-checks", "export-controls", "preemption")
MISSION_TYPES = {"Advocacy group", "Foundation", "Pollster"}
# How a group whose purpose is AI risk tends to describe its own lobbying.
RISK_WORDS = re.compile(r"superintelligen\w*|catastrophic|existential|extinction|loss of (human )?control|"
                        r"uncontrolled|ai safety|frontier (ai|model)|dangerous (ai|capabilit\w*)|advanced ai risk",
                        re.I)
LIMIT = 40


def week_of(day):
    monday = day - dt.timedelta(days=day.weekday())
    return monday, f"{monday.isocalendar()[0]}-W{monday.isocalendar()[1]:02d}"


def cell(text, n=140):
    text = " ".join(str(text or "").split()).replace("|", "/")
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def build(db, today):
    """The report as markdown, and how many items it holds."""
    since = (today - dt.timedelta(days=7)).isoformat()
    shown = {m["id"]: m for m in measures_in_scope(db)}
    names = {c["slug"]: c["name"] for c in config("controls")}

    def where(mid):
        m = shown.get(mid) or db.execute("SELECT * FROM measures WHERE id=?", (mid,)).fetchone()
        if not m:
            return mid
        return f"[{m['jurisdiction_name']} {m['identifier'] or ''}]({m['url']})".replace("  ", " ")

    parts, items = [], 0

    held = db.execute("SELECT * FROM checks WHERE verdict = 'hold' ORDER BY checked_at DESC").fetchall()
    if held:
        items += len(held)
        parts.append(f"## Held: {len(held)} refusals the check did not apply\n\n"
                     "A run refused more labels than the check's limit allows, so nothing was taken off. "
                     "Read a few. If they are right, the check's prompt needs no change and the hold can be "
                     "released; if they are wrong, the prompt misread its instructions.\n\n"
                     "| Measure | Label | Quote | Why the check said no |\n|---|---|---|---|\n"
                     + "\n".join(f"| {where(r['target'])} | {names.get(r['value'], r['value'])} | {cell(r['evidence'])} "
                                 f"| {cell(r['reason'])} |" for r in held[:LIMIT]))

    refused = db.execute("SELECT * FROM checks WHERE verdict = 'no' AND checked_at >= ? ORDER BY kind, value",
                         (since,)).fetchall()
    if refused:
        items += len(refused)
        parts.append(f"## Taken off by the check this week: {len(refused)}\n\n"
                     "Each of these passed the quote check and failed the third reading. A wrong refusal is "
                     "put back by telling Claude which one.\n\n"
                     "| Measure | Label | Quote | Why the check said no |\n|---|---|---|---|\n"
                     + "\n".join(f"| {where(r['target'])} | "
                                 f"{('Office: ' + r['value']) if r['kind'] == 'agency' else 'About AI at all' if r['kind'] == 'about' else names.get(r['value'], r['value'])} "
                                 f"| {cell(r['evidence'])} | {cell(r['reason'])} |" for r in refused[:LIMIT])
                     + (f"\n\nAnd {len(refused) - LIMIT} more, in the checks table on the data branch."
                        if len(refused) > LIMIT else ""))

    rare = db.execute(
        "SELECT t.target, t.value, t.evidence, c.verdict, c.reason FROM tags t LEFT JOIN checks c "
        "ON c.target = t.target AND c.kind = t.kind AND c.value = t.value AND c.evidence = t.evidence "
        f"WHERE t.kind = 'control' AND t.value IN ({','.join('?' * len(RARE))}) AND t.tagged_at >= ?",
        (*RARE, since)).fetchall()
    rare = [r for r in rare if r["target"] in shown]
    if rare:
        items += len(rare)
        parts.append(f"## New labels in the rare groups: {len(rare)}\n\n"
                     "These decide which measure leads the daily post, and they were wrong most often. "
                     "Each is worth ten seconds against its quote.\n\n"
                     "| Measure | Label | Quote | Check |\n|---|---|---|---|\n"
                     + "\n".join(f"| {where(r['target'])} | {names.get(r['value'], r['value'])} | {cell(r['evidence'])} "
                                 f"| {r['verdict'] or 'not yet checked'}{': ' + cell(r['reason'], 90) if r['reason'] else ''} |"
                                 for r in rare[:LIMIT]))

    # Groups lobbying on the fears that the entity list does not know, heaviest first, with the ones
    # describing AI risk in their own filings on top: those are the ones likely to be advocacy.
    from .export import recent_quarters, reported
    ents, kw, window = Entities(), fear_keywords(), recent_quarters(4, today)
    latest = {}
    for r in db.execute("SELECT * FROM lobbying WHERE amount IS NOT NULL ORDER BY posted"):
        latest[(r["registrant"], r["client_key"], r["year"], r["quarter"])] = dict(r)
    unknown = collections.defaultdict(list)
    for r in latest.values():
        if (r["year"], int(r["quarter"][1]) if r["quarter"] else 0) not in window:
            continue
        if not r["client_key"] or ents.match_name(r["client"]) or not fears_mentioned(r["issues"], kw):
            continue
        r["self"] = name_key(r["registrant"]) == r["client_key"]
        unknown[r["client_key"]].append(r)
    groups = []
    for key, rs in unknown.items():
        risk = next((m.group(0) for r in rs for m in [RISK_WORDS.search(r["issues"] or "")] if m), "")
        groups.append((bool(risk), reported(rs), rs[0]["client"], len(rs), risk))
    groups.sort(key=lambda g: (not g[0], -g[1]))
    candidates = [g for g in groups if g[0]][:15]
    if candidates:
        items += len(candidates)
        parts.append("## Groups whose filings describe AI risk and that the lists do not know\n\n"
                     "They are ranked with everyone else until someone gives them a type in "
                     "config/entities.json. An advocacy group or foundation moves to the funders list.\n\n"
                     "| Client | Filings, past year | Reported | Their words |\n|---|---|---|---|\n"
                     + "\n".join(f"| {cell(name, 60)} | {n} | ${amount:,.0f} | {cell(risk, 60)} |"
                                 for _, amount, name, n, risk in candidates))

    # Laws whose outcome is known from outside the record, and that the record gets wrong. This is
    # the one list here about what is missing rather than what is shown wrongly.
    from . import known
    wrong = known.problems(db)
    if wrong:
        items += len(wrong)
        parts.insert(0, f"## Known AI laws the record gets wrong: {len(wrong)}\n\n"
                     "Each is in config/known_laws.json with its outcome confirmed elsewhere. One that is "
                     "not in the record means a search missed it; one marked wrongly means a legislature "
                     "words its last step in a way the status rule does not read yet.\n\n"
                     "| Law | Problem |\n|---|---|\n"
                     + "\n".join(f"| {law['jurisdiction'].upper()} {law['identifier']}: {cell(law['name'], 70)} "
                                 f"| {cell(problem, 160)} |" for law, problem in wrong))
    if not parts:
        return "", 0
    head = (f"The weekly list for a person to read. Nothing here changed the site by itself except "
            f"the labels the check took off, which are listed so a wrong one can be put back.\n\n"
            f"Built {iso()}.")
    return head + "\n\n" + "\n\n".join(parts) + "\n", items


def open_issue(title, body):
    token, repo = os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return None
    r = requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, json={"title": title, "body": body[:60000]})
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub would not open the issue: {r.status_code} {r.text[:200]}")
    return r.json().get("html_url")


def run(db, state, mode):
    today = now().date()
    monday, week = week_of(today)
    if kv_get(db, "review:week") == week:
        state["message"] = f"this week's list ({week}) is already out"
        return
    body, items = build(db, today)
    kv_set(db, "review:latest", {"week": week, "items": items, "body": body})
    kv_set(db, "review:week", week)
    db.commit()
    if not items:
        state["message"] = f"nothing to review for {week}"
        return
    url = open_issue(f"Label review: week of {monday:%B} {monday.day}", body)
    state["added"] = items
    state["message"] = f"{items} items for review" + (f": {url}" if url else "; no token, kept on the data branch")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/index.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = connect(args.db)
    body, items = build(db, now().date())
    print(body or "nothing to review")
    print(f"\n{items} items")


if __name__ == "__main__":
    main()
