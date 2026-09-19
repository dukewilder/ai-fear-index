# AI Fear Report

Tracks the fears cited in AI bills, rules, and orders, the controls those measures would add,
who funds the lobbying and election money around them, and who would gain authority.

Everything runs on GitHub Actions:

- `.github/workflows/update.yml` runs every hour. It restores the database from the `data`
  branch, runs the collectors in `pipeline/`, tags new records with Claude, exports
  `site_data.json`, rebuilds the site from `site/`, and publishes it to GitHub Pages.
- The first run backfills everything since January 2025. The daily sources run at 06:00 UTC.
- `.github/workflows/recheck.yml` checks error reports filed through the site's form against
  the item's own source document, and pulls items the source doesn't support.

Secrets used: `FEC_API_KEY`, `CONGRESS_API_KEY`, `OPENSTATES_API_KEY`, `LDA_API_KEY`,
`ANTHROPIC_API_KEY`, `CONTACT_EMAIL`, and optionally `LEGISCAN_API_KEY`.

Data: the `data` branch holds the SQLite database, `site_data.json`, `status.json`, and CSVs
in `public/`. Each source's last successful run is listed in `status.json` and on the Method page.

Local check without network access: `python -m tests.test_offline`.
