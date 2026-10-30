"""Bounded, deterministic review-comment analysis for collector v0.1 envelopes.

Taxonomy is an explicit keyword/rule-based signal, not a semantic judgment.
Rules are case-insensitive literal/phrase matches; one comment can match several
categories. Unmatched or textless comments are ``other_unclassified``. Returned
comment text is evidence for local display only and is never copied into a
category summary. This module makes no network calls.

The requested UTC creation window selects target-authored PRs inclusively.
Activity is observed through the envelope's UTC observation cutoff. No
business-time or addressed/resolved inference is made. Recurrence uses the
requested PR-creation window as its history window and only PRs with complete
review-comment, issue-comment, and submitted-review histories as its PR denominator.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

try:
    from .author_filter import normalize_author_filter, parse_author_logins
except ImportError:
    from author_filter import normalize_author_filter, parse_author_logins


ANALYSIS_VERSION = "comment-analysis-v1"
SUPPORTED_SCHEMA_VERSION = "0.1"
UTC = timezone.utc

# Conservative literal signals. These rules are intentionally transparent and
# are not an attempt to interpret intent or establish whether feedback is valid.
TAXONOMY_RULES: dict[str, tuple[str, ...]] = {
    "functional_defect": (
        "bug", "incorrect", "wrong result", "doesn't work", "does not work",
        "not working", "regression", "fix this", "broken",
    ),
    "conditional_logic": (
        "conditional logic", "if/else", "branch condition", "guard condition",
        "guard clause", "boolean condition", "inverted condition",
    ),
    "api_contract": (
        "api contract", "endpoint contract", "api endpoint", "request/response",
        "request and response", "response shape", "response schema",
        "request schema", "backward compatible", "backward compatibility",
        "http status code",
    ),
    "edge_case": (
        "edge case", "boundary case", "empty input", "null value",
        "nil value", "zero-length", "off-by-one",
    ),
    "test_coverage": (
        "add a test", "add tests", "missing test", "needs a test",
        "test coverage", "cover this case", "unit test", "test case",
        "please add tests", "could you add tests", "can we add tests",
        "no tests", "add test coverage",
    ),
    "flaky_test": ("flaky", "flakiness", "intermittent test", "non-deterministic test"),
    "lint_style": (
        "formatting", "style", "lint", "pep8", "naming", "indentation",
        "trailing whitespace",
    ),
    "duplication": ("duplicate", "duplicated", "duplication", "copy-paste", "repeated code"),
    "maintainability": (
        "maintainability", "hard to maintain", "hard to read", "simplify",
        "refactor", "clearer", "readability",
    ),
    "exception_handling": (
        "exception", "exception handling", "error handling", "raise", "catch",
        "swallow the error", "try/except",
    ),
    "security": (
        "security", "vulnerability", "injection", "secret", "credential",
        "authorization", "authentication", "sanitize", "unsafe",
    ),
    "documentation": (
        "documentation", "document this", "docstring", "readme", "comment this",
    ),
    "test_evidence": (
        "tests pass", "test results", "test output", "run the tests",
        "show test", "ci result",
    ),
    "pr_description_scope": (
        "pr description", "pull request description", "scope", "out of scope",
        "description is", "update the description",
    ),
}
CATEGORIES = tuple(TAXONOMY_RULES) + ("other_unclassified",)
_VALID_STATUSES = {"complete", "partial", "unavailable", "not_requested", "error"}
_COMMENT_RESOURCES = ("review_comments", "issue_comments", "reviews")
_COMMENT_ARRAYS = {
    "review_comments": "review_comments",
    "issue_comments": "issue_comments",
    "reviews": "reviews",
}
_RESOURCE_TIMESTAMPS = {
    "review_comments": "created_at",
    "issue_comments": "created_at",
    "reviews": "submitted_at",
}


class CommentAnalysisInputError(ValueError):
    """The input is not a valid, bounded collector repository envelope."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CommentAnalysisInputError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CommentAnalysisInputError(f"{name} must be an array")
    if any(not isinstance(item, Mapping) for item in value):
        raise CommentAnalysisInputError(f"{name} must contain only objects")
    return value


def _instant(value: Any) -> datetime | None:
    """Parse an aware RFC3339-like timestamp; missing, naive, and invalid -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _identity(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _login(value: Any) -> str | None:
    login = _identity(value).get("login")
    return login.strip().casefold() if isinstance(login, str) and login.strip() else None


def _resource_usable(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("status") == "complete"
        and value.get("pagination_complete") is True
        and value.get("history_complete") is True
    )


def _validate_envelope(envelope: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    data = _mapping(envelope, "envelope")
    if data.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise CommentAnalysisInputError(
            f"schema_version must be {SUPPORTED_SCHEMA_VERSION!r}"
        )
    repository = _mapping(data.get("repository"), "repository")
    if not isinstance(repository.get("owner"), str) or not repository["owner"]:
        raise CommentAnalysisInputError("repository.owner must be a non-empty string")
    if not isinstance(repository.get("name"), str) or not repository["name"]:
        raise CommentAnalysisInputError("repository.name must be a non-empty string")
    window = _mapping(data.get("requested_window"), "requested_window")
    start, end = _instant(window.get("from")), _instant(window.get("to"))
    cutoff = _instant(data.get("observation_cutoff"))
    if start is None or end is None or start > end:
        raise CommentAnalysisInputError("requested_window must contain valid ordered UTC instants")
    if cutoff is None:
        raise CommentAnalysisInputError("observation_cutoff must be a valid timezone-aware timestamp")
    _mapping(data.get("filters"), "filters")
    if "author" in data["filters"] and data["filters"]["author"] is not None and not isinstance(
        data["filters"]["author"], str
    ):
        raise CommentAnalysisInputError("filters.author must be a login string or null")
    _mapping(data.get("resources"), "resources")
    _mapping(data.get("pull_request_resources"), "pull_request_resources")
    _sequence(data.get("pull_requests"), "pull_requests")
    return data, repository


def _comment_categories(text: Any) -> list[str]:
    normalized = text.casefold() if isinstance(text, str) else ""
    matches = [
        category
        for category, phrases in TAXONOMY_RULES.items()
        if any(
            re.search(
                r"(?<!\w)" + r"\s+".join(re.escape(part) for part in phrase.split()) + r"(?!\w)",
                normalized,
            )
            for phrase in phrases
        )
    ]
    return matches or ["other_unclassified"]


def _comment_evidence(comment: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "repository_id": comment.get("repository_id"),
        "pull_request_id": comment["pull_request_id"],
        "pull_request_number": comment["pull_request_number"],
        "comment_id": comment.get("comment_id"),
        "comment_source": comment["comment_source"],
        "comment_url": comment.get("comment_url"),
    }


def analyze_comments(envelope: Mapping[str, Any], target_author: str) -> dict[str, Any]:
    """Analyze local comment records for target-authored PRs in a collector envelope.

    Args:
        envelope: One repository result produced by collector schema 0.1.
        target_author: GitHub login selected by the caller; compared
            case-insensitively against each PR's author login.

    Returns inventory, evidence-linked category summaries, activity counts,
    and explicit per-PR comment resource coverage. Records without a stable
    ``pull_request_id`` or that conflict with their PR/repository are excluded
    and counted rather than attached by ambiguous PR number.
    """
    data, repository = _validate_envelope(envelope)
    if not isinstance(target_author, str) or not target_author.strip():
        raise CommentAnalysisInputError("target_author must be a non-empty login")
    try:
        target_logins = parse_author_logins(target_author)
        normalized_target = normalize_author_filter(target_author)
    except ValueError as error:
        raise CommentAnalysisInputError("target_author must contain valid GitHub logins") from error
    if not target_logins:
        raise CommentAnalysisInputError("target_author must contain at least one GitHub login")
    target = {login.casefold() for login in target_logins}
    requested_author = data["filters"].get("author")
    if isinstance(requested_author, str) and requested_author.strip():
        try:
            requested = {login.casefold() for login in parse_author_logins(requested_author)}
        except ValueError as error:
            raise CommentAnalysisInputError("envelope author filter must contain valid GitHub logins") from error
        if not target.issubset(requested):
            raise CommentAnalysisInputError(
                "envelope author filter excludes target_author; collect all authors or the selected author"
            )
    repo_id = repository.get("id")
    repo_owner = repository["owner"].casefold()
    repo_name = repository["name"].casefold()
    start = _instant(data["requested_window"]["from"])
    end = _instant(data["requested_window"]["to"])
    cutoff = _instant(data["observation_cutoff"])
    assert start is not None and end is not None and cutoff is not None

    population: dict[str, Mapping[str, Any]] = {}
    population_exclusions: Counter[str] = Counter()
    for pr in data["pull_requests"]:
        pr_id = pr.get("id")
        if pr_id is None:
            population_exclusions["missing_pull_request_id"] += 1
            continue
        pr_repository_id = pr.get("repository_id")
        if pr_repository_id is not None and repo_id is not None and str(pr_repository_id) != str(repo_id):
            population_exclusions["repository_identity_mismatch"] += 1
            continue
        pr_repository = pr.get("repository")
        if isinstance(pr_repository, Mapping) and (
            (
                isinstance(pr_repository.get("owner"), str)
                and pr_repository["owner"].casefold() != repo_owner
            )
            or (
                isinstance(pr_repository.get("name"), str)
                and pr_repository["name"].casefold() != repo_name
            )
        ):
            population_exclusions["repository_identity_mismatch"] += 1
            continue
        key = str(pr_id)
        created = _instant(pr.get("created_at"))
        author_login = _login(pr.get("author"))
        if created is None:
            population_exclusions["missing_or_invalid_created_at"] += 1
            continue
        if created < start or created > end:
            population_exclusions["outside_creation_window"] += 1
            continue
        if author_login is None:
            population_exclusions["missing_author_identity"] += 1
            continue
        if author_login.casefold() not in target:
            population_exclusions["different_author"] += 1
            continue
        if key in population:
            raise CommentAnalysisInputError(f"duplicate pull request ID {key}")
        population[key] = pr

    global_resources = data["resources"]
    pr_resources = data["pull_request_resources"]
    if "pull_requests" not in global_resources:
        raise CommentAnalysisInputError("resources.pull_requests is required")
    pull_request_resource = _mapping(global_resources["pull_requests"], "resources.pull_requests")
    pr_resource_status = pull_request_resource.get("status")
    if pr_resource_status not in _VALID_STATUSES:
        raise CommentAnalysisInputError("resources.pull_requests.status is invalid")
    pr_resource_count = pull_request_resource.get("count")
    if pr_resource_status in {"not_requested", "unavailable", "error"}:
        if pr_resource_count is not None:
            raise CommentAnalysisInputError("resources.pull_requests count must be null when unavailable")
    elif not isinstance(pr_resource_count, int) or isinstance(pr_resource_count, bool) or pr_resource_count < 0:
        raise CommentAnalysisInputError("resources.pull_requests.count must be a non-negative integer")
    if not isinstance(pull_request_resource.get("pagination_complete"), bool) or not isinstance(
        pull_request_resource.get("history_complete"), bool
    ):
        raise CommentAnalysisInputError("pull_requests completeness flags must be booleans")
    for resource in _COMMENT_RESOURCES:
        if resource not in global_resources:
            raise CommentAnalysisInputError(f"resources.{resource} is required")
        global_item = _mapping(global_resources[resource], f"resources.{resource}")
        status = global_item.get("status")
        if status not in _VALID_STATUSES:
            raise CommentAnalysisInputError(f"resources.{resource}.status is invalid")
        global_count = global_item.get("count")
        if status in {"not_requested", "unavailable", "error"}:
            if global_count is not None:
                raise CommentAnalysisInputError(
                    f"resources.{resource} count must be null for status {status}"
                )
        elif not isinstance(global_count, int) or isinstance(global_count, bool) or global_count < 0:
            raise CommentAnalysisInputError(f"resources.{resource}.count must be a non-negative integer")
        values = data.get(_COMMENT_ARRAYS[resource])
        if status == "not_requested":
            if values is not None:
                _sequence(values, _COMMENT_ARRAYS[resource])
        else:
            _sequence(values, _COMMENT_ARRAYS[resource])

    coverage: dict[str, dict[str, Any]] = {}
    fully_covered_prs: set[str] = set()
    for pr_id, pr in population.items():
        per_pr_value = pr_resources.get(pr_id)
        per_pr = _mapping(per_pr_value, f"pull_request_resources[{pr_id}]") if per_pr_value is not None else {}
        resources_for_pr: dict[str, Any] = {}
        complete_all = True
        for resource in _COMMENT_RESOURCES:
            item = per_pr.get(resource)
            if item is None:
                # The collector omits per-PR entries for resources not requested.
                status = global_resources[resource]["status"]
                if status != "not_requested":
                    raise CommentAnalysisInputError(
                        f"pull_request_resources[{pr_id}].{resource} is missing"
                    )
                item = {"status": "not_requested", "count": None,
                        "pagination_complete": False, "history_complete": False}
            item = _mapping(item, f"pull_request_resources[{pr_id}].{resource}")
            status = item.get("status")
            if status not in _VALID_STATUSES:
                raise CommentAnalysisInputError(
                    f"pull_request_resources[{pr_id}].{resource}.status is invalid"
                )
            if not isinstance(item.get("pagination_complete"), bool) or not isinstance(
                item.get("history_complete"), bool
            ):
                raise CommentAnalysisInputError(
                    f"{resource} completeness flags must be booleans for PR {pr_id}"
                )
            count = item.get("count")
            if status in {"not_requested", "unavailable", "error"}:
                if count is not None:
                    raise CommentAnalysisInputError(
                        f"{resource} {status} count must be null for PR {pr_id}"
                    )
            elif not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise CommentAnalysisInputError(f"{resource} count must be a non-negative integer")
            complete = _resource_usable(item)
            complete_all = complete_all and complete
            resources_for_pr[resource] = {
                "status": status,
                "count": count,
                "pagination_complete": item.get("pagination_complete") is True,
                "history_complete": item.get("history_complete") is True,
                "complete_history": complete,
            }
        if complete_all:
            fully_covered_prs.add(pr_id)
        coverage[pr_id] = {
            "pull_request_id": pr.get("id"),
            "pull_request_number": pr.get("number"),
            "resources": resources_for_pr,
            "all_comment_sources_complete": complete_all,
        }

    excluded_records: Counter[str] = Counter()
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    inventory: list[dict[str, Any]] = []
    identity_mismatch = {"repository_id": 0, "repository": 0, "pull_request_number": 0}
    duplicate_omitted = 0
    duplicate_conflicts = 0

    for resource in _COMMENT_RESOURCES:
        records = data.get(_COMMENT_ARRAYS[resource]) or []
        for raw in records:
            record = dict(raw)
            linked_id = record.get("pull_request_id")
            if linked_id is None or str(linked_id) not in population:
                excluded_records["missing_or_out_of_population_pull_request_link"] += 1
                continue
            pr_key = str(linked_id)
            pr = population[pr_key]
            observed_resource = coverage[pr_key]["resources"][resource]["status"]
            if observed_resource in {"not_requested", "unavailable", "error"}:
                raise CommentAnalysisInputError(
                    f"{resource} returned records for PR {pr_key} with status {observed_resource}; "
                    "preserve collected pages with partial status instead."
                )
            if record.get("repository_id") is not None and repo_id is not None:
                if str(record["repository_id"]) != str(repo_id):
                    identity_mismatch["repository_id"] += 1
                    excluded_records["repository_identity_mismatch"] += 1
                    continue
            linked_repo = record.get("repository")
            if isinstance(linked_repo, Mapping):
                if (
                    isinstance(linked_repo.get("owner"), str)
                    and linked_repo["owner"].casefold() != repo_owner
                ) or (
                    isinstance(linked_repo.get("name"), str)
                    and linked_repo["name"].casefold() != repo_name
                ):
                    identity_mismatch["repository"] += 1
                    excluded_records["repository_identity_mismatch"] += 1
                    continue
            if record.get("pull_request_number") is not None and pr.get("number") is not None:
                if str(record["pull_request_number"]) != str(pr["number"]):
                    identity_mismatch["pull_request_number"] += 1
                    excluded_records["pull_request_number_mismatch"] += 1
                    continue

            comment_id = record.get("id")
            dedupe_key = (resource, pr_key, str(comment_id)) if comment_id is not None else None
            relevant_signature = tuple(
                record.get(field) for field in (
                    "body", "created_at", "submitted_at", "state", "url",
                    "in_reply_to_id", "path", "line",
                    "original_line",
                )
            ) + (
                _login(record.get("author")),
                _identity(record.get("author")).get("id"),
            )
            if dedupe_key is not None and dedupe_key in seen:
                previous = seen[dedupe_key]
                if previous["_signature"] == relevant_signature:
                    duplicate_omitted += 1
                    continue
                duplicate_conflicts += 1

            author = _identity(record.get("author"))
            comment_author = _login(author)
            pr_author = _login(pr.get("author"))
            role = (
                "author_response" if comment_author is not None and comment_author == pr_author
                else "reviewer" if comment_author is not None
                else "unknown"
            )
            timestamp_field = _RESOURCE_TIMESTAMPS[resource]
            source_timestamp = record.get(timestamp_field)
            parsed_created = _instant(source_timestamp)
            timestamp_status = (
                "missing" if source_timestamp is None or source_timestamp == ""
                else "invalid" if parsed_created is None
                else "before_pr_creation" if parsed_created < (_instant(pr.get("created_at")) or start)
                else "after_observation_cutoff" if parsed_created > cutoff
                else "observed"
            )
            included = timestamp_status == "observed"
            categories = _comment_categories(record.get("body")) if included and role == "reviewer" else []
            comment = {
                "_signature": relevant_signature,
                "repository_id": repo_id,
                "pull_request_id": pr.get("id"),
                "pull_request_number": pr.get("number"),
                "pull_request_title": pr.get("title"),
                "pull_request_url": pr.get("url"),
                "comment_id": comment_id,
                "comment_source": resource,
                "comment_type": {
                    "review_comments": "review_comment",
                    "issue_comments": "issue_comment",
                    "reviews": "review_summary",
                }[resource],
                "comment_url": record.get("url"),
                "author_login": author.get("login") if isinstance(author.get("login"), str) else None,
                "author_role": role,
                "created_at": source_timestamp if resource != "reviews" else None,
                "submitted_at": source_timestamp if resource == "reviews" else None,
                "observed_at": source_timestamp,
                "timestamp_field": timestamp_field,
                "review_state": record.get("state") if resource == "reviews" else None,
                "path": record.get("path"),
                "line": record.get("line"),
                "original_line": record.get("original_line"),
                "in_reply_to_id": record.get("in_reply_to_id"),
                "categories": categories,
                "addressed_status": "not_assessed",
                "included_in_observed_activity": included,
                "included_in_summary": (
                    isinstance(record.get("body"), str)
                    and bool(record["body"].strip())
                ),
                "timestamp_status": timestamp_status,
                "duplicate_id_conflict": False,
            }
            # Do not expose comment body for records that cannot be placed at or
            # before the cutoff. Valid, in-scope evidence remains local output.
            if included:
                comment["comment_text"] = record.get("body") if isinstance(record.get("body"), str) else ""
            if comment["included_in_summary"]:
                comment["summary_text"] = record["body"]
            if dedupe_key is not None and dedupe_key not in seen:
                comment["_signature"] = relevant_signature
                seen[dedupe_key] = comment
            elif dedupe_key is not None:
                comment["duplicate_id_conflict"] = True
                comment["included_in_observed_activity"] = False
                comment["analysis_exclusion_reason"] = "duplicate_id_conflict_same_stable_identity"
                comment["categories"] = []
            inventory.append(comment)

    for comment in inventory:
        comment.pop("_signature", None)

    observed = [item for item in inventory if item["included_in_observed_activity"]]
    reviewer_comments = [item for item in observed if item["author_role"] == "reviewer"]
    author_responses = [item for item in observed if item["author_role"] == "author_response"]
    unknown_author = [item for item in observed if item["author_role"] == "unknown"]
    denominator = len(reviewer_comments)
    category_summaries: dict[str, Any] = {}
    for category in CATEGORIES:
        matching = [item for item in reviewer_comments if category in item["categories"]]
        affected = sorted({item["pull_request_id"] for item in matching})
        recurrence_affected = sorted(
            {
                item["pull_request_id"]
                for item in matching
                if str(item["pull_request_id"]) in fully_covered_prs
            }
        )
        recurrence_denominator = len(fully_covered_prs)
        category_summaries[category] = {
            "comment_count": len(matching),
            "affected_pr_count": len(affected),
            "percentage": {
                "numerator": len(matching),
                "denominator": denominator,
                "value": (100.0 * len(matching) / denominator) if denominator else None,
                "denominator_definition": (
                    "all observed, timestamp-valid, cutoff-bounded comments by known "
                    "non-PR-authors on selected target-authored PRs, across review comments, issue comments, and submitted review summaries"
                ),
                "completeness": "observed_only; see resource_coverage",
                "categories_are_multi_label": True,
            },
            "recurrence_by_pr": {
                "observed_affected_pr_count": len(affected),
                "affected_pr_count": len(recurrence_affected),
                "denominator": recurrence_denominator,
                "denominator_definition": (
                    "selected target-authored PRs in requested_window with review_comments, "
                    "issue_comments, and reviews complete, paginated, and history-complete"
                ),
                "percentage": (
                    100.0 * len(recurrence_affected) / recurrence_denominator
                    if recurrence_denominator else None
                ),
                "recurring_in_at_least_two_prs": len(recurrence_affected) >= 2,
                "history_window": dict(data["requested_window"]),
                "completeness": "complete_history_denominator" if recurrence_denominator else "unavailable_no_complete_prs",
            },
            "evidence": [_comment_evidence(item) for item in matching],
        }

    resource_coverage: dict[str, Any] = {}
    for resource in _COMMENT_RESOURCES:
        statuses = Counter(
            coverage[pr_id]["resources"][resource]["status"] for pr_id in population
        )
        resource_coverage[resource] = {
            "pr_count_by_status": {status: statuses.get(status, 0) for status in sorted(_VALID_STATUSES)},
            "complete_pr_count": sum(
                1 for pr_id in population
                if coverage[pr_id]["resources"][resource]["complete_history"]
            ),
            "population_pr_count": len(population),
            "successful_empty_pr_count": sum(
                1 for pr_id in population
                if coverage[pr_id]["resources"][resource]["complete_history"]
                and coverage[pr_id]["resources"][resource]["count"] == 0
            ),
            "population_complete": (
                bool(population)
                and all(
                    coverage[pr_id]["resources"][resource]["complete_history"]
                    for pr_id in population
                )
            ),
        }

    return {
        "analysis_version": ANALYSIS_VERSION,
        "repository": {
            "id": repo_id,
            "host_url": repository.get("host_url"),
            "owner": repository["owner"],
            "name": repository["name"],
            "url": repository.get("url"),
        },
        "target_author": normalized_target or target_author.strip(),
        "creation_window": dict(data["requested_window"]),
        "observation_cutoff": data["observation_cutoff"],
        "elapsed_time_semantics": "not_applicable; no durations are calculated",
        "population": {
            "definition": (
                "PRs authored by target_author with valid created_at in the inclusive "
                "requested UTC creation window"
            ),
            "pull_request_count": len(population),
            "coverage": {
                "status": pr_resource_status,
                "pagination_complete": pull_request_resource["pagination_complete"],
                "history_complete": pull_request_resource["history_complete"],
                "selected_pr_array_count": len(data["pull_requests"]),
                "declared_count": pr_resource_count,
                "complete_for_target_selection": (
                    pr_resource_status == "complete"
                    and pull_request_resource["pagination_complete"] is True
                    and pull_request_resource["history_complete"] is True
                    and not any(
                        reason in population_exclusions
                        for reason in (
                            "missing_pull_request_id",
                            "missing_or_invalid_created_at",
                            "missing_author_identity",
                            "repository_identity_mismatch",
                        )
                    )
                ),
            },
            "excluded_pull_request_count_by_reason": dict(population_exclusions),
            "pull_request_evidence": [
                {
                    "pull_request_id": pr_id,
                    "pull_request_number": pr.get("number"),
                    "title": pr.get("title"),
                    "url": pr.get("url"),
                }
                for pr_id, pr in population.items()
            ],
        },
        "resource_coverage": resource_coverage,
        "per_pull_request_coverage": coverage,
        "comment_activity": {
            "observed_comment_count": len(observed),
            "summary_comment_count": sum(
                1 for item in inventory if item["included_in_summary"]
            ),
            "summary_comment_count_by_source": {
                resource: sum(
                    1
                    for item in inventory
                    if item["included_in_summary"] and item["comment_source"] == resource
                )
                for resource in _COMMENT_RESOURCES
            },
            "observed_reviewer_comment_count": len(reviewer_comments),
            "observed_author_response_count": len(author_responses),
            "observed_unknown_author_count": len(unknown_author),
            "observed_review_summary_count": sum(
                1 for item in observed if item["comment_source"] == "reviews"
            ),
            "observed_review_comment_count": sum(
                1 for item in observed if item["comment_source"] == "review_comments"
            ),
            "observed_issue_comment_count": sum(
                1 for item in observed if item["comment_source"] == "issue_comments"
            ),
            "comments_missing_or_invalid_timestamp": sum(
                1 for item in inventory if item["timestamp_status"] in {"missing", "invalid", "before_pr_creation"}
            ),
            "comments_after_observation_cutoff": sum(
                1 for item in inventory if item["timestamp_status"] == "after_observation_cutoff"
            ),
            "feedback_denominator": denominator,
            "feedback_denominator_definition": (
                "observed, timestamp-valid, cutoff-bounded review comments, issue comments, "
                "and submitted review summaries by known non-PR-authors"
            ),
        },
        "categories": category_summaries,
        "taxonomy": {
            "method": "deterministic, case-insensitive keyword/rule-based signals with token boundaries",
            "semantic_certainty": False,
            "rules": {category: list(phrases) for category, phrases in TAXONOMY_RULES.items()},
            "unclassified_category": "other_unclassified",
            "multiple_categories_per_comment": True,
        },
        "comment_inventory": inventory,
        "data_quality": {
            "excluded_comment_record_count_by_reason": dict(excluded_records),
            "repository_or_PR_link_mismatch_count": identity_mismatch,
            "duplicate_comment_records_omitted_by_stable_id": duplicate_omitted,
            "duplicate_id_conflict_records_retained": duplicate_conflicts,
            "comment_resource_coverage_complete_for_population": all(
                resource_coverage[resource]["population_complete"] for resource in _COMMENT_RESOURCES
            ),
            "observations_are_not_population-complete_unless_coverage_says_so": True,
        },
        "interpretation_limits": [
            "Categories are deterministic keyword/rule-based signals, not semantic certainty; review the linked source text.",
            "Comment text is included only in comment_inventory for local evidence display, never in category summaries.",
            "addressed_status is always not_assessed; a reply or author response does not establish resolution.",
            "Partial, unavailable, and not_requested resources are not zero; coverage is reported separately.",
            "No motivation, competence, personal-performance, or causal-delay inference is made.",
        ],
    }
