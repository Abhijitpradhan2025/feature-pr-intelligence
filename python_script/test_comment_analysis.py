import copy
import unittest

from comment_analysis import CommentAnalysisInputError, analyze_comments


START = "2026-01-01T00:00:00Z"
END = "2026-01-31T23:59:59Z"
CUTOFF = "2026-02-01T00:00:00Z"


def pr(pr_id, number, author="target", created=START):
    return {
        "id": pr_id,
        "number": number,
        "title": f"PR {number}",
        "url": f"https://example.test/repo/pull/{number}",
        "created_at": created,
        "author": {"id": 500, "login": author},
    }


def comment(comment_id, pr_id, number, *, source="review_comments",
            login="reviewer", body="please add a test", created="2026-01-05T12:00:00Z",
            **extra):
    return {
        "id": comment_id,
        "repository_id": 42,
        "repository": {"owner": "acme", "name": "repo"},
        "pull_request_id": pr_id,
        "pull_request_number": number,
        "author": {"id": 700, "login": login} if login is not None else None,
        "created_at": created,
        "body": body,
        "url": f"https://example.test/comment/{comment_id}",
        **extra,
    }


def envelope(prs=None):
    prs = [pr(10, 1)] if prs is None else prs
    return {
        "schema_version": "0.1",
        "repository": {
            "id": 42, "owner": "acme", "name": "repo",
            "url": "https://example.test/acme/repo",
        },
        "requested_window": {"from": START, "to": END},
        "filters": {"author": "target"},
        "observation_cutoff": CUTOFF,
        "fetched_at": CUTOFF,
        "status": "partial",
        "resources": {
            "pull_requests": {
                "status": "complete", "count": len(prs),
                "pagination_complete": True, "history_complete": True,
            },
            "review_comments": {"status": "complete", "count": 0},
            "issue_comments": {"status": "complete", "count": 0},
            "reviews": {"status": "complete", "count": 0},
        },
        "pull_requests": prs,
        "pull_request_resources": {
            str(item["id"]): {
                resource: {
                    "status": "complete",
                    "count": 0,
                    "pagination_complete": True,
                    "history_complete": True,
                }
                for resource in ("review_comments", "issue_comments", "reviews")
            }
            for item in prs
        },
        "review_comments": [],
        "issue_comments": [],
        "reviews": [],
    }


class CommentAnalysisTests(unittest.TestCase):
    def test_multiple_selected_authors_share_one_deduplicated_report(self):
        data = envelope([
            pr(10, 1, author="target"),
            pr(20, 2, author="second"),
            pr(30, 3, author="outside"),
        ])
        data["filters"]["author"] = "target, second"
        data["review_comments"] = [
            comment(101, 10, 1),
            comment(202, 20, 2, body="Please handle the error case"),
        ]
        data["resources"]["review_comments"]["count"] = 2
        data["pull_request_resources"]["10"]["review_comments"]["count"] = 1
        data["pull_request_resources"]["20"]["review_comments"]["count"] = 1

        result = analyze_comments(data, " target, second, target ")

        self.assertEqual("target, second", result["target_author"])
        self.assertEqual(2, result["population"]["pull_request_count"])
        self.assertEqual({101, 202}, {item["comment_id"] for item in result["comment_inventory"]})
        self.assertEqual(2, result["comment_activity"]["observed_reviewer_comment_count"])

    def test_resource_coverage_distinguishes_empty_unavailable_and_not_requested(self):
        data = envelope([pr(10, 1), pr(20, 2), pr(30, 3)])
        data["pull_request_resources"]["20"]["review_comments"] = {
            "status": "unavailable", "count": None,
            "pagination_complete": False, "history_complete": False,
        }
        data["pull_request_resources"]["30"]["issue_comments"] = {
            "status": "not_requested", "count": None,
            "pagination_complete": False, "history_complete": False,
        }
        data["pull_request_resources"]["20"]["reviews"] = {
            "status": "unavailable", "count": None,
            "pagination_complete": False, "history_complete": False,
        }
        data["pull_request_resources"]["30"]["reviews"] = {
            "status": "not_requested", "count": None,
            "pagination_complete": False, "history_complete": False,
        }
        data["resources"]["issue_comments"]["status"] = "partial"
        data["resources"]["reviews"] = {
            "status": "partial", "count": 0,
            "pagination_complete": False, "history_complete": False,
        }
        result = analyze_comments(data, "target")
        review_coverage = result["resource_coverage"]["review_comments"]
        issue_coverage = result["resource_coverage"]["issue_comments"]
        self.assertEqual(2, review_coverage["successful_empty_pr_count"])
        self.assertEqual(1, review_coverage["pr_count_by_status"]["unavailable"])
        self.assertEqual(1, issue_coverage["pr_count_by_status"]["not_requested"])
        self.assertEqual(2, issue_coverage["successful_empty_pr_count"])
        reviews_coverage = result["resource_coverage"]["reviews"]
        self.assertEqual(1, reviews_coverage["pr_count_by_status"]["unavailable"])
        self.assertEqual(1, reviews_coverage["pr_count_by_status"]["not_requested"])
        self.assertEqual(1, reviews_coverage["successful_empty_pr_count"])
        self.assertIsNone(result["per_pull_request_coverage"]["20"]["resources"]["reviews"]["count"])
        self.assertFalse(result["data_quality"]["comment_resource_coverage_complete_for_population"])
        self.assertIsNone(result["per_pull_request_coverage"]["20"]["resources"]["review_comments"]["count"])
        self.assertTrue(
            result["per_pull_request_coverage"]["10"]["all_comment_sources_complete"]
        )

        not_requested = envelope([pr(10, 1)])
        not_requested["resources"]["reviews"] = {
            "status": "not_requested", "count": None,
            "pagination_complete": False, "history_complete": False,
        }
        del not_requested["reviews"]
        not_requested["pull_request_resources"]["10"].pop("reviews")
        not_requested_result = analyze_comments(not_requested, "target")
        self.assertEqual(
            1, not_requested_result["resource_coverage"]["reviews"]["pr_count_by_status"]["not_requested"]
        )
        self.assertEqual(
            0, not_requested_result["resource_coverage"]["reviews"]["successful_empty_pr_count"]
        )

    def test_empty_population_is_not_reported_as_complete_source_history(self):
        data = envelope([])
        result = analyze_comments(data, "target")
        for source in ("reviews", "review_comments", "issue_comments"):
            self.assertEqual(0, result["resource_coverage"][source]["population_pr_count"])
            self.assertEqual(0, result["resource_coverage"][source]["complete_pr_count"])
            self.assertFalse(result["resource_coverage"][source]["population_complete"])
            self.assertEqual(0, result["resource_coverage"][source]["successful_empty_pr_count"])
        self.assertFalse(
            result["data_quality"]["comment_resource_coverage_complete_for_population"]
        )

    def test_author_responses_are_separate_and_reviewer_metadata_is_linked(self):
        data = envelope()
        data["review_comments"] = [
            comment(1, 10, 1, body="Please add a test", path="src/a.py", line=9,
                    in_reply_to_id=77),
            comment(2, 10, 1, source="issue_comments", login="target",
                    body="I added the test"),
        ]
        result = analyze_comments(data, "TARGET")
        self.assertEqual(1, result["comment_activity"]["observed_reviewer_comment_count"])
        self.assertEqual(1, result["comment_activity"]["observed_author_response_count"])
        reviewer = result["comment_inventory"][0]
        self.assertEqual("reviewer", reviewer["author_role"])
        self.assertEqual(10, reviewer["pull_request_id"])
        self.assertEqual("src/a.py", reviewer["path"])
        self.assertEqual(9, reviewer["line"])
        self.assertEqual(77, reviewer["in_reply_to_id"])
        self.assertEqual("Please add a test", reviewer["comment_text"])
        self.assertEqual("not_assessed", reviewer["addressed_status"])
        self.assertEqual("https://example.test/repo/pull/1", reviewer["pull_request_url"])

    def test_feedback_after_creation_window_end_is_included_for_selected_prs_only(self):
        data = envelope([pr(10, 1), pr(20, 2, created="2026-02-01T00:00:00Z")])
        data["observation_cutoff"] = "2026-02-10T00:00:00Z"
        data["review_comments"] = [
            comment(
                101, 10, 1, body="Please cover this edge case",
                created="2026-02-02T12:00:00Z",
            ),
            comment(
                102, 20, 2, body="Please add a test",
                created="2026-02-03T12:00:00Z",
            ),
        ]
        data["reviews"] = [{
            "id": 103,
            "pull_request_id": 10,
            "repository_id": 42,
            "pull_request_number": 1,
            "author": {"id": 700, "login": "reviewer"},
            "submitted_at": "2026-02-04T12:00:00Z",
            "state": "CHANGES_REQUESTED",
            "body": "Please add a test for the edge case",
            "url": "https://example.test/review/103",
        }]
        data["issue_comments"] = [
            comment(
                104, 10, 1, source="issue_comments", login="target",
                body="I added the requested test",
                created="2026-02-05T12:00:00Z",
            )
        ]
        data["resources"]["review_comments"]["count"] = 2
        data["resources"]["reviews"]["count"] = 1
        data["resources"]["issue_comments"]["count"] = 1
        data["pull_request_resources"]["10"]["review_comments"]["count"] = 1
        data["pull_request_resources"]["10"]["reviews"]["count"] = 1
        data["pull_request_resources"]["10"]["issue_comments"]["count"] = 1

        result = analyze_comments(data, "target")

        self.assertEqual(1, result["population"]["pull_request_count"])
        self.assertEqual({1}, {
            item["pull_request_number"] for item in result["comment_inventory"]
        })
        self.assertTrue(all(
            item["observed_at"] > END
            for item in result["comment_inventory"]
        ))
        self.assertEqual(2, result["comment_activity"]["observed_reviewer_comment_count"])
        self.assertEqual(1, result["comment_activity"]["observed_author_response_count"])
        self.assertEqual(
            1,
            result["categories"]["edge_case"]["affected_pr_count"],
        )

    def test_submitted_review_summary_is_feedback_with_own_source_time_state_and_dedupe_namespace(self):
        data = envelope()
        data["review_comments"] = [
            comment(4, 10, 1, body="security issue", created="2026-01-05T12:00:00Z")
        ]
        data["reviews"] = [
            {
                "id": 4,
                "repository_id": 42,
                "repository": {"owner": "acme", "name": "repo"},
                "pull_request_id": 10,
                "pull_request_number": 1,
                "author": {"id": 700, "login": "reviewer"},
                "submitted_at": "2026-01-06T12:00:00Z",
                "state": "CHANGES_REQUESTED",
                "body": "Please fix the security vulnerability",
                "url": "https://example.test/reviews/4",
            },
            {
                "id": 5,
                "repository_id": 42,
                "pull_request_id": 10,
                "pull_request_number": 1,
                "author": {"id": 700, "login": "reviewer"},
                "submitted_at": "2026-02-02T12:00:00Z",
                "state": "COMMENTED",
                "body": "future review body",
                "url": "https://example.test/reviews/5",
            },
        ]
        data["resources"]["review_comments"]["count"] = 1
        data["resources"]["reviews"]["count"] = 2
        data["pull_request_resources"]["10"]["reviews"]["count"] = 2
        data["pull_request_resources"]["10"]["review_comments"]["count"] = 1
        result = analyze_comments(data, "target")
        self.assertEqual(2, result["comment_activity"]["observed_reviewer_comment_count"])
        review_summary = next(
            item for item in result["comment_inventory"]
            if item["comment_source"] == "reviews"
        )
        self.assertEqual("review_summary", review_summary["comment_type"])
        self.assertEqual("2026-01-06T12:00:00Z", review_summary["submitted_at"])
        self.assertEqual("2026-01-06T12:00:00Z", review_summary["observed_at"])
        self.assertEqual("submitted_at", review_summary["timestamp_field"])
        self.assertEqual("CHANGES_REQUESTED", review_summary["review_state"])
        self.assertIn("security", review_summary["categories"])
        self.assertEqual("not_assessed", review_summary["addressed_status"])
        self.assertEqual(1, result["comment_activity"]["observed_review_summary_count"])
        self.assertNotIn("Please fix the security vulnerability", repr(result["categories"]))
        future_review = next(
            item for item in result["comment_inventory"] if item["comment_id"] == 5
        )
        self.assertEqual("after_observation_cutoff", future_review["timestamp_status"])
        self.assertNotIn("comment_text", future_review)
        self.assertEqual(0, result["data_quality"]["duplicate_comment_records_omitted_by_stable_id"])

    def test_multiple_categories_and_category_summary_never_contains_text(self):
        data = envelope()
        data["review_comments"] = [
            comment(1, 10, 1, body="Bug: add a test for this edge case")
        ]
        result = analyze_comments(data, "target")
        self.assertIn("functional_defect", result["categories"])
        self.assertEqual(1, result["categories"]["functional_defect"]["comment_count"])
        self.assertEqual(1, result["categories"]["test_coverage"]["comment_count"])
        self.assertEqual(1, result["categories"]["edge_case"]["comment_count"])
        self.assertEqual("Bug: add a test for this edge case",
                         result["comment_inventory"][0]["comment_text"])
        summary_text = repr(result["categories"])
        self.assertNotIn("Bug:", summary_text)
        self.assertNotIn("add a test for this edge case", summary_text)
        self.assertEqual(100.0, result["categories"]["test_coverage"]["percentage"]["value"])

    def test_api_contract_and_conditional_logic_review_signals(self):
        data = envelope()
        data["review_comments"] = [
            comment(1, 10, 1, body="Please keep the API contract response shape compatible."),
            comment(2, 10, 1, body="This inverted condition misses the false branch."),
        ]

        result = analyze_comments(data, "target")

        self.assertIn(
            "api_contract",
            next(item for item in result["comment_inventory"] if item["comment_id"] == 1)["categories"],
        )
        self.assertIn(
            "conditional_logic",
            next(item for item in result["comment_inventory"] if item["comment_id"] == 2)["categories"],
        )
        self.assertEqual(1, result["categories"]["api_contract"]["comment_count"])
        self.assertEqual(1, result["categories"]["conditional_logic"]["comment_count"])

    def test_unclassified_and_no_false_addressed_state_with_reply(self):
        data = envelope()
        data["review_comments"] = [
            comment(1, 10, 1, body="Could we consider this?")
        ]
        data["issue_comments"] = [
            comment(2, 10, 1, source="issue_comments", login="target",
                    body="Done", in_reply_to_id=1)
        ]
        result = analyze_comments(data, "target")
        reviewer = result["comment_inventory"][0]
        self.assertEqual(["other_unclassified"], reviewer["categories"])
        self.assertEqual("not_assessed", reviewer["addressed_status"])
        self.assertEqual(1, result["categories"]["other_unclassified"]["comment_count"])
        self.assertEqual(1, result["comment_activity"]["observed_author_response_count"])

    def test_stable_comment_id_dedupes_matching_records_only(self):
        data = envelope()
        duplicate = comment(8, 10, 1, body="add a test")
        data["review_comments"] = [duplicate, copy.deepcopy(duplicate)]
        result = analyze_comments(data, "target")
        self.assertEqual(1, len(result["comment_inventory"]))
        self.assertEqual(1, result["data_quality"]["duplicate_comment_records_omitted_by_stable_id"])
        self.assertEqual(1, result["categories"]["test_coverage"]["comment_count"])

        data["review_comments"].append(comment(8, 10, 1, body="different evidence"))
        conflicted = analyze_comments(data, "target")
        self.assertEqual(2, len(conflicted["comment_inventory"]))
        self.assertEqual(1, conflicted["data_quality"]["duplicate_id_conflict_records_retained"])
        self.assertTrue(conflicted["comment_inventory"][-1]["duplicate_id_conflict"])
        self.assertEqual(1, conflicted["comment_activity"]["observed_reviewer_comment_count"])
        self.assertEqual(2, conflicted["comment_activity"]["summary_comment_count"])
        self.assertTrue(conflicted["comment_inventory"][-1]["included_in_summary"])
        self.assertEqual("different evidence", conflicted["comment_inventory"][-1]["summary_text"])

    def test_missing_timestamps_are_counted_not_assumed_in_cutoff_population(self):
        data = envelope()
        data["review_comments"] = [
            comment(1, 10, 1, created=None, body="missing time"),
            comment(2, 10, 1, created="not-a-time", body="invalid time"),
            comment(3, 10, 1, created="2026-02-02T00:00:00Z", body="future"),
        ]
        result = analyze_comments(data, "target")
        self.assertEqual(0, result["comment_activity"]["observed_reviewer_comment_count"])
        self.assertEqual(2, result["comment_activity"]["comments_missing_or_invalid_timestamp"])
        self.assertEqual(1, result["comment_activity"]["comments_after_observation_cutoff"])
        self.assertNotIn("comment_text", result["comment_inventory"][0])
        self.assertNotIn("comment_text", result["comment_inventory"][2])

    def test_summary_inventory_retains_text_outside_observed_timestamp_window(self):
        data = envelope()
        data["review_comments"] = [
            comment(101, 10, 1, body="Missing timestamp feedback", created=None),
            comment(
                102, 10, 1, body="Feedback posted during collection",
                created="2026-02-02T00:00:00Z",
            ),
            comment(
                103, 10, 1, body="Invalid timestamp feedback",
                created="not-a-timestamp",
            ),
        ]
        result = analyze_comments(data, "target")
        self.assertEqual(0, result["comment_activity"]["observed_comment_count"])
        self.assertEqual(3, result["comment_activity"]["summary_comment_count"])
        summary_items = [
            item for item in result["comment_inventory"] if item["included_in_summary"]
        ]
        self.assertEqual(
            ["missing", "after_observation_cutoff", "invalid"],
            [item["timestamp_status"] for item in summary_items],
        )
        self.assertEqual(
            [
                "Missing timestamp feedback",
                "Feedback posted during collection",
                "Invalid timestamp feedback",
            ],
            [item["summary_text"] for item in summary_items],
        )
        self.assertFalse(any("comment_text" in item for item in summary_items))

    def test_records_cannot_be_silently_discarded_under_unavailable_source_status(self):
        for status in ("not_requested", "unavailable", "error"):
            data = envelope()
            incomplete = {
                "status": status,
                "count": None,
                "pagination_complete": False,
                "history_complete": False,
            }
            data["resources"]["review_comments"] = dict(incomplete)
            data["pull_request_resources"]["10"]["review_comments"] = dict(incomplete)
            data["review_comments"] = [
                comment(101, 10, 1, body="Collected text must not be lost")
            ]
            with self.subTest(status=status):
                with self.assertRaises(CommentAnalysisInputError):
                    analyze_comments(data, "target")

    def test_missing_comment_identity_is_observed_but_not_assigned_to_reviewer(self):
        data = envelope()
        data["review_comments"] = [
            comment(1, 10, 1, login=None, body="bug found")
        ]
        result = analyze_comments(data, "target")
        self.assertEqual(1, result["comment_activity"]["observed_unknown_author_count"])
        self.assertEqual(0, result["comment_activity"]["observed_reviewer_comment_count"])
        self.assertEqual(0, result["categories"]["functional_defect"]["comment_count"])
        self.assertEqual("unknown", result["comment_inventory"][0]["author_role"])
        self.assertEqual("bug found", result["comment_inventory"][0]["comment_text"])

    def test_recurrence_uses_only_complete_prs_as_explicit_denominator(self):
        data = envelope([pr(10, 1), pr(20, 2), pr(30, 3)])
        data["pull_request_resources"]["30"]["issue_comments"] = {
            "status": "partial", "count": 0,
            "pagination_complete": True, "history_complete": False,
        }
        data["review_comments"] = [
            comment(1, 10, 1, body="bug found"),
            comment(2, 20, 2, body="bug found"),
            comment(3, 30, 3, body="bug found"),
        ]
        result = analyze_comments(data, "target")
        recurrence = result["categories"]["functional_defect"]["recurrence_by_pr"]
        self.assertEqual(2, recurrence["affected_pr_count"])
        self.assertEqual(2, recurrence["denominator"])
        self.assertEqual(100.0, recurrence["percentage"])
        self.assertTrue(recurrence["recurring_in_at_least_two_prs"])
        self.assertEqual(3, result["categories"]["functional_defect"]["comment_count"])

    def test_records_from_other_pr_or_repository_are_not_attached(self):
        data = envelope()
        data["review_comments"] = [
            comment(1, 999, 9),
            comment(2, 10, 1, repository_id=999),
        ]
        result = analyze_comments(data, "target")
        self.assertEqual([], result["comment_inventory"])
        self.assertEqual(2, sum(result["data_quality"]["excluded_comment_record_count_by_reason"].values()))
        self.assertEqual(1, result["data_quality"]["repository_or_PR_link_mismatch_count"]["repository_id"])

    def test_population_and_envelope_schema_are_validated(self):
        data = envelope([pr(10, 1), pr(20, 2, author="someone")])
        result = analyze_comments(data, "target")
        self.assertEqual(1, result["population"]["pull_request_count"])
        self.assertEqual(1, result["population"]["excluded_pull_request_count_by_reason"]["different_author"])
        data["schema_version"] = "unsupported"
        with self.assertRaises(CommentAnalysisInputError):
            analyze_comments(data, "target")

    def test_mismatched_collection_author_filter_is_rejected(self):
        data = envelope()
        data["filters"]["author"] = "someone-else"
        with self.assertRaisesRegex(CommentAnalysisInputError, "author filter excludes"):
            analyze_comments(data, "target")


if __name__ == "__main__":
    unittest.main()
