"""Themes for books and music (the movie themes are refresh_themes.py, which runs on the ROG with Ollama).
This one runs in GitHub Actions with fastembed (BAAI/bge-small-en-v1.5, CPU, ~130 MB model), so it needs no local machine.

    python refresh_media_themes.py books      # books.json  → themes_books.json
    python refresh_media_themes.py music      # music.json  → themes_music.json

Only NEW titles are embedded; each joins the existing theme whose centroid it is closest to, so theme names stay stable.
Files per medium M (all tracked in git):
    theme_names_M.json  the names, written by hand after cluster_media.py
    centroids_M.npy, mean_M.npy  the cluster geometry from cluster_media.py
    labels_M.json       title → theme
    embeddings_M.npy (float16) + embed_ids_M.json  the embedding cache, so a run only embeds what is new
    themes_M.json       what themes.html reads
Prints 'changed' when themes_M.json was rewritten, 'no change' otherwise."""
import json, base64, os, sys, numpy as np

MODEL = 'BAAI/bge-small-en-v1.5'
D = 256

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
    else:
        raise SystemExit('medium must be books or music')
    return out

def embed(texts):
    from fastembed import TextEmbedding
    model = TextEmbedding(MODEL, threads=os.cpu_count())
    return np.array(list(model.embed(texts, batch_size=32)), dtype=np.float32)

def main(medium):
    data = json.load(open(f'{medium}.json', encoding='utf-8'))
    cur = texts_for(medium, data)
    names = json.load(open(f'theme_names_{medium}.json'))
    labels = json.load(open(f'labels_{medium}.json'))
    cent, mean = np.load(f'centroids_{medium}.npy'), np.load(f'mean_{medium}.npy')
    emb_f, ids_f = f'embeddings_{medium}.npy', f'embed_ids_{medium}.json'
    if os.path.exists(emb_f):
        ids, X = json.load(open(ids_f)), np.load(emb_f).astype(np.float32)
    else:
        ids, X = [], np.zeros((0, cent.shape[1]), np.float32)
    have = set(ids)
    new = [k for k in cur if k not in have]          # also re-reads everything if the cache was lost
    if new:
        print(f'embedding {len(new)} new titles…')
        X = np.vstack([X, embed([cur[k] for k in new])]); ids += new
        np.save(emb_f, X.astype(np.float16)); json.dump(ids, open(ids_f, 'w'))
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
