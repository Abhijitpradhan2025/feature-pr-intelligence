# PR Intelligence Dashboard: Phase 1 Contracts

## Status and scope

This is the Phase 1 shared contract (v0.1). The local Streamlit app at `dashboard/app.py` supports session-only repository discovery, multi-select collection, one shared creation-date window and author filter, per-repository status, and partial portfolio reporting. Portfolio exact counts use each metric's own validated evidence and coverage; unrelated partial resources do not suppress eligible counts. Counts from incomplete metric coverage are marked partial, and duration/PR-size medians remain per repository. The collector implements read-only REST collection, pagination, stable activity linkage, per-PR completeness, and an explicit unavailable review-thread resource. API requests use the standard GitHub Bearer authorization scheme; the token remains in process memory. The fixed repository list is a convenience, not a complete inventory of accessible repositories. Deterministic metrics are implemented in `python_script/metrics.py`.

The analytics definitions below are finalized contract rules and six lifecycle metrics have deterministic implementations; they are constrained to evidence the collector returns. The deterministic keyword-rule analyzer covers feedback categories and the reviewer-focused Action Plan. The local model summarizes all text-bearing PR comments; semantic feedback classification, addressed/resolved decisions, quality scores, and AI-generated recommendations remain unavailable. A read-only live collection was completed against the configured default repository and author for the last 30 days; it returned zero PRs, so it did not verify access to live comments.

**Orchestrator acceptance record:** The runtime Streamlit submit path invokes explicit specialist components in `python_script/orchestrator.py`. The project-venv test suites passed (109 backend tests, 39 dashboard tests); the Streamlit submit invocation is tested with a mocked collector/orchestrator. A synthetic end-to-end Orchestrator review with one reviewer conversation comment completed local Ollama synthesis and deterministic validation. Authenticated read-only GitHub collection completed against the configured repository/author but returned zero PRs for the selected 30-day window; live comment evidence was therefore unavailable. No persistence or GitHub write operation was added.

**Local comment-summary v2 acceptance record:** The request preserves host-scoped repository identity, per-repository PR-population completeness, source-level comment coverage, commenter role, and linked evidence. A local adapter calls Ollama at `127.0.0.1:11434`, with no remote-provider fallback in application code; this is not OS-level network-egress enforcement. Actual Qwen2.5 3B inference was verified with synthetic reviewer-comment evidence and returned a citation-validated theme. The live GitHub query found no PRs in its date window, so no live comment inference was possible.

**Dashboard presentation status:** The Streamlit app retains its Portfolio Overview, Repository Analytics, People Analytics, PR Explorer, Quality & Reliability, and Insights & Recommendations pages. Portfolio PR-state KPIs and activity/outcome charts use complete PR populations; the activity chart describes the selected PR cohort, not portfolio-wide merge throughput. The runtime quality stage reports collected current-head check outcomes and observed PR hygiene; it does not score quality or diagnose flaky tests without repeated-run evidence. Unsupported response-time attribution and semantic defect classification remain unavailable.

This workspace contains the agent configuration, this contract, a standalone collector at `python_script/fetch_github_pr_data.py`, a pure analytics module, and a local Streamlit UI foundation. The application and collector use the fixed GitHub Enterprise API root `https://github.azc.ext.hp.com/api/v3`; `GITHUB_API_URL` and `--api-url` overrides are not supported. The default repository is `abhijit-pradhan1/abhi_sandbox` and default author is `abhijit-pradhan1`. Other configured repositories are `Ascendion-AAVA-Sandbox/workpath-services-dune`, `karthikeyan-subhash/hp-expensemanager`, and `Ascendion-AAVA-Sandbox/webjetadmin-development`. Provide another repository positionally, and use `--all-authors` to disable the author filter. Do not infer credentials.

Example (PowerShell; use a newly rotated, read-only token only in the current process):

```powershell
$env:GITHUB_TOKEN = "<fresh read-only token>"
python .\python_script\fetch_github_pr_data.py --from 2026-01-01 --to 2026-01-31 --output .\pr-data.json
```

For the local dashboard, install the declared dependency in the project virtual environment and start it from the workspace root with `.\venv\Scripts\python.exe -m streamlit run dashboard/app.py`. The checked-in Streamlit configuration binds to `127.0.0.1` by default. The dashboard prompts each user for a password-masked, read-only token and keeps it only in the active Streamlit session; no process environment variable is required. The standalone collector example above still uses `GITHUB_TOKEN` in its process environment.

The collector paginates repository pull requests and associated activity, retries GitHub rate limits, filters by the configured author by default, and emits one result object for one repository or an array of result objects for multiple repositories. Use `--list-repositories` for the offline fixed list, or `--discover-repositories` for authenticated discovery (requires `GITHUB_TOKEN`, sends read-only requests to `https://github.azc.ext.hp.com/api/v3`, and returns repository ID, owner/name, canonical URL, default branch, language, visibility, and API-provided permissions). Pagination links are accepted only on the configured Enterprise API host and under `/api/v3`. Discovery is read-only and paginated. Review-thread resolution is explicitly unavailable in this REST-only implementation; such runs are marked partial rather than treating missing thread state as an empty result. A historical `--as-of` cutoff filters some activity records but cannot reconstruct mutable PR metadata as it was at that time; the result includes a warning. The script does not call GitHub until invoked with a valid `GITHUB_TOKEN`.

## Shared data contract

### Collection request

The collection boundary accepts:

| Field | Meaning |
|---|---|
| `host_url` | GitHub.com or GitHub Enterprise API/web origin; never infer it from a repository name |
| `owner`, `repository` | Explicit repository identity |
| `from`, `to` | Inclusive UTC PR-creation window selecting the metric population, with `from <= to`; these bounds do not truncate the activity history needed to measure selected PRs |
| `as_of` | Optional UTC observation cutoff for activity metrics; if omitted, the collector fixes it at collection start and returns that value as `observation_cutoff` |
| `include` | Optional explicit selection from `pull_requests`, `pull_request_details`, `lifecycle_events`, `commits`, `reviews`, `review_comments`, `issue_comments`, `review_threads`, and `checks`. `pull_requests` is required. Omission selects all supported resources. Unknown or duplicate names, or a selection without `pull_requests`, are rejected. |

The collector implements `include` in both its CLI (`--include`) and `fetch_repository` call. Selection is resource-level; requesting pull requests alone avoids detail and activity endpoint calls. `pull_request_details` fetches the individual PR endpoint for GitHub-reported `additions`, `deletions`, and `changed_files`; it is tracked separately from the pull-request population so a detail failure does not erase an otherwise complete PR list. Collection is read-only and paginated/rate-limit-aware. No persistence, cache, or retention policy is implied.

### Collection result

Each repository result contains:

| Field | Meaning |
|---|---|
| `schema_version` | Versioned contract identifier |
| `repository` | Stable repository ID when supplied, owner, repository name, host origin, and canonical URL |
| `requested_window` | Original UTC bounds |
| `fetched_at` | UTC fetch timestamp |
| `observation_cutoff` | Fixed UTC cutoff through which metric activity was observed |
| `status` | `complete`, `partial`, `unavailable`, or `error` |
| `resources` | Status and observed count per supported resource, with `pagination_complete`, `history_complete`, and coverage bounds where applicable. A status may be `complete`, `partial`, `unavailable`, or `not_requested`. Not-requested resources have `count: null`, false completeness flags, and null coverage bounds. |
| `warnings` | Explicit permission, truncation, or historical-coverage limitations |
| `pull_request_resources` | Map keyed by stringified stable PR ID. For every selected PR and selected resource, gives that PR/resource's status, count, pagination/history flags, and coverage bounds. Not-requested resource entries are omitted. |
| Activity arrays | Arrays such as `pull_requests`, `events`, `commits`, `reviews`, `review_comments`, `issue_comments`, and `checks` are included only when their corresponding resource is requested. A requested array may be empty when retrieval succeeds with no records; unavailable data is represented by resource status/count, not by an empty array alone. `pull_request_details` enriches the existing PR records and has no separate array. |

Errors must remain explicit, actionable, and distinguishable from empty data. Never log credentials or silently replace inaccessible data with empty collections.

#### Current per-PR completeness and aggregate semantics (verified in collector)

Per-PR `coverage_start`/`coverage_end` are set to the intended PR-created-through-cutoff bounds even for unavailable resources; they describe the interval requested, not proof that it was successfully observed. Status and completeness flags determine whether that interval is actually covered. `pull_request_details` is a current metadata snapshot, not historical activity; it is complete only when the detail endpoint succeeds and all three non-negative size counts are present. A failed or incomplete detail request leaves the counts unavailable and is shown separately from PR-list coverage. For checks, these bounds do not remove the current-head-only limitation; for unavailable review threads, they do not imply any thread coverage.

The PR selection population is the filtered set found in the requested inclusive creation window. For each selected PR, the `pull_request_resources` key is `str(PR.id)`. Every selected resource gets a per-PR record. A successfully paginated REST request for lifecycle events, commits, reviews, review comments, or issue comments is `complete`, with the count of records retained after the collector's observation-cutoff filter and coverage from that PR's `created_at` through `observation_cutoff`. A successful empty response is an evidenced zero only for that requested PR/resource and stated coverage. On an API error, that PR/resource is `unavailable`, has `count: null`, and both completeness flags false; its failure is not silently converted into zero. Other PRs may succeed independently.

`pull_requests` is marked complete for each selected PR. Its aggregate coverage describes the requested PR-creation window, not per-PR activity. For selected resources, aggregate count sums known per-PR counts (while a corresponding returned array count is its actual length); aggregate pagination/history flags are true only if all per-PR values are true. Aggregate status is complete when every per-PR status is complete, partial when at least one PR is complete or partial and not all are complete, and unavailable when all per-PR statuses are unavailable. A partial aggregate count can include successful PR records and is not a population-complete total. With no selected PRs, a supported selected resource aggregates to complete/count zero (review threads remain unavailable); requested checks still carry a warning and the envelope can be partial. The envelope is partial if any selected resource is partial/unavailable or any warning is present; repository-level failures use an `error` envelope.

Resources omitted from `include` have `not_requested` aggregate status and are absent from their activity array and per-PR resource maps. They are unknown/not collected, not empty. `review_threads`, if requested, is unavailable for the aggregate and every selected PR (`count: null`, pagination/history false): this collector does not call GraphQL `reviewThreads`. Requested checks query only each PR's current head commit. When that endpoint succeeds, the per-PR result is partial, with the number of current-head check runs, pagination complete, and history incomplete; request failure or missing head SHA is unavailable. Current-head checks are partial even with zero returned runs and cannot establish historical check absence or CI wait. `history_complete: true` declares successful endpoint coverage for that resource; it does not prove each record has a usable timestamp or any cross-resource causal relationship.

`history_complete: true` is operational: it means the collector successfully paginated the relevant endpoint(s) for that PR without a reported request error. It is not an external guarantee that GitHub retains every deleted or otherwise unavailable historical record.

**Timestamp collection limitation:** source timestamps are retained as received. Null event timestamps are retained, but a malformed non-null activity timestamp encountered in the collector's cutoff comparison raises `ValueError` and can turn the repository result into an `error` envelope rather than a resource-level invalid-record count. Analytics must still validate timestamps and report invalid/missing observations; handling malformed timestamps gracefully in collection is an unresolved backend hardening item, not a claimed collector capability.

### Normalized records

All records preserve stable GitHub identifiers, repository identity, canonical source URL, and source provenance. Timestamps use RFC 3339 UTC; nullable/unavailable values stay null and are accompanied by completeness information.

`PullRequest` minimum fields: `id`, `number`, `repository_id`, `repository` (`owner`, `name`), `title`, `body`, `state`, `author` (stable GitHub ID when available), `created_at`, `updated_at`, `closed_at`, `merged_at`, `base_ref`, `head_ref`, `draft`, `additions`, `deletions`, `changed_files`, `labels`, `url`.

`Event` minimum fields: `id`, `repository_id`, `repository` (`owner`, `name`), `pull_request_id`, `pull_request_number`, `kind`, `actor` (stable GitHub ID when available), `occurred_at`, `url`, `source_url`, and `payload/provenance`. Reviews, comments, commits, and checks likewise carry repository identity plus `pull_request_id` and `pull_request_number`, stable source identity (`id` or commit SHA), and source URLs. The lifecycle-event set must at least distinguish `reopened`, `closed`, and `merged` when the host exposes them. Preserve source events without implying unsupported event types or causal attribution.

Reviews, comments, commits, and checks remain typed records linked to their PR and source URLs. Preserve review state (including pending/dismissed states when supplied), author identity, and nullable submitted time; comment author identity, created/updated time and body; commit identity, GitHub-associated actor when supplied, and time; and check name, status/conclusion, start/completion time and URL when supplied by GitHub. Avoid collecting or persisting more private content than the requested analysis requires.

### Metric data availability requirements

For each PR in the creation-date population, collect the requested PR activity from that PR's `created_at` through `observation_cutoff`, not merely between `from` and `to`. Report per-resource coverage bounds and whether the relevant history is complete. A successful empty result is meaningful only when that history is complete; missing permissions, unsupported lifecycle endpoints, truncation, or inaccessible historical records must make the affected resource/metric unavailable or partial, never a zero observation.

The collector returns review state, author identity and submitted time; issue and review comments with author identity and created/updated time; commits with a GitHub-associated actor identity when supplied; and lifecycle events. Missing actor linkage must not be guessed from commit author text or email. PR lifecycle history is needed to substantiate reopened-PR limitations. Checks are collectable context only: no CI/check metric is defined here, and check data must not be used to attribute delay.

**Observed identity contract:** each normalized activity record carries `repository_id`, `repository: {owner, name}`, `pull_request_id`, and `pull_request_number`; PR records carry stable `id`/`number` and repository identity. The envelope remains necessary for host identity, observation window/cutoff, resource statuses, and source context. Do not detach or combine records without retaining that envelope and host/repository scope. Record source URLs are retained (`url` and, where supplied, `source_url`).

### Security and access

Use least-privilege read-only authorization. Tokens stay server-side/out of logs and source control. Repository-level authorization must be enforced before exposing private data. Missing permission, unsupported endpoint, rate limiting, and incomplete history are explicit resource-level outcomes. Do not add storage or broad caching until justified, authorized, and covered by a retention/access-control decision.

## Finalized Phase 1 analytics contract and implementation

### Common population, evidence, and time rules

The base population is the PRs in one repository result whose `created_at` is within the inclusive UTC `requested_window.from`–`requested_window.to` creation window and which pass the envelope's author filter. The default collector filter is `abhijit-pradhan1`; `--all-authors` removes that filter. The window selects PRs, not activity: activity is intended to cover each selected PR from creation through the envelope's `observation_cutoff`. Repository comparisons are allowed only when repository, creation window, author filter, cutoff, requested resources, and completeness eligibility are the same and reported.

Activity records link directly by stable `pull_request_id` and include `pull_request_number`, `repository_id`, and owner/name; retain the enclosing result for host, window, cutoff, and completeness. Do not combine records across repositories without scope. Eligibility is evaluated per PR against `pull_request_resources[str(PR.id)][resource]`, not inferred from aggregate `resources` status alone. Complete per-PR resource status plus `history_complete: true` and `pagination_complete: true` permits metrics using that resource, subject to the metric's timestamp and identity requirements. Partial or unavailable records are not zeros and are excluded from complete-history denominators, with their coverage reported. A requested resource whose successful response has count zero is zero for that PR/resource; `not_requested` and `count: null` mean unknown. Missing or malformed evidence is unknown, not zero. Never impute timestamps, actor identity, review state, PR size, or absent activity. When a denominator is zero, report the value as unavailable/not applicable with denominator zero, not as a numeric zero.

Parse source timestamp strings as timezone-aware instants and normalize to UTC before subtraction. Naive, malformed, null, or negative-interval timestamps are invalid and excluded with explicit invalid/missing counts. The collector retains activity with absent event timestamps rather than proving whether it falls before the cutoff; cutoff-bounded temporal metrics must therefore exclude such records and count them as timestamp-unknown. Preserve source timestamp and URL. Durations below are elapsed wall-clock time (UTC), including nights, weekends, and holidays; none are business-time measures. An `as_of` cutoff is reliable for filtering timestamped activity only to the extent the collector did so. Current PR metadata (`state`, `closed_at`, `merged_at`, size) is mutable and is not a historical snapshot: metadata-dependent calculations are eligible only as current-at-fetch observations without a historical-cutoff warning; for a historical cutoff they are unavailable unless an independently evidenced as-of value exists.

Metric output contract: include metric name and definition/version; repository identity; creation window, author filter, and observation cutoff; population and resource-completeness status; numerator/denominator or observation count with explicit eligibility rules; missing/invalid counts; unit and elapsed-time semantics; value (or explicit unavailable reason); and source evidence links. For event-derived observations include the PR number, source record kind/ID, timestamp, actor identity as supplied, and canonical/source URL for each endpoint. The analytics output envelope is implemented in `python_script/metrics.py`; the collector itself remains a collection-only component.

### Metrics supported in principle by the returned evidence

The rules below define supported calculations and their evidence eligibility. Per-PR completeness and stable activity linkage are present, and the six listed metrics are implemented in `python_script/metrics.py`. A population-wide result still requires the relevant resource to have been requested and complete for each PR included in its coverage denominator. Report covered and excluded/unknown PR counts; never silently label a partial-population result as population-wide.

| Metric | Formula and eligible population | Denominator, missing data, and expected evidence/output |
|---|---|---|
| PR size — **eligible now, current snapshot only** | For each population PR with both GitHub-reported counts, `additions + deletions` at fetch time. Also report `additions`, `deletions`, and `changed_files` separately; do not infer from patches or call this rework. | Size coverage denominator is all selected population PRs; value denominator is PRs with both counts. Report count and coverage fraction (`valid size PRs / population PRs`), plus a declared summary statistic such as median; count unavailable size as missing, not zero. Evidence: PR `id`, `number`, repository identity, counts, `url`, fetch time, and completeness. Snapshot is current-at-fetch only, not historical. |
| First submitted review — **eligible now when reviews are requested and complete per PR** | Per PR, earliest valid `submitted_at` among reviews whose state is `APPROVED`, `CHANGES_REQUESTED`, or `COMMENTED` and whose known GitHub author ID differs from the PR author's known GitHub ID. Latency is that timestamp minus `created_at`. Pending/dismissed/unknown states, same-author reviews, and records with unknown author comparison are ineligible. | Review-complete PRs are the coverage denominator; report first-review coverage (PRs with an eligible review / review-complete PRs). Report median elapsed latency only across those PRs with a valid first review, alongside observation count. A complete review history with no eligible review contributes to coverage denominator but not latency denominator. Missing identity/time makes affected eligibility unknown, not “no review.” Per-PR `reviews` must be complete, history- and pagination-complete. Evidence: PR ID/number, author ID, creation time, each candidate review ID/state/author ID/submitted time/URL, and per-PR completeness. |
| Merge latency (canonical cycle-time measure) — **eligible only as current-at-fetch observation** | For population PRs with a valid `merged_at` no later than the observation cutoff, `merged_at - created_at`. This is creation-to-merge elapsed time. Do not publish a duplicate “cycle time” metric. | Report merged-PR count / population count and median latency among those merged PRs; unmerged PRs are excluded from latency, not assigned zero. Missing or historically unreconstructable `merged_at` is unknown. For reopened PRs this includes the entire creation-to-merge span and does not subtract reopened gaps; a reopened-event count is eligible only where per-PR lifecycle history is complete. Evidence: PR ID/number, creation and merge timestamps, lifecycle events when available, URLs, cutoff, and completeness. Historical-cutoff calculations are unavailable with current mutable PR metadata. |
| Reopened-event count — **eligible now when lifecycle events are requested and complete per PR** | Per PR, count returned lifecycle-event records whose normalized `kind` is `reopened` and whose valid `occurred_at` is no later than the observation cutoff. This is a count of observed reopened events, not a judgment about PR quality or author behavior. | Denominator is PRs with complete, history- and pagination-complete lifecycle events; report covered/unknown PR counts and event-count distribution. Missing or invalid event timestamps are unknown and excluded from the cutoff-bounded count. Evidence: PR ID/number, event ID/kind/time/URL, cutoff, and per-PR completeness. |
| PR commit count — **eligible now when commits are requested and complete per PR** | Number of commit records for the PR whose `committed_at` is valid and no later than the observation cutoff; do not treat commit count as review rework or author effort. Records with missing/invalid timestamps are excluded from the cutoff count and reported as timestamp-unknown, not silently included. | Denominator is PRs with complete commit history and timestamp eligibility determined; report count/coverage and a declared summary (for example median commits per covered PR). Incomplete/missing commit history is unavailable, not zero. Evidence: per-PR commit SHAs, PR ID/number, associated GitHub author identity when supplied, committed time, URL, and per-PR completeness. |
| Observed adjacent-activity gap — **eligible now only for fully requested, complete event histories** | Within one PR, sort records with valid timestamps from lifecycle events, reviews, issue comments, review comments, and commits. For consecutive records with strictly increasing timestamps, gap = later timestamp − earlier timestamp. Treat as an inter-event elapsed gap only, not a response or waiting-time attribution. | Denominator is valid adjacent pairs from PRs with complete histories for all five included resources; each must be requested and complete per PR. Report pair count and median gap, plus excluded/missing/tied timestamp counts. Do not order equal-time records as though a causal sequence were known; omit that pair and report ambiguity. Do not deduplicate across resource types without evidence that records represent the same event. Evidence/output must include both endpoint kinds, IDs (or SHA), timestamps, actors as supplied, URLs, PR/repository identity, and resource completeness. This remains observational, not time attributed to author, reviewer, or CI. |

**Implementation status:** those six metrics are implemented in `python_script/metrics.py`, with synthetic unit tests in `python_script/test_metrics.py`. The implementation returns a versioned metric envelope, definitions, population/window/filter/cutoff, numerator/denominator, coverage and invalid counts, units, and evidence identifiers/URLs without copying PR or comment text. It rejects malformed population envelopes and unlinked activity rather than interpreting them as empty. Incomplete resource history reduces metric eligibility; complete empty activity can count as zero only for a PR/resource whose per-PR history is complete. Historical PR size and merge metadata are unavailable when the collector marks an explicit historical observation cutoff. The local UI displays these metrics. Local comment analysis is implemented in `python_script/comment_analysis.py` and covered by synthetic tests; its limitations are specified below. Semantic/AI feedback analysis, recommendations, quality score, persistence, and production deployment/authentication are not implemented.

### Explicitly unavailable in this evidence contract

| Metric/request | Why unavailable; do not substitute |
|---|---|
| Review rounds / author-reviewer exchange cycles | A count of submitted review records is derivable, but a review “round” requires grouping reviews and author responses into exchanges. Review-thread resolution is explicitly unavailable; review records alone do not establish rounds. Do not relabel review count as rounds. |
| Author response interval and reviewer follow-up interval | Records expose timestamps and actors but no reliable reply-to, thread, or review-round linkage. Overlapping threads and multiple comments/reviews make “next author/reviewer event” an unsupported causal pairing. Do not present adjacent-activity gaps as these metrics. |
| Time to closure | `closed_at` is mutable and can be cleared/changed on reopen. Complete per-PR lifecycle events can expose close/reopen observations when requested, but the analytics contract has not defined whether closure means first close, final close, or closure as-of cutoff, nor how to handle a still-open/reopened PR in the denominator. Do not infer a final-closure interval from current `closed_at` alone. |
| Rework after feedback | Commit records do not establish that a commit is a response to a particular review request or that code was revised; commit actor linkage may be absent. Commit count is not a rework proxy. |
| CI wait / check delay | Checks are collected for the current PR head and do not provide a complete historical check-to-feedback/merge causal chain. Checks must not be used to attribute delay. |
| Semantic/AI review-feedback interpretation and quality score | Semantic/AI classification, addressed/resolved decisions, and quality-score formulas remain unavailable and unagreed. A local deterministic Action Plan is implemented below; it must not be presented as semantic certainty or employee-performance scoring. |

### Local review-comment analysis (rule-based; not semantic)

The v0.1 analyzer covers three collected sources for target-authored PRs in the inclusive creation window: submitted review summaries (`reviews`), inline review comments (`review_comments`), and PR conversation comments (`issue_comments`). It separates comments by known non-PR-authors from the PR author's own responses and unknown commenter identities. The analysis uses stable repository/PR links, comment IDs, source URLs, timestamps, and per-PR resource completeness. Matching duplicate records with stable IDs are omitted; conflicting duplicate IDs remain visible but are excluded from counted feedback.

The explicit multi-label taxonomy provides transparent case-insensitive keyword-rule signals for functional defects, conditional logic, API contracts, edge cases, test coverage, flaky-test mentions, lint/style, duplication, maintainability, exception handling, security, documentation, test evidence, PR description/scope, and `other_unclassified`. One item may match multiple categories, so category percentages can sum above 100%. Category counts use observed, timestamp-valid, cutoff-bounded items by known non-PR-authors as their comment denominator. Recurrence is the number/percentage of distinct PRs signaled for a category, with a denominator limited to PRs whose submitted reviews, inline review comments, and conversation comments are all complete, paginated, and history-complete. Unavailable/not-requested sources are never treated as empty success.

The Streamlit UI displays comment text only in the local evidence view, with PR/comment links. No comment text is sent to an external AI provider, persisted, or copied into category summaries. `addressed_status` is always `not_assessed`: a reply or later commit does not prove that feedback was resolved, and REST-only review-thread resolution is unavailable. The taxonomy is a rule-based navigation aid for human review, not an assertion that feedback is correct, a causal explanation, or an assessment/ranking of an individual. Missing or invalid timestamps and incomplete histories are visible and excluded from time-bounded conclusions.

### Runtime orchestration (implemented)

The five `.github/agents/*.agent.md` files remain VS Code Copilot development
instructions. They are not executed by the website. The Streamlit submit action
now invokes `python_script.orchestrator.Orchestrator`, which invokes explicit
runtime specialist components against versioned request/result contracts:

1. `GitHubDataAgent` reuses `fetch_repository` and requests PR details,
   lifecycle events, commits, reviews, both comment sources, and current-head
   checks. Collector provenance, pagination, identity, date filtering, and
   coverage remain authoritative.
2. `PRAnalyticsReviewIntelligenceAgent` reuses deterministic metrics,
   taxonomy classification, and the evidence-linked Action Plan.
3. `CodeQualityTestReliabilityAgent` summarizes collected PR hygiene and check
   outcomes. Checks describe current heads; no repeated-run test evidence is
   collected, so it does not diagnose flaky tests.
4. `DashboardUIUXAgent` supplies the agreed section order and coverage/status
   data contract for the existing Streamlit UI.
5. The local synthesis stage invokes `summarize_comments` through Ollama on
   loopback only when comment evidence exists. This is the only LLM reasoning
   stage; local Qwen output remains citation-validated.
6. `ValidationAgent` reconciles included comment-role counts and checks model
   citations against returned evidence IDs before display.

Each stage reports pending/running/completed/skipped/failed. Model failure
preserves deterministic metrics and the Action Plan. The dashboard shows the
Action Plan first after filters, followed by runtime/source coverage and the
existing analytical views. GitHub collection remains read-only; credentials are read from the user session input and passed only to the GitHub client. They are not included in model context or persisted by the app.

### Local all-role comment summary (additive v2 boundary; agreed)

After the user selects **Load PR metrics**, the dashboard builds one summary request across the successfully analyzed repositories and invokes the local adapter once when text-bearing evidence exists. The summary input includes text from all three collected sources—submitted review summaries, inline review comments, and PR conversation comments—including the selected PR author's responses and comments with unknown identities. The author/date filter selects the PR population; it does not discard later comments based on their timestamp. Exact identical stable-ID records are deduplicated by the analyzer and counted in data-quality metadata; conflicting duplicate IDs retain distinct text with conflict provenance.

Each repository context carries its host origin, owner/name, observation cutoff, selected PR count, and population coverage (`status`, pagination/history completeness, whether the population is complete for target selection, declared count when available, and exclusion counts). The request separately carries per-source/per-PR comment-history status. Therefore, complete source histories for returned PRs do not imply a complete PR population. The dashboard warns that the summary is partial when either population selection or any source history is incomplete. A successfully empty source is distinct from unavailable, failed, or not-requested history.

Every model theme and factual claim must cite supplied evidence IDs. The adapter validates the response schema and all citations before displaying it; original comment text and source/PR links remain available for verification. Comment text is treated as untrusted input, and output is explicitly an uncertain model-generated interpretation, not deterministic classification, a resolution judgment, or an author-performance score. Empty evidence skips inference; model errors leave collected metrics available and can be retried. The result is cached only in the active Streamlit session.

The adapter's application-code boundary is a single HTTP request to `127.0.0.1:11434` with no remote fallback and no GitHub token. This does not enforce OS-level network isolation or constrain other software on the machine. No external AI service or GitHub write operation is used by this flow. Tests mock the adapter; no real local inference or authenticated GitHub collection has been verified. The configured model is Qwen2.5 3B; oversized evidence is rejected rather than truncated.

### Author Improvement Action Plan

After filters are applied, the dashboard displays an author-specific Action Plan before the selected analytics page. It reuses the same comment-analysis report and taxonomy as People Analytics, selecting target-authored PRs by the inclusive creation window while including their eligible later feedback through the collection observation cutoff. The selected author is shared with detailed comment analytics on the dashboard pages.

The plan offers up to five deterministic recommendations, limited to categorized reviewer-role evidence. Each recommendation reports the matched taxonomy signal, distinct affected PR count, deduplicated evidence-item count, a category-specific action and validation step, and links with original reviewer comment excerpts. Its “what reviewers flagged” summary includes the distinct-PR/item counts and up to two original reviewer excerpts. Ordering is explicit: security, functional behavior/API contracts, error handling, edge cases, testing, then maintainability/style/documentation; within a tier, affected PR count and then evidence-item count break ties. This policy is a deterministic ordering, not an empirical severity score. A validation checklist is displayed only when recommendations exist and is derived from their validation steps; generic checklist text is not shown as personalized advice. Unclassified reviewer feedback is still counted and shown, but does not generate a category recommendation. The source-coverage table separates observed reviewer feedback items, author responses, and unknown identities per source from PR-history coverage counts, so repeated PR totals are not mistaken for comment totals.

Coverage is shown separately for submitted review summaries, inline review comments, and PR conversation comments, including complete, successfully empty, partial, unavailable, and not-requested histories. Empty feedback is asserted only when the selected PR population and all three sources are complete. Failed repositories or report analysis are called out and recommendations are explicitly scoped to successful/analyzable repositories; missing evidence is never treated as zero. Responses from the selected author and comments with unknown identities are counted separately from reviewer feedback. Duplicate stable IDs are deduplicated according to the analyzer's source identity rules.

The plan uses no external AI provider and makes no GitHub write operations. Taxonomy matches are rule-based signals, not verified defects or semantic interpretations; templates advise what to inspect and how to validate, without claiming a concern is true. It does not assess motivation, competence, or personal performance. Recommendation limits and source coverage must accompany conclusions.

Never attribute delay to an author, reviewer, or CI without event evidence that supports the specific causal interval. Otherwise report only the named observation (or unknown/unavailable). Never infer motivation, competence, or personal performance from raw statistics.

## Acceptance criteria

### Phase 1: contracts and backend foundation

1. Five agents load from `.github/agents/`; frontmatter parses with required descriptions, tool names and allowed-agent references resolve, and the backend agent reads the shared instructions and contract.
2. Backend implementation follows the agreed request, result, normalized-record, error, provenance, and security contracts; any deviation is documented and agreed before UI work.
3. GitHub collection is read-only, paginated, rate-limit aware, and explicit about partial coverage, permission failures, unsupported data, and API errors.
4. Mocked tests cover pagination, stable normalization/provenance, missing permission, incomplete history, rate-limit/error behavior, and empty-success versus unavailable results.
5. Analytics definitions in this document are finalized contract rules. The six supported calculations are implemented and have focused synthetic tests for denominators, timestamp validity, incomplete histories, cutoffs, invalid timestamps, and evidence. Unsupported metric families remain gated on separate evidence/definition agreement.
6. No real GitHub token or live private-repository data is needed for tests; secrets are not stored in code, logs, or fixtures.

### UI start gate

Full UI implementation may start only after the orchestrator records backend and analytics agreement on: request/result schemas; PR/event/review/comment/check record fields; completeness/error semantics; time-window/population rules; and each displayed metric's formula, denominator, and missing-data behavior.

### Overall dashboard acceptance

When UI work is authorized, views use live contract-backed data and show clear loading, empty, error, and partial-data states; distinguish unavailable from zero/negative; expose metric definitions and source evidence; respect repository authorization; and provide focused tests for the supported states. Fixtures, if needed during development, are labeled and never presented as live data.
