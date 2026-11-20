#!/usr/bin/env python3
"""Fetch song lyrics for every track in music.json from LRCLIB (lrclib.net, free, no key) into a local cache.

The lyrics stay on this machine (cache/lyrics.sqlite, not in git). build_lyrics_index.py turns them into the
hashed search index the site uses, which holds no lyric text.

    python3 fetch_lyrics.py [--minutes N]     # stop after N minutes (default: run until done)

Prints "changed: N new" when it stored lyrics for N more songs, else "unchanged".
"""
import fcntl, json, re, sqlite3, sys, time, threading, unicodedata, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

DB = 'cache/lyrics.sqlite'
API = 'https://lrclib.net/api/get'
UA = 'entertainment-lyrics-index/1.0'
RATE = 1.4           # requests per second, all workers together (LRCLIB allows a named client about 90 a minute)
WORKERS = 2
RETRY_MISS_DAYS = 30  # a song LRCLIB did not have is asked again after this long
RETRY_NEW_DAYS = 3    # ... or this long while its album is under two months old (lyrics show up late)

def key(artist, track):
    n = lambda s: re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', s or '').lower()).strip()
    return n(artist) + '\t' + n(track)

PAREN = re.compile(r'\s*[\(\[][^)\]]*[\)\]]')
SUFFIX = re.compile(r'\s+[-–]\s+[^-–]*\b(remaster|live|version|edit|mix|mono|stereo|bonus|demo|acoustic|single|from|feat)\b.*$', re.I)
def clean_title(t):
    c = SUFFIX.sub('', PAREN.sub('', t)).strip()
    return c or t

_lock = threading.Lock(); _next = [0.0]; limited = [0]
def throttle():
    with _lock:
        now = time.monotonic(); at = max(now, _next[0]); _next[0] = at + 1.0 / RATE
    if at > now: time.sleep(at - now)

def get(artist, track):
    """-> lyrics text ('' for instrumental), None when LRCLIB has none; raises on network trouble."""
    url = API + '?' + urllib.parse.urlencode({'artist_name': artist, 'track_name': track})
    for attempt in range(4):
        throttle()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=30) as r:
                d = json.load(r)
            return '' if d.get('instrumental') else (d.get('plainLyrics') or re.sub(r'\[[\d:.]+\]\s*', '', d.get('syncedLyrics') or '') or None)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404): return None
            if e.code == 429: limited[0] += 1
            time.sleep(min(int(e.headers.get('Retry-After') or 0) + 1, 120) if e.code == 429 else 5 * (attempt + 1))
        except Exception:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError('LRCLIB not answering')

def lookup(job):
    artist, track = job
    try:
        text = get(artist, track)
        if text is None and clean_title(track) != track: text = get(artist, clean_title(track))
        return job, text, True
    except Exception:
        return job, None, False

def main():
    minutes = float(sys.argv[sys.argv.index('--minutes') + 1]) if '--minutes' in sys.argv else None
    deadline = time.time() + minutes * 60 if minutes else None
    lock = open('cache/lyrics.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print('unchanged'); return          # another run is already fetching
    db = sqlite3.connect(DB)
    db.execute('CREATE TABLE IF NOT EXISTS lyrics (k TEXT PRIMARY KEY, text TEXT, ts INTEGER)')   # text NULL = LRCLIB had none
    have = {k: (text is not None, ts) for k, text, ts in db.execute('SELECT k, text, ts FROM lyrics')}
    albums = json.load(open('music.json'))['albums']
    albums.sort(key=lambda a: -(a.get('fans') or 0))           # most-loved albums first, so a partial run is already useful
    now = time.time(); today = time.strftime('%Y-%m-%d'); jobs = []; seen = set()
    for a in albums:
        fresh = (a.get('release_date') or '') >= time.strftime('%Y-%m-%d', time.gmtime(now - 60 * 86400))
        for t in a.get('tracks') or []:
            k = key(a['artist'], t)
            if k in seen: continue
            seen.add(k)
            if k in have and (have[k][0] or now - have[k][1] < (RETRY_NEW_DAYS if fresh else RETRY_MISS_DAYS) * 86400): continue
            jobs.append((a['artist'], t))
    new = done = failed = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for i in range(0, len(jobs), 60):
            if deadline and time.time() > deadline: break
            for (artist, track), text, ok in ex.map(lookup, jobs[i:i + 60]):
                if not ok: failed += 1; continue
                db.execute('INSERT OR REPLACE INTO lyrics VALUES (?,?,?)', (key(artist, track), text, int(time.time())))
                done += 1; new += text is not None and text != ''
            db.commit()
            if (i // 60) % 5 == 0: print(f'{done + failed}/{len(jobs)} asked, {new} with lyrics, {failed} failed, {limited[0]} told to slow down', file=sys.stderr, flush=True)
            if failed > 200 and failed > done: break          # the service is down; try again next run
    print(f'changed: {new} new' if new else 'unchanged')

if __name__ == '__main__':
    main()
