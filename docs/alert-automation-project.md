# Alert-Driven Automation Platform for Infrastructure & Application Teams

> Half-page project description prepared for the managers' workshop (session 2 with Tomi).

## The pain

Every time an alert fires on one of our shared platforms — a database, a Kafka cluster, an Airflow scheduler, anything in the cloud infra stack — an L2/L3 engineer has to manually triage it: pull context from three dashboards, cross-reference a wiki runbook, open a ticket, ping the right channel, maybe restart a pod or scale a consumer group. The runbooks live as prose in wikis, not as code. Engineers paged at 03:00 rediscover the same playbook for the seventh time. Mean-time-to-remediate is dominated by glue work, not by diagnosis. Alert fatigue grows, the on-call rotation burns out, and avoidable downtime reaches the business. The application teams that depend on these platforms have no self-service way to react to alerts that affect them — every change is gated on the platform team's calendar.

Crucially, our runbooks are **not** "send a notification and wait for a human". They almost always end with a real remediation action: scale a Deployment up or down, restart a Pod, delete or clean a PVC that has filled up, drain or rebalance a Kafka consumer group, run a maintenance script. Today that final step happens by hand — an engineer with a `kubectl` shell open at 03:00 — which is precisely the work we want the platform to take over.

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

**Closed-loop remediation is a hard requirement, not a future aspiration.** Workflows can run arbitrary `bash` and `python` for scripted recovery, and use the Kubernetes provider to scale Deployments, restart Pods, delete or clean PVCs, evict workloads, etc. — the same actions an L2/L3 engineer types into a terminal at 03:00, encoded once per alert and run automatically thereafter. The same workflow can chain "fetch context → make a decision → take the action → notify the channel → open a ticket" in a single declarative definition.

## Architecture note: should Keep transpile workflows to Argo / Airflow DAGs?

A reasonable proposal is to keep Keep as the authoring surface — YAML, CEL triggers, provider catalog, alert ingestion — but at runtime translate each workflow into an Argo `Workflow` (one container per step) or an Airflow DAG (one operator per step) and let those engines execute. The pull is real: Keep's runtime today is a single-node thread-pool with an in-memory queue, and Argo/Airflow are distributed by design with mature isolation, retry and UI.

We do not recommend it as the v1 default, for three reasons. **(1) Latency on the dominant case.** ~90% of alert workflows are "post a Slack message, maybe call one HTTP endpoint, optionally open a ticket" — sub-second end-to-end. An Argo container-per-step adds a 5–15 s pod-startup tax we would inherit on every alert; an Airflow operator pays a scheduler tick. We would degrade the headline alert-to-action latency by an order of magnitude to buy scale we do not yet need. **(2) Keep stays on the front anyway.** Argo is submit-and-wait batch; Airflow is interval-scheduled. Neither has a native "match this CEL against every incoming alert and fan out workflows" model — so Keep would still own ingestion, CEL matching, dedup, severity-changed/only-on-change gating and fingerprint dedup, and would RPC into Argo/Airflow per match. We add a network hop without removing complexity. **(3) The scale path is already here.** Keep ships ARQ (Redis-backed distributed queue) plumbing in `src/common/arq_pool.py` and the ingestion path; horizontal scale is a configuration flip, not a re-platform.

Where the idea does pay off is the **hybrid escape hatch**: some workflows have legitimately heavy steps (multi-minute backfills, ETL kick-offs, multi-stage remediation needing container isolation). It also pays off for **destructive remediation** — a step that runs `kubectl delete pvc` or arbitrary `bash`/`python` is, today, executed inside the Keep process, which means Keep needs the blast-radius credentials of every team it serves. Routing those specific steps through a sandboxed per-step Argo container gives us proper isolation, per-team RBAC and a stronger audit boundary. For both cases, an individual step declares `executor: argo` and Keep dispatches just that step to Argo and awaits completion. Small, well-scoped extension — one new provider/executor adapter — versus a re-platform.

Recommendation: ship on Keep's engine for v1; enable Keep's built-in ARQ distribution if/when load demands it; add an Argo step-executor as a hybrid escape hatch once at least one team has a concrete heavy-step need. Defer any full transpilation work until we have data on workflow shape, QPS and step durations across teams.

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

The remediation surface for our use case is the combination of **`bash`**, **`python`** and **`kubernetes`** providers. The Python provider (`src/providers/python_provider/python_provider.py`) `eval`s a single expression with selectable imports and returns its value as the step result; multi-statement scripts go through the bash provider; and the Kubernetes provider exposes the `kubectl`-equivalent verbs (scale, restart, delete, evict) that the on-call engineer would otherwise type by hand. Per-tenant credentials are wired through the standard provider config, so a workflow's blast radius is the credentials its team's provider was given.

### Persistence and observability

`WorkflowExecution` table stores status, trigger source (`alert:<fingerprint>`, `interval`, `manually by ...`), duration, results JSON, error message and revision. `WorkflowExecutionLog` streams per-execution log lines. Prometheus metrics for execution duration, errors, queue size and running workers.

### Rules engine

`src/rulesengine/rulesengine.py` is a separate concern: CEL-based alert correlation that groups alerts into incidents. Workflows can trigger on `incident` as well as `alert`, so the platform supports both per-alert reaction and incident-level orchestration.

### Deeper analysis: Keep-as-authoring-surface, Argo/Airflow-as-executor

The clarified architectural question is whether Keep should transpile each workflow at runtime into an Argo `Workflow` or an Airflow DAG. Beyond the three top-level reasons in the half-page note, four further frictions matter:

- **Provider model mismatch.** Keep providers are Python classes with `query()`/`notify()` methods sharing an in-process `ContextManager` that carries alert payload, step results, enrichments, aliases, consts and `foreach` iterators across steps. Argo passes step outputs as parameters/artifacts (size-bounded); Airflow passes via XCom (Postgres-backed, also size-bounded). A faithful transpile means either packaging every provider as a container image (per-pod cold start) or building an Operator-per-provider adapter layer.
- **Throttles, dedup and enrichment are Keep-native.** `WorkflowToAlertExecution` fingerprint dedup, `one-until-resolved` throttles and `enrich_alert` side-effects are tightly coupled to Keep's persistence. Either Keep retains them on the front and the executor only runs the step body, or we duplicate them outside.
- **Authoring/debug UX regression.** Today, debugging a failed automation means looking at the Keep execution log with the alert payload and step results inline. After transpiling, users land in the Argo or Airflow UI with container logs but no alert/enrichment context — unless we build a join-back layer.
- **Maintenance tracks upstream.** Every Keep feature (new condition type, new throttle, new trigger semantic) needs a corresponding translation rule. Permanent chase of upstream surface area.

### Option comparison

| Option | Trade-off |
|---|---|
| **Keep alone (current)** | Sub-second alert-to-action latency. Full provider catalog, multi-tenant, dedup/enrichment first-class. Single-node executor today; scale path via Keep's built-in ARQ distribution (not yet enabled). |
| **Keep → Argo transpile** | Container-per-step adds 5–15 s pod-startup latency on every alert. Output passing via parameters/artifacts is size-bounded. Provider catalog must be repackaged as container images; transpiler maintenance tracks upstream Keep. |
| **Keep → Airflow transpile** | Operator-per-step requires an adapter layer for the provider catalog. XCom for state passing is size-bounded. Airflow's interval-scheduled model fits alert-reactive workloads poorly. |
| **Hybrid: Keep + per-step Argo executor** *(recommended escape hatch)* | Keep owns the fast path; individual heavy steps opt in via `executor: argo` and dispatch to Argo. Small, well-scoped extension; pay container cost only where it earns it. |

### Open questions worth validating before any executor decision

- What is our current Argo/Airflow operational footprint, and is one of them a sunk cost we should leverage?
- What mix of fast (<1 s) vs heavy (>30 s) workflows do we expect once L2/L3 teams are onboarded?
- Are there compliance constraints requiring all automated actions to flow through a particular orchestration plane?
- What blast-radius and approval controls do we need around destructive remediation (e.g. `kubectl delete pvc`) — manual approval gate, dry-run mode, audit-only first phase, per-team scoped credentials, sandboxed-per-step execution?
