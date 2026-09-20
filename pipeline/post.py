"""Put one edition of the brief on X, with its plate, once.

    python -m pipeline.post --db state/index.db --brief state/brief --dry-run

The post carries no link. X charges more than ten times as much for a post containing one and
shows it to fewer people, and the plate already has the address on it.

Signing is done here rather than by a library, because the pipeline runs every twenty minutes and
one more dependency in that path is one more thing that can stop it. OAuth 1.0a is a signature over
the method, the address, and the parameters, and that is all this is.
"""
import argparse
import base64
import hashlib
import hmac
import json
import pathlib
import secrets
import time
import urllib.parse

import requests

from .brief import edition_date
from .common import connect, env, kv_get, kv_set, log, now

POST_URL = "https://api.x.com/2/tweets"
MEDIA_V2 = "https://api.x.com/2/media/upload"
MEDIA_V1 = "https://upload.twitter.com/1.1/media/upload.json"


def quote(value):
    """Percent-encoding as OAuth defines it, which is not what quote defaults to."""
    return urllib.parse.quote(str(value), safe="-._~")


def auth_header(method, url, creds, params=None):
    """One Authorization header.

    Query parameters and form fields belong in the signature; a JSON or multipart body does not,
    which is why nothing here reads the body.
    """
    oauth = {
        "oauth_consumer_key": creds["key"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": creds["token"],
        "oauth_version": "1.0",
    }
    joined = "&".join(f"{quote(k)}={quote(v)}" for k, v in
                      sorted({**oauth, **(params or {})}.items()))
    base = "&".join([method.upper(), quote(url), quote(joined)])
    signing_key = f"{quote(creds['secret'])}&{quote(creds['token_secret'])}"
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{quote(k)}="{quote(v)}"' for k, v in sorted(oauth.items()))


def credentials():
    names = {"key": "X_API_KEY", "secret": "X_API_SECRET",
             "token": "X_ACCESS_TOKEN", "token_secret": "X_ACCESS_SECRET"}
    # asked for without required, so a run that is missing three of them says all three
    creds = {k: env(v, required=False) for k, v in names.items()}
    missing = [names[k] for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError(f"not set in this environment: {', '.join(missing)}")
    return creds


def upload(creds, image):
    """Put the plate up and return its id.

    Two endpoints exist for this and which one an account may use has moved about, so the newer is
    tried first and the older is the fallback. Whichever answers is named in the log, so the next
    person to read this knows which one this account actually has.
    """
    blob = pathlib.Path(image).read_bytes()
    last = ""
    for url, field in ((MEDIA_V2, "media"), (MEDIA_V1, "media")):
        try:
            r = requests.post(url, timeout=120,
                              headers={"Authorization": auth_header("POST", url, creds)},
                              files={field: ("plate.png", blob, "image/png")})
        except requests.RequestException as exc:
            last = str(exc)
            continue
        if r.status_code in (200, 201):
            body = r.json()
            media_id = str(body.get("id") or body.get("media_id_string")
                           or (body.get("data") or {}).get("id") or "")
            if media_id:
                log(f"[post] image accepted by {url}")
                return media_id
            last = f"{url}: no id in {r.text[:200]}"
            continue
        last = f"{url}: {r.status_code} {r.text[:200]}"
        log(f"[post] {last}")
    raise RuntimeError(f"could not upload the image. {last}")


def publish(creds, text, media_id=None):
    payload = {"text": text}
    if media_id:
        payload["media"] = {"media_ids": [media_id]}
    r = requests.post(POST_URL, timeout=60, json=payload,
                      headers={"Authorization": auth_header("POST", POST_URL, creds),
                               "Content-Type": "application/json"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"X refused the post: {r.status_code} {r.text[:400]}")
    return (r.json().get("data") or {}).get("id", "")


def edition_for(brief_dir, date):
    path = pathlib.Path(brief_dir) / f"{date}.json"
    if not path.exists():
        raise FileNotFoundError(f"no edition written for {date} ({path})")
    return json.loads(path.read_text()), path.with_suffix(".png")


def run(db, brief_dir, date, dry_run=False, force=False):
    """One edition, once. The database remembers which, so a second run says so and stops."""
    done = kv_get(db, f"posted:{date}")
    if done and not force:
        log(f"[post] {date} already went out as {done.get('id')}")
        return None
    data, image = edition_for(brief_dir, date)
    text = data["text"]
    if dry_run:
        print("\n" + "-" * 68)
        print(text)
        print("-" * 68)
        print(f"image: {image}  ({'found' if image.exists() else 'MISSING'})")
        print(f"alt:   {data.get('alt', '')[:150]}")
        print(f"\n{len(text)} characters. Nothing was posted.")
        return None
    creds = credentials()
    media_id = upload(creds, image) if image.exists() else None
    if not media_id:
        log("[post] going out without the plate")
    post_id = publish(creds, text, media_id)
    kv_set(db, f"posted:{date}", {"id": post_id, "at": now().isoformat()})
    db.commit()
    log(f"[post] {date} is up: https://x.com/aiFearReport/status/{post_id}")
    return post_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/index.db")
    ap.add_argument("--brief", default="state/brief")
    ap.add_argument("--date", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="post again even if this date already went")
    args = ap.parse_args()
    db = connect(args.db)
    run(db, args.brief, args.date or edition_date(), args.dry_run, args.force)


if __name__ == "__main__":
    main()
