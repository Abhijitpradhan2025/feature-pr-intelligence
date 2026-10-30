import unittest

from metrics import AnalyticsInputError, analyze_repository


START = "2026-01-01T00:00:00Z"
CUTOFF = "2026-01-10T00:00:00Z"
FETCHED = CUTOFF
RESOURCES = (
    "pull_requests", "lifecycle_events", "commits", "reviews",
    "review_comments", "issue_comments",
)
ARRAYS = {
    "lifecycle_events": "events",
    "commits": "commits",
    "reviews": "reviews",
    "review_comments": "review_comments",
    "issue_comments": "issue_comments",
}


def pr(pr_id, number, created=START, author_id=1, additions=3, deletions=2,
       merged_at=None):
    return {
        "id": pr_id,
        "number": number,
        "created_at": created,
        "author": {"id": author_id} if author_id is not None else None,
        "additions": additions,
        "deletions": deletions,
        "changed_files": 2,
        "merged_at": merged_at,
        "url": f"https://example.invalid/repo/pull/{number}",
        "body": "synthetic body must not be copied",
    }


def envelope(prs=None):
    prs = [pr(10, 1, merged_at="2026-01-04T00:00:00Z")] if prs is None else prs
    return {
        "repository": {
            "id": 99, "owner": "synthetic", "name": "sample",
            "url": "https://example.invalid/synthetic/sample",
        },
        "requested_window": {"from": START, "to": "2026-01-05T00:00:00Z"},
        "filters": {"author": None},
        "fetched_at": FETCHED,
        "observation_cutoff": CUTOFF,
        "status": "complete",
        "resources": {
            name: {
                "status": "complete", "count": len(prs),
                "pagination_complete": True, "history_complete": True,
            }
            for name in RESOURCES
        },
        "pull_requests": prs,
        "pull_request_resources": {
            str(item["id"]): {
                name: {
                    "status": "complete", "count": 1,
                    "pagination_complete": True, "history_complete": True,
                }
                for name in RESOURCES
            }
            for item in prs
        },
        "events": [],
        "commits": [],
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "warnings": [],
    }


def activity(pr_id, record_id, when, **fields):
    return {
        "id": record_id, "pull_request_id": pr_id, "url": f"https://example.invalid/item/{record_id}",
        **fields,
        **({"submitted_at": when} if "state" in fields else {}),
    }


def set_resource(data, pr_id, resource, status="partial", history=False, pagination=False):
    data["pull_request_resources"][str(pr_id)][resource] = {
        "status": status,
        "count": None if status in {"unavailable", "not_requested"} else 0,
        "pagination_complete": pagination,
        "history_complete": history,
    }
    data["resources"][resource]["status"] = status
    data["resources"][resource]["history_complete"] = False
    data["resources"][resource]["pagination_complete"] = False


class MetricsTests(unittest.TestCase):
    def test_supported_formulas_and_output_omits_private_text(self):
        first = pr(10, 1, merged_at="2026-01-04T00:00:00Z")
        second = pr(20, 2, created="2026-01-02T00:00:00Z", additions=7, deletions=1,
                    author_id=3)
        data = envelope([first, second])
        data["reviews"] = [
            activity(10, 100, "2026-01-01T01:00:00Z", state="APPROVED",
                     author={"id": 1}, body="do not emit"),
            activity(10, 101, "2026-01-02T00:00:00Z", state="PENDING",
                     author={"id": 5}),
            activity(10, 102, "2026-01-03T00:00:00Z", state="CHANGES_REQUESTED",
                     author={"id": 2}, body="private"),
            activity(20, 103, "2026-01-03T00:00:00Z", state="COMMENTED",
                     author={"id": 4}),
        ]
        data["events"] = [
            {"id": 200, "pull_request_id": 10, "kind": "reopened",
             "occurred_at": "2026-01-05T00:00:00Z", "url": "https://example.invalid/e/200"},
            {"id": 201, "pull_request_id": 10, "kind": "closed",
             "occurred_at": "2026-01-06T00:00:00Z"},
        ]
        data["commits"] = [
            {"sha": "abc", "pull_request_id": 10, "committed_at": "2026-01-02T00:00:00Z",
             "url": "https://example.invalid/c/abc", "message": "private commit message"},
            {"sha": "def", "pull_request_id": 10, "committed_at": "2026-01-11T00:00:00Z"},
        ]
        data["reviews"][2]["url"] = "https://example.invalid/reviews/102"
        data["reviews"][3]["url"] = "https://example.invalid/reviews/103"
        result = analyze_repository(data)
        metrics = result["metrics"]
        self.assertEqual(6.5, metrics["pr_size"]["value"])
        self.assertEqual((2, 2), (metrics["pr_size"]["numerator"], metrics["pr_size"]["denominator"]))
        self.assertEqual(1.0, metrics["pr_size"]["coverage_fraction"])
        self.assertEqual(5.0, metrics["pr_size"]["median_additions"])
        self.assertEqual(2, metrics["first_submitted_review_latency"]["latency_observation_count"])
        self.assertEqual(129600.0, metrics["first_submitted_review_latency"]["value"])
        self.assertEqual(2, metrics["first_submitted_review_latency"]["denominator"])
        self.assertEqual(259200.0, metrics["merge_latency"]["value"])
        self.assertEqual(1, metrics["merge_latency"]["merged_pr_count"])
        self.assertEqual(1, metrics["reopened_event_count"]["value"])
        self.assertEqual(1, metrics["commit_count"]["total_valid_commits"])
        self.assertEqual(0.5, metrics["commit_count"]["value"])
        self.assertNotIn("private", repr(result))
        self.assertNotIn("body", repr(result))
        self.assertEqual("https://example.invalid/reviews/102",
                         metrics["first_submitted_review_latency"]["evidence"][0]["url"])
        self.assertEqual("abc", metrics["commit_count"]["evidence"][0]["record_id"])
        self.assertEqual("seconds", metrics["merge_latency"]["unit"])

    def test_resource_completeness_controls_denominator_not_empty_array(self):
        data = envelope([pr(10, 1), pr(20, 2)])
        set_resource(data, 20, "commits")
        result = analyze_repository(data)["metrics"]["commit_count"]
        self.assertEqual(1, result["covered_pr_count"])
        self.assertEqual(1, result["denominator"])
        self.assertEqual(1, result["unknown_pr_count"])
        self.assertEqual("partial", result["status"])
        self.assertEqual(0.0, result["value"])
        unavailable = envelope([pr(10, 1)])
        set_resource(unavailable, 10, "commits", "not_requested")
        unavailable["resources"]["commits"]["status"] = "not_requested"
        metric = analyze_repository(unavailable)["metrics"]["commit_count"]
        self.assertEqual("unavailable", metric["status"])
        self.assertIsNone(metric["value"])
        self.assertEqual("no_complete_commit_histories", metric["unavailable_reason"])

    def test_complete_empty_resources_are_zero_observations(self):
        metrics = analyze_repository(envelope([pr(10, 1)]))["metrics"]
        self.assertEqual(0, metrics["reopened_event_count"]["value"])
        self.assertEqual(0, metrics["reopened_event_count"]["numerator"])
        self.assertEqual(1, metrics["reopened_event_count"]["denominator"])
        self.assertEqual(0.0, metrics["commit_count"]["value"])
        self.assertEqual("complete", metrics["commit_count"]["status"])
        self.assertIsNone(metrics["adjacent_activity_gap"]["value"])
        self.assertEqual("no_positive_adjacent_pairs",
                         metrics["adjacent_activity_gap"]["unavailable_reason"])

    def test_review_requires_known_distinct_author_and_eligible_state(self):
        data = envelope([pr(10, 1, author_id=None)])
        data["reviews"] = [
            activity(10, 1, "2026-01-02T00:00:00Z", state="APPROVED", author={"id": 2}),
            activity(10, 2, "2026-01-02T00:00:00Z", state="COMMENTED", author=None),
            activity(10, 3, "2026-01-02T00:00:00Z", state="DISMISSED", author={"id": 3}),
        ]
        metric = analyze_repository(data)["metrics"]["first_submitted_review_latency"]
        self.assertIsNone(metric["value"])
        self.assertEqual(2, metric["invalid_counts"]["unknown_identity_reviews"])
        self.assertEqual(1, metric["denominator"])
        self.assertEqual("no_eligible_external_submitted_reviews", metric["unavailable_reason"])

    def test_missing_malformed_naive_and_negative_review_timestamps_count_unknown(self):
        data = envelope([pr(10, 1)])
        data["reviews"] = [
            activity(10, 1, None, state="APPROVED", author={"id": 2}),
            activity(10, 2, "bad timestamp", state="COMMENTED", author={"id": 2}),
            activity(10, 3, "2026-01-01T01:00:00", state="COMMENTED", author={"id": 2}),
            activity(10, 4, "2025-12-31T23:59:00Z", state="CHANGES_REQUESTED", author={"id": 2}),
        ]
        metric = analyze_repository(data)["metrics"]["first_submitted_review_latency"]
        self.assertEqual(1, metric["invalid_counts"]["missing_review_timestamps"])
        self.assertEqual(2, metric["invalid_counts"]["invalid_review_timestamps"])
        self.assertEqual(1, metric["invalid_counts"]["negative_review_intervals"])
        self.assertIsNone(metric["value"])

    def test_historical_cutoff_disables_mutable_merge_metadata(self):
        data = envelope([pr(10, 1, merged_at="2026-01-04T00:00:00Z")])
        data["observation_cutoff"] = "2026-01-05T00:00:00Z"
        data["fetched_at"] = "2026-01-10T00:00:00Z"
        data["warnings"].append("Historical observation cutoff requested.")
        metric = analyze_repository(data)["metrics"]["merge_latency"]
        self.assertEqual("unavailable", metric["status"])
        self.assertIsNone(metric["value"])
        self.assertEqual("historical_metadata_not_reconstructable", metric["unavailable_reason"])
        size = analyze_repository(data)["metrics"]["pr_size"]
        self.assertEqual("unavailable", size["status"])
        self.assertIsNone(size["value"])
        self.assertEqual("historical_metadata_not_reconstructable", size["unavailable_reason"])

    def test_current_collection_cutoff_before_fetch_does_not_masquerade_as_historical_as_of(self):
        data = envelope([pr(10, 1, merged_at="2026-01-04T00:00:00Z")])
        data["observation_cutoff"] = "2026-01-05T00:00:00Z"
        data["fetched_at"] = "2026-01-10T00:00:00Z"
        data["warnings"] = []
        metrics = analyze_repository(data)["metrics"]
        self.assertEqual("complete", metrics["pr_size"]["status"])
        self.assertEqual("complete", metrics["merge_latency"]["status"])
        self.assertEqual(5.0, metrics["pr_size"]["value"])
        self.assertEqual(259200.0, metrics["merge_latency"]["value"])

    def test_merge_cutoff_excludes_future_timestamp_and_unmerged_pr(self):
        data = envelope([
            pr(10, 1, merged_at="2026-01-09T00:00:00Z"),
            pr(20, 2, created="2026-01-02T00:00:00Z", merged_at=None),
        ])
        metric = analyze_repository(data)["metrics"]["merge_latency"]
        self.assertEqual(1, metric["merged_pr_count"])
        self.assertEqual(2, metric["denominator"])
        self.assertEqual("complete", metric["status"])
        self.assertEqual(1, metric["invalid_counts"]["unmerged_prs_excluded"])

    def test_reopened_events_obey_cutoff_and_invalid_timestamps_are_counted(self):
        data = envelope([pr(10, 1)])
        data["events"] = [
            {"id": 1, "pull_request_id": 10, "kind": "reopened", "occurred_at": CUTOFF},
            {"id": 2, "pull_request_id": 10, "kind": "reopened", "occurred_at": "2026-01-11T00:00:00Z"},
            {"id": 3, "pull_request_id": 10, "kind": "reopened", "occurred_at": None},
            {"id": 4, "pull_request_id": 10, "kind": "reopened", "occurred_at": "2026-01-03T00:00:00"},
        ]
        metric = analyze_repository(data)["metrics"]["reopened_event_count"]
        self.assertEqual(1, metric["value"])
        self.assertEqual(1, metric["invalid_counts"]["missing_event_timestamps"])
        self.assertEqual(1, metric["invalid_counts"]["invalid_event_timestamps"])
        self.assertEqual(1, len(metric["evidence"]))

    def test_commit_timestamps_and_cutoff(self):
        data = envelope([pr(10, 1)])
        data["commits"] = [
            {"sha": "a", "pull_request_id": 10, "committed_at": CUTOFF},
            {"sha": "b", "pull_request_id": 10, "committed_at": "2026-01-11T00:00:00Z"},
            {"sha": "c", "pull_request_id": 10, "committed_at": None},
            {"sha": "d", "pull_request_id": 10, "committed_at": "2026-01-02T00:00:00"},
        ]
        metric = analyze_repository(data)["metrics"]["commit_count"]
        self.assertEqual(1, metric["value"])
        self.assertEqual(1, metric["invalid_counts"]["missing_commit_timestamps"])
        self.assertEqual(1, metric["invalid_counts"]["invalid_commit_timestamps"])
        self.assertEqual("a", metric["evidence"][0]["record_id"])

    def test_adjacent_gaps_stay_within_pr_and_omit_ties(self):
        data = envelope([pr(10, 1), pr(20, 2, created="2026-01-02T00:00:00Z")])
        data["commits"] = [
            {"sha": "a", "pull_request_id": 10, "committed_at": "2026-01-02T00:00:00Z"},
            {"sha": "b", "pull_request_id": 10, "committed_at": "2026-01-02T00:00:00Z"},
            {"sha": "c", "pull_request_id": 10, "committed_at": "2026-01-03T00:00:00Z"},
            {"sha": "d", "pull_request_id": 20, "committed_at": "2026-01-03T00:00:00Z"},
        ]
        metric = analyze_repository(data)["metrics"]["adjacent_activity_gap"]
        self.assertEqual(1, metric["valid_pair_count"])
        self.assertEqual(86400.0, metric["value"])
        self.assertEqual(1, metric["invalid_counts"]["equal_timestamp_adjacent_pairs_omitted"])
        self.assertEqual(2, metric["covered_pr_count"])
        self.assertIn("attribution", metric)
        self.assertEqual("observational only; no author, reviewer, or CI attribution",
                         metric["attribution"])
        self.assertIn("from", metric["evidence"][0])
        self.assertIn("to", metric["evidence"][0])

    def test_gaps_unavailable_if_any_of_five_histories_incomplete(self):
        data = envelope([pr(10, 1)])
        set_resource(data, 10, "review_comments")
        metric = analyze_repository(data)["metrics"]["adjacent_activity_gap"]
        self.assertEqual("unavailable", metric["status"])
        self.assertEqual(0, metric["denominator"])
        self.assertEqual(1, metric["unknown_pr_count"])
        self.assertIsNone(metric["value"])

    def test_empty_population_is_not_numeric_zero(self):
        data = envelope([])
        metrics = analyze_repository(data)["metrics"]
        self.assertEqual(0, metrics["pr_size"]["population_count"])
        self.assertIsNone(metrics["pr_size"]["value"])
        self.assertIsNone(metrics["pr_size"]["coverage_fraction"])
        self.assertEqual("zero_population", metrics["pr_size"]["unavailable_reason"])
        self.assertIsNone(metrics["first_submitted_review_latency"]["value"])
        self.assertIsNone(metrics["reopened_event_count"]["value"])
        self.assertIsNone(metrics["commit_count"]["value"])
        self.assertEqual("zero_population", metrics["commit_count"]["unavailable_reason"])

    def test_population_window_is_inclusive_and_bad_creation_time_is_error(self):
        data = envelope([
            pr(10, 1, created=START),
            pr(20, 2, created="2026-01-05T00:00:00Z"),
            pr(30, 3, created="2026-01-06T00:00:00Z"),
        ])
        result = analyze_repository(data)
        self.assertEqual(2, result["population_count"])
        data["pull_requests"][0]["created_at"] = "not a timestamp"
        with self.assertRaisesRegex(AnalyticsInputError, "population is ambiguous"):
            analyze_repository(data)

    def test_incomplete_population_or_error_envelope_raises(self):
        data = envelope([])
        data["resources"]["pull_requests"]["status"] = "partial"
        with self.assertRaisesRegex(AnalyticsInputError, "population is not complete"):
            analyze_repository(data)
        data["resources"]["pull_requests"]["status"] = "complete"
        data["status"] = "error"
        with self.assertRaisesRegex(AnalyticsInputError, "error status"):
            analyze_repository(data)

    def test_complete_resource_without_array_is_not_silent_empty(self):
        data = envelope([pr(10, 1)])
        del data["commits"]
        with self.assertRaisesRegex(AnalyticsInputError, "despite complete per-PR"):
            analyze_repository(data)

    def test_activity_without_stable_population_link_is_not_counted_as_zero(self):
        data = envelope([pr(10, 1)])
        data["commits"] = [{"sha": "missing-link", "committed_at": CUTOFF}]
        with self.assertRaisesRegex(AnalyticsInputError, "without a population PR link"):
            analyze_repository(data)

    def test_activity_from_another_repository_is_rejected(self):
        data = envelope([pr(10, 1)])
        data["repository"]["id"] = 99
        data["commits"] = [{
            "sha": "foreign", "pull_request_id": 10, "repository_id": 100,
            "committed_at": CUTOFF,
        }]
        with self.assertRaisesRegex(AnalyticsInputError, "different repository"):
            analyze_repository(data)


if __name__ == "__main__":
    unittest.main()
