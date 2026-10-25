# Total tiempo: 8.5 horas

# Credicorp Agent Chat

A FastAPI backend and a Streamlit chat application powered by Microsoft Agent Framework and
OpenAI. The sidebar switches between **Agent Chat**, with saved conversations and a chat composer,
**Document Ingestion**, with drag-and-drop document uploads, and **Reindex**. Chat context and messages survive
browser refreshes and application restarts.

## Setup

Run commands from the repository root. Python 3.11+ is required; this environment uses Python 3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

The `.venv` and `.env` are already created in this workspace. For runtime dependencies only,
install `requirements.txt`. `requirements.lock.txt` records the complete tested development
environment on Windows with Python 3.14; use it in place of `requirements-dev.txt` to reproduce
those versions on that platform.

Set these values in the root `.env`, then restart FastAPI and Streamlit:

```dotenv
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-4.1-mini
CHAT_USER_ID=d5bbf9d1-5945-44d8-bb10-0065164a8356
```

The API key is used only by the backend. `OPENAI_MODEL` selects the model. `CHAT_USER_ID` is the
stable local development identity used by Streamlit; keeping it unchanged restores the same
chats across browser sessions. Change it to another UUID to use a separate conversation history.

This project pins Microsoft Agent Framework core **1.23.0** and its independently versioned
OpenAI provider **1.17.0**. It uses the Responses API with local framework history (`store=False`).
The framework's current name for a conversation thread is `AgentSession`.

## Alpaca MCP

The backend launches **Alpaca MCP Server 2.4.0** automatically through `uv`, connects with
Agent Framework's `MCPStdioTool`, and discovers its tools. No VS Code MCP configuration or extra
HTTP port is needed. Install the updated requirements in the backend's virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Add your keys and selected toolsets to the root `.env` (keep secrets out of `.env.example`):

```dotenv
ALPACA_MCP_ENABLED=true
ALPACA_API_KEY=your-alpaca-key
ALPACA_SECRET_KEY=your-alpaca-secret
ALPACA_PAPER_TRADE=true
ALPACA_TOOLSETS=stock-data,assets,news
```

Restart FastAPI using the usual command below. `uv` downloads the pinned server on its first
launch into an isolated environment; downloads need internet access. The connection uses
`MCPStdioTool` directly, launching `python -m uv tool run alpaca-mcp-server==2.4.0` with
FastAPI's own Python interpreter. This is equivalent to `uvx` and uses uv's standard cache;
it does not depend on shell activation or finding a global executable on PATH.
One MCP subprocess is reused across conversations per backend worker and closed on shutdown.

`stock-data,assets,news` enables market research. To include account and trading tools, use your
selected `stock-data,trading,news,account` configuration. Paper mode must use paper-account keys;
live mode uses live-account keys and `ALPACA_PAPER_TRADE=false`. These credentials are backend-wide:
all chat users share the configured Alpaca account; `userId` only separates conversation history.
Alpaca write operations require an explicit `HUMAN_FLOW_REPLY` confirmation, including in paper
mode. Buying, selling, replacing/canceling orders, closing positions, and other writes are gated.
Read tools whose names start with `get_`, `list_`, `search_`, or `fetch_` remain automatic;
unrecognized Alpaca actions require approval by default.

Supported toolsets: `account`, `trading`, `watchlists`, `assets`, `stock-data`, `crypto-data`,
`options-data`, `corporate-actions`, `news`, `fixed-income-data`, and `locates`. Blank or unknown
toolsets fail validation instead of silently enabling everything. The pinned upstream server also
advertises documentation tools and shared historical-data helpers. Tools are prefixed `alpaca_`
to avoid name collisions with local tools. See [Alpaca's MCP documentation](https://docs.alpaca.markets/us/docs/trading-api-mcp).

Try **"Get the latest NVDA quote using the IEX feed, including its timestamp."** The agent can
combine Alpaca market data with the existing document retrieval tool. It is instructed to preserve
timestamps, distinguish feeds and financial periods, and report missing data or provider errors.
MCP results are sent to OpenAI and retained in the framework conversation history.

Check `logs/backend.log` for `alpaca_mcp_starting`, `alpaca_mcp_connected` (including tool count),
and `alpaca_mcp_stopped`. Existing function middleware logs each tool name, request/session ID,
duration, and failures without arguments, results, or keys. The MCP subprocess's stderr is appended
to `logs/alpaca-mcp.log` (under `LOG_DIR` if configured), avoiding Git Bash terminal handle issues.
A small `MCPStdioTool` subclass supplies the SDK's `errlog` file and disables automatic retries
of write calls after connection loss. Read calls retain SDK retry behavior. The SDK still manages
the connection and process cleanup. Uvicorn and application logs continue to appear in the terminal.
This raw subprocess log does not use the application's JSON formatter or automatic rotation.
Discovery confirms connectivity, not credential validity;
Alpaca validates credentials when a tool calls its API.
Startup failures include a `reason` field with configured API secrets redacted. Check `alpaca-mcp.log`
for subprocess output such as package download failures, which may precede the MCP exception.
With these pinned versions, the SDK's initial `server/discover` probe can produce validation
warnings in `alpaca-mcp.log` before falling back to its supported handshake. Successful startup
is indicated by `alpaca_mcp_connected`; no custom protocol workaround is applied.

If both keys are absent, Alpaca is skipped and document chat still works. A partial key pair or a
configured server that cannot start produces an actionable startup error. Set
`ALPACA_MCP_ENABLED=false` to run without Alpaca. The SDK handles connection setup and cleanup;
`ALPACA_MCP_REQUEST_TIMEOUT_SECONDS` controls its request timeout, including initialization.
For a slow first download, run
`.\.venv\Scripts\python.exe -m uv tool run alpaca-mcp-server==2.4.0 --help` once to cache the package.
Restart FastAPI after
changing keys/toolsets or if the MCP subprocess exits unexpectedly.

## Run

Standard development startup, with the virtual environment activated:

```bash
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
```

The equivalent command on Windows without activating the environment is:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
```

Start the frontend in another terminal:

```powershell
.\.venv\Scripts\python.exe -m streamlit run frontend/app.py
```

- Chat: <http://127.0.0.1:8501>
- API docs and interactive requests: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

Commands starting with `.venv` use that environment directly, so activation is optional for them.
To activate it in PowerShell, run `.\.venv\Scripts\Activate.ps1`.

For Bash on Windows (for example, Git Bash), activate it from the repository root with:

```bash
source .venv/Scripts/activate
```

Once activated, use `python` to run commands. Run `deactivate` to leave the virtual environment.

For example, start the frontend in Git Bash with:

```bash
source .venv/Scripts/activate
python -m streamlit run frontend/app.py
```

**Windows Git Bash:**

Use the standard command above. The application redirects only MCP subprocess stderr
to `logs/alpaca-mcp.log`. The regular command has been verified in Git Bash without shell
redirection on this Windows setup. In another terminal:

```bash
tail -f logs/alpaca-mcp.log
```

Uvicorn diagnostics appear in the terminal. Application JSON logs are also saved in
`logs/backend.log`. No shell redirection is needed.

The standalone `streamlit run frontend/app.py` command also works in the activated environment.

On macOS/Linux, use `.venv/bin/python` in place of `.\.venv\Scripts\python.exe`.
Stop either process with **Ctrl+C**.

### Linux VM

Create the environment on the VM and install runtime dependencies. Use `requirements.txt`;
the Windows development lock file contains platform-specific packages.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Configure the root `.env` or the VM service's environment variables, then run from the repository
root. For a deployed server, omit development auto-reload:

```bash
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

This binds to localhost for a reverse proxy on the same VM. Use `--host 0.0.0.0` if the server
should listen on the VM's network interfaces. One worker matches the current local file logging
setup and starts one Alpaca MCP subprocess. Run it under the VM's service manager for automatic
startup and restart.

The Linux deployment command has not yet been validated on a Linux VM; this repository has been
tested on Windows. The MCP launcher uses
the active Python interpreter and supports Linux. The VM service user needs writable document,
Chroma, session, log, and uv cache directories, plus outbound access for package/model downloads
and the configured APIs. Keep document and session directories on persistent storage.
No existing `.artifacts` files or diagnostic scripts are required.

## API

`GET /health` returns HTTP 200 with `{"status":"ok"}`.

`POST /agent` accepts:

```json
{
  "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
  "query": "thanks!",
  "sessionId": "test-session-1001",
  "messageType": "TEXT"
}
```

It returns HTTP 200 with the model's answer, for example:

```json
{"message": "You're welcome!"}
```

`userId` must be a UUID. `query` must contain 1–20,000 characters after trimming, and
`sessionId` must contain 1–128 characters. Missing, invalid, or extra fields return HTTP 422.
`messageType` accepts `TEXT` (the default when omitted) or `HUMAN_FLOW_REPLY`. Streamlit sends
`TEXT`. `HUMAN_FLOW_REPLY` submits a decision for a saved operation, as described below; its
`query` must be empty or omitted.
The frontend uses `CHAT_USER_ID` and generates a separate session UUID per chat. Reusing both
identifiers restores that conversation's framework state before running the next message.
There is no authentication yet; user IDs partition local development data, rather than proving
the caller's identity.

Stock investment requests use **`build_investment_assessment(ticker, risk_tolerance, horizon_months)`**.
The tool fetches Yahoo financial statements using yfinance (no API key) and market data through
the existing Alpaca MCP connection, then runs deterministic calculations. The agent still uses
retrieval and relevant market/news tools for research and context; it does not copy numbers into
the calculator. Risk profiles: **1 Conservative, 2 Moderate (default), 3 Aggressive**; horizons:
**6?36 months**. ETFs need a separate model and are explicitly unsupported by corporate scoring.
Provider failures and missing evidence are reported; assessments never automatically place trades.
See the [investment tool guide](backend/app/investment/README.md) for formulas, data provenance,
limitations and `config.yaml` settings. No document reindex is required for this workflow.

Run traces and evaluations are also available in the **Judge** tab. Each new agent invocation is
recorded in `data/evaluations/runs.csv`; `POST /judge` evaluates selected or missing runs in batches
and saves scores to `evaluations.csv`, replacing the previous evaluation for each run.
`GET /judge` includes runs not evaluated yet. The UI shows
the latest 50 rows with selection checkboxes and **Evaluate selected / Evaluate Missing Runs**.
Alongside retrieval and grounding metrics, **Tool usage** and **Intention success** assess the
agent's tool choices and whether it fulfilled the request (0 failed, 0.5 partial, 1 successful).
Existing evaluations become stale under rubric 2.0; **Evaluate Missing Runs** adds the new scores
by replacing each run's previous evaluation.
See the [evaluation README](backend/app/evaluation/README.md) for CSV schemas, curl examples,
configuration, metric definitions, and reference labels needed for recall@k.

Additional endpoints used by Streamlit:

| Method and path | Input | Result |
| --- | --- | --- |
| `GET /conversations` | Query: `userId` | That user's chats, most recently updated first |
| `GET /conversations/detail` | Query: `userId`, `sessionId` | Saved title, messages, and confirmations |
| `POST /conversations` | JSON: `userId`, optional `sessionId` | Create an empty saved chat |

Conversation APIs do not expose the internal framework session. Missing keys return 503 from
`/agent`; model timeouts return 504, quota/rate limits return 429, and a busy conversation returns
409. Invalid credentials or other model failures return 502. Health and history remain available
without an OpenAI key.

For retries, reuse the original `X-Request-ID`. A completed turn is returned without another model
call or duplicate messages. Reusing the same ID with different text returns 409. Streamlit manages
these IDs automatically.

### Human approval and continuation

When the agent proposes an Alpaca write, `ToolApprovalMiddleware` returns a native Agent Framework
`function_approval_request` without executing the tool. The run **ends**, the API saves the framework
session and approval in that conversation's JSON, and HTTP 200 contains:

```json
{
  "message": "Please confirm or reject the proposed operation(s).",
  "status": "confirmation_required",
  "confirmations": [{
    "confirmationId": "d397f786-6e6b-44ad-9f55-f42330793517",
    "toolName": "alpaca_place_stock_order",
    "arguments": {"symbol": "AAPL", "qty": 1, "side": "buy", "type": "market", "time_in_force": "day"},
    "paperTrading": true,
    "status": "pending",
    "createdAt": "2026-10-24T20:00:00Z"
  }]
}
```

The actual arguments follow the selected tool's schema. Inspect them before deciding. Submit the
returned confirmation ID with the **same userId and sessionId**; do not resend or edit arguments:

```bash
curl http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: approval-reply-1001' \
  -d '{
    "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
    "sessionId": "test-session-1001",
    "messageType": "HUMAN_FLOW_REPLY",
    "confirmationId": "REPLACE_WITH_RETURNED_CONFIRMATION_ID",
    "approved": true,
    "query": ""
  }'
```

Use `approved: false` to reject. Plain chat text such as "yes" never approves a tool. Multiple
proposals have separate IDs and require separate decisions. A continuation may propose another
operation; its response then contains another `confirmation_required` result.

**Restore point:** [`AgentService._continue`](backend/app/services/agent.py) calls
`AgentSession.from_dict(record.agent_session)`, rebuilds the saved approval with `Content.from_dict`,
and supplies `to_function_approval_response(approved=...)` to a new `runtime.run(...)`. Framework
binding ties the response to the original tool and arguments. No HTTP request or coroutine waits
for the human. Pending approvals currently have no expiry and survive a backend restart.

Streamlit displays a confirmation chat bubble with the operation, arguments, paper/live mode,
and **Confirm / Reject** buttons. Text input is disabled only in that conversation. The backend
also rejects new `TEXT` turns there with 409 until its pending decisions are resolved. Other chats
for the same user stay usable, and refreshing the browser reloads pending approvals from storage.

Decisions are claimed on disk before execution. Retrying the same confirmation returns its saved
response without repeating the tool; changing its decision returns 409. Changing Alpaca credentials
or paper/live mode requires rejecting the old proposal and requesting a new one. `completed` means
the approval continuation finished, **not** that a market order has filled; inspect the tool's order
status. Rejections never execute the requested write.

If approved execution or the subsequent model reply fails, the state is `outcome_unknown`; a process crash
can leave `processing`. These states block that conversation and prevent automatic replay. Check
the Alpaca account/order status before making any new trading request in another conversation.
There is no automatic reconciliation/unlock endpoint yet. A failed final storage write likewise
leaves the durable processing claim in place. Do not delete that claim to retry an order blindly.
If a rejection's model reply fails before the final save, the operation returns to pending and
the rejection can be retried safely, since no write was approved.
This prevents application retries of a confirmation, but does not guarantee broker-level exactly-once
execution or deduplicate separately approved proposals in other chats.

Approval events in `logs/backend.log` include `tool_approval_requested`, `tool_approval_decided`,
and `tool_approval_outcome_unknown`, with request/session IDs and the decision's confirmation ID.
Tool arguments and keys are not added to these logs. The MCP write retry override targets the
pinned Agent Framework version; keep the approval and MCP tests passing when upgrading it.

PowerShell example:

```powershell
$payload = @{
    userId = "d5bbf9d1-5945-44d8-bb10-0065164a8356"
    query = "thanks!"
    sessionId = "test-session-1001"
    messageType = "TEXT"
} | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8000/agent -Method Post -ContentType application/json -Body $payload
```

Document APIs are also available: `POST /docs/ingest` for batch file uploads,
`POST /docs/reindex` to rebuild safely from saved originals, and `POST /docs/retrieve` to search
with citation metadata. Pipeline settings live in [config.yaml](config.yaml). See the
[document pipeline README](backend/app/documents/README.md) for curl examples, persistence details,
and every configurable parameter. The API's interactive Swagger page remains at `GET /docs`.

In Streamlit, choose **Document Ingestion**, drag in one or more files, then click **Ingest documents**.
The app sends one multipart batch to FastAPI and shows an elapsed-time loading indicator until
processing finishes. The endpoint does not expose intermediate progress, so no estimated percentage
is shown. Results distinguish added, updated, unchanged, duplicate, and failed documents. File names
serve as source IDs: upload the same name to update a document; different documents need distinct
names. Results stay visible when switching sections, and switching does not resubmit files.

Choose **Reindex** to rebuild the saved document library. Both checkboxes default to unchecked:
**Force reparse** bypasses saved parsing results; **Force reembed** regenerates embeddings and
incurs OpenAI embedding usage costs. Click **Reindex documents** to submit the selected flags to
`/docs/reindex`. A loading indicator runs until completion, then document/chunk counts and generated
versus reused embeddings appear. Results remain visible across navigation without resubmitting.
If rebuilding times out, check the backend logs before starting another rebuild.

Document requests use `DOCUMENT_API_TIMEOUT_SECONDS` (600 seconds by default), independently of chat
requests. If a request times out, the backend may finish processing; retry the same files to check
their status. The backend enforces the limits and supported formats from `config.yaml`; Streamlit's
own upload limit also applies (200 MB per file by default, configurable with `server.maxUploadSize`).

## Structure

```text
backend/
  app/
    main.py                Application factory and lifecycle
    api/router.py          Route registration
    api/routes/            Health, agent, conversation, document, and judge endpoints
    agent/                 Agent factory, OpenAI runtime, and system instructions
    middleware/            Request tracing, API errors, and agent invocation logging
    tools/                 Retrieval tool, evidence formatting, Alpaca MCP lifecycle, and registry
    repositories/          JSON storage, atomic writes, and conversation locking
    documents/             Docling, HybridChunker, embedding cache, and persistent Chroma indexing
    evaluation/            CSV traces, batch LLM judge, scoring, and evaluation orchestration
      metrics/             Independent retrieval and answer metric calculations
    schemas/               Validated request/response models
    services/              Restore, run, and save conversation orchestration
    core/                  Backend configuration and application errors
frontend/
  app.py                   Streamlit entry point
  components/              Sidebar, chat, ingestion, reindex, and judge rendering
  services/                FastAPI HTTP client and error handling
  state/                   Active chat and temporary pending/retry state
  core/                    Frontend configuration
  assets/                  UI styling
shared/
  conversations.py         Shared API contracts
  evaluations.py           Shared run/evaluation CSV and HTTP contracts
  logging_config.py        JSON logging
  settings.py              Common settings
  sessions/
    conversations/         Saved JSON files, grouped by user ID (Git-ignored)
tests/
  backend/                 Contracts, framework persistence, errors, and concurrency
  frontend/                HTTP errors and interactive chat workflows
.streamlit/config.toml     Theme and local server defaults
.env.example              Configuration template
logs/                     Generated logs, ignored by Git
config.yaml               Central document-pipeline and evaluation configuration
data/                     Generated originals, cache, Chroma, and evaluation CSVs (Git-ignored)
```

Edit `backend/app/agent/prompts.py` for system instructions. Add future tools under
`backend/app/tools/` and return them from `get_tools(settings)`. The agent factory already uses this
registry. `middleware/agent_logging.py` wraps each agent run, and
`middleware/function_logging.py` wraps individual function/tool calls. Function middleware logs
the function name, request/session IDs, duration, and error type while leaving arguments and
results out of the logs. Both are registered in the agent factory; future tools automatically
use them.

The agent's `retrieve_documents` tool posts `{"query":"..."}` to `/docs/retrieve`. The system
instructions tell the model to use it for questions about uploaded documents and financial filings.
The tool validates the endpoint response, sorts and deduplicates chunks, and returns structured
evidence with stable citation labels, filenames, source URLs, pages, headings, and table row references.
Complete chunks retain their original figures, units, and periods. A configurable token budget omits
whole chunks instead of cutting rows; the output reports omitted results, empty searches, and errors
separately. The model composes the final answer from this evidence and is instructed to cite sources
and treat retrieved text as untrusted data. Retrieved evidence is sent to the OpenAI chat model and
saved as part of the local framework conversation history.

Set `RETRIEVAL_API_BASE_URL` to the backend address reachable from the backend process (default
`http://127.0.0.1:8000`). Update it if you change the API port or deployment address. The tool uses
the retrieval defaults in `config.yaml`, including the result count and distance filter. Requests
carry the original chat's `X-Request-ID` so retrieval and function middleware logs can be correlated.

## Saved conversations

```text
shared/sessions/
  conversations/
    <userId>/
      <sha256(sessionId)>.json
  .locks/                  Cross-process locks for each user/session pair
```

Each JSON contains `userId`, `sessionId`, title, timestamps, visible `messages`, a schema version,
and the serialized `agentSession`. The filename hashes the session ID to support arbitrary IDs
without unsafe paths or Windows filename collisions; the original ID is inside the JSON.

FastAPI loads the file, restores `AgentSession`, runs the agent, and atomically replaces the file
with the updated framework state and messages. Both are committed together after a successful
reply. Failures keep the last saved state intact. Requests for the same user/session are serialized
with a file lock; unrelated conversations can run independently. Unreadable files produce an error
and are not silently replaced.

Streamlit reads its sidebar and transcripts through the conversation endpoints on each rerun.
**New chat** creates a JSON file immediately; sending the first message from the initial welcome
screen creates one after a successful reply. **Refresh chats** reloads chats created by other clients.
The old browser-only placeholder chats are discarded automatically on the first updated app run.
Unsent messages are temporary UI state and can be retried or discarded.

JSON files contain conversation text and are ignored by Git. This local storage design assumes
both applications use the same backend and that backend processes share one filesystem. There is
no automatic context truncation; very long chats may eventually reach the selected model's limit.

## Configuration

Settings load from the root `.env`; environment variables take precedence. Restart the apps
after changing settings.

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_BASE_URL` | `http://127.0.0.1:8000` | Backend address used by Streamlit |
| `API_TIMEOUT_SECONDS` | `90` | Streamlit HTTP timeout; keep above the agent timeout |
| `DOCUMENT_API_TIMEOUT_SECONDS` | `600` | Streamlit HTTP timeout for ingestion and reindexing |
| `JUDGE_API_TIMEOUT_SECONDS` | `600` | Streamlit HTTP timeout for judge batches |
| `OPENAI_API_KEY` | Empty | Required for generating replies, embeddings, and judge evaluations |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Model passed to the OpenAI provider |
| `AGENT_TIMEOUT_SECONDS` | `60` | Maximum duration of an agent run |
| `RETRIEVAL_API_BASE_URL` | `http://127.0.0.1:8000` | Backend address used by the agent retrieval tool |
| `RETRIEVAL_TOOL_TIMEOUT_SECONDS` | `30` | Retrieval HTTP timeout, within the overall agent timeout |
| `RETRIEVAL_TOOL_MAX_TOKENS` | `8000` | Tool evidence budget using cl100k_base; range 256–32000 |
| `ALPACA_MCP_ENABLED` | `true` | Connect Alpaca when both credentials are configured |
| `ALPACA_API_KEY` | Empty | Alpaca API key, passed only to the MCP subprocess |
| `ALPACA_SECRET_KEY` | Empty | Alpaca secret key, passed only to the MCP subprocess |
| `ALPACA_PAPER_TRADE` | `true` | Use the paper trading environment; requires matching keys |
| `ALPACA_TOOLSETS` | `stock-data,assets,news` | Comma-separated toolsets; the example file may select account/trading too |
| `ALPACA_MCP_REQUEST_TIMEOUT_SECONDS` | `30` | Individual MCP request timeout; also applies during startup |
| `CHAT_USER_ID` | UUID from the example above | Stable Streamlit development identity |
| `SESSIONS_DIR` | `shared/sessions` | Storage root, absolute or relative to this repo |
| `DOCS_CONFIG_PATH` | `config.yaml` | Central document-pipeline and evaluation YAML configuration |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, or CRITICAL |
| `LOG_DIR` | `logs` | Absolute path or path relative to this repo |
| `LOG_MAX_BYTES` | `5242880` | Rotate each log after 5 MiB |
| `LOG_BACKUP_COUNT` | `5` | Number of rotated files retained per app |

Streamlit calls FastAPI from its Python process, so no browser CORS setup is needed.

## Debugging

Application logs are JSON lines written to the console and separate rotating files:

- `logs/backend.log`: lifecycle, requests, agent execution, storage events, and server exceptions.
- `logs/frontend.log`: browser session startup, outgoing requests, timing, and failures.

Each request carries `X-Request-ID` from Streamlit through FastAPI and back in the response
header. Both logs include that ID, status, and duration. The backend generates an ID for other
clients and replaces malformed IDs. Failed chat requests show a reference ID and a retry button.
Request bodies and chat text are not included in normal application logs. Unexpected backend
exceptions include tracebacks, so treat log files as internal debugging data.

```powershell
Get-Content logs\backend.log -Tail 20 -Wait
Select-String -Path logs\*.log -Pattern "YOUR-REQUEST-ID"
```

`--no-access-log` avoids a second unstructured access line from Uvicorn; its lifecycle output
and Streamlit's own runtime output remain in their consoles. Run one worker per app with this
local file logger. Multiple production workers should write to a central log collector instead.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

The chat tests use Streamlit's AppTest and FastAPI's TestClient. Agent Framework and the OpenAI
SDK run normally; only the outbound OpenAI HTTP boundary is mocked, so tests need no API key and
incur no API charges. Checks cover restored context after restart, user/session isolation,
browser refreshes, retries, atomic write failures, corrupt files, model failures, and concurrent turns.
Document tests exercise real Docling parsing/chunking and persistent Chroma with mocked embedding
HTTP responses, including duplicate detection, updates, restart recovery, and safe reindexing.
Alpaca tests use a real local MCP subprocess with deterministic fixture tools and the mocked OpenAI
boundary. They verify tool discovery, execution, errors, shared lifecycle, logging, and cleanup
without calling Alpaca APIs or placing orders. Alpaca itself runs in a separate uv environment;
its pinned package is declared in `backend/app/tools/alpaca.py`, outside the backend lock file.

Framework references: [FastAPI request models](https://fastapi.tiangolo.com/tutorial/body/),
[Streamlit chat UI](https://docs.streamlit.io/develop/tutorials/chat-and-llm-apps/build-conversational-apps),
and [Streamlit AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest).

Agent references: [Microsoft Agent Framework core](https://pypi.org/project/agent-framework-core/),
[OpenAI provider](https://pypi.org/project/agent-framework-openai/),
[framework sessions](https://learn.microsoft.com/en-us/agent-framework/concepts/agents/conversations/),
and [OpenAI API setup](https://developers.openai.com/api/docs/quickstart).
