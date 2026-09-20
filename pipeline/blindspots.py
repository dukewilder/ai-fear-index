"""What the nine fears do not cover, counted rather than guessed.

The nine are hand-written on purpose: a ranking only means something if its categories hold still,
and one fear has to be defined identically across four sources that share no vocabulary. The cost
of that is a blind spot. A fear nobody has written down yet is invisible, however many bills are
filed about it, because nothing is looking.

This looks, and it separates two things that a word count runs together.

A measure with no fear on it whose text plainly matches a fear that already exists is a recall
gap: the words are there and the tagger did not reach it. Those are counted by fear and taken out
of everything below, so they cannot masquerade as a new subject.

What is left is measures that match no fear at all. The phrases recurring in those are subjects
legislatures are writing about that this report has no name for. It names no fear and adds
nothing: a person reads the list and decides.

    python -m pipeline.blindspots --db state/index.db
"""
import argparse
import collections
import json
import pathlib
import re

from .common import EUPHEMISM, config, connect, iso, kv_get, kv_set

MIN_TEXT = 200      # characters of summary; below this the tagger had nothing to read
MIN_DOCS = 5        # a phrase in fewer measures than this is noise, not a subject
MIN_PLACES = 3      # and in fewer legislatures than this it is one state's drafting formula
TOP = 40
NGRAMS = (2, 3)  # a single word is a register, not a subject: "public", "requirements", "information"

WORD = re.compile(r"[a-z][a-z-]{2,}")
NUMBERISH = re.compile(r"^\d|^[a-z]$")

# Legislative furniture. These recur in every bill ever written and say nothing about its subject.
BOILERPLATE = set("""
act bill acts bills relating relative concerning concerns regarding respecting provide provides
provided providing require requires required requiring establish establishes established
establishing create creates created creating enact enacts enacted enacting repeal repeals
repealed add adds added adding revise revises revised revising authorize authorizes authorized
section sections chapter chapters title titles article articles subsection subsections paragraph
code codes statute statutes law laws legislature legislative general assembly senate house
committee session regular special introduced referred reading engrossed enrolled adopted
amend amends amended amending amendment amendments
state states commonwealth department departments office offices division agency agencies
board boards commission commissions authority authorities director secretary administrator
relates relate related provisions provision purposes purpose respect certain various
shall must may not any all each other others such same than then there these this that
appropriation appropriations fund funds funding moneys money fiscal year years date effective
definitions definition defined define including include includes included
artificial intelligence intelligent systems system technology technologies technological
use uses used using user users make makes making take takes report reports reporting
new person persons entity entities
the and for with that from into than about upon their its his her our your them they
are was were been being have has had will would could should can shall does did
within without under over between through during before after above below
one two three four five six seven eight nine ten first second third
who whom which what when where while because however therefore thus hereby herein
""".split())


def phrases(text):
    """Every one, two and three word phrase in a piece of text, boilerplate stripped."""
    words = WORD.findall((text or "").lower())
    out = set()
    for n in NGRAMS:
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in BOILERPLATE or gram[-1] in BOILERPLATE:
                continue
            joined = " ".join(gram)
            if NUMBERISH.match(joined):
                continue
            out.add(joined)
    return out


def fear_patterns(fears):
    """One entry per fear: every pattern it lists, all of which have to appear.

    config/fears.json states a fear as a set of conditions, not alternatives, which is how the
    news collector reads it too. Firing on any one of them makes six fears match the same bill.
    """
    out = []
    for f in fears:
        rules = []
        for pat in f.get("match") or []:
            try:
                rules.append(re.compile(pat, re.I))
            except re.error:
                pass
        if rules:
            out.append((f["slug"], rules))
    return out


def fear_names(fears):
    """The words the nine call themselves, so the list does not report them back."""
    words = set()
    for f in fears:
        for source in [f["name"], f.get("short") or ""]:
            words |= set(WORD.findall(source.lower()))
    return words


def unread(db):
    """AI measures with enough text to judge and no fear on them."""
    return [dict(r) for r in db.execute(
        "SELECT m.id, m.jurisdiction_name, m.identifier, m.title, m.summary "
        "FROM measures m JOIN tag_runs t ON t.target = m.id "
        "WHERE t.ai_related = 1 AND LENGTH(COALESCE(m.summary,'')) >= ? "
        "  AND m.id NOT IN (SELECT target FROM tags WHERE kind='fear')", (MIN_TEXT,))]


def condense(counts):
    """Drop a phrase that is only ever part of a longer one that counts nearly the same.

    Without this the list reads "health", "health care", "health care service" three times over
    and the longest, which is the one that says something, is buried under the other two.
    """
    ordered = sorted(counts, key=lambda p: (-len(p.split()), -counts[p]))
    kept = {}
    for phrase in ordered:
        if any(phrase != longer and phrase in longer and counts[phrase] <= counts[longer] * 1.25
               for longer in kept):
            continue
        kept[phrase] = counts[phrase]
    return kept


SOFT = re.compile(r"\b(?:safe\w*|protect\w*|secure|security|responsib\w*|account\w*|trust\w*|"
                 r"ethic\w*|transparen\w*|modern\w*|framework\w*|govern\w*|guid\w*|standard\w*|"
                 r"assur\w*|integrity|wellbeing|welfare|harm|risk|oversight|balanc\w*|reasonab\w*|"
                 r"appropriat\w*|sensib\w*|empower\w*|ensur\w*|innovat\w*|fair\w*|equitab\w*|"
                 r"consumer|common[\s-]?sense)\b", re.I)


def sponsor_words(db):
    """Words sponsors put in their own bill titles that the euphemism list does not yet refuse.

    A list of euphemisms written from memory is a list of the ones already thought of. The bills
    arrive every day carrying the real vocabulary, so this reads the titles rather than guessing:
    every soft word a sponsor chose to name a bill, minus the ones already refused, by how many
    bills and how many legislatures use it. Adding one is a decision, not an automatic thing.
    """
    counts, places = collections.Counter(), collections.defaultdict(set)
    for r in db.execute("SELECT m.title, m.jurisdiction_name j FROM measures m "
                        "JOIN tag_runs t ON t.target = m.id WHERE t.ai_related = 1"):
        title = r["title"] or ""
        for m in SOFT.finditer(title):
            word = m.group(0).lower()
            if EUPHEMISM.search(word):
                continue   # already refused
            counts[word] += 1
            places[word].add(r["j"])
    return [{"word": w, "titles": n, "places": len(places[w])}
            for w, n in counts.most_common(30) if n >= 5 and len(places[w]) >= 3]


def run(db, state, mode):
    fears = config("fears")
    patterns, named = fear_patterns(fears), fear_names(fears)
    rows = unread(db)
    counts, missed = collections.Counter(), collections.Counter()
    places = collections.defaultdict(set)
    examples = collections.defaultdict(list)
    uncovered_rows = 0

    for r in rows:
        text = f"{r['title'] or ''} {r['summary'] or ''}"[:4000]
        hits = {slug for slug, rules in patterns if all(rx.search(text) for rx in rules)}
        if hits:
            # the words of an existing fear are in the text and no tag came of it
            for slug in hits:
                missed[slug] += 1
                if len(examples[slug]) < 5:
                    examples[slug].append(f"{r['jurisdiction_name']} {r['identifier']}")
            continue
        uncovered_rows += 1
        for p in phrases(text):
            if any(w in named for w in p.split()):
                continue
            counts[p] += 1
            places[p].add(r["jurisdiction_name"])

    # A subject shows up in several legislatures. A phrase confined to one or two is that state's
    # drafting formula: "official code georgia annotated", "impose state-mandated local program".
    # Counting the places it appears in throws those out without a list of them having to be kept.
    counts = condense({p: n for p, n in counts.items()
                       if n >= MIN_DOCS and len(places[p]) >= MIN_PLACES})
    top = sorted(counts.items(), key=lambda kv: (-len(places[kv[0]]), -kv[1]))[:TOP]

    soft = sponsor_words(db)
    was = kv_get(db, "blindspots:last", {})
    report = {
        "at": iso(), "read": len(rows), "uncovered_measures": uncovered_rows, "floor": MIN_TEXT,
        "uncovered": [{"phrase": p, "measures": n, "places": len(places[p]),
                       "change": (n - was[p]) if p in was else None} for p, n in top],
        "missed": [{"fear": slug, "measures": n, "examples": examples[slug]}
                   for slug, n in sorted(missed.items(), key=lambda kv: -kv[1])],
        "sponsor_words": soft,
    }
    kv_set(db, "blindspots:last", {p: n for p, n in top})
    kv_set(db, "blindspots:report", report)
    db.commit()

    risers = [u for u in report["uncovered"] if (u["change"] or 0) > 0][:3]
    state["added"] = len(top)
    state["message"] = (
        f"{uncovered_rows} of {len(rows)} untagged measures match no fear at all; "
        f"{len(top)} subjects the nine do not name"
        + (f"; growing: {', '.join(u['phrase'] + ' +' + str(u['change']) for u in risers)}" if risers else "")
        + (f"; recall gaps: {', '.join(m['fear'] + ' ' + str(m['measures']) for m in report['missed'][:3])}"
           if report["missed"] else "")
        + (f"; sponsors' words not yet refused: {', '.join(w['word'] for w in soft[:6])}" if soft else ""))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/index.db")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    db = connect(args.db)
    state = {"added": 0, "message": ""}
    report = run(db, state, "manual")
    print(state["message"], "\n")
    print(f"  {'subject the nine do not name':<40}{'measures':>9}{'places':>8}{'change':>8}")
    for u in report["uncovered"]:
        change = "" if u["change"] is None else f"{u['change']:+d}"
        print(f"  {u['phrase']:<40}{u['measures']:>9}{u['places']:>8}{change:>8}")
    if report["missed"]:
        print(f"\n  {'a fear its text matches, with no tag on it':<44}{'measures':>9}")
        for m in report["missed"]:
            print(f"  {m['fear']:<44}{m['measures']:>9}   e.g. {', '.join(m['examples'][:3])}")
    if report["sponsor_words"]:
        print(f"\n  {'a soft word in bill titles, not yet refused':<44}{'titles':>9}{'places':>8}")
        for w in report["sponsor_words"]:
            print(f"  {w['word']:<44}{w['titles']:>9}{w['places']:>8}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=1) + "\n")
        print(f"\nwritten to {args.json}")


if __name__ == "__main__":
    main()
