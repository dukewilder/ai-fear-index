"""Draw the Bitcoin code the pitch-in section shows, from the address in config/site.json.

Run by hand, and only when that address changes:

    pip install segno
    python site/btc_qr.py

The code is drawn once and committed rather than built every run, so the site does not take on a
dependency for a picture that changes when the address does, which is never. check_every_page_asks
fails the build if the address in the config and the one this file wrote stop agreeing, and says
to run this.

Dark modules on a fixed white ground in both themes. An inverted code, light on dark, reads in
the iOS camera but not in every wallet, and a wallet is exactly what will be pointed at it.
"""
import io
import json
import pathlib

import segno

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "templates" / "btc-qr.svg"


def main():
    addr = json.loads((ROOT / "config" / "site.json").read_text())["donate_btc"]
    # Level M survives a smudged or dim screen and keeps the grid small: version 3, 29 modules.
    code = segno.make(f"bitcoin:{addr}", error="m")
    buf = io.BytesIO()
    # The title carries the address, which is what the check reads to know this is current.
    code.save(buf, kind="svg", dark="#141210", light="#FFFFFF", border=2, xmldecl=False,
              svgns=True, omitsize=True, title=addr, svgclass="qr", nl=False)
    OUT.write_text(buf.getvalue().decode() + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: version {code.version}, {addr}")


if __name__ == "__main__":
    main()
