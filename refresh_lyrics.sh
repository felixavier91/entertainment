#!/bin/bash
# Lyrics search for the music section. Called at the end of refresh_themes.sh (hourly on the ROG):
#   1. fetch_lyrics.py        asks LRCLIB for lyrics of songs not looked up yet (lyrics stay in cache/, never published)
#   2. build_lyrics_index.py  rebuilds lyrics/ (word-pair hashes -> songs, no lyric text) when there is something new, at most every 3 hours
#   3. the index goes to the branch lyrics-index as ONE commit, force-pushed, so its ~40 MB never pile up in the history;
#      the Pages workflow checks that branch out into lyrics/ when it deploys
#   4. lyrics_version.json is committed on main, which starts that deploy
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
python3 fetch_lyrics.py --minutes 45 2>>fetch_lyrics.log || true
out=$(.venv/bin/python build_lyrics_index.py --min-hours 3); echo "lyrics: $out"
[[ $out == changed* ]] || exit 0
rm -rf lyrics/.git
git -C lyrics init -q -b lyrics-index
git -C lyrics add -A
git -C lyrics commit -q -m "Lyrics index $(date -u +'%Y-%m-%d %H:%M UTC'): ${out#changed: }"
git -C lyrics push -q -f "$(git remote get-url origin)" lyrics-index
rm -rf lyrics/.git
python3 -c "import json; m=json.load(open('lyrics/meta.json')); json.dump({'v': m['v'], 'songs': m['songs']}, open('lyrics_version.json', 'w'))"
git add lyrics_version.json
git commit -q -m "Lyrics index: ${out#changed: }" -- lyrics_version.json
git push -q || { git pull -q --rebase --autostash && git push -q; }
