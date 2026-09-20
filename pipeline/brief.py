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
import zoneinfo
import json
import pathlib
import re
import sys

from .common import Entities, annotate, config, connect, env, kv_set, log, now
from .export import gave
from .tag import agreed, call, norm

MODEL_NOTE = "claude-haiku-4-5-20251001"
WANT = 3          # lines in an edition: enough to show a pattern, few enough to read
POOL_DAYS = 45    # measures move slower than a day, so an edition draws from a rolling pool
MAX_SENTENCE = 210  # a sentence that names the power needs more room than one that names a duty


EASTERN = zoneinfo.ZoneInfo("America/New_York")


def edition_date():
    """The date an American reader would call today, which is not the date in UTC.

    A post sent at nine in the morning in New York goes out on the UTC date that matches, but one
    sent in the evening does not, and the plate then carries tomorrow.
    """
    return now().astimezone(EASTERN).date().isoformat()


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


def score(row, today, recent=()):
    """How well one measure would carry an edition.

    recent is the jurisdictions the last few editions led with. A state that files the most bills
    would otherwise lead most mornings, and a week of California reads as one state's problem
    rather than the same thing happening in thirty. The penalty is small enough that a measure
    that actually matters more still wins.
    """
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
    points -= 2 * list(recent).count(row["jurisdiction_name"])
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
    recent = [r["jurisdiction"] for r in db.execute(
        "SELECT b.edition, m.jurisdiction_name AS jurisdiction FROM brief b JOIN measures m ON m.id = b.target "
        "ORDER BY b.edition DESC LIMIT 4")]
    rows.sort(key=lambda r: (-score(r, today, recent), r["moved"] or ""))
    return rows[:limit]


def doc(row):
    return f"Jurisdiction: {row['jurisdiction_name']}\nIdentifier: {row['identifier']}\n" \
           f"Title: {row['title'] or ''}\nSummary: {(row['summary'] or '')[:6000]}"


SYSTEM = (
    "You write one sentence about a U.S. bill or rule for a public record of what fear of AI is "
    "buying and who ends up holding it. The reader is about to be bound by these measures. Reply "
    "with a single JSON object and nothing else."
)

RULES = (
    "Write one sentence, at most 30 words. {tense}\n"
    "If the measure says it starts in a later year, put that year in the sentence. A law on the "
    "books that bites in 2029 is still the story, and saying from 2029 is more of it, not less.\n"
    "Say what the measure puts under whose control. Name the government body that would decide, "
    "inspect, licence, or be reported to, inside the sentence. If the measure names no body, say "
    "the state or the jurisdiction by name.\n"
    "A duty on a company is a power for whoever it answers to, and it is the power that goes in "
    "the sentence. Not required to restore the water, but put under the department that decides "
    "whether it has. Not required to submit to audits reported to the attorney general, but put "
    "under an auditor whose report the attorney general can demand.\n"
    "So do not open with the jurisdiction and then requires, mandates, directs or orders followed "
    "by a company. Open with what is put under whose control.\n"
    "If a duty falls on everyone in order to identify some people, the sentence says everyone: a "
    "rule that checks whether a user is a child checks every user.\n"
    "Never use a word the sponsor chose to make the measure sound smaller than it is. Do not write "
    "safeguards, guardrails, protections, safety net, common sense, modernize, framework, or "
    "oversight. Name the power.\n"
    "Use the measure's own words for what it does. Do not reach for a near neighbour of one: a "
    "supply a data centre diminishes is a diminished supply, never a diminutive one.\n"
    "Report it. Do not argue it, do not say what it shows or reveals or highlights, and do not "
    "end on a clause beginning with a participle: no underscoring its importance, no reflecting a "
    "trend. Do not say it is not one thing but another. Do not ask a question and answer it.\n"
    "Plain words. Not robust, comprehensive, sweeping, landscape, framework, pivotal, crucial, "
    "significant, or key. No adjective that is not doing specific work.\n"
    "Do not say whether the measure is good or bad, and do not add a reason it was introduced.\n"
    "Do not name the bill number, and do not write the words bill, act, or legislation. Start with "
    "the jurisdiction.\n"
    "quote is copied word for word from the Title or Summary and is the text the sentence rests "
    "on, between 6 and 30 words."
)


LAW = ("This measure has passed. Write it in the present tense: it requires, it puts, it bans.")
PENDING = ("This measure has not passed. It was introduced and is somewhere in the process, so every "
           "verb is conditional: it would require, it would put. Nobody is bound by it yet, so do not "
           "write that anyone must do anything, or that anything is already the case.")


def sentence(key, row):
    rules = RULES.format(tense=LAW if (row["status"] or "") == "passed" else PENDING)
    out = call(key, SYSTEM, f"TEXT\n{doc(row)}\n\n{rules}\n\n"
                            'Return JSON: {"sentence": "...", "quote": "..."}', max_tokens=400)
    return (out.get("sentence") or "").strip(), (out.get("quote") or "").strip()


BANNED = re.compile(
    r"\b(safeguard\w*|guardrail\w*|protections?|safety net|common ?sense|modern\w*|framework\w*|"
    r"oversight|responsib\w*|thoughtful\w*|balanced?|sensible|reasonable steps|bad actors?|"
    # the measured ones: vocabulary that marks a machine wrote it, and the puffery under it
    r"robust|comprehensive|sweeping|landscape|pivotal|crucial|vital|significant|"
    r"underscor\w*|highlight\w*|showcas\w*|delv\w*|intricate|nuanced|holistic|seamless|"
    r"transformative|groundbreaking|far-reaching|unprecedented)\b", re.I)

# a sentence that stops reporting and starts explaining what it means
EXPLAINING = re.compile(r",\s+\w+ing\b[^.]*\.$|\bnot (just|only|merely)\b|\bit is not\b", re.I)


CONDITIONAL = re.compile(r"\b(would|could|may)\b", re.I)

# A sentence that names the power has a shape: something is put under a body, a body is handed
# something, or a body does the deciding. The words alone are not enough. "Under penalty of
# perjury", "under the Clean Energy Act" and "gives notice to every user" all contain the shape
# and none of them hands anybody anything, so what follows has to be a body.
BODY = (r"(?:the\s+|an?\s+)?(?:[A-Z][\w.'-]*\s+){0,4}"
        r"(?:Department|Office|Bureau|Division|Board|Commission|Agency|Authority|Council|Registry|"
        r"Attorney\s+General|Secretary|Director|Administrator|Comptroller|Inspector|Auditor|Regulator)"
        r"|(?:an?|the)\s+(?:independent\s+|outside\s+|third[- ]party\s+|state\s+)?"
        r"(?:auditor|inspector|regulator|registry|register|licence|license|permit|moratorium|"
        r"inventory|review|registration|certification|audit|approval)")
POWER = re.compile(
    rf"\bunder\s+(?:{BODY})"
    r"|\b(?:which|who|whom|that)\s+(?:\w+\s+){0,2}"
    r"(?:decid\w+|licens\w+|approv\w+|inspect\w+|certif\w+|investigat\w+|revok\w+|"
    r"registers?|determin\w+|enforc\w+|permits?|refus\w+|withhold\w+|bars?|blocks?|"
    r"suspends?|suspend|suspending|issu\w+|audits?|holds?|keeps?)\b"
    rf"|\b(?:gives?|hands?|grants?|leaves?)\s+(?:{BODY})"
    r"|\b(?:licen[sc]ed|approved|certified|registered|inspected|authoris?zed|vetted)\s+by\b", re.I)
DUTY_FRAMED = "written as a duty on a company, not the power it creates"
# Looked for in the main clause rather than the first few words, because "Health and Human Services
# Department, Food and Drug Administration requires operators to file reports" is the same sentence
# with a longer name on the front.
DUTY_OPENER = re.compile(r"\b(requires?|mandates?|obligates?|directs?|orders?|compels?)\b", re.I)

# When a measure that has passed says it starts in a later year, the present tense alone claims it
# is in force now. Four of the strongest candidates in the pool are exactly this: California bills
# chaptered this year whose registries and duties begin in 2027, 2028 or 2029.
STARTS = re.compile(r"\b(?:commencing|operative|effective|beginning|no later than|on or after)\s+"
                    r"(?:on\s+)?(?:January|February|March|April|May|June|July|August|September|"
                    r"October|November|December)\s+\d{1,2},\s*(20\d\d)", re.I)


def starts_later(text, today):
    """The first year after this one that the measure names as its own start, if it names one."""
    year = dt.date.fromisoformat(today).year if isinstance(today, str) else today.year
    for m in STARTS.finditer(text or ""):
        if int(m.group(1)) > year:
            return m.group(1)
    return ""

# Where the tense of the sentence lives. The sentence has to start with the jurisdiction, so the
# main verb is in the first clause; everything from the first comma or relative pronoun onward
# describes the office. That matters because "may" is the natural word for a power the office
# holds, and "decides who may operate" is a present-tense fact, not a measure that might pass.
MAIN_CLAUSE = re.compile(r",|\b(which|who|whom|whose|that|whether)\b", re.I)


def tense_of(text, jurisdiction=""):
    """The first clause, which is the one carrying the tense.

    The jurisdiction comes off the front first, because one of them is called "Health and Human
    Services Department, Food and Drug Administration" and the comma in its name is not a clause
    boundary. Without this the clause is the name alone, which carries no verb at all: every
    pending rule from that agency would read as written in the present tense, and every duty
    sentence from it would slip the check for one.
    """
    body = (text or "").strip()
    if jurisdiction and body.lower().startswith(jurisdiction.lower()):
        body = body[len(jurisdiction):].lstrip(" ,")
    return MAIN_CLAUSE.split(body, 1)[0]


OFFICIAL = re.compile(
    r"\b(?:[A-Z][\w.'-]*\s+){0,4}"
    r"(?:Department|Office|Bureau|Division|Board|Commission|Agency|Authority|Committee|Council)"
    r"(?:\s+(?:of|for|on|and|the)\s+[A-Z][\w.'-]*|\s+[A-Z][\w.'-]*){0,5}")


def without_names(text, office="", loose=False):
    """The sentence minus the proper names of the bodies in it.

    The banned list is there to stop the sponsor's vocabulary getting in: protections, safeguards,
    oversight. A body actually called the Department of Environmental Protection carries one of
    those words in its own name, and naming the body is the point of the sentence. Eight of the
    195 offices on file are in this position, the New York Office for AI Model Developer Oversight
    among them, and without this the edition throws away its best line and falls back to a count.
    """
    out = text
    if office:
        out = re.sub(re.escape(office), " ", out, flags=re.I)
    out = OFFICIAL.sub(" ", out)
    if loose and office:
        # A headline has nine words and drops "Department of", so the full name never survives it
        # and neither does the pattern that looks for one. Every word of the office's own name
        # comes out instead. Only for the headline: a sentence has room to write the name in full,
        # and stripping single words there would let the sponsor's own use of one through.
        for word in set(re.findall(r"[A-Za-z]{4,}", office)):
            out = re.sub(rf"\b{re.escape(word)}\b", " ", out, flags=re.I)
    return out


def why_not(text, quote, body_norm, passed=False, office="", starts="", jurisdiction=""):
    """The reason a sentence was rejected, or an empty string if it holds up.

    Named rather than returned as a bare False, so a run that throws every candidate away says
    which gate did it instead of leaving the next person to guess.
    """
    if not text:
        return "no sentence"
    if not quote:
        return "no quote"
    head = tense_of(text, jurisdiction)
    if passed and CONDITIONAL.search(head):
        return f"passed but says {CONDITIONAL.search(head).group(0)!r}"
    if not passed and not CONDITIONAL.search(head):
        return "pending but written as though it binds"
    if len(text) > MAX_SENTENCE:
        return f"{len(text)} characters, over {MAX_SENTENCE}"
    if not text.endswith("."):
        return "no full stop"
    banned = BANNED.search(without_names(text, office))
    if banned:
        return f"sponsor's word {banned.group(0)!r}"
    if EXPLAINING.search(text):
        return "explains rather than reports"
    loose = re.search(r"\b(bill|act|legislation|lawmakers?)\b", text, re.I)
    if loose:
        return f"says {loose.group(0)!r} instead of naming who is bound"
    if passed and starts and starts not in text:
        return f"passed but starts in {starts}, and the sentence does not say so"
    if DUTY_OPENER.search(head) and not POWER.search(text):
        return DUTY_FRAMED
    if not agreed({"controls": {"line": quote}}, body_norm, "controls"):
        return "the quote it leaned on is not in the measure"
    return ""


def usable(text, quote, body_norm, passed=False, office="", starts="", jurisdiction=""):
    """The sentence has to rest on words in the text, not borrow the sponsor's vocabulary, and not
    describe a bill sitting in committee as though it already bound anyone.

    The tense is the whole difference between a record and a rumour, so it is checked here rather
    than trusted to the instruction above: a measure that has not passed says would, and one that
    has does not.
    """
    return not why_not(text, quote, body_norm, passed, office, starts, jurisdiction)


def note(attempts, where, reason, sentence_text="", fatal=False):
    if attempts is not None:
        attempts.append({"measure": where, "reason": reason,
                         "sentence": (sentence_text or "")[:220], "unreachable": fatal})


def write_lines(key, rows, today, want=WANT, attempts=None):
    """Turn candidate measures into checked sentences, stopping once the edition is full.

    Every candidate that does not make it appends its reason to attempts, which the edition stores
    in the database. An edition that falls back to counts has thrown a dozen sentences away, and
    without this the only record of why is a CI log that expires.
    """
    lines, places, second = [], set(), None
    for row in rows:
        if len(lines) >= want:
            break
        # one state should not be the whole edition; the case is that this is everywhere
        if row["jurisdiction_name"] in places:
            continue
        where = f"{row['jurisdiction_name']} {row['identifier']}"
        body = norm(f"{row['title'] or ''}\n{row['summary'] or ''}")
        try:
            text, quote = sentence(key, row)
        except (NameError, AttributeError, TypeError, KeyError, IndexError):
            # A broken name or signature is a fault in this file. Swallowing it row by row is how
            # a missing tense string cost every edition for a day while the log said the sentences
            # did not hold up. It stops the run instead.
            raise
        except Exception as exc:  # one bad row must not cost the edition
            note(attempts, where, f"the model did not answer: {str(exc)[:160]}", fatal=True)
            log(f"[brief] {where}: the model did not answer, {str(exc)[:160]}")
            continue
        starts = starts_later(f"{row['title'] or ''} {row['summary'] or ''}", today)
        reason = why_not(text, quote, body, (row["status"] or "") == "passed", row["office"] or "",
                         starts, row["jurisdiction_name"] or "")
        if reason:
            note(attempts, where, reason, sentence_text=text)
            log(f"[brief] {where}: skipped, {reason}")
            if reason == DUTY_FRAMED and second is None:
                # True, checkable, and the wrong way round. Worth keeping in reserve: an edition
                # of counts says less than a sentence in the sponsor's grammar.
                second = (row, where, text, quote)
            continue
        note(attempts, where, "used")
        places.add(row["jurisdiction_name"])
        lines.append(made(row, text, quote))
    if not lines and second is not None:
        row, where, text, quote = second
        note(attempts, where, "used, though it is written as a duty")
        log(f"[brief] {where}: used, though it is written as a duty and nothing better was written")
        lines.append(made(row, text, quote))
    return lines


def made(row, text, quote):
    return {"target": row["id"], "sentence": text, "quote": quote,
            "office": row["office"] or "", "controls": row["controls"] or "",
            "fears": row["fears"] or "", "jurisdiction": row["jurisdiction_name"],
            "url": row["url"], "status": row["status"] or ""}


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
    "If the sentence says something is put under a body, or that a body decides or licenses or "
    "refuses, that is the thing it does and it stays. A list of what a company has to do is not.\n"
    "It is a plain statement with a verb. Someone who reads only this line and nothing else "
    "should come away knowing one fact.\n"
    "Do not be clever, do not ask a question, and do not open with who, what, how, why, or when.\n"
    "Keep the tense exactly as the sentence has it. If the sentence says would, the headline says "
    "would. Dropping it turns a proposal into a law and the headline into a false one.\n"
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
    # the headline is the largest text on the plate, so it answers for its tense like the rest
    source = set(NUMERAL.findall(text))
    place = lead.get("jurisdiction") or ""
    conditional = bool(CONDITIONAL.search(tense_of(text, place)))
    if line and len(line.split()) <= 10 and not BANNED.search(without_names(line, lead.get("office") or "", loose=True)) \
            and set(NUMERAL.findall(line)) <= source \
            and bool(CONDITIONAL.search(tense_of(line, place))) == conditional:
        return line
    return plain(lead, names)


def alt(head, lines):
    """What the card says, for anyone who cannot see it."""
    body = " ".join(f"{l['sentence']}"
                    f"{' Held by ' + l['office'] + '.' if l['office'] else ''}" for l in lines)
    return f"A card from the AI Fear Report headed: {head}. {body}"


def totals(db):
    one = lambda q: db.execute(q).fetchone()[0]
    return {
        "measures": one("SELECT COUNT(*) FROM measures m JOIN tag_runs r ON r.target=m.id WHERE r.ai_related=1"),
        "controlled": one("SELECT COUNT(DISTINCT m.id) FROM measures m JOIN tags t ON t.target=m.id "
                          "WHERE t.kind='control'"),
        "offices": one("SELECT COUNT(DISTINCT value) FROM tags WHERE kind='agency'"),
    }


def subject(db, lead):
    """Which of a measure's controls the rest of the post talks about.

    A measure often carries several. The one worth the most is the one to lead on when nothing
    else decides it, but the reason and the pattern both have to be about the same one or the
    post says three things about three subjects.
    """
    controls = [c for c in (lead.get("controls") or "").split("|") if c]
    return max(controls, key=lambda c: WEIGHT.get(c, 1)) if controls else ""


def cited(lead, fears):
    """The fear the measure's own text cites, or nothing.

    Only what this measure says. An earlier version inferred a fear from the other measures
    imposing the same control and said so, which is an inference dressed as reporting.
    """
    for slug in (lead.get("fears") or "").split("|"):
        f = fears.get(slug)
        if f and f.get("because"):
            return f["because"]
    return ""


STATUS = {"passed": "Passed.", "failed": "Died in committee.", "pending": "Still live."}


def compose(lead, spare, tot, cfg, fears):
    """The measures, what they cite, and the counts. Nothing joining them up.

    The site does not tell a reader what to make of a row and neither does this. No line here
    says that one measure is part of a pattern; the measure is stated, the count is stated, and
    the reader puts them together or does not.
    """
    parts = [cfg["opener"], ""]
    first = [lead["sentence"]]
    if lead.get("status") in STATUS:
        first.append(STATUS[lead["status"]])
    fear = cited(lead, fears)
    if fear:
        first.append(f"It cites the fear that {fear}.")
    parts += [" ".join(first)]
    for p in spare:
        parts += ["", p["sentence"]]
    parts += ["", f"{tot['measures']:,} measures tracked. {tot['controlled']} carry a control. "
                  f"{tot['offices']} offices hold one."]
    return "\n".join(parts).strip()


FRESH = 1  # a measure that carries a control and names an office arrives about twice a week


def spare_for(db, lead, today, used):
    """The two counts under the measure: one about the control, one about a fear.

    A post that never names a fear does not answer for its own first line. A quarter of measures
    cite one in their own text, so the fear is carried by a count instead, which is a fact about
    the record rather than a claim about the measure above it.
    """
    spare = sorted(patterns(db, today, used), key=lambda p: -p["weight"])
    controls = [c for c in (lead.get("controls") or "").split("|") if c]
    mine = max(controls, key=lambda c: WEIGHT.get(c, 1)) if controls else ""
    by_control = [p for p in spare if p["key"].startswith("control:")]
    if mine:
        by_control.sort(key=lambda p: not p["key"].startswith(f"control:{mine}:"))
    # the fears all weigh the same, so the one worth printing is the one with the most behind it
    by_fear = sorted((p for p in spare if p["key"].startswith("fear:")),
                     key=lambda p: -int(p["key"].rsplit(":", 1)[1]))
    out = by_control[:1] + by_fear[:1]
    return out or spare[:2]


def edition(db, key, out_dir, today, want=WANT, dry_run=False):
    """One measure that moved, and the counts it sits among.

    Measures carrying a control arrive at about nine a week, which one a day can live on. The
    counts are computed from the whole record, so they hold on any day, and each is keyed to its
    own number: it comes round again only once that number has moved.
    """
    cfg = config("brief")
    names, fear_names = controls_by_slug(), fears_by_slug()
    day = dt.date.fromisoformat(today)
    if db.execute("SELECT 1 FROM brief WHERE edition=? LIMIT 1", (today,)).fetchone():
        log(f"[brief] {today} is already written")
        return None
    used = {r["target"] for r in db.execute("SELECT target FROM brief")}

    attempts = []
    lines = write_lines(key, pool(db, today), today, 1, attempts)
    kv_set(db, f"brief:attempts:{today}", attempts)
    db.commit()
    unreachable = [a for a in attempts if a.get("unreachable")]
    if attempts and len(unreachable) == len(attempts):
        # Not one candidate was judged: the model could not be reached at all. The counts below
        # are still true and the edition still goes out, because a day with no post is worse than
        # a plain one. But an edition of counts looks like an ordinary quiet day from outside, so
        # this marks the run red rather than letting it pass for one.
        annotate("error", "the brief reached no model",
                 f"none of {len(attempts)} candidates were judged: {unreachable[0]['reason']}")
        kv_set(db, f"brief:unreachable:{today}", unreachable[0]["reason"][:300])
    lead = lines[0] if lines else None
    if lead is None:
        spare = sorted(patterns(db, today, used), key=lambda p: -p["weight"])
        if not spare:
            log("[brief] nothing to say today, no edition")
            return None
        top = spare[0]
        lead = {"target": top["key"], "sentence": top["sentence"], "quote": "", "office": top["office"],
                "controls": "", "fears": "", "meta": top["meta"], "head": top["head"],
                "jurisdiction": "", "url": "", "status": ""}
        spare = spare[1:3]
    else:
        spare = spare_for(db, lead, today, used)

    tot = totals(db)
    head = headline(key, [lead], names, tot)
    text = compose(lead, spare, tot, cfg, fear_names)
    rows = [lead] + [{"target": p["key"], "sentence": p["sentence"], "quote": "", "office": p["office"],
                      "controls": "", "meta": p["meta"], "head": p["head"], "jurisdiction": "",
                      "url": "", "status": ""} for p in spare]

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    image = str(out / f"{today}.png")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "site"))
    from brand import plate
    plate(image, day.strftime("%A %-d %B %Y"), head)
    edition_data = {"edition": today, "headline": head, "text": text, "image": image,
                    "alt": alt(head, rows), "lines": rows, "totals": tot}
    (out / f"{today}.json").write_text(json.dumps(edition_data, indent=1))
    if not dry_run:
        for l in rows:
            db.execute("INSERT OR REPLACE INTO brief(target, edition, sentence, evidence, office, "
                       "controls, written_at) VALUES(?,?,?,?,?,?,?)",
                       (l["target"], today, l["sentence"], l["quote"], l["office"], l["controls"],
                        now().isoformat()))
        db.commit()
    log(f"[brief] {today}: {len(rows)} entries, plate at {image}"
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
    today = args.date or edition_date()
    data = edition(db, env("ANTHROPIC_API_KEY"), args.out, today, args.lines, args.dry_run)
    if data:
        print("\n" + "-" * 60)
        print(data["text"])
        print("-" * 60)


if __name__ == "__main__":
    main()
