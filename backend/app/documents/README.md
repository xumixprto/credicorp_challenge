# Document ingestion and retrieval

`POST /docs/ingest` converts uploaded documents with Docling, chunks them with HybridChunker,
embeds the text using `text-embedding-3-small`, and stores vectors and citation metadata in Chroma.
`POST /docs/retrieve` searches the current index. `POST /docs/reindex` rebuilds it from saved originals.

Set `OPENAI_API_KEY` in the root `.env`. Install `requirements-dev.txt` and start FastAPI using
the root README. Docling and Chroma are installed in this workspace's `.venv` already. PDF/image
conversion can download Docling layout, table, and OCR model assets on first use; allow that initial
download or set `parsing.artifacts_path` to predownloaded assets. Tiktoken also caches its encoding
data on first use. EasyOCR requires its optional dependencies; Tesseract CLI requires the system
executable. RapidOCR is the included default.

## Requests

These examples work in Git Bash. Upload multiple files by repeating `files`:

```bash
curl -X POST http://127.0.0.1:8000/docs/ingest \
  -F "files=@./documents/policy.pdf" \
  -F "files=@./documents/handbook.docx" \
  -F "source_ids=hr/policy" \
  -F "source_ids=hr/handbook"

curl -X POST http://127.0.0.1:8000/docs/retrieve \
  -H "Content-Type: application/json" \
  -d '{"query":"What is the leave policy?","top_k":5}'

curl -X POST http://127.0.0.1:8000/docs/reindex \
  -H "Content-Type: application/json" \
  -d '{}'
```

Ingestion accepts optional repeated `source_ids` and `source_uris`, in the same order and count
as the files. A source ID identifies a logical document across updates; it defaults to the basename.
Use explicit IDs when different directories contain identically named files. Source URIs are
citation labels only: the server never fetches them. Filenames and source IDs remain in the catalog;
physical storage paths use content hashes. Every source gets a stable `document_id`.

The ingestion response includes `collection`, per-file `results`, `embedded_texts`, and
`cached_texts`. Status is `ingested`, `updated`, `unchanged`, `duplicate`, or `failed`.
Byte-identical content in the same format shares vectors across different source IDs, while
preserving all source references. With `skip_failed_files: true`, check every result: a 200 response
can contain failed files. Valid files are committed together only after indexing succeeds.
Provider/indexing failures leave the previous catalog active; successfully cached vectors can be
reused on retry. Originals accepted for parsing remain on disk even when conversion fails.

Retrieval also accepts `document_ids` (restrict the search to these sources) and `max_distance`.
It returns `collection`, `distance_metric`, and `hits`. Each hit has `chunk_id`, raw `text`, `distance`,
`pages`, `headings` (the section path), Docling `references`, and one or more `sources` with IDs,
filename, URI, and content hash. References preserve available bounding boxes and character spans.
Page numbers are original, one-based Docling page numbers; formats without pagination return an
empty page list. Distance is **lower-is-better**, not a normalized relevance probability.

Reindex accepts optional `force_reparse` and `force_reembed` booleans. It rereads `config.yaml`,
reuses cached artifacts/vectors by default, builds a separate collection, and atomically commits
the new collection plus source revisions. Existing queries continue using the previous generation.
Failed rebuilds do not change the active index. Superseded chunks are filtered out immediately
after an incremental update. Old committed collections, revisions, artifacts, and originals are
retained; automatic deletion/garbage collection is intentionally not implemented.

All endpoints are synchronous HTTP operations from the caller's perspective: large conversions
can take minutes. Parsing and Chroma work run in worker threads, leaving the API event loop free.
Concurrent ingest/reindex operations serialize through a file lock; a busy writer returns 409.
File/batch byte limits return 413, invalid inputs/configuration return 422, and a missing embedding
key returns 503. A fresh empty index can be searched without an OpenAI key.

## Persistence and interfaces

FastAPI owns one `PersistentClient` for its entire lifespan and closes it at shutdown. Chroma is
disk-backed, so vectors survive process restarts as well as individual requests. Run one FastAPI
worker with embedded Chroma; use a standalone Chroma server before scaling to multiple workers.
Back up both configured directories while ingestion/reindexing are stopped.

```text
data/documents/
  originals/         Exact original bytes, addressed by SHA-256 and extension
  parsed/            Lossless DoclingDocument JSON, keyed by content + parser settings/version
  chunks/            Text and citation JSON, keyed by parse artifact + chunker settings/version
  catalog.sqlite3    Active index/source manifest and durable embedding cache (SQLite/WAL)
  writer.lock        Serialize index mutations
data/chroma/         Persistent Chroma collections, vectors, chunks, and citation metadata
```

`processing.py` exposes `Parser` and `Chunker` protocols and their Docling adapters.
`embeddings.py` exposes `Embedder` and the batched OpenAI adapter. `catalog.py` handles transactional
manifests/cache; `vectors.py` owns Chroma; `service.py` coordinates publication. Routes only validate
uploads and call the service. The document catalog supplies current source identities for shared
chunks, while Chroma stores chunk text, content hashes, and structural citation metadata.

Embedding cache keys include API base URL, model, dimensions, and exact embedding text. Repeated
texts within a request are deduplicated. Only cache misses go to OpenAI, grouped by input count
and token budget with bounded parallelism. Responses are reordered by their returned indices
and checked for dimensions and finite values. Changing parser/chunker/model/HNSW settings after
restart blocks new ingestion until reindexing; queries still use the active index's embedding
model and dimensions. Secrets stay in `.env`, outside the YAML and persisted index configuration.

## Configuration reference

The root `config.yaml` is the single source for pipeline settings. `DOCS_CONFIG_PATH` in `.env`
can select another YAML file. Paths are relative to the repository root, or absolute. Defaults
below match the checked-in YAML. Unknown keys and invalid limits are rejected. Restart to reload
all settings, or call `/docs/reindex` to reload them and publish a compatible index. Moving storage
paths requires a restart and migration/copy of both storage directories if retaining existing data.

| Parameter | Default | Purpose |
| --- | --- | --- |
| `storage.root` | `data/documents` | Originals, parse/chunk artifacts, manifest, embedding cache, writer lock |
| `storage.sqlite_timeout_seconds` | `30` | Wait for SQLite locks |
| `storage.lock_timeout_seconds` | `5` | Wait for another index writer before returning 409 |
| `ingestion.max_files` | `20` | Maximum files per HTTP batch (1–1000) |
| `ingestion.max_file_mb` | `50` | Per-file limit in MiB |
| `ingestion.max_batch_mb` | `200` | Aggregate file-content limit in MiB |
| `ingestion.skip_failed_files` | `true` | Report conversion failures individually and commit successful files |
| `parsing.format_map` | See YAML | Extension-to-Docling-format allowlist; lower-case extensions without dots. Includes PDF, DOCX, Markdown, HTML, PPTX, XLSX, CSV, text via Markdown, and images. Extend with supported Docling formats/dependencies. |
| `parsing.max_pages` | `500` | Docling conversion page limit; incomplete conversions are rejected |
| `parsing.timeout_seconds` | `180` | Docling PDF/image pipeline timeout; not a hard timeout for every backend |
| `parsing.accelerator` | `auto` | `auto`, `cpu`, `cuda`, `mps`, or `xpu`; requires compatible hardware/runtime |
| `parsing.num_threads` | `4` | Docling accelerator CPU threads |
| `parsing.artifacts_path` | `null` | Optional local directory of Docling model assets |
| `parsing.pdf.do_ocr` | `true` | Enable OCR for scanned pages/images |
| `parsing.pdf.ocr_engine` | `rapidocr` | `rapidocr`, `easyocr`, or `tesseract_cli` |
| `parsing.pdf.ocr_languages` | `[]` | OCR engine-specific language codes; empty uses engine defaults |
| `parsing.pdf.force_full_page_ocr` | `false` | OCR the entire page even when text is present |
| `parsing.pdf.do_table_structure` | `true` | Recognize table structure |
| `parsing.pdf.table_mode` | `accurate` | TableFormer `fast` or `accurate` |
| `parsing.pdf.do_cell_matching` | `true` | Match table structure to detected text cells |
| `parsing.pdf.images_scale` | `1.0` | Page/image rendering scale |
| `parsing.pdf.generate_page_images` | `false` | Retain rendered page images in parse artifacts |
| `parsing.pdf.generate_picture_images` | `false` | Retain extracted picture images |
| `chunking.tokenizer` | `cl100k_base` | Tiktoken encoding; keep aligned with the embedding model |
| `chunking.max_tokens` | `512` | HybridChunker token budget, including contextual metadata where possible (32–8191) |
| `chunking.merge_peers` | `true` | Merge compatible small neighboring chunks |
| `chunking.contextualize` | `true` | Embed contextualized text with headings; return raw chunk text for citations |
| `chunking.repeat_table_header` | `true` | Repeat headers in native Docling table chunks; contextual rows always carry their headers |
| `chunking.omit_header_on_overflow` | `false` | Allow native HybridChunker to omit an oversized table header; contextual rows never omit a value's header |
| `chunking.always_emit_headings` | `false` | Emit standalone headings according to HybridChunker's behavior |
| `chunking.table_strategy` | `contextual_rows` | Emit tables as contextual rows; `docling` restores native HybridChunker table serialization |
| `chunking.infer_table_headers` | `true` | Infer missing leading date/period or simple textual headers; explicit Docling headers take precedence |
| `chunking.table_header_max_rows` | `8` | Maximum leading physical rows inspected when inferring headers |
| `chunking.table_context_items` | `6` | Nearby preceding text items considered as table context; resets at each table |
| `chunking.table_context_max_tokens` | `128` | Budget for whole nearby context items, additionally capped at one third of the chunk budget |
| `embeddings.model` | `text-embedding-3-small` | OpenAI embedding model |
| `embeddings.dimensions` | `1536` | Output vector dimensions; up to 1536 for the default model |
| `embeddings.base_url` | `https://api.openai.com/v1` | OpenAI API endpoint; included in cache identity |
| `embeddings.batch_size` | `64` | Maximum inputs per API request (up to 2048) |
| `embeddings.max_batch_tokens` | `200000` | Maximum combined input tokens per request (up to 300000) |
| `embeddings.max_input_tokens` | `8191` | Maximum tokens per input; must fit in one batch |
| `embeddings.max_concurrency` | `4` | Maximum concurrent embedding requests per operation |
| `embeddings.timeout_seconds` | `60` | OpenAI SDK HTTP timeout |
| `embeddings.max_retries` | `3` | SDK retry count for transient failures |
| `embeddings.cache_enabled` | `true` | Read/write the durable vector cache; unchanged indexed revisions still skip work |
| `chroma.persist_directory` | `data/chroma` | On-disk Chroma database directory |
| `chroma.collection_prefix` | `documents` | Prefix for uniquely named collection generations |
| `chroma.write_batch_size` | `512` | Upsert batch size, also capped by Chroma's supported maximum |
| `chroma.hnsw.space` | `cosine` | Distance metric: `cosine`, `l2`, or `ip` |
| `chroma.hnsw.ef_construction` | `200` | Construction candidate pool; larger trades time/memory for recall |
| `chroma.hnsw.ef_search` | `100` | Search candidate pool |
| `chroma.hnsw.max_neighbors` | `16` | Graph neighbors per node |
| `chroma.hnsw.num_threads` | `4` | Index/search threads |
| `chroma.hnsw.batch_size` | `100` | Chroma in-memory index batch size |
| `chroma.hnsw.sync_threshold` | `1000` | HNSW disk synchronization threshold; at least `batch_size` |
| `chroma.hnsw.resize_factor` | `1.2` | Index capacity growth factor (>1) |
| `retrieval.top_k` | `5` | Default maximum returned chunks |
| `retrieval.max_top_k` | `50` | Per-request top-k ceiling |
| `retrieval.max_distance` | `null` | Optional maximum distance; null disables post-filtering |
| `reindex.force_reparse` | `false` | Default bypass of parsed/chunk artifacts; request body can override |
| `reindex.force_reembed` | `false` | Default bypass of vector cache; request body can override |

Long documents are chunked without discarding source references. An input that still exceeds the
embedding limit is rejected before calling OpenAI. Keep `chunking.max_tokens <= embeddings.max_input_tokens`,
`embeddings.max_input_tokens <= embeddings.max_batch_tokens`, and `retrieval.top_k <= retrieval.max_top_k`.

## Table chunks

The default `contextual_rows` strategy works on Docling's common table representation for HTML,
PDF, DOCX, and other formats. It preserves merged header spans, joins currency/percentage symbols
with values, omits empty layout cells, and repeats nearby title/unit context and column headers for
each row. Wide rows split between labeled values, never through a value. Prose still uses Docling
HybridChunker. Table context and headers are part of the returned text as well as embedding text.

References include the table's original provenance, zero-based physical `table_rows`, and cell
coordinates `[row_start, row_end, column_start, column_end]` with exclusive end offsets. Context
items are separately tagged `role: table_context`. Originals and parsed tables remain unchanged.
Explicit headers take precedence; conservative inference handles date/period headers and simple
textual headers when missing. Rich cells containing only faithfully represented text are supported.
Ambiguous, nested/non-text, headerless, or individually oversized tables fall back to native Docling
chunking. `tables_contextualized` logs report table and normalized counts.
PDF page references are retained when available. OCR supplies recognized text; Docling's table
structure recognition supplies cell geometry. Neither this step nor OCR guarantees correct cells
for a poorly recognized table. Keep `parsing.pdf.do_table_structure` enabled for PDF tables.

Reindex to apply these settings to existing documents. Both force flags may be used for a full
rebuild; changed chunking settings also invalidate the chunk cache automatically. Retrieval ranking
still depends on the query and embeddings; the change preserves context rather than guaranteeing
that every table query will rank first.

## Following an ingestion

Watch `logs/backend.log` and filter by the `X-Request-ID` response header. The normal sequence is
`request_started` → `ingestion_started` → `document_processing_started` →
`docling_conversion_started` → `docling_conversion_completed` → `document_chunking_started` →
`document_chunked` → `document_embeddings_started` → `embedding_batch_completed` →
`document_index_write_started` → `document_index_write_completed` → `ingestion_committed` →
`request_completed`. Cached documents emit cache-hit events and can skip these stages.

`request_completed` means the HTTP request has finished. A 422 response needs its JSON `detail`;
validation logs now record missing/invalid field locations without logging input values. For batch
responses, also check each result's status: HTTP 200 can include conversion failures.
`docling_conversion_failed` records conversion status and structured error categories/pages;
timeout errors also produce an actionable per-file response. Loading model weights is preparation,
not a PDF processing percentage. Model initialization can add time outside the document timeout.
For a confirmed timeout on a large PDF, increase `parsing.timeout_seconds` (for example to 600),
restart FastAPI, and retry ingestion. Failed/unindexed documents do not require reindexing.

Do not copy an exported placeholder multipart boundary into curl: use `-F` and let curl generate
the Content-Type header. Add `-i` to show the HTTP status and request ID along with the response.

## Verification and references

Run `python -m pytest tests/backend/test_documents.py` from the activated virtual environment.
Tests use real Docling/HybridChunker, real persisted Chroma, and mocked OpenAI HTTP responses.
They cover Markdown/DOCX/HTML parsing, citation provenance, duplicates, updates, batching/cache
reuse, restart persistence, and retrieval during successful or failed reindex operations.
`tests/backend/test_table_chunking.py` also checks SEC-style merged HTML headers, PDF table/page
provenance, text-only rich cells, token limits, fallback behavior, and chunk-cache invalidation.

References: [Docling chunking](https://docling-project.github.io/docling/concepts/chunking/),
[HybridChunker](https://docling-project.github.io/docling/_generated/examples/hybrid_chunking/),
[OpenAI embeddings](https://developers.openai.com/api/docs/guides/embeddings),
[Chroma HNSW configuration](https://docs.trychroma.com/docs/collections/configure).

## Agent evaluation settings

The root `config.yaml` also has an `evaluation` section. It does not change document parsing,
embedding, or index signatures. See [evaluation configuration](../evaluation/README.md) for every
judge parameter, CSV schema, and retrieval/answer metric definition.
