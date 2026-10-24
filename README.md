# Credicorp Agent Chat

A FastAPI backend and a Streamlit chat application powered by Microsoft Agent Framework and
OpenAI. The UI has one **Agent Chat** section, a sidebar of saved conversations, and a chat area
with a message composer. Context and messages survive browser refreshes and application restarts.

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

## Run

Start the backend in one terminal:

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

These commands use the virtual environment directly, so PowerShell activation is optional.
To activate it, run `.\.venv\Scripts\Activate.ps1`.

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

The standalone `streamlit run frontend/app.py` command also works in the activated environment.

On macOS/Linux, use `.venv/bin/python` in place of `.\.venv\Scripts\python.exe`.
Stop either process with **Ctrl+C**.

## API

`GET /health` returns HTTP 200 with `{"status":"ok"}`.

`POST /agent` accepts:

```json
{
  "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
  "query": "thanks!",
  "sessionId": "test-session-1001"
}
```

It returns HTTP 200 with the model's answer, for example:

```json
{"message": "You're welcome!"}
```

`userId` must be a UUID. `query` must contain 1–20,000 characters after trimming, and
`sessionId` must contain 1–128 characters. Missing, invalid, or extra fields return HTTP 422.
The frontend uses `CHAT_USER_ID` and generates a separate session UUID per chat. Reusing both
identifiers restores that conversation's framework state before running the next message.
There is no authentication yet; user IDs partition local development data, rather than proving
the caller's identity.

Additional endpoints used by Streamlit:

| Method and path | Input | Result |
| --- | --- | --- |
| `GET /conversations` | Query: `userId` | That user's chats, most recently updated first |
| `GET /conversations/detail` | Query: `userId`, `sessionId` | Saved title and visible messages |
| `POST /conversations` | JSON: `userId`, optional `sessionId` | Create an empty saved chat |

Conversation APIs do not expose the internal framework session. Missing keys return 503 from
`/agent`; model timeouts return 504, quota/rate limits return 429, and a busy conversation returns
409. Invalid credentials or other model failures return 502. Health and history remain available
without an OpenAI key.

For retries, reuse the original `X-Request-ID`. A completed turn is returned without another model
call or duplicate messages. Reusing the same ID with different text returns 409. Streamlit manages
these IDs automatically.

PowerShell example:

```powershell
$payload = @{
    userId = "d5bbf9d1-5945-44d8-bb10-0065164a8356"
    query = "thanks!"
    sessionId = "test-session-1001"
} | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8000/agent -Method Post -ContentType application/json -Body $payload
```

## Structure

```text
backend/
  app/
    main.py                Application factory and lifecycle
    api/router.py          Route registration
    api/routes/            Health, agent, and conversation endpoints
    agent/                 Agent factory, OpenAI runtime, and system instructions
    middleware/            Request tracing, API errors, and agent invocation logging
    tools/                 Future tool registration (currently empty)
    repositories/          JSON storage, atomic writes, and conversation locking
    schemas/               Validated request/response models
    services/              Restore, run, and save conversation orchestration
    core/                  Backend configuration and application errors
frontend/
  app.py                   Streamlit entry point
  components/              Sidebar and chat rendering
  services/                FastAPI HTTP client and error handling
  state/                   Active chat and temporary pending/retry state
  core/                    Frontend configuration
  assets/                  UI styling
shared/
  conversations.py         Shared API contracts
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
```

Edit `backend/app/agent/prompts.py` for system instructions. Add future tools under
`backend/app/tools/` and return them from `get_tools()`. The agent factory already uses this
registry. `middleware/agent_logging.py` wraps each agent run, and
`middleware/function_logging.py` wraps individual function/tool calls. Function middleware logs
the function name, request/session IDs, duration, and error type while leaving arguments and
results out of the logs. Both are registered in the agent factory; future tools automatically
use them.

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
| `OPENAI_API_KEY` | Empty | Required for generating replies |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Model passed to the OpenAI provider |
| `AGENT_TIMEOUT_SECONDS` | `60` | Maximum duration of an agent run |
| `CHAT_USER_ID` | UUID from the example above | Stable Streamlit development identity |
| `SESSIONS_DIR` | `shared/sessions` | Storage root, absolute or relative to this repo |
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

Framework references: [FastAPI request models](https://fastapi.tiangolo.com/tutorial/body/),
[Streamlit chat UI](https://docs.streamlit.io/develop/tutorials/chat-and-llm-apps/build-conversational-apps),
and [Streamlit AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest).

Agent references: [Microsoft Agent Framework core](https://pypi.org/project/agent-framework-core/),
[OpenAI provider](https://pypi.org/project/agent-framework-openai/),
[framework sessions](https://learn.microsoft.com/en-us/agent-framework/concepts/agents/conversations/),
and [OpenAI API setup](https://developers.openai.com/api/docs/quickstart).
