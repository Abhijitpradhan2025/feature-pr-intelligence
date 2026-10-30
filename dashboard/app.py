"""Local PR metrics dashboard.

Run from the workspace root with: ``streamlit run dashboard/app.py``.
Enter a read-only GitHub Enterprise token in the sidebar. The token is kept
only in the active Streamlit session and is never shown by the UI.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import streamlit as st

from python_script.author_filter import normalize_author_filter, parse_author_logins
from python_script.fetch_github_pr_data import (
    AVAILABLE_REPOSITORIES,
    DEFAULT_API_URL,
    GitHubAPIError,
    GitHubClient,
    TARGET_AUTHOR,
    discover_accessible_repositories,
    fetch_repository,
    parse_repository,
    parse_utc,
)
from python_script.comment_analysis import (
    CommentAnalysisInputError,
    analyze_comments,
)
from python_script.action_plan import ActionPlanInputError, build_action_plan
from python_script.comment_summary import CommentSummaryContractError
from python_script.local_comment_agent import (
    LocalCommentAgentError,
    summarize_comments,
)
from python_script.metrics import AnalyticsInputError, analyze_repository
from python_script.orchestrator import Orchestrator
from python_script.orchestrator import Orchestrator


METRICS_RESOURCES = (
    "pull_requests",
    "pull_request_details",
    "lifecycle_events",
    "commits",
    "reviews",
    "review_comments",
    "issue_comments",
)

METRIC_TITLES = {
    "pr_size": "PR size",
    "first_submitted_review_latency": "First submitted review",
    "merge_latency": "Merge latency",
    "reopened_event_count": "Reopened events",
    "commit_count": "Commit count",
    "adjacent_activity_gap": "Adjacent activity gaps",
}

PAGES = (
    "Overview",
    "Repository Analytics",
    "People Analytics",
    "PR Explorer",
    "Quality & Reliability",
    "Insights & Recommendations",
)


class DashboardInputError(ValueError):
    """A safe, user-correctable input validation error."""


def validate_author(value: str) -> str | None:
    """Return normalized comma-separated GitHub logins, or None for all authors."""
    try:
        return normalize_author_filter(value)
    except ValueError as error:
        raise DashboardInputError(
            "Enter valid GitHub usernames separated by commas, or leave blank for all authors."
        ) from error


def configured_api_url() -> str:
    """Return the fixed Enterprise API root; environment overrides are ignored."""
    return DEFAULT_API_URL


def present_metric(metric: dict[str, Any]) -> dict[str, Any]:
    """Prepare a metric for display without converting unknown values to zero."""
    status = metric.get("status", "unavailable")
    value = metric.get("value")
    reason = metric.get("unavailable_reason")
    denominator = metric.get("denominator")
    numerator = metric.get("numerator")
    coverage = metric.get("coverage_fraction")
    if status == "unavailable" or value is None:
        if reason == "no_eligible_external_submitted_reviews":
            value_text = "No eligible review observations"
        elif reason == "no_merged_prs":
            value_text = "No merged PR observations"
        elif reason == "zero_population":
            value_text = "Not applicable — no PRs in the selected population"
        elif reason:
            value_text = "Unavailable"
        else:
            value_text = "Unknown / unavailable"
    else:
        value_text = format_metric_value(value, metric.get("unit"))

    denominator_text = metric_denominator_text(
        metric.get("name"), numerator, denominator, status
    )

    if coverage is not None:
        coverage_text = f"{coverage:.0%} observation coverage"
    elif status == "unavailable":
        coverage_text = "Coverage unavailable"
    else:
        coverage_text = "Coverage not applicable"

    if status == "partial":
        status_text = "Partial — value covers eligible records only"
    elif status == "complete":
        status_text = "Complete for the stated denominator"
    else:
        status_text = "Unavailable — not a zero result"

    return {
        "title": METRIC_TITLES.get(metric.get("name"), metric.get("name", "Metric")),
        "value": value_text,
        "status": status_text,
        "denominator": denominator_text,
        "coverage": coverage_text,
        "definition": metric.get("definition") or "Definition unavailable.",
        "unit": metric.get("unit"),
        "evidence": metric.get("evidence") if isinstance(metric.get("evidence"), list) else [],
        "invalid_counts": (
            metric.get("invalid_counts")
            if isinstance(metric.get("invalid_counts"), dict)
            else {}
        ),
        "unknown_pr_count": metric.get("unknown_pr_count"),
        "unavailable_reason": reason,
    }


def metric_denominator_text(
    name: str | None, numerator: Any, denominator: Any, status: str
) -> str:
    if denominator is None:
        return "Denominator unavailable"
    if status == "unavailable":
        return f"Unavailable · denominator {denominator}"
    if name == "pr_size":
        return f"{numerator} / {denominator} population PRs with size data"
    if name == "first_submitted_review_latency":
        return f"{numerator} / {denominator} review-complete PRs with an eligible first review"
    if name == "merge_latency":
        return f"{numerator} / {denominator} population PRs merged"
    if name == "reopened_event_count":
        return f"{numerator} observed events / {denominator} lifecycle-covered PRs"
    if name == "commit_count":
        return f"{numerator} valid commits / {denominator} commit-covered PRs"
    if name == "adjacent_activity_gap":
        return f"{numerator} observed pairs / {denominator} PRs with complete activity histories"
    return f"{numerator} / {denominator} observations"


def format_metric_value(value: Any, unit: str | None) -> str:
    if unit == "seconds":
        return f"{value / 3600:,.1f} hours (median elapsed UTC)"
    if unit == "seconds per adjacent pair":
        return f"{value / 3600:,.1f} hours (median elapsed UTC gap)"
    if unit == "changed lines":
        return f"{value:,.1f} changed lines (median)"
    if unit == "commits per PR":
        return f"{value:,.1f} commits per covered PR (median)"
    if unit == "events":
        return f"{value:,} observed reopened events"
    return str(value)


def prepare_pr_explorer(
    pull_requests: Any,
    pull_request_resources: Any = None,
) -> list[dict[str, Any]]:
    """Keep explorer output to approved non-body PR metadata."""
    if not isinstance(pull_requests, list):
        return []
    per_pr_resources = (
        pull_request_resources if isinstance(pull_request_resources, dict) else {}
    )
    rows = []
    for pr in pull_requests:
        if not isinstance(pr, dict):
            continue
        author = pr.get("author")
        author_name = author.get("login") if isinstance(author, dict) else None
        pr_url = pr.get("url") if safe_https_url(pr.get("url")) else None
        detail_record = per_pr_resources.get(str(pr.get("id")), {})
        detail_status = (
            detail_record.get("pull_request_details", {}).get("status", "unknown")
            if isinstance(detail_record, dict)
            and isinstance(detail_record.get("pull_request_details"), dict)
            else "unknown"
        )
        rows.append({
            "Number": pr.get("number"),
            "Title": pr.get("title") or "(untitled)",
            "Author": author_name or "Unknown",
            "State": pr.get("state") or "Unknown",
            "Created": pr.get("created_at") or "Unknown",
            "Additions": pr.get("additions") if pr.get("additions") is not None else "Unavailable",
            "Deletions": pr.get("deletions") if pr.get("deletions") is not None else "Unavailable",
            "Changed files": pr.get("changed_files") if pr.get("changed_files") is not None else "Unavailable",
            "Size details": detail_status.capitalize(),
            "PR": pr_url,
            "Files changed": f"{pr_url.split('#', 1)[0].rstrip('/')}/files" if pr_url else None,
        })
    return rows


def safe_https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
        and not parsed.query
    )


def safe_error_message(error: BaseException) -> str:
    """Return non-sensitive error guidance; never echo exception or token text."""
    if isinstance(error, GitHubAPIError):
        if error.status == 401:
            return "GitHub rejected this token. Check that it is active and valid for this Enterprise host."
        if error.status == 403:
            return "GitHub denied access or rate-limited this request. Check that the token has read access to the repository and try later."
        if error.status == 404:
            return "The repository or requested endpoint was not found or is not accessible to this token."
        if error.status:
            return f"GitHub request failed (HTTP {error.status}). Check the API host, permissions, and connectivity."
        return "GitHub request failed. Check the API host and network connectivity."
    if isinstance(error, AnalyticsInputError):
        return "The collected response did not satisfy the agreed analytics contract; no metrics were rendered."
    if isinstance(error, LocalCommentAgentError):
        return str(error)
    if isinstance(error, CommentSummaryContractError):
        return "The collected comment evidence did not satisfy the local summary contract."
    if isinstance(error, DashboardInputError):
        return str(error)
    return "The request could not be completed. Check input, permissions, and connectivity; sensitive details were omitted."


def _clear_token_scoped_state() -> None:
    """Discard cached GitHub data when the session credential changes."""
    for key in (
        "discovered_repositories", "portfolio_results", "portfolio_failures",
        "portfolio_repositories", "runtime_orchestration", "current_action_plan",
        "local_comment_summary",
    ):
        st.session_state.pop(key, None)
    st.session_state["collection_complete"] = False
    st.session_state["collection_generation"] = (
        st.session_state.get("collection_generation", 0) + 1
    )
    configured = set(AVAILABLE_REPOSITORIES)
    selected = st.session_state.get("selected_repositories")
    if isinstance(selected, list):
        st.session_state["selected_repositories"] = [
            repository for repository in selected if repository in configured
        ]


def _handle_github_token_change() -> None:
    """Clear prior-credential data whenever the password widget changes."""
    _clear_token_scoped_state()
    st.session_state["repository_discovery_status"] = {
        "kind": "info",
        "text": "Token updated; previous discovery and collected results were cleared.",
    }


def collect_metrics(
    *,
    owner: str,
    repository: str,
    from_day: date,
    to_day: date,
    author: str | None,
    token: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Collect only metric-required resources and analyze in memory."""
    credential = token
    if not credential:
        raise DashboardInputError(
            "Enter your GitHub Enterprise token in the sidebar before collecting data."
        )
    client = GitHubClient(DEFAULT_API_URL, credential)
    envelope = fetch_repository(
        client,
        owner,
        repository,
        parse_utc(from_day.isoformat()),
        parse_utc(to_day.isoformat(), date_end=True),
        None,
        author=author,
        include=METRICS_RESOURCES,
        progress_callback=progress_callback,
    )
    if envelope.get("status") == "error":
        raise GitHubAPIError(
            (envelope.get("error") or {}).get("status_code") or 0,
            "Repository collection failed",
        )
    return envelope, analyze_repository(envelope)


def discover_repositories(token: str | None = None) -> list[dict[str, Any]]:
    """Discover accessible repositories only when called by an explicit UI action."""
    credential = token
    if not credential:
        raise DashboardInputError(
            "Enter your GitHub Enterprise token in the sidebar before repository discovery."
        )
    return discover_accessible_repositories(GitHubClient(DEFAULT_API_URL, credential))


def repository_identity(repository: dict[str, Any] | str) -> tuple[str, str, str]:
    """Normalize configured, discovered, and manual repository selections."""
    if isinstance(repository, str):
        owner, name = parse_repository(repository)
    else:
        owner = repository.get("owner")
        name = repository.get("name")
        if not owner or not name:
            owner, name = parse_repository(str(repository.get("full_name") or ""))
    return str(owner), str(name), f"{owner}/{name}"


def collect_portfolio_metrics(
    repositories: list[dict[str, Any] | str],
    *,
    from_day: date,
    to_day: date,
    author: str | None,
    token: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, tuple[dict[str, Any], dict[str, Any]]], dict[str, str]]:
    """Collect each repository independently; retain successful results on failures."""
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    failures: dict[str, str] = {}
    seen: set[str] = set()
    for selected in repositories:
        try:
            owner, name, label = repository_identity(selected)
            key = label.casefold()
            if key in seen:
                continue
            seen.add(key)
            if progress_callback is not None:
                progress_callback(f"Starting collection for {label}…")
            collect_options = {
                "owner": owner,
                "repository": name,
                "from_day": from_day,
                "to_day": to_day,
                "author": author,
                "token": token,
            }
            if progress_callback is not None:
                collect_options["progress_callback"] = progress_callback
            envelope, analysis = collect_metrics(
                **collect_options,
            )
            results[label] = (envelope, analysis)
            if progress_callback is not None:
                comment_count = sum(
                    len(envelope.get(resource) or [])
                    for resource in ("reviews", "review_comments", "issue_comments")
                )
                progress_callback(
                    f"Collected {len(envelope.get('pull_requests') or [])} PR(s) and "
                    f"{comment_count} review/comment record(s) from {label}."
                )
        except Exception as error:
            try:
                _, _, label = repository_identity(selected)
            except Exception:
                label = "Selected repository"
            failures[label] = safe_error_message(error)
            if progress_callback is not None:
                progress_callback(f"Collection failed for {label}; continuing with other repositories.")
    return results, failures


def _exact_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def repository_metadata_caption(repository: dict[str, Any]) -> str:
    """Format only discovered repository metadata; never infer a branch."""
    parts = []
    for field, title in (
        ("visibility", "Visibility"),
        ("language", "Language"),
        ("default_branch", "Default branch"),
    ):
        value = repository.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(f"{title}: {value}")
    return " · ".join(parts)


def aggregate_portfolio_metrics(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Aggregate only validated exact counts; never combine repository medians."""
    failures = failures or {}
    if not results:
        return {"status": "unavailable", "population_count": None, "metrics": {}}

    envelope_statuses = {
        envelope.get("status") for envelope, _ in results.values()
    }
    population_values = []
    for envelope, analysis in results.values():
        population = analysis.get("population_count")
        pr_resource = (envelope.get("resources") or {}).get("pull_requests") or {}
        if (
            not _exact_nonnegative_int(population)
            or pr_resource.get("status") != "complete"
            or pr_resource.get("pagination_complete") is not True
            or not _exact_nonnegative_int(pr_resource.get("count"))
            or pr_resource.get("count") != population
        ):
            population_values = []
            break
        population_values.append(population)
    population_available = len(population_values) == len(results)

    metric_keys = {
        "reopened_event_count": ("numerator", "denominator", "events"),
        "commit_count": ("total_valid_commits", "denominator", "commits"),
    }
    aggregate_metrics: dict[str, dict[str, Any]] = {}
    for metric_name, (numerator_field, denominator_field, unit) in metric_keys.items():
        metric_records = []
        for envelope, analysis in results.values():
            metric = (analysis.get("metrics") or {}).get(metric_name)
            metric = metric if isinstance(metric, dict) else {}
            metric_records.append((envelope, analysis, metric))
        eligible = [
            (envelope, analysis, metric)
            for envelope, analysis, metric in metric_records
            if metric.get("status") in {"complete", "partial"}
            and _exact_nonnegative_int(metric.get(numerator_field))
            and _exact_nonnegative_int(metric.get("numerator"))
            and _exact_nonnegative_int(metric.get(denominator_field))
            and metric.get("name") == metric_name
            and isinstance(metric.get("definition"), str)
            and metric.get("definition")
            and metric.get("unit") == unit
            and metric.get("numerator") == metric.get(numerator_field)
            and _exact_nonnegative_int(metric.get("population_count"))
            and metric.get("population_count") == analysis.get("population_count")
            and metric.get(denominator_field) <= metric.get("population_count")
        ]
        metrics = [(envelope, metric) for envelope, _, metric in eligible]
        excluded_reasons = []
        for label, (envelope, analysis) in zip(results, results.values()):
            metric = (analysis.get("metrics") or {}).get(metric_name) or {}
            if metric not in [entry[2] for entry in eligible]:
                reason = metric.get("unavailable_reason") or metric.get("status") or "invalid metric contract"
                excluded_reasons.append(f"{label}: {reason}")
        same_basis = all(
            envelope.get("requested_window") == metrics[0][0].get("requested_window")
            and (envelope.get("filters") or {}).get("author")
            == (metrics[0][0].get("filters") or {}).get("author")
            for envelope, _ in metrics
        )
        same_definition = len({metric.get("definition") for _, metric in metrics}) == 1
        has_uncovered_results = len(eligible) != len(results)
        if (
            metrics and same_basis and same_definition
        ):
            numerator = sum(metric[numerator_field] for _, metric in metrics)
            denominator = sum(metric[denominator_field] for _, metric in metrics)
            population_total = sum(population_values) if population_available else None
            metric_statuses = {metric.get("status") for _, metric in metrics}
            aggregate_status = (
                "complete" if not has_uncovered_results and metric_statuses == {"complete"}
                else "partial"
            )
            aggregate_metrics[metric_name] = {
                "status": aggregate_status,
                "value": numerator,
                "numerator": numerator,
                "denominator": denominator,
                "unit": unit,
                "definition": metrics[0][1]["definition"],
                "covered_pr_count": denominator,
                "unknown_pr_count": (
                    max(0, population_total - denominator)
                    if population_total is not None else None
                ),
                "coverage_fraction": (
                    denominator / population_total
                    if population_total else None
                ),
                "repositories": len(metrics),
                "excluded_repository_count": len(results) - len(metrics),
                "coverage_note": (
                    "Exact total across metric-eligible repositories. "
                    + ("Excluded: " + "; ".join(excluded_reasons) if excluded_reasons else "All selected repositories contributed.")
                ),
            }
        else:
            aggregate_metrics[metric_name] = {
                "status": "partial" if results else "unavailable",
                "value": None,
                "message": (
                    "No repository has eligible metric coverage. "
                    + ("; ".join(excluded_reasons) if excluded_reasons else "The selected repositories use different windows, filters, or metric definitions.")
                ),
            }

    valid_commit_metric = aggregate_metrics["commit_count"]
    if valid_commit_metric.get("value") is not None:
        valid_commit_metric["total_valid_commits"] = valid_commit_metric["value"]
        valid_commit_metric["value"] = None
        valid_commit_metric["message"] = (
            "Exact total valid commits; repository medians are shown separately below."
        )
    portfolio_status = (
        "partial" if failures or envelope_statuses != {"complete"} else "complete"
    )
    return {
        "status": portfolio_status,
        "population_count": sum(population_values) if population_available else None,
        "population_status": "complete" if population_available and not failures else "partial",
        "metrics": aggregate_metrics,
    }


def render_portfolio(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str],
    repositories: list[dict[str, Any] | str],
    *,
    comment_author: str | None = None,
) -> None:
    aggregate = aggregate_portfolio_metrics(results, failures)
    status_message = collection_status_message(
        results,
        failures,
        expected_repository_count=len(repositories),
    )
    if status_message.startswith("PR retrieval and feedback-source collection are complete"):
        st.success(status_message)
    elif results or failures:
        st.warning(status_message)
    else:
        st.info(status_message)
    if aggregate.get("population_count") is None:
        st.metric("Portfolio PR population", "Unavailable / partial")
    else:
        st.metric("Portfolio PR population", aggregate["population_count"])
    st.caption(
        "Exact sum of repositories with complete pull-request pagination."
        if aggregate.get("population_status") == "complete"
        else "Partial sum of successful repositories; failed or incomplete repository population is unknown."
    )
    for name, metric in aggregate["metrics"].items():
        if metric.get("value") is None and metric.get("total_valid_commits") is None:
            st.caption(f"{METRIC_TITLES.get(name, name)} portfolio count: {metric['message']}")
        elif name == "commit_count":
            st.metric("Portfolio total valid commits", metric["total_valid_commits"])
            if metric["coverage_fraction"] is not None:
                coverage = f"{metric['coverage_fraction']:.0%} of portfolio PRs"
            else:
                coverage = "Portfolio coverage unavailable"
            st.caption(
                f"{metric.get('coverage_note', metric['message'])} {metric['denominator']} commit-covered PRs; "
                f"{coverage}; {metric['unknown_pr_count']} PRs unknown."
            )
        else:
            st.metric("Portfolio reopened events", metric["value"])
            st.caption(
                f"{metric['status'].capitalize()} exact sum across {metric['repositories']} repositories. {metric.get('coverage_note', '')} "
                f"{metric['numerator']} observed events / {metric['denominator']} "
                f"lifecycle-covered PRs; {metric['coverage_fraction']:.0%} covered"
                f" ({metric['unknown_pr_count']} PRs unknown)"
                if metric["coverage_fraction"] is not None
                else f"{metric['status'].capitalize()} exact sum; denominator {metric['denominator']}."
            )

    selected_metadata = {}
    for selected in repositories:
        if isinstance(selected, dict):
            try:
                _, _, label = repository_identity(selected)
                selected_metadata[label] = selected
            except Exception:
                continue
    st.subheader("Repository results")
    for label, (envelope, analysis) in results.items():
        metadata = selected_metadata.get(label, {})
        with st.expander(f"{label} · {envelope.get('status', 'unknown')}"):
            metadata_caption = repository_metadata_caption(metadata)
            if metadata_caption:
                st.caption(metadata_caption)
            st.caption("Duration and PR-size medians are reported per repository; no portfolio median is calculated.")
            render_result(envelope, analysis, comment_author=comment_author)
    for label, message in failures.items():
        st.error(f"{label}: collection failed. {message}")


def render_metric(metric: dict[str, Any]) -> None:
    view = present_metric(metric)
    with st.container(border=True):
        st.subheader(view["title"])
        st.markdown(f"**{view['value']}**")
        st.caption(view["status"])
        st.caption(f"{view['denominator']} · {view['coverage']}")
        st.caption(f"Definition: {view['definition']}")
        if view["unknown_pr_count"] is not None:
            st.caption(f"PRs without complete eligible coverage: {view['unknown_pr_count']}")
        invalid_counts = [f"{name.replace('_', ' ')}: {count}" for name, count in view["invalid_counts"].items() if count]
        if invalid_counts:
            st.caption("Excluded/unknown evidence — " + "; ".join(invalid_counts))
        evidence = view["evidence"]
        links = []
        for item in evidence[:5]:
            if not isinstance(item, dict):
                continue
            link = item.get("url") or item.get("source_url")
            number = item.get("number")
            if safe_https_url(link):
                links.append((f"PR #{number}" if number is not None else "Source evidence", link))
        if links:
            st.caption("Evidence links (up to 5 shown):")
            link_columns = st.columns(min(len(links), 5))
            for column, (label, url) in zip(link_columns, links):
                column.link_button(label, url)


def render_explorer(
    envelope: dict[str, Any],
    analysis: dict[str, Any] | None = None,
) -> None:
    st.subheader("PR explorer")
    rows = prepare_pr_explorer(
        envelope.get("pull_requests"),
        envelope.get("pull_request_resources"),
    )
    if not rows:
        st.info("No PRs matched the selected repository, date window, and author filter.")
        return
    search = st.text_input(
        "Search PR number, title, or author",
        key="pr_explorer_search",
        help="Search uses only non-body PR metadata; comment text and PR bodies are not displayed.",
    ).strip().casefold()
    states = sorted({str(row["State"]) for row in rows})
    selected_states = st.multiselect("Filter by PR state", states, default=states)
    filtered = [
        row for row in rows
        if row["State"] in selected_states
        and (
            not search
            or search in str(row["Number"]).casefold()
            or search in str(row["Title"]).casefold()
            or search in str(row["Author"]).casefold()
        )
    ]
    if not filtered:
        st.info("No PRs match the current explorer search and state filters.")
        return
    st.dataframe(
        filtered,
        hide_index=True,
        column_config={
            "PR": st.column_config.LinkColumn("PR link", display_text="Open PR"),
            "Files changed": st.column_config.LinkColumn(
                "Changed files", display_text="View files on GitHub"
            ),
        },
        use_container_width=True,
    )
    selected_labels = [
        f"#{row['Number']} · {row['Title']}" for row in filtered
    ]
    selected_label = st.selectbox(
        "Open pull request details",
        selected_labels,
        key="selected_pr_detail",
        help="Details are limited to collected metadata; PR bodies are not shown.",
    )
    selected_row = filtered[selected_labels.index(selected_label)]
    pr_number = selected_row["Number"]
    st.subheader(f"PR #{selected_row['Number']} details")
    details = st.columns(3)
    for column, label, value in zip(
        details,
        ("Author", "State", "Created"),
        (selected_row["Author"], selected_row["State"], selected_row["Created"]),
    ):
        with column:
            with st.container(border=True):
                st.caption(label)
                st.write(value)
    details = st.columns(3)
    for column, label, value in zip(
        details,
        ("Additions", "Deletions", "Changed files"),
        (selected_row["Additions"], selected_row["Deletions"], selected_row["Changed files"]),
    ):
        with column:
            with st.container(border=True):
                st.caption(label)
                st.write(value)
    st.caption(f"PR size detail endpoint: {selected_row['Size details']}.")
    if selected_row["PR"]:
        st.link_button("Open pull request on GitHub", selected_row["PR"])
    if selected_row["Files changed"]:
        st.link_button("View changed files", selected_row["Files changed"])
    selected_pr = next(
        (
            pr for pr in envelope.get("pull_requests", [])
            if isinstance(pr, dict)
            and str(pr.get("number")) == str(pr_number)
        ),
        {},
    )
    per_pr = (envelope.get("pull_request_resources") or {}).get(str(selected_pr.get("id")), {})
    st.markdown("**Review-feedback source coverage**")
    feedback_links = []
    for resource, label in (
        ("reviews", "Submitted reviews"),
        ("review_comments", "Inline review comments"),
        ("issue_comments", "PR conversation comments"),
    ):
        record = per_pr.get(resource) if isinstance(per_pr, dict) else None
        if not isinstance(record, dict):
            st.caption(f"{label}: unavailable / not requested.")
            continue
        count = record.get("count")
        st.caption(
            f"{label}: {record.get('status', 'unknown')} · "
            f"{count if count is not None else 'count unavailable'} records."
        )
        for source in envelope.get(resource, []):
            if (
                isinstance(source, dict)
                and str(source.get("pull_request_id")) == str(selected_pr.get("id"))
            ):
                url = source.get("url") or source.get("source_url")
                if safe_https_url(url):
                    feedback_links.append((label, url))
    for index, (label, url) in enumerate(feedback_links[:5], start=1):
        st.link_button(f"{label} source {index}", url)
    if analysis:
        st.markdown("**Available lifecycle evidence for this PR**")
        lifecycle_keys = (
            "first_submitted_review_latency",
            "merge_latency",
            "reopened_event_count",
            "commit_count",
            "adjacent_activity_gap",
        )
        evidence_found = False
        for metric_key in lifecycle_keys:
            metric = (analysis.get("metrics") or {}).get(metric_key) or {}
            matches = [
                item for item in metric.get("evidence", [])
                if isinstance(item, dict)
                and str(item.get("number")) == str(pr_number)
            ]
            if not matches:
                continue
            evidence_found = True
            st.caption(f"{METRIC_TITLES.get(metric_key, metric_key)} · {metric.get('definition', 'Definition unavailable.')}")
            for item in matches[:5]:
                timestamps = [
                    f"{field.replace('_', ' ')}: {item[field]}"
                    for field in (
                        "created_at", "submitted_at", "merged_at",
                        "occurred_at", "committed_at", "first_at", "second_at",
                    )
                    if item.get(field)
                ]
                st.write(" · ".join(timestamps) if timestamps else "Evidence record available; event timestamp unavailable.")
                source = item.get("url") or item.get("source_url")
                if safe_https_url(source):
                    st.link_button(f"Open {METRIC_TITLES.get(metric_key, 'source')} evidence", source)
        if not evidence_found:
            st.info("No eligible lifecycle evidence records are available for this PR; this is not evidence of no activity.")


def comment_author_options(envelope: dict[str, Any]) -> list[str]:
    """Return author logins present in the selected PR population."""
    authors_by_key: dict[str, str] = {}
    for author in (
        pr.get("author")
        for pr in envelope.get("pull_requests", [])
        if isinstance(pr, dict)
    ):
        if (
            isinstance(author, dict)
            and isinstance(author.get("login"), str)
            and author["login"].strip()
        ):
            login = author["login"].strip()
            authors_by_key.setdefault(login.casefold(), login)
    return sorted(authors_by_key.values(), key=str.casefold)


def build_comment_matrix(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build observed per-PR category counts without implying full coverage."""
    categories = [
        *report["taxonomy"]["rules"],
        report["taxonomy"]["unclassified_category"],
    ]
    inventory_by_pr: dict[str, list[dict[str, Any]]] = {}
    for item in report["comment_inventory"]:
        if (
            item["author_role"] == "reviewer"
            and item["included_in_observed_activity"]
        ):
            inventory_by_pr.setdefault(str(item["pull_request_id"]), []).append(item)

    rows = []
    for pr in report["population"]["pull_request_evidence"]:
        pr_id = str(pr["pull_request_id"])
        coverage = report["per_pull_request_coverage"].get(pr_id, {})
        complete = coverage.get("all_comment_sources_complete") is True
        comments = inventory_by_pr.get(pr_id, [])
        row: dict[str, Any] = {
            "PR": pr.get("pull_request_number"),
            "Title": pr.get("title") or "(untitled)",
            "Observed reviewer feedback items": len(comments),
            "History coverage": "Complete" if complete else "Partial / unavailable",
            "PR link": pr.get("url") if safe_https_url(pr.get("url")) else None,
        }
        for category in categories:
            column = category.replace("_", " ").title()
            row[column] = sum(category in item["categories"] for item in comments)
        rows.append(row)
    return rows


def render_comment_analysis(
    envelope: dict[str, Any],
    *,
    target_author: str | None = None,
) -> None:
    """Render local, evidence-linked comment inventory and rule-based themes."""
    st.subheader("Review comment analysis")
    st.caption(
        "Includes submitted review summaries, inline review comments, and PR conversation comments. "
        "Comment text stays in this local app; no external AI service is used. "
        "Category labels are keyword signals for human review, not definitive judgments."
    )
    configured_author = (envelope.get("filters") or {}).get("author")
    authors = comment_author_options(envelope)
    if target_author is None and isinstance(configured_author, str) and configured_author.strip():
        target_author = configured_author.strip()
        selected_authors = {login.casefold() for login in parse_author_logins(target_author)}
        available_authors = {author.casefold() for author in authors}
        if selected_authors.isdisjoint(available_authors):
            st.info(f"No PRs by the selected author(s) {target_author} are present in this collection.")
            return
    elif target_author is None and not authors:
        st.info("No PR authors are available for comment analysis.")
        return
    elif target_author is None:
        target_author = st.selectbox(
            "Analyze feedback on PRs authored by",
            authors,
            key=f"comment_analysis_author_{(envelope.get('repository') or {}).get('id', 'repo')}",
        )
    st.caption(
        f"Feedback written by known non-authors on PRs authored by {target_author}; "
        "the PR author's replies and comments with unknown identity are shown separately."
    )

    try:
        report = analyze_comments(envelope, target_author)
    except CommentAnalysisInputError:
        st.error("Comment analysis is unavailable because the collected data does not satisfy its evidence contract.")
        return

    coverage = report["resource_coverage"]
    coverage_rows = []
    for resource, title in (
        ("reviews", "Submitted review summaries"),
        ("review_comments", "Inline review comments"),
        ("issue_comments", "PR conversation comments"),
    ):
        data = coverage[resource]
        status_counts = data["pr_count_by_status"]
        coverage_rows.append({
            "Source": title,
            "Complete PR histories": data["complete_pr_count"],
            "Successful empty PR histories": data["successful_empty_pr_count"],
            "Partial PR histories": status_counts.get("partial", 0),
            "Unavailable PR histories": status_counts.get("unavailable", 0)
                + status_counts.get("error", 0),
            "Not requested": status_counts.get("not_requested", 0),
            "Complete for population": data["population_complete"],
        })
    st.dataframe(
        coverage_rows,
        hide_index=True,
        use_container_width=True,
        alt="Comment-source completeness for target-authored pull requests",
    )
    if not report["data_quality"]["comment_resource_coverage_complete_for_population"]:
        st.warning(
            "Comment history is incomplete for some PRs. Category counts below describe observed "
            "comments only; missing or inaccessible comments are not treated as zero."
        )

    activity = report["comment_activity"]
    st.write(
        f"PRs analyzed: {report['population']['pull_request_count']} · "
        f"Observed reviewer feedback items: {activity['observed_reviewer_comment_count']} · "
        f"Author responses: {activity['observed_author_response_count']} · "
        f"Unknown commenter identity: {activity['observed_unknown_author_count']}"
    )
    st.caption(
        f"Window: {report['creation_window']['from']} to {report['creation_window']['to']} "
        f"(PR creation, inclusive); activity cutoff: {report['observation_cutoff']}."
    )
    if activity["comments_missing_or_invalid_timestamp"]:
        st.caption(
            f"{activity['comments_missing_or_invalid_timestamp']} comments had missing/invalid "
            "timestamps or predated PR creation; those items were not categorized."
        )
    if activity["comments_after_observation_cutoff"]:
        st.caption(
            f"{activity['comments_after_observation_cutoff']} comments were after the observation "
            "cutoff and were not included."
        )

    st.markdown("**PR-by-feedback category matrix**")
    st.caption(
        "Cells show observed rule signals per PR. A zero means no matching signal was observed, "
        "not necessarily no feedback, unless that row's three comment-source histories are complete."
    )
    matrix = build_comment_matrix(report)
    if matrix:
        st.dataframe(
            matrix,
            hide_index=True,
            use_container_width=True,
            column_config={
                "PR link": st.column_config.LinkColumn("PR link", display_text="Open PR"),
            },
            alt="Observed reviewer feedback category counts by pull request",
        )

    st.markdown("**Feedback category signals**")
    category_rows = []
    for category, summary in report["categories"].items():
        recurrence = summary["recurrence_by_pr"]
        category_rows.append({
            "Category": category.replace("_", " ").title(),
            "Comments": summary["comment_count"],
            "Affected PRs": summary["affected_pr_count"],
            "Share of observed reviewer feedback (%)": summary["percentage"]["value"],
            "Affected fully covered PRs": recurrence["affected_pr_count"],
            "Recurrence denominator (covered PRs)": recurrence["denominator"],
        })
    st.dataframe(
        category_rows,
        hide_index=True,
        use_container_width=True,
        alt="Rule-based feedback categories and their evidence denominators",
    )
    st.caption(
        "Categories can overlap, so percentages across categories may add up to more than 100%. "
        "Recurrence means a category was signaled on at least two distinct PRs; the denominator "
        "includes only PRs with complete submitted-review, inline-comment, and conversation history."
    )

    st.markdown("**Comment evidence**")
    inventory = report["comment_inventory"]
    if not inventory:
        if report["data_quality"]["comment_resource_coverage_complete_for_population"]:
            st.info("No comments or review summaries were returned for the fully covered PR population.")
        else:
            st.info("No eligible comment evidence is available; check the completeness table above.")
        return
    reviewers = sorted({
        item["author_login"]
        for item in inventory
        if item.get("author_role") == "reviewer"
        and isinstance(item.get("author_login"), str)
        and item["author_login"].strip()
    }, key=str.casefold)
    reviewer_filter = st.selectbox(
        "Reviewer evidence filter",
        ["All reviewers", *reviewers],
        help="Filters the evidence list only; category totals and coverage above include all observed reviewers.",
    )
    visible_inventory = [
        item for item in inventory
        if reviewer_filter == "All reviewers"
        or item.get("author_role") != "reviewer"
        or item.get("author_login") == reviewer_filter
    ]
    st.caption("Reviewer evidence filter changes the detail list only; aggregate signals above remain unfiltered.")
    for item in visible_inventory:
        role = item["author_role"].replace("_", " ")
        categories = ", ".join(category.replace("_", " ") for category in item["categories"])
        title = (
            f"PR #{item['pull_request_number']} · {role} · "
            f"{item['comment_source'].replace('_', ' ')} · {item['created_at'] or 'timestamp unknown'}"
        )
        with st.expander(title):
            st.write(f"PR: {item['pull_request_title'] or '(untitled)'}")
            st.write(f"Commenter: {item['author_login'] or 'Unknown'}")
            if item.get("review_state"):
                st.write(f"Review state: {item['review_state']}")
            if item["path"]:
                location = item["path"]
                line = item.get("line") or item.get("original_line")
                st.write(f"File: {location}" + (f" · line {line}" if line is not None else ""))
            st.write(f"Category signals: {categories or 'Not categorized'}")
            st.write(f"Addressed/resolved: {item['addressed_status']}")
            if item.get("comment_text"):
                st.text(item["comment_text"])
            else:
                st.caption("Comment text is unavailable or could not be placed within the observation window.")
            links = []
            if safe_https_url(item.get("pull_request_url")):
                links.append(("Open PR", item["pull_request_url"]))
            if safe_https_url(item.get("comment_url")):
                links.append(("Open comment", item["comment_url"]))
            for label, url in links:
                st.link_button(label, url)


def render_result(
    envelope: dict[str, Any],
    analysis: dict[str, Any],
    *,
    include_explorer: bool = True,
    include_people: bool = True,
    comment_author: str | None = None,
) -> None:
    status = envelope.get("status", "unavailable")
    if status == "partial":
        st.warning("Partial data: some requested records or history are unavailable. Values are limited to eligible evidence.")
    elif status == "unavailable":
        st.warning("Data unavailable. Missing data is not treated as zero.")
    else:
        st.success("Collection completed. Review each metric's denominator and coverage before interpretation.")
    repository = envelope.get("repository") or {}
    st.caption(
        f"Repository: {repository.get('owner', 'unknown')}/{repository.get('name', 'unknown')} · "
        f"Window: {(envelope.get('requested_window') or {}).get('from', '?')} – "
        f"{(envelope.get('requested_window') or {}).get('to', '?')} · "
        f"Author filter: {(envelope.get('filters') or {}).get('author') or 'all authors'} · "
        f"Observed through: {envelope.get('observation_cutoff', 'unknown')}"
    )
    warnings = envelope.get("warnings") or []
    if warnings:
        st.info(
            f"Collection reported {len(warnings)} warning(s). Detailed messages are omitted to avoid "
            "displaying sensitive server or repository data."
        )
    population = analysis.get("population_count", 0)
    st.metric("PRs in selected population", population)
    if population == 0:
        st.info("Collection succeeded, but no PRs matched the selected filters.")

    metrics = analysis.get("metrics") or {}
    metric_order = (
        "pr_size",
        "first_submitted_review_latency",
        "merge_latency",
        "reopened_event_count",
        "commit_count",
        "adjacent_activity_gap",
    )
    for offset in range(0, len(metric_order), 2):
        columns = st.columns(2)
        for column, key in zip(columns, metric_order[offset:offset + 2]):
            if key in metrics:
                with column:
                    render_metric(metrics[key])
    if include_explorer:
        render_explorer(envelope, analysis)
    if include_people:
        render_comment_analysis(envelope, target_author=comment_author)


def _selected_result(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    key: str,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    if not results:
        return None
    labels = list(results)
    default = st.session_state.get(key)
    if default not in labels:
        default = labels[0]
    label = st.selectbox(
        "Repository",
        labels,
        index=labels.index(default),
        key=key,
        help="Choose a repository from the successful results in this collection.",
    )
    envelope, analysis = results[label]
    return label, envelope, analysis


def _page_header(title: str, description: str) -> None:
    st.header(title)
    st.caption(description)


def build_portfolio_overview(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str],
) -> dict[str, Any] | None:
    """Build state and monthly activity charts only from complete PR populations."""
    aggregate = aggregate_portfolio_metrics(results, failures)
    if failures:
        return None

    outcomes = {"Merged": 0, "Closed unmerged": 0, "Open": 0}
    monthly: dict[str, dict[str, int]] = {}
    creation_months: set[str] = set()
    observed = 0

    def month_for(timestamp: Any) -> str | None:
        if not isinstance(timestamp, str) or not timestamp.strip():
            return None
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-01")

    for envelope, analysis in results.values():
        pull_requests = envelope.get("pull_requests")
        resource = (envelope.get("resources") or {}).get("pull_requests") or {}
        if (
            not isinstance(pull_requests, list)
            or resource.get("status") != "complete"
            or resource.get("pagination_complete") is not True
            or resource.get("count") != len(pull_requests)
            or len(pull_requests) != analysis.get("population_count")
        ):
            return None

        for pr in pull_requests:
            if not isinstance(pr, dict):
                return None
            created_month = month_for(pr.get("created_at"))
            if created_month is None:
                return None
            monthly.setdefault(
                created_month, {"Created": 0, "Merged": 0}
            )["Created"] += 1
            creation_months.add(created_month)
            observed += 1

            merged_at = pr.get("merged_at")
            state = str(pr.get("state") or "").casefold()
            if merged_at:
                merged_month = month_for(merged_at)
                if merged_month is None:
                    return None
                outcomes["Merged"] += 1
                monthly.setdefault(
                    merged_month, {"Created": 0, "Merged": 0}
                )["Merged"] += 1
            elif state == "open":
                outcomes["Open"] += 1
            elif state == "closed":
                outcomes["Closed unmerged"] += 1
            else:
                return None

    if observed != aggregate.get("population_count"):
        return None
    trend = [
        {"month": month, "series": series, "count": counts[series]}
        for month, counts in sorted(monthly.items())
        for series in ("Created", "Merged")
    ]
    return {
        "outcomes": outcomes,
        "trend": trend,
        "population_count": observed,
        "activity_trend_month_count": len(creation_months),
    }


def collection_status_message(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str],
    *,
    expected_repository_count: int | None = None,
) -> str:
    """Describe PR retrieval separately from feedback-source coverage."""
    expected = expected_repository_count or len(results) + len(failures)
    if not results:
        return "PR retrieval failed: no repository data was retrieved."

    pr_listing_complete = not failures and len(results) == expected
    incomplete_prs = []
    incomplete_sources: set[str] = set()
    for label, (envelope, _analysis) in results.items():
        resources = envelope.get("resources") or {}
        pr_resource = resources.get("pull_requests") or {}
        pull_requests = envelope.get("pull_requests")
        if (
            pr_resource.get("status") != "complete"
            or pr_resource.get("pagination_complete") is not True
            or not isinstance(pull_requests, list)
            or pr_resource.get("count") != len(pull_requests)
        ):
            pr_listing_complete = False
            incomplete_prs.append(label)
        for source in ("reviews", "review_comments", "issue_comments"):
            source_resource = resources.get(source) or {}
            if (
                source_resource.get("status") != "complete"
                or source_resource.get("pagination_complete") is not True
            ):
                incomplete_sources.add(source)

    if not pr_listing_complete:
        details = []
        if failures:
            details.append(f"{len(failures)} repository collection failure(s)")
        if incomplete_prs:
            details.append("incomplete PR listing or pagination for " + ", ".join(incomplete_prs))
        if len(results) != expected and not failures:
            details.append(f"retrieved {len(results)} of {expected} selected repositories")
        return "PR retrieval is partial: " + "; ".join(details or ["coverage is incomplete"]) + "."

    if incomplete_sources:
        labels = {
            "reviews": "submitted reviews",
            "review_comments": "inline comments",
            "issue_comments": "PR conversation comments",
        }
        sources = ", ".join(labels[source] for source in sorted(incomplete_sources))
        return (
            "PR retrieval is complete; feedback-source coverage is partial or unavailable "
            f"for {sources}."
        )
    return "PR retrieval and feedback-source collection are complete."


def portfolio_overview_unavailable_reason(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str],
) -> str:
    if not results and not failures:
        return "Apply the repository, author, and date filters to load PR outcomes."
    if failures:
        return "Cross-repository outcomes are incomplete because collection failed for: " + ", ".join(failures) + "."
    for label, (envelope, analysis) in results.items():
        resource = (envelope.get("resources") or {}).get("pull_requests") or {}
        prs = envelope.get("pull_requests")
        if resource.get("status") != "complete":
            return f"{label}: PR listing status is {resource.get('status', 'unknown')}."
        if resource.get("pagination_complete") is not True:
            return f"{label}: PR pagination is incomplete."
        if not isinstance(prs, list) or resource.get("count") != len(prs):
            return f"{label}: declared PR count does not match returned PR records."
        if len(prs) != analysis.get("population_count"):
            return f"{label}: returned PR count does not match the analyzed population."
        for pr in prs:
            if not isinstance(pr, dict) or not isinstance(pr.get("created_at"), str):
                return f"{label}: a PR creation timestamp is missing or invalid."
            try:
                parsed = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError:
                return f"{label}: a PR creation timestamp is invalid or lacks a timezone."
            merged_at = pr.get("merged_at")
            if merged_at:
                try:
                    parsed_merge = datetime.fromisoformat(str(merged_at).replace("Z", "+00:00"))
                    if parsed_merge.tzinfo is None:
                        raise ValueError
                except ValueError:
                    return f"{label}: a merge timestamp is invalid or lacks a timezone."
    return "PR outcome data did not satisfy the population and timestamp contract."


def portfolio_kpi_rows(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str],
    action_plan: dict[str, Any] | None = None,
) -> list[tuple[str, Any, str]]:
    """Build only supported overview cards, preserving unknown and partial states."""
    overview = build_portfolio_overview(results, failures)
    if overview:
        population = overview["population_count"]
        merged = overview["outcomes"]["Merged"]
        merge_rate: Any = f"{merged / population:.0%}" if population else "N/A"
        state_note = "Complete selected PR population"
        state_kpis: list[tuple[str, Any, str]] = [
            ("Selected PRs", population, state_note),
            ("Merged PRs", merged, "Current merge timestamp observed"),
            ("Merge rate", merge_rate, "Merged / selected PRs"),
            ("Open PRs", overview["outcomes"]["Open"], "Current PR state"),
            ("Closed unmerged", overview["outcomes"]["Closed unmerged"], "No merge timestamp"),
        ]
    else:
        state_kpis = [("Selected PRs", "Partial / unavailable", portfolio_overview_unavailable_reason(results, failures))]

    plan = action_plan or {}
    if plan:
        state_kpis.extend([
            ("Reviewer feedback", plan.get("reviewer_feedback_count", "Unavailable"), "Observed, deduplicated items; author replies excluded"),
            ("Prioritized concerns", len(plan.get("recommendations", [])), "Deterministic keyword signals; not confirmed defects"),
        ])
    if len(results) == 1:
        _label, (_envelope, analysis) = next(iter(results.items()))
        metric_labels = (
            ("first_submitted_review_latency", "Median time to first review"),
            ("merge_latency", "Median time to merge"),
        )
        for metric_key, label in metric_labels:
            metric = (analysis.get("metrics") or {}).get(metric_key, {})
            value = metric.get("value")
            if metric.get("status") in {"complete", "partial"} and isinstance(value, (int, float)):
                rendered_value: Any = f"{value / 3600:,.1f} h"
                note = f"Median elapsed UTC · {metric.get('numerator', 0)} / {metric.get('denominator', 0)} eligible PRs · {metric.get('status')}"
            else:
                rendered_value = "Unavailable"
                reason = metric.get("unavailable_reason") or metric.get("status") or "metric evidence unavailable"
                note = f"Reason: {reason}"
            state_kpis.append((label, rendered_value, note))
    if results:
        checks_complete = all(
            ((envelope.get("resources") or {}).get("checks") or {}).get("status") == "complete"
            for envelope, _ in results.values()
        ) and not failures
        observed_failures = sum(
            1 for envelope, _ in results.values() for check in envelope.get("checks", [])
            if isinstance(check, dict) and str(check.get("conclusion") or "").casefold()
            in {"failure", "timed_out", "action_required"}
        )
        state_kpis.append((
            "Failed current-head checks",
            observed_failures if checks_complete else f"{observed_failures} observed",
            "Complete current-head histories" if checks_complete else "Partial coverage; observed failures only",
        ))
    else:
        state_kpis.append(("Failed current-head checks", "Unavailable", "No collected check data"))
    return state_kpis


def render_portfolio_page(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str],
    repositories: list[dict[str, Any] | str],
    *,
    target_author: str | None = None,
) -> None:
    _page_header(
        "Portfolio Overview",
        "Repository scope, selected PR window, author feedback, outcomes, and supported event totals. Cross-repository medians are not inferred.",
    )
    windows = {(envelope.get("requested_window") or {}).get("from", "?")[:10] + " → " +
               (envelope.get("requested_window") or {}).get("to", "?")[:10]
               for envelope, _ in results.values()}
    scope_labels = list(results)
    if not scope_labels:
        for item in repositories:
            try:
                scope_labels.append(repository_identity(item)[2])
            except Exception:
                scope_labels.append("Invalid repository selection")
    scope = ", ".join(scope_labels)
    st.caption(f"Repositories: {scope or 'none selected'} · PR creation window: {', '.join(sorted(windows)) or 'not yet applied'}")
    overview = build_portfolio_overview(results, failures)
    outcomes = overview["outcomes"] if overview else {}
    population = overview["population_count"] if overview else None
    merge_rate = (
        f"{outcomes['Merged'] / population:.0%}"
        if population
        else ("N/A" if population == 0 else "Unavailable")
    )
    action_state = st.session_state.get("current_action_plan") or {}
    action_plan = action_state.get("plan") if action_state.get("collection_generation") == st.session_state.get("collection_generation", 0) else None
    kpis = [("Repositories", len(repositories), f"{len(results)} successful · {len(failures)} failed")]
    kpis.extend(portfolio_kpi_rows(results, failures, action_plan))
    for offset in range(0, len(kpis), 3):
        columns = st.columns(3)
        for column, (label, value, note) in zip(columns, kpis[offset:offset + 3]):
            with column:
                with st.container(border=True):
                    st.metric(label, value)
                    st.caption(note)
    if overview is None:
        st.info(portfolio_overview_unavailable_reason(results, failures))
    elif population == 0:
        st.info("No pull requests match the selected filters; outcome and activity charts are not applicable.")
    else:
        chart_columns = st.columns(2)
        with chart_columns[0]:
            with st.container(border=True):
                st.subheader("PR activity trend")
                if overview["activity_trend_month_count"] < 2:
                    st.info("A trend needs PRs created in at least two different months.")
                else:
                    st.caption("Created and merged events by month for this selected PR cohort.")
                    st.vega_lite_chart(
                        overview["trend"],
                        {
                            "title": "Created vs merged",
                            "mark": {"type": "line", "point": True},
                            "encoding": {
                                "x": {
                                    "field": "month", "type": "temporal", "title": "Month",
                                    "axis": {"format": "%b %Y", "labelAngle": -30},
                                },
                                "y": {"field": "count", "type": "quantitative", "title": "PRs"},
                                "color": {
                                    "field": "series",
                                    "type": "nominal",
                                    "scale": {
                                        "domain": ["Created", "Merged"],
                                        "range": ["#1877d2", "#2da44e"],
                                    },
                                    "legend": {"title": None},
                                },
                                "tooltip": [
                                    {"field": "month", "type": "temporal", "title": "Month", "format": "%b %Y"},
                                    {"field": "series", "type": "nominal", "title": "Event"},
                                    {"field": "count", "type": "quantitative", "title": "PRs"},
                                ],
                            },
                        },
                        width="stretch",
                        key="portfolio_activity_trend",
                    )
        with chart_columns[1]:
            with st.container(border=True):
                st.subheader("PR outcome distribution")
                st.vega_lite_chart(
                    [
                        {"outcome": label, "count": count}
                        for label, count in outcomes.items()
                    ],
                    {
                        "title": "Merged, closed unmerged, and open",
                        "mark": {"type": "arc", "innerRadius": 56, "stroke": "#ffffff"},
                        "encoding": {
                            "theta": {"field": "count", "type": "quantitative"},
                            "color": {
                                "field": "outcome",
                                "type": "nominal",
                                "scale": {
                                    "domain": ["Merged", "Closed unmerged", "Open"],
                                    "range": ["#2da44e", "#f0883e", "#1877d2"],
                                },
                                "legend": {"title": None, "labelLimit": 220},
                            },
                            "tooltip": [
                                {"field": "outcome", "type": "nominal", "title": "Outcome"},
                                {"field": "count", "type": "quantitative", "title": "PRs"},
                            ],
                        },
                    },
                    width="stretch",
                    key="portfolio_outcome_distribution",
                )
    render_portfolio(
        results, failures, repositories, comment_author=target_author
    )


def render_repository_page(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    target_author: str | None = None,
) -> None:
    _page_header(
        "Repository Analytics",
        "Lifecycle metrics from the selected repository, with source definitions, eligible denominators, and coverage.",
    )
    selection = _selected_result(results, key="repository_analytics_selection")
    if selection is None:
        st.info("No repository results are available yet. Select repositories and collect PR metrics.")
        return
    _label, envelope, analysis = selection
    render_result(
        envelope, analysis, include_explorer=False, include_people=False,
        comment_author=target_author,
    )
    render_latency_distributions(analysis)


def latency_distribution(metric: dict[str, Any], start_field: str, end_field: str) -> list[dict[str, Any]]:
    observations = []
    for item in metric.get("evidence", []):
        if not isinstance(item, dict):
            continue
        try:
            start = datetime.fromisoformat(str(item[start_field]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(item[end_field]).replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None or end < start:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        observations.append({"PR": item.get("number"), "elapsed_hours": (end - start).total_seconds() / 3600})
    return observations


def render_latency_distributions(analysis: dict[str, Any]) -> None:
    metrics = analysis.get("metrics") or {}
    chart_specs = (
        ("first_submitted_review_latency", "Time to first submitted review", "created_at", "submitted_at"),
        ("merge_latency", "Creation to merge", "created_at", "merged_at"),
    )
    available = []
    for key, title, start, end in chart_specs:
        metric = metrics.get(key, {})
        observations = latency_distribution(metric, start, end)
        if observations:
            available.append((title, observations, metric))
    if not available:
        st.info("No valid per-PR review or merge latency observations are available to plot.")
        return
    st.subheader("Review and merge timing distributions")
    st.caption("Each bar counts eligible PRs in an elapsed-hours bin. These are elapsed UTC durations, not business time.")
    columns = st.columns(len(available))
    for column, (title, observations, metric) in zip(columns, available):
        with column:
            st.subheader(title)
            st.caption(f"{len(observations)} eligible PR(s) · source status: {metric.get('status', 'unknown')}")
            st.vega_lite_chart(
                observations,
                {"mark": "bar", "encoding": {
                    "x": {"field": "elapsed_hours", "type": "quantitative", "bin": {"maxbins": 12}, "title": "Elapsed hours"},
                    "y": {"aggregate": "count", "type": "quantitative", "title": "Pull requests"},
                    "tooltip": [{"field": "elapsed_hours", "type": "quantitative", "title": "Hours"}],
                }},
                width="stretch",
                key=f"latency_distribution_{title.casefold().replace(' ', '_')}",
            )


def render_people_page(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    target_author: str | None = None,
) -> None:
    _page_header(
        "People Analytics",
        "Observed review feedback on selected-author PRs. Category signals are deterministic keyword rules, not semantic classification.",
    )
    selection = _selected_result(results, key="people_repository_selection")
    if selection is None:
        st.info("No repository results are available yet. Collect data to view observed feedback.")
        return
    _label, envelope, _analysis = selection
    st.info(
        "Feedback categories are deterministic keyword signals over collected review/comment text. "
        "They can miss context and are not sentiment, quality, or semantic judgments."
    )
    render_comment_analysis(envelope, target_author=target_author)


def render_explorer_page(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    _page_header(
        "PR Explorer",
        "Search and inspect collected pull-request metadata. Bodies and comment text are excluded from this view.",
    )
    selection = _selected_result(results, key="explorer_repository_selection")
    if selection is None:
        st.info("No repository results are available yet. Collect data to explore pull requests.")
        return
    _label, envelope, analysis = selection
    render_explorer(envelope, analysis)


def render_quality_page(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    _page_header(
        "Quality & Reliability",
        "Observed PR hygiene and current-head checks only. No quality score or flaky-test diagnosis is inferred.",
    )
    selection = _selected_result(results, key="quality_repository_selection")
    st.warning("Quality score: unavailable / not implemented.")
    if selection is None:
        st.info("No repository results are available to inspect.")
        return
    _label, envelope, analysis = selection
    details = ((envelope.get("resources") or {}).get("pull_request_details") or {})
    check_coverage = ((envelope.get("resources") or {}).get("checks") or {})
    checks = envelope.get("checks") if isinstance(envelope.get("checks"), list) else []
    st.subheader("CI / check-run evidence")
    if check_coverage.get("status") not in {"complete", "partial"}:
        st.info(f"Check-run data is {check_coverage.get('status', 'unavailable')}; no outcome is inferred.")
    else:
        conclusions: dict[str, int] = {}
        for check in checks:
            outcome = str(check.get("conclusion") or check.get("status") or "unknown").casefold()
            conclusions[outcome] = conclusions.get(outcome, 0) + 1
        failure_count = sum(conclusions.get(name, 0) for name in ("failure", "timed_out", "action_required"))
        check_columns = st.columns(3)
        with check_columns[0]:
            st.metric("Checks observed", check_coverage.get("count", len(checks)))
        with check_columns[1]:
            st.metric("Failed / action required", failure_count if check_coverage.get("status") == "complete" else f"{failure_count} observed")
        with check_columns[2]:
            st.metric("Cancelled", conclusions.get("cancelled", 0) if check_coverage.get("status") == "complete" else f"{conclusions.get('cancelled', 0)} observed")
        st.caption("Checks describe each PR's current head only. Repeated-run evidence is not collected, so flaky tests are not identified.")
        if checks:
            check_rows = [{
                "PR": check.get("pull_request_number"), "Check": check.get("name") or "(unnamed)",
                "Status": check.get("status") or "unknown", "Conclusion": check.get("conclusion") or "pending",
                "Started": check.get("started_at") or "Unavailable", "Completed": check.get("completed_at") or "Unavailable",
                "Source": check.get("url") if safe_https_url(check.get("url")) else None,
            } for check in checks]
            st.dataframe(check_rows, hide_index=True, use_container_width=True)
        elif check_coverage.get("status") == "complete":
            st.info("Check histories were completely collected and contained no check records.")
        else:
            st.warning("No check records were observed, but check collection is partial; this does not establish zero failures.")
    st.subheader("PR hygiene evidence")
    missing_titles = [pr.get("number") for pr in envelope.get("pull_requests", []) if not str(pr.get("title") or "").strip()]
    st.write(f"PRs with a missing title: {len(missing_titles)} / {analysis.get('population_count', 0)}")
    st.caption("This is a narrow observed hygiene signal. Repository-specific lint, test results, and repeated CI runs are not available in this collection.")
    st.subheader("Available source evidence")
    st.write(
        f"PR population: {analysis.get('population_count', 'Unavailable')} · "
        f"PR size details: {details.get('status', 'unavailable')} "
        f"({details.get('count') if details.get('count') is not None else 'count unavailable'} records)."
    )
    render_metric((analysis.get("metrics") or {}).get("pr_size", {
        "name": "pr_size",
        "status": "unavailable",
        "value": None,
        "unavailable_reason": "not_available",
        "definition": "PR size is based on GitHub-reported additions and deletions where available.",
    }))


def render_insights_page(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    target_author: str | None = None,
) -> None:
    _page_header(
        "Insights & Recommendations",
        "Prioritized reviewer-feedback signals and local evidence-grounded synthesis. Signals are not confirmed defects or performance scores.",
    )
    selection = _selected_result(results, key="insights_repository_selection")
    if selection is None:
        st.info("No repository results are available to summarize.")
        return
    _label, envelope, analysis = selection
    st.caption("The Author Improvement Action Plan and local PR summary are shown at the top of the dashboard. This page provides the detailed source taxonomy and comment evidence for the selected repository.")
    render_comment_analysis(envelope, target_author=target_author)


def _action_plan_author_options(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> list[str]:
    authors: dict[str, str] = {}
    for envelope, _ in results.values():
        for author in comment_author_options(envelope):
            authors.setdefault(author.casefold(), author)
    return sorted(authors.values(), key=str.casefold)


def _render_orchestrated_summaries(
    summaries: list[dict[str, Any]],
    orchestration: dict[str, Any],
    action_plan: dict[str, Any] | None = None,
) -> None:
    """Explain the action plan with concise model summary and reviewer patterns."""
    st.subheader("WHY THESE ACTIONS?")
    st.markdown("**PR and review summary**")
    if summaries:
        st.caption("Local Qwen2.5 summary of collected comments. Claims link to their source evidence.")
        for summary_index, summary in enumerate(summaries):
            evidence_by_id = {
                item.get("evidence_id"): item for item in summary.get("evidence", [])
                if isinstance(item, dict) and item.get("evidence_id")
            }
            context = summary.get("collection_context", {}).get("repositories", [])
            repository = context[0].get("repository", {}) if context else {}
            if len(summaries) > 1:
                st.caption(f"{repository.get('owner', 'Repository')}/{repository.get('name', '')}")
            themes = summary.get("themes", [])[:3]
            if not themes:
                st.info("Comments were collected, but the local model returned no supported summary.")
            for theme_index, theme in enumerate(themes):
                st.text(f"Review theme: {theme.get('theme', 'Review theme')}")
                st.text(theme.get("summary", ""))
                cited_by_claims: set[str] = set()
                for claim_index, claim in enumerate(theme.get("claims", [])):
                    st.caption(f"Model claim {claim_index + 1}")
                    st.text(claim.get("text", ""))
                    for evidence_index, evidence_id in enumerate(claim.get("evidence_ids", [])):
                        item = evidence_by_id.get(evidence_id)
                        if item:
                            cited_by_claims.add(evidence_id)
                            _render_summary_evidence(
                                item,
                                f"why_{summary_index}_{theme_index}_claim_{claim_index}_{evidence_index}",
                            )
                remaining_ids = [
                    evidence_id for evidence_id in theme.get("evidence_ids", [])
                    if evidence_id not in cited_by_claims
                ]
                for evidence_index, evidence_id in enumerate(remaining_ids):
                    item = evidence_by_id.get(evidence_id)
                    if item:
                        st.caption("Additional evidence for this theme")
                        _render_summary_evidence(
                            item,
                            f"why_{summary_index}_{theme_index}_theme_{evidence_index}",
                        )
        for run in orchestration.get("repository_runs", []):
            stage = run.get("status", {}).get("local_language_synthesis", {})
            if stage.get("status") in {"failed", "skipped"}:
                message = stage.get("message") or stage.get("reason") or "No diagnostic was returned."
                st.warning(f"Local summary for {run.get('repository', 'a selected repository')} {stage['status']}: {message}")
    else:
        statuses = [run.get("status", {}).get("local_language_synthesis", {})
                    for run in orchestration.get("repository_runs", [])]
        if not statuses:
            statuses = [orchestration.get("status", {}).get("local_language_synthesis", {})]
        failures = [item.get("message", "No diagnostic was returned.") for item in statuses if item.get("status") == "failed"]
        skipped = [item.get("reason") for item in statuses if item.get("status") == "skipped" and item.get("reason")]
        if failures:
            st.warning("Local summary unavailable: " + "; ".join(dict.fromkeys(failures)))
        elif skipped:
            st.info("Local summary skipped: " + "; ".join(dict.fromkeys(skipped)))
        elif orchestration:
            st.info("No local summary was produced for this collection.")
        else:
            st.info("Apply filters to collect PR and reviewer-comment evidence for a summary.")

    st.markdown("**Recurring reviewer concerns**")
    recommendations = (action_plan or {}).get("recommendations", [])[:3]
    if recommendations:
        for recommendation in recommendations:
            st.markdown(
                f"- {recommendation.get('focus', recommendation.get('title', 'Reviewer concern'))}: "
                f"{recommendation.get('affected_pr_count', 0)} distinct PR(s), "
                f"{recommendation.get('distinct_feedback_count', 0)} deduplicated feedback item(s)."
            )
        st.caption("Concerns are deterministic keyword-based signals in reviewer feedback, not confirmed defects.")
    elif action_plan:
        st.info("No recurring reviewer concern was supported by the collected feedback.")
    else:
        st.info("Reviewer concern patterns will appear after filters are applied.")

    if orchestration:
        with st.expander("Collection coverage and runtime status", expanded=False):
            render_orchestration_status(orchestration)

def _render_summary_evidence(item: dict[str, Any], key: str) -> None:
    repository = item.get("repository") or {}
    pr = item.get("pull_request") or {}
    role = str(item.get("commenter_role") or "unknown").replace("_", " ")
    source = str(item.get("source") or "comment").replace("_", " ")
    with st.expander(
        f"{role.title()} · {source.title()} · {repository.get('owner', '?')}/{repository.get('name', '?')} PR #{pr.get('number', '?')}"
    ):
        st.text(item.get("original_text") or "Original comment text unavailable.")
        st.caption(f"Timestamp: {item.get('timestamp_status', 'unknown')} · {item.get('timestamp') or 'unavailable'}")
        if safe_https_url(pr.get("url")):
            st.link_button("Open PR", pr["url"], key=f"{key}_pr")
        if safe_https_url(item.get("source_url")):
            st.link_button("Open source comment", item["source_url"], key=f"{key}_comment")


def _display_comment_excerpt(value: Any, *, limit: int = 240) -> str:
    """Show source text without converting truncation into different wording."""
    if not isinstance(value, str):
        return "Original comment text unavailable."
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    end = text.rfind(" ", 0, limit)
    if end < 1:
        end = limit
    return text[:end].rstrip(" ,;:") + "…"


def _render_local_comment_summary(
    reports: list[dict[str, Any]],
    target_author: str,
    *,
    failures: dict[str, str],
    report_errors: list[str],
) -> None:
    st.subheader("Local PR comment summary")
    st.caption(
        "One evidence-linked Qwen2.5 3B summary call through Ollama on 127.0.0.1. "
        "Comment text stays on this machine; no GitHub token is sent to the model."
    )
    cache_key = (
        st.session_state.get("collection_generation", 0),
        target_author.casefold(),
    )
    cached = st.session_state.get("local_comment_summary")
    retry = False
    if isinstance(cached, dict) and cached.get("key") == cache_key and cached.get("error"):
        retry = st.button(
            "Retry local summary",
            key=f"retry_local_comment_summary_{cache_key[0]}_{cache_key[1]}",
        )
    if not isinstance(cached, dict) or cached.get("key") != cache_key or retry:
        if not reports:
            cached = {
                "key": cache_key,
                "summary": None,
                "error": "No valid comment-analysis report was available.",
            }
        else:
            try:
                with st.status("Preparing local comment summary…", expanded=True) as summary_status:
                    def report_summary_progress(message: str) -> None:
                        summary_status.update(
                            label=message,
                            state="running",
                            expanded=True,
                        )

                    summary = summarize_comments(
                        reports,
                        progress_callback=report_summary_progress,
                    )
                    summary_status.update(
                        label="Local comment summary ready.",
                        state="complete",
                        expanded=False,
                    )
                cached = {"key": cache_key, "summary": summary, "error": None}
            except (LocalCommentAgentError, CommentSummaryContractError) as error:
                cached = {
                    "key": cache_key,
                    "summary": None,
                    "error": safe_error_message(error),
                }
        st.session_state["local_comment_summary"] = cached

    if cached.get("error"):
        st.error(cached["error"])
        st.caption("The collected PR metrics remain available. Retry the summary or load the data again.")
        return

    summary = cached["summary"]
    evidence = summary["evidence"]
    coverage = summary["source_coverage"]
    source_labels = {
        "reviews": "Submitted reviews",
        "review_comments": "Inline comments",
        "issue_comments": "PR conversation comments",
    }
    evidence_counts = {
        source: sum(1 for item in evidence if item["source"] == source)
        for source in source_labels
    }
    rows = []
    for source, label in source_labels.items():
        item = coverage[source]
        status = item["status"].replace("_", " ").title()
        if item["status"] == "not_applicable":
            status = "Not applicable — no PRs"
        rows.append({
            "Comment source": label,
            "Collection status": status,
            "Text-bearing comments summarized": evidence_counts[source],
            "Complete / selected PR histories": (
                f"{item['complete_pr_count']} / {item['population_pr_count']}"
            ),
            "Successfully empty PR histories": item["successful_empty_pr_count"],
            "Unavailable / partial / not requested / error": sum(
                count
                for state, count in item["pr_count_by_status"].items()
                if state != "complete"
            ),
        })
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        alt="Local comment summarization coverage by GitHub comment source",
    )
    role_counts = {
        role: sum(1 for item in evidence if item["commenter_role"] == role)
        for role in ("reviewer", "author_response", "unknown")
    }
    st.caption(
        f"Included text-bearing items by commenter: {role_counts['reviewer']} reviewer, "
        f"{role_counts['author_response']} PR-author response, "
        f"{role_counts['unknown']} unknown identity. "
        "Comments with missing, invalid, or post-cutoff timestamps are retained as evidence "
        "but are not used to infer ordering."
    )
    partial_coverage = any(
        item["status"] not in {"complete", "not_applicable"}
        for item in coverage.values()
    )
    population_incomplete = any(
        item["population_coverage"]["complete_for_target_selection"] is not True
        for item in summary["collection_context"]["repositories"]
    )
    if failures or report_errors or partial_coverage or population_incomplete:
        st.warning(
            "This is a partial summary of the collected comments. Source coverage applies to observed PRs only; "
            "an incomplete PR listing or comment history is not treated as empty."
        )

    if not evidence:
        if failures or report_errors or partial_coverage or population_incomplete:
            st.warning(
                "No text-bearing comments were available to summarize, but source coverage is incomplete; "
                "this does not establish that no comments exist."
            )
        elif all(item["status"] == "not_applicable" for item in coverage.values()):
            st.info("No PRs matched this author and date window, so there are no comments to summarize.")
        else:
            st.info("No text-bearing comments were returned by the completely retrieved sources.")
        return

    evidence_by_id = {item["evidence_id"]: item for item in evidence}

    def render_evidence(evidence_id: str, key_prefix: str) -> None:
        item = evidence_by_id[evidence_id]
        repository = item["repository"]
        pr = item["pull_request"]
        with st.container(border=True):
            st.caption(
                f"{repository['owner']}/{repository['name']} PR #{pr['number']} · "
                f"{item['source'].replace('_', ' ')} · "
                f"{item['commenter_role'].replace('_', ' ')}"
            )
            st.text(item["original_text"])
            st.caption(
                f"Timestamp status: {item['timestamp_status']} · "
                f"{item['timestamp'] or 'timestamp unavailable'}"
            )
            if safe_https_url(pr.get("url")):
                st.link_button(
                    "Open pull request",
                    pr["url"],
                    key=f"{key_prefix}_pr_{evidence_id}",
                )
            if safe_https_url(item.get("source_url")):
                st.link_button(
                    "Open source comment",
                    item["source_url"],
                    key=f"{key_prefix}_comment_{evidence_id}",
                )

    if not summary["themes"]:
        st.info(
            "The local model did not return a recurring theme. Review the collected evidence below; "
            "no finding has been invented."
        )
    for theme_index, theme in enumerate(summary["themes"]):
        with st.container(border=True):
            st.text(theme["theme"])
            st.text(theme["summary"])
            for claim_index, claim in enumerate(theme["claims"]):
                st.text(f"• {claim['text']}")
                for evidence_index, evidence_id in enumerate(claim["evidence_ids"]):
                    st.caption(f"Evidence for claim {claim_index + 1}")
                    render_evidence(
                        evidence_id,
                        f"local_summary_{theme_index}_{claim_index}_{evidence_index}",
                    )
            if theme["evidence_ids"]:
                st.caption("Supporting comments for this theme")
                for evidence_index, evidence_id in enumerate(theme["evidence_ids"]):
                    render_evidence(
                        evidence_id,
                        f"local_summary_{theme_index}_theme_{evidence_index}",
                    )

    with st.expander(f"All collected comment text ({len(evidence)} items)"):
        st.dataframe(
            [{
                "Repository": f"{item['repository']['owner']}/{item['repository']['name']}",
                "PR": f"#{item['pull_request']['number']}",
                "Commenter role": item["commenter_role"].replace("_", " "),
                "Source": item["source"].replace("_", " "),
                "Timestamp status": item["timestamp_status"],
                "Comment text": item["original_text"],
                "PR URL": item["pull_request"].get("url"),
                "Comment URL": item.get("source_url"),
            } for item in evidence],
            hide_index=True,
            width="stretch",
            alt="Every text-bearing PR comment sent to the local model",
        )


def _render_action_plan_coverage(plan: dict[str, Any]) -> None:
    rows = []
    labels = {
        "reviews": "Submitted review summaries",
        "review_comments": "Inline review comments",
        "issue_comments": "PR conversation comments",
    }
    for resource, label in labels.items():
        coverage = plan["coverage"].get(resource, {})
        statuses = coverage.get("pr_count_by_status")
        activity = plan.get("observed_activity_by_source", {}).get(resource, {})
        complete_count = coverage.get("complete_pr_count")
        selected_count = coverage.get("selected_pr_count")
        coverage_label = (
            "No PRs selected"
            if selected_count == 0
            else coverage.get("status", "unknown").replace("_", " ").title()
        )
        no_selected_prs = selected_count == 0
        rows.append({
            "Feedback source": label,
            "Coverage": coverage_label,
            "Reviewer feedback items": (
                "N/A" if no_selected_prs
                else activity.get("reviewer_feedback_count", "Unknown")
            ),
            "Author responses": (
                "N/A" if no_selected_prs
                else activity.get("author_response_count", "Unknown")
            ),
            "Unknown identities": (
                "N/A" if no_selected_prs
                else activity.get("unknown_identity_count", "Unknown")
            ),
            "Complete / selected histories": (
                f"{complete_count if complete_count is not None else 'Unknown'} / "
                f"{selected_count if selected_count is not None else 'Unknown'}"
            ),
            "Successfully empty histories": (
                coverage.get("successful_empty_pr_count")
                if coverage.get("successful_empty_pr_count") is not None
                else "Unknown"
            ),
            "Partial / unavailable / not requested": (
                sum(
                    count for status, count in statuses.items()
                    if status != "complete"
                )
                if isinstance(statuses, dict)
                else "Unknown"
            ),
        })
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        alt="Reviewer feedback and collection coverage by comment source",
    )
    st.caption(
        "Item counts are separated by commenter role; history counts are PR-level coverage, "
        "not comment totals. N/A item counts mean no PRs matched the selected author and date filters."
    )


def render_author_action_plan(
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str],
    *,
    collection_complete: bool,
    render_local_summary: bool = False,
    orchestration: dict[str, Any] | None = None,
) -> str | None:
    """Render a concise, evidence-linked next-PR action plan."""
    with st.container(key="next-pr-action-plan"):
        st.subheader("NEXT PR ACTION PLAN")
        if not collection_complete:
            st.info("Apply repository, author, and PR creation-date filters to build an evidence-backed plan.")
            return None
        if not results:
            st.info("No repository collection succeeded. The action plan is unavailable.")
            return None

        configured_authors = {
            str((envelope.get("filters") or {}).get("author")).strip()
            for envelope, _ in results.values()
            if isinstance((envelope.get("filters") or {}).get("author"), str)
            and (envelope.get("filters") or {}).get("author").strip()
        }
        if len({author.casefold() for author in configured_authors}) > 1:
            st.info("The action plan is unavailable because repositories use different author filters.")
            return None
        if configured_authors:
            target_author = next(iter(configured_authors))
        else:
            authors = _action_plan_author_options(results)
            if not authors:
                st.info("No PR authors are available in the selected scope.")
                return None
            target_author = st.selectbox("Author for this action plan", authors, key="action_plan_author")

        reports = []
        report_errors = []
        with st.spinner(f"Analyzing reviewer feedback for {target_author}?"):
            if orchestration and orchestration.get("comment_reports"):
                reports = orchestration["comment_reports"]
            elif orchestration and orchestration.get("analytics", {}).get("comment_report"):
                reports = [orchestration["analytics"]["comment_report"]]
            else:
                for repository, (envelope, _) in results.items():
                    try:
                        reports.append(analyze_comments(envelope, target_author))
                    except CommentAnalysisInputError:
                        report_errors.append(repository)
            if reports:
                try:
                    plan = (
                        orchestration.get("action_plan")
                        if orchestration and orchestration.get("action_plan")
                        else orchestration["analytics"].get("action_plan")
                        if orchestration and orchestration.get("analytics", {}).get("action_plan")
                        else build_action_plan(reports)
                    )
                except ActionPlanInputError:
                    st.info("The action plan is unavailable because comment evidence failed validation.")
                    return target_author
            else:
                plan = None

        if render_local_summary and reports and not orchestration:
            _render_local_comment_summary(
                reports, target_author, failures=failures, report_errors=report_errors
            )
        if failures or report_errors:
            affected = ", ".join([*failures.keys(), *report_errors])
            st.caption(
                "Partial collection" + (f" for {affected}" if affected else "")
                + ": recommendations cover successful evidence only; failed repositories and sources are not treated as zero."
            )
        if plan is None:
            st.info("No valid reviewer feedback report is available for this author.")
            return target_author
        if plan["status"].startswith("incomplete"):
            st.caption("Partial feedback coverage: actions describe observed evidence only; missing sources are not treated as zero.")

        st.caption(
            f"{plan['reviewer_feedback_count']} observed reviewer feedback item(s) across "
            f"{plan['population_pr_count'] if plan['population_pr_count'] is not None else 'an unknown number of'} selected PRs."
        )
        recommendations = plan.get("recommendations", [])[:3]
        if not recommendations:
            if plan["status"] == "no_feedback_found":
                response_note = (
                    f" {plan['author_response_count']} author response(s) were kept separate."
                    if plan.get("author_response_count") else ""
                )
                st.info("No reviewer feedback supported an actionable recurring concern." + response_note)
            elif plan["reviewer_feedback_count"]:
                st.info("Reviewer feedback was observed, but it did not support a recurring action in the available categories.")
            else:
                st.info("No reviewer feedback was available to support an action.")
        for index, recommendation in enumerate(recommendations, 1):
            st.markdown(f"**Action {index}:** {recommendation['action']}")
            why = recommendation.get("what_reviewers_flagged") or recommendation.get("focus", "Reviewer feedback")
            # The full source excerpts are shown below as plain text. Keep this
            # explanation to the deterministic rule and counts only.
            why = why.split(" Reviewer wording:", 1)[0].rstrip()
            st.markdown(f"**Why:** {why}")
            st.markdown(
                f"**Evidence:** {recommendation['affected_pr_count']} distinct PR(s); "
                f"{recommendation['distinct_feedback_count']} deduplicated reviewer item(s)."
            )
            links = []
            seen_urls = set()
            excerpt_count = 0
            for item in recommendation.get("evidence", []):
                pr_number = item.get("pull_request_number")
                if excerpt_count < 3:
                    excerpt = _display_comment_excerpt(item.get("comment_text"))
                    if excerpt:
                        st.caption(f"Reviewer comment excerpt · PR #{pr_number}")
                        st.text(excerpt)
                    excerpt_count += 1
                for label, url in (
                    (f"PR #{pr_number}", item.get("pull_request_url")),
                    ("comment", item.get("comment_url")),
                ):
                    if safe_https_url(url) and url not in seen_urls:
                        links.append(f"[{label}]({url})")
                        seen_urls.add(url)
            if links:
                st.markdown(" | ".join(links[:6]))
            validation = recommendation.get("validation", "")
            validation = _display_comment_excerpt(validation, limit=180)
            st.markdown(f"**Validation:** {validation}")
        st.caption("Recommendations use deterministic keyword signals in reviewer comments; they are not confirmed defects or performance scores.")
        with st.expander("Feedback-source coverage", expanded=False):
            _render_action_plan_coverage(plan)

        try:
            st.session_state["current_action_plan"] = {
                "collection_generation": st.session_state.get("collection_generation", 0),
                "target_author": target_author,
                "plan": plan,
            }
        except Exception:
            pass
        return target_author

def render_orchestration_status(result: dict[str, Any]) -> None:
    """Show specialist execution and evidence-source coverage after the plan."""
    st.subheader("Feedback-source coverage and runtime agent status")
    labels = {
        "github_data": "GitHub Data Agent",
        "pr_analytics_review_intelligence": "PR Analytics and Review Intelligence Agent",
        "code_quality_test_reliability": "Code Quality and Test Reliability Agent",
        "dashboard_ui_ux": "Dashboard UI and User Experience Agent",
        "local_language_synthesis": "Local Qwen2.5 3B synthesis",
        "validation": "Validation Agent",
    }
    runs = result.get("repository_runs") or [{"repository": "Selected repository", "status": result.get("status", {})}]
    stage_rows = [{"Repository": run.get("repository"), "Runtime stage": labels.get(key, key),
                   "Status": value.get("status", "unknown"),
                   "Details": value.get("message") or value.get("reason") or ""}
                  for run in runs for key, value in run.get("status", {}).items()]
    if stage_rows:
        st.dataframe(stage_rows, hide_index=True, use_container_width=True)
    reports = result.get("comment_reports") or ([result.get("analytics", {}).get("comment_report")] if result.get("analytics", {}).get("comment_report") else [])
    if reports:
        rows = [{"Repository": repository, "Feedback source": source.replace("_", " ").title(),
                 "Complete PR histories": coverage.get("complete_pr_count"),
                 "Successful empty histories": coverage.get("successful_empty_pr_count"),
                 "Selected PR histories": coverage.get("population_pr_count"),
                 "Coverage": coverage.get("status")}
                for run in runs for repository, report in [(run.get("repository"), run.get("comment_report"))]
                if report for source, coverage in report.get("resource_coverage", {}).items()]
        st.dataframe(rows, hide_index=True, use_container_width=True)


def render_page(
    page: str,
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    failures: dict[str, str],
    repositories: list[dict[str, Any] | str],
    *,
    target_author: str | None = None,
) -> None:
    """Route the accessible sidebar page choice to its presentation."""
    if page in {"Overview", "Portfolio Overview"}:
        if target_author is None:
            render_portfolio_page(results, failures, repositories)
        else:
            render_portfolio_page(
                results, failures, repositories, target_author=target_author
            )
    elif page == "Repository Analytics":
        if target_author is None:
            render_repository_page(results)
        else:
            render_repository_page(results, target_author=target_author)
    elif page == "People Analytics":
        if target_author is None:
            render_people_page(results)
        else:
            render_people_page(results, target_author=target_author)
    elif page == "PR Explorer":
        render_explorer_page(results)
    elif page == "Quality & Reliability":
        render_quality_page(results)
    elif page == "Insights & Recommendations":
        if target_author is None:
            render_insights_page(results)
        else:
            render_insights_page(results, target_author=target_author)
    else:
        st.error("The selected dashboard page is unavailable.")


def run_dashboard() -> None:
    st.set_page_config(page_title="PR Intelligence", page_icon="📊", layout="wide")
    st.markdown(
        """
        <style>
        :root { --navy: #10233f; --blue: #1877d2; --ink: #172b4d; --muted: #5c6b80; }
        .stApp { background: #f8fafc; color: var(--ink); font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
        h1, h2, h3 { color: var(--navy); letter-spacing: -.02em; }
        h1 { margin-bottom: .35rem; }
        [data-testid="stCaptionContainer"] { color: var(--muted); }
        [data-testid="stHorizontalBlock"] { gap: 1rem; }
        [data-testid="stSidebar"] { background: #f1f4f8; border-right: 1px solid #e2e8f0; }
        [data-testid="stSidebar"] * { color: #172b4d; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label { border-radius: 6px; padding: .28rem .42rem; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) { background: #e1edfb; }
        [data-testid="stSidebar"] input, [data-testid="stSidebar"] textarea { color: #172b4d; }
        [data-testid="stSidebar"] [data-testid="stDateInputField"] [role="spinbutton"],
        [data-testid="stSidebar"] [data-testid="stDateInputField"] [data-type="literal"] {
          color: #172b4d !important;
          -webkit-text-fill-color: #172b4d !important;
          opacity: 1 !important;
        }
        [data-testid="stSidebar"] [data-testid="stDateInput"] input,
        [data-testid="stSidebar"] input[type="date"] {
          background: #ffffff !important;
          color: #172b4d !important;
          -webkit-text-fill-color: #172b4d !important;
          color-scheme: light;
        }
        [data-testid="stSidebar"] [data-testid="stDateInput"] button {
          color: #334155 !important;
          background: #ffffff !important;
        }
        [data-testid="stSidebar"] input[type="date"]::-webkit-calendar-picker-indicator {
          opacity: .85;
          filter: brightness(0) saturate(100%) invert(18%) sepia(17%) saturate(1223%) hue-rotate(178deg) brightness(93%) contrast(91%);
          cursor: pointer;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button,
        [data-testid="stSidebar"] [data-testid="stFormSubmitButton"] button {
          background: #f8fafc !important;
          border: 1px solid #cbd5e1 !important;
          color: #10233f !important;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button *,
        [data-testid="stSidebar"] [data-testid="stFormSubmitButton"] button * {
          color: #10233f !important;
          fill: #10233f !important;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button:hover,
        [data-testid="stSidebar"] [data-testid="stFormSubmitButton"] button:hover {
          background: #1877d2 !important;
          border-color: #1877d2 !important;
          color: #ffffff !important;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button:hover *,
        [data-testid="stSidebar"] [data-testid="stFormSubmitButton"] button:hover * {
          color: #ffffff !important;
          fill: #ffffff !important;
        }
        [data-testid="stSidebar"] [data-testid="stDateInput"] input:focus {
          border-color: #1877d2 !important;
          box-shadow: 0 0 0 2px rgba(24, 119, 210, .2) !important;
        }
        [data-testid="stMetric"] { background: #fff; border: 1px solid #dce3ed; border-radius: 9px; padding: 12px; }
        [data-testid="stMetricLabel"] { color: #58677e; font-weight: 600; }
        [data-testid="stMetricValue"] { color: #10233f; font-weight: 700; }
        [data-testid="stVerticalBlockBorderWrapper"] { border-color: #e5eaf1; border-radius: 8px; background: #fff; }
        .st-key-next-pr-action-plan { background: #f0f5fb; border-radius: 8px; padding: .85rem 1rem; }
        .st-key-next-pr-action-plan h3 { margin-top: .1rem; }
        @media (max-width: 900px) {
          [data-testid="stHorizontalBlock"] { flex-wrap: wrap; gap: .65rem; }
          [data-testid="stHorizontalBlock"] > [data-testid="column"] { min-width: min(100%, 240px); flex: 1 1 240px; }
          [data-testid="stMetric"] { padding: 8px; }
          h1 { font-size: 1.7rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    api_url = configured_api_url()
    st.title("Pull request intelligence")
    sidebar = st.sidebar
    with sidebar:
        github_token = st.text_input(
            "GitHub Enterprise token",
            type="password",
            key="github_token_input",
            on_change=_handle_github_token_change,
            help="Used only for read-only requests during this browser session; it is not saved by the app.",
        )
        has_token = isinstance(github_token, str) and bool(github_token.strip())
        token_ready = has_token and github_token == github_token.strip()
        st.caption(f"GitHub Enterprise API: {api_url} · read-only")
        if not has_token:
            st.info("Enter your GitHub token to enable repository discovery and PR collection.")
        elif not token_ready:
            st.warning("Remove leading or trailing whitespace from the GitHub token.")
        if st.button(
            "Discover accessible repositories",
            key="discover_repositories",
            disabled=not token_ready,
            width="stretch",
        ):
            st.session_state["discovered_repositories"] = []
            try:
                discovered = discover_repositories(github_token)
                st.session_state["discovered_repositories"] = [
                    {
                        key: item.get(key)
                        for key in ("id", "owner", "name", "full_name", "url", "default_branch", "language", "visibility")
                    }
                    for item in discovered
                    if item.get("owner") and item.get("name")
                ]
                st.session_state["repository_discovery_status"] = {
                    "kind": "success" if discovered else "info",
                    "text": (f"Discovered {len(discovered)} accessible repositories." if discovered
                             else "Discovery succeeded; no accessible repositories were returned."),
                }
            except Exception as error:
                st.session_state["repository_discovery_status"] = {
                    "kind": "error", "text": safe_error_message(error),
                }
        discovery_status = st.session_state.get("repository_discovery_status")
        if isinstance(discovery_status, dict) and discovery_status.get("text"):
            status_kind = discovery_status.get("kind")
            if status_kind == "success":
                st.success(discovery_status["text"])
            elif status_kind == "error":
                st.error(discovery_status["text"])
            else:
                st.caption(discovery_status["text"])

        st.markdown("---")
        st.markdown("### Filters")
        discovered_metadata = st.session_state.get("discovered_repositories", [])
        repository_options = list(AVAILABLE_REPOSITORIES)
        metadata_by_name = {}
        for item in discovered_metadata:
            full_name = item.get("full_name") or f"{item.get('owner')}/{item.get('name')}"
            metadata_by_name[full_name] = item
            if full_name not in repository_options:
                repository_options.append(full_name)
        default_repo = AVAILABLE_REPOSITORIES[0]
        st.session_state.setdefault(
            "selected_repositories",
            [default_repo] if default_repo in repository_options else [],
        )
        with st.form("collection_form"):
            selected_names = st.multiselect(
                "Repositories to include",
                options=repository_options,
                key="selected_repositories",
                help="Configured repositories are conveniences, not a complete accessible inventory.",
            )
            manual_value = st.text_input(
                "Add repository (OWNER/REPOSITORY)",
                placeholder="owner/repository",
                help="Optional additional repository.",
            ).strip()
            today = date.today()
            from_day = st.date_input("PR created from (inclusive)", value=today - timedelta(days=30))
            to_day = st.date_input("PR created to (inclusive)", value=today)
            author_text = st.text_input(
                "Author filter (comma-separated)",
                value=TARGET_AUTHOR,
                placeholder="author1, author2, author3",
                help="Enter one or more GitHub usernames separated by commas. Whitespace and duplicate names are ignored; leave blank to include all authors.",
            )
            submitted = st.form_submit_button(
                "Apply Filters", use_container_width=True, disabled=not token_ready
            )

        st.markdown("---")
        st.markdown("### Pages")
        page = st.radio("Pages", PAGES, key="dashboard_page", label_visibility="collapsed")


    selected_repositories: list[dict[str, Any] | str] = [
        metadata_by_name.get(name, name) for name in selected_names
    ]
    if manual_value:
        selected_repositories.append(manual_value)

    if submitted:
        # A submitted filter set starts a new collection scope; never display
        # the prior scope as though it matched the new filters after failure.
        st.session_state["collection_generation"] = (
            st.session_state.get("collection_generation", 0) + 1
        )
        st.session_state.pop("local_comment_summary", None)
        st.session_state.pop("runtime_orchestration", None)
        st.session_state.pop("current_action_plan", None)
        st.session_state["collection_complete"] = False
        st.session_state["portfolio_results"] = {}
        st.session_state["portfolio_failures"] = {}
        st.session_state["portfolio_repositories"] = selected_repositories
        try:
            if not selected_repositories:
                raise DashboardInputError("Select at least one repository or enter OWNER/REPOSITORY.")
            author = validate_author(author_text)
            if from_day > to_day:
                raise DashboardInputError("The start date must be on or before the end date.")
            if not token_ready:
                raise DashboardInputError(
                    "Enter a GitHub Enterprise token without surrounding whitespace before collecting data."
                )
            with st.status("Starting GitHub collection…", expanded=True) as collection_status:
                progress_text = st.empty()

                def report_collection_progress(message: str) -> None:
                    progress_text.caption(message)
                    collection_status.update(
                        label=message,
                        state="running",
                        expanded=True,
                    )

                orchestration: dict[str, Any] = {"status": {}, "results": {}}
                repository_runs: list[dict[str, Any]] = []
                all_reports: list[dict[str, Any]] = []
                all_summaries: list[dict[str, Any]] = []
                results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
                failures: dict[str, str] = {}
                runtime = Orchestrator()
                for selected in selected_repositories:
                    owner, name, label = repository_identity(selected)
                    orchestration = runtime.run(
                        owner=owner, repository=name, from_day=from_day, to_day=to_day,
                        author=author, token=github_token,
                        progress_callback=report_collection_progress,
                    )
                    repository_run = {"repository": label,
                                      "status": orchestration.get("status", {}),
                                      "comment_report": (orchestration.get("analytics") or {}).get("comment_report")}
                    repository_runs.append(repository_run)
                    if repository_run["comment_report"]:
                        all_reports.append(repository_run["comment_report"])
                    if orchestration.get("summary"):
                        all_summaries.append(orchestration["summary"])
                    if orchestration.get("envelope"):
                        envelope = orchestration["envelope"]
                        analysis = (orchestration.get("analytics") or {}).get("metrics")
                        if analysis is None:
                            analysis = analyze_repository(envelope)
                        results[label] = (envelope, analysis)
                    for error_key, message in orchestration.get("failures", {}).items():
                        failures[label] = message
                if all_reports:
                    from python_script.action_plan import build_action_plan
                    orchestration["comment_reports"] = all_reports
                    orchestration["action_plan"] = build_action_plan(all_reports)
                    orchestration["summaries"] = all_summaries
                    orchestration["repository_runs"] = repository_runs
                st.session_state["runtime_orchestration"] = orchestration
            st.session_state["portfolio_results"] = results
            st.session_state["portfolio_failures"] = failures
            st.session_state["portfolio_repositories"] = selected_repositories
            st.session_state["collection_complete"] = True
            collection_status.update(
                label=(
                    collection_status_message(
                        results,
                        failures,
                        expected_repository_count=len(selected_repositories),
                    )
                ),
                state="error" if not results else "complete",
                expanded=bool(failures) or bool(results and collection_status_message(
                    results, failures, expected_repository_count=len(selected_repositories)
                ).startswith("PR retrieval is partial")),
            )
        except (DashboardInputError, GitHubAPIError, AnalyticsInputError, ValueError) as error:
            st.error(safe_error_message(error))
        except Exception as error:
            st.error(safe_error_message(error))

    results = st.session_state.get("portfolio_results", {})
    failures = st.session_state.get("portfolio_failures", {})
    orchestration = st.session_state.get("runtime_orchestration")
    result_repositories = st.session_state.get("portfolio_repositories", selected_repositories)
    collection_complete = st.session_state.get("collection_complete", False)
    if page == "Overview":
        target_author = render_author_action_plan(
            results, failures, collection_complete=collection_complete,
            orchestration=orchestration,
        )
        action_state = st.session_state.get("current_action_plan") or {}
        action_plan = (
            action_state.get("plan")
            if action_state.get("collection_generation") == st.session_state.get("collection_generation", 0)
            else None
        )
        summaries = orchestration.get("summaries", []) if orchestration else []
        if not summaries and orchestration and orchestration.get("summary"):
            summaries = [orchestration["summary"]]
        _render_orchestrated_summaries(summaries, orchestration or {}, action_plan)
        if collection_complete:
            with st.expander("Portfolio metrics and trends", expanded=False):
                render_page(page, results, failures, result_repositories, target_author=target_author)
    else:
        if collection_complete:
            st.caption("Collection results are held in this session only; partial failures retain successful repositories.")
        else:
            st.info("No collection has been run yet. Apply filters in the sidebar to collect PR data.")
        target_author = next((
            (envelope.get("filters") or {}).get("author")
            for envelope, _ in results.values()
            if (envelope.get("filters") or {}).get("author")
        ), None)
        render_page(page, results, failures, result_repositories, target_author=target_author)


if __name__ == "__main__":
    run_dashboard()
