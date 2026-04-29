# Alert-Driven Automation Platform — Pre-Discovery Checklist

> Pre-discovery write-up for the alert-automation platform project (KeepHQ-based). All eleven checklist items addressed; items that genuinely require empirical measurement are scoped to **discovery week 1** so the pre-discovery gate is not blocked on data that is, by definition, the work of discovery itself.

## 1. Background & Context — *what's the motivation behind this idea?*

Our L2/L3 platform teams (DBs, Kafka, Airflow, cloud infrastructure) and the application teams that depend on them spend a growing share of on-call time on glue work — manually triaging alerts, re-running the same wiki-runbook steps, and executing remediation actions (`kubectl`, shell scripts, DB commands) by hand at 03:00. **KeepHQ**, an OSS alert-automation engine, has matured to a point where we can stand it up as a shared platform and let each team encode its runbooks as version-controlled YAML workflows that execute automatically on the matching alert.

*Why now:*

- KeepHQ deployment and integration into our infra is already underway (current branch ships v0.50.0).
- **Alert volume.** Our alerting pipeline ingests roughly **4,000 alerts/minute** (~240K/hour, ~5.7M/day). At this volume, manual triage is structurally infeasible — there is no headcount answer. Encoding the existing manual runbooks as automated workflows is the only realistic path to keep up; the human-paging slice and the top-N contributing alert classes are quantified during discovery (see §4).
- **Strategic alignment.** Bottom-up initiative raised by the platform group, with manager-level sponsorship in place. Not currently mapped to a top-level SRE/Reliability OKR; ladders into the org's operational-excellence themes — reducing on-call toil, codifying tribal knowledge, and unblocking application teams from platform-team capacity. Formal OKR placement is itself a discovery deliverable: either propose this as a contributor to an existing reliability OKR or stand up a dedicated key result.

## 2. Problem & Pain Points — *what specific problems are we solving?*

- Wiki runbooks are prose, not code; engineers re-discover the same steps repeatedly.
- Runbooks end with **real remediation** — scale a Deployment, restart a Pod, delete or clean a PVC, drain a Kafka consumer group, run a maintenance script — currently done by hand at 03:00 with `kubectl`.
- MTTR is dominated by glue work, not diagnosis.
- Alert fatigue → on-call burnout → avoidable downtime.
- Application teams have no self-service way to react to alerts on the platforms they depend on; every new automation is gated on the platform team's calendar.

## 3. High-Level Requirements — *what's in scope, what's out?*

**In scope (v1):**

- Alert-driven workflow execution: workflows trigger on a matching incoming alert (CEL filter), an interval, or a manual invocation.
- Low-code YAML authoring on top of a 60+ provider catalog — including `bash`, `python` and `kubernetes` for closed-loop remediation.
- Multi-tenancy: per-team workflow scoping, identities, secrets and audit.
- Per-execution audit trail and Prometheus observability.
- Throttling, dedup, retries, severity-change and on-change gates.

**Out of scope (v1):**

- Building our own workflow engine — we adopt KeepHQ.
- Authoring alert rules *inside* Keep — alert source-of-truth stays in Prometheus / Datadog / Grafana / Elastic / vendor systems.
- Replacing PagerDuty / Opsgenie for human paging.
- Transpiling Keep workflows to Argo / Airflow DAGs (deferred — see the project doc).
- Org-wide rollout in v1; we ship to ≤ 3 pilot teams first.
- Non-alert workloads (data pipelines, scheduled batch ETL): those remain on Airflow.

## 4. Supporting Data — *what evidence suggests this is valuable?*

**Known today:**

- **Alerting-pipeline ingest rate: ~4,000 alerts/minute** (~240K/hour, ~5.7M/day) across the platforms in scope. This volume alone defeats any human-triage strategy and is the load-bearing fact for the project.

**To be measured during discovery:**

- The human-paging slice of the 4K/min figure (post-dedup, post-correlation, post-routing) — this is the slice workflows actually act on.
- MTTR distribution for the top-10 most-paged alert classes.
- Count and inventory of existing wiki runbooks per L2/L3 team.
- % of alerts that page a human vs. resolve automatically today.
- On-call satisfaction score / number of weekly after-hours pages.
- Anecdotal: 3–5 recent incidents whose remediation was a documented manual runbook step.

## 5. Main Use Cases — *what scenarios will this feature support?*

Proposed POC scope (to be confirmed with team leads). Each is *trigger → context → action*:

1. **PVC near-full** — Prometheus `kubelet_volume_stats_used_bytes / capacity > 0.9` → fetch top-N largest paths via `kubectl exec` → run team-owned cleanup script → if still > 0.85, scale the Deployment; in all cases post a structured Slack message to the owner channel.
2. **Kafka consumer-group lag** — alert `consumer_group_lag > X for Y min` → fetch lag distribution per partition → trigger rebalance / restart consumer pods → open a ticket if lag persists after action.
3. **Pod CrashLoopBackOff** — alert `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}` for > N min → collect `kubectl describe` + last 200 log lines → post to Slack with the context → if owner has pre-acknowledged auto-remediation, restart the Pod.
4. **Airflow DAG stuck** — alert on a DAG run exceeding its SLA → query Airflow API for the stuck task → kill and retry once → escalate to owner if it fails again.
5. **DB connection-pool saturation** — Prometheus `pgbouncer_pools_client_active / size > 0.9` → query `pg_stat_activity` for top consumers → notify the owning service team in Slack with the offenders → optionally bump the pool size via the team's IaC pipeline.

## 6. Personas — *who are the main users or beneficiaries?*

- **Kafka platform L3** *(e.g. Yossi)*. Today: PagerDuty wakes him at 03:00 for consumer-group lag; he SSHes in, runs the rebalance, posts in `#kafka-ops`. With Keep: writes that as a workflow once; future pages auto-resolve or arrive already-mitigated with the action recorded.
- **Airflow platform L2** *(e.g. Maya)*. Today: stuck DAGs trigger tickets; she clears them by hand. With Keep: a workflow handles the known stuck-task patterns and only escalates the unfamiliar ones.
- **Application-team SRE** *(e.g. Avi, owning a service whose DB is shared)*. Today: cannot self-serve any automation against the platform alerts she's paged on. With Keep: writes a workflow scoped to her team's tenant that reacts to alerts on *her* service without queuing on the DB platform team's calendar.

*Names are illustrative archetypes representing the three persona shapes (platform L3, platform L2, application-team SRE). Real pilot-team representatives are assigned during discovery interviews — see §11.*

## 7. Customer Impact — *how will this affect users or customers?*

**Internal customers:**

- On-call engineers — fewer 03:00 pages, less manual `kubectl` toil, runbooks that are version-controlled rather than wiki-rotting.
- Platform-team leads — visibility into which automations exist, who owns them, success/failure rates, and audit trail of every action.
- Application teams — self-service ability to add automations against the platforms they depend on, without queuing on the platform team.

**External customers** *(end-users of services running on our platforms):*

- Faster recovery from incidents → fewer / shorter outages → directly tied to product-level availability SLOs.
- Scope: every customer-facing service whose availability is gated on the in-scope platforms (DBs, Kafka, Airflow, cloud infra). The exact service list is enumerated during the stakeholder-mapping pass in discovery week 1 — for the pre-discovery gate, treat the impact as "any product-level SLO that depends on these platforms continuing to function".

## 8. Market & Competition — *are there market trends or competitors we should consider?*

Considered alternatives in the alert / runbook automation category:

| Tool | Pros for our use case | Cons |
|---|---|---|
| **KeepHQ** *(chosen)* | Open source, self-hostable, alert-trigger semantics first-class, 60+ providers including `bash`/`python`/`kubernetes`, multi-tenant, low-code YAML, no per-action vendor cost. | Single-node executor today; smaller community than commercial tools; we own the operational burden. |
| PagerDuty Runbook Automation (Rundeck) | Mature, strong RBAC, enterprise support. | Per-execution licensing cost, sits behind PagerDuty, weaker alert-trigger semantics, less open. |
| Shoreline.io | Strong K8s remediation focus, good UX. | Closed source, vendor lock-in, pricing. |
| ServiceNow Now Assist / Workflow | Already deployed in some orgs as ITSM. | Heavy, slow alert-to-action loop, not engineer-friendly authoring. |
| FireHydrant Runbooks | Incident-response orchestration. | Incident-centric, not alert-centric; closed source. |
| Build in-house | Maximum control. | Months of work to recreate Keep's provider catalog and trigger semantics; not a differentiator. |

*Procurement review is not required:* KeepHQ is OSS (MIT-style licensed), self-hosted, with no per-action vendor cost. *Security review:* none on file at pre-discovery time — initiated as the first item in discovery week 1 (§11), focused on the `bash` / `python` / `kubernetes` remediation surface and the per-tenant credentials model.

**Trend tailwind:** industry shift toward "code-defined runbooks" and "everything-as-code" (Backstage, GitOps, OpenTelemetry). This project is aligned with that direction.

## 9. Key Metrics / OKRs — *what measurable outcomes might improve?*

**Top-line funnel** *(the project's headline metric):*

> **4,000 alerts/min ingested** → `X%` route to a human after dedup/correlation → `Y%` of those have a documented manual runbook → today, **0%** are auto-resolved by Keep.
>
> *Target:* by `[horizon]`, `Z%` of the `Y`-slice end-to-end auto-resolved without paging a human. `X` and `Y` are discovery deliverables; the headline OKR is the `Z`.

Supporting metrics (each needs **baseline + target + horizon + owner** before the pre-discovery gate):

| Metric | Baseline | Target | Horizon | Owner |
|---|---|---|---|---|
| MTTR for top-10 paged alerts | Measured discovery W1 (PagerDuty/Opsgenie analytics) | −50% on POC-covered alerts | end of POC (~10 wks from gate) | SRE/Infra sponsor |
| % of alerts that page a human (= `X` in funnel) | Measured discovery W1 | −25% absolute within pilot scope | end of POC | SRE/Infra sponsor |
| Duplicate manual runbook executions / week | Surveyed discovery W1 (pilot-team interviews) | −75% on POC-covered alerts | end of POC | each pilot team lead |
| Automated alert→action paths in production per team | 0 | ≥ 5 per pilot team | end of POC | each L2/L3 team lead |
| After-hours pages per on-call rotation | Measured discovery W1 | −25% within pilot scope | end of POC | SRE/Infra sponsor |

*Targets are directional engineering judgement at pre-discovery time and are tightened once discovery W1 baselines land.*

## 10. Stakeholders — *who are the key people inside and outside the company?*

**Inside:**

- *Sponsor* — engineering manager of the platform group raising the initiative (manager-level sponsorship per §1); specific name confirmed at the pre-discovery gate
- *L2/L3 team leads* — DB, Kafka, Airflow, Cloud Infra (each pilot-team representative)
- *Security / AppSec* — non-negotiable sign-off for steps that mutate state (`kubectl delete pvc`, scaling, restarts) — blast-radius review, audit, secrets handling
- *IAM* — per-tenant credentials and provider-config secrets
- *Platform / DevEx* — ownership of the Keep deployment itself: SLOs, on-call for Keep, upgrades
- *Application-team reps* — early adopters who consume platform alerts
- *Observability team* — owners of the alerting sources (Prometheus / Datadog / Grafana) Keep ingests

**Outside:**

- KeepHQ upstream (OSS dependency) — relationship and contribution strategy; do we upstream fixes or maintain a fork?
- Vendors whose alert sources we ingest, where SLA-relevant integrations apply.

*Specific names per role assigned by the sponsor in discovery week 1 alongside discovery-group composition (see §11).*

## 11. Discovery Plan — *who needs to be involved and what steps will follow next?*

**Discovery group (proposed):**

- Project lead — engineer leading the alert-automation rollout (the initiative raiser / document author)
- 1 representative per pilot L2/L3 team (3 teams)
- Security rep
- Platform / DevEx rep (Keep deployment owner)
- 1 application-team early adopter

**Steps after the pre-discovery gate:**

1. **Discovery (~4 weeks).** Interviews with each pilot team to nail down their top runbooks; security review of the `bash` / `python` / `kubernetes` remediation surface; choose POC workflows from §5; finalise the multi-tenant onboarding playbook.
2. **POC build (~6 weeks).** Implement 3–5 production workflows for one pilot team end-to-end. Acceptance criteria: workflows pass dry-run review, run successfully against staging alerts, and reduce manual runbook executions by ≥ X% on the targeted alerts.
3. **POC review gate.** Go / no-go on broader rollout; decide whether to build the hybrid `executor: argo` escape hatch (per the project doc) — only if a concrete heavy-step or destructive-remediation use case has emerged.
4. **Phased rollout.** Onboard the next two pilot teams; publish authoring docs; set Keep's own service SLOs.

**Open architectural decisions to revisit during discovery:**

- Whether to enable Keep's distributed mode. *Note:* enabling it is **not** a config flag flip — the in-source ARQ handler for workflow execution is currently a `TODO` (commented-out block in `src/workflowmanager/workflowmanager.py:522-568`). The connection plumbing exists; the worker entrypoint has to be finished.
- Whether to build the per-step `executor: argo` adapter pre-emptively or only on first heavy-step / destructive-remediation demand.
- Approval, dry-run and audit controls for destructive remediation steps.
