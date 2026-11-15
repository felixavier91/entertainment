#!/bin/bash
# Hourly on the ROG (systemd user timer themes-refresh.timer): pull the newest data, give new titles a theme with the
# local model (Ollama bge-m3), publish. Movies/TV = refresh_themes.py; books, music, games = refresh_media_themes.py.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
git pull -q --rebase --autostash
files=(); msg=()
out=$(.venv/bin/python refresh_themes.py); echo "movies: $out"
if [[ $out == changed* ]]; then files+=(themes.json labels.json); msg+=("movies ${out#changed: }"); fi
for m in books music games; do
    [[ -f $m.json && -f centroids_$m.npy && -f theme_names_$m.json ]] || continue   # themes not built for this medium yet
    out=$(.venv/bin/python refresh_media_themes.py "$m"); echo "$m: $out"
    if [[ $out == changed* ]]; then files+=("themes_$m.json" "labels_$m.json"); msg+=("$m ${out#changed: }"); fi
done
[[ ${#files[@]} -gt 0 ]] || exit 0
git commit -q -m "Themes refresh: $(IFS='; '; echo "${msg[*]}" | cut -c1-200)" -- "${files[@]}"
git push -q || { git pull -q --rebase --autostash && git push -q; }
