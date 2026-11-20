#!/usr/bin/env python3
"""Turn the local lyrics cache (cache/lyrics.sqlite, see fetch_lyrics.py) into the search index the site reads: lyrics/.

The index holds no lyric text. Every pair of neighbouring words in a song is hashed (crc32 of "word1 word2"); the index
maps hash -> songs. The page hashes the typed words the same way and keeps the songs that have every pair.

    lyrics/meta.json    {"v": build stamp, "albums": [album ids; a song's album is a position in this list], ...}
    lyrics/s/XXX.bin    4096 shards by the top 12 bits of the hash. Entries sorted by key:
                        u16 key (hash bits 4..19) | varint count | varint deltas of the sorted songs
                        song = album position * 256 + track number (0-based, 255 = "255 or later")

    .venv/bin/python build_lyrics_index.py [--min-hours N]   # N: leave an index younger than N hours alone

Prints "changed: ..." when it wrote a new index, else "unchanged".
"""
import json, os, shutil, sqlite3, sys, time, unicodedata, zlib
import numpy as np
from fetch_lyrics import key

OUT = 'lyrics'
APOS = "'’`ʼ"

def words(text):
    out = []
    for ch in unicodedata.normalize('NFD', text.lower()):
        c = unicodedata.category(ch)[0]
        if c in 'LN': out.append(ch)
        elif c == 'M' or ch in APOS: continue
        else: out.append(' ')
    return ''.join(out).split()

def varints(nums, buf):
    for n in nums:
        while n > 127:
            buf.append((n & 127) | 128); n >>= 7
        buf.append(n)

def main():
    min_hours = float(sys.argv[sys.argv.index('--min-hours') + 1]) if '--min-hours' in sys.argv else 0
    albums = json.load(open('music.json'))['albums']
    db = sqlite3.connect('cache/lyrics.sqlite')
    n_lyrics = db.execute("SELECT count(*) FROM lyrics WHERE text <> ''").fetchone()[0]
    sig = f'{zlib.crc32(json.dumps([[a["id"], a.get("tracks")] for a in albums]).encode())}:{n_lyrics}'
    try: old = json.load(open(f'{OUT}/meta.json'))
    except Exception: old = {}
    if old.get('sig') == sig or (old and time.time() - old.get('built', 0) < min_hours * 3600):
        print('unchanged'); return
    lyr = dict(db.execute("SELECT k, text FROM lyrics WHERE text <> ''"))
    hs, ps = [], []; songs = 0
    for pos, a in enumerate(albums):
        for ti, t in enumerate(a.get('tracks') or []):
            text = lyr.get(key(a['artist'], t))
            if not text: continue
            w = words(text)
            if len(w) < 2: continue
            h = np.unique(np.fromiter((zlib.crc32(f'{w[i]} {w[i + 1]}'.encode()) for i in range(len(w) - 1)), dtype=np.uint32, count=len(w) - 1))
            hs.append(h); ps.append(np.full(len(h), pos * 256 + min(ti, 255), dtype=np.uint32)); songs += 1
    if not hs: print('unchanged'); return
    h = np.concatenate(hs); p = np.concatenate(ps)
    order = np.lexsort((p, h >> 4)); h = h[order]; p = p[order]      # hashes that share their top 28 bits share an entry
    tmp = OUT + '.new'; shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp + '/s')
    bounds = np.searchsorted(h >> 20, np.arange(4097))
    total = 0
    for s in range(4096):
        a, b = bounds[s], bounds[s + 1]
        buf = bytearray()
        if b > a:
            k = ((h[a:b] >> 4) & 0xFFFF).tolist(); pp = p[a:b].tolist(); i = 0
            while i < len(k):
                j = i
                while j < len(k) and k[j] == k[i]: j += 1
                run = sorted(set(pp[i:j]))
                buf += k[i].to_bytes(2, 'little')
                varints([len(run), run[0]] + [run[x] - run[x - 1] for x in range(1, len(run))], buf)
                i = j
        open(f'{tmp}/s/{s:03x}.bin', 'wb').write(buf); total += len(buf)
    stamp = time.strftime('%Y%m%d%H%M')
    json.dump({'v': stamp, 'built': int(time.time()), 'sig': sig, 'songs': songs, 'albums': [a['id'] for a in albums]},
              open(f'{tmp}/meta.json', 'w'), separators=(',', ':'))
    shutil.rmtree(OUT, ignore_errors=True); os.rename(tmp, OUT)
    print(f'changed: {songs} songs with lyrics, {len(h)} word pairs, {total / 1e6:.1f} MB')

if __name__ == '__main__':
    main()
