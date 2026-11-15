# Entertainment (movies, shows, books, music, games) — Repo Guide

One static site, five media, zero ads. Everything is plain HTML + JSON on GitHub Pages
(https://felixavier91.github.io/entertainment/). Scheduled jobs refresh the JSON; the
pages just `fetch()` it and render client-side.

> **Keep this file current.** Every change to a script, workflow, filter, schedule or data
> format should update this README in the same commit. If the README and the code disagree,
> the code wins — fix the README.

---

## 1. Architecture

```
GitHub Actions (every 6 h)            data files (committed)              pages (static)
─────────────────────────────    →    ───────────────────────────    →    ──────────────────────────
fetch_data.py   (TMDB, needs key)     data.json  (movies + tv_shows)      index.html   posters + filters
fetch_books.py  (Open Library)        books.json (books)                  plots.html   full-text search
fetch_music.py  (Deezer + Wikipedia)  music.json (albums + EPs)           themes.html  themes + "For me"
fetch_games.py  (RAWG, needs key)     games.json (video games)
                                      cache/albums.json, cache/artists.json  (Deezer cache, not read by pages)
refresh_media_themes.py books|music|games   themes_<m>.json (+ labels/centroids/embeddings)

ROG laptop (hourly systemd timer)
refresh_themes.sh → refresh_themes.py themes.json, labels.json          (movie/TV themes, Ollama bge-m3)
```

The content-type pills on `index.html` (🎬 Movies · 📺 TV Shows · 📚 Books · 🎵 Music · 🎮 Games) decide
which list is shown; sort, genre, year and keyword filters work the same way for all four.

### Repo map

| Path | What it is |
|---|---|
| `index.html` | Poster grid with the filter panel. Movies/TV cards unchanged; `mediaCard()` renders book and album cards (badge = Borrow / Read free for books, ▶ Preview for albums) |
| `plots.html` | Text search over plots, book blurbs, album notes and track lists. `?q=heist&type=book` pre-fills |
| `themes.html` | Themes + "For me" per medium (🎬 Movies & Shows · 📚 Books · 🎵 Music switch at the top). Hash routes: `#books/t=3`, `#music/me`; no prefix = movies |
| `fetch_data.py` | TMDB fetcher → `data.json` (unchanged) |
| `fetch_books.py` | Open Library fetcher → `books.json` |
| `fetch_music.py` | Deezer fetcher → `music.json` + `cache/` |
| `fetch_games.py` | RAWG fetcher → `games.json` |
| `refresh_themes.py`, `refresh_themes.sh`, `cluster.py` | Movie/TV themes (run on the ROG with Ollama) |
| `refresh_media_themes.py` | Book and music themes (run in Actions with fastembed) |
| `cluster_media.py` | One-off: build the initial K themes for a medium and print a report for naming them |
| `theme_names_*.json` | Hand-written theme names (edit freely; the next refresh picks them up) |
| `.github/workflows/update-data.yml` | The scheduled job (§2) |

### Secrets

| Secret | Used by |
|---|---|
| `TMDB_TOKEN` | `fetch_data.py` |
| `RAWG_KEY` | `fetch_games.py` (free key from https://rawg.io/apidocs; the games step is skipped while the secret is missing) |
| — | Open Library, Deezer and Wikipedia need no key |

---

## 2. The scheduled job

`update-data.yml` runs every 6 hours (and on every push, but a push only redeploys — it does
not refetch). Steps, in order: `fetch_data.py` → `fetch_books.py` → `fetch_music.py` →
`fetch_games.py` (only the 00:00 UTC run, or manual runs, to stay inside RAWG's 20k/month) →
`refresh_media_themes.py` for books, music and games (each skipped until its themes exist) → commit → deploy Pages.
A `concurrency` group stops two runs from overlapping; the job may take 20–40 minutes because
of the Deezer refresh (§4).

---

## 3. Books (`fetch_books.py` → `books.json`)

**Source:** Open Library search, one query per year from 1990:
`first_publish_year:Y readinglog_count:[T TO *] language:eng`, sorted by reading-log count.
`T = 20 + 10 × (years old)` so new books need 20 readers, a 1990 book 380.

**Per book:** title, author, first publish year (+ best full date for "Most Recent"), cover id,
rating (0–5) and ratings count, `readers` (reading-log count = "Most Readers"), pages,
genres (subjects mapped through `GENRE_RULES`), `type` fiction/nonfiction (subject keywords),
`ebook` access (`public` → "Read free", `borrowable` → "Borrow") and the archive.org id.

**Description:** the Open Library work record; if that is under 60 characters, Wikipedia
(`generator=search` + `extracts`, accepted only when Wikipedia calls the page a novel/book/
memoir…); else the first sentence. Descriptions are reused from the previous `books.json`,
so a run only fetches work records for new books.

**Excluded:** anything for kids (subjects with juvenile, picture book, children, early/chapter/beginning reader, middle grade,
nursery; young adult stays), plus textbooks, study guides, workbooks, test prep, colouring/board books, dictionaries, calendars.

Thresholds and the genre mapping are at the top of the script.

---

## 4. Music (`fetch_music.py` → `music.json`, `cache/`)

**Source:** Deezer public API (no key).
1. Seeds: the album and artist charts of all 28 Deezer genres.
2. Walk "related artists" biggest-first until `MAX_ARTISTS` (2,500); artists under
   `MIN_ARTIST_FANS` (40k) are not followed. Found artists are remembered in `cache/artists.json`.
3. Every artist's albums and EPs since 1950 (`record_type` album/ep, no singles/compilations).
4. `/album/{id}` for fans, genres, label, tracks, first 30-second preview URL → `cache/albums.json`.
   Albums from the last 2 years are re-read once a day, older ones every 30 days.
5. Keep albums with `fans ≥ 1,000 + 400 × years old` (2026: 1,000 · 2010: 7,400 · 1990: 15,400 · 1950: 31,400); deluxe/remaster variants collapse into the
   most-followed one (`norm_title`). Kids music is dropped: Deezer's "Kids" genre is never seeded or kept, and
   titles/artists matching `KIDS_RE` (nursery, lullaby, CoComelon, Disney Junior, sing-along…) are skipped.
6. (Off by default.) Wikipedia album notes: `WIKI_BUDGET_MIN=25 python fetch_music.py` spends up to 25 min per run on
   the Wikipedia summary endpoint, which throttles anonymous callers hard. Albums need no synopsis, so the site
   shows the track list instead and the themes are built from artist, title, genres and track names.

**Per album:** title, artist, release date, fans (= "Most Fans"), genres (album genres, else the
artist's chart genre), label, track titles, track count, duration, explicit flag, cover, preview
MP3, Deezer link. There are no ratings, so the "Highest Rated" pill is hidden for music. No descriptions either: the card shows the track list.

The first run is slow (≈50k Deezer calls, 1–3 h). Later runs cost ~5–15k calls.

---

## 4b. Games (`fetch_games.py` → `games.json`)

**Source:** RAWG API, one query per year from 1990, ordered by `added` (users who put the game in a
library), paging until the first game under the threshold `60 + 40 × (years old)` adds, at most 10
pages (400 games) per year. DLC/editions are excluded (`exclude_additions`), as are games without
artwork, unreleased games, "Adults Only" ESRB, anything tagged or titled NSFW, and games with a RAWG
player rating under 3 unless Metacritic is 75 or higher.

**Per game:** title, release date, artwork, RAWG rating (0–5) and count, Metacritic, `added`
(= "Most Players"), average playtime, genres, platforms (+ `family`: pc/playstation/xbox/nintendo),
developer, publisher, store links, ESRB, tags, and the description from the game record (HTML
stripped, Spanish half dropped). Description and credits are reused from the previous `games.json`.

The card badge is the Metacritic score (green ≥ 75, amber ≥ 50, red below); "Highest Rated" sorts by
Metacritic, falling back to the RAWG rating.

---

## 5. Themes

All four media use the same local model and method, on the ROG: Ollama `bge-m3` on CPU (so GPU jobs are
never disturbed), unloaded right after each run. The hourly `themes-refresh.timer` runs `refresh_themes.sh`,
which pulls, runs `refresh_themes.py` (movies + TV, 60 themes in `theme_names.json`) and
`refresh_media_themes.py` for books, music and games, and commits + pushes whatever changed.
Only NEW titles are embedded; each joins the nearest existing centroid, so theme names stay stable.

Text per book = title, author, genres, blurb, subjects; per album = artist, title, year, genres,
Wikipedia notes, track list; per game = title, year, genres, tags, description.

Files per medium `M`: `theme_names_M.json` (hand-written names), `centroids_M.npy`, `mean_M.npy`,
`labels_M.json`, `themes_M.json` (what the page reads) — all tracked; `embeddings_M.npy` +
`embed_ids_M.json` are the local embedding cache (gitignored, like the movies' `embeddings.npy`; if lost,
the next run just re-reads everything).

To re-cluster a medium from scratch (new theme set), on the ROG:
```bash
rm -f theme_names_books.json labels_books.json
.venv/bin/python cluster_media.py books 40     # prints keywords + examples per cluster, writes cluster_report_books.json
# write names into theme_names_books.json (order = cluster number), then:
.venv/bin/python refresh_media_themes.py books
```

"For me" likes/dislikes live in the browser's `localStorage` (`taste`), keyed `movie:ID`,
`tv:ID`, `book:OLxxxW`, `music:ID`, `game:ID`, so the media never mix.

---

## 6. Start of a new year

Nothing to change: thresholds in all four fetchers are computed from the current year.
