#!/usr/bin/env python3
"""
Deezer data fetcher → music.json
Albums and EPs released from START_YEAR to today, ranked by how many Deezer users follow them
("fans"). Artists are discovered from the Deezer charts of every genre and then by walking
"related artists". Descriptions come from Wikipedia. No API key needed.

Incremental: cache/albums.json keeps every album ever looked at, so a rerun only costs requests
for new albums, for albums whose fan count is stale, and for new Wikipedia lookups.
"""
import json, os, re, sys, time, heapq, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
import requests

sys.stdout.reconfigure(line_buffering=True)

# ============= CONFIGURATION =============
START_YEAR = 1990
CURRENT_YEAR = datetime.now().year
TODAY = date.today().isoformat()
UA = 'new-music (github.com/felixavier91/new-music)'
DZ = 'https://api.deezer.com'
RATE = 9.0                       # requests per second (Deezer allows 50 per 5 s)
WORKERS = 6                      # parallel connections; the rate limiter keeps the total under RATE
MAX_ARTISTS = int(os.environ.get('MAX_ARTISTS', 2500))
MIN_ARTIST_FANS = 40_000         # related artists below this are not explored
REFRESH_DAYS = 30                # how often an old album's fan count is re-read
RECENT_YEARS = 1                 # albums this new are re-read every run
WIKI_DELAY = 0.2
TYPES = ('album', 'ep')
KIDS_GENRE_ID = 95               # Deezer's "Kids" genre: never seeded, never kept
KIDS_RE = re.compile(r"\b(kids?|children|nursery|lullab|toddler|baby shark|cocomelon|disney junior|sing-?along|bedtime)\b", re.I)

def min_fans_for_year(year):
    """Fans an album needs to be listed. Newer albums need fewer.
    2026: 300 · 2025: 600 · 2020: 2,100 · 2010: 5,100 · 2000: 8,100 · 1990: 11,100"""
    return 300 * (CURRENT_YEAR - year + 1)

session = requests.Session()
session.headers['User-Agent'] = UA
session.mount('https://', requests.adapters.HTTPAdapter(pool_maxsize=WORKERS * 2))
calls = 0
_lock = threading.Lock()
_next_slot = [0.0]
def throttle(rate=None):
    """Token-bucket-ish limiter shared by all threads: at most `rate` requests per second overall."""
    with _lock:
        now = time.monotonic()
        _next_slot[0] = max(_next_slot[0], now) + 1.0 / (rate or RATE)
        wait = _next_slot[0] - now - 1.0 / (rate or RATE)
    if wait > 0: time.sleep(wait)
def pmap(fn, items, workers=WORKERS):
    with ThreadPoolExecutor(workers) as ex: return list(ex.map(fn, items))

def dz(path, **params):
    global calls
    for attempt in range(5):
        try:
            throttle()
            r = session.get(DZ + path, params=params, timeout=60)
            with _lock: calls += 1
            d = r.json()
            if isinstance(d, dict) and d.get('error'):
                code = d['error'].get('code')
                if code == 4:                      # quota exceeded → back off
                    time.sleep(5 * (attempt + 1)); continue
                return None                        # 800: no data, etc.
            return d
        except (requests.RequestException, ValueError) as e:
            print(f'  ! {path} {e}; retrying'); time.sleep(3 * (attempt + 1))
    return None

def dz_list(path, **params):
    """Follow Deezer pagination ('next' links)."""
    out, d = [], dz(path, limit=100, **params)
    while d:
        out += d.get('data', [])
        nxt = d.get('next')
        if not nxt: break
        d = dz(nxt.replace(DZ, ''))
    return out

NOISE_RE = re.compile(r'\s*[\(\[][^)\]]*(deluxe|edition|version|remaster|expanded|anniversary|bonus|explicit|clean|'
                      r'reissue|re-issue|special|complete|collector|platinum|tour|live|mono|stereo|super|digital|'
                      r'instrumental|commentary|extended|international|japan|uk |us )[^)\]]*[\)\]]', re.I)
def norm_title(t):
    t = NOISE_RE.sub('', t)
    t = re.sub(r'\s*[-:–]\s*(deluxe|remastered|expanded|anniversary).*$', '', t, flags=re.I)
    return re.sub(r'\W+', ' ', t).strip().lower()

# ============= CACHE =============
os.makedirs('cache', exist_ok=True)
CACHE = 'cache/albums.json'
cache = json.load(open(CACHE, encoding='utf-8')) if os.path.exists(CACHE) else {}
old_desc = {}
if os.path.exists('music.json'):
    try:
        for a in json.load(open('music.json', encoding='utf-8')).get('albums', []):
            if 'overview' in a: old_desc[str(a['id'])] = a['overview']
    except Exception as e:
        print('could not read old music.json:', e)
print(f'🎵 Deezer fetch, {START_YEAR}–{CURRENT_YEAR}; {len(cache)} albums cached, {len(old_desc)} descriptions known\n')

# ============= 1. GENRES + SEEDS =============
genres = {g['id']: g['name'] for g in (dz('/genre') or {}).get('data', []) if g['id'] != 0}
genres[0] = 'All'
print(f'genres: {len(genres)}')
artists = {}          # id → {'name', 'fans', 'genres': set()}
seed_albums = {}      # id → genre name it charted in
def add_artist(a, genre=None):
    e = artists.setdefault(a['id'], {'name': a['name'], 'fans': a.get('nb_fan', 0), 'genres': set()})
    if a.get('nb_fan'): e['fans'] = max(e['fans'], a['nb_fan'])
    if genre and genre != 'All': e['genres'].add(genre)
for gid, gname in genres.items():
    if gid == KIDS_GENRE_ID: continue
    for a in (dz(f'/chart/{gid}/albums', limit=100) or {}).get('data', []):
        seed_albums.setdefault(a['id'], gname); add_artist(a['artist'], gname)
    for a in (dz(f'/chart/{gid}/artists', limit=100) or {}).get('data', []): add_artist(a, gname)
    for a in (dz(f'/genre/{gid}/artists') or {}).get('data', []): add_artist(a, gname)
print(f'seeds: {len(artists)} artists, {len(seed_albums)} charting albums ({calls} calls)')

# ============= 2. WALK RELATED ARTISTS (biggest first) =============
# artists found in earlier runs keep their place so the corpus stays stable
known = json.load(open('cache/artists.json')) if os.path.exists('cache/artists.json') else {}
for aid, e in known.items():
    if int(aid) not in artists: artists[int(aid)] = {'name': e['name'], 'fans': e['fans'], 'genres': set(e.get('genres', []))}
explored = set()
heap = [(-e['fans'], aid) for aid, e in artists.items()]
heapq.heapify(heap)
while heap and len(artists) < MAX_ARTISTS:
    batch = []
    while heap and len(batch) < 40:                      # biggest unexplored artists, a batch at a time
        _, aid = heapq.heappop(heap)
        if aid not in explored: explored.add(aid); batch.append(aid)
    for aid, rel in zip(batch, pmap(lambda i: (dz(f'/artist/{i}/related', limit=50) or {}).get('data', []), batch)):
        for a in rel:
            if a.get('nb_fan', 0) < MIN_ARTIST_FANS: continue
            if a['id'] not in artists:
                add_artist(a)
                for g in artists[aid]['genres']: artists[a['id']]['genres'].add(g)   # inherit the seed's genre as a hint
                heapq.heappush(heap, (-a['nb_fan'], a['id']))
    print(f'  related: {len(explored)} explored, {len(artists)} artists ({calls} calls)')
json.dump({str(k): {'name': v['name'], 'fans': v['fans'], 'genres': sorted(v['genres'])} for k, v in artists.items()},
          open('cache/artists.json', 'w', encoding='utf-8'), ensure_ascii=False)
print(f'artists: {len(artists)} ({calls} calls)\n')

# ============= 3. EACH ARTIST'S ALBUMS =============
candidates = {}       # album id → list entry (+ artist info)
order = sorted(artists.items(), key=lambda kv: -kv[1]['fans'])
for start in range(0, len(order), 200):
    chunk = order[start:start + 200]
    for (aid, art), albums_of in zip(chunk, pmap(lambda kv: dz_list(f'/artist/{kv[0]}/albums'), chunk)):
        for a in albums_of:
            if a.get('record_type') not in TYPES: continue
            y = int((a.get('release_date') or '0')[:4] or 0)
            if y < START_YEAR or (a.get('release_date') or '') > TODAY: continue
            a['_artist'] = {'id': aid, 'name': art['name'], 'genres': sorted(art['genres'])}
            candidates[a['id']] = a
    print(f'  albums: {start + len(chunk)}/{len(artists)} artists, {len(candidates)} candidates ({calls} calls)')
missing_seeds = [i for i in seed_albums if i not in candidates]
for alb_id, d in zip(missing_seeds, pmap(lambda i: dz(f'/album/{i}'), missing_seeds)):
    if d and d.get('record_type') in TYPES:
        d['_artist'] = {'id': d['artist']['id'], 'name': d['artist']['name'], 'genres': [seed_albums[alb_id]]}
        candidates[alb_id] = d
print(f'candidates: {len(candidates)} albums/EPs since {START_YEAR} ({calls} calls)\n')

# ============= 4. DETAILS (fans, genres, tracks) WITH CACHE =============
def slim(d):
    tracks = (d.get('tracks') or {}).get('data') or []
    return {
        'title': d.get('title'), 'fans': d.get('fans', 0), 'release_date': d.get('release_date'),
        'record_type': d.get('record_type'), 'nb_tracks': d.get('nb_tracks'), 'duration': d.get('duration'),
        'label': d.get('label'), 'explicit': bool(d.get('explicit_lyrics')), 'cover': d.get('cover_xl') or d.get('cover_big'),
        'genres': [g['name'] for g in (d.get('genres') or {}).get('data', []) if g.get('id', -1) != -1],
        'artist': {'id': d['artist']['id'], 'name': d['artist']['name']} if d.get('artist') else None,
        'contributors': [c['name'] for c in (d.get('contributors') or [])[:4]],
        'tracks': [t['title'] for t in tracks][:40],
        'preview': next((t.get('preview') for t in tracks if t.get('preview')), None),
        'fetched': TODAY,
    }
def save_cache():
    """Write the cache atomically, so a run killed mid-write never leaves a corrupt file."""
    tmp = CACHE + '.tmp'
    json.dump(cache, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, CACHE)
def stale(entry, year):
    if not entry or not entry.get('fetched'): return True
    if year >= CURRENT_YEAR - RECENT_YEARS: return entry['fetched'] != TODAY
    return (date.today() - date.fromisoformat(entry['fetched'])).days > REFRESH_DAYS
todo = [aid for aid, a in candidates.items() if stale(cache.get(str(aid)), int(a['release_date'][:4]))]
print(f'details: {len(todo)} of {len(candidates)} need a request')
for start in range(0, len(todo), 500):
    chunk = todo[start:start + 500]
    for aid, d in zip(chunk, pmap(lambda i: dz(f'/album/{i}'), chunk)):
        if d: cache[str(aid)] = slim(d)
    print(f'  details {start + len(chunk)}/{len(todo)} ({calls} calls)')
    save_cache()
save_cache()

# ============= 5. THRESHOLD + DEDUPE (deluxe/remaster variants collapse into one) =============
kept = {}
for aid, a in candidates.items():
    c = cache.get(str(aid))
    if not c or not c.get('release_date'): continue
    year = int(c['release_date'][:4])
    if c['fans'] < min_fans_for_year(year): continue
    if 'Kids' in c['genres'] or KIDS_RE.search(c['title'] or '') or KIDS_RE.search(a['_artist']['name']): continue
    key = (a['_artist']['id'], norm_title(c['title']))
    cur = kept.get(key)
    if cur is None or c['fans'] > cache[str(cur)]['fans']: kept[key] = aid
print(f'kept: {len(kept)} after fan thresholds and dedupe\n')

# ============= 6. WIKIPEDIA DESCRIPTIONS =============
def wikipedia(title, artist):
    d = None
    try:
        throttle(1.0 / WIKI_DELAY)
        r = session.get('https://en.wikipedia.org/w/api.php', timeout=60, params=dict(
            action='query', generator='search', gsrsearch=f'"{title}" {artist} album', gsrlimit=1,
            prop='extracts|description', exintro=1, explaintext=1, exsentences=5, format='json', formatversion=2))
        d = r.json()
    except (requests.RequestException, ValueError):
        return None
    pages = (d or {}).get('query', {}).get('pages') or []
    if not pages: return ''
    p = pages[0]
    kind = (p.get('description') or '').lower()
    if not re.search(r'album|\bep\b|mixtape|soundtrack|compilation', kind): return ''
    text = (p.get('extract') or '').strip()
    return re.sub(r'\s*\n\s*', ' ', text)[:1500]

albums, wiki_calls = [], 0
need = [aid for aid in kept.values() if old_desc.get(str(aid)) is None]
print(f'wikipedia: {len(need)} albums need a lookup')
for start in range(0, len(need), 300):
    chunk = need[start:start + 300]
    for aid, text in zip(chunk, pmap(lambda i: wikipedia(cache[str(i)]['title'], candidates[i]['_artist']['name']), chunk, workers=3)):
        old_desc[str(aid)] = text or ''; wiki_calls += 1
    print(f'  wikipedia {start + len(chunk)}/{len(need)}')
for n, aid in enumerate(kept.values()):
    c, a = cache[str(aid)], candidates[aid]
    desc = old_desc.get(str(aid)) or ''
    gen = c['genres'] or a['_artist']['genres'][:2] or (['Reggaeton'] if a.get('genre_id') == 122 else [])
    if not gen and a.get('genre_id') in genres: gen = [genres[a['genre_id']]]
    albums.append({
        'id': aid,
        'title': c['title'],
        'artist': a['_artist']['name'],
        'artist_id': a['_artist']['id'],
        'overview': desc,
        'cover': c['cover'],
        'release_date': c['release_date'],
        'year': int(c['release_date'][:4]),
        'fans': c['fans'],
        'genres': ', '.join(gen) if gen else 'N/A',
        'label': c['label'],
        'tracks': c['tracks'],
        'nb_tracks': c['nb_tracks'],
        'duration': c['duration'],
        'explicit': c['explicit'],
        'contributors': c['contributors'],
        'preview': c['preview'],
        'type': c['record_type'],
        'link': f'https://www.deezer.com/album/{aid}',
    })

albums.sort(key=lambda a: a['release_date'], reverse=True)
meta = {
    'generated_at': datetime.now().isoformat(),
    'start_year': START_YEAR, 'end_year': CURRENT_YEAR,
    'total_albums': sum(a['type'] == 'album' for a in albums),
    'total_eps': sum(a['type'] == 'ep' for a in albums),
    'total_items': len(albums), 'artists_scanned': len(artists), 'candidates': len(candidates),
    'source': 'Deezer (api.deezer.com) + Wikipedia for descriptions',
}
json.dump({'albums': albums, 'metadata': meta}, open('music.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('=' * 50)
print(f"✅ {meta['total_items']} releases ({meta['total_albums']} albums, {meta['total_eps']} EPs) from {len(artists)} artists")
print(f"📦 music.json {os.path.getsize('music.json') / 1e6:.1f} MB · cache {os.path.getsize(CACHE) / 1e6:.1f} MB · {calls} Deezer calls · {wiki_calls} Wikipedia lookups")
print('=' * 50)
