#!/usr/bin/env python3
"""
RAWG data fetcher → games.json
Video games released from START_YEAR to today, ranked by how many RAWG users added them to a library
("added"). Descriptions come from each game's RAWG record. Needs a free RAWG key (https://rawg.io/apidocs)
in the RAWG_KEY environment variable; the free tier allows 20,000 requests a month, which is why the
workflow runs this once a day.

Incremental: descriptions already in games.json are reused, so a run only costs the per-year list pages
plus one request per new game.
"""
import json, os, re, sys, time
from datetime import datetime, date
import requests

sys.stdout.reconfigure(line_buffering=True)

# ============= CONFIGURATION =============
START_YEAR = 1990
CURRENT_YEAR = datetime.now().year
TODAY = date.today().isoformat()
API = 'https://api.rawg.io/api'
KEY = os.environ.get('RAWG_KEY')
if not KEY:
    raise SystemExit('RAWG_KEY environment variable not set (free key at https://rawg.io/apidocs)')
PAGE_SIZE = 40
MIN_RATING = 3.0                 # RAWG player rating (0–5) a game needs
NSFW_RE = re.compile(r'\b(nsfw|hentai|porn|erotic|sex(y|ual)?|adult only|strip)\b', re.I)
MAX_PAGES_PER_YEAR = 10          # 400 games per year at most
DELAY = 0.25

def min_added_for_year(year):
    """Library adds a game needs to be listed. Newer games need fewer.
    2026: 60 · 2025: 100 · 2020: 300 · 2010: 700 · 2000: 1,100 · 1990: 1,500"""
    return 60 + 40 * (CURRENT_YEAR - year)

FAMILY = [('nintendo', r'nintendo|wii|switch|game boy|gamecube|nes|ds\b'), ('playstation', r'playstation|ps vita|psp'),
          ('xbox', r'xbox'), ('pc', r'^pc$|macos|linux|web')]
def families(platforms):
    out = []
    for fam, pat in FAMILY:
        if any(re.search(pat, p.lower()) for p in platforms): out.append(fam)
    return out or ['other']

session = requests.Session()
session.headers['User-Agent'] = 'new-movies-and-shows (github.com/felixavier91/entertainment)'
calls = 0
def get(path, **params):
    global calls
    params['key'] = KEY
    for attempt in range(4):
        try:
            time.sleep(DELAY)
            r = session.get(API + path, params=params, timeout=60); calls += 1
            if r.status_code in (429, 502, 503, 504):
                time.sleep(10 * (attempt + 1)); continue
            if r.status_code == 404: return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f'  ! {path} {e}; retrying'); time.sleep(5 * (attempt + 1))
    return None

def clean(text):
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = re.sub(r'\s*\n\s*', ' ', text).strip()
    text = re.sub(r'\s{2,}', ' ', text)
    # RAWG often appends the Spanish translation after "Español"; keep the English half
    text = re.split(r'\bEspañol\b', text)[0].strip()
    # Steam store boilerplate ("JOIN OUR DISCORD About the Game …") and glued-together SHOUTY headers
    text = re.sub(r'^.{0,200}?\bAbout the Game\b\s*', '', text, flags=re.S)
    text = re.sub(r'^(JOIN OUR DISCORD|Wishlist now!?|Follow us on \w+)[^.!?]*[.!?]?\s*', '', text, flags=re.I)
    text = re.sub(r'(?<=[a-z.!?])([A-Z]{3,}(?: [A-Z&]{2,}){0,5})(?=[A-Z][a-z])', r' \1: ', text)
    return text.strip()[:1500]

# ============= PREVIOUS RUN =============
old = {}
if os.path.exists('games.json'):
    try:
        for g in json.load(open('games.json', encoding='utf-8')).get('games', []): old[g['id']] = g
    except Exception as e:
        print('could not read old games.json:', e)
print(f'🎮 RAWG fetch, {START_YEAR}–{CURRENT_YEAR}, {len(old)} games already known\n')

# ============= LIST, YEAR BY YEAR =============
found = {}
for year in range(START_YEAR, CURRENT_YEAR + 1):
    thr, n = min_added_for_year(year), 0
    for page in range(1, MAX_PAGES_PER_YEAR + 1):
        d = get('/games', dates=f'{year}-01-01,{year}-12-31', ordering='-added', page_size=PAGE_SIZE, page=page,
                exclude_additions='true')
        if not d: break
        below = False
        for g in d.get('results', []):
            if (g.get('added') or 0) < thr: below = True; break
            if not g.get('background_image') or (g.get('released') or '') > TODAY: continue
            esrb = (g.get('esrb_rating') or {}).get('name') or ''
            if esrb == 'Adults Only': continue
            if (g.get('rating') or 0) < MIN_RATING: continue
            if NSFW_RE.search(g.get('name') or '') or any(NSFW_RE.search(t.get('name') or '') for t in g.get('tags') or []): continue
            found[g['id']] = g; n += 1
        if below or not d.get('next'): break
    print(f'  {year}: ≥{thr} adds → {n} games')
print(f'\n✅ {len(found)} games listed ({calls} calls)\n')

# ============= DETAILS FOR NEW GAMES =============
games, fetched = [], 0
for i, (gid, g) in enumerate(found.items()):
    prev = old.get(gid)
    if prev and prev.get('overview') is not None and prev.get('developer') is not None:
        desc, dev, pub, stores, web = prev['overview'], prev['developer'], prev.get('publisher'), prev.get('stores', []), prev.get('website')
    else:
        d = get(f'/games/{gid}') or {}; fetched += 1
        desc = clean(d.get('description_raw') or d.get('description') or '')
        dev = ', '.join(x['name'] for x in (d.get('developers') or [])[:2])
        pub = ', '.join(x['name'] for x in (d.get('publishers') or [])[:2])
        web = d.get('website') or ''
        stores = []
        for st in d.get('stores') or []:
            name = (st.get('store') or {}).get('name'); url = st.get('url') or ''
            if name and url and not any(x['name'] == name for x in stores): stores.append({'name': name, 'url': url})
        if i % 50 == 0: print(f'  details {i}/{len(found)} ({fetched} fetched, {calls} calls)')
    platforms = [p['platform']['name'] for p in (g.get('platforms') or []) if p.get('platform')]
    games.append({
        'id': gid,
        'slug': g.get('slug'),
        'title': g.get('name'),
        'overview': desc,
        'image': g.get('background_image'),
        'release_date': g.get('released'),
        'year': int((g.get('released') or '0')[:4] or 0),
        'rating': g.get('rating') or None,              # RAWG users, 0–5
        'ratings_count': g.get('ratings_count') or 0,
        'metacritic': g.get('metacritic'),
        'added': g.get('added') or 0,                    # users who added it to a library = popularity
        'playtime': g.get('playtime') or 0,
        'genres': ', '.join(x['name'] for x in (g.get('genres') or [])) or 'N/A',
        'platforms': ', '.join(platforms),
        'family': families(platforms)[0],
        'families': families(platforms),
        'developer': dev, 'publisher': pub, 'website': web, 'stores': stores[:5],
        'esrb': (g.get('esrb_rating') or {}).get('name'),
        'tags': [t['name'] for t in (g.get('tags') or []) if t.get('language') == 'eng'][:12],
    })

games.sort(key=lambda g: g['release_date'] or '', reverse=True)
meta = {'generated_at': datetime.now().isoformat(), 'start_year': START_YEAR, 'end_year': CURRENT_YEAR,
        'total_games': len(games), 'source': 'RAWG (rawg.io)', 'requests': calls}
json.dump({'games': games, 'metadata': meta}, open('games.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('=' * 50)
print(f"✅ {len(games)} games · {fetched} detail records fetched · {calls} RAWG calls this run")
print(f"📦 games.json {os.path.getsize('games.json') / 1e6:.1f} MB")
print('=' * 50)
