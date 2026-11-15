"""Themes for books, music and games, the same way refresh_themes.py does movies: the local model (Ollama bge-m3, on CPU
so it never disturbs GPU jobs) reads each new title once, and the title joins the existing theme whose plots it is
closest to, so theme names stay stable. Runs hourly on the ROG from refresh_themes.sh.

    python refresh_media_themes.py books      # books.json  → themes_books.json
    python refresh_media_themes.py music      # music.json  → themes_music.json
    python refresh_media_themes.py games      # games.json  → themes_games.json

Files per medium M:
    theme_names_M.json  the names, written by hand after cluster_media.py          (tracked)
    centroids_M.npy, mean_M.npy  the cluster geometry from cluster_media.py       (tracked)
    labels_M.json       title → theme                                              (tracked)
    themes_M.json       what themes.html reads                                     (tracked)
    embeddings_M.npy + embed_ids_M.json  the embedding cache (local only, like the movies' embeddings.npy)
Prints 'changed' when themes_M.json was rewritten, 'no change' otherwise."""
import json, base64, os, sys, urllib.request, numpy as np

D = 256
OLLAMA = 'http://localhost:11434'
MODEL = 'bge-m3'

def post(path, body):
    req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=900))

def embed(texts):
    out = []
    for i in range(0, len(texts), 16):
        out += post('/api/embed', {'model': MODEL, 'input': texts[i:i+16], 'keep_alive': '1m', 'options': {'num_gpu': 0}})['embeddings']
        if i and i % 800 == 0: print(f'  embedded {i}/{len(texts)}', flush=True)
    post('/api/generate', {'model': MODEL, 'keep_alive': 0})      # unload right away
    return np.array(out, dtype=np.float32)

def texts_for(medium, data):
    """key → text the model reads. Items without enough text are left out (they still show on the posters page)."""
    out = {}
    if medium == 'books':
        for b in data['books']:
            desc = (b.get('overview') or '').strip()
            subj = ', '.join((b.get('subjects_full') or [])[:15])
            if len(desc) < 40 and len(subj) < 40: continue
            out[f"book:{b['id']}"] = f"{b['title']} by {b.get('author', '')}. {b.get('genres', '')}. {desc} {subj}"[:2000]
    elif medium == 'music':
        for a in data['albums']:
            desc = (a.get('overview') or '').strip()
            tracks = ' · '.join(a.get('tracks') or [])
            if not desc and len(tracks) < 20: continue
            out[f"music:{a['id']}"] = f"{a['artist']} – {a['title']} ({a.get('year', '')}). {a.get('genres', '')}. {desc} Tracks: {tracks}"[:2000]
    elif medium == 'games':
        for g in data['games']:
            desc = (g.get('overview') or '').strip()
            if len(desc) < 40: continue
            out[f"game:{g['id']}"] = f"{g['title']} ({g.get('year', '')}). {g.get('genres', '')}. {', '.join(g.get('tags') or [])}. {desc}"[:2000]
    else:
        raise SystemExit('medium must be books, music or games')
    return out

def load_cache(medium, dim=None):
    emb_f, ids_f = f'embeddings_{medium}.npy', f'embed_ids_{medium}.json'
    if os.path.exists(emb_f):
        return json.load(open(ids_f)), np.load(emb_f).astype(np.float32)
    return [], np.zeros((0, dim or 1024), np.float32)

def save_cache(medium, ids, X):
    np.save(f'embeddings_{medium}.npy', X); json.dump(ids, open(f'embed_ids_{medium}.json', 'w'))

def main(medium):
    data = json.load(open(f'{medium}.json', encoding='utf-8'))
    cur = texts_for(medium, data)
    names = json.load(open(f'theme_names_{medium}.json'))
    labels = json.load(open(f'labels_{medium}.json'))
    cent, mean = np.load(f'centroids_{medium}.npy'), np.load(f'mean_{medium}.npy')
    ids, X = load_cache(medium, cent.shape[1])
    have = set(ids)
    new = [k for k in cur if k not in have]          # also re-reads everything if the cache was lost
    if new:
        print(f'embedding {len(new)} new titles…')
        X = np.vstack([X, embed([cur[k] for k in new])]); ids += new
        save_cache(medium, ids, X)
    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Xc = Xn - mean
    unl = [i for i, k in enumerate(ids) if k not in labels]
    if unl:
        v = Xc[unl] / np.linalg.norm(Xc[unl], axis=1, keepdims=True)
        for i, th in zip(unl, np.argmax(v @ cent.T, 1)): labels[ids[i]] = int(th)
        json.dump(labels, open(f'labels_{medium}.json', 'w'), separators=(',', ':'))
    keep = [i for i, k in enumerate(ids) if k in cur]
    out_ids = [ids[i] for i in keep]
    out_f = f'themes_{medium}.json'
    old = json.load(open(out_f)) if os.path.exists(out_f) else {}
    if not new and not unl and set(old.get('ids', [])) == set(out_ids) and old.get('themes') == names:
        print('no change'); return
    P = Xc[keep]
    d = min(D, P.shape[0], P.shape[1])
    _, _, Vt = np.linalg.svd(P, full_matrices=False)
    P = P @ Vt[:d].T
    P /= np.linalg.norm(P, axis=1, keepdims=True)
    q = np.clip(np.round(P * 127 / np.abs(P).max()), -127, 127).astype(np.int8)
    json.dump({'themes': names, 'ids': out_ids, 'theme': [labels[k] for k in out_ids], 'dim': d,
               'vec': base64.b64encode(q.tobytes()).decode()}, open(out_f, 'w'), separators=(',', ':'))
    print(f'changed: {len(new)} new, {len(out_ids)} titles')
    for k in new[:40]: print('  +', k, '->', names[labels[k]])

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'books')
