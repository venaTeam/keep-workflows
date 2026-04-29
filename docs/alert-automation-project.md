# Alert-Driven Automation Platform for Infrastructure & Application Teams

> Half-page project description prepared for the managers' workshop (session 2 with Tomi).

## The pain

Every time an alert fires on one of our shared platforms — a database, a Kafka cluster, an Airflow scheduler, anything in the cloud infra stack — an L2/L3 engineer has to manually triage it: pull context from three dashboards, cross-reference a wiki runbook, open a ticket, ping the right channel, maybe restart a pod or scale a consumer group. The runbooks live as prose in wikis, not as code. Engineers paged at 03:00 rediscover the same playbook for the seventh time. Mean-time-to-remediate is dominated by glue work, not by diagnosis. Alert fatigue grows, the on-call rotation burns out, and avoidable downtime reaches the business. The application teams that depend on these platforms have no self-service way to react to alerts that affect them — every change is gated on the platform team's calendar.

## The goal

Give every L2/L3 team — and the application teams downstream of them — a self-service, low-code way to author automations that run automatically the moment a matching alert arrives. The engineer who owns the alert should own its runbook, expressed as version-controlled code, observable, auditable, and shared across the org.

Success metrics:

- Lower MTTR.
- Lower percentage of alerts that page a human.
- Fewer duplicate manual runbook executions per week.
- More automated alert→action paths owned per team.

## The solution

We are deploying **KeepHQ** as the organisation's alert-automation backbone. Keep ingests alerts from every observability source we already use (Prometheus, Datadog, Grafana, Elastic, vendor webhooks, Kafka topics), normalises them to a common schema, deduplicates by fingerprint and enriches them with context.

Each team writes its automations as a declarative YAML *workflow* with three parts:

- A **trigger** — a CEL expression matching incoming alerts, e.g. `source == "prometheus" && labels.team == "kafka" && severity == "critical"`.
- A sequence of **steps** that pull live context — SQL queries, HTTP calls, `kubectl`.
- A sequence of **actions** that change the world — open a ServiceNow ticket, post a structured Slack message, restart a pod, scale a deployment.

60+ provider integrations ship out-of-the-box, so a typical workflow is ~30 lines of YAML — authorable by anyone on an L2/L3 team without writing Python. Multi-tenancy, throttling, retries, severity-change and on-change gates, and per-execution audit trails let teams roll automations out incrementally: start with a Slack notification, graduate to ticket creation, eventually close the loop with remediation.

## Architecture note: why we are not refactoring to Argo Workflows or Airflow

Keep's runtime today is a single-node thread-pool scheduler with an in-memory queue, polled every second. It already ships with the plumbing for a distributed Redis-backed queue (ARQ); the path to horizontal scale is a configuration flip, not a re-platform.

A move to **Argo Workflows** (K8s, container-per-step, designed for ML/batch DAGs) or **Airflow** (Python DAGs, scheduled batch orchestration) would cost us the entire value proposition — the 60+ alert/notification providers, the alert→workflow CEL matching, the dedup/enrichment/severity-change semantics, and the low-code YAML authoring experience our L2/L3 audience needs.

We will ship on Keep's existing engine and address scale by enabling its built-in distributed mode if and when load demands it.

---

## Appendix: technical findings from the repo deep-dive

For defending the architectural recommendation if it comes up in the workshop.

### Workflow definition

Declarative YAML with three parts: `triggers` (alert / incident / interval / manual), `steps` (provider `.query()` calls that fetch context), `actions` (provider `.notify()` calls that act on the world). The same `Step` class executes both; only the provider method differs (`src/step/step.py:345-352`). Authoring is low-code — `examples/workflows/db_disk_space_monitor.yml` is ~30 lines and writes a rich Slack message with interactive blocks.

### Trigger matching

For every incoming alert, `WorkflowManager.insert_events()` (`src/workflowmanager/workflowmanager.py:286`) iterates the tenant's workflows and evaluates a CEL expression against the alert payload. Legacy key/value `filters:` auto-convert to CEL. Built-in `severity_changed` and `only_on_change` semantics avoid noisy re-triggers.

### Runtime

Single-node `WorkflowScheduler` polling loop (1 s tick) plus a fixed `ThreadPoolExecutor` (20 workers, `src/workflowmanager/workflowscheduler.py:649`). In-memory queue (`workflows_to_run`, lock-protected). ARQ (Redis-backed) plumbing already exists in `src/common/arq_pool.py` and the alert-ingestion path (`src/common/event_management/process_event_task.py`) — currently disabled in the workflow scheduler but switchable when load requires distribution.

### Concurrency and safety

Per-workflow `strategy` enum: `parallel | nonparallel | nonparallel_with_retry`, fingerprint-based dedup via `WorkflowToAlertExecution`, per-step retries with backoff, per-action throttles (e.g. `one-until-resolved`).

### Multi-tenancy

`tenant_id` enforced top-to-bottom (`src/identitymanager/authenticatedentity.py`); workflows, alerts, executions and rules are all tenant-scoped. Identity backends: Okta, OneLogin, OAuth2Proxy, DB, no-auth.

### Providers

Catalog under `src/providers/`: Slack, ServiceNow, PagerDuty, Datadog, Prometheus, Grafana, Elastic, Jira, YouTrack, Kubernetes, HTTP, bash, python, postgres, etc. Each is a `BaseProvider` subclass exposing `query()` and/or `notify()`.

### Persistence and observability

`WorkflowExecution` table stores status, trigger source (`alert:<fingerprint>`, `interval`, `manually by ...`), duration, results JSON, error message and revision. `WorkflowExecutionLog` streams per-execution log lines. Prometheus metrics for execution duration, errors, queue size and running workers.

### Rules engine

`src/rulesengine/rulesengine.py` is a separate concern: CEL-based alert correlation that groups alerts into incidents. Workflows can trigger on `incident` as well as `alert`, so the platform supports both per-alert reaction and incident-level orchestration.

### Comparison summary

| Option | Fit |
|---|---|
| **Keep (current)** | Purpose-built for alert-driven automation. YAML + provider catalog is the right authoring surface for L2/L3. Multi-tenant, dedup, enrichment and CEL matching are first-class. Scale path = enable existing ARQ distribution, not re-platform. |
| **Argo Workflows** | K8s-native, container-per-step, designed for ML/batch DAGs. Heavy for "post a Slack message", no alert ingestion or provider catalog, authoring requires manifests. Would force us to rebuild Keep's value prop on top. |
| **Airflow** | Python DAGs (not low-code), scheduled batch orchestration model, not reactive sub-second. Wrong audience and wrong workload shape. |
