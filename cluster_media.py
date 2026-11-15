"""One-off: group a medium's titles into K themes and print what each one is about, so the themes can be named by hand.
    python cluster_media.py books 40
Writes centroids_M.npy, mean_M.npy, labels_M.json, the embedding cache, a placeholder theme_names_M.json (if none exists)
and cluster_report_M.json (keywords + examples per cluster, for naming). Rerunning renames nothing: edit theme_names_M.json."""
import json, os, sys, numpy as np
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import CountVectorizer
from refresh_media_themes import texts_for, embed

medium = sys.argv[1]
K = int(sys.argv[2]) if len(sys.argv) > 2 else 40
data = json.load(open(f'{medium}.json', encoding='utf-8'))
cur = texts_for(medium, data)
emb_f, ids_f = f'embeddings_{medium}.npy', f'embed_ids_{medium}.json'
if os.path.exists(emb_f):
    ids, X = json.load(open(ids_f)), np.load(emb_f).astype(np.float32)
else:
    ids, X = [], np.zeros((0, 384), np.float32)
new = [k for k in cur if k not in set(ids)]
if new:
    print(f'embedding {len(new)} titles…')
    X = np.vstack([X, embed([cur[k] for k in new])]); ids += new
    np.save(emb_f, X.astype(np.float16)); json.dump(ids, open(ids_f, 'w'))
keep = [i for i, k in enumerate(ids) if k in cur]
ids = [ids[i] for i in keep]; X = X[keep]
Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
mean = Xn.mean(0)
Xc = Xn - mean; Xc /= np.linalg.norm(Xc, axis=1, keepdims=True)
km = KMeans(K, n_init=5, random_state=0).fit(Xc)
cent = km.cluster_centers_ / np.linalg.norm(km.cluster_centers_, axis=1, keepdims=True)
np.save(f'centroids_{medium}.npy', cent); np.save(f'mean_{medium}.npy', mean)
json.dump({k: int(l) for k, l in zip(ids, km.labels_)}, open(f'labels_{medium}.json', 'w'), separators=(',', ':'))
if not os.path.exists(f'theme_names_{medium}.json'):
    json.dump([f'Theme {k}' for k in range(K)], open(f'theme_names_{medium}.json', 'w'), indent=0)

by = {}
if medium == 'books':
    for b in data['books']: by[f"book:{b['id']}"] = (b['title'], b.get('author', ''), b.get('readers', 0))
else:
    for a in data['albums']: by[f"music:{a['id']}"] = (a['title'], a['artist'], a.get('fans', 0))
cv = CountVectorizer(stop_words='english', min_df=3, ngram_range=(1, 2), max_features=30000)
C = cv.fit_transform([cur[k] for k in ids]); vocab = np.array(cv.get_feature_names_out())
tot = np.asarray(C.sum(0)).ravel() + 1
report = []
for k in range(K):
    idx = np.where(km.labels_ == k)[0]
    tf = np.asarray(C[idx].sum(0)).ravel()
    score = (tf / max(tf.sum(), 1)) * np.log(1 + C.shape[0] / tot) * np.log(1 + tf)
    words = [str(w) for w in vocab[np.argsort(-score)[:12]]]
    pop = sorted(idx, key=lambda j: -by[ids[j]][2])[:8]
    cen = Xc[idx] @ cent[k]
    central = [by[ids[j]][:2] for j in idx[np.argsort(-cen)[:6]]]
    report.append({'k': k, 'n': int(len(idx)), 'keywords': words,
                   'popular': [f'{by[ids[j]][0]} ({by[ids[j]][1]})' for j in pop],
                   'central': [f'{t} ({a})' for t, a in central]})
    print(f"{k} n={len(idx)} | {', '.join(words[:8])} | {'; '.join(report[-1]['popular'][:5])}")
json.dump(report, open(f'cluster_report_{medium}.json', 'w'), indent=1, ensure_ascii=False)
