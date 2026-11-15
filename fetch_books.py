#!/usr/bin/env python3
"""
Open Library data fetcher → books.json
Books first published from START_YEAR to today, picked by how many Open Library readers
logged them (want to read / reading / read). Descriptions come from the Open Library work
record, with Wikipedia as a fallback. No API key needed.

Incremental: descriptions already in books.json are reused, so only new books cost requests.
"""
import json, os, re, sys, time
from datetime import datetime
import requests

sys.stdout.reconfigure(line_buffering=True)

# ============= CONFIGURATION =============
START_YEAR = 1990
CURRENT_YEAR = datetime.now().year
UA = 'new-books (github.com/felixavier91/new-books)'
OL = 'https://openlibrary.org'
PAGE = 100
DELAY = 0.5                     # Open Library asks for a gentle pace
WIKI_DELAY = 0.25

def min_readers_for_year(year):
    """Readers (reading-log entries) a book needs to be listed. Newer books need fewer.
    2026: 20 · 2025: 30 · 2020: 80 · 2010: 180 · 2000: 280 · 1990: 380"""
    return 20 + 10 * max(0, CURRENT_YEAR - year)

# Subjects → the genre list shown in the dropdown. Order matters: first match wins for the primary genre.
GENRE_RULES = [
    ('Graphic Novels & Comics', r'graphic novel|comics|comic books|manga|bande dessin'),
    ('Fantasy', r'\bfantasy|magic\b|wizards|dragons|epic fantasy'),
    ('Science Fiction', r'science fiction|sci-fi|space opera|time travel|dystopia|post-apocalyp'),
    ('Horror', r'\bhorror|ghost stories|vampires|zombies|supernatural'),
    ('Mystery', r'mystery|detective|whodunit|private investigators|murder'),
    ('Thriller', r'thriller|suspense|espionage|spy|crime fiction|serial killer'),
    ('Romance', r'romance|love stories|romantic'),
    ('Historical Fiction', r'historical fiction'),
    ('Young Adult', r'young adult|teen|coming of age|adolescence'),
    ('Humor', r'humor|humour|satire|comedy|comic novel'),
    ('Short Stories', r'short stories'),
    ('Poetry', r'poetry|poems'),
    ('Classics', r'classic literature|classics|literature, classic'),
    ('Literary Fiction', r'literary fiction|fiction, general|psychological fiction|domestic fiction|literary'),
    ('Biography & Memoir', r'biograph|memoir|autobiograph|diaries|personal narratives'),
    ('History', r'\bhistory\b|world war|civil war|ancient|medieval'),
    ('True Crime', r'true crime|criminals'),
    ('Science & Nature', r'\bscience\b|physics|biology|evolution|astronomy|cosmology|nature|animals|climate'),
    ('Psychology', r'psycholog|mental health|emotions|behavior|cognition|mindfulness'),
    ('Self-Help', r'self-help|self help|self-actualization|success|habits|personal development|motivation|happiness|conduct of life'),
    ('Business & Economics', r'business|economics|finance|investing|management|entrepreneur|leadership|money'),
    ('Philosophy', r'philosoph|ethics|stoicism'),
    ('Religion & Spirituality', r'religio|spiritual|christian|buddhis|bible|faith|theology'),
    ('Politics & Society', r'politic|sociology|social|feminism|race relations|government|journalism|current events'),
    ('Health & Food', r'health|nutrition|diet|cooking|cookbook|fitness|medicine'),
    ('Technology', r'computer|technology|internet|artificial intelligence|programming|software'),
    ('Travel & Adventure', r'travel|adventure|exploration|voyages|mountaineering'),
    ('Essays', r'essays'),
]
FICTION_RE = re.compile(r'fiction|novel|stories|romance|fantasy|thriller|mystery|poetry|graphic novel|comics|manga|drama|plays', re.I)
NONFICTION_RE = re.compile(r'nonfiction|non-fiction|biograph|memoir|history|science|business|self-help|psycholog|philosoph|politic|economics|essays|religio|health|cooking|true crime|travel|technology|sociology|journalism|education', re.I)
# Not interested: schoolbooks, and anything written for kids (picture books, juvenile series, middle grade). YA stays.
SKIP_RE = re.compile(r'textbook|study guide|coloring book|board book|workbook|examination|test prep|dictionar|calendar|'
                     r'juvenile|picture book|children|early reader|chapter book|middle grade|beginning reader|read-aloud|nursery', re.I)

def rated(d):
    return d or {}

session = requests.Session()
session.headers['User-Agent'] = UA

def get(url, **params):
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * (attempt + 1)); continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f'  ! {url} {e}; retrying'); time.sleep(3 * (attempt + 1))
    return None

def pick_date(doc, year):
    """Best full date for the first publication (used for 'Most Recent'); falls back to Jan 1."""
    best = None
    for s in doc.get('publish_date') or []:
        for fmt in ('%Y-%m-%d', '%B %d, %Y', '%b %d, %Y', '%d %B %Y', '%d %b %Y', '%d/%m/%Y', '%Y-%m', '%B %Y', '%b %Y'):
            try:
                d = datetime.strptime(s.strip().replace('Sept ', 'Sep '), fmt)
            except ValueError:
                continue
            if d.year == year and (best is None or d < best): best = d
            break
    return (best or datetime(year, 1, 1)).strftime('%Y-%m-%d')

def genres_from(subjects):
    text = ' | '.join(subjects).lower()
    found = [name for name, pat in GENRE_RULES if re.search(pat, text)]
    return found[:4]

def book_type(subjects):
    text = ' | '.join(subjects)
    if FICTION_RE.search(text): return 'fiction'
    if NONFICTION_RE.search(text): return 'nonfiction'
    return 'fiction'

def description_from_work(work):
    d = work.get('description')
    if isinstance(d, dict): d = d.get('value')
    if not d: return ''
    d = re.sub(r'\[source\]\[\d+\].*', '', d, flags=re.S)
    d = re.sub(r'\(\[source\]\(.*?\)\)|\[\d+\]:\s*http\S+', '', d)
    d = re.sub(r'-{3,}.*', '', d, flags=re.S)                 # OL often appends "----------\nAlso contained in:" lists
    d = re.sub(r'\s*\n\s*', ' ', d).strip()
    return d[:1500]

def wikipedia_description(title, author):
    q = f'"{title}" {author or ""} book'
    d = get('https://en.wikipedia.org/w/api.php', action='query', generator='search', gsrsearch=q, gsrlimit=1,
            prop='extracts|description', exintro=1, explaintext=1, exsentences=4, format='json', formatversion=2)
    time.sleep(WIKI_DELAY)
    pages = (d or {}).get('query', {}).get('pages') or []
    if not pages: return ''
    p = pages[0]
    kind = (p.get('description') or '').lower()
    if not re.search(r'novel|book|memoir|novella|anthology|collection|comic|manga|poetry', kind): return ''
    return (p.get('extract') or '').strip()[:1500]

# ============= LOAD PREVIOUS RUN (for descriptions) =============
old = {}
if os.path.exists('books.json'):
    try:
        for b in json.load(open('books.json', encoding='utf-8')).get('books', []): old[b['id']] = b
    except Exception as e:
        print('could not read old books.json:', e)
print(f'📚 Open Library fetch, {START_YEAR}–{CURRENT_YEAR}, {len(old)} books already known\n')

# ============= SEARCH, YEAR BY YEAR =============
FIELDS = ','.join(['key', 'title', 'author_name', 'author_key', 'first_publish_year', 'publish_date', 'cover_i',
                   'ratings_average', 'ratings_count', 'readinglog_count', 'want_to_read_count',
                   'currently_reading_count', 'already_read_count', 'subject', 'number_of_pages_median',
                   'language', 'ebook_access', 'ia', 'first_sentence', 'edition_count'])
docs = {}
for year in range(START_YEAR, CURRENT_YEAR + 1):
    thr = min_readers_for_year(year)
    q = f'first_publish_year:{year} readinglog_count:[{thr} TO *] language:eng'
    page, n = 1, 0
    while True:
        d = get(f'{OL}/search.json', q=q, sort='readinglog', limit=PAGE, page=page, fields=FIELDS)
        time.sleep(DELAY)
        if not d: break
        for doc in d.get('docs', []):
            key = doc['key'].rsplit('/', 1)[-1]
            if key not in docs: docs[key] = doc; n += 1
        if page * PAGE >= d.get('numFound', 0) or not d.get('docs'): break
        page += 1
    print(f'  {year}: ≥{thr} readers → {n} books')
print(f'\n✅ {len(docs)} works found\n')

# ============= BUILD RECORDS =============
books, skipped, fetched, wiki = [], 0, 0, 0
for i, (key, doc) in enumerate(docs.items()):
    subjects = doc.get('subject') or []
    if SKIP_RE.search(' | '.join(subjects)) or not doc.get('title'): skipped += 1; continue
    year = doc.get('first_publish_year') or 0
    prev = old.get(key)
    desc = (prev or {}).get('description')
    if desc is None or (desc == '' and (prev or {}).get('desc_checked') != CURRENT_YEAR):
        work = get(f'{OL}/works/{key}.json') or {}
        time.sleep(DELAY); fetched += 1
        desc = description_from_work(work)
        if work.get('subjects'):
            subjects = list(dict.fromkeys(subjects + work['subjects']))
        if len(desc) < 60:
            w = wikipedia_description(doc['title'], (doc.get('author_name') or [''])[0]); wiki += 1
            if len(w) > len(desc): desc = w
        if not desc and doc.get('first_sentence'):
            fs = doc['first_sentence']; desc = fs[0] if isinstance(fs, list) else str(fs)
    if prev and prev.get('subjects_full'): subjects = list(dict.fromkeys(subjects + prev['subjects_full']))
    if SKIP_RE.search(' | '.join(subjects)): skipped += 1; continue
    if i % 100 == 0: print(f'  building {i}/{len(docs)} (work records fetched: {fetched}, wikipedia: {wiki})')
    genres = genres_from(subjects)
    books.append({
        'id': key,
        'title': doc['title'],
        'author': ', '.join((doc.get('author_name') or ['Unknown'])[:3]),
        'author_key': (doc.get('author_key') or [None])[0],
        'overview': desc,
        'desc_checked': CURRENT_YEAR,
        'cover_id': doc.get('cover_i'),
        'year': year,
        'release_date': pick_date(doc, year) if year else None,
        'rating': round(doc['ratings_average'], 2) if doc.get('ratings_average') else None,
        'ratings_count': doc.get('ratings_count') or 0,
        'readers': doc.get('readinglog_count') or 0,
        'want_to_read': doc.get('want_to_read_count') or 0,
        'already_read': doc.get('already_read_count') or 0,
        'pages': doc.get('number_of_pages_median'),
        'editions': doc.get('edition_count'),
        'genres': ', '.join(genres) if genres else 'N/A',
        'subjects_full': subjects[:40],
        'type': book_type(subjects),
        'ebook': doc.get('ebook_access') or 'no_ebook',   # public | borrowable | printdisabled | no_ebook
        'ia': (doc.get('ia') or [None])[0],
    })

books.sort(key=lambda b: (b['release_date'] or ''), reverse=True)
meta = {
    'generated_at': datetime.now().isoformat(),
    'start_year': START_YEAR, 'end_year': CURRENT_YEAR,
    'total_books': len(books),
    'fiction': sum(b['type'] == 'fiction' for b in books),
    'nonfiction': sum(b['type'] == 'nonfiction' for b in books),
    'source': 'Open Library (openlibrary.org) + Wikipedia for missing descriptions',
}
json.dump({'books': books, 'metadata': meta}, open('books.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('=' * 50)
print(f"✅ {len(books)} books ({meta['fiction']} fiction, {meta['nonfiction']} nonfiction), {skipped} skipped")
print(f"📦 books.json {os.path.getsize('books.json') / 1e6:.1f} MB · work records fetched {fetched} · wikipedia lookups {wiki}")
print('=' * 50)
