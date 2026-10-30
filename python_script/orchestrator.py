"""Explicit runtime orchestration for a filtered PR intelligence review.

The specialists are callable application components with versioned, validated
JSON-like inputs and outputs. Language synthesis uses only the existing local
Ollama adapter; collection, metrics, taxonomy, and validation remain Python.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from .action_plan import build_action_plan
from .comment_analysis import analyze_comments
from .comment_summary import build_request
from .fetch_github_pr_data import (
    DEFAULT_API_URL,
    GitHubClient,
    GitHubAPIError,
    fetch_repository,
    parse_utc,
)
from .local_comment_agent import summarize_comments
from .local_comment_agent import LocalCommentAgentError
from .metrics import analyze_repository


REVIEW_RESOURCES = (
    "pull_requests", "pull_request_details", "lifecycle_events", "commits",
    "reviews", "review_comments", "issue_comments", "checks",
)
STAGES = (
    "github_data", "pr_analytics_review_intelligence",
    "code_quality_test_reliability", "dashboard_ui_ux",
    "local_language_synthesis", "validation",
)


def _stage(status: str, **details: Any) -> dict[str, Any]:
    return {"status": status, **details}


def _safe_failure(error: BaseException) -> str:
    """Expose adapter-safe guidance without echoing exception bodies or credentials."""
    if isinstance(error, LocalCommentAgentError):
        return str(error)
    if isinstance(error, GitHubAPIError):
        status = error.status
        if status == 401:
            return "GitHub rejected the token. Check that it is active and valid for this Enterprise host."
        if status == 403:
            return "GitHub denied access. Check repository read permissions and token scopes, or try again after rate limiting clears."
        if status == 404:
            return "The repository was not found or is not accessible to this token."
        return f"GitHub collection failed (HTTP {status}). Check repository access and API connectivity."
    return f"{type(error).__name__}; sensitive error details were omitted."


class GitHubDataAgent:
    """Collection specialist; consumes filters and returns the collector envelope."""

    input_schema = "github-review-request-v1"
    output_schema = "github-review-evidence-v1"

    def __init__(self, client_factory: Callable[..., Any] = GitHubClient):
        self.client_factory = client_factory

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("schema_version") != self.input_schema:
            raise ValueError("GitHub data request schema mismatch")
        client = self.client_factory(DEFAULT_API_URL, request["token"])
        envelope = fetch_repository(
            client, request["owner"], request["repository"],
            parse_utc(request["from_day"]), parse_utc(request["to_day"], date_end=True),
            None, author=request.get("author"), include=REVIEW_RESOURCES,
            progress_callback=request.get("progress_callback"),
        )
        if not isinstance(envelope, dict) or not isinstance(envelope.get("resources"), dict):
            raise ValueError("GitHub collector returned malformed evidence")
        return {"schema_version": self.output_schema, "envelope": envelope}


class PRAnalyticsReviewIntelligenceAgent:
    """Combines established deterministic metrics, comment taxonomy, and action plan."""

    input_schema = "pr-analytics-input-v1"
    output_schema = "pr-analytics-result-v1"

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("schema_version") != self.input_schema:
            raise ValueError("PR analytics input schema mismatch")
        envelope = request["envelope"]
        metrics = analyze_repository(envelope)
        report = analyze_comments(envelope, request["author"])
        plan = build_action_plan([report])
        return {"schema_version": self.output_schema, "metrics": metrics,
                "comment_report": report, "action_plan": plan}


class CodeQualityTestReliabilityAgent:
    """Summarizes observed hygiene/check evidence; never diagnoses flakiness."""

    input_schema = "quality-evidence-input-v1"
    output_schema = "quality-evidence-result-v1"

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("schema_version") != self.input_schema:
            raise ValueError("Quality evidence input schema mismatch")
        envelope = request["envelope"]
        resources = envelope.get("resources", {})
        checks_resource = resources.get("checks", {})
        checks = envelope.get("checks", [])
        counts: dict[str, int] = {}
        for check in checks:
            state = str(check.get("conclusion") or check.get("status") or "unknown").casefold()
            counts[state] = counts.get(state, 0) + 1
        return {"schema_version": self.output_schema,
                "checks": {"status": checks_resource.get("status", "unavailable"),
                           "count": checks_resource.get("count"), "outcomes": counts,
                           "scope": "current PR head only"},
                "pr_hygiene": {"status": "observed", "signals": [
                    key for key in ("title", "body")
                    if any(not pr.get(key) for pr in envelope.get("pull_requests", []))
                ]},
                "flaky_test_claim": None,
                "flaky_test_note": "Repeated-run evidence is not collected; no flaky-test claim is made."}


class DashboardUIUXAgent:
    """Produces the agreed dashboard presentation contract without duplicating UI logic."""

    input_schema = "dashboard-presentation-input-v1"
    output_schema = "dashboard-presentation-v1"

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("schema_version") != self.input_schema:
            raise ValueError("Dashboard presentation input schema mismatch")
        return {"schema_version": self.output_schema,
                "sections": ["Author Improvement Action Plan", "Source coverage and runtime status",
                             "PR metrics and lifecycle analytics", "Feedback evidence and PR drill-downs"],
                "action_plan": request["action_plan"],
                "source_coverage": request["comment_report"]["resource_coverage"],
                "agent_status": request["agent_status"]}


class ValidationAgent:
    """Deterministic evidence, count, and citation validation stage."""

    input_schema = "validation-input-v1"
    output_schema = "validation-result-v1"

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("schema_version") != self.input_schema:
            raise ValueError("Validation input schema mismatch")
        report = request["comment_report"]
        inventory = report.get("comment_inventory", [])
        summary = request.get("summary")
        evidence_ids: set[str] = set()
        if summary:
            expected_evidence_ids = {
                item.get("evidence_id") for item in build_request(report).get("evidence", [])
            }
            returned_evidence_ids = {
                item.get("evidence_id") for item in summary.get("evidence", [])
            }
            if not returned_evidence_ids <= expected_evidence_ids:
                raise ValueError("Summary returned evidence outside collected inputs")
            evidence_ids = expected_evidence_ids
            for theme in summary.get("themes", []):
                if not set(theme.get("evidence_ids", [])) <= evidence_ids:
                    raise ValueError("Summary cited evidence outside collected inputs")
                for claim in theme.get("claims", []):
                    if not set(claim.get("evidence_ids", [])) <= evidence_ids:
                        raise ValueError("Summary claim cited evidence outside collected inputs")
        activity = report.get("comment_activity", {})
        observed_roles = sum(activity.get(key, 0) for key in (
            "observed_reviewer_comment_count", "observed_author_response_count",
            "observed_unknown_author_count"))
        included = sum(item.get("included_in_observed_activity") is True for item in inventory)
        if observed_roles != included:
            raise ValueError("Comment activity counts disagree with the evidence inventory")
        return {"schema_version": self.output_schema, "valid": True,
                "evidence_count": len(evidence_ids), "validated_claim_citations": bool(summary),
                "summary_status": "validated" if summary else "skipped"}


class Orchestrator:
    """Run explicit specialist stages and retain deterministic output on model failure."""

    def __init__(self, *, data_agent: Any | None = None, analytics_agent: Any | None = None,
                 quality_agent: Any | None = None, ui_agent: Any | None = None,
                 validation_agent: Any | None = None,
                 summary_fn: Callable[..., dict[str, Any]] = summarize_comments):
        self.data_agent = data_agent or GitHubDataAgent()
        self.analytics_agent = analytics_agent or PRAnalyticsReviewIntelligenceAgent()
        self.quality_agent = quality_agent or CodeQualityTestReliabilityAgent()
        self.ui_agent = ui_agent or DashboardUIUXAgent()
        self.validation_agent = validation_agent or ValidationAgent()
        self.summary_fn = summary_fn

    def run(self, *, owner: str, repository: str, from_day: date, to_day: date,
            author: str | None, token: str,
            progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
        statuses = {name: _stage("pending") for name in STAGES}
        def announce(message: str) -> None:
            if progress_callback is not None:
                progress_callback(message)
        request = {"schema_version": GitHubDataAgent.input_schema, "owner": owner,
                   "repository": repository, "from_day": from_day.isoformat(),
                   "to_day": to_day.isoformat(), "author": author,
                   "token": token, "progress_callback": progress_callback}
        statuses["github_data"] = _stage("running")
        announce("Runtime agent: GitHub Data Agent running.")
        try:
            data = self.data_agent.run(request)
            envelope = data["envelope"]
            if data.get("schema_version") != GitHubDataAgent.output_schema:
                raise ValueError("GitHub data output schema mismatch")
            statuses["github_data"] = _stage("completed", coverage=envelope.get("resources", {}))
            announce("Runtime agent: GitHub Data Agent completed.")
        except Exception as error:
            message = _safe_failure(error)
            statuses["github_data"] = _stage("failed", message=message)
            return {"status": statuses, "results": {}, "failures": {"collection": message}}

        analytics = None
        for key, agent, schema, payload in (
            ("pr_analytics_review_intelligence", self.analytics_agent,
             PRAnalyticsReviewIntelligenceAgent.input_schema,
             {"envelope": envelope, "author": author}),
            ("code_quality_test_reliability", self.quality_agent,
             CodeQualityTestReliabilityAgent.input_schema, {"envelope": envelope}),
        ):
            statuses[key] = _stage("running")
            announce(f"Runtime agent: {key.replace('_', ' ')} running.")
            try:
                result = agent.run({"schema_version": schema, **payload})
                if result.get("schema_version") != agent.output_schema:
                    raise ValueError("Specialist output schema mismatch")
                if key == "pr_analytics_review_intelligence":
                    if not isinstance(result.get("metrics"), dict) or not isinstance(result.get("comment_report"), dict):
                        raise ValueError("PR analytics output is malformed")
                elif not isinstance(result.get("checks"), dict):
                    raise ValueError("Quality evidence output is malformed")
                if key == "pr_analytics_review_intelligence":
                    analytics = result
                else:
                    quality = result
                statuses[key] = _stage("completed")
                announce(f"Runtime agent: {key.replace('_', ' ')} completed.")
            except Exception as error:
                statuses[key] = _stage("failed", message=_safe_failure(error))
                if key == "pr_analytics_review_intelligence":
                    return {"status": statuses, "envelope": envelope,
                            "failures": {key: _safe_failure(error)}}
                quality = None

        if analytics is None:
            analytics = {"metrics": analyze_repository(envelope), "comment_report": None,
                         "action_plan": None}
        reports = [analytics["comment_report"]] if analytics.get("comment_report") else []
        statuses["local_language_synthesis"] = _stage("running")
        announce("Runtime agent: local language synthesis running.")
        summary = None
        try:
            summary_ready_count = sum(
                isinstance(item, Mapping)
                and item.get("included_in_summary") is True
                and isinstance(item.get("summary_text"), str)
                and bool(item["summary_text"].strip())
                for item in reports[0].get("comment_inventory", [])
            ) if reports else 0
            if reports and summary_ready_count:
                summary = self.summary_fn(reports, progress_callback=progress_callback)
            else:
                statuses["local_language_synthesis"] = _stage(
                    "skipped", reason="No text-bearing comments in the summary-ready inventory"
                )
        except Exception as error:
            statuses["local_language_synthesis"] = _stage("failed", message=_safe_failure(error))
        if summary is not None:
            # Synthesis is complete only when the adapter explicitly certifies
            # completeness. Missing or unknown status must fail closed.
            synthesis_status = summary.get("synthesis_status")
            if synthesis_status == "complete":
                statuses["local_language_synthesis"] = _stage(
                    "completed", model="qwen2.5:3b via local Ollama",
                    summary_status=synthesis_status,
                    batch_count=summary.get("batch_count"),
                    completed_batch_count=summary.get("completed_batch_count"),
                )
                announce("Runtime agent: local language synthesis completed.")
            else:
                statuses["local_language_synthesis"] = _stage(
                    "failed", model="qwen2.5:3b via local Ollama",
                    summary_status=synthesis_status,
                    message=summary.get("synthesis_message", "Local synthesis was incomplete."),
                    batch_count=summary.get("batch_count"),
                    completed_batch_count=summary.get("completed_batch_count"),
                )
                announce("Runtime agent: local language synthesis partial; deterministic recommendations remain available.")

        presentation = None
        if analytics.get("comment_report"):
            statuses["dashboard_ui_ux"] = _stage("running")
            announce("Runtime agent: Dashboard UI and User Experience Agent running.")
            try:
                presentation = self.ui_agent.run({
                    "schema_version": DashboardUIUXAgent.input_schema,
                    "action_plan": analytics.get("action_plan"),
                    "comment_report": analytics["comment_report"], "agent_status": statuses})
                if (not isinstance(presentation, dict)
                        or presentation.get("schema_version") != self.ui_agent.output_schema
                        or not isinstance(presentation.get("sections"), list)):
                    raise ValueError("Dashboard presentation output is malformed")
                statuses["dashboard_ui_ux"] = _stage("completed")
                announce("Runtime agent: Dashboard UI and User Experience Agent completed.")
            except Exception as error:
                statuses["dashboard_ui_ux"] = _stage("failed", message=_safe_failure(error))
            statuses["validation"] = _stage("running")
            announce("Runtime agent: Validation Agent running.")
            try:
                validation = self.validation_agent.run({
                    "schema_version": ValidationAgent.input_schema,
                    "comment_report": analytics["comment_report"], "summary": summary})
                if (not isinstance(validation, dict)
                        or validation.get("schema_version") != self.validation_agent.output_schema
                        or validation.get("valid") is not True):
                    raise ValueError("Validation output is malformed or rejected")
                statuses["validation"] = _stage("completed", result=validation)
                announce("Runtime agent: Validation Agent completed.")
            except Exception as error:
                statuses["validation"] = _stage("failed", message=_safe_failure(error))
        return {"status": statuses, "envelope": envelope, "analytics": analytics,
                "quality": quality, "summary": summary, "presentation": presentation}
