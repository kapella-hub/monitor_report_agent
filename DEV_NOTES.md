### Dev Notes – Architecture & Implementation Status

This project implements an AI-driven log monitoring agent with the following main pieces:

1. **FastAPI app & lifecycle**
   - `app.main:app` is the FastAPI application.
   - Uses a lifespan context to start/stop the background scheduler (`monitor_dispatcher`).
   - Simple API-token guard via `require_api_key` and `X-API-Key` header when `API_TOKEN` is set.

2. **Core entities (schemas + storage)**
   - `Target` / `TargetCreate` / `TargetUpdate` in `app/schemas.py` with persistence in `Storage` (`targets` table).
   - `LogSource` (optional file/docker log readers) in `schemas` + `log_sources` table.
   - `PromptMonitor` in `schemas` + `monitors` table:
     - Fields: `id`, `name`, `target_id`, optional `log_source_id`, `interval_seconds`, `prompt`, `inputs`,
       `window_config`, `notification_config`, **`remediation_config`**, `llm_provider`, `llm_provider_metadata`,
       `last_run_at`, `enabled`.
   - `MonitorRun` in `schemas` + `monitor_runs` table with LLM request/response payloads and status.

3. **Scheduling (polling loop)**
   - `app.scheduler.monitor_dispatcher` runs in a background task when `SCHEDULER_ENABLED=true`.
   - Every `SCHEDULER_TICK_SECONDS`, it:
     - Loads monitors via `storage.list_monitors()`.
     - Checks `enabled`, `interval_seconds`, and `last_run_at`.
     - When due, schedules `service.run_monitor(monitor)` and updates `last_run_at` via `touch_monitor_last_run`.

4. **Log collection**
   - Primary entry: `service.collect_monitor_inputs`.
   - Uses `_collect_monitor_logs` to:
     - Run explicit `inputs` commands concurrently (mode = `command`, via `_run_command`).
     - Optionally read from a configured `LogSource` using `log_reader.read_logs` (file/docker drivers).
     - Apply per-input and global `window_config` for `max_lines` / `max_chars` via `_truncate_output`.
     - Aggregate into a single `logs_text` block grouped by `[LABEL]` sections.

5. **LLM abstraction**
   - `LLMClient` protocol + concrete implementations in `app.llm_client`:
     - `OpenAIClient` (default provider) using `openai.AsyncOpenAI`.
     - `AmazonQClient` using `boto3.client("qbusiness")`.
     - `StubLLMClient` for local/testing (no external calls).
   - Provider chosen via `settings.llm_provider` or `monitor.llm_provider` and created by `get_llm_client`.
   - All providers implement `analyze_logs(monitor_prompt, logs_text, provider_metadata=...)` and must return:

     ```json
     {
       "status": "HEALTHY" | "WARNING" | "CRITICAL",
       "summary": "short overall summary",
       "report": "full multi-section human-readable report",
       "recommendations": ["..."]
     }
     ```

6. **Monitor execution & history**
   - `service.run_monitor`:
     - Creates a `MonitorRun` row at start (status `None`).
     - Calls `collect_monitor_inputs` to build labeled `logs_text`.
     - Calls `llm_client.analyze_logs(...)` and maps the LLM status via `_map_llm_status` → internal `ok|warn|alert|error`.
     - Truncates stored LLM input/output via `_truncate_storage` and updates the run row.
     - Prunes history using `settings.max_run_history_per_monitor`.
     - Invokes `_maybe_remediate` (see below) and `_maybe_notify`.

7. **Notifications**
   - `NotificationConfig` in `schemas` with:
     - `email_recipients`, `sms_recipients`, `notify_on` (`alert_only` | `warn_and_alert` | `all`).
   - `_should_notify` and `_maybe_notify` in `service`:
     - Map LLM status → internal status.
     - Respect `notify_on` rules.
     - Use `notifications.send_email` (SMTP) and `notifications.send_sms` (Twilio or stub).

8. **Remediation hook (new)**
   - `RemediationConfig` in `schemas` and `remediation_config` column in the `monitors` table.
   - Shape:
     - `enabled: bool` – master switch.
     - `trigger_on: list[str]` – LLM-level statuses (e.g. `CRITICAL`, `WARNING`) that should trigger incidents.
     - `remediation_endpoint: str | None` – HTTP URL for the remediation agent.
     - `max_auto_actions: int | None` – forwarded metadata.
     - `require_human_approval: bool | None` – forwarded metadata.
   - `service._maybe_remediate` (called from `run_monitor` **before** notifications):
     - Checks `remediation_config.enabled`, `remediation_endpoint`, and whether the LLM `status` is in
       `remediation_config.trigger_on`.
     - Builds an incident payload:

       ```json
       {
         "incident_id": "<uuid>",
         "monitor_id": "...",
         "monitor_name": "...",
         "severity": "CRITICAL" | "WARNING" | ...,   // raw LLM status (uppercased)
         "summary": "...",                            // from latest run
         "report": "...",
         "recommendations": ["..."],
         "logs_excerpt": "...",                        // truncated logs_text
         "timestamp": "<ISO8601>",
         "remediation_metadata": {                      // optional
           "max_auto_actions": 5,
           "require_human_approval": true
         }
       }
       ```

     - Sends it as `POST <remediation_endpoint>` using `httpx.AsyncClient` (10s timeout).
     - Logs success/failure but never changes the monitor run outcome if remediation fails.

9. **API surface**
   - Targets: `POST/GET/PUT/DELETE /targets`.
   - Log sources: `POST/GET/PUT/DELETE /log-sources`, `POST /log-sources/{id}/test`.
   - Monitors:
     - `POST /monitors` – create `PromptMonitor` (supports inputs, window_config, notification_config,
       **remediation_config**, llm_provider, etc.).
     - `GET /monitors`, `GET /monitors/{id}`, `PUT /monitors/{id}`, `DELETE /monitors/{id}`.
     - `POST /monitors/{id}/run-once` – immediate execution.
     - `POST /monitors/{id}/collect` – collect inputs only, return `MonitorCollection`.
     - `GET /monitors/{id}/status`, `GET /monitors/status` – scheduling snapshots.
     - `POST /monitors/{id}/enable`, `POST /monitors/{id}/disable`.
   - Runs:
     - `GET /monitors/{id}/runs`, `GET /monitors/{id}/runs/latest`, `GET /runs/{run_id}`.

10. **What is still intentionally out of scope**
    - The remediation agent itself (acting on incidents) is external; this service only emits HTTP incidents.
    - Non-local/remote command execution and richer target types are not yet implemented; targets are effectively
      metadata + grouping for now.

These notes are kept deliberately high-level to help future changes stay aligned with the intended architecture.
