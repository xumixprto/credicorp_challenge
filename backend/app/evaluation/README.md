# Run traces and LLM evaluation

Every actual `/agent` invocation is recorded in `data/evaluations/runs.csv`. Judgments are
saved to `data/evaluations/evaluations.csv`, one latest evaluation per run. The directory is configurable. CSV uses UTF-8,
standard quoting, and JSON in columns ending in `_json`; embedded commas, quotes, and newlines
round-trip without flattening evidence. Empty scalar cells represent unavailable values.
Import text columns as text when opening untrusted conversation content in a spreadsheet.

## Folder structure

```text
backend/app/evaluation/
  config.py          # Typed config.yaml settings
  storage.py         # File locks, CSV serialization, atomic writes
  trace.py           # Context shared by one invocation and its tool tasks
  models.py          # Structured LLM assessment schema
  schema.py          # Required run/retrieval/chunk fields and allowed citation IDs
  errors.py          # Safe, specific judge-output validation failures
  prompts.py         # Judge rubric and version
  judge.py           # Evidence assembly, OpenAI client, output validation
  service.py         # Run selection, batching, scoring, joined table
  metrics/
    retrieval.py     # Precision, recall, NDCG
    grounding.py     # Evidence-relative claim rates
    behavior.py      # Tool usage and intention success grade normalization
backend/app/middleware/tool_trace.py
backend/app/api/routes/judge.py
shared/evaluations.py              # CSV and HTTP record schemas
frontend/components/judge.py
frontend/services/judge_client.py
```

## `runs.csv`: one row per invocation attempt

| Column | Meaning |
| --- | --- |
| `schema_version` | Record format version, currently 1 |
| `run_id` | Generated UUID; evaluation foreign key |
| `request_id` | Original HTTP request ID for correlation |
| `user_id`, `session_id` | Conversation identity |
| `message_type` | `TEXT` or `HUMAN_FLOW_REPLY` |
| `started_at`, `finished_at` | UTC timestamps |
| `duration_ms` | Model/tool invocation duration |
| `model` | Agent model configured for this run |
| `status` | `running`, `completed`, `approval_required`, or `error` |
| `query` | User query, or latest visible user turn for a continuation |
| `answer` | Text returned by the agent |
| `input_json` | Original validated API payload, including approval decision when applicable |
| `context_json` | Framework conversation state before invocation; contains prior messages/tool results |
| `response_json` | Framework response messages, tool calls/results, and approval requests |
| `tools_json` | Executed tool names, call IDs, arguments, results/status, duration, safe error type |
| `retrievals_json` | Query and generated retrieval ID per call, original ranked hits, collection, distances, full citation metadata/text, visible evidence, omitted count, status/duration |
| `usage_json` | Agent Framework's reported token usage |
| `configuration_json` | Agent system instructions, evidence token budget, Alpaca mode and toolsets; no configured API keys |
| `error_type` | Exception class when invocation fails; exception text is not captured |

A normal turn and its later approval continuation are separate runs, linked through the
conversation and confirmation ID in their inputs/response. Rejected validation requests and
cached HTTP retries do not create a model invocation or duplicate run. Provider retries after
a failed turn are new invocation attempts. A pending proposal has no final answer to evaluate.
Only agent invocations are traced; standalone `/docs/retrieve` requests are not separate runs.
Existing conversations are not backfilled: prior retrieved evidence cannot reliably be recreated.

The tool trace middleware runs **after** the approval gate. Proposed orders are represented by
approval content, not falsely recorded as executed tools. Retrieval captures the actual response
at call time, so evaluation never re-queries Chroma or re-executes an Alpaca tool.
The stored context includes visible model outputs and tool evidence, not hidden chain of thought.

## `evaluations.csv`: latest evaluation per run

| Column | Meaning |
| --- | --- |
| `schema_version` | Record format version, currently 2 (older rows retain version 1) |
| `evaluation_id` | UUID of this assessment attempt |
| `run_id` | Foreign key to `runs.csv` |
| `evaluated_at` | UTC time of evaluation |
| `judge_model`, `rubric_version`, `k` | Reproducibility and missing-evaluation selection |
| `status` | `completed`, `not_applicable`, or `error` |
| `precision_at_k`, `recall_at_k`, `ndcg_at_k` | Macro means over applicable retrieval calls in this run |
| `hallucination`, `groundedness` | Evidence-relative claim fractions in [0, 1], or blank |
| `tool_usage`, `intention_success` | Independent LLM rubric scores: 0 failed, 0.5 partial, 1 successful; blank if insufficient evidence |
| `claim_count` | Number of atomic factual claims assessed |
| `summary` | Concise judge assessment or reason evaluation could not run |
| `retrieval_metrics_json` | Individual query scores, relevance source, NDCG scope, recall availability |
| `assessment_json` | Chunk grades, claim verdicts, behavior grades, evidence references and explanations |
| `reference_json` | Optional externally supplied relevance labels/reference answer used in this evaluation |
| `duration_ms` | Time for the assessment batch containing this row; not per-run billed time |
| `error_type` | Safe exception class or `ContextTooLarge`; failed scores stay blank |

Repeating an evaluation replaces that run's row, including scores, assessment, timestamp, and ID.
A failed re-evaluation also replaces the old row with an error and blank scores, so outdated
successes are not presented as current. Existing duplicate rows are compacted on the next write,
keeping the last stored attempt per run. Other runs retain their latest evaluations.
The table endpoint joins each run to its latest
evaluation for the configured model/rubric/k. If only a different configuration exists, it shows
the last scores as `stale`. No evaluation row means `missing`. `not_applicable` counts as handled;
blank recall or claim scores in a completed evaluation do not trigger repeated paid judgments.
Failed latest attempts can be retried. Previously supplied reference labels are reused unless
explicitly replaced; send an empty reference object for that run to remove old labels.

Rubric **2.0** adds tool usage and intention success. Existing v1 CSV files remain readable;
the next evaluation write atomically adds the two columns, keeping the latest row per run and
leaving new scores blank for runs not re-evaluated. Old rubric evaluations appear as `stale`; **Evaluate Missing Runs**
evaluates them with the new rubric. Downloads retain the original file schema until that write.

## Metric definitions and limits

The model labels relevance, factual claims, and behavior; **Python calculates the scores**. These are
LLM-assisted measurements, not guaranteed human ground truth. The judge validates numerical
units, reporting periods, citations and supported calculations, and gives concise explanations.
It receives no tools. Trace text is treated as untrusted evidence rather than instructions.

| Metric | Calculation / interpretation |
| --- | --- |
| Precision@k | Number of relevant chunks in the first k unique ranked results, divided by k. Unfilled slots count as irrelevant. Grade 2 or 3 is relevant. |
| Recall@k | Relevant chunks retrieved in the first k, divided by **all** relevant chunks in a complete externally supplied label set. Blank without complete labels, or if that set has no relevant chunks. |
| NDCG@k | DCG uses `(2^grade - 1) / log2(rank + 1)`; divide by the ideal sorted DCG at k. Relevance grades are 0 irrelevant, 1 tangential, 2 partially answering, 3 directly answering. Zero if ideal DCG is zero. |
| Groundedness | Supported atomic factual claims / total factual claims. Higher is better. |
| Hallucination | (Unsupported + contradicted factual claims) / total factual claims. Lower is better. This is an evidence-relative proxy, not proof that every unsupported statement is false. |
| Tool usage | Rubric grade / 2: 2 appropriate tool choice, arguments, sequencing and error handling (including correctly using no tool); 1 useful attempt with material mistakes; 0 wrong/unnecessary tools or missing a required available tool. Higher is better. |
| Intention success | Rubric grade / 2: 2 the requested outcome and constraints were fulfilled; 1 partially fulfilled; 0 failed or answered the wrong question. Higher is better. |

Both behavior metrics work without retrieval or relevance labels. They are subjective rubric
scores, not probabilities or replacements for recall. Reasons and trace references are stored in
`assessment_json.tool_usage` and `assessment_json.intention_success`, and shown in the UI's
**Inspect an evaluation** panel. If the trace cannot support a judgment, the grade and score are
null, with a reason; this does not cause repeated automatic evaluation of a completed assessment.

The judge receives executed calls (including failures), proposed calls/approval responses, user
confirmation decisions, history, and final answers. New runs also capture the available tool
names, descriptions and argument schemas under `configuration_json.available_tools`. Older runs
without a catalog can still be assessed for observed behavior; the judge must not invent missing
capabilities. These trace references support behavior judgments, not factual answer claims.

A correctly formed call that fails because of a provider outage can have high tool usage and low
intention success. A supported answer to the wrong question can have high groundedness and low
intention success. Greetings need no tools and can score 1 for both behavior metrics. A proposal
is not execution, and submitting an order does not establish that it filled. On a rejection
continuation, honoring the rejection is the intended outcome. Pending approvals and failed agent
runs still receive `not_applicable`; completed replies that describe failed tools can be judged.

Without external labels, precision uses judge relevance labels and NDCG is explicitly marked
`retrieved_pool`: its ideal ranking contains only retrieved candidates. This diagnoses their
ordering, **not missed relevant documents elsewhere in the corpus**. With complete reference
labels, NDCG uses the labeled corpus (`reference_corpus`) and recall becomes available.
With incomplete labels (`complete: false`), known grades override judge grades, recall stays
blank, and NDCG remains relative to the retrieved pool.

Successful empty retrievals score precision/NDCG zero; retrieval failures are unavailable rather
than silently counted as irrelevant content. Runs with no retrieval calls have blank retrieval
metrics. Aggregate recall may cover fewer calls than precision; inspect `retrieval_metrics_json`.

Answer grounding uses only evidence actually visible to the agent, prior successful tool outputs,
and an optional reference answer. Raw chunks omitted by the tool's context budget cannot support
an answer claim. Prior assistant statements are context, not factual evidence. General factual
claims without supplied evidence may be unsupported even when true. Greetings or clarifying
questions with no factual claims have blank claim scores. Groundedness and hallucination are
complements under this rubric; contradictions remain distinguishable in `assessment_json`.

## API

List the latest 50 rows, including missing evaluations:

```bash
curl 'http://127.0.0.1:8000/judge?limit=50&offset=0'
```

Retrieve the trace, chunk IDs, and retrieval IDs for one run:

```bash
curl 'http://127.0.0.1:8000/judge/runs/RUN_ID'
```

Evaluate missing/stale/failed runs across the entire table (the default):

```bash
curl -X POST 'http://127.0.0.1:8000/judge' \
  -H 'Content-Type: application/json' -d '{}'
```

Evaluate selected runs, including ones already evaluated:

```bash
curl -X POST 'http://127.0.0.1:8000/judge' \
  -H 'Content-Type: application/json' \
  -d '{"run_ids":["RUN_ID_1","RUN_ID_2"],"missing_only":false,"k":5}'
```

Evaluate **all** finished runs again: `{"missing_only": false}`. To evaluate only missing runs
within a selection, send `run_ids` and leave `missing_only` true. Running invocations are skipped.
An unknown run ID returns 404 before any evaluation is started. Empty selections return 422.
Failed agent runs and pending approvals receive `not_applicable` without calling the judge model.

Optional external labels (chunk IDs, not filenames or citation aliases):

```json
{
  "run_ids": ["RUN_ID"],
  "missing_only": false,
  "k": 5,
  "references": [{
    "run_id": "RUN_ID",
    "expected_answer": "Optional benchmark answer with the correct period and units.",
    "retrievals": [{
      "retrieval_id": "RETRIEVAL_ID_FROM_TRACE",
      "complete": true,
      "relevance": {"retrieved-chunk-id": 3, "missed-but-relevant-chunk-id": 2}
    }]
  }]
}
```

`complete: true` asserts that the labels cover **all** relevant chunks for this retrieval query;
unlisted chunks are treated as irrelevant. Keep labels tied to the captured index generation.
If the relevant universe is unknown, omit labels or use `complete: false`; do not manufacture
recall by labeling only the top-k results. Missing or duplicate retrieval/reference IDs return 422.

## Batching, persistence, and configuration

This uses bounded, synchronous multi-run requests to the OpenAI Responses API with
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), not the
asynchronous OpenAI Batch API. Small traces share a request; batches run with bounded concurrency.
Each request uses required response fields (`run_1`, `run_2`, etc.) for its selected runs.
Python maps those named fields back to the saved run IDs; the model does not copy or generate
run UUIDs. Missing/extra fields fail schema validation, and response field order cannot change
the mapping. Historical turns remain context, not additional runs to evaluate.
The same binding applies to `retrieval_1` / `chunk_1` fields within each run. Every retrieval
and every ranked chunk gets its own required assessment, even when multiple queries return
overlapping results. Citation fields are constrained to the supplied evidence IDs; behavior
citations additionally allow the supplied trace IDs. Schema size is included in the input budget.
Exact document citation labels and chunk IDs are normalized to canonical evidence IDs only when
they identify one supplied evidence entry. Actual tool call IDs are accepted for behavior judgments;
only successful non-retrieval tool results can provide tool-ID aliases for factual claims.
Unknown or ambiguous labels remain invalid. Chunks omitted from the agent's visible evidence
cannot become factual support through an alias. This normalization makes no additional model call.
Results are validated for exact run/retrieval/chunk coverage, valid claim/behavior evidence IDs,
and required trace references for non-null behavior grades before scoring. A run's invalid
judgment produces `error` with blank scores for that run; valid siblings in the batch still score.
Refusal, malformed batch output, invalid run IDs, truncation or provider failure fails the batch.
Other batches continue and are saved independently. Safe validation reasons appear in the row's
`summary` and `logs/backend.log` as `judge_run_failed`, with `run_id` and `reason`.
Rejected parsed judgments are retained under `assessment_json.rejected_judgment` for inspection;
they are diagnostic data, never accepted scores. Provider exception bodies are not logged.

| `config.yaml` parameter | Default | Meaning |
| --- | --- | --- |
| `evaluation.directory` | `data/evaluations` | CSV/lock directory; relative to repo root |
| `evaluation.judge_model` | `gpt-4.1-mini` | Model used for structured assessments; uses `OPENAI_API_KEY` |
| `evaluation.k` | `5` | Default retrieval cutoff; API override range 1–100 |
| `evaluation.batch_size` | `3` | Maximum runs per model request, 1–20 |
| `evaluation.max_concurrency` | `2` | Concurrent model requests, 1–8 |
| `evaluation.max_input_tokens` | `24000` | Approximate input budget per batch, counted with cl100k_base plus schema overhead |
| `evaluation.max_output_tokens` | `8000` | Model output budget per batch |
| `evaluation.timeout_seconds` | `90` | Timeout for each model request attempt |
| `evaluation.max_retries` | `1` | OpenAI SDK retries for transient failures, 0–3 |

Restart FastAPI after config changes. `JUDGE_API_TIMEOUT_SECONDS=600` in `.env` controls only the
Streamlit HTTP wait. Long evaluations may outlast that wait; refresh the table before retrying.
The evaluation lock prevents simultaneous `/judge` calls from duplicating paid work (409 if busy).
Completed batches remain saved if a later batch fails or the request is interrupted.
Oversized individual traces fail with `ContextTooLarge`; evidence is never silently truncated.
Increase the input budget within your chosen model's limits, or evaluate shorter conversations.

CSV updates use one file lock and atomic replacement. These are local prototype tables: writes
rewrite the table, storage grows with conversation history, and they are not a distributed data
warehouse. A run is saved as `running` before invocation. If final trace persistence fails, the
API preserves the agent/trading result and logs `run_trace_save_failed`; it never repeats an order
to repair telemetry. A crash can leave a `running` row, which is not judged automatically.
Two CSVs are independent files, not a cross-file transaction. Keep both when transporting data.

The tables contain user messages, document excerpts, and account/tool results. They are ignored
by Git under `data/`. The existing application has no authentication; the Judge tab/API is a
local administrative view over all users. API keys are not included in trace configuration.
Judge requests send the selected evidence to OpenAI with `store=False`.

## Streamlit and extension

Choose **Judge** in the sidebar. Check rows and click **Evaluate selected** to evaluate those runs,
or click **Evaluate Missing Runs** to process unfinished evaluations across the full table,
including runs beyond the visible 50. Results survive page refresh and backend restarts. Expand
**Inspect an evaluation** for per-query scores, claim verdicts, evidence IDs and failure details.

Use **Download evaluations CSV** or **Download runs CSV** to export the complete stored tables,
including rows beyond the visible 50 and the latest evaluation per run. Files are fetched on click;
downloading does not run the judge. The evaluation CSV contains actual evaluations; join it to
the runs CSV using `run_id` to include unevaluated runs. An empty table exports column headers.
Direct downloads are available at `GET /judge/export/evaluations` and `GET /judge/export/runs`.

To add a metric, put the pure calculation in `metrics/`, add fields to the shared record schema,
and connect it in `JudgeService._score`. Update the rubric/schema only if the metric requires new
model labels. Bump `RUBRIC_VERSION` when changing evaluation semantics so old scores become stale.
Changing CSV columns requires explicit compatibility handling in `storage.py`; the v1-to-v2
upgrade above is supported. Other unrecognized CSV schemas are rejected.
