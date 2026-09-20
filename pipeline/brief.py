"""Write one edition of the daily brief: the post text and the card that goes with it.

The site can show the whole chain and let a reader take it at their own pace. A post gets one
scroll, so an edition has to make the case in a few lines. It does that by choosing measures that
already carry a control, stating each one in the terms the measure itself uses rather than the
terms its sponsor prefers, and ending each line at the office that would hold the power.

Nothing is said here that the site cannot show. Every sentence is written from the measure's own
title and summary, and then checked back against them the way a tag is checked: the model has to
quote the words it relied on, and the quote has to be in the text. A sentence that fails the check
is dropped rather than softened, and the edition runs a line shorter.

    python -m pipeline.brief --db state/index.db --out state/brief --dry-run
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys

from .common import Entities, config, connect, env, log, now
from .export import gave
from .tag import agreed, call, norm

MODEL_NOTE = "claude-haiku-4-5-20251001"
WANT = 3          # lines in an edition: enough to show a pattern, few enough to read
POOL_DAYS = 45    # measures move slower than a day, so an edition draws from a rolling pool
MAX_SENTENCE = 120


def controls_by_slug():
    return {c["slug"]: c for c in config("controls")}


def fears_by_slug():
    return {f["slug"]: f for f in config("fears")}


ENFORCER = re.compile(r"\b(attorney general|commission|department|board|bureau|authority|agency|"
                      r"secretary|director|administrator|office of)\b", re.I)

# What a control is worth to an edition. A duty to file a report is real but it is not the story;
# a licence, an identity check, or a new office with a power over you is.
WEIGHT = {"license-to-build": 5, "training-caps": 5, "id-age-checks": 5, "open-model-limits": 4,
          "new-agency-powers": 4, "preemption": 4, "export-controls": 3, "data-center-limits": 3,
          "mandatory-reporting": 1, "labeling-mandates": 1}


OTHER_TONGUE = re.compile(r"\b(para|que|los|las|del|por|una|con|sobre|ley|articulo|art[ií]culo|"
                          r"gubernamental|inteligencia)\b", re.I)


def english(row):
    """The collectors reach every jurisdiction, and Puerto Rico legislates in Spanish.

    A Spanish summary is almost all ASCII, so the give-away is the vocabulary rather than the
    characters. Three or more of these words in the first stretch of text and the row waits for a
    version of this that can read it.
    """
    text = f"{row['title'] or ''} {row['summary'] or ''}"[:600]
    return bool(text) and len(OTHER_TONGUE.findall(text)) < 3


def score(row, today):
    """How well one measure would carry an edition."""
    controls = (row["controls"] or "").split("|")
    points = max((WEIGHT.get(c, 1) for c in controls), default=0)
    if (row["status"] or "") == "passed":
        points += 3
    if row["office"] and ENFORCER.search(row["office"]):
        points += 2
    if row["fears"]:
        points += 2
    moved = row["moved"] or ""
    if moved >= (dt.date.fromisoformat(today) - dt.timedelta(days=14)).isoformat():
        points += 1
    return points


def pool(db, today, days=POOL_DAYS, limit=12):
    """Measures that carry a control, have not been in an edition, and moved recently.

    A resolution is left out on purpose: it states an opinion rather than binding anyone, and every
    line of an edition claims somebody is bound. A measure that names an office is worth more than
    one that does not, which the score handles, but it is not required: measures carrying both a
    control and an office arrive at under three a week, and a daily post cannot wait on that.
    """
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT m.id, m.kind, m.jurisdiction_name, m.identifier, m.title, m.summary, m.status, m.url, "
        "  COALESCE(m.latest_action_date, m.updated, m.first_seen) AS moved, "
        "  (SELECT GROUP_CONCAT(value, '|') FROM tags t WHERE t.target=m.id AND t.kind='control') AS controls, "
        "  (SELECT GROUP_CONCAT(value, '|') FROM tags t WHERE t.target=m.id AND t.kind='fear') AS fears, "
        "  (SELECT value FROM tags t WHERE t.target=m.id AND t.kind='agency' ORDER BY LENGTH(value) LIMIT 1) AS office "
        "FROM measures m JOIN tag_runs r ON r.target = m.id "
        "WHERE r.ai_related = 1 AND controls IS NOT NULL AND m.kind != 'resolution' "
        "  AND m.id NOT IN (SELECT target FROM brief) "
        "  AND moved >= ? AND moved <= ? "
        "ORDER BY moved DESC", (since, today)).fetchall()
    rows = [r for r in rows if english(r)]
    rows.sort(key=lambda r: (-score(r, today), r["moved"] or ""), reverse=False)
    rows.sort(key=lambda r: -score(r, today))
    return rows[:limit]


def doc(row):
    return f"Jurisdiction: {row['jurisdiction_name']}\nIdentifier: {row['identifier']}\n" \
           f"Title: {row['title'] or ''}\nSummary: {(row['summary'] or '')[:6000]}"


SYSTEM = (
    "You write one sentence about a U.S. bill or rule for a public record of what AI fear is buying. "
    "You are read by people who are about to be bound by these measures, so you say what a measure "
    "does to them in the words the measure itself uses. Reply with a single JSON object and nothing else."
)

RULES = (
    "Write one sentence, at most 20 words. {tense}\n"
    "Say who is bound and what they must do. If a duty falls on everyone in order to identify some "
    "people, the sentence says everyone: a rule that checks whether a user is a child checks every "
    "user, so write that it checks every user.\n"
    "Never use a word the sponsor chose to make the measure sound smaller than it is. Do not write "
    "safeguards, guardrails, protections, safety net, common sense, modernize, framework, or oversight "
    "when the measure creates a duty, a licence, a register, or a power to inspect. Name the duty.\n"
    "Do not say whether the measure is good or bad, and do not add a reason it was introduced.\n"
    "Do not name the bill number, and do not write the words bill, act, or legislation. Start with the "
    "jurisdiction.\n"
    "quote is copied word for word from the Title or Summary and is the text the sentence rests on, "
    "between 6 and 30 words."
)


LAW = ("This measure has passed, so write in the present tense: it requires, it makes, it bans.")
PENDING = ("This measure has NOT passed. It was introduced and is still somewhere in the process, so "
           "every verb is conditional: it WOULD require, it WOULD make. Never write that anyone must "
           "do anything, or that anything is the case, because none of it is yet.")


def sentence(key, row):
    rules = RULES.format(tense=LAW if (row["status"] or "") == "passed" else PENDING)
    out = call(key, SYSTEM, f"TEXT\n{doc(row)}\n\n{rules}\n\n"
                            'Return JSON: {"sentence": "...", "quote": "..."}', max_tokens=400)
    return (out.get("sentence") or "").strip(), (out.get("quote") or "").strip()


BANNED = re.compile(
    r"\b(safeguard\w*|guardrail\w*|protections?|safety net|common ?sense|modern\w*|framework\w*|"
    r"oversight|responsib\w*|thoughtful\w*|balanced?|sensible|reasonable steps|bad actors?)\b", re.I)


CONDITIONAL = re.compile(r"\b(would|could|may)\b", re.I)


def usable(text, quote, body_norm, passed=False):
    """The sentence has to rest on words in the text, not borrow the sponsor's vocabulary, and not
    describe a bill sitting in committee as though it already bound anyone.

    The tense is the whole difference between a record and a rumour, so it is checked here rather
    than trusted to the instruction above: a measure that has not passed says would, and one that
    has does not.
    """
    if not text or not quote:
        return False
    if passed and CONDITIONAL.search(text):
        return False
    if not passed and not CONDITIONAL.search(text):
        return False
    if len(text) > MAX_SENTENCE or not text.endswith("."):
        return False
    if BANNED.search(text):
        return False
    if re.search(r"\b(bill|act|legislation|lawmakers?)\b", text, re.I):
        return False
    return bool(agreed({"controls": {"line": quote}}, body_norm, "controls"))


def write_lines(key, rows, want=WANT):
    """Turn candidate measures into checked sentences, stopping once the edition is full."""
    lines, places = [], set()
    for row in rows:
        if len(lines) >= want:
            break
        # one state should not be the whole edition; the case is that this is everywhere
        if row["jurisdiction_name"] in places:
            continue
        body = norm(f"{row['title'] or ''}\n{row['summary'] or ''}")
        try:
            text, quote = sentence(key, row)
        except Exception as exc:  # one bad row must not cost the edition
            log(f"[brief] {row['id']}: {exc}")
            continue
        if not usable(text, quote, body, (row["status"] or "") == "passed"):
            log(f"[brief] {row['id']}: sentence did not hold up, skipped")
            continue
        places.add(row["jurisdiction_name"])
        lines.append({"target": row["id"], "sentence": text, "quote": quote,
                      "office": row["office"] or "", "controls": row["controls"] or "",
                      "jurisdiction": row["jurisdiction_name"], "url": row["url"],
                      "status": row["status"] or ""})
    return lines


ARROW = "\u2192"

NUMBERS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def words(n, cap=False):
    """Ten and under reads better spelled out, and a sentence never opens with a numeral."""
    word = NUMBERS[n] if n < len(NUMBERS) else f"{n:,}"
    return word[0].upper() + word[1:] if cap and word[0].islower() else word


def count(n, one, many=None, cap=False):
    return f"{words(n, cap)} {one if n == 1 else (many or one + 's')}"


def places(db, kind, value):
    """Say states when they are all states. Congress and a federal agency are not."""
    rows = db.execute(
        "SELECT DISTINCT m.jurisdiction, m.jurisdiction_name FROM measures m "
        "JOIN tags t ON t.target = m.id JOIN tag_runs tr ON tr.target = m.id "
        "WHERE tr.ai_related = 1 AND t.kind = ? AND t.value = ?", (kind, value)).fetchall()
    federal = any((r["jurisdiction"] or "") in ("us", "fed") or
                  (r["jurisdiction_name"] or "").endswith(("Department", "Administration", "Commission"))
                  for r in rows)
    return len(rows), ("jurisdiction" if federal else "state")


def patterns(db, today, recent):
    """Lines that state what the whole record adds up to, computed fresh every time.

    Measures that carry a control arrive at about three a week, which is not a daily brief. These
    are: each one is true of the database as it stands, so an edition can always be filled without
    reaching for a measure that does not carry its weight. They repeat only when the number moves,
    which is what makes them worth reading twice.
    """
    names = controls_by_slug()
    fears = fears_by_slug()
    out = []

    for r in db.execute(
            "SELECT t.value AS slug, COUNT(DISTINCT m.id) AS n, COUNT(DISTINCT m.jurisdiction_name) AS j "
            "FROM tags t JOIN measures m ON m.id = t.target JOIN tag_runs tr ON tr.target = m.id "
            "WHERE t.kind='control' AND tr.ai_related=1 GROUP BY t.value HAVING n >= 5"):
        c = names.get(r["slug"])
        if not c:
            continue
        n, word = places(db, "control", r["slug"])
        out.append({"key": f"control:{r['slug']}:{r['n']}", "weight": WEIGHT.get(r["slug"], 1),
                    "sentence": f"{count(r['n'], 'measure', cap=True)} in {count(n, word)} "
                                f"would {c['pattern']}.",
                    "meta": c["chip"], "office": "",
                    "head": f"{r['n']} measures in {count(n, word)}: {c['head']}"})

    for r in db.execute(
            "SELECT t.value AS slug, COUNT(DISTINCT m.id) AS n, COUNT(DISTINCT m.jurisdiction_name) AS j "
            "FROM tags t JOIN measures m ON m.id = t.target JOIN tag_runs tr ON tr.target = m.id "
            "WHERE t.kind='fear' AND tr.ai_related=1 GROUP BY t.value HAVING n >= 5"):
        f = fears.get(r["slug"])
        if not f:
            continue
        n, word = places(db, "fear", r["slug"])
        out.append({"key": f"fear:{r['slug']}:{r['n']}", "weight": 3,
                    "sentence": f"{count(r['n'], 'measure', cap=True)} in {count(n, word)} "
                                f"name the same fear: {f['name']}.",
                    "meta": f["name"], "office": "",
                    "head": f"{r['n']} measures name the same fear: {f['name']}"})

    row = db.execute(
        "SELECT COUNT(DISTINCT value) AS n FROM tags WHERE kind='agency'").fetchone()
    top = db.execute(
        "SELECT value, COUNT(*) n FROM tags WHERE kind='agency' GROUP BY value ORDER BY n DESC LIMIT 1"
    ).fetchone()
    if row["n"] and top:
        out.append({"key": f"offices:{row['n']}", "weight": 4,
                    "sentence": f"{count(row['n'], 'named office', 'named offices', cap=True)} would gain a "
                                f"power over AI. The one named most often is the {top['value']}, in "
                                f"{count(top['n'], 'measure')}.",
                    "meta": "", "office": "",  # the sentence already names it
                    "head": f"{row['n']} offices would gain a power over AI"})

    # the same line-number rule the site uses, so a total here matches the total there
    ents = Entities()
    given, donors, committees = 0.0, set(), set()
    for r in db.execute("SELECT committee_name, counterparty, counterparty_key, amount, line_number FROM fec"):
        if not gave(r["line_number"], ents.match_name(r["counterparty"] or "")):
            continue
        given += r["amount"] or 0
        donors.add(r["counterparty_key"])
        committees.add(r["committee_name"])
    if len(committees) >= 2:
        out.append({"key": f"money:{int(given)}", "weight": 4,
                    "sentence": f"${given / 1e6:,.1f} million from {count(len(donors), 'donor')} sits in "
                                f"{count(len(committees), 'committee')} arguing about how afraid you "
                                f"should be of AI.",
                    "meta": ", ".join(sorted(c.title() for c in committees)), "office": "",
                    "head": f"${given / 1e6:,.1f} million on how afraid you should be"})

    return [p for p in out if p["key"] not in recent]


HEAD_SYSTEM = ("You shorten one sentence about a U.S. bill into a headline for a public record. "
               "Reply with a single JSON object and nothing else.")

HEAD_RULES = (
    "Shorten the sentence to at most nine words.\n"
    "Keep the place it names and the thing it does. Drop everything else.\n"
    "It is a plain statement with a verb. Someone who reads only this line and nothing else "
    "should come away knowing one fact.\n"
    "Do not be clever, do not ask a question, and do not open with who, what, how, why, or when.\n"
    "Do not add a number, a place, or anything else the sentence does not already say."
)

NUMERAL = re.compile(r"\d[\d,.]*")


def plain(line, names):
    """A headline that is always well formed: the place, then what the measure does to people.

    Cutting a sentence off at nine words lands mid-phrase and says something the measure does
    not, so the fallback builds a new line out of two fields instead of trimming one.
    """
    best = sorted((c for c in (line.get("controls") or "").split("|") if c in names),
                  key=lambda c: -WEIGHT.get(c, 1))
    where = line.get("jurisdiction") or ""
    if best and where:
        return f"{where}: {names[best[0]]['head']}"
    return names[best[0]]["head"].capitalize() if best else where


def headline(key, lines, names, totals):
    """The one line on the plate, and the only text on it that is not a fixed part of the mark.

    A pattern line brings its own, written from the same numbers that made it. A measure line is
    shortened by the model, and then checked: no digit may appear that the sentence did not
    already contain, which is the way a headline would go wrong if it went wrong. Anything that
    fails falls back to the sentence's own opening, which is already checked against the source.
    """
    lead = lines[0]
    if lead.get("head"):
        return lead["head"]
    text = lead["sentence"]
    try:
        out = call(key, HEAD_SYSTEM, f"SENTENCE\n{text}\n\n{HEAD_RULES}\n\n"
                                    'Return JSON: {"line": "..."}', max_tokens=150)
        line = (out.get("line") or "").strip().rstrip(".")
    except Exception as exc:
        log(f"[brief] headline: {exc}")
        line = ""
    source = set(NUMERAL.findall(text))
    if line and len(line.split()) <= 10 and not BANNED.search(line) \
            and set(NUMERAL.findall(line)) <= source:
        return line
    return plain(lead, names)


def alt(head, lines):
    """What the card says, for anyone who cannot see it."""
    body = " ".join(f"{l['sentence']}"
                    f"{' Held by ' + l['office'] + '.' if l['office'] else ''}" for l in lines)
    return f"A card from the AI Fear Report headed: {head}. {body}"


def chips(controls, names, most=2):
    """The two that matter. A measure can carry four, and four chips is a list, not a label."""
    seen = sorted((c for c in (controls or "").split("|") if c in names),
                  key=lambda c: -WEIGHT.get(c, 1))
    return ", ".join(dict.fromkeys(names[c]["chip"] for c in seen[:most]))


def totals(db):
    one = lambda q: db.execute(q).fetchone()[0]
    return {
        "measures": one("SELECT COUNT(*) FROM measures m JOIN tag_runs r ON r.target=m.id WHERE r.ai_related=1"),
        "controlled": one("SELECT COUNT(DISTINCT m.id) FROM measures m JOIN tags t ON t.target=m.id "
                          "WHERE t.kind='control'"),
        "offices": one("SELECT COUNT(DISTINCT value) FROM tags WHERE kind='agency'"),
    }


def entry(line):
    """One line, always the same shape: what it does, then who ends up holding it."""
    text = line["sentence"].rstrip()
    office = line.get("office") or ""
    if office and office.lower() not in text.lower():
        article = "" if office.startswith(("the ", "The ")) else "the "
        text = f"{text} Held by {article}{office}."
    return f"{ARROW} {text}"


def compose(lines, tot, cfg):
    """The post, the same shape every day: the line, the entries, the count, the address.

    Nothing rotates. A reader who sees this twice should recognise the second one before they
    have read a word of it, which is the only thing a format is for.
    """
    body = "\n".join(entry(l) for l in lines)
    # No address in the text. X linkifies a bare domain, which puts the post on the rate for a
    # post with a link and costs it reach besides. The plate carries the address instead.
    return (f"{cfg['opener']}\n\n{body}\n\n"
            f"{tot['measures']:,} measures. {tot['controlled']} carry a control. "
            f"{tot['offices']} offices hold one.")


FRESH = 1  # a measure that carries a control and names an office arrives about twice a week


def edition(db, key, out_dir, today, want=WANT, dry_run=False):
    """Build one edition: what moved, then what it all adds up to.

    Measures that carry a control arrive at roughly three a week, so an edition that waited for
    three of them would either run empty or reach for a measure that does not carry its weight.
    The pattern lines are there for that: each is computed from the whole record, so it is true on
    any day, and it only repeats once the number behind it has moved.
    """
    cfg = config("brief")
    names = controls_by_slug()
    day = dt.date.fromisoformat(today)
    if db.execute("SELECT 1 FROM brief WHERE edition=? LIMIT 1", (today,)).fetchone():
        log(f"[brief] {today} is already written")
        return None
    used = {r["target"] for r in db.execute("SELECT target FROM brief")}

    lines = write_lines(key, pool(db, today), min(FRESH, want))
    if len(lines) < want:
        # a pattern line about a control an entry above already showed says it twice
        shown = {c for l in lines for c in (l["controls"] or "").split("|") if c}
        spare = [p for p in patterns(db, today, used)
                 if p["key"].split(":")[0] != "control" or p["key"].split(":")[1] not in shown]
        spare.sort(key=lambda p: -p["weight"])
        # two lines of the same family in one edition read as padding, so take one of each
        families, picked = set(), []
        for p in spare:
            family = p["key"].split(":")[0]
            if family in families:
                continue
            families.add(family)
            picked.append(p)
        order = list({p["key"]: p for p in picked + spare}.values())
        for p in order[:want - len(lines)]:
            lines.append({"target": p["key"], "sentence": p["sentence"], "quote": "",
                          "office": p["office"], "controls": "", "meta": p["meta"],
                          "head": p["head"], "jurisdiction": "", "url": "", "status": ""})
    if not lines:
        log("[brief] nothing to say today, no edition")
        return None

    tot = totals(db)
    head = headline(key, lines, names, tot)
    text = compose(lines, tot, cfg)

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    image = str(out / f"{today}.png")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "site"))
    from brand import plate
    plate(image, day.strftime("%A %-d %B %Y"), head)
    edition_data = {"edition": today, "headline": head, "text": text, "image": image,
                    "alt": alt(head, lines), "lines": lines, "totals": tot}
    (out / f"{today}.json").write_text(json.dumps(edition_data, indent=1))
    if not dry_run:
        for l in lines:
            db.execute("INSERT OR REPLACE INTO brief(target, edition, sentence, evidence, office, "
                       "controls, written_at) VALUES(?,?,?,?,?,?,?)",
                       (l["target"], today, l["sentence"], l["quote"], l["office"], l["controls"],
                        now().isoformat()))
        db.commit()
    log(f"[brief] {today}: {len(lines)} entries, card at {image}"
        f"{' (dry run, nothing recorded)' if dry_run else ''}")
    return edition_data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/index.db")
    ap.add_argument("--out", default="state/brief")
    ap.add_argument("--date", default="")
    ap.add_argument("--lines", type=int, default=WANT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = connect(args.db)
    today = args.date or now().date().isoformat()
    data = edition(db, env("ANTHROPIC_API_KEY"), args.out, today, args.lines, args.dry_run)
    if data:
        print("\n" + "-" * 60)
        print(data["text"])
        print("-" * 60)


if __name__ == "__main__":
    main()
