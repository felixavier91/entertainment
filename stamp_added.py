#!/usr/bin/env python3
"""Keeps added.json: when each title first showed up on the site (drives the "Recently Added" sort).
Run with no arguments (hourly, from refresh_themes.sh): any id not stamped yet gets the current time.
Run with --seed once: rebuilds every stamp from the git history of the data files.
Format: {type: {"YYYY-MM-DDTHH:MM": [ids]}}; prints "changed: N new" or "same"."""
import json, subprocess, sys
from datetime import datetime, timezone

SOURCES = {'movie': ('data.json', 'movies'), 'tv': ('data.json', 'tv_shows'), 'book': ('books.json', 'books'),
           'music': ('music.json', 'albums'), 'game': ('games.json', 'games')}
OUT = 'added.json'

def ids_of(doc, key):
    return [str(x['id']) for x in (doc.get(key) or doc.get('tv' if key == 'tv_shows' else key) or []) if 'id' in x]

def load():
    try:
        return json.load(open(OUT))
    except (OSError, ValueError):
        return {}

def flat(groups):
    return {i: ts for ts, ids in groups.items() for i in ids}

def save(first):                                   # first = {type: {id: ts}}
    out = {}
    for t, m in first.items():
        g = {}
        for i, ts in m.items():
            g.setdefault(ts, []).append(i)
        out[t] = {ts: g[ts] for ts in sorted(g)}
    json.dump(out, open(OUT, 'w'), separators=(',', ':'))

def seed():
    first = {t: {} for t in SOURCES}
    for f in sorted({f for f, _ in SOURCES.values()}):
        log = subprocess.run(['git', 'log', '--reverse', '--format=%H %cI', '--', f], capture_output=True, text=True, check=True).stdout.split('\n')
        for line in filter(None, log):
            h, when = line.split()
            ts = datetime.fromisoformat(when).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M')
            try:
                doc = json.loads(subprocess.run(['git', 'show', f'{h}:{f}'], capture_output=True, check=True).stdout)
            except (subprocess.CalledProcessError, ValueError):
                continue
            if not isinstance(doc, dict):
                continue
            for t, (ff, key) in SOURCES.items():
                if ff == f:
                    for i in ids_of(doc, key):
                        first[t].setdefault(i, ts)
    return first

def main():
    first = seed() if '--seed' in sys.argv else {t: flat(g) for t, g in load().items()}
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M')
    new = 0
    for t, (f, key) in SOURCES.items():
        try:
            ids = ids_of(json.load(open(f)), key)
        except (OSError, ValueError):
            continue
        m = first.setdefault(t, {})
        for i in ids:
            if i not in m:
                m[i] = now; new += 1
    if new or '--seed' in sys.argv:
        save(first)
    print(f'changed: {new} new' if new else 'same')

if __name__ == '__main__':
    main()
