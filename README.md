# RDC Fiscal Reference Assistant

A multi-source, citation-first RAG assistant for fiscal, legal, accounting,
customs, monetary, and employment-reference documents in the `Scapper`
repository.

The assistant does **not** train a model on the repository. It extracts
page-aware text and maintains two retrieval paths:

- **Primary:** OpenAI hosted vector store and File Search.
- **Fallback:** a local SQLite FTS5 index retrieves passages, then OpenRouter
  sends only those passages to `openai/gpt-5.6-terra` for the answer.

With `FISCAL_RAG_PROVIDER=auto`, OpenAI is used whenever its key and hosted
index are ready. If OpenAI is unavailable or its request fails, the server
temporarily switches to OpenRouter and the local index.

## Included sources

- AWA-Afrika
- Banque Centrale du Congo (BCC)
- Direction Générale des Impôts (DGI)
- DGRAD
- Ministère des Finances
- ONEC-RDC
- ONEM
- CNSS (Caisse Nationale de Sécurité Sociale)
- LegaNews (veille législative — texte + annexes PDF)
- Scribd (Journaux Officiels)
- Leganet.cd (Journaux Officiels et législation)
- Droit Congolais (droitcongolais.info — codes, lois, jurisprudence par matière)
- ANICNS (Agence Nationale de l'Industrie du Cobalt et autres Substances Minérales Stratégiques)
- Congo Mines (congomines.org)
- FAOLEX (FAO — agriculture, ressources naturelles, environnement)
- Lextenso (Journaux Officiels et textes légaux)
- NATLEX (OIT/ILO — droit du travail et sécurité sociale)
- Government & Public Affairs (external Rikolto document library)

The external Government & Public Affairs source includes PDF, DOCX, XLSX,
XLS, CSV, and TXT files. Images, videos, archives, scripts, and logs are
intentionally excluded from the evidence index.

For privacy, the complete external source is inventoried but flagged
`requires_review` and skipped by extraction/upload by default. Some filenames
indicate identifiable individuals, employee, NIF, CNSS, or operational tax
data. Review the contents, your authorization to process them, and your
organization's data policy before opting in:

```powershell
python cli.py prepare --source government_public_affairs --include-review-required
python cli.py index --source government_public_affairs --include-review-required
```

Edit [`config/sources.json`](config/sources.json) to add another folder or
catalog. Paths may be relative to the parent `Scapper` repository or absolute.
Folders can be nested. CSV catalogs are optional; when present, common title,
URL, category, filename, and date columns are detected automatically. Use an
`extensions` list on a source to opt into non-PDF document types.

### Adding a new source, step by step

This is the exact sequence used to bring a new scraper's output online (the
`cnss` source was added this way):

1. **Place the scraped files** under a new folder directly inside `Scapper/`
   (a sibling of `awa_scraper/`), e.g. `Scapper/cnss/downloads/`. PDFs can be
   organized into category subfolders (`downloads/lois-et-decrets/`,
   `downloads/communiques/`, ...) — the scan is recursive. If the scraper also
   saves its own `.txt` sidecars (OCR/extraction byproducts, not a distinct
   source of truth), leave the source's `extensions` at the default `[".pdf"]`
   so they aren't double-indexed as separate documents.

2. **(Optional) Point at a catalog CSV** if the scraper produced one with
   per-document metadata. `catalog.py` auto-detects common column names —
   `full_name`/`website_title`/`title`/`name` for the title, `pdf_filename`
   for the matching PDF, plus category/date/URL columns when present. This is
   what supplies each citation's real, clickable `source_url` (the online
   original), not just the local file path.

3. **Add an entry to `config/sources.json`**:

   ```json
   {
     "id": "cnss",
     "label": "CNSS",
     "description": "Caisse Nationale de Sécurité Sociale : lois et décrets, communiqués, formulaires employeur/travailleur et publications",
     "paths": ["cnss/downloads"],
     "catalogs": ["cnss/output/text_name_mapping_cnss.csv"],
     "enabled": true
   }
   ```

   `id` must be unique and stable — it's used as the source filter key
   throughout the app and API. For a source needing privacy review before any
   document is uploaded (personal data, internal-only files), follow the
   `government_public_affairs` pattern instead: add `"review_all": true` and
   list the extensions that need review under `"review_extensions"`.

4. **Bring it online** (from the `fiscal_rag` directory):

   ```powershell
   # Discover the new documents (also runs automatically at the start of prepare/index)
   python cli.py scan

   # Extract text, with OCR for scanned pages
   python cli.py prepare --ocr --source cnss --workers 4

   # Index to both the OpenAI hosted store and the local fallback
   python cli.py index --source cnss --workers 4

   # Confirm it's picked up
   python cli.py status
   ```

   `--source` can be repeated to bring up several new sources in one command.
   Scope every command to the new source id while testing — indexing the
   whole corpus by accident is slow and unnecessary for a one-source addition.

5. **Restart `server.py`.** The source list, like all of `Settings`, is read
   once at process startup — a running server won't see a newly-added source
   (or filter it from `/api/status`) until it restarts. Static frontend files
   (`static/*`) are served fresh from disk on every request and never need a
   restart.

### Removing a source

There is no single `cli.py` command that purges a source in one step; which
approach to use depends on whether the goal is to stop it growing or to
actually remove its documents from search.

**To stop a source from being discovered or shown** (the common case — e.g.
a scraper is being retired, or content shouldn't be offered to users
anymore), set it to disabled in `config/sources.json` and restart the server:

```json
{ "id": "cnss", ..., "enabled": false }
```

This is quick and reversible. `scan()` will no longer discover new documents
for it, and it disappears from the sidebar's source filter. **It does not by
itself remove documents already indexed** — a disabled source's existing
records are simply left alone (not marked absent), so anything already
uploaded to the OpenAI vector store or written into the local SQLite index
stays searchable until a full rebuild.

**To actually remove its documents from both indexes**, remove its entry
from `config/sources.json` (and delete or move its folder, so nothing
resurrects it on the next scan), then rebuild both indexes from scratch so
the removed documents are no longer discovered and get dropped:

```powershell
# Stop the server first - it holds the local index file open
Remove-Item "$env:LOCALAPPDATA\RAF_Fiscal_Assistant\local_retrieval.sqlite3*"
python cli.py index-local
```

For the **OpenAI hosted vector store**, note that this local rebuild does
**not** retroactively delete the removed source's files from OpenAI's side —
`index()` only deletes a remote file when it's replacing that same document
with a changed version, not when a document stops being discovered at all.
Removing a source's files from the OpenAI vector store itself currently
requires either deleting them individually via the OpenAI API/dashboard
(`state.json`'s `openai_file_id` per document, before you remove its
records) or recreating the vector store and re-running `python cli.py index`
against the smaller, updated source list. Treat this step deliberately —
deleting from a live vector store is not easily reversible.

## First-time setup on Windows

1. Double-click `Setup Fiscal Assistant.cmd`.
2. Open the generated `.env` file and add one or both keys:

   ```text
   OPENAI_API_KEY=your-openai-key
   OPENROUTER_API_KEY=your-openrouter-key
   ```

   The key is read only by Python. It is excluded from source control and is
   never included in the browser JavaScript. Because this project is inside a
   OneDrive folder, use a user-level environment variable instead of `.env` if
   organizational policy prohibits syncing API credentials.

3. Test five documents in the local fallback index before starting the full index:

   ```powershell
   .\.venv\Scripts\python.exe cli.py index-local --limit 5
   ```

4. Build every available index. Without a funded OpenAI key, this still keeps
   the local/OpenRouter fallback usable:

   ```powershell
   .\.venv\Scripts\python.exe cli.py index
   ```

5. Double-click `Open Fiscal Assistant.cmd` and verify the retrieved citations.

The process saves progress after every document and is safe to restart.

## Provider configuration

```text
FISCAL_RAG_PROVIDER=auto
FISCAL_RAG_FALLBACK_ENABLED=true
FISCAL_RAG_HISTORY_ENABLED=true
FISCAL_RAG_DATA_DIR=%LOCALAPPDATA%\RAF_Fiscal_Assistant
FISCAL_RAG_OPENAI_MODEL=gpt-5.6-terra
FISCAL_RAG_OPENROUTER_MODEL=openai/gpt-5.6-terra
FISCAL_RAG_OPENROUTER_DATA_COLLECTION=deny
```

Provider modes are `auto`, `openai`, and `openrouter`. `auto` is recommended.
OpenAI failures open a five-minute circuit before the server retries the
primary provider. Set `FISCAL_RAG_OPENAI_RETRY_SECONDS` to change this delay.

`FISCAL_RAG_OPENROUTER_DATA_COLLECTION=deny` restricts OpenRouter routing to
providers that do not collect data. `FISCAL_RAG_OPENROUTER_ZDR=true` can impose
the stricter zero-data-retention filter, but may reduce provider availability.
The generated state and SQLite files use `%LOCALAPPDATA%` because mutable
databases and atomic state files can hang inside a OneDrive placeholder folder.

## Local chat history

Conversation history is enabled with `FISCAL_RAG_HISTORY_ENABLED=true` and is
stored in `%LOCALAPPDATA%\RAF_Fiscal_Assistant\chat_history.sqlite3`. The local
database contains the user's questions, generated answers, model/provider
labels, and citation references. Retrieved context passages are not copied into
the history database. Conversations can be reopened or permanently deleted
from the History section in the application sidebar. Set the option to `false`
and restart the server to disable history persistence.

## Commands

```powershell
# Discover all configured PDFs; no extraction and no API calls
python cli.py scan

# Extract page-aware text locally; no API calls
python cli.py prepare

# Try OCR for pages with little embedded text (requires Tesseract + fra/eng data)
powershell -ExecutionPolicy Bypass -File .\setup_ocr_languages.ps1
python cli.py prepare --ocr

# Prepare and upload changed documents
python cli.py index

# Build only the private local retrieval index used by OpenRouter
python cli.py index-local

# Small end-to-end indexing test
python cli.py index --limit 5

# Index one institution first (source IDs are defined in config/sources.json)
python cli.py index --source dgi

# Rebuild the structured legal graph (instrument-level, then article-level)
python cli.py legal-build
python cli.py article-graph-build

# Preview which documents are redundant copies of the same instrument (safe, no changes)
python cli.py dedupe

# Actually mark duplicates and delete their OpenAI files/local index rows
python cli.py dedupe --apply

# Display inventory/index status
python cli.py status

# Test OpenRouter safely with fabricated content only
python scripts/smoke_openrouter_fallback.py

# Start the web application
python server.py
```

## Updating the knowledge base

After any scraper downloads new or changed PDFs, run `cli.py index` again.
Unchanged extracted and indexed documents are skipped. The local fallback is
built first. When OpenAI is configured, changed documents are then uploaded;
only after a replacement succeeds does the application try to delete the
previous remote file.

The local state, extracted text, and SQLite retrieval database live under
`%LOCALAPPDATA%\RAF_Fiscal_Assistant` by default. They are generated artifacts,
not source files. The OpenAI vector store remains available until it is deleted
through the OpenAI platform or API.

### Deduplicating the corpus

The same real-world law is routinely scraped by several sources (droitcongolais,
leganews, leganet, dgrad, ...), and heavy duplication dilutes `file_search`
ranking - a genuinely relevant document can lose to a dozen near-identical
copies of itself competing for the same result slots. `python cli.py dedupe`
groups documents that are the same instrument, keeps one canonical copy (source
priority - official/primary sources first - then extraction quality), and
removes the rest from the local index and the OpenAI vector store.

Grouping is title-based first (via the same instrument-identity parser used by
the legal graph) but is **not** applied on title alone: many implementing
decrees open their title with a citation to the base law they apply ("Loi n
015/2002 ... Code du Travail, specialement en son article 169 ; Vu ..."), so
title agreement alone would merge distinct documents. A second, content-based
gate (word-shingle similarity, comparing each document's text from its first
"Article" onward) confirms two candidates are actually the same writing before
either is touched - calibrated to lean toward under-merging over over-merging,
since an incorrect merge permanently deletes a document while an incorrect
exclusion only leaves harmless redundancy in place.

`--apply` is required to actually change anything; without it, `dedupe` only
previews the groups it would act on - review that preview before running
`--apply`, since it deletes OpenAI files. Marked duplicates get a
`duplicate_of` field (not `present: false`) so `scan()` never resurrects them,
and the canonical record gains a `mirrors` list of the other sources/URLs the
same instrument is still available from, which citations surface as "also
available via ...".

Run it after `scan`/`index`, before relying on retrieval quality for a topic
you know has duplicate-heavy sources.

### Topic-triggered focused reading

`file_search` ranking can still fail to surface a genuinely relevant law even
after deduplication, when the question never names the law itself - "quelles
sont les obligations fiscales d'une ASBL" never says "Loi 004/2001". For a
question that names a specific instrument, `resolve_named_instrument_text()`
(in `retrieval.py`) already reads that instrument's text directly instead of
relying on corpus-wide ranking; `_TOPIC_INSTRUMENTS` in the same file extends
this to a small, hand-maintained list of `{trigger keywords -> instrument}`
mappings for known cases where a common question never names the law that
answers it. A candidate still has to clear a content check (the instrument's
own text must mention the topic keyword many times in its body, not once or
zero, which is how a mistitled document - see the dedupe section above - gets
excluded) before it is trusted. Add an entry here when a real question is
confirmed, by testing, to consistently fail to retrieve a law that directly
answers it.

## Structured legal knowledge graph

Beyond retrieval, the assistant maintains a deterministic (non-LLM) index of
legal instruments and their relationships, parsed from titles using this
corpus's consistent `Type n° X du DATE ...` naming:

- **Instruments**: every document whose title identifies a Loi, Ordonnance-loi,
  Décret, Arrêté, Convention, etc. becomes one row, keyed by (type, normalized
  number) so the same text scraped by several sources (awa, leganet,
  droitcongolais, natlex, ...) collapses into a single entry.
- **Relationships**: title phrases such as *"modifiant"*, *"complétant"*,
  *"abrogeant"*, *"remplaçant"*, *"portant ratification de"* become MODIFIES /
  COMPLEMENTS / REPEALS / REPLACES / RATIFIES / IMPLEMENTS / EXTENDS /
  DEROGATES edges to the instrument they name — including instruments only
  *referenced* by another title, never themselves found in the corpus (kept as
  stub entries so the graph still names them).
- **Partial vs. whole-instrument repeal**: "abrogeant l'article 321 de la loi
  n° 73-021" is recognized as repealing one article, not the whole law — only
  a whole-instrument repeal flips an instrument's status to "Abrogé". This
  distinction matters: a foundational law with one repealed article is very
  different from a repealed law, and conflating them would make the assistant
  confidently wrong.

Before answering, the assistant scans the question for instrument references
(e.g. "l'ordonnance-loi n°69/009") and, on a match, injects a **Structured
legal index lookup** block reporting a deterministic status — 🟡 presumed in
force in corpus / 🔵 modified / 🔴 repealed / ⚪ referenced but not indexed —
plus every known modifying or repealing text. This is the "legal validity
check" step: a non-generative signal the model must lead with, separate from
(and never a substitute for) the retrieved passage it still must cite.

Rebuild the graph after any indexing run (this happens automatically at the
end of `scan`/`prepare`/`index`, or run it standalone):

```powershell
python cli.py legal-build

# Look up one instrument's deterministic status
python cli.py legal-status "ordonnance-loi 69/009"
```

### Article-level relationships

The same idea extends one level deeper: which specific *article* modifies,
repeals, or inserts which specific *article* — not just "this law amends
that law" but "article 2 of this ordonnance-loi supprime article 44 of that
one." This is parsed deterministically from citation sentences found inside
already-chunked article text (reusing the per-chunk `article_number` the
citation-precision chunking already tags), covering three real surface
forms: the passive dispositif ("L'article N de l'Ordonnance-loi n° X ... est
modifié"), the parenthetical amendment note ("(modifié et complété par
l'article N du Décret n° Y)"), and insertion formulas ("il est inséré les
articles 68bis, 68ter, ...").

Build it **after** `legal-build` (it depends on fresh instrument rows) and
after the local index is up to date (it reads chunk text from
`local_retrieval.sqlite3`):

```powershell
python cli.py article-graph-build
```

This runs automatically immediately after every `legal-build`, including the
one at the end of `scan`/`prepare`/`index` — this ordering matters more than
it looks: `legal_article`'s link to its instrument is `ON DELETE SET NULL`
against `legal_instrument`, and `legal-build` always fully deletes and
reinserts `legal_instrument` rows. Running `legal-build` a second time
*without* immediately re-running `article-graph-build` silently nulls out
every article-level link — which is exactly why the two are now always
chained together rather than left as two commands to remember to run in
order.

When a question names both an instrument and an article number, the
assistant injects a second, article-level "Structured legal index lookup"
block alongside the instrument-level one, reporting the same kind of
deterministic status at article granularity.

Both graph layers reason only about what's stated in indexed text — always
confirm substantive rule content (rates, thresholds, deadlines) against the
retrieved passage itself, not the graph alone.

## Evidence and safety rules

The server instructs the model to:

- search indexed sources before substantive answers;
- cite document title and original PDF page;
- never treat a catalog/server timestamp as an effective date;
- identify conflicting sources and uncertain legal force;
- say when the repository does not contain sufficient evidence;
- remind the user to verify high-stakes conclusions with the competent
  authority or a qualified adviser.

Scanned PDFs with insufficient text are marked `needs_ocr` and are not uploaded
as reliable evidence until OCR produces enough text.

On Windows, `setup_ocr_languages.ps1` copies the installed English model and
downloads the official Tesseract `tessdata_fast` French model into
`%LOCALAPPDATA%\RAF_Fiscal_Assistant\tessdata`. The OCR command validates both
models before processing begins, so a missing language fails once with a clear
message instead of repeating an error for every page.

## Performing good OCR

`cli.py prepare --ocr` only attempts OCR on pages with too little embedded
text to trust — it does not blindly re-OCR every page of every document, so
it's normally safe to just add `--ocr` to a routine `prepare`/`index` run.

**Language setup (once per machine)**: run
`setup_ocr_languages.ps1` before the first OCR pass. `FISCAL_RAG_OCR_LANGUAGES`
(default `fra+eng`) controls which Tesseract models are loaded; add a language
code here and re-run the setup script if a source uses another language.

**Parallelize with `--workers`** for anything beyond a handful of documents.
OCR is CPU-bound per page and embarrassingly parallel across documents:

```powershell
python cli.py prepare --ocr --workers 8
```

Leave a couple of CPU cores free for everything else running on the machine
(the OS, the server if it's already up, other tooling) — `--workers` spawns
that many separate extraction processes; `state.json` is still written by
only the main process, one result at a time, so this is safe to interrupt and
resume at any point.

**Defer the handful of huge documents** so they don't block a large batch of
short ones. `--max-pages 400` skips (for now) any document already known to
have more than 400 pages; run that first, then a separate low-priority pass
with `--min-pages 401` for the deferred giants once the bulk of the backlog
is done:

```powershell
python cli.py prepare --ocr --max-pages 400 --workers 8
python cli.py prepare --ocr --min-pages 401 --workers 8
```

A single one-off large document can be re-OCR'd on its own without a
`--min-pages` filter simply by scoping to its source and `--limit`.

### Finding which documents have broken OCR

`cli.py status` only gives aggregate counts (`needs_ocr`,
`ocr_attempted_unresolved`, `insufficient_text`, `errors`). To see exactly
*which* documents are affected, read `state.json` directly:

```powershell
.\.venv\Scripts\python.exe -c "
import json
from pathlib import Path
state = json.loads(Path(r'%LOCALAPPDATA%\RAF_Fiscal_Assistant\state.json').read_text(encoding='utf-8'))
stuck = [d for d in state['documents'].values() if d.get('status') == 'needs_ocr']
for d in sorted(stuck, key=lambda d: -(d.get('page_count') or 0)):
    print(d.get('page_count'), d.get('source'), d.get('filename'))
"
```

For any one of them, check whether the underlying PDF is actually readable
before assuming it's an OCR-quality problem — a quick, direct check with the
same library the pipeline uses:

```powershell
.\.venv\Scripts\python.exe -c "
import fitz
doc = fitz.open(r'C:\path\to\the\file.pdf')
print('pages:', doc.page_count)
print('text chars:', sum(len(p.get_text()) for p in doc))
print('images:', sum(len(p.get_images()) for p in doc))
"
```

- `pages: 0` on a file that "opens ok" means a corrupted/truncated download —
  re-download it, don't re-OCR it.
- `pages: N` with `text chars: 0` and `images: >0` is a genuine scanned
  image with no text layer — the normal case `--ocr` is built for.
- If it still fails after `--ocr`, view the page itself (the `Read` tool
  renders PDF pages) to judge whether it's legible-but-hard (worth a retry
  with a cleaner source copy if one exists) or genuinely blank/illegible (not
  fixable by OCR at all — see below).

### Repairing OCR failures

For documents that only need OCR to actually run (not yet attempted, or the
Tesseract language data was missing at the time), simply re-run `--ocr`
scoped to the affected source — it only touches documents currently marked
`needs_ocr`, so it's safe and cheap to repeat:

```powershell
python cli.py prepare --ocr --source <id> --workers 8
```

For documents still stuck after a real attempt
(`ocr_attempted_unresolved`), re-running the same command will not change
the outcome — Tesseract already tried and failed on that exact image.
Triage each one with the diagnostic steps above first, then:

- **Corrupted file** → re-download from the source and re-run `prepare`.
- **Genuinely blank page** → nothing to repair; it's expected to stay
  `needs_ocr` (or manually exclude it via the source's `exclude` list in
  `config/sources.json` if it's cluttering status reports).
- **Legible but low-quality scan** (skew, overlapping stamps, a handwritten
  date inserted into a printed template) → for a small number of documents,
  reading the page directly and hand-transcribing the few facts actually
  needed is faster and more reliable than trying to tune OCR settings; for a
  large number, this is a signal a different extraction approach (e.g. a
  vision-model transcription fallback) is worth building rather than
  repeatedly re-running Tesseract on the same images.

After repairing any documents, re-run `index`/`index-local` and restart the
server so the corrected text is actually searchable.
