"""A law's own text, read where its official summary gives nothing to quote.

Most legislatures publish no summary of a bill, and some publish a line. New York's for its frontier
AI law, the RAISE Act, reads in full: "Relates to the training and use of artificial intelligence
frontier models; defines terms; establishes remedies for violations." Every label on the site rests
on a quote from the measure, and a title and a line like that hold nothing to quote. On 25 September
2026, 125 of the 193 laws on the site carried no control, the RAISE Act, Texas's AI governance act,
Utah's AI consumer protection laws and Washington's companion chatbot law among them, and 83 of the
125 had a summary of under 25 words or none at all.

So for a law whose official summary is missing or shorter than THIN words, the tagger reads the
enacted text as well, and a label's quote may come from it. Laws only: a bill still moving changes
its text with every amendment, and a label would rest on words since struck out. The text is the
legislature's own copy, the latest version of the bill as Open States lists it, or for a rule or an
executive order the Federal Register's.
"""
import collections
import html as htmllib
import io
import re
import time

from .common import Http, HttpError, MissingKey, env, iso, kv_get, kv_set, log, measures_in_scope

THIN = 25             # words: a law whose summary is shorter than this is read with its text
TEXT_MAX = 60000      # characters of a law's text a reading takes, about fifteen pages
KEEP = 250000         # characters stored
LAW = ("passed", "signed", "final")
# Open States requests a day, out of the free tier's 250 a day that the collector also draws on. The
# collector leaves this much room while laws are still waiting for their text (collect_openstates).
OPEN_STATES_CAP = 40
TRIES = 3             # a legislature's server that refuses a document three times is left alone
SECONDS = 420
# Versions of a bill that are the law as passed, by the names legislatures give them.
ENACTED = re.compile(r"\b(enrolled|chaptered|enacted|signed|public act|session law|chapter|final|as passed|"
                     r"passed)\b", re.I)
FR_DOC = "https://www.federalregister.gov/api/v1/documents/{}.json"
# What the text of a law says of itself, somewhere near the top.
LAWLIKE = re.compile(r"(?i)be it enacted|\ban act\b|\bdo enact\b|it is hereby ordered|by the authority vested|"
                     r"\bthis (?:act|chapter|rule) (?:shall|may) be cited\b")
READABLE = ("text/html", "application/pdf", "text/plain")


def thin(summary):
    """True when a summary is too short to hold what a law does."""
    return len(re.findall(r"[A-Za-z]+", summary or "")) < THIN


def wanted(db):
    """The laws on the site whose official summary gives a reader nothing to quote."""
    return [m for m in measures_in_scope(db)
            if m["status"] in LAW and m["kind"] != "resolution" and thin(m["summary"])
            and (m["source"] == "Open States" or m["id"].startswith("fr-"))]


def law_texts(db):
    """{measure id: its law's text} for every text on file."""
    return {r["target"]: r["text"] for r in db.execute("SELECT target, text FROM texts WHERE text IS NOT NULL")}


def reading(m, texts):
    """The text a reading of this measure takes beside its title and summary, or "" for none.

    Only a law with a thin summary. A law whose summary later grows past THIN words is read from
    the summary again, like every other measure.
    """
    text = texts.get(m["id"]) or ""
    if not text or m.get("status") not in LAW or not thin(m.get("summary")):
        return ""
    return text[:TEXT_MAX]


def excerpt(text, quote, width=5000):
    """The part of a law around the words a label quotes, for the check to read in context.

    A whole law in every question would be most of the check's cost. The opening of the law is kept
    too, where its definitions and its purpose usually are.
    """
    words = re.findall(r"[A-Za-z0-9]+", quote or "")
    hit = re.search(r"\W+".join(map(re.escape, words)), text, re.I) if words else None
    if not hit:
        return text[:2 * width]
    a, b = max(0, hit.start() - width), min(len(text), hit.end() + width)
    head = text[:1500] + "\n[...]\n" if a > 1500 else text[:a]
    return head + text[a:b]


def text_links(b):
    """Where a law's enacted text can be read, among a bill's versions on Open States, best first.

    The latest version marked enrolled, chaptered, signed and the like, or the latest version when
    none is marked; HTML first, then PDF, then plain text. More than one, because some legislatures'
    HTML is a page that loads the bill with a script and holds none of it (Utah's), and the PDF of
    the same version is then the one to read.
    """
    versions = sorted(b.get("versions") or [], key=lambda v: v.get("date") or "")
    enacted = [v for v in versions if ENACTED.search(v.get("note") or "")]
    out = []
    for version in reversed(enacted or versions):
        links = [l for l in version.get("links") or [] if (l.get("url") or "").strip().startswith("http")]
        ranked = sorted(links, key=lambda l: next((i for i, k in enumerate(READABLE)
                                                   if (l.get("media_type") or "").lower().split(";")[0] == k),
                                                  len(READABLE) if re.search(r"\.(html?|pdf|txt)(\?|$)",
                                                                             l["url"].strip(), re.I) else 99))
        out += [l["url"].strip() for l in ranked if l["url"].strip() not in out]
        if out:
            break
    return out[:4]


# The legislature's copy of an enacted bill, where its address follows from the bill's own page on
# file. Tried before Open States is asked, because it costs none of the day's 250 requests, and the
# laws readers are likeliest to look up are here: New York's RAISE Act, Texas's governance act, Utah's.
FROM_PAGE = [
    # New York's Assembly page shows the bill's text when asked for it.
    (re.compile(r"^(https?://nyassembly\.gov/leg/\?(?!.*\bText=Y).*)$", re.I), lambda m: m.group(1) + "&Text=Y"),
    # Texas files each version by a letter, and F is the enrolled bill.
    (re.compile(r"capitol\.texas\.gov/BillLookup/History\.aspx\?LegSess=(\w+)&Bill=([A-Z]+)(\d+)", re.I),
     lambda m: f"https://capitol.texas.gov/tlodocs/{m.group(1)}/billtext/html/"
               f"{m.group(2).upper()}{int(m.group(3)):05d}F.htm"),
    # Utah's bill page loads the bill with a script. The enrolled copy is a PDF beside it.
    (re.compile(r"le\.utah\.gov/~(\d{4})/bills/static/([A-Z]+\d+)\.html", re.I),
     lambda m: f"https://le.utah.gov/Session/{m.group(1)}/bills/enrolled/{m.group(2).upper()}.pdf"),
]


def from_page(url):
    """The address of the enacted text that follows from a bill's page on the legislature's site, or ""."""
    for rx, make in FROM_PAGE:
        hit = rx.search(url or "")
        if hit:
            return make(hit)
    return ""


# Words a bill strikes out of the law are printed struck through. They are not the law it makes.
STRUCK = re.compile(r"<(s|strike|del)\b[^>]*>.*?</\1\s*>|<span[^>]*line-through[^>]*>[^<]*</span>", re.I | re.S)


def from_html(page):
    page = re.sub(r"(?is)<(script|style|head|noscript)\b.*?</\1\s*>", " ", page)
    page = STRUCK.sub(" ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table|pre|center)>", "\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    return htmllib.unescape(page)


def from_pdf(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:150])
    lines = [l for l in text.split("\n") if l.strip()]
    # Most bills print a line number down the margin, and the numbers land inside every quote.
    numbered = sum(1 for l in lines if re.match(r"\s*\d{1,3}\s+\S", l))
    if lines and numbered > len(lines) / 2:
        text = re.sub(r"(?m)^[ \t]*\d{1,3}[ \t]+(?=\S)", "", text)
    # A line holding nothing but a number is a line or page number, not the law's words.
    return re.sub(r"(?m)^[ \t]*\d{1,3}[ \t]*$\n?", "", text)


def tidy(text):
    text = text.replace("\r", "\n").replace("\u00a0", " ").replace("\u00ad", "")
    text = re.sub(r"(?<=[a-z])-\n\s*(?=[a-z])", "", text)  # a word broken across two lines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read(http, url):
    """The words of the document at url, from HTML, PDF or plain text."""
    resp = http.get(url, tries=2)
    kind = (resp.headers.get("Content-Type") or "").lower()
    body = resp.content
    if "pdf" in kind or body[:5] == b"%PDF-":
        text = from_pdf(body)
    elif "html" in kind or b"<html" in body[:4000].lower() or re.search(r"\.html?(\?|$)", url, re.I):
        text = from_html(resp.text)
    else:
        text = resp.text
    return usable(tidy(text))


def usable(text):
    """The text, if it is the text of a law. A page around the law is not.

    New York's bill page without its text still names the sections the bill adds ("Add Art 44-B
    \u00a7\u00a71420 - 1425"), so a page is taken for a law only when it is long enough to be one and
    reads like one: an enacting clause, or section after section.
    """
    if len(re.findall(r"[A-Za-z]+", text)) < 300:
        raise ValueError("too short to be the text of a law")
    if not LAWLIKE.search(text) and len(re.findall(r"(?i)\b(?:section|sec\.)\s*\d|\u00a7", text)) < 5:
        raise ValueError("not the text of a law: a page around it, or a page that loads it with a script")
    return text


def open_states_room(db):
    from .collect_openstates import DAY_BUDGET
    day = iso()[:10]
    spent = kv_get(db, "openstates_spend", {})
    used = spent.get("n", 0) if spent.get("date") == day else 0
    mine = kv_get(db, "texts_spend", {})
    mine_used = mine.get("n", 0) if mine.get("date") == day else 0
    return max(0, min(OPEN_STATES_CAP - mine_used, DAY_BUDGET - used)), day, used, mine_used


def find_links(db, laws, started):
    """Ask Open States for the versions of each law, twenty laws to a request."""
    from .collect_openstates import BASE, measure_id, ocd_id
    try:
        key = env("OPENSTATES_API_KEY")
    except MissingKey:
        return 0
    room, day, used, mine_used = open_states_room(db)
    http = Http(min_interval=6.5, headers={"X-API-KEY": key})
    groups = collections.OrderedDict()
    for m in laws:
        groups.setdefault((m["jurisdiction"], m["session"]), []).append(m)
    asked = 0
    try:
        for (code, session), ms in groups.items():
            for i in range(0, len(ms), 20):
                if asked >= room or time.time() - started > SECONDS:
                    return asked
                chunk = ms[i:i + 20]
                try:
                    data = http.json(BASE, params={"jurisdiction": ocd_id(code), "session": session,
                                                   "identifier": [m["identifier"] for m in chunk],
                                                   "include": ["versions"], "per_page": 20})
                except HttpError as exc:
                    if "exceeded limit" in str(exc) or exc.status == 429:
                        return asked
                    if exc.status != 400:
                        raise
                    data = {"results": []}  # a session or number it will not take: asked, and none given
                asked += 1
                links = {measure_id(b): "\n".join(text_links(b)) for b in data.get("results") or []}
                for m in chunk:
                    # A law the answer leaves out, or one with no readable version, is marked as asked
                    # with no address, and not asked again.
                    db.execute("INSERT INTO texts(target, url, tries, asked) VALUES(?,?,0,?) "
                               "ON CONFLICT(target) DO UPDATE SET url=excluded.url, tries=0, asked=excluded.asked, "
                               "error=NULL", (m["id"], links.get(m["id"], ""), day))
                db.commit()
        return asked
    finally:
        kv_set(db, "openstates_spend", {"date": day, "n": used + asked})
        kv_set(db, "texts_spend", {"date": day, "n": mine_used + asked})
        db.commit()


def fr_links(db, docs, started):
    """The Federal Register's plain text of each rule and executive order."""
    http = Http(min_interval=0.5)
    found = 0
    for m in docs:
        if time.time() - started > SECONDS:
            break
        try:
            d = http.json(FR_DOC.format(m["id"][3:]), params={"fields[]": ["raw_text_url", "body_html_url"]})
        except HttpError as exc:
            log(f"[texts] {m['id']}: {str(exc)[:160]}")
            continue
        url = d.get("raw_text_url") or d.get("body_html_url") or ""
        db.execute("INSERT INTO texts(target, url, tries) VALUES(?,?,0) ON CONFLICT(target) DO UPDATE SET url=excluded.url",
                   (m["id"], url))
        found += bool(url)
    db.commit()
    return found


def fetch_texts(db, started):
    """Read each document found and not yet read. A refusal counts a try; three and it is left."""
    http = Http(min_interval=1.0, timeout=90)
    got = 0
    rows = db.execute("SELECT target, url FROM texts WHERE url != '' AND text IS NULL AND tries < ?",
                      (TRIES,)).fetchall()
    for r in rows:
        if time.time() - started > SECONDS:
            break
        text, errors = "", []
        for url in r["url"].split("\n"):
            try:
                text = read(http, url)
                break
            except Exception as exc:  # one legislature's server is not the run's problem
                errors.append(f"{url}: {str(exc)[:140]}")
        if text:
            db.execute("UPDATE texts SET text = ?, fetched = ?, error = NULL WHERE target = ?",
                       (text[:KEEP], iso(), r["target"]))
            got += 1
        else:
            db.execute("UPDATE texts SET tries = tries + 1, error = ? WHERE target = ?",
                       ("; ".join(errors)[:600], r["target"]))
        db.commit()
    return got


def run(db, state, mode):
    started = time.time()
    laws = wanted(db)

    def on_file():
        return {r["target"]: dict(r) for r in db.execute("SELECT target, url, text, tries, asked FROM texts")}
    rows = on_file()
    # First the legislature's own copy where its address follows from the bill's page.
    for m in laws:
        url = from_page(m.get("source_url") or "")
        if m["id"] not in rows and url:
            db.execute("INSERT INTO texts(target, url, tries) VALUES(?,?,0)", (m["id"], url))
    db.commit()
    got = fetch_texts(db, started)
    rows = on_file()
    # Then Open States, for a law with no address yet and for one whose page gave nothing.
    need = [m for m in laws if m["source"] == "Open States" and m["identifier"] and m["session"]
            and (m["id"] not in rows or (not rows[m["id"]]["text"] and not rows[m["id"]]["asked"]
                                          and rows[m["id"]]["tries"] >= 1))]
    asked = find_links(db, need, started) if need else 0
    found = fr_links(db, [m for m in laws if m["id"].startswith("fr-") and m["id"] not in rows], started)
    got += fetch_texts(db, started)
    rows = on_file()
    kv_set(db, "texts_waiting", sum(1 for m in need if not (rows.get(m["id"]) or {}).get("asked")))
    have = sum(1 for m in laws if (rows.get(m["id"]) or {}).get("text"))
    db.commit()
    state["added"] = got
    state["message"] = (f"{len(laws)} laws with a summary under {THIN} words; {have} of them read from their text"
                        + (f"; {got} texts read this run" if got else "")
                        + (f"; asked Open States {asked} times" if asked else "")
                        + (f"; {found} Federal Register texts found" if found else ""))
