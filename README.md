# AI Log Monitoring Agent

A small FastAPI service that monitors container or file logs with AI-driven prompts. It stores targets, log sources, and prompt-based monitors in SQLite, runs monitors on an interval, and can send email or SMS alerts.

## Features

- Manage **Targets**, **LogSources**, **PromptMonitors**, and **MonitorRuns** via HTTP API.
- Read logs from local files or Docker containers.
- Stubbed LLM analysis with a clear interface to plug in a real provider later.
- Simple scheduler that runs monitors based on `interval_seconds`.
- Email notifications via SMTP and stubbed SMS notifications (skipped when no recipients are configured).

## Running locally

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set environment variables for command execution safety and SMTP (email alerts optional):

```bash
export COMMAND_TIMEOUT_SECONDS=60   # per-command timeout for monitor inputs
export MAX_COMMAND_WORKERS=4        # max concurrent command inputs
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587
export SMTP_USERNAME=user
export SMTP_PASSWORD=pass
export SMTP_FROM=alerts@example.com
```

3. Start the API:

```bash
uvicorn app.main:app --reload
```

The service stores data in `monitor.db` by default. Override with `DATABASE_PATH` if needed.

## Example workflow

### 1) Create a target

```bash
curl -X POST http://localhost:8000/targets \
  -H "Content-Type: application/json" \
  -d '{"name": "trade-bot-host-1", "type": "docker_host", "connection_config": {"host": "localhost"}}'
```

### 2) Create a Docker log source

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

### 3) Create a monitor with a custom prompt (5-minute interval)

```bash
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "BTC bot anomaly detector",
    "log_source_id": "<log-source-id>",
    "interval_seconds": 300,
    "prompt": "You are monitoring a crypto trading bot. Look for errors, stuck loops, abnormal fills, and API failures.",
    "inputs": [
      {
        "label": "FULL_LOGS",
        "mode": "command",
        "command": "docker logs --since 5m btc_trading_service"
      }
    ],
    "window_config": {"max_lines": 500, "max_chars": 8000},
    "notification_config": {"email_recipients": ["ops@example.com"], "notify_on": "alert_only"}
  }'
```

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

The response reports whether the scheduler task is alive and the database connection is reachable.

### 9) Update existing resources

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

### 10) Delete resources

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
  "command": "docker compose -f docker-compose-multi.yml logs --since 1h | grep -E \"GRID DEPTH|GRID INVARIANT\""
}
```

- `label`: short identifier for this slice of logs (e.g. `FULL_LOGS`, `GRID_HEALTH`, `BUDGET`, `ERRORS`).
- `mode`: currently `command` (runs a shell command and captures stdout). More modes can be added later.
- `command`: the exact shell command to run on the target host to fetch logs.

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
