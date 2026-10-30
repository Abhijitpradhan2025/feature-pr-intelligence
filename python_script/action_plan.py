"""Deterministic, evidence-linked action plans built from comment-analysis reports.

Every recommendation is a rule-based taxonomy signal, not a finding that the
underlying concern is true. The plan only uses observed reviewer-role items
included by ``comment_analysis``. It makes no network calls and infers no intent,
competence, or individual performance.

Risk tiers are an explicit ordering policy, not a severity assessment:
security; functional defects; error handling; edge cases; testing; then
maintainability/style/documentation. Within each tier, categories sort by
distinct affected PRs descending, then unique evidence items descending, then
taxonomy declaration order. Priority labels indicate this deterministic rank.

Frequency numerator is distinct repository+PR identities with a category
signal; its denominator is the distinct selected PR population represented by
the reports. The denominator is the observed selected population, not an
assurance that all requested PRs were collected. Coverage is reported
separately. Evidence count is the number of deduplicated reviewer items, not
the PR frequency. Missing population or coverage evidence is unknown rather
than zero.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

try:  # Supports both ``python -m python_script.action_plan`` and local imports.
    from .comment_analysis import TAXONOMY_RULES
except ImportError:  # pragma: no cover - exercised by direct-script import layout
    from comment_analysis import TAXONOMY_RULES


ACTION_PLAN_VERSION = "action-plan-v1"
COMMENT_RESOURCES = ("review_comments", "issue_comments", "reviews")
TAXONOMY_ORDER = tuple(TAXONOMY_RULES)

# Tier 1 (highest ordering precedence) through tier 6. Related categories share
# a tier and do not imply the same underlying defect.
RISK_TIERS: dict[str, int] = {
    "security": 1,
    "functional_defect": 2,
    "conditional_logic": 2,
    "api_contract": 2,
    "exception_handling": 3,
    "edge_case": 4,
    "test_coverage": 5,
    "flaky_test": 5,
    "test_evidence": 5,
    "maintainability": 6,
    "duplication": 6,
    "lint_style": 6,
    "documentation": 6,
    "pr_description_scope": 6,
}
TIER_NAMES = {
    1: "security",
    2: "functional behavior and API contracts",
    3: "error handling",
    4: "edge cases",
    5: "testing",
    6: "maintainability/style/documentation",
}

# Text deliberately describes only what taxonomy matched and suggests a
# category-appropriate check; no specific bug or semantics are fabricated.
TEMPLATES: dict[str, dict[str, str]] = {
    "security": {
        "focus": "security-related rule-based signal",
        "action": "Review the flagged change for the security concern represented by this category and make any needed safeguards explicit.",
        "validation": "A targeted security review records the relevant threat/check and verifies the changed behavior; add a focused test where applicable.",
    },
    "functional_defect": {
        "focus": "functional-defect rule-based signal",
        "action": "Trace the affected behavior against its intended result and correct any confirmed mismatch.",
        "validation": "A focused regression test demonstrates the intended result for the behavior under review.",
    },
    "conditional_logic": {
        "focus": "conditional-logic rule-based signal",
        "action": "Review each affected branch and guard condition against the intended behavior, including the false and boundary paths.",
        "validation": "Add focused tests for each relevant branch and its boundary conditions.",
    },
    "api_contract": {
        "focus": "API-contract rule-based signal",
        "action": "Verify the endpoint's request and response contract, including status codes and compatibility with existing callers.",
        "validation": "Add or update endpoint tests for expected success and error responses and any compatibility requirements.",
    },
    "exception_handling": {
        "focus": "error-handling rule-based signal",
        "action": "Review the changed failure paths and make error handling deliberate and observable.",
        "validation": "Tests exercise the relevant failure path and confirm the expected error behavior.",
    },
    "edge_case": {
        "focus": "edge-case rule-based signal",
        "action": "Identify the boundary conditions relevant to the change and handle them explicitly.",
        "validation": "Focused tests cover the applicable boundary conditions as well as a normal input.",
    },
    "test_coverage": {
        "focus": "test-coverage rule-based signal",
        "action": "Add or update focused tests for the changed behavior before requesting review.",
        "validation": "The new or updated tests run successfully and cover the behavior changed by the PR.",
    },
    "flaky_test": {
        "focus": "test-stability rule-based signal",
        "action": "Investigate test nondeterminism and remove timing or environmental assumptions where possible.",
        "validation": "The relevant test passes repeatedly under the repository's normal test command.",
    },
    "test_evidence": {
        "focus": "test-evidence rule-based signal",
        "action": "Run the relevant checks and include concise, reproducible results with the PR.",
        "validation": "The stated test command and required CI checks complete successfully, with results linked or recorded.",
    },
    "maintainability": {
        "focus": "maintainability rule-based signal",
        "action": "Review the affected code for a simpler, clearer structure that preserves behavior.",
        "validation": "The revised code remains behaviorally covered and passes the relevant tests.",
    },
    "duplication": {
        "focus": "duplication rule-based signal",
        "action": "Check whether the repeated logic should be consolidated without obscuring distinct behavior.",
        "validation": "Any consolidation preserves the relevant behavior and is covered by focused tests.",
    },
    "lint_style": {
        "focus": "style/lint rule-based signal",
        "action": "Apply the repository's established formatting, naming, and lint conventions to the changed code.",
        "validation": "The configured formatter or linter passes for the changed files.",
    },
    "documentation": {
        "focus": "documentation rule-based signal",
        "action": "Update the relevant documentation to reflect the changed behavior or usage.",
        "validation": "Documentation matches the implemented behavior and any referenced commands or options are current.",
    },
    "pr_description_scope": {
        "focus": "PR-description/scope rule-based signal",
        "action": "Clarify the PR description, including intended scope and any relevant context.",
        "validation": "The description states the change, scope, and verification performed in terms consistent with the diff.",
    },
}

class ActionPlanInputError(ValueError):
    """Raised when a report collection cannot be interpreted consistently."""


def _safe_https_url(value: Any) -> str | None:
    """Return an unambiguous HTTPS URL, rejecting credentials and unsafe syntax."""
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https" or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        # Accessing .port also validates malformed ports.
        _ = parts.port
        if any(char.isspace() for char in parts.hostname):
            return None
    except (ValueError, UnicodeError):
        return None
    return value


def _string(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _comment_excerpt(value: Any, *, limit: int = 220) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _identity(repo: Mapping[str, Any], pr_id: Any, number: Any) -> tuple[str, str] | None:
    owner, name = _string(repo.get("owner")), _string(repo.get("name"))
    identifier = _string(pr_id)
    if not owner or not name:
        return None
    if identifier is None:
        identifier = f"number:{_string(number)}" if _string(number) else None
    return (f"{owner.casefold()}/{name.casefold()}", identifier) if identifier else None


def _comment_identity(repository_key: str, item: Mapping[str, Any]) -> tuple[Any, ...]:
    pr_identity = _string(item.get("pull_request_id"))
    if pr_identity is None:
        pr_identity = f"number:{_string(item.get('pull_request_number')) or 'unknown'}"
    source = _string(item.get("comment_source")) or _string(item.get("comment_type")) or "unknown"
    comment_id = _string(item.get("comment_id"))
    if comment_id is not None:
        return repository_key, pr_identity, source, "id", comment_id
    # Stable fallback excludes URLs and mapping order; values are normalized to
    # strings to keep the signature hashable across JSON-like report shapes.
    signature = (
        _string(item.get("comment_text")) or "",
        _string(item.get("observed_at")) or "",
        _string(item.get("author_login")) or "",
        _string(item.get("path")) or "",
        _string(item.get("line")) or "",
        tuple(sorted(str(category) for category in item.get("categories", [])
                     if isinstance(category, str))),
    )
    return repository_key, pr_identity, source, "signature", signature


def _valid_reports(reports: Any) -> list[Mapping[str, Any]]:
    if isinstance(reports, Mapping):
        reports = [reports]
    if not isinstance(reports, Sequence) or isinstance(reports, (str, bytes, bytearray)):
        raise ActionPlanInputError("reports must be one report object or a sequence of reports")
    if not reports:
        raise ActionPlanInputError("at least one comment-analysis report is required")
    if any(not isinstance(report, Mapping) for report in reports):
        raise ActionPlanInputError("each report must be an object")
    return list(reports)


def _resource_entries(report: Mapping[str, Any], resource: str) -> dict[str, dict[str, Any]]:
    """Expand either detailed per-PR coverage or aggregate coverage for merging."""
    result: dict[str, dict[str, Any]] = {}
    per_pr = report.get("per_pull_request_coverage")
    repo = report.get("repository")
    repo = repo if isinstance(repo, Mapping) else {}
    repo_key = "/".join(
        (_string(repo.get("owner")) or "unknown", _string(repo.get("name")) or "unknown")
    ).casefold()
    if isinstance(per_pr, Mapping):
        for pr_id, item in per_pr.items():
            if not isinstance(item, Mapping):
                continue
            resources = item.get("resources")
            coverage = resources.get(resource) if isinstance(resources, Mapping) else None
            if not isinstance(coverage, Mapping):
                continue
            status = coverage.get("status")
            result[f"{repo_key}:{_string(item.get('pull_request_id')) or str(pr_id)}"] = {
                "status": status if isinstance(status, str) else "unknown",
                "count": coverage.get("count"),
                "complete": coverage.get("complete_history") is True,
            }
        return result

    # Backward-compatible fallback: preserve aggregates, but without per-PR
    # identities they cannot be deduplicated against another report.
    aggregate = report.get("resource_coverage")
    item = aggregate.get(resource) if isinstance(aggregate, Mapping) else None
    if isinstance(item, Mapping):
        for status, count in (item.get("pr_count_by_status") or {}).items():
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                for index in range(count):
                    key = f"{repo_key}:{resource}:{status}:{index}"
                    result[key] = {
                        "status": status,
                        "count": None,
                        "complete": status == "complete",
                    }
        return result
    return result


def build_action_plan(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a deterministic cross-repository action plan from analysis reports.

    Args:
        reports: A non-empty sequence returned by
            ``comment_analysis.analyze_comments``.

    Returns a plain JSON-compatible mapping. Source coverage is merged by
    repository and PR identity; successful-empty resources remain distinct
    from incomplete or unobserved resources.
    """
    report_list = _valid_reports(reports)
    authors = {
        author.casefold(): author
        for report in report_list
        if (author := _string(report.get("target_author")))
    }
    if len(authors) > 1:
        raise ActionPlanInputError("reports must use the same selected author")
    selected_author = next(iter(authors.values()), None)

    selected_prs: set[tuple[str, str]] = set()
    populations_known = True
    resources_by_pr: dict[str, dict[str, dict[str, Any]]] = {
        source: {} for source in COMMENT_RESOURCES
    }
    evidence_by_category: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {
        category: {} for category in TAXONOMY_ORDER
    }
    reviewer_item_identities: set[tuple[Any, ...]] = set()
    observed_role_items: dict[str, set[tuple[Any, ...]]] = {
        "reviewer": set(),
        "author_response": set(),
        "unknown": set(),
    }
    observed_role_items_by_source: dict[str, dict[str, set[tuple[Any, ...]]]] = {
        source: {
            "reviewer": set(),
            "author_response": set(),
            "unknown": set(),
        }
        for source in COMMENT_RESOURCES
    }
    observed_reviewer_item_count = 0

    for report_index, report in enumerate(report_list):
        repo = report.get("repository")
        repo = repo if isinstance(repo, Mapping) else {}
        owner, name = _string(repo.get("owner")), _string(repo.get("name"))
        if not owner or not name:
            populations_known = False
            repo_key = f"unknown-repository-{report_index}"
        else:
            repo_key = f"{owner.casefold()}/{name.casefold()}"
        population = report.get("population")
        if not isinstance(population, Mapping):
            populations_known = False
        else:
            count = population.get("pull_request_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                populations_known = False
            population_coverage = population.get("coverage")
            if (
                isinstance(population_coverage, Mapping)
                and population_coverage.get("complete_for_target_selection") is not True
            ):
                populations_known = False
            prs = population.get("pull_request_evidence")
            if isinstance(prs, Sequence) and not isinstance(prs, (str, bytes, bytearray)):
                for pr in prs:
                    if isinstance(pr, Mapping):
                        key = _identity(repo, pr.get("pull_request_id"), pr.get("pull_request_number"))
                        if key:
                            selected_prs.add(key)
                if isinstance(count, int) and len(prs) != count:
                    populations_known = False
            elif count:
                populations_known = False
            # Ensure selected population count can be represented when PR details
            # are omitted; use report-local anonymous identities without claiming
            # overlap can be deduplicated.
            if isinstance(count, int) and count > 0 and not isinstance(prs, Sequence):
                for index in range(count):
                    selected_prs.add((repo_key, f"unknown:{report_index}:{index}"))

        for resource in COMMENT_RESOURCES:
            for pr_key, coverage in _resource_entries(report, resource).items():
                previous = resources_by_pr[resource].get(pr_key)
                if previous is None:
                    resources_by_pr[resource][pr_key] = coverage
                elif previous != coverage:
                    # Conflicting repeated snapshots cannot be merged as complete.
                    resources_by_pr[resource][pr_key] = {
                        "status": "conflict",
                        "count": None,
                        "complete": False,
                    }

        inventory = report.get("comment_inventory")
        if not isinstance(inventory, Sequence) or isinstance(inventory, (str, bytes, bytearray)):
            continue
        for raw_item in inventory:
            if not isinstance(raw_item, Mapping):
                continue
            role = raw_item.get("author_role")
            if (
                role not in observed_role_items
                or raw_item.get("included_in_observed_activity") is not True
                or raw_item.get("duplicate_id_conflict") is True
            ):
                continue
            role_identity = _comment_identity(repo_key, raw_item)
            observed_role_items[role].add(role_identity)
            source = (
                _string(raw_item.get("comment_source"))
                or _string(raw_item.get("comment_type"))
                or "unknown"
            )
            if source in observed_role_items_by_source:
                observed_role_items_by_source[source][role].add(role_identity)
            if role != "reviewer":
                continue
            categories = raw_item.get("categories")
            if not isinstance(categories, Sequence) or isinstance(categories, (str, bytes, bytearray)):
                continue
            valid_categories = [category for category in categories if category in TAXONOMY_RULES]
            if not valid_categories:
                continue
            pr_key = _identity(repo, raw_item.get("pull_request_id"), raw_item.get("pull_request_number"))
            if pr_key is None:
                continue
            dedupe_identity = _comment_identity(repo_key, raw_item)
            if dedupe_identity in reviewer_item_identities:
                continue
            reviewer_item_identities.add(dedupe_identity)
            observed_reviewer_item_count += 1
            pr_canonical = _safe_https_url(raw_item.get("pull_request_url"))
            comment_canonical = _safe_https_url(raw_item.get("comment_url"))
            # Preserve original observed comment details while excluding unsafe
            # source URLs; validated canonical links are exposed separately.
            original_comment = {
                key: value for key, value in raw_item.items()
                if key not in {"pull_request_url", "comment_url"}
            }
            evidence = {
                "repository": {"owner": owner, "name": name},
                "pull_request_id": raw_item.get("pull_request_id"),
                "pull_request_number": raw_item.get("pull_request_number"),
                "comment_id": raw_item.get("comment_id"),
                "comment_source": raw_item.get("comment_source"),
                "pull_request_url": pr_canonical,
                "comment_url": comment_canonical,
                "original_comment": original_comment,
            }
            for category in valid_categories:
                evidence_by_category[category][dedupe_identity] = {
                    **evidence,
                    "_pr_identity": pr_key,
                    "_dedupe_identity": dedupe_identity,
                }

    # Count the same selected PR across reports only once. If population detail
    # is absent the count is unknown, never silently reported as zero.
    selected_pr_count: int | None = len(selected_prs) if populations_known else None

    source_coverage: dict[str, dict[str, Any]] = {}
    for resource in COMMENT_RESOURCES:
        entries = resources_by_pr[resource]
        status_counts = Counter(item["status"] for item in entries.values())
        complete_count = sum(item["complete"] for item in entries.values())
        empty_count = sum(
            item["complete"] and item["count"] == 0 for item in entries.values()
        )
        # A known empty PR population has complete zero coverage; it is not
        # missing per-PR coverage data.
        coverage_known = bool(entries) or (
            populations_known and selected_pr_count == 0
        )
        population_count = selected_pr_count
        if selected_pr_count == 0 and populations_known:
            coverage_complete = True
        else:
            coverage_complete = (
                populations_known
                and selected_pr_count is not None
                and len(entries) == selected_pr_count
                and complete_count == selected_pr_count
            )
        source_coverage[resource] = {
            "status": (
                "complete" if coverage_complete
                else "incomplete" if coverage_known
                else "unknown"
            ),
            "observed_pr_count": len(entries) if coverage_known else None,
            "selected_pr_count": selected_pr_count,
            "complete_pr_count": complete_count if coverage_known else None,
            "successful_empty_pr_count": empty_count if coverage_known else None,
            "pr_count_by_status": dict(sorted(status_counts.items())) if coverage_known else None,
            "population_complete": coverage_complete if populations_known else None,
            "definition": (
                "Per-repository/per-PR resource coverage from comment-analysis reports. "
                "A successful empty means complete history with count 0; incomplete, "
                "not-requested, unavailable, or absent coverage is not zero."
            ),
        }

    recommendations = []
    for category in TAXONOMY_ORDER:
        records = sorted(
            evidence_by_category[category].values(),
            key=lambda record: (
                str(record.get("repository", {}).get("owner", "")).casefold(),
                str(record.get("repository", {}).get("name", "")).casefold(),
                str(record.get("pull_request_id", "")),
                str(record.get("comment_source", "")),
                str(record.get("comment_id", "")),
                str(record.get("original_comment", {}).get("comment_text", "")),
            ),
        )
        if not records:
            continue
        pr_identities = {record["_pr_identity"] for record in records}
        template = TEMPLATES[category]
        excerpts = []
        seen_excerpts: set[str] = set()
        for record in records:
            original = record.get("original_comment")
            excerpt = _comment_excerpt(
                original.get("comment_text") if isinstance(original, Mapping) else None
            )
            if excerpt and excerpt.casefold() not in seen_excerpts:
                seen_excerpts.add(excerpt.casefold())
                excerpts.append(excerpt)
            if len(excerpts) == 2:
                break
        flag_summary = (
            f"{len(records)} deduplicated reviewer feedback item(s) across "
            f"{len(pr_identities)} distinct PR(s) matched the {category.replace('_', ' ')} "
            "keyword rule. This is a rule-based signal, not confirmation of a defect."
        )
        if excerpts:
            flag_summary += " Reviewer wording: " + " | ".join(
                f'“{excerpt}”' for excerpt in excerpts
            )
        affected_prs = {}
        for record in records:
            repo_record = record.get("repository", {})
            repo_label = (
                f"{repo_record.get('owner')}/{repo_record.get('name')}"
                if repo_record.get("owner") and repo_record.get("name")
                else None
            )
            identity = (
                str(repo_label or "").casefold(),
                str(record.get("pull_request_id", "")),
                str(record.get("pull_request_number", "")),
            )
            affected_prs[identity] = {
                "repository": repo_label,
                "pull_request_number": record.get("pull_request_number"),
                "pull_request_url": record.get("pull_request_url"),
            }
        recommendations.append({
            "category": category,
            "title": f"Review {template['focus']}",
            "focus": template["focus"],
            "priority_tier": RISK_TIERS[category],
            "priority_tier_name": TIER_NAMES[RISK_TIERS[category]],
            "priority_rationale": (
                f"Ordered in the {TIER_NAMES[RISK_TIERS[category]]} tier; "
                f"{len(pr_identities)} distinct affected PR(s), then "
                f"{len(records)} deduplicated reviewer evidence item(s) are the "
                "frequency tie-breakers within this tier. This is a deterministic "
                "ordering, not a severity or personal-performance score."
            ),
            "what_reviewers_flagged": flag_summary,
            "action": template["action"],
            "validation": template["validation"],
            "affected_pr_count": len(pr_identities),
            "distinct_feedback_count": len(records),
            "frequency": {
                "distinct_affected_pr_count": len(pr_identities),
                "selected_pr_denominator": selected_pr_count,
                "denominator_definition": (
                    "Distinct selected target-authored PRs represented in the input "
                    "comment-analysis reports; denominator completeness is stated in "
                    "source_coverage and is not inferred from the observed signal."
                ),
            },
            "distinct_evidence_item_count": len(records),
            "affected_prs": [
                affected_prs[key] for key in sorted(affected_prs)
            ],
            "evidence": [
                {
                    "pull_request_number": record.get("pull_request_number"),
                    "comment_text": record.get("original_comment", {}).get("comment_text"),
                    "source": record.get("comment_source"),
                    "pull_request_url": record.get("pull_request_url"),
                    "comment_url": record.get("comment_url"),
                    "pull_request_id": record.get("pull_request_id"),
                    "comment_id": record.get("comment_id"),
                    "repository": record.get("repository"),
                    "original_comment": record.get("original_comment"),
                }
                for record in records
            ],
        })

    # Explicit ordering and priority rank are stable regardless of report order.
    recommendations.sort(
        key=lambda item: (
            item["priority_tier"],
            -item["frequency"]["distinct_affected_pr_count"],
            -item["distinct_evidence_item_count"],
            TAXONOMY_ORDER.index(item["category"]),
        )
    )
    recommendations = recommendations[:5]
    for index, recommendation in enumerate(recommendations, start=1):
        recommendation["priority"] = f"P{index}"

    any_partial = any(item["status"] != "complete" for item in source_coverage.values())
    observed_reviewer_count = len(observed_role_items["reviewer"])
    any_reviewer_feedback_observed = observed_reviewer_count > 0
    any_recommendation_signal = bool(recommendations)
    if any_partial:
        if any_recommendation_signal:
            status = "incomplete_with_feedback"
            executive_summary = (
                f"Evidence coverage is incomplete. {len(recommendations)} rule-based "
                "recommendation(s) are supported by observed reviewer evidence."
            )
        elif any_reviewer_feedback_observed:
            status = "incomplete_feedback_no_taxonomy_signal"
            executive_summary = (
                "Evidence coverage is incomplete. Reviewer feedback was observed, but "
                "no taxonomy recommendation signal matched the observed reviewer comments; "
                "incomplete sources mean this does not establish that no other feedback exists."
            )
        else:
            status = "incomplete_no_feedback_observed"
            executive_summary = (
                "Evidence coverage is incomplete and no reviewer feedback was observed; "
                "this is not evidence that feedback is absent."
            )
    elif any_recommendation_signal:
        status = "recommendations_available"
        executive_summary = (
            f"{len(recommendations)} evidence-backed, rule-based recommendation(s) "
            "were derived from observed reviewer feedback."
        )
    elif any_reviewer_feedback_observed:
        status = "feedback_observed_no_taxonomy_signal"
        executive_summary = (
            "Reviewer feedback was observed, but no taxonomy recommendation signal "
            "matched the observed reviewer comments."
        )
    else:
        status = "no_feedback_found"
        executive_summary = (
            "No eligible reviewer feedback was found in the fully covered "
            "selected PR population."
        )

    population_pr_count = selected_pr_count
    data_quality = {
        "eligible_deduplicated_reviewer_evidence_item_count": observed_reviewer_item_count,
        "reviewer_feedback_count_definition": (
            "Distinct observed reviewer-role comment inventory items, including items "
            "without a known taxonomy category; excluded author/unknown roles, "
            "unobserved items, and duplicate-conflict records."
        ),
        "author_response_count_definition": (
            "Distinct observed author_response-role comment inventory items after "
            "the same source identity deduplication."
        ),
        "unknown_author_count_definition": (
            "Distinct observed unknown-role comment inventory items after the same "
            "source identity deduplication."
        ),
        "recommendation_evidence_count_definition": (
            "Deduplicated observed reviewer items with a non-empty category present "
            "in comment_analysis.TAXONOMY_RULES."
        ),
        "source_coverage_complete": not any_partial,
        "population_count_known": population_pr_count is not None,
        "excluded_duplicate_conflict_records": sum(
            1
            for report in report_list
            for item in (
                report.get("comment_inventory", [])
                if isinstance(report.get("comment_inventory", []), Sequence)
                and not isinstance(report.get("comment_inventory", []), (str, bytes, bytearray))
                else []
            )
            if isinstance(item, Mapping) and item.get("duplicate_id_conflict") is True
        ),
        "interpretation_limits": [
            "Category matches are deterministic rule-based signals, not semantic certainty or proof that a concern is valid.",
            "Incomplete, not-requested, unavailable, conflicting, and absent source coverage is unknown, not zero.",
            "No motivation, competence, personal-performance, or causal inference is made.",
        ],
    }
    return {
        "action_plan_version": ACTION_PLAN_VERSION,
        "status": status,
        "summary": executive_summary,
        "target_author": selected_author,
        "population_pr_count": population_pr_count,
        "population_pr_count_definition": (
            "Distinct repository owner/name plus pull_request_id across report PR evidence; "
            "null when the report population is unavailable or incomplete in shape."
        ),
        "reviewer_feedback_count": len(observed_role_items["reviewer"]),
        "author_response_count": len(observed_role_items["author_response"]),
        "unknown_author_count": len(observed_role_items["unknown"]),
        "observed_activity_by_source": {
            source: {
                "reviewer_feedback_count": len(role_items["reviewer"]),
                "author_response_count": len(role_items["author_response"]),
                "unknown_identity_count": len(role_items["unknown"]),
            }
            for source, role_items in observed_role_items_by_source.items()
        },
        "coverage": source_coverage,
        "recommendations": recommendations,
        "checklist": {
            "type": (
                "evidence_based_validation"
                if recommendations
                else "unavailable_without_categorized_feedback"
            ),
            "items": list(dict.fromkeys(
                item["validation"] for item in recommendations
            )),
        },
        "rule_based_label": {
            "label": "rule-based reviewer-feedback signals",
            "meaning": (
                "Deterministic matches to comment_analysis.TAXONOMY_RULES; not semantic "
                "certainty and not a claim that a reviewer concern is true."
            ),
        },
        "data_quality": data_quality,
    }


__all__ = ["ActionPlanInputError", "build_action_plan"]
