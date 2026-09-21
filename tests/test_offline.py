"""Offline check: build a synthetic database, export it, and render every page.

Run with: python -m tests.test_offline
Nothing here touches the network or the real data.
"""
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import export  # noqa: E402
from pipeline.common import config, connect, iso, name_key, upsert  # noqa: E402


def check_brief_prompt():
    """Build the prompt the brief sends, for both statuses, without touching the network.

    A rewrite of the rules once deleted the two tense strings and left the line that formats them
    in. Every sentence then died of a NameError that the row-by-row catch swallowed, so a whole
    day of editions fell back to counts while the log said the sentences did not hold up. This
    costs nothing and would have caught it.
    """
    from pipeline import brief
    for status, expect in (("passed", "present tense"), ("pending", "conditional")):
        tense = brief.LAW if status == "passed" else brief.PENDING
        rules = brief.RULES.format(tense=tense)
        assert expect in rules, f"{status}: the rules do not tell the model the tense"
        assert "{tense}" not in rules, f"{status}: the placeholder survived"
    row = {"jurisdiction_name": "Ohio", "identifier": "HB 1", "title": "A title",
           "summary": "A summary long enough to quote from.", "status": "pending",
           "office": "Ohio Department of Commerce"}
    assert "Ohio" in brief.doc(row)
    # and the gates, both ways round
    body = "ohio would put every operator under the ohio department of commerce"
    good = "Ohio would put every operator under the Ohio Department of Commerce, which decides who may run one."
    assert brief.why_not(good, "would put every operator under the ohio department", body, False,
                         row["office"]) == "", "a sound pending sentence was rejected"
    assert brief.why_not(good, "would put every operator under the ohio department", body, True,
                         row["office"]), "a passed measure written conditionally was accepted"
    # the gates that decide whether a sentence is reporting or repeating the sponsor
    FDA = "Health and Human Services Department, Food and Drug Administration"
    SHAPES = [
        # the sponsor's grammar, including the ways it can look like the other kind
        ("California requires operators of companion chatbots to perform risk assessments and submit "
         "to independent audits reported to the Attorney General.", "California", False),
        (FDA + " requires operators to file reports and keep records.", FDA, False),
        ("California requires operators to certify compliance under penalty of perjury.", "California", False),
        ("California requires operators to file reports that may be audited later.", "California", False),
        ("Michigan would require utilities to file forecasts under the Clean Energy Act.", "Michigan", False),
        ("Texas requires a developer to give notice to every user of the system.", "Texas", False),
        ("Michigan would require utilities to file data center load forecasts with the Public "
         "Service Commission.", "Michigan", False),
        # and sentences that do name a power
        ("California puts AI auditors under registration with the Government Operations Agency, "
         "which licenses them and investigates violations.", "California", True),
        ("New York would require every frontier developer to register with the Office for AI Model "
         "Developer Oversight, which decides who may release one.", "New York", True),
        ("California puts companion chatbots under independent child safety audits reported to the "
         "Attorney General beginning July 1, 2027.", "California", True),
        ("Michigan would put every data center's load forecast under the Public Service Commission.",
         "Michigan", True),
        ("California requires every companion chatbot to be certified by an auditor the Attorney "
         "General approves.", "California", True),
        ("Ohio prohibits a person from deploying an artificial intelligence that represents itself "
         "as a therapist.", "Ohio", True),
    ]
    for text, place, keep in SHAPES:
        head = brief.tense_of(text, place)
        kept = not (brief.DUTY_OPENER.search(head) and not brief.POWER.search(text))
        assert kept == keep, f"power framing: {text[:60]!r} should be {'kept' if keep else 'rejected'}"
    # and through why_not itself, so removing the gate fails here rather than in production
    duty = ("California requires operators of companion chatbots to perform risk assessments and "
            "submit to independent audits reported to the Attorney General.")
    dbody = ("california requires operators of companion chatbots to perform risk assessments and "
             "submit to independent audits reported to the attorney general")
    assert "duty" in brief.why_not(duty, "requires operators of companion chatbots to perform risk",
                                   dbody, True, "", "", "California"), "the duty gate is not wired in"
    assert brief.starts_later("commencing January 1, 2029, the agency", "2026-09-20") == "2029"
    assert brief.starts_later("effective January 1, 2025, the agency", "2026-09-20") == ""
    later = ("California puts AI auditors under registration with the Government Operations Agency, "
             "which licenses them.")
    lbody = ("california puts ai auditors under registration with the government operations agency "
             "which licenses them")
    assert "2029" in brief.why_not(later, "puts ai auditors under registration with the government",
                                   lbody, True, "", "2029", "California"), "the start-year gate is not wired in"
    assert brief.why_not(later.replace("Agency,", "Agency from 2029,"),
                         "puts ai auditors under registration with the government",
                         lbody, True, "", "2029", "California") == "", "a sentence carrying the year was rejected"
    print("brief prompt and gates: ok")


def check_plate_fits():
    """The card must carry the whole headline, at whatever size that takes.

    It used to set three lines at one size and drop the rest, so an eleven word headline went out
    reading "...UNDER ENVIRONMENTAL" and stopping. A smaller headline is a headline. Half of one
    is a different claim.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    try:
        import brand
    except Exception as exc:  # the fonts are not always present, and the pages matter more
        print("plate check skipped:", exc)
        return
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (1600, 900)))
    room = (900 - 92 - 30) - 34 - (92 + 24 + 120 * 1.62 + 132)
    for head in ("California puts companion chatbots under child safety audits",
                 "Pennsylvania would put commercial data center water supplies under Environmental Protection",
                 "New York puts every frontier model developer under an office that decides who may release one"):
        _, lines, step = brand.fit_headline(d, head, 1600 - 184, room)
        assert " ".join(lines).split() == head.upper().split(), f"the card would drop words from: {head}"
        assert len(lines) * step <= room, f"the card would run into its own footer: {head}"
    print("plate fits the whole headline: ok")


def check_headline_tidy():
    """Tidying a headline may move punctuation and cut a repeated publisher. Never a word.

    Every headline on the site goes through this, so the thing to hold it to is that the letters
    survive. Checked against all 354 stored at the time it was written: 152 changed punctuation
    only, 3 had a credit cut from the end, none was altered in any other way.
    """
    import re as _re
    from pipeline.common import tidy_headline
    letters = lambda s: _re.sub(r"[^a-z0-9]", "", (s or "").lower())
    KEEP = [
        ("U . S . weighs new rules on chips", "reuters.com", "U.S. weighs new rules on chips"),
        ("Trump Announces  AI Force , Czar Appointment", "dailycaller.com",
         "Trump Announces AI Force, Czar Appointment"),
        ("How Can AI Kill All Humans ? Experts Explain", "nypost.com",
         "How Can AI Kill All Humans? Experts Explain"),
        ("The case for AI - and against it", "theguardian.com", "The case for AI - and against it"),
        ("AI and the state-by-state patchwork", "axios.com", "AI and the state-by-state patchwork"),
    ]
    CUT = [
        ("House Passes Bill \u2013 NaturalNews . com", "naturalnews.com", "House Passes Bill"),
        ("AI , open models , China | Homeland Security Newswire", "homelandsecuritynewswire.com",
         "AI, open models, China"),
        ("Anthropic raises round - Axios", "axios.com", "Anthropic raises round"),
    ]
    for raw, dom, want in KEEP:
        got = tidy_headline(raw, dom)
        assert got == want, f"tidy changed {raw!r} to {got!r}, wanted {want!r}"
        assert letters(got) == letters(raw), f"tidy lost letters from {raw!r}"
    for raw, dom, want in CUT:
        got = tidy_headline(raw, dom)
        assert got == want, f"tidy changed {raw!r} to {got!r}, wanted {want!r}"
        assert letters(raw).startswith(letters(got)), f"tidy did more than cut a tail from {raw!r}"
    for raw, dom, _ in KEEP + CUT:
        once = tidy_headline(raw, dom)
        assert tidy_headline(once, dom) == once, f"tidy is not idempotent on {raw!r}"
    print("headline tidying keeps every letter: ok")


def check_fears_config():
    """Every fear has to carry the same fields and patterns that compile.

    The tagger, the news matcher, GDELT and the Wikipedia collector each read a different key off
    these, and a fear missing one fails whichever collector wanted it, hours later and quietly.
    """
    from pipeline.common import config
    import re as _re
    fears = config("fears")
    keys = set(fears[0])
    assert keys >= {"slug", "name", "sentence", "short", "definition", "keywords", "gdelt",
                    "match", "wikipedia", "because"}, f"the first fear is missing fields: {keys}"
    seen = set()
    for f in fears:
        assert set(f) == keys, f"{f.get('slug')} has fields {set(f) ^ keys} the others do not"
        assert f["slug"] not in seen, f"two fears called {f['slug']}"
        seen.add(f["slug"])
        for field in ("name", "sentence", "short", "definition", "gdelt", "because"):
            assert isinstance(f[field], str) and f[field].strip(), f"{f['slug']} has an empty {field}"
        # "sentence" is the name dropped mid-sentence, after "cite", so it carries no heading
        # capital. Same allowlist as "because" below: only a proper noun keeps one.
        s = f["sentence"]
        # It has to be the same name, not a second one: an article in front is the only licence,
        # for the fears whose name will not take a bare "cite" in front of it.
        bare = s[4:] if s.lower().startswith("the ") else s
        assert bare.lower() == f["name"].lower(), f"{f['slug']}: sentence is not the name, it is {s!r}"
        assert s[0] == s[0].lower() or s.split()[0] in ("AI", "China", "OpenAI", "Congress"), \
            f"{f['slug']}: sentence starts with a capital that is not a proper noun"
        assert f["keywords"], f"{f['slug']} has no keywords"
        assert f["match"], f"{f['slug']} has no match patterns"
        for pat in f["match"]:
            _re.compile(pat)   # raises here rather than inside a collector
        # "because" is dropped in after "It cites the fear that", so it is a clause, not a sentence
        why = f["because"]
        assert not why.endswith("."), f"{f['slug']}: because ends in a full stop and one is added"
        assert not why.lower().startswith("that "), f"{f['slug']}: because repeats the 'that'"
        assert why[0] == why[0].lower() or why.split()[0] in ("AI", "China", "OpenAI", "Congress"), \
            f"{f['slug']}: because starts mid-sentence, so only a proper noun is capitalised"
    print(f"{len(fears)} fears, all well formed: ok")


def check_no_euphemism():
    """Nothing the site says in its own voice may use the vocabulary the report exists to undo.

    Every string here is one somebody wrote by hand: the control taxonomy, the fear names and the
    lines that follow "It cites the fear that", the brief's opener, the tagline, and the headings
    and captions in the templates. A measure's own title and anything quoted from one are not in
    this list and never will be, because the gap between how a power is sold and what it does is
    the exhibit, and editing a quote would destroy it.

    The same list gates the sentence and the card the model writes each morning, so the check on
    the copy and the check on the writing are one definition in one place.
    """
    import json as _json, re as _re
    from pipeline.common import EUPHEMISM
    found = []

    def scan(where, text):
        for m in EUPHEMISM.finditer(text or ""):
            found.append((where, m.group(0), (text or "")[:90]))

    for c in _json.loads((ROOT / "config" / "controls.json").read_text()):
        for k in ("name", "chip", "head", "pattern", "definition"):
            scan(f"controls/{c['slug']}/{k}", c.get(k))
    # A fear's definition is the one string here the reader never sees. It goes to the tagger, and
    # the tagger has to speak the sponsor's language to find the sponsor's bills: a fear about
    # children and chatbots is written in bills that say "minors' safety". Control definitions are
    # not exempt, because the site prints those on the row they label.
    for f in _json.loads((ROOT / "config" / "fears.json").read_text()):
        for k in ("name", "short", "because"):
            scan(f"fears/{f['slug']}/{k}", f.get(k))
    for name in ("brief", "site"):
        blob = _json.loads((ROOT / "config" / f"{name}.json").read_text())
        for k, v in (blob.items() if isinstance(blob, dict) else []):
            if isinstance(v, str):
                scan(f"{name}/{k}", v)
    for t in sorted((ROOT / "site" / "templates").rglob("*.html")):
        # Tags, expressions and Jinja comments all come out first. A {# #} comment reaches no
        # reader, and the note explaining why a word is banned has to be able to name the word.
        # It flagged its own explanation the first time, which is the check working and the strip
        # being one character short.
        text = _re.sub(r"\{[%{#].*?[#%}]\}", " ", t.read_text(), flags=_re.S)
        scan(f"template {t.name}", " ".join(_re.sub(r"<[^>]+>", " ", text).split()))

    # and the copy written straight into the code, which is neither config nor template: the tile
    # captions, the section labels, the words around every figure.
    import ast as _ast
    PROSE = _re.compile(r"^[A-Za-z][A-Za-z0-9 ,.:;'\u2019()%$-]{12,}$")
    SQLISH = _re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|GROUP BY|ORDER BY|JOIN|VALUES)\b")
    for src in ("pipeline/export.py", "pipeline/brief.py", "site/build.py", "site/brand.py"):
        path = ROOT / src
        for node in _ast.walk(_ast.parse(path.read_text())):
            if not isinstance(node, _ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if not PROSE.match(text) or SQLISH.search(text) or "\\" in text:
                continue
            scan(f"{path.name}:{node.lineno}", text)

    if found:
        lines = "\n".join(f"    {w}: {word!r} in {ctx}" for w, word, ctx in found)
        raise AssertionError(f"the site writes the sponsor's vocabulary in its own voice:\n{lines}")
    print("no euphemism in anything the site says itself: ok")


def check_fec_sweep():
    """The register sweep writes money into the database unattended, so it is exercised here.

    Four things have to hold. A committee on the entity list is left to the loop that owns it. One
    the sweep found before is read again, because its receipts keep changing after the day it was
    found. One it should never have taken is dropped. And a name that only matches inside the
    bracket the FEC adds to tell similar committees apart is not a match at all.
    """
    from pipeline import collect_fec as fec
    from pipeline.common import connect as _connect
    import tempfile as _tempfile
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "fec.db")
    for cid, name, ent in (("C1", "AI SAFETY PAC", None),
                           ("C2", "APPRAISAL INSTITUTE PAC (AI PAC)", None),
                           ("C3", "LEADING THE FUTURE", "leading-the-future")):
        db.execute("INSERT INTO committees(id,name,entity,query,receipts) VALUES(?,?,?,?,?)",
                   (cid, name, ent, "sweep:x" if ent is None else "by name", 1000))
    db.commit()
    register = [{"committee_id": "C1", "name": "AI SAFETY PAC", "committee_type": "O"},
                {"committee_id": "C4", "name": "HUMANITY ABOVE ARTIFICIAL INTELLIGENCE PAC",
                 "committee_type": "O"},
                {"committee_id": "C3", "name": "LEADING THE FUTURE", "committee_type": "O"},
                {"committee_id": "C5", "name": "AIR LINE PILOTS ASSOCIATION PAC", "committee_type": "N"}]

    class Http:
        asked = []

        def json(self, url, params=None, **kw):
            if url.endswith("/committees/"):
                return {"results": register if params["q"] == fec.SWEEP[0] else []}
            if "/totals/" in url:
                Http.asked.append(url.split("/committee/")[1].split("/")[0])
                return {"results": [{"receipts": 5000, "disbursements": 0,
                                     "independent_expenditures": 0}]}
            return {"results": []}

    fec.sweep(db, Http(), "key", None, 2026, [])
    left = {r["id"]: r for r in db.execute("SELECT id, name, entity, receipts FROM committees")}
    assert "C2" not in left, "the sweep kept a committee that only matches inside the bracket"
    assert "C5" not in left, "the sweep took a committee whose name is not about AI"
    assert "C4" in left, "the sweep missed a new committee"
    assert left["C1"]["receipts"] == 5000, "a committee the sweep found before was not read again"
    assert left["C3"]["receipts"] == 1000, "the sweep overwrote a committee the entity list owns"
    assert Http.asked == ["C1", "C4"], f"totals asked for the wrong set: {Http.asked}"
    print("the FEC register sweep: ok")


def check_post_needs_entries():
    """An edition is posted from the database, not from whatever file is lying about.

    The file on the data branch outlives the code that wrote it. A pass that clears the day to
    write it again and then cannot write it leaves the old file in place, and the poster reads
    files. Without this guard the morning post would carry a sentence the current code has
    already refused. So: rows for the day, or nothing goes out.
    """
    from pipeline import post
    from pipeline.common import connect as _connect, kv_get as _kv_get
    import tempfile as _tempfile
    room = pathlib.Path(_tempfile.mkdtemp())
    db = _connect(room / "post.db")
    day = "2026-09-20"
    briefs = room / "brief"
    briefs.mkdir()
    (briefs / f"{day}.json").write_text(json.dumps(
        {"edition": day, "headline": "stale", "text": "a sentence the code has since refused",
         "alt": "", "lines": []}))

    def refuse():
        raise AssertionError("the poster reached for credentials with nothing written for the day")

    kept, post.credentials = post.credentials, refuse
    try:
        assert post.run(db, str(briefs), day) is None, "a stale file was posted"
        db.execute("INSERT INTO brief(target, edition, sentence, evidence, office, controls, "
                   "written_at) VALUES(?,?,?,?,?,?,?)",
                   ("t1", day, "a sentence this code wrote", "", "", "", iso(dt.datetime.now())))
        db.commit()
        try:
            post.run(db, str(briefs), day)
        except AssertionError as e:
            if "reached for credentials" not in str(e):
                raise
        else:
            raise AssertionError("the guard held an edition the database does have")
    finally:
        post.credentials = kept
    # And when X refuses the post, the reason is written down where it can be read later
    room2 = pathlib.Path(_tempfile.mkdtemp())
    db2 = _connect(room2 / "post.db")
    briefs2 = room2 / "brief"
    briefs2.mkdir()
    (briefs2 / f"{day}.json").write_text(json.dumps(
        {"edition": day, "headline": "h", "text": "x" * 444, "alt": "", "lines": []}))
    db2.execute("INSERT INTO brief(target, edition, sentence, evidence, office, controls, "
                "written_at) VALUES(?,?,?,?,?,?,?)",
                ("t1", day, "s", "", "", "", iso(dt.datetime.now())))
    db2.commit()

    def refuse_post(*a, **k):
        raise RuntimeError("X refused the post: 403 Your account is not allowed to post this")

    kept_creds, kept_pub = post.credentials, post.publish
    post.credentials, post.publish = (lambda: {"key": "k", "secret": "s", "token": "t",
                                               "token_secret": "ts"}), refuse_post
    try:
        try:
            post.run(db2, str(briefs2), day)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a refusal from X was swallowed instead of failing the run")
    finally:
        post.credentials, post.publish = kept_creds, kept_pub
    noted = _kv_get(db2, f"post:refused:{day}")
    assert noted, "X refused the post and nothing was written down"
    assert noted["characters"] == 444, f"the length was not recorded: {noted}"
    assert "403" in noted["why"], f"the reason was not recorded: {noted}"
    print("the poster refuses a stale edition, and records a refusal: ok")


def check_failed_source_retries():
    """A daily source that errors is tried again today, not tomorrow.

    The daily pass marks itself done whatever happened inside it, so before this a source that
    threw at six in the morning sat red for twenty-four hours, and a fix pushed at noon did not
    run until the next morning either. Four things have to hold: a daily source that errored is
    asked for again; one that succeeded lets go of the key it was carrying; one that set its own
    retry during the run keeps it, because openstates asks for another turn when it reaches its
    allowance, which is not a failure; and an hourly source is left alone, since the next run is
    twenty minutes away.
    """
    from pipeline import run as runner
    from pipeline.common import connect as _connect, kv_get as _kv_get
    import tempfile as _tempfile
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "runs.db")
    for name, ok in (("fec", 0), ("lda", 1), ("openstates", 1), ("congress", 1), ("rss", 0)):
        db.execute("INSERT INTO status(source,last_run,ok,added,message) VALUES(?,?,?,?,?)",
                   (name, iso(dt.datetime.now()), ok, 0, ""))
    db.execute("INSERT INTO kv(key,value) VALUES('retry:lda', '\"2026-01-01T00:00:00\"')")
    db.execute("INSERT INTO kv(key,value) VALUES('retry:openstates', '\"2026-01-01T00:00:00\"')")
    db.commit()
    asked = {"fec": None, "lda": "2026-01-01T00:00:00", "congress": None, "rss": None,
             "openstates": "2026-01-01T00:00:00"}
    # openstates reached its allowance during the run and moved its own key on the way past
    db.execute("UPDATE kv SET value='\"2026-09-20T18:00:00\"' WHERE key='retry:openstates'")
    db.commit()
    runner.ask_again(db, list(asked), asked)
    assert _kv_get(db, "retry:fec"), "a daily source that errored was not asked for again"
    assert not _kv_get(db, "retry:lda"), "a daily source that succeeded kept a spent retry"
    assert _kv_get(db, "retry:openstates") == "2026-09-20T18:00:00", \
        "a source that asked for its own next turn had the request overwritten"
    assert not _kv_get(db, "retry:congress"), "a source that succeeded was asked for again"
    assert not _kv_get(db, "retry:rss"), "an hourly source was given a retry it does not need"
    print("a failed daily source is tried again today: ok")


def check_places_word():
    """An executive order is not a state, whatever its office is called.

    The plate says "in ten states" or "in ten jurisdictions" depending on what it counted. The
    test for that read the end of the place's name, so Education Department was federal and
    Executive Office of the President was a state. The jurisdiction code is the answer.
    """
    from pipeline import brief
    from pipeline.common import connect as _connect
    import tempfile as _tempfile
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "places.db")
    day = dt.date.today().isoformat()
    rows = [("m1", "ca", "California"), ("m2", "ny", "New York"), ("m3", "tx", "Texas")]
    for mid, code, name in rows:
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": code,
                                "jurisdiction_name": name, "session": "2025", "identifier": "SB 1",
                                "title": "t", "summary": "s", "status": "pending", "url": f"u{mid}",
                                "introduced_date": day, "source": "test", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", "c", "q", "t", iso()))
    db.commit()
    assert brief.places(db, "control", "c") == (3, "state"), "three states are three states"

    def add(mid, kind, code, name, ident):
        upsert(db, "measures", {"id": mid, "kind": kind, "jurisdiction": code, "jurisdiction_name": name,
                                "session": "", "identifier": ident, "title": f"t {mid}", "summary": "s",
                                "status": "passed", "url": f"u{mid}", "introduced_date": day, "source": "test",
                                "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", "c", "q", "t", iso()))
        db.commit()
    add("m4", "order", "us-exec", "Executive Office of the President", "EO 1")
    n, word = brief.places(db, "control", "c")
    assert (n, word) == (4, "jurisdiction"), \
        f"an executive order counted as a state: the plate would have said {n} {word}s"
    # The federal government is one place however many offices issued its measures. Counting names
    # made it one place per agency: Congress, the President and the FDA were three jurisdictions.
    add("m5", "rule", "us-exec", "Health and Human Services Department, Food and Drug Administration", "FR 1")
    add("m6", "bill", "us", "Congress", "H.R. 1")
    n, word = brief.places(db, "control", "c")
    assert (n, word) == (4, "jurisdiction"), f"the federal government counted as {n - 3} places"
    print("an executive order is not a state, and the federal government is one place: ok")

    # Puerto Rico is not a state, on the plate or on the page
    db2 = _connect(pathlib.Path(_tempfile.mkdtemp()) / "places2.db")
    for mid, code, name in [("p1", "ca", "California"), ("p2", "pr", "Puerto Rico")]:
        upsert(db2, "measures", {"id": mid, "kind": "bill", "jurisdiction": code, "jurisdiction_name": name,
                                 "session": "2025", "identifier": "SB 1", "title": "t", "summary": "s",
                                 "status": "pending", "url": f"u{mid}", "introduced_date": day, "source": "test",
                                 "first_seen": iso()})
        db2.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        db2.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", "c", "q", "t", iso()))
    db2.commit()
    assert brief.places(db2, "control", "c") == (2, "jurisdiction"), "Puerto Rico was called a state"
    from pipeline.export import where_phrase
    assert where_phrase({"ca", "ny", "pr", "us", "us-exec"}) == "2 states, Puerto Rico and the federal government"
    assert where_phrase({"ca", "us"}) == "1 state and Congress"
    assert where_phrase({"us-exec"}) == "the federal government"
    print("Puerto Rico is named, not counted as a state: ok")


def check_headline_keeps_the_power():
    """A headline that drops the body reads the measure backwards.

    The morning of 20 September the plate said "California would exempt data center projects from
    environmental review". The sentence it came from said the projects go under the Governor's
    authority to certify them as exempt. One office deciding who is exempt is a power; the plate
    said a rule had gone away, which is the opposite of what this report is for. Everything else
    guarding the headline stops it saying something untrue. This stops it saying the true thing
    backwards.
    """
    from pipeline import brief
    real = brief.call
    lead = {"sentence": "California would put data center projects under the Governor's authority "
                        "to certify them as environmental leadership development projects exempt "
                        "from standard environmental review.",
            "office": "California Office of Land Use and Climate Innovation",
            "jurisdiction": "California", "controls": "data-center-limits", "head": ""}
    names = {c["slug"]: c for c in config("controls")}
    assert brief.power_holder(lead["sentence"]) == {"governor"}, brief.power_holder(lead["sentence"])
    try:
        said = []

        def two_tries(key, system, prompt, max_tokens=0):
            said.append(prompt)
            return {"line": ["California would exempt data center projects from environmental review",
                             "California would let the Governor exempt data center projects"][len(said) - 1]}

        brief.call = two_tries
        got = brief.headline("k", [lead], names, {})
        assert "governor" in got.lower(), f"the plate dropped the body again: {got}"
        assert len(said) == 2, "the headline was not asked again after it dropped the body"

        # and when it will not keep the body, the plate falls back to the sentence's own words
        def never(key, system, prompt, max_tokens=0):
            return {"line": "California would exempt data center projects from review"}

        brief.call = never
        got = brief.headline("k", [lead], names, {})
        assert got == "California would put data center projects under the Governor's authority", got

        # a sentence that names no body is asked once, as before
        plainly = dict(lead, sentence="Texas would require an AI company to label synthetic media.",
                       jurisdiction="Texas", controls="labeling-mandates")
        asked = []

        def once(key, system, prompt, max_tokens=0):
            asked.append(1)
            return {"line": "Texas would require labels on synthetic media"}

        brief.call = once
        assert brief.headline("k", [plainly], names, {}) == "Texas would require labels on synthetic media"
        assert len(asked) == 1, "a sentence with no body was asked twice"
    finally:
        brief.call = real
    print("the plate keeps who holds the power: ok")


def check_label_rules():
    """The quote check refuses what a reader would refuse.

    A recital of the law already in force, a legislature named as an agency, a state bill carrying
    "state laws overridden by Washington", an agency's report to its own legislature called a
    company's duty to report, and a quote that only matches half a word.
    """
    from pipeline.tag import agreed, norm
    raw = ("Existing law requires the Department of Technology to conduct an inventory of automated decision "
           "systems. This bill would require a developer to register a frontier model with the Attorney General "
           "and would require the Department of Technology to submit a report to the Legislature. It would "
           "prohibit the use of drones and aircraft for surveillance, and would preempt any ordinance adopted "
           "by a city or county on the same subject.")
    body = norm(raw)

    def ok(kind, label, quote, code="ca", jur="California"):
        return bool(agreed({kind: {label: quote}}, body, kind, jur, raw=raw, code=code))
    assert not ok("controls", "mandatory-reporting", "requires the Department of Technology to conduct an inventory"), \
        "a label rested on the law already in force"
    assert ok("controls", "license-to-build", "require a developer to register a frontier model")
    assert not ok("controls", "mandatory-reporting", "require the Department of Technology to submit a report to the Legislature"), \
        "an agency reporting to its legislature was counted as a company's duty to report"
    assert not ok("controls", "preemption", "would preempt any ordinance adopted by a city or county"), \
        "a state bill carried the control named for Washington overriding the states"
    assert ok("controls", "preemption", "would preempt any ordinance adopted by a city or county", code="us")
    assert not ok("controls", "labeling-mandates", "prohibit the use of drones and ai"), "half a word matched"
    assert not ok("agencies", "Kansas Legislature (via task force)", "the Attorney General"), "a legislature counted as an agency"
    assert ok("agencies", "California Attorney General", "the Attorney General")
    print("the quote check refuses recitals, legislatures, half words and misplaced controls: ok")


def check_third_reading():
    """A label that passed the quote check can still say the opposite of what the measure does.

    The third reading asks one question per control and office label. A no takes the label off and
    is remembered against its quote; a label not yet checked cannot lead the post; and a key that
    cannot use the stronger model falls back to the next one instead of checking nothing.
    """
    from pipeline import brief, check
    from pipeline.common import connect as _connect
    import tempfile as _tempfile
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "check.db")
    day = dt.date.today().isoformat()
    for mid, title, controls, office in (
            ("c1", "Sandbox for AI testing under waived rules", ["license-to-build"], "Federal Reserve Board"),
            ("c2", "Frontier model licensing", ["license-to-build"], "State AI Licensing Office")):
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": "ca", "jurisdiction_name": "California",
                                "session": "2025", "identifier": f"SB {mid}", "title": title, "summary": title,
                                "status": "pending", "url": f"u{mid}", "introduced_date": day,
                                "latest_action_date": day, "source": "test", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        for c in controls:
            db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", c, title, "t", iso()))
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "agency", office, title, "t", iso()))
    db.commit()
    assert not brief.pool(db, day), "a measure led the post on labels nobody had checked"
    asked = []

    def fake(key, prompt):
        asked.append(prompt)
        no = "waived rules" in prompt
        return {"verdict": "no" if no else "yes", "reason": "a sandbox" if no else "it licenses"}, "fake-model"
    line = check.run(db, "k", ask_fn=fake)
    assert len(asked) == 4, f"expected four questions, asked {len(asked)}"
    left = {(r["target"], r["kind"]) for r in db.execute("SELECT target, kind FROM tags")}
    assert ("c1", "control") not in left and ("c1", "agency") not in left, "a refused label stayed on"
    assert ("c2", "control") in left and ("c2", "agency") in left, "a confirmed label was taken off"
    lead = brief.pool(db, day)
    assert [r["id"] for r in lead] == ["c2"] and lead[0]["office"] == "State AI Licensing Office", \
        "only the confirmed measure, with its confirmed office, should be able to lead"
    # Read again and landing on the same quote, the refused label goes without a second question
    db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("c1", "control", "license-to-build",
                                                        "Sandbox for AI testing under waived rules", "t", iso()))
    db.commit()
    asked.clear()
    check.run(db, "k", ask_fn=fake)
    assert not asked and not db.execute("SELECT 1 FROM tags WHERE target='c1' AND kind='control'").fetchone(), \
        "a label refused on this quote came back"
    assert "took off" in line

    # A key that cannot use the stronger model gets the next one, not no check at all
    class Reply:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

        def json(self):
            return {"content": [{"type": "text", "text": self.text}]}
    calls = []

    def post(url, **kw):
        calls.append(kw["json"]["model"])
        if kw["json"]["model"] == check.MODELS[0]:
            return Reply(404, '{"type":"error","error":{"type":"not_found_error","message":"model: x"}}')
        return Reply(200, '{"verdict": "yes", "reason": "fine"}')
    real, check.requests.post = check.requests.post, post
    check._model["i"] = 0
    try:
        answer, model = check.ask("k", "question")
    finally:
        check.requests.post, check._model["i"] = real, 0
    assert answer["verdict"] == "yes" and model == check.MODELS[1] and calls == check.MODELS[:2], calls
    print("a third reading takes off what the measure does not do, and only checked labels lead: ok")

    # A run that refuses far more than the hand check ever found holds its refusals for a person
    db2 = _connect(pathlib.Path(_tempfile.mkdtemp()) / "hold.db")
    for i in range(70):
        mid = f"h{i}"
        upsert(db2, "measures", {"id": mid, "kind": "bill", "jurisdiction": "ny", "jurisdiction_name": "New York",
                                 "session": "2025", "identifier": f"A {i}", "title": f"AI bill {i}", "summary": "s",
                                 "status": "pending", "url": f"h{mid}", "introduced_date": day, "source": "test",
                                 "first_seen": iso()})
        db2.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        db2.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", "labeling-mandates", f"q{i}", "t", iso()))
    db2.commit()
    check.run(db2, "k", ask_fn=lambda key, prompt: ({"verdict": "no", "reason": "misread"}, "fake-model"))
    assert db2.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 70, "a wave of refusals took the labels down"
    assert db2.execute("SELECT COUNT(*) FROM checks WHERE verdict='hold'").fetchone()[0] == 70
    from pipeline import review
    body, items = review.build(db2, dt.date.today())
    assert "Held: 70 refusals" in body and items >= 70, "the held refusals are not on the weekly list"
    db2.execute("DELETE FROM kv WHERE key LIKE 'check:released:%'")
    assert check.release_holds(db2) == 70 and not db2.execute("SELECT COUNT(*) FROM tags").fetchone()[0], \
        "released refusals did not come off"
    assert check.release_holds(db2) == 0, "a release ran twice"
    print("a wave of refusals is held for a person, listed, and applied once released: ok")

    # An executive order the Register marks as revoked comes off the record
    from pipeline import collect_fedreg

    class FakeHttp:
        def json(self, url, params=None, tries=5):
            return {"executive_order_notes": "See: EO 14028\r\nRevoked by: EO 14318, July 23, 2025"} \
                if "2025-01395" in url else {"executive_order_notes": "Revokes: EO 14141, January 14, 2025"}
    for mid, ident in (("fr-2025-01395", "Executive Order 14141"), ("fr-2025-14212", "Executive Order 14318")):
        upsert(db2, "measures", {"id": mid, "kind": "order", "jurisdiction": "us-exec", "jurisdiction_name": "EOP",
                                 "session": "", "identifier": ident, "title": "AI", "summary": "", "status": "passed",
                                 "url": f"u{mid}", "introduced_date": day, "source": "test", "first_seen": iso()})
    db2.commit()
    gone = collect_fedreg.revoked_orders(db2, FakeHttp())
    left = {r["id"] for r in db2.execute("SELECT id FROM measures WHERE kind='order'")}
    assert gone == ["Executive Order 14141"] and left == {"fr-2025-14212"}, (gone, left)
    print("a revoked executive order comes off, and the order revoking it stays: ok")


def check_lobbying_money():
    """A year of lobbying is four quarters of reports, and a client's money is counted once.

    The window took in the quarter still running, which has a handful of early filings, so "past
    year" was three quarters and a stub. And a client lobbying in-house reports what it paid its
    outside firms among its own expenses, while each firm reports the same money as income: summing
    every filing counted it twice.
    """
    from pipeline.export import recent_quarters, reported
    assert sorted(recent_quarters(4, dt.date(2026, 9, 21))) == [(2025, 3), (2025, 4), (2026, 1), (2026, 2)], \
        "the lobbying year includes the quarter still running"
    assert (2026, 3) not in recent_quarters(4, dt.date(2026, 10, 5)), "a quarter counted before its reports are due"
    assert (2026, 3) in recent_quarters(4, dt.date(2026, 10, 21)), "a quarter left out after its reports were due"

    def filing(client, own, amount, quarter="Q1"):
        return {"client_key": client, "year": 2026, "quarter": quarter, "amount": amount, "self": own}
    rows = [filing("msft", True, 2_300_000), filing("msft", False, 90_000), filing("msft", False, 60_000),
            filing("ari", False, 50_000), filing("msft", False, 70_000, "Q2")]
    assert reported(rows) == 2_300_000 + 50_000 + 70_000, \
        f"outside firms' fees were added on top of the client's own report: {reported(rows):,}"
    print("a lobbying year is four quarters of reports, counted once: ok")


def check_lobbying_keywords():
    """A lobbying filing names a fear, or it does not. Runs of letters are not names.

    Matching a keyword anywhere in the text found "agi" inside imaging, managing and agile, which
    is where 168 of the 197 filings tagged with it came from; "labor" inside laboratory and
    collaboration; "teen" inside fifteen; "minor" inside minority. Separately, the words
    themselves were too broad: 2,265 of the 2,443 filings tagged with the fear of AI cyberattacks
    rested on the bare string "cyber", which is how a filing reading "Cloud technology;
    Cybersecurity; Encryption policy" came to name a fear of AI attacks.
    """
    from pipeline.common import fear_keywords, fears_mentioned
    kw = fear_keywords()
    for text, fear, want in (
            ("medical imaging and managing engagement", "loss-of-control", False),
            ("laboratory collaboration agreements", "job-loss", False),
            ("fifteen reports on minority business", "kids-chatbots", False),
            ("Cloud technology; Cybersecurity; Encryption policy", "ai-cyberattacks", False),
            ("FY 2026 Labor/HHS Appropriations", "job-loss", False),
            ("pandemic preparedness and drug pricing", "bioweapons", False),
            ("AGI and superintelligence risk", "loss-of-control", True),
            ("discriminatory automated decisions", "bias", True),
            ("ransomware and hacking of AI systems", "ai-cyberattacks", True),
            ("reskilling for the future of work", "job-loss", True),
            ("companion chatbots and child safety", "kids-chatbots", True)):
        got = fear in fears_mentioned(text, kw)
        assert got == want, \
            f"{text!r} {'should' if want else 'should not'} name {fear}"
    # the words the evidence refused, kept out by name so they are not quietly restored
    refused = {"cyber", "labor", "jobs", "worker", "workforce", "national security", "dominance",
               "pandemic", "algorithmic", "high-risk", "civil rights", "child", "children",
               "youth", "minor", "critical infrastructure"}
    for f in config("fears"):
        bare = {k.lower().rstrip("*") for k in f["keywords"]}
        clash = bare & refused
        assert not clash, f"{f['slug']} took back a word the filings showed to be generic: {clash}"
    print("lobbying keywords name a fear rather than matching letters: ok")


def check_position_carries_no_control():
    """A measure that opposes a thing was being counted among the measures that do it.

    Kansas HR 6023 is titled "Opposing the federal preemption of state laws that regulate
    artificial intelligence" and carried the preemption control. So did a Pennsylvania resolution
    urging Congress to drop federal legislation. Two of the five measures the site said would
    override state AI law were measures against doing that. A title that asks, urges or objects
    imposes nothing, so it carries no control; it can still name the fear it is worried about.
    """
    from pipeline.common import connect as _connect, states_a_position
    import tempfile as _tempfile
    for title, want in (
            ("Opposing the federal preemption of state laws that regulate artificial intelligence.", True),
            ("A Resolution urging the United States Congress to suspend any and all efforts", True),
            ("REQUESTING THE HAWAII STATE COMMISSION TO ESTABLISH A WORKING GROUP", True),
            ("A resolution condemning and calling for the reversal of the decision", True),
            ("Artificial intelligence: auditors: registration.", False),
            ("Data Center Moratorium", False),
            ("Companion chatbots.", False)):
        assert states_a_position(title) is want, f"{title[:50]!r} judged wrong"

    room = pathlib.Path(_tempfile.mkdtemp())
    db = _connect(room / "pos.db")
    day = iso()[:10]
    for mid, title in (("m-against", "Opposing the federal preemption of state AI laws."),
                       ("m-acts", "Artificial intelligence: auditors: registration.")):
        upsert(db, "measures", {"id": mid, "kind": "resolution" if "against" in mid else "bill",
                                "jurisdiction": "ks", "jurisdiction_name": "Kansas", "session": "2025",
                                "identifier": "HR 1", "title": title, "summary": "s",
                                "status": "pending", "url": f"u{mid}", "introduced_date": day,
                                "source": "test", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        for kind, value in (("control", "preemption"), ("fear", "china-race")):
            db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, kind, value, "q", "t", iso()))
    db.commit()
    from pipeline.common import drop_position_controls
    db.execute("DELETE FROM kv WHERE key LIKE 'repair:position-controls%'")
    db.commit()
    assert drop_position_controls(db) == 1, "the repair did not find the measure that takes a position"
    left = {(r["target"], r["kind"]) for r in db.execute("SELECT target, kind FROM tags")}
    assert ("m-against", "control") not in left, "a measure that opposes a thing still carries it"
    assert ("m-against", "fear") in left, "the fear it names was thrown out with the control"
    assert ("m-acts", "control") in left, "a measure that actually does something lost its control"
    assert drop_position_controls(db) == 0, "the repair ran twice"
    print("a measure that only takes a position carries no control: ok")


def check_overdue_daily_source():
    """A daily source that has not succeeded today is due, flag or no flag.

    The pass sets one done flag for itself whatever happened inside it. On 20 September the FEC
    register threw a 404 at 07:21, the flag went up anyway, and the fix pushed at noon could not
    run until the next morning: twenty-two hours with the whole election register missing and the
    site saying so in red. Success is recorded per source, so that is what decides.
    """
    from pipeline import run as runner
    from pipeline.common import connect as _connect, kv_set as _kv_set
    import tempfile as _tempfile
    import datetime as _dt
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "due.db")
    today = _dt.date.today().isoformat()
    for name, last_ok in (("congress", today + "T07:17:09+00:00"),
                          ("openstates", today + "T07:20:50+00:00"),
                          ("lda", today + "T07:21:18+00:00"),
                          ("wikipedia", today + "T07:21:46+00:00"),
                          ("fec", "2000-01-01T00:00:00+00:00")):
        db.execute("INSERT INTO status(source,last_run,ok,added,message,last_ok) "
                   "VALUES(?,?,?,?,?,?)", (name, iso(), 1, 0, "", last_ok))
    db.commit()
    assert runner.overdue(db, today) == ["fec"], runner.overdue(db, today)
    # one that keeps failing is held off until the hour it asked for, rather than every pass
    _kv_set(db, "retry:fec", (_dt.datetime.now(_dt.timezone.utc)
                              + _dt.timedelta(hours=3)).isoformat(timespec="seconds"))
    db.commit()
    assert runner.overdue(db, today) == [], "a source that asked to be left alone was run anyway"
    _kv_set(db, "retry:fec", "2000-01-01T00:00:00")
    db.commit()
    assert runner.overdue(db, today) == ["fec"], "the hour it asked for came and it was not run"
    print("a daily source that has not succeeded today is due: ok")


def check_mark_geometry():
    """The mark is a square with the letters centred in it, and the square sits on the word.

    The box was 354 by 315 on the avatar, a third more air at the sides than above, which is what
    made a mark that measured centred look uncentred. The avatar also carried its own copy of the
    geometry, so it could drift from the wordmark without anything noticing. Drawn here on a plain
    field and measured off the pixels.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    from PIL import Image, ImageDraw
    import brand

    def box_of(im, *colours):
        a = im.load()
        w, h = im.size
        xs, ys = [], []
        for y in range(h):
            for x in range(w):
                if any(sum(abs(a[x, y][i] - c[i]) for i in range(3)) < 90 for c in colours):
                    xs.append(x); ys.append(y)
        return (min(xs), min(ys), max(xs), max(ys)) if xs else None

    PAPER, VERM = (241, 237, 227), (179, 50, 31)
    im = Image.new("RGB", (520, 400), (0, 0, 0))
    d = ImageDraw.Draw(im)
    bw, bh = brand.badge(d, 60, 60, 200, fg=VERM, bg=PAPER)
    assert bw == bh, f"badge() returned {bw} by {bh}, which is not a square"
    x0, y0, x1, y1 = box_of(im, PAPER, VERM)
    side_w, side_h = x1 - x0 + 1, y1 - y0 + 1
    assert abs(side_w - side_h) <= 1, f"the drawn square is {side_w} by {side_h}"
    letters = box_of(im.crop((x0, y0, x1 + 1, y1 + 1)), VERM)
    lx0, ly0, lx1, ly1 = letters
    off_h = (lx0 - (side_w - 1 - lx1)) / 2
    off_v = (ly0 - (side_h - 1 - ly1)) / 2
    # Up and to the left by about a sixtieth of the box: the deliberate optical offset, no more.
    assert -0.06 * side_w < off_h <= 0.5, f"the letters sit {off_h:+.1f}px off across the square"
    assert -0.06 * side_h < off_v <= 0.5, f"the letters sit {off_v:+.1f}px off down the square"

    # and the square is centred on the cap height of FEAR REPORT, not on its line box
    im2 = Image.new("RGB", (1400, 400), (0, 0, 0))
    d2 = ImageDraw.Draw(im2)
    brand.wordmark(d2, 60, 100, 160, fill=PAPER, back=VERM)
    sq = box_of(im2.crop((0, 0, 400, 400)), PAPER, VERM)
    word = box_of(im2.crop((sq[2] + 8, 0, 1400, 400)), PAPER)
    drift = ((sq[1] + sq[3]) / 2) - ((word[1] + word[3]) / 2)
    assert abs(drift) <= 1.5, f"the square sits {drift:+.1f}px off the middle of FEAR REPORT"
    print("the mark is a square, centred on its letters and on the word: ok")


ALLOWED_HOSTS = {"gc.zgo.at"}   # the counter, and nothing else


def check_every_page_asks(dist, db, day):
    """The brief and the pitch-in are on every page, in that order, and the card is real.

    They used to live in the front page's own template, so a reader who arrived on a fear page
    from a link reached the end of the report with nothing to do. Moved into base.html, which is
    easy to undo by accident, hence this.

    The card is checked as a path, because pass.sh copies state/brief into dist/brief after the
    build: an <img> naming an edition that was never written is a broken picture on every page at
    once, not one. And nothing on any page may load from another host. The obvious way to show an
    X account is X's own timeline widget, which is a third-party script on every page, a set of
    X's cookies for every reader, and a box that does not match this site in either theme.
    """
    pages = sorted(dist.rglob("*.html"))
    assert len(pages) >= 5, f"only {len(pages)} pages built"
    for p in pages:
        t, where = p.read_text(), p.relative_to(dist).as_posix()
        assert 'id="brief"' in t, f"{where} has no daily brief section"
        assert 'id="keep"' in t, f"{where} has no pitch-in section"
        assert t.index('id="brief"') < t.index('id="keep"'), \
            f"{where} puts the pitch-in above the brief"
        assert 'class="brief-card"' in t, f"{where} names an edition but shows no card"
        assert f"/brief/{day}.png" in t, f"{where} points at the wrong edition"
        for m in re.finditer(r'<(?:img|script|iframe)[^>]+src="(?:https?:)?//([^"/]+)', t):
            # One exception, named rather than assumed: the counter, which sets no cookie and
            # follows nobody between sites. Anything else arriving here is a new third party
            # watching every reader of a site about who is watching, and has to be argued for.
            assert m.group(1) in ALLOWED_HOSTS, \
                f"{where} loads from {m.group(1)}; this site ships self-contained"
    # The pitch-in carries money, so three more things about it are held here rather than hoped.
    site_cfg = json.loads((ROOT / "config" / "site.json").read_text())
    addr = site_cfg.get("donate_btc", "")
    home = (dist / "index.html").read_text()
    if site_cfg.get("donate_url"):
        # On the button, not anywhere: the footer links to the same checkout, and a check for the
        # address on the page passed with the button pointing nowhere.
        assert re.search(r'<a class="pay-go" href="' + re.escape(site_cfg["donate_url"]) + '"', home), \
            "the card button does not go to the checkout"
    if addr:
        assert f'href="bitcoin:{addr}"' in home, "the Bitcoin address is not a wallet link"
        assert f'data-copy="{addr}"' in home, "the copy button copies something other than the address"
    # The copy button said Copied on success, on failure, and where no clipboard exists at all.
    # For an address that sends the reader to paste whatever was there before. It may only say
    # so on the path where the write resolved.
    js = (ROOT / "site" / "templates" / "base.html").read_text()
    assert "then(done, done)" not in js and "else done()" not in js, \
        "the copy button claims success when the copy failed"
    assert 'then(function () { say("Copied"); }, failed)' in js, \
        "the copy button no longer separates success from failure"

    # Three states, because the card comes from two places. With the key, the headline is known.
    # Without it, the table still knows which days were published, which is every edition written
    # before the key existed. With neither, the section has to render without a card rather than
    # point at a picture that was never made.
    db.execute("DELETE FROM kv WHERE key='brief:latest'")
    db.execute("INSERT OR REPLACE INTO brief(target, edition, sentence, evidence, office, "
               "controls, written_at) VALUES('t', '2026-09-19', 's', 'q', '', '', '')")
    db.commit()
    fell_back = export.brief_latest(db)
    assert fell_back and fell_back["date"] == "2026-09-19", \
        f"the table should still name an edition: {fell_back}"
    assert fell_back["alt"].strip(), "a card with no headline still needs alt text"
    db.execute("DELETE FROM brief")
    db.commit()
    assert export.brief_latest(db) is None, "a database with no edition at all must report no card"
    print(f"the brief and the pitch-in are on all {len(pages)} pages, brief first: ok")


def check_icon_centred():
    """The tab icon is one mark, centred, at every size a browser asks for.

    What shipped was the avatar shrunk: a red field, a cream square inset in it, then the letters
    inside that. Three shapes inside sixteen pixels, and the letters landed 1.5px right and 1px
    low of the cream square, which is 7.5% of a 20px box and plainly wrong in a tab. The head
    also carried an inline SVG drawn in Arial Narrow, so the icon changed face depending on which
    file the browser picked. One renderer, one face, measured off the pixels at each size.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    from PIL import Image
    import brand

    VERM, PAPER = (179, 50, 31), (241, 237, 227)
    with tempfile.TemporaryDirectory() as tmp:
        for px in (16, 32, 180, 512):
            path = pathlib.Path(tmp) / f"{px}.png"
            brand.icon(path, size=px)
            im = Image.open(path).convert("RGB")
            assert im.size == (px, px), f"{px}: {im.size}"
            a = im.load()
            xs, ys = [], []
            for y in range(px):
                for x in range(px):
                    if sum(abs(a[x, y][i] - VERM[i]) for i in range(3)) > 80:
                        xs.append(x); ys.append(y)
            assert xs, f"{px}: nothing drawn on the field"
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            # The field is solid to the edge: no square inset inside another square.
            assert sum(abs(a[0, 0][i] - VERM[i]) for i in range(3)) < 30, \
                f"{px}: the corner is not the field colour, so something is inset"
            assert x0 > 0 and y0 > 0 and x1 < px - 1 and y1 < px - 1, \
                f"{px}: the letters touch the edge"
            off_h = (x0 - (px - 1 - x1)) / 2
            off_v = (y0 - (px - 1 - y1)) / 2
            # Never more than a pixel out, and what slack there is goes up and to the left,
            # which is the same optical offset the badge uses. The upper bound is 0, not 0.5:
            # at 16 pixels the only choices are half a pixel high or half a pixel low, and a
            # bound of 0.5 accepts both, which is how the first version of this check passed
            # over the very arithmetic it was written for.
            assert -0.02 * px - 1 <= off_h <= 0, f"{px}: letters {off_h:+.1f}px off across"
            assert -0.02 * px - 1 <= off_v <= 0, f"{px}: letters {off_v:+.1f}px off down"
            cap = (y1 - y0 + 1) / px
            assert 0.5 <= cap <= 0.68, f"{px}: the letters are {cap:.0%} of the frame"
    # and the head must not offer a second icon drawn by some other means
    head = (ROOT / "site" / "templates" / "base.html").read_text()
    assert "data:image/svg+xml" not in head, "the head still carries an inline SVG icon"
    for px in (16, 32):
        assert f"/icon/{px}.png" in head, f"the head does not offer the {px}px icon"
    print("the tab icon is one centred mark at every size: ok")


def check_share_card_fits():
    """The card gives way rather than running through its own rules.

    A two-line name with a two-line line of counts under it overflowed by sixty pixels: the top
    of the figure was cut off against the rule under the mark and the counts ran through the rule
    above the address. The figure steps down until the block fits.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    from PIL import Image
    import tempfile as _tempfile
    import build as sitebuild
    room = pathlib.Path(_tempfile.mkdtemp())
    cases = {
        "short": ("3", "AI therapists", "3 of 100"),
        "long": ("1,420", "Bills, rules and orders about artificial intelligence since January 2025",
                 "1,420 measures tracked across 51 jurisdictions, 513 of them carrying at least "
                 "one new government control held by 180 named offices"),
    }
    for name, (big, label, sub) in cases.items():
        path = room / f"{name}.png"
        sitebuild.share_card(path, big, label, sub)
        im = Image.open(path).convert("RGB")
        px = im.load()
        w, h = im.size
        assert (w, h) == (1200, 630), f"{name}: card is {w} by {h}"

        def inked(y):
            return sum(1 for x in range(8, w - 8)
                       if sum(abs(px[x, y][i] - c) for i, c in enumerate((241, 237, 227))) > 90)

        # The content lives between the rule under the mark and the rule above the address. Find
        # where it actually reaches, rather than probing rows and hoping the overflow lands on one.
        band, foot = 118, 556
        rows = [y for y in range(band + 3, foot - 2) if inked(y) > 3]
        assert rows, f"{name}: the card came out empty between its rules"
        assert rows[0] >= band + 18, \
            f"{name}: content reaches y={rows[0]}, hard against the rule under the mark at {band}"
        assert rows[-1] <= foot - 10, \
            f"{name}: content reaches y={rows[-1]}, hard against the rule above the address at {foot}"
    print("a share card gives way rather than running through its rules: ok")


def main():
    check_brief_prompt()
    check_plate_fits()
    check_mark_geometry()
    check_icon_centred()
    check_share_card_fits()
    check_headline_tidy()
    check_fears_config()
    check_no_euphemism()
    check_fec_sweep()
    check_post_needs_entries()
    check_failed_source_retries()
    check_overdue_daily_source()
    check_places_word()
    check_headline_keeps_the_power()
    check_label_rules()
    check_third_reading()
    check_lobbying_money()
    check_lobbying_keywords()
    check_position_carries_no_control()
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = connect(tmp / "index.db")
    today = dt.date.today()
    for i in range(40):
        day = (today - dt.timedelta(days=i * 9)).isoformat()
        mid = f"os-test{i}"
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": ["ca", "ny", "tx", "us"][i % 4],
                                "jurisdiction_name": ["California", "New York", "Texas", "Congress"][i % 4],
                                "session": "2025", "identifier": f"SB {100 + i}" if i % 4 != 3 else f"H.R. {1000 + i}",
                                "title": f"Sample AI safety bill {i}", "summary": "A sample summary.",
                                "status": ["pending", "passed"][i % 2], "latest_action": "Referred to committee",
                                "latest_action_date": day, "introduced_date": day, "url": "https://example.com",
                                "sponsors": "Sample", "source": "test", "updated": day, "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        for kind, value in [("fear", ["loss-of-control", "deepfakes", "kids-chatbots", "china-race"][i % 4]),
                            ("control", ["mandatory-reporting", "new-agency-powers", "labeling-mandates"][i % 3]),
                            # The third spelling is the second office again. Real tags arrive
                            # worded however the bill worded them, and a count of distinct
                            # strings reports one office as two.
                            ("agency", ["California Attorney General", "Federal Trade Commission",
                                        "the FEDERAL  TRADE COMMISSION"][i % 3])]:
            db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, kind, value, "quote", "test", iso()))
    # A bill carried into the next session. Open States files it again with a new id, and it was
    # counted twice on the page and on the plate. It carries an office of its own, so a count that
    # reads both copies also shows up in the offices.
    carried = dict(db.execute("SELECT * FROM measures WHERE id='os-test0'").fetchone())
    carried.update({"id": "os-test0-carried", "session": "2026", "latest_action_date": today.isoformat()})
    upsert(db, "measures", carried)
    db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", ("os-test0-carried", "h", 1, iso()))
    for kind, value in (("fear", "loss-of-control"), ("control", "mandatory-reporting"),
                        ("agency", "California Attorney General")):
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("os-test0-carried", kind, value, "quote", "test", iso()))
    # A measure the front page does not show: read, judged not about AI, and tagged all the same.
    # Anything counting over the whole tags table rather than the page's own set picks it up.
    upsert(db, "measures", {"id": "os-offpage", "kind": "bill", "jurisdiction": "ca",
                            "jurisdiction_name": "California", "session": "2025", "identifier": "SB 999",
                            "title": "A bill about something else", "summary": "Not about AI.",
                            "status": "pending", "latest_action": "Referred", "latest_action_date": iso()[:10],
                            "introduced_date": iso()[:10], "url": "https://example.com/offpage",
                            "sponsors": "Sample", "source": "test", "updated": iso()[:10], "first_seen": iso()})
    db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", ("os-offpage", "h", 0, iso()))
    for kind, value in (("control", "new-agency-powers"), ("agency", "Nevada Gaming Control Board")):
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("os-offpage", kind, value, "quote", "test", iso()))
    clients = ["OPENAI OPCO, LLC", "META PLATFORMS, INC.", "BUSINESS ROUNDTABLE",
               "CENTER FOR AI SAFETY ACTION FUND", "AMERICANS FOR RESPONSIBLE INNOVATION"]
    for i in range(30):
        y, q = today.year - (i // 12), (i % 4) + 1
        upsert(db, "lobbying", {"id": f"l{i}", "client": clients[i % len(clients)],
                                "client_key": name_key(clients[i % len(clients)]),
                                "registrant": "Firm", "year": y, "period": "q", "quarter": f"Q{q}", "amount": 50000 + i * 1000,
                                "issues": "Artificial intelligence safety, deepfakes, children online, H.R. 1003",
                                "bill_refs": "H.R. 1003", "gov_entities": "SENATE", "url": "https://example.com",
                                "posted": (today - dt.timedelta(days=i)).isoformat() + "T10:00:00", "filing_type": "Q",
                                "first_seen": iso()})
    upsert(db, "committees", {"id": "C001", "name": "LEADING THE FUTURE", "entity": "leading-the-future", "query": "x",
                              "committee_type": "Super PAC", "receipts": 1e8, "disbursements": 2e7,
                              "independent_expenditures": 2.4e7, "cycle": 2026, "updated": iso()})
    upsert(db, "fec", {"id": "a1", "kind": "receipt", "committee_id": "C001", "committee_name": "LEADING THE FUTURE",
                       "counterparty": "ANDREESSEN HOROWITZ", "counterparty_key": name_key("ANDREESSEN HOROWITZ"),
                       "amount": 2.5e7, "date": today.isoformat(), "description": "", "support_oppose": None,
                       "candidate": None, "url": "https://example.com", "first_seen": iso()})
    for i in range(700):
        day = (today - dt.timedelta(days=i)).isoformat()
        db.execute("INSERT OR REPLACE INTO series VALUES(?,?,?)", ("wiki:loss-of-control", day, 1000 + i))
        if i < 60:
            db.execute("INSERT OR REPLACE INTO series VALUES(?,?,?)", ("news:deepfakes", day, 300 - i))
    for i in range(12):
        upsert(db, "articles", {"id": f"a{i}", "fear": "deepfakes", "title": f"Deepfake scam wave hits voters in state {i}",
                                "url": "https://example.com", "domain": "example.com",
                                "seen": (today - dt.timedelta(days=1)).isoformat() + "T10:00:00", "entity": None})
    db.execute("INSERT INTO status VALUES('congress',?,1,5,'ok',?)", (iso(), iso()))
    db.commit()
    data = export.export(db, tmp)
    assert data["index"]["value"] != "0", data["index"]
    assert data["index"]["total_measures"] == 40, \
        f"a carried-over bill was counted twice: {data['index']['total_measures']} measures, not 40"
    # The plate goes out on X while the page it describes is one click away. They are counted by
    # two different pieces of code, so the numbers are held against each other here.
    from pipeline.brief import totals as plate_totals  # noqa: E402
    plate = plate_totals(db)
    chain = {label: value for value, label in data["index"]["chain"]}
    for word, key in (("bills, rules and orders", "measures"),
                      ("new government control", "controlled"),
                      ("would hand new power", "offices")):
        label = next((l for l in chain if word in l), None)
        assert label, f"the front page stopped saying {word!r}; the plate still counts it"
        assert chain[label] == f"{plate[key]:,}", \
            f"the plate says {plate[key]:,} and the page says {chain[label]} for {word!r}"
    # The plate counts offices in two places of its own, and they drifted apart once already.
    from pipeline.brief import patterns as plate_patterns  # noqa: E402
    lines = plate_patterns(db, dt.date.today().isoformat(), ())
    offices = next((l for l in lines if l["key"].startswith("offices:")), None)
    assert offices, "the plate stopped counting offices"
    assert offices["key"] == f"offices:{plate['offices']}", \
        f"the plate's office line says {offices['key']} and its own total says {plate['offices']}"
    assert data["fears"] and data["controls"]
    # The top of the page has to say who ends up holding the controls, not just that they exist
    assert "office" in (data["exhibit"]["bought"] or ""), \
        f"the hero stopped naming who holds the controls: {data['exhibit']['bought']!r}"
    assert data["funders"], "advocacy lobbying should rank separately"
    assert data["industry"], "company and trade group lobbying should rank separately"
    assert not ({f["name"] for f in data["funders"]} & {i["name"] for i in data["industry"]}), \
        "an organization must appear in one ranking or the other, never both"
    # An edition on record, so the page shows the card rather than only the words about it. The
    # real one is written by pipeline.brief; this is the row it leaves behind.
    from pipeline.common import kv_set as _kv_set  # noqa: E402
    brief_day = dt.date.today().isoformat()
    _kv_set(db, "brief:latest", {"edition": brief_day, "headline": "An office takes a register",
                                 "alt": "A card from the AI Fear Report."})
    db.commit()
    data = export.export(db, tmp, "")
    assert data["brief"]["image"] == f"/brief/{brief_day}.png", data["brief"]
    site = ROOT / "site" / "build.py"
    subprocess.run([sys.executable, str(site), "--data", str(tmp / "site_data.json"), "--out", str(tmp / "dist"),
                    "--base", ""], check=True)
    subprocess.run([sys.executable, str(site), "--data", str(tmp / "site_data.json"), "--preview", str(tmp / "p.html")],
                   check=True)
    # A row whose count is empty rendered its unit on its own: "Lobbying client  filings"
    for page in (tmp / "dist").rglob("index.html"):
        body = page.read_text()
        for stray in ("</span> filings", "> filings<", "mentions  fear"):
            assert stray not in body, f"{page.name} renders a unit with no number: {stray!r}"
        # One h1 per page. The front page had none at all, which is the page that matters most.
        heads = re.findall(r"<h1[^>]*>", body)
        assert len(heads) == 1, f"{page.relative_to(tmp / 'dist')} has {len(heads)} h1 headings"
        # Election receipts run the length of a two-year cycle, not the last four quarters
        assert "election money, past year" not in body and "election money this year" not in body, \
            f"{page.name} dates cycle-to-date election money as a year"
    check_every_page_asks(tmp / "dist", db, brief_day)
    pages = sorted(str(p.relative_to(tmp / "dist")) for p in (tmp / "dist").rglob("index.html"))
    print("pages:", len(pages), pages[:6])
    print("index:", json.dumps(data["index"])[:200])
    print("exhibit:", json.dumps(data["exhibit"])[:300])
    print("top fear:", data["fears"][0])
    print("top funder:", data["funders"][0]["name"], data["funders"][0]["amount"],
          "| top industry:", data["industry"][0]["name"], data["industry"][0]["amount"])
    print("OK", tmp)


if __name__ == "__main__":
    main()
