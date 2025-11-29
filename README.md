# AI Log Monitoring Agent

A small FastAPI service that monitors logs with AI-driven prompts. It stores targets, command-based prompt monitors, and run history in SQLite by default (with an optional Postgres connector), runs monitors on an interval, and can send email or SMS alerts.

## Features

- Manage **Targets**, optional **LogSources**, **PromptMonitors**, and **MonitorRuns** via HTTP API.
- Execute explicit shell commands per monitor input to gather labeled log text.
- Pluggable LLM analysis with OpenAI default and optional Amazon Q Business support.
- Simple scheduler that runs monitors based on `interval_seconds`.
- Email notifications via SMTP and stubbed SMS notifications (skipped when no recipients are configured).

## Running locally

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set environment variables for command execution safety, LLM provider selection, and SMTP (email alerts optional):

```bash
export COMMAND_TIMEOUT_SECONDS=60   # per-command timeout for monitor inputs
export MAX_COMMAND_WORKERS=4        # max concurrent command inputs
export LLM_PROVIDER=openai          # or amazon_q or stub for offline testing
export OPENAI_API_KEY=sk-...        # required when LLM_PROVIDER=openai
export OPENAI_MODEL=gpt-4.1-mini    # model name passed to OpenAI
export QBUSINESS_APP_ID=app-id      # required when LLM_PROVIDER=amazon_q
export AWS_REGION=us-east-1         # AWS region for Amazon Q Business
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587
export SMTP_USERNAME=user
export SMTP_PASSWORD=pass
export SMTP_FROM=alerts@example.com
export MAX_RUN_HISTORY_PER_MONITOR=200 # optional: cap stored runs per monitor
export DATABASE_BACKEND=sqlite         # or postgres if desired
export DEFAULT_TARGET_NAME=local       # optional: auto-created target name when none exist
```

`LLM_PROVIDER` accepts `openai`, `amazon_q`, or stub-friendly values (`stub`, `dummy`, `mock`).

Set `LLM_PROVIDER=stub` to run without external credentials; the stub client returns deterministic statuses based on log text
and is useful for local development.

You can also override the provider per monitor by setting `llm_provider` (and optional
`llm_provider_metadata`) in the monitor payload when you need different backends for
different checks. Metadata is merged with provider defaults, so you can override only the
fields you need (e.g., `{ "model": "gpt-4.1" }` for OpenAI or `{ "application_id": "...",
"region": "..." }` for Amazon Q) while still keeping sensible fallbacks.

3. Start the API:

```bash
uvicorn app.main:app --reload
```

The service stores data in `monitor.db` by default. Override with `DATABASE_PATH` if needed.

Set `SCHEDULER_ENABLED=false` to disable the background dispatcher when you only want manual `run-once`
invocations (helpful for local testing or running under an external scheduler). When disabled, health
reports the scheduler as disabled but still returns `status="ok"` if the database is reachable.

On startup, the service will auto-create a single local target named by `DEFAULT_TARGET_NAME` when no targets exist. You can
rename it via the env var or create additional targets via the API at any time.

### Optional: use Postgres instead of SQLite

Provide a Postgres connection string and switch the backend to `postgres` to use a shared database:

```bash
export DATABASE_BACKEND=postgres
export DATABASE_URL=postgresql://user:pass@localhost:5432/monitoring
```

The service will initialize the same schema in Postgres. The connector uses `psycopg`; ensure the dependency is installed (it is
included in `requirements.txt`). If these variables are unset, SQLite remains the default.

On startup, the service will automatically add any missing columns to existing SQLite or Postgres databases so upgrades keep
schemas aligned without manual migrations. Common indexes (by target, log source, and monitor run fields) are also created when
missing to keep lookups fast as history grows.

## Example workflow

### 1) Create a target

```bash
curl -X POST http://localhost:8000/targets \
  -H "Content-Type: application/json" \
  -d '{"name": "trade-bot-host-1", "type": "docker_host", "connection_config": {"host": "localhost"}}'
```

### 2) (Optional) Create a Docker log source for reuse

```bash
curl -X POST http://localhost:8000/log-sources \
  -H "Content-Type: application/json" \
  -d '{
    "target_id": "<target-id>",
    "name": "btc-bot-docker-log",
    "mode": "docker_logs",
    "config": {"container_name": "btc_trading_service"}
  }'
```

### 3) Create a monitor with explicit command inputs and a custom prompt (15-minute interval)

```bash
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "BTC bot anomaly detector",
    "target_id": "<target-id>",
    "interval_seconds": 900,
    "prompt": "You are monitoring a crypto trading bot. Look for errors, stuck loops, abnormal fills, and API failures.",
    "llm_provider": "openai",
    "inputs": [
      {"label": "FULL_LOGS", "mode": "command", "command": "docker compose -f docker-compose-multi.yml logs --since 1h"},
      {"label": "GRID_HEALTH", "mode": "command", "command": "docker compose -f docker-compose-multi.yml logs --since 1h | grep -E \"GRID DEPTH|GRID INVARIANT\""},
      {"label": "BUDGET", "mode": "command", "command": "docker compose -f docker-compose-multi.yml logs --since 1h | grep -E \"GRID BUDGET|BUDGET-CONSTRAINED|skipped.*budget\""},
      {"label": "ERRORS", "mode": "command", "command": "docker compose -f docker-compose-multi.yml logs --since 1h | grep -E \"ERROR|WARNING|INSUFFICIENT|failed|Exception|Traceback\""}
    ],
    "window_config": {"max_lines": 500, "max_chars": 8000},
    "notification_config": {"email_recipients": ["ops@example.com"], "notify_on": "alert_only"}
  }'
```

Add optional `timeout_seconds`, `workdir`, or `env` keys to any input if individual commands need different limits, working directories, or environment overrides than the global defaults.

### 4) Trigger a manual run

```bash
curl -X POST http://localhost:8000/monitors/<monitor-id>/run-once
```

### 5) Test a log source connection

```bash
curl -X POST http://localhost:8000/log-sources/<log-source-id>/test
```

### 6) List runs for a monitor

```bash
curl http://localhost:8000/monitors/<monitor-id>/runs
```

### 7) Fetch the latest run for quick status

```bash
curl http://localhost:8000/monitors/<monitor-id>/runs/latest
```

### 8) Health check

```bash
curl http://localhost:8000/health
```

The response reports whether the scheduler is enabled and running, the database connection is reachable, which database backend
is active, and LLM readiness details (`llm_provider`, `llm_ready`, `llm_message`, `supported_llm_providers`).

### 9) List LLM providers and readiness

```bash
curl http://localhost:8000/llm/providers
```

Filter to a specific provider (e.g., `openai`, `amazon_q`, `stub`) to verify credential presence:

```bash
curl "http://localhost:8000/llm/providers?provider=openai"
```

Run history is automatically trimmed after each execution to keep at most `MAX_RUN_HISTORY_PER_MONITOR` records per monitor (default: 200), so long-running deployments don't accumulate unbounded history. Set the environment variable to adjust retention.

Each run record stores the `llm_provider` and `llm_provider_metadata` (e.g., OpenAI model name or Amazon Q region/app ID) so you can verify which backend produced a given result when debugging.

### LLM contract

The LLM receives a monitoring prompt and aggregated logs grouped by labels like `[FULL_LOGS]` and `[ERRORS]`. Providers must return JSON with:

```json
{
  "status": "HEALTHY" | "WARNING" | "CRITICAL",
  "summary": "short overall summary",
  "report": "multi-section human-readable report",
  "recommendations": ["next steps", "..."]
}
```

Statuses map to internal `ok`, `warn`, and `alert` for notifications.

### 10) Update existing resources

- Update a target's metadata:

```bash
curl -X PUT http://localhost:8000/targets/<target-id> \
  -H "Content-Type: application/json" \
  -d '{"name": "trade-bot-host-1a", "connection_config": {"host": "localhost"}}'
```

- Update a log source (e.g., move to a new container or tweak config):

```bash
curl -X PUT http://localhost:8000/log-sources/<log-source-id> \
  -H "Content-Type: application/json" \
  -d '{"name": "btc-bot-updated", "config": {"container_name": "btc_trading_service_v2"}}'
```

### 11) Delete resources

- Delete a monitor (removes its run history first):

```bash
curl -X DELETE http://localhost:8000/monitors/<monitor-id>
```

- Delete a log source (must not have monitors attached):

```bash
curl -X DELETE http://localhost:8000/log-sources/<log-source-id>
```

- Delete a target (must not have log sources attached):

```bash
curl -X DELETE http://localhost:8000/targets/<target-id>
```

## Extending

- Replace `analyze_logs_with_llm` in `app/llm_client.py` with a real LLM integration (OpenAI, Anthropic, local model, etc.).
- Implement a real SMS sender in `app/notifications.py` for production alerts.
- Add new log reader implementations to `app/log_reader.py` for Kubernetes, journald, or remote hosts.

## Log inputs / commands per monitor

Each PromptMonitor defines exactly which commands to run so the agent never "guesses". Add an `inputs` array to your monitor payload, where each object looks like:

```json
{
  "label": "GRID_HEALTH",
  "mode": "command",
  "command": "docker compose -f docker-compose-multi.yml logs --since 1h | grep -E \"GRID DEPTH|GRID INVARIANT\"",
  "timeout_seconds": 90,
  "workdir": "/opt/trading",
  "env": {"TRADING_ENV": "prod"}
}
```

- `label`: short identifier for this slice of logs (e.g. `FULL_LOGS`, `GRID_HEALTH`, `BUDGET`, `ERRORS`).
- `mode`: currently `command` (runs a shell command and captures stdout). More modes can be added later.
- `command`: the exact shell command to run on the target host to fetch logs.
- `timeout_seconds`: optional per-input timeout; falls back to `COMMAND_TIMEOUT_SECONDS` when omitted.
- `workdir`: optional working directory for the command execution.
- `env`: optional map of environment variables merged into the process environment for this command.

Labels must be unique within a monitor, and both `label` and `command` must be non-empty strings. Inputs with blank values are
rejected during validation so every run has predictable, labeled outputs.

During execution, the monitor runner:

1. Runs each command concurrently (bounded by `MAX_COMMAND_WORKERS`) so long-running inputs do not block others.
2. Captures stdout text, truncating each input according to `window_config` (supports `max_lines` and `max_chars`).
3. Stores results keyed by label. If every input fails, the monitor run is marked as an error and notifications follow the `alert_only`/`warn_and_alert`/`all` rules.
4. Builds a single aggregated `logs_text` grouped by label and applies a final truncation pass with `window_config` so the combined payload cannot exceed your defined bounds, e.g.:

```
[FULL_LOGS]
...full log text...

[GRID_HEALTH]
...grid-related lines...

[BUDGET]
...budget-related lines...

[ERRORS]
...error and warning lines...
```

This aggregated text and your monitor prompt are passed to `analyze_logs_with_llm`. The LLM only receives the labeled log text and never executes commands itself.

## LLM behavior & contract

- `analyze_logs_with_llm(monitor_prompt, logs_text)` combines a system role (monitoring assistant) and a user message that includes your monitor prompt plus the aggregated logs.
- The logs will be provided as a single text block, grouped by labels in the form:

```
[LABEL_NAME]
log lines...
```

Use these labels (e.g. `FULL_LOGS`, `GRID_HEALTH`, `BUDGET`, `ERRORS`) together with the instructions in `monitor_prompt` to perform your analysis.
