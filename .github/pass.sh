#!/usr/bin/env bash
# One pass of the whole thing: collect and export, save the database, build the site, save
# the site. With "loop" the passes repeat every twenty minutes until the job's time is up.
#
#   bash .github/pass.sh once   one pass and stop
#   bash .github/pass.sh loop   one pass, then another every twenty minutes
#   bash .github/pass.sh more   the loop without the first pass, for a run that made it already
#
# GitHub's scheduler serves this repo's cron slots rarely, so a run cannot do one pass and
# stop. It keeps passing for most of the six-hour job limit, and the next scheduled run
# waits in the concurrency group to take over the moment this one ends.
#
# Needs GH_TOKEN, GITHUB_REPOSITORY, STARTED (job start, epoch seconds) and the source keys
# in the environment. MODE and ONLY are passed through to pipeline.run.
set -u
REMOTE="${REMOTE:-https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git}"
MODE="${MODE:-auto}"
ONLY="${ONLY:-}"
LOOP_MINUTES="${LOOP_MINUTES:-330}"   # of a 360-minute job limit; the rest is for wrap-up
PERIOD="${PERIOD:-1200}"              # twenty minutes between passes
DATA_EVERY="${DATA_EVERY:-3600}"      # the database is 23MB, so it is saved hourly, not every pass
data_saved=0

save() {  # push a folder as a fresh orphan branch; the old history is left to GitHub's gc
  local folder="$1" branch="$2" label="$3"
  ( cd "$folder" && rm -rf .git && git init -q && git checkout -q --orphan "$branch" && git add -A \
    && git -c user.name="ai-fear-report" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
           commit -qm "$label $(date -u +%Y-%m-%dT%H:%MZ)" \
    && git push -qf "$REMOTE" "$branch" ) || echo "::warning::could not save $branch"
  rm -rf "$folder/.git"
}

one_pass() {
  echo "::group::pass $1 at $(date -u +%H:%MZ)"
  python -m pipeline.run --mode "$MODE" --only "$ONLY" --db state/index.db --out state \
    || echo "::warning::pass $1 ended in an error; whatever it saved before that is kept"
  if [ -f state/index.db ] && [ $(( $(date +%s) - data_saved )) -ge "$DATA_EVERY" ]; then
    save state data Data
    data_saved=$(date +%s)
  fi
  # One edition of the brief a day, written but not posted. It rides the data branch so the
  # next run knows what has already been said, and is copied onto the site to be looked at.
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    python -m pipeline.brief --db state/index.db --out state/brief \
      || echo "::warning::pass $1 wrote no brief"
  fi
  if [ -f state/site_data.json ]; then
    rm -rf dist
    if python site/build.py --data state/site_data.json --out dist --base "" > /dev/null; then
      touch dist/.nojekyll
      # Pages keeps the custom domain in this file, and every pass replaces the branch
      printf 'aifearreport.com\n' > dist/CNAME
      [ -d state/brief ] && cp -r state/brief dist/brief
      save dist gh-pages Site
    else
      echo "::warning::pass $1 built no site; the last one stays up"
    fi
  fi
  echo "::endgroup::"
}

mode="${1:-once}"
[ "$mode" = "more" ] || one_pass 1
[ "$mode" = "once" ] && exit 0

deadline=$(( ${STARTED:-$(date +%s)} + LOOP_MINUTES * 60 ))
pass=1
[ "$mode" = "more" ] && data_saved=$(date +%s)
while :; do
  now=$(date +%s)
  next=$(( (now / PERIOD + 1) * PERIOD ))
  [ "$next" -ge "$deadline" ] && break
  sleep $(( next - now ))
  pass=$((pass + 1))
  one_pass "$pass"
done
# The data branch is what the next run starts from, so it is saved whatever the clock says.
[ -f state/index.db ] && save state data Data
echo "$pass passes; the next run takes it from here"
