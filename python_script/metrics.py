"""Deterministic, evidence-preserving analytics for one repository envelope.

Only contract-supported metrics are emitted. Activity text is intentionally never
copied into output. Durations are elapsed UTC seconds, not business time.
"""

from datetime import datetime, timezone
from statistics import median
from collections.abc import Mapping, Sequence


METRIC_VERSION = "pr-analytics-v1"
UTC = timezone.utc

_ACTIVITY = {
    "lifecycle_events": ("events", "occurred_at"),
    "commits": ("commits", "committed_at"),
    "reviews": ("reviews", "submitted_at"),
    "review_comments": ("review_comments", "created_at"),
    "issue_comments": ("issue_comments", "created_at"),
}
_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
_GAP_RESOURCES = tuple(_ACTIVITY)


class AnalyticsInputError(ValueError):
    """The repository result is malformed or insufficiently scoped to analyze."""


def _parse_timestamp(value):
    """Return (UTC datetime or None, issue), where issue is missing/invalid/None."""
    if value is None:
        return None, "missing"
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None, "invalid"
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        else:
            return None, "invalid"
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, "invalid"
        return parsed.astimezone(UTC), None
    except (ValueError, OverflowError):
        return None, "invalid"


def _required_mapping(value, name):
    if not isinstance(value, Mapping):
        raise AnalyticsInputError(f"{name} must be an object")
    return value


def _required_sequence(value, name):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AnalyticsInputError(f"{name} must be an array")
    if any(not isinstance(item, Mapping) for item in value):
        raise AnalyticsInputError(f"{name} must contain only objects")
    return value


def _median_or_none(values):
    return median(values) if values else None


def _common(name, definition, envelope, population, numerator, denominator,
            unit, value, status, invalid_counts, evidence, **extra):
    repo = envelope["repository"]
    return {
        "name": name,
        "definition_version": METRIC_VERSION,
        "definition": definition,
        "repository": {
            "repository_id": repo.get("id"),
            "owner": repo.get("owner"),
            "name": repo.get("name"),
            "url": repo.get("url"),
        },
        "creation_window": dict(envelope["requested_window"]),
        "author_filter": envelope.get("filters", {}).get("author"),
        "observation_cutoff": envelope["observation_cutoff"],
        "fetched_at": envelope["fetched_at"],
        "population_count": len(population),
        "population_definition": (
            "PRs in pull_requests whose created_at is within the inclusive "
            "requested_window; the envelope's author filter has already been applied"
        ),
        "status": status,
        "numerator": numerator,
        "denominator": denominator,
        "invalid_counts": dict(invalid_counts),
        "unit": unit,
        "time_semantics": "elapsed UTC wall-clock time; includes nights, weekends, and holidays"
        if unit in {"seconds", "seconds per adjacent pair"} else None,
        "value": value,
        "evidence": evidence,
        **extra,
    }


def _resource_complete(envelope, pr, resource):
    aggregate = envelope.get("resources", {}).get(resource)
    if not isinstance(aggregate, Mapping) or aggregate.get("status") not in {"complete", "partial"}:
        return False
    mapping = envelope.get("pull_request_resources")
    if not isinstance(mapping, Mapping):
        return False
    per_pr = mapping.get(str(pr.get("id")))
    if not isinstance(per_pr, Mapping):
        return False
    record = per_pr.get(resource)
    return (
        isinstance(record, Mapping)
        and record.get("status") == "complete"
        and record.get("history_complete") is True
        and record.get("pagination_complete") is True
        and isinstance(record.get("count"), int)
        and not isinstance(record.get("count"), bool)
        and record.get("count") >= 0
    )


def _activity_for_pr(envelope, pr, resource):
    array_name, _ = _ACTIVITY[resource]
    records = envelope.get(array_name)
    if records is None:
        if _resource_complete(envelope, pr, resource):
            raise AnalyticsInputError(
                f"{array_name} is missing despite complete per-PR {resource} history"
            )
        # Missing/not-requested is not an empty successful response.
        return None, 0
    records = _required_sequence(records, array_name)
    result, unlinked = [], 0
    pr_id = str(pr.get("id"))
    population_ids = {str(item.get("id")) for item in envelope["pull_requests"]}
    for record in records:
        linked_id = record.get("pull_request_id")
        if linked_id is None or str(linked_id) not in population_ids:
            raise AnalyticsInputError(
                f"{array_name} contains an activity record without a population PR link"
            )
        envelope_repository_id = envelope["repository"].get("id")
        record_repository_id = record.get("repository_id")
        if (
            envelope_repository_id is not None
            and record_repository_id is not None
            and str(envelope_repository_id) != str(record_repository_id)
        ):
            raise AnalyticsInputError(
                f"{array_name} contains a record from a different repository"
            )
        if str(linked_id) == pr_id:
            result.append(record)
    return result, unlinked


def _evidence_pr(pr):
    return {
        "pr_id": pr.get("id"),
        "pr_number": pr.get("number"),
        "url": pr.get("url"),
    }


def _activity_evidence(pr, kind, record, timestamp_field, timestamp=None):
    return {
        **_evidence_pr(pr),
        "kind": kind,
        "record_id": record.get("id") if record.get("id") is not None else record.get("sha"),
        "timestamp": record.get(timestamp_field),
        "actor_id": (record.get("author") or record.get("actor") or {}).get("id")
        if isinstance(record.get("author") or record.get("actor"), Mapping) else None,
        "url": record.get("url"),
        "source_url": record.get("source_url"),
    }


def _base_envelope(envelope):
    if not isinstance(envelope, Mapping):
        raise AnalyticsInputError("repository result must be an object")
    if envelope.get("status") == "error":
        raise AnalyticsInputError("repository result has error status")
    for field in ("repository", "requested_window", "resources"):
        _required_mapping(envelope.get(field), field)
    for field in ("fetched_at", "observation_cutoff"):
        if field not in envelope:
            raise AnalyticsInputError(f"missing envelope field: {field}")
        parsed, issue = _parse_timestamp(envelope[field])
        if issue:
            raise AnalyticsInputError(f"{field} must be a valid timezone-aware timestamp")
        if field == "fetched_at":
            fetched = parsed
        else:
            cutoff = parsed
    window = envelope["requested_window"]
    start, start_issue = _parse_timestamp(window.get("from"))
    end, end_issue = _parse_timestamp(window.get("to"))
    if start_issue or end_issue or start > end:
        raise AnalyticsInputError("requested_window must contain valid ordered UTC timestamps")
    pull_requests = _required_sequence(envelope.get("pull_requests"), "pull_requests")
    pr_resource = envelope["resources"].get("pull_requests")
    if not isinstance(pr_resource, Mapping) or pr_resource.get("status") != "complete":
        raise AnalyticsInputError("pull_requests population is not complete")
    if pr_resource.get("pagination_complete") is not True or pr_resource.get("history_complete") is not True:
        raise AnalyticsInputError("pull_requests pagination is incomplete")
    population = []
    seen_ids = set()
    for pr in pull_requests:
        if pr.get("id") is None:
            raise AnalyticsInputError("each pull request requires a stable id")
        if str(pr["id"]) in seen_ids:
            raise AnalyticsInputError("duplicate pull request id")
        seen_ids.add(str(pr["id"]))
        created, issue = _parse_timestamp(pr.get("created_at"))
        if issue:
            raise AnalyticsInputError(f"PR {pr['id']} has invalid created_at; population is ambiguous")
        if start <= created <= end:
            population.append(pr)
    return population, fetched, cutoff


def _status(covered, total):
    if total == 0:
        return "complete"
    if covered == total:
        return "complete"
    return "partial" if covered else "unavailable"


def _historical_metadata(envelope):
    warnings = envelope.get("warnings", [])
    return any(
        isinstance(w, str) and "historical observation cutoff" in w.lower()
        for w in warnings
    ) if isinstance(warnings, Sequence) else False


def analyze_repository(envelope):
    """Return six deterministic metrics from a single repository result envelope.

    Raises AnalyticsInputError for malformed population/envelope inputs. Missing
    activity resources reduce eligible coverage and are never interpreted as zero.
    """
    population, fetched, cutoff = _base_envelope(envelope)
    historical_metadata = _historical_metadata(envelope)
    output = {
        "metric_version": METRIC_VERSION,
        "repository": dict(envelope["repository"]),
        "creation_window": dict(envelope["requested_window"]),
        "author_filter": envelope.get("filters", {}).get("author"),
        "observation_cutoff": envelope["observation_cutoff"],
        "population_count": len(population),
        "metrics": {},
        "unsupported_metrics": [
            "review_rounds", "author_response_interval", "reviewer_follow_up_interval",
            "time_to_closure", "rework_after_feedback", "ci_wait",
        ],
    }
    metrics = output["metrics"]

    # Current PR-size snapshot.
    size_values, size_evidence, size_missing = [], [], 0
    size_additions, size_deletions = [], []
    for pr in population:
        additions, deletions = pr.get("additions"), pr.get("deletions")
        valid = (
            isinstance(additions, int) and not isinstance(additions, bool) and additions >= 0
            and isinstance(deletions, int) and not isinstance(deletions, bool) and deletions >= 0
        )
        if valid:
            size_values.append(additions + deletions)
            size_additions.append(additions)
            size_deletions.append(deletions)
            size_evidence.append({
                **_evidence_pr(pr), "additions": additions, "deletions": deletions,
                "changed_files": pr.get("changed_files"), "fetched_at": envelope["fetched_at"],
            })
        else:
            size_missing += 1
    size_status = "unavailable" if historical_metadata else _status(len(size_values), len(population))
    metrics["pr_size"] = _common(
        "pr_size", "Current snapshot: additions + deletions per PR; report median.",
        envelope, population, len(size_values), len(population), "changed lines",
        None if historical_metadata else _median_or_none(size_values), size_status,
        {"missing_size_prs": size_missing},
        size_evidence, covered_pr_count=len(size_values),
        coverage_fraction=(len(size_values) / len(population)
                           if population and not historical_metadata else None),
        median_additions=_median_or_none(size_additions),
        median_deletions=_median_or_none(size_deletions),
        median_changed_files=_median_or_none([
            pr["changed_files"] for pr in population
            if isinstance(pr.get("changed_files"), int) and not isinstance(pr.get("changed_files"), bool)
            and pr["changed_files"] >= 0
        ]),
        changed_files_valid_count=sum(
            isinstance(pr.get("changed_files"), int)
            and not isinstance(pr.get("changed_files"), bool)
            and pr["changed_files"] >= 0 for pr in population
        ),
        changed_files_coverage_denominator=len(population),
        unavailable_reason="historical_metadata_not_reconstructable" if historical_metadata else (
            "zero_population" if not population else
            "no_valid_size_observations" if not size_values else None
        ),
        snapshot_only=True,
        historical_metadata_unavailable=historical_metadata,
    )

    # First eligible external submitted review.
    complete_prs, review_latencies, review_evidence = [], [], []
    unknown_review_records = missing_review_timestamps = invalid_review_timestamps = 0
    negative_review_intervals = unlinked_review_records = 0
    for pr in population:
        if not _resource_complete(envelope, pr, "reviews"):
            continue
        complete_prs.append(pr)
        records, unlinked = _activity_for_pr(envelope, pr, "reviews")
        unlinked_review_records += unlinked
        if records is None:
            continue
        created, _ = _parse_timestamp(pr["created_at"])
        author = pr.get("author")
        author_id = author.get("id") if isinstance(author, Mapping) else None
        candidates = []
        for review in records:
            state = str(review.get("state") or "").upper()
            if state not in _REVIEW_STATES:
                continue
            reviewer = review.get("author")
            reviewer_id = reviewer.get("id") if isinstance(reviewer, Mapping) else None
            if author_id is None or reviewer_id is None:
                unknown_review_records += 1
                continue
            if str(reviewer_id) == str(author_id):
                continue
            submitted, issue = _parse_timestamp(review.get("submitted_at"))
            if issue == "missing":
                missing_review_timestamps += 1
                continue
            if issue:
                invalid_review_timestamps += 1
                continue
            if submitted < created:
                negative_review_intervals += 1
                continue
            if submitted > cutoff:
                continue
            candidates.append((submitted, review))
        if candidates:
            submitted, review = min(candidates, key=lambda item: item[0])
            review_latencies.append((submitted - created).total_seconds())
            review_evidence.append({
                **_evidence_pr(pr),
                "review_id": review.get("id"),
                "state": review.get("state"),
                "pr_author_id": author_id,
                "review_author_id": (review.get("author") or {}).get("id"),
                "created_at": pr.get("created_at"),
                "submitted_at": review.get("submitted_at"),
                "url": review.get("url"),
                "source_url": review.get("source_url"),
            })
    review_den = len(complete_prs)
    metrics["first_submitted_review_latency"] = _common(
        "first_submitted_review_latency",
        "Earliest submitted APPROVED/CHANGES_REQUESTED/COMMENTED review by a known, non-PR-author identity, minus PR creation.",
        envelope, population, len(review_latencies), review_den, "seconds",
        _median_or_none(review_latencies), _status(review_den, len(population)),
        {"unknown_identity_reviews": unknown_review_records,
         "missing_review_timestamps": missing_review_timestamps,
         "invalid_review_timestamps": invalid_review_timestamps,
         "negative_review_intervals": negative_review_intervals,
         "unlinked_records": unlinked_review_records},
        review_evidence, covered_pr_count=review_den,
        latency_observation_count=len(review_latencies),
        coverage_fraction=(len(review_latencies) / review_den if review_den else None),
        unknown_pr_count=len(population) - review_den,
        unavailable_reason=("zero_population" if not population else
                            "no_complete_review_histories" if not review_den else
                            "no_eligible_external_submitted_reviews" if not review_latencies else None),
    )

    # Merge latency uses mutable current metadata and is never backdated.
    historical = historical_metadata
    merge_items, merge_invalid = [], 0
    if not historical:
        for pr in population:
            if pr.get("merged_at") is None:
                continue  # Current unmerged PRs are deliberately excluded.
            merged, issue = _parse_timestamp(pr.get("merged_at"))
            created, _ = _parse_timestamp(pr.get("created_at"))
            if issue or merged < created or merged > cutoff:
                merge_invalid += 1
                continue
            merge_items.append((merged, pr, created))
    merge_values = [(merged - created).total_seconds() for merged, _, created in merge_items]
    merge_evidence = [{
        **_evidence_pr(pr), "created_at": pr.get("created_at"),
        "merged_at": pr.get("merged_at"), "url": pr.get("url"),
        "current_snapshot_fetched_at": envelope["fetched_at"],
    } for _, pr, _ in merge_items]
    metrics["merge_latency"] = _common(
        "merge_latency", "Current-at-fetch merged_at minus created_at; merged PRs only.",
        envelope, population, len(merge_values), len(population), "seconds",
        _median_or_none(merge_values),
        "unavailable" if historical else "complete",
        {"invalid_or_negative_merge_timestamps": merge_invalid,
         "unmerged_prs_excluded": sum(pr.get("merged_at") is None for pr in population)},
        merge_evidence, merged_pr_count=len(merge_values), historical_metadata_unavailable=historical,
        unavailable_reason=("historical_metadata_not_reconstructable" if historical else
                            "zero_population" if not population else
                            "no_merged_prs" if not merge_values else None),
    )

    # Reopened events, complete lifecycle histories only.
    life_covered, reopened_total, reopened_counts, reopened_ev = [], 0, [], []
    missing_life_ts = invalid_life_ts = unlinked_life = 0
    for pr in population:
        if not _resource_complete(envelope, pr, "lifecycle_events"):
            continue
        life_covered.append(pr)
        records, unlinked = _activity_for_pr(envelope, pr, "lifecycle_events")
        unlinked_life += unlinked
        count = 0
        for event in records or []:
            if str(event.get("kind") or "").lower() != "reopened":
                continue
            occurred, issue = _parse_timestamp(event.get("occurred_at"))
            if issue == "missing":
                missing_life_ts += 1
            elif issue:
                invalid_life_ts += 1
            elif occurred <= cutoff:
                count += 1
                reopened_ev.append(_activity_evidence(pr, "lifecycle_events", event, "occurred_at"))
        reopened_counts.append(count)
        reopened_total += count
    metrics["reopened_event_count"] = _common(
        "reopened_event_count", "Count of observed reopened lifecycle events no later than cutoff.",
        envelope, population, reopened_total, len(life_covered), "events",
        reopened_total if life_covered else None, _status(len(life_covered), len(population)),
        {"missing_event_timestamps": missing_life_ts, "invalid_event_timestamps": invalid_life_ts,
         "unlinked_records": unlinked_life},
        reopened_ev, covered_pr_count=len(life_covered),
        coverage_fraction=len(life_covered) / len(population) if population else None,
        unknown_pr_count=len(population) - len(life_covered),
        event_count_distribution=reopened_counts,
        unavailable_reason=("zero_population" if not population else
                            "no_complete_lifecycle_histories" if not life_covered else None),
    )

    # Commit count: complete histories, count only timestamped commits <= cutoff.
    commit_covered, commit_counts, commit_evidence = [], [], []
    missing_commit_ts = invalid_commit_ts = unlinked_commits = 0
    for pr in population:
        if not _resource_complete(envelope, pr, "commits"):
            continue
        commit_covered.append(pr)
        records, unlinked = _activity_for_pr(envelope, pr, "commits")
        unlinked_commits += unlinked
        count = 0
        for commit in records or []:
            committed, issue = _parse_timestamp(commit.get("committed_at"))
            if issue == "missing":
                missing_commit_ts += 1
            elif issue:
                invalid_commit_ts += 1
            elif committed <= cutoff:
                count += 1
                commit_evidence.append(_activity_evidence(pr, "commits", commit, "committed_at"))
        commit_counts.append(count)
    metrics["commit_count"] = _common(
        "commit_count", "Per PR, count commits with valid committed_at no later than cutoff; report median.",
        envelope, population, sum(commit_counts), len(commit_covered), "commits per PR",
        _median_or_none(commit_counts), _status(len(commit_covered), len(population)),
        {"missing_commit_timestamps": missing_commit_ts, "invalid_commit_timestamps": invalid_commit_ts,
         "unlinked_records": unlinked_commits},
        commit_evidence, covered_pr_count=len(commit_covered),
        coverage_fraction=len(commit_covered) / len(population) if population else None,
        total_valid_commits=sum(commit_counts), median_commits_per_covered_pr=_median_or_none(commit_counts),
        unknown_pr_count=len(population) - len(commit_covered),
        unavailable_reason=("zero_population" if not population else
                            "no_complete_commit_histories" if not commit_covered else None),
    )

    # Within-PR adjacent activity gaps across all five complete resources.
    gap_covered, gaps, gap_evidence = [], [], []
    missing_gap_ts = invalid_gap_ts = tied_pairs = negative_gap_intervals = unlinked_gap = 0
    for pr in population:
        if not all(_resource_complete(envelope, pr, resource) for resource in _GAP_RESOURCES):
            continue
        gap_covered.append(pr)
        records_for_pr = []
        for resource, (array_name, timestamp_field) in _ACTIVITY.items():
            records, unlinked = _activity_for_pr(envelope, pr, resource)
            unlinked_gap += unlinked
            for record in records or []:
                stamp, issue = _parse_timestamp(record.get(timestamp_field))
                if issue == "missing":
                    missing_gap_ts += 1
                elif issue:
                    invalid_gap_ts += 1
                elif stamp <= cutoff:
                    records_for_pr.append((stamp, resource, record, timestamp_field))
        records_for_pr.sort(key=lambda row: (
            row[0], row[1], str(row[2].get("id", row[2].get("sha", "")))
        ))
        for left, right in zip(records_for_pr, records_for_pr[1:]):
            seconds = (right[0] - left[0]).total_seconds()
            if seconds == 0:
                tied_pairs += 1
                continue
            if seconds < 0:  # Defensive; sorted UTC timestamps should make this impossible.
                negative_gap_intervals += 1
                continue
            gaps.append(seconds)
            gap_evidence.append({
                **_evidence_pr(pr),
                "gap_seconds": seconds,
                "from": _activity_evidence(pr, left[1], left[2], left[3]),
                "to": _activity_evidence(pr, right[1], right[2], right[3]),
            })
    metrics["adjacent_activity_gap"] = _common(
        "adjacent_activity_gap",
        "Positive elapsed gaps between consecutive valid timestamped records within a PR across lifecycle events, reviews, review comments, issue comments, and commits.",
        envelope, population, len(gaps), len(gap_covered), "seconds per adjacent pair",
        _median_or_none(gaps), _status(len(gap_covered), len(population)),
        {"missing_activity_timestamps": missing_gap_ts,
         "invalid_activity_timestamps": invalid_gap_ts,
         "equal_timestamp_adjacent_pairs_omitted": tied_pairs,
         "negative_intervals": negative_gap_intervals,
         "unlinked_records": unlinked_gap},
        gap_evidence, covered_pr_count=len(gap_covered),
        coverage_fraction=len(gap_covered) / len(population) if population else None,
        unknown_pr_count=len(population) - len(gap_covered),
        valid_pair_count=len(gaps),
        unavailable_reason=("zero_population" if not population else
                            "no_complete_five_resource_histories" if not gap_covered else
                            "no_positive_adjacent_pairs" if not gaps else None),
        attribution="observational only; no author, reviewer, or CI attribution",
    )
    return output
