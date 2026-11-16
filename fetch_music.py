#!/usr/bin/env python3
"""
Deezer data fetcher → music.json
Albums and EPs released from START_YEAR to today, ranked by how many Deezer users follow them
("fans"). Artists are discovered from the Deezer charts of every genre and then by walking
"related artists". No API key needed. (Wikipedia album notes exist but are off by default.)

Incremental: cache/albums.json keeps every album ever looked at, so a rerun only costs requests
for new albums, for albums whose fan count is stale, and for new Wikipedia lookups.
"""
import json, os, re, sys, time, heapq, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
import requests

sys.stdout.reconfigure(line_buffering=True)

# ============= CONFIGURATION =============
START_YEAR = 1950
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
WIKI_DELAY = 1.0                 # one Wikipedia summary per second; anything faster gets this address throttled
TYPES = ('album', 'ep')
MB_BUDGET = int(os.environ.get('MB_BUDGET_MIN', 20)) * 60      # seconds per run spent asking MusicBrainz for original release dates (1 req/s)
TAGS_BUDGET = int(os.environ.get('TAGS_BUDGET_MIN', 15)) * 60  # seconds per run spent asking MusicBrainz for artist subgenres (1 req/s)
SPOTIFY_ID, SPOTIFY_SECRET = os.environ.get('SPOTIFY_CLIENT_ID'), os.environ.get('SPOTIFY_CLIENT_SECRET')   # optional: direct album links
KIDS_GENRE_ID = 95               # Deezer's "Kids" genre: never seeded, never kept
KIDS_RE = re.compile(r"\b(kids?|children|nursery|lullab|toddler|baby shark|cocomelon|disney junior|sing-?along|bedtime)\b", re.I)

def min_fans_for_year(year):
    """Fans an album needs to be listed. Newer albums need fewer.
    2026: 1,000 · 2025: 1,400 · 2020: 3,400 · 2010: 7,400 · 2000: 11,400 · 1990: 15,400 · 1970: 23,400 · 1950: 31,400"""
    return 1000 + 400 * (CURRENT_YEAR - year)

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
old_desc = {}          # album id → notes ('' = Wikipedia was asked and has no article; only albums never asked get a lookup)
if os.path.exists('music.json'):
    try:
        for a in json.load(open('music.json', encoding='utf-8')).get('albums', []):
            if a.get('wiki_checked') or (a.get('overview') or '').strip(): old_desc[str(a['id'])] = a.get('overview') or ''
    except Exception as e:
        print('could not read old music.json:', e)
# Enrichment that must survive without the big crawl cache (it is committed, the crawl cache is not):
# original release dates per album, subgenres per artist, Spotify ids. Loaded into the caches here, written back at the end.
ENRICH = 'cache/enrichment.json'
enrich = json.load(open(ENRICH, encoding='utf-8')) if os.path.exists(ENRICH) else {'albums': {}, 'artists': {}}
for aid, e in enrich.get('albums', {}).items():
    if aid in cache:
        for k in ('orig', 'spotify'):
            if k in e and k not in cache[aid]: cache[aid][k] = e[k]
print(f'🎵 Deezer fetch, {START_YEAR}–{CURRENT_YEAR}; {len(cache)} albums cached, {len(enrich.get("albums", {}))} enriched, {len(old_desc)} descriptions known\n')

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
for art_id, e in enrich.get('artists', {}).items():
    known.setdefault(art_id, {'name': e.get('name', ''), 'fans': e.get('fans', 0), 'genres': []})
    if 'mbtags' in e and 'mbtags' not in known[art_id]: known[art_id]['mbtags'] = e['mbtags']
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
json.dump({str(k): {**known.get(str(k), {}), 'name': v['name'], 'fans': v['fans'], 'genres': sorted(v['genres'])} for k, v in artists.items()},
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
    if not c or not c.get('release_date') or c['release_date'] > TODAY: continue      # unreleased
    year = int(c['release_date'][:4])
    if c['fans'] < min_fans_for_year(year): continue
    if 'Kids' in c['genres'] or KIDS_RE.search(c['title'] or '') or KIDS_RE.search(a['_artist']['name']): continue
    key = (a['_artist']['id'], norm_title(c['title']))
    cur = kept.get(key)
    if cur is None or c['fans'] > cache[str(cur)]['fans']: kept[key] = aid
print(f'kept: {len(kept)} after fan thresholds and dedupe\n')

# ============= 6. WIKIPEDIA DESCRIPTIONS =============
def wikipedia(title, artist):
    """Notes for an album from the Wikipedia page summary (light endpoint). Tries the usual page titles in turn.
    Returns '' when there is no article, None when Wikipedia throttled us (left unknown, retried next run)."""
    import urllib.parse
    last = artist.split()[-1].lower()
    for guess in (f'{title} ({artist} album)', f'{title} (album)', title):
        url = 'https://en.wikipedia.org/api/rest_v1/page/summary/' + urllib.parse.quote(guess.replace(' ', '_'), safe='')
        for attempt in range(3):
            try:
                throttle(1.0 / WIKI_DELAY)
                r = session.get(url, timeout=30, headers={'Accept': 'application/json'})
            except requests.RequestException:
                time.sleep(5); continue
            if r.status_code == 429:
                wait = min(int(r.headers.get('retry-after') or 15), 60)
                with _lock: wiki_state['throttled'] += 1
                time.sleep(wait); continue
            break
        else:
            return None
        if r.status_code != 200: continue
        try: d = r.json()
        except ValueError: continue
        if d.get('type') == 'disambiguation': continue
        desc = (d.get('description') or '').lower(); extract = (d.get('extract') or '').strip()
        if re.search(r'album|\bep\b|mixtape|soundtrack|compilation', desc) and (last in extract.lower() or artist.lower() in desc):
            return re.sub(r'\s*\n\s*', ' ', extract)[:1500]
    return ''

wiki_state = {'throttled': 0}
WIKI_BUDGET = int(os.environ.get('WIKI_BUDGET_MIN', 0)) * 60     # album notes are OFF by default (an album needs no synopsis); set WIKI_BUDGET_MIN to enable

# ============= 5b. ORIGINAL RELEASE DATES (MusicBrainz) =============
# Deezer dates a remaster by the remaster ("Abbey Road (Remastered 2009)" → 2015). MusicBrainz knows the original
# release date of the release group; ask once per album (1 request/second, as MusicBrainz requires) and cache it.
DISPLAY_NOISE = re.compile(r'\s*[\(\[][^)\]]*(remaster|deluxe|edition|expanded|anniversary|bonus|reissue|re-issue|super|digital|version|explicit)[^)\]]*[\)\]]', re.I)
def display_title(t):
    t = DISPLAY_NOISE.sub('', t)
    return re.sub(r'\s*[-:–]\s*(remastered|deluxe|expanded|anniversary)[^-:–]*$', '', t, flags=re.I).strip() or t
def musicbrainz_date(title, artist):
    """'YYYY-MM-DD' / 'YYYY' of the first release, '' when MusicBrainz has nothing convincing, None when the lookup failed."""
    q = f'releasegroup:"{display_title(title)}" AND artist:"{artist}" AND (primarytype:album OR primarytype:ep)'
    for attempt in range(3):
        try:
            throttle(1.0)
            r = session.get('https://musicbrainz.org/ws/2/release-group/', params={'query': q, 'fmt': 'json', 'limit': 5}, timeout=30,
                            headers={'User-Agent': 'entertainment-site/0.1 (github.com/felixavier91/entertainment)'})
            if r.status_code == 503 or r.status_code == 429: time.sleep(5 * (attempt + 1)); continue
            if r.status_code != 200: return ''
            d = r.json(); break
        except (requests.RequestException, ValueError):
            time.sleep(3)
    else:
        return None
    want = norm_title(title); dates = []
    for rg in d.get('release-groups', []):
        if int(rg.get('score', 0)) < 85: continue
        names = [c.get('artist', {}).get('name', '').lower() for c in rg.get('artist-credit', [])]
        if not any(artist.lower() in n or n in artist.lower() for n in names): continue
        if norm_title(rg.get('title', '')) != want and not rg.get('title', '').lower().startswith(display_title(title).lower()[:12]): continue
        if rg.get('first-release-date'): dates.append(rg['first-release-date'])
    return min(dates) if dates else ''          # earliest matching release group = the original

need_mb = [aid for aid in kept.values() if 'orig' not in cache[str(aid)]]
print(f'musicbrainz: {len(need_mb)} albums need an original date (budget {MB_BUDGET // 60} min)')
t_mb = time.monotonic(); mb_calls = 0
for n, aid in enumerate(need_mb):
    if time.monotonic() - t_mb > MB_BUDGET:
        print(f'  musicbrainz: budget used, {len(need_mb) - n} left for the next run'); break
    c = cache[str(aid)]
    res = musicbrainz_date(c['title'], c['artist']['name']); mb_calls += 1
    if res is None: continue
    c['orig'] = res
    if n % 100 == 99: print(f'  musicbrainz {n + 1}/{len(need_mb)}'); save_cache()
save_cache()

# ============= 5c. SUBGENRES (MusicBrainz artist tags) =============
# Deezer only has top-level genres (Rock, Pop…). MusicBrainz tags artists with real subgenres; keep the ones that are
# in MusicBrainz's own genre list (drops "seen live", "female vocals"…), top 5 by votes, cached per artist.
GENRE_LIST = 'cache/mb_genres.txt'
def mb_genres():
    if not os.path.exists(GENRE_LIST) or (date.today() - date.fromtimestamp(os.path.getmtime(GENRE_LIST))).days > 60:
        try:
            throttle(1.0)
            r = session.get('https://musicbrainz.org/ws/2/genre/all', params={'fmt': 'txt'}, timeout=60,
                            headers={'User-Agent': 'entertainment-site/0.1 (github.com/felixavier91/entertainment)'})
            if r.status_code == 200 and len(r.text) > 1000: open(GENRE_LIST, 'w', encoding='utf-8').write(r.text)
        except requests.RequestException:
            pass
    return set(l.strip().lower() for l in open(GENRE_LIST, encoding='utf-8')) if os.path.exists(GENRE_LIST) else set()
def musicbrainz_tags(name, genres_ok):
    """Top subgenres for an artist; [] when MusicBrainz has none, None when the lookup failed (retried next run)."""
    for attempt in range(3):
        try:
            throttle(1.0)
            r = session.get('https://musicbrainz.org/ws/2/artist/', params={'query': f'artist:"{name}"', 'fmt': 'json', 'limit': 3}, timeout=30,
                            headers={'User-Agent': 'entertainment-site/0.1 (github.com/felixavier91/entertainment)'})
            if r.status_code in (429, 503): time.sleep(5 * (attempt + 1)); continue
            if r.status_code != 200: return []
            d = r.json(); break
        except (requests.RequestException, ValueError):
            time.sleep(3)
    else:
        return None
    for a in d.get('artists', []):
        if int(a.get('score', 0)) < 90 or a.get('name', '').lower() != name.lower() and norm_title(a.get('name', '')) != norm_title(name): continue
        tags = sorted(a.get('tags') or [], key=lambda t: -t['count'])
        out = []
        for t in tags:
            n = t['name'].lower()
            if n in genres_ok and n not in out: out.append(n)
            if len(out) == 5: break
        return out
    return []

artist_tags = json.load(open('cache/artists.json', encoding='utf-8'))
genres_ok = mb_genres()
need_tags = sorted({str(candidates[aid]['_artist']['id']) for aid in kept.values()
                    if 'mbtags' not in artist_tags.get(str(candidates[aid]['_artist']['id']), {})})
print(f'musicbrainz tags: {len(need_tags)} artists need subgenres (budget {TAGS_BUDGET // 60} min, {len(genres_ok)} known genres)')
t_tags = time.monotonic(); tag_calls = 0
for n, art_id in enumerate(need_tags):
    if time.monotonic() - t_tags > TAGS_BUDGET:
        print(f'  musicbrainz tags: budget used, {len(need_tags) - n} artists left for the next run'); break
    entry = artist_tags.setdefault(art_id, {'name': artists[int(art_id)]['name'], 'fans': artists[int(art_id)]['fans'], 'genres': sorted(artists[int(art_id)]['genres'])})
    res = musicbrainz_tags(entry['name'], genres_ok); tag_calls += 1
    if res is None: continue
    entry['mbtags'] = res
    if n % 100 == 99:
        print(f'  musicbrainz tags {n + 1}/{len(need_tags)}')
        json.dump(artist_tags, open('cache/artists.json', 'w', encoding='utf-8'), ensure_ascii=False)
json.dump(artist_tags, open('cache/artists.json', 'w', encoding='utf-8'), ensure_ascii=False)

# ============= 5d. SPOTIFY ALBUM IDS (optional, needs a free Spotify developer app) =============
# With SPOTIFY_CLIENT_ID/SECRET set, each kept album is looked up once (client-credentials flow, no user login) so the
# card can open the album page itself instead of a search. Cached per album ('spotify': id or '' when not found).
spotify_token = None
def spotify_auth():
    global spotify_token
    r = session.post('https://accounts.spotify.com/api/token', data={'grant_type': 'client_credentials'}, auth=(SPOTIFY_ID, SPOTIFY_SECRET), timeout=30)
    r.raise_for_status(); spotify_token = r.json()['access_token']
def spotify_album_id(title, artist):
    """Spotify album id, '' when nothing convincing, None when the request failed (retried next run)."""
    q = f'album:{display_title(title)} artist:{artist}'
    for attempt in range(4):
        try:
            throttle(8.0)
            r = session.get('https://api.spotify.com/v1/search', params={'q': q, 'type': 'album', 'limit': 5}, timeout=30,
                            headers={'Authorization': f'Bearer {spotify_token}'})
            if r.status_code == 401: spotify_auth(); continue
            if r.status_code == 429: time.sleep(min(int(r.headers.get('retry-after') or 5), 60) + 1); continue
            if r.status_code != 200: return ''
            items = r.json().get('albums', {}).get('items', []); break
        except (requests.RequestException, ValueError):
            time.sleep(3)
    else:
        return None
    want = norm_title(title)
    for it in items:
        names = [a['name'].lower() for a in it.get('artists', [])]
        if not any(artist.lower() in n or n in artist.lower() for n in names): continue
        if norm_title(it['name']) == want or norm_title(it['name']).startswith(want[:15]) or want.startswith(norm_title(it['name'])[:15]):
            return it['id']
    return ''

sp_calls = 0
if SPOTIFY_ID and SPOTIFY_SECRET:
    try:
        spotify_auth()
        need_sp = [aid for aid in kept.values() if 'spotify' not in cache[str(aid)]]
        print(f'spotify: {len(need_sp)} albums need an id')
        for n, aid in enumerate(need_sp):
            c = cache[str(aid)]
            res = spotify_album_id(c['title'], c['artist']['name']); sp_calls += 1
            if res is None: continue
            c['spotify'] = res
            if n % 500 == 499: print(f'  spotify {n + 1}/{len(need_sp)}'); save_cache()
        save_cache()
    except Exception as e:
        print('spotify: skipped,', e)
else:
    print('spotify: no credentials, cards keep the search link')

albums, wiki_calls = [], 0
need = [aid for aid in kept.values() if old_desc.get(str(aid)) is None] if WIKI_BUDGET > 0 else []
print(f'wikipedia: {len(need)} albums need a lookup' if WIKI_BUDGET > 0 else 'wikipedia notes: off')
t_wiki = time.monotonic(); streak = 0
for n, aid in enumerate(need):
    if time.monotonic() - t_wiki > WIKI_BUDGET:
        print(f'  wikipedia: budget of {WIKI_BUDGET // 60} min used, {len(need) - n} albums left for the next run'); break
    if streak >= 5:
        print(f'  wikipedia: throttled {streak} times in a row, giving up for this run ({len(need) - n} albums left)'); break
    text = wikipedia(cache[str(aid)]['title'], candidates[aid]['_artist']['name']); wiki_calls += 1
    if text is None: streak += 1; continue               # throttled: leave it unknown so the next run retries
    streak = 0; old_desc[str(aid)] = text
    if n % 100 == 99: print(f'  wikipedia {n + 1}/{len(need)}')
for n, aid in enumerate(kept.values()):
    c, a = cache[str(aid)], candidates[aid]
    desc = old_desc.get(str(aid))
    checked = desc is not None
    desc = desc or ''
    gen = c['genres'] or a['_artist']['genres'][:2] or (['Reggaeton'] if a.get('genre_id') == 122 else [])
    if not gen and a.get('genre_id') in genres: gen = [genres[a['genre_id']]]
    orig = (c.get('orig') or '')
    rel = c['release_date']
    if orig and int(orig[:4]) < int(rel[:4]):
        rel = orig if len(orig) == 10 else (orig[:7] + '-01' if len(orig) == 7 else orig[:4] + '-01-01')
    albums.append({
        'id': aid,
        'title': display_title(c['title']),
        'title_full': c['title'],
        'artist': a['_artist']['name'],
        'artist_id': a['_artist']['id'],
        'overview': desc,
        'cover': c['cover'],
        'release_date': rel,
        'year': int(rel[:4]),
        'deezer_release_date': c['release_date'],
        'fans': c['fans'],
        'genres': ', '.join(gen) if gen else 'N/A',
        'subgenres': artist_tags.get(str(a['_artist']['id']), {}).get('mbtags', []),
        'spotify': c.get('spotify') or None,
        'label': c['label'],
        'tracks': c['tracks'],
        'nb_tracks': c['nb_tracks'],
        'duration': c['duration'],
        'explicit': c['explicit'],
        'contributors': c['contributors'],
        'preview': c['preview'],
        'type': c['record_type'],
        'link': f'https://www.deezer.com/album/{aid}',
        'wiki_checked': TODAY if checked else None,
    })

# write the enrichment file: everything learned about kept albums/artists that is not re-derivable from Deezer
enrich_out = {'albums': {}, 'artists': {}}
for aid in kept.values():
    c = cache[str(aid)]; e = {k: c[k] for k in ('orig', 'spotify') if k in c}
    if e: enrich_out['albums'][str(aid)] = e
for aid in kept.values():
    art_id = str(candidates[aid]['_artist']['id']); t = artist_tags.get(art_id, {})
    if 'mbtags' in t: enrich_out['artists'][art_id] = {'name': t.get('name', ''), 'fans': t.get('fans', 0), 'mbtags': t['mbtags']}
json.dump(enrich_out, open(ENRICH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))

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
print(f"📦 music.json {os.path.getsize('music.json') / 1e6:.1f} MB · cache {os.path.getsize(CACHE) / 1e6:.1f} MB · {calls} Deezer calls · {mb_calls} MusicBrainz date lookups · {tag_calls} artist tag lookups · {sp_calls} Spotify lookups · {wiki_calls} Wikipedia lookups")
print('=' * 50)
