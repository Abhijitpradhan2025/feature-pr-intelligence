import copy
import unittest

from python_script.action_plan import build_action_plan
from python_script.comment_analysis import analyze_comments


START = "2026-01-01T00:00:00Z"
END = "2026-01-31T23:59:59Z"
CUTOFF = "2026-02-01T00:00:00Z"


def pr(pr_id, number, author="target"):
    return {
        "id": pr_id,
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.example.test/acme/repo/pull/{number}",
        "created_at": "2026-01-03T10:00:00Z",
        "author": {"id": 500, "login": author},
    }


def comment(comment_id, pr_id, number, body, *, login="reviewer", source="review_comments",
            created="2026-01-05T12:00:00Z", url=None, repository_name="repo"):
    item = {
        "id": comment_id,
        "repository_id": 42,
        "repository": {"owner": "acme", "name": repository_name},
        "pull_request_id": pr_id,
        "pull_request_number": number,
        "author": {"id": 700, "login": login} if login is not None else None,
        "body": body,
        "url": url or f"https://github.example.test/acme/repo/pull/{number}#discussion_r{comment_id}",
    }
    item["submitted_at" if source == "reviews" else "created_at"] = created
    item["_source"] = source
    return item


def envelope(prs, comments=(), *, owner="acme", name="repo", partial=()):
    """Build collector-v0.1-shaped fixture data for the real comment analyzer."""
    all_comments = {"review_comments": [], "issue_comments": [], "reviews": []}
    for item in comments:
        all_comments[item.pop("_source", "review_comments")].append(item)
    resources = {}
    per_pr = {}
    for source in all_comments:
        is_partial = source in partial
        resources[source] = {
            "status": "partial" if is_partial else "complete",
            "count": len(all_comments[source]),
            "pagination_complete": not is_partial,
            "history_complete": not is_partial,
        }
    for item in prs:
        key = str(item["id"])
        per_pr[key] = {}
        for source in all_comments:
            count = sum(str(record["pull_request_id"]) == key for record in all_comments[source])
            is_partial = source in partial
            per_pr[key][source] = {
                "status": "partial" if is_partial else "complete",
                "count": count,
                "pagination_complete": not is_partial,
                "history_complete": not is_partial,
            }
    return {
        "schema_version": "0.1",
        "repository": {
            "id": 42, "owner": owner, "name": name,
            "url": f"https://github.example.test/{owner}/{name}",
        },
        "requested_window": {"from": START, "to": END},
        "filters": {"author": "target"},
        "observation_cutoff": CUTOFF,
        "resources": {
            "pull_requests": {
                "status": "complete", "count": len(prs),
                "pagination_complete": True, "history_complete": True,
            },
            **resources,
        },
        "pull_requests": prs,
        "pull_request_resources": per_pr,
        **all_comments,
    }


def report(prs, comments=(), *, name="repo", partial=()):
    return analyze_comments(
        envelope(prs, list(comments), name=name, partial=partial), "target"
    )


class ActionPlanTests(unittest.TestCase):
    def test_multi_author_plan_aggregates_distinct_prs_without_duplicate_reports(self):
        prs = [pr(10, 1, author="author1"), pr(20, 2, author="author2")]
        comments = [
            comment(101, 10, 1, "Please add a test for the regression behavior"),
            comment(202, 20, 2, "Please add a test for the boundary"),
        ]
        data = envelope(prs, comments)
        data["filters"]["author"] = "author1, author2"
        analyzed = analyze_comments(data, "author1, author2")

        result = build_action_plan([analyzed, analyzed])

        recommendation = next(item for item in result["recommendations"]
                              if item["category"] == "test_coverage")
        self.assertEqual("author1, author2", result["target_author"])
        self.assertEqual(2, result["population_pr_count"])
        self.assertEqual(2, recommendation["frequency"]["distinct_affected_pr_count"])
        self.assertEqual(2, recommendation["distinct_evidence_item_count"])

    def test_many_same_category_comments_on_one_pr_count_as_one_affected_pr(self):
        prs = [pr(10, 1)]
        comments = [
            comment(1, 10, 1, "bug in this behavior"),
            comment(2, 10, 1, "incorrect output here"),
            comment(3, 10, 1, "this regression needs attention"),
        ]
        result = build_action_plan(report(prs, comments))
        recommendation = result["recommendations"][0]
        self.assertEqual("functional_defect", recommendation["category"])
        self.assertEqual(1, recommendation["frequency"]["distinct_affected_pr_count"])
        self.assertEqual(1, recommendation["frequency"]["selected_pr_denominator"])
        self.assertEqual(3, recommendation["distinct_evidence_item_count"])

    def test_recurrence_across_prs_and_repositories_is_counted_by_distinct_pr(self):
        left = report(
            [pr(10, 1), pr(20, 2)],
            [
                comment(1, 10, 1, "Please add a test"),
                comment(2, 20, 2, "Missing test for this case"),
            ],
        )
        right_pr = pr(30, 3)
        right = analyze_comments(
            envelope([right_pr], [
                comment(3, 30, 3, "Unit test needed", repository_name="other"),
            ], owner="acme", name="other"),
            "target",
        )
        result = build_action_plan([left, right])
        recommendation = next(
            item for item in result["recommendations"]
            if item["category"] == "test_coverage"
        )
        self.assertEqual(3, recommendation["frequency"]["distinct_affected_pr_count"])
        self.assertEqual(3, recommendation["frequency"]["selected_pr_denominator"])
        self.assertEqual(3, recommendation["distinct_evidence_item_count"])

    def test_priority_orders_by_declared_risk_tier_then_pr_frequency_then_item_count(self):
        prs = [pr(10, 1), pr(20, 2)]
        comments = [
            comment(1, 10, 1, "style issue"),
            comment(2, 10, 1, "security vulnerability"),
            comment(3, 10, 1, "security concern"),
            comment(4, 10, 1, "edge case"),
            comment(5, 20, 2, "bug with incorrect result"),
            comment(6, 10, 1, "bug with incorrect output"),
        ]
        result = build_action_plan(report(prs, comments))
        categories = [item["category"] for item in result["recommendations"]]
        self.assertEqual(
            ["security", "functional_defect", "edge_case", "lint_style"],
            categories,
        )
        self.assertEqual("P1", result["recommendations"][0]["priority"])
        self.assertIn("2 distinct affected PR(s)", result["recommendations"][1]["priority_rationale"])

    def test_author_unknown_unobserved_and_duplicate_conflict_items_are_excluded(self):
        prs = [pr(10, 1)]
        comments = [
            comment(1, 10, 1, "security issue"),
            comment(2, 10, 1, "security issue", login="target", source="issue_comments"),
            comment(3, 10, 1, "security issue", login=None, source="reviews"),
            comment(4, 10, 1, "security issue", created="2026-02-05T12:00:00Z"),
        ]
        analyzed = report(prs, comments)
        conflict = copy.deepcopy(analyzed["comment_inventory"][0])
        conflict["comment_id"] = 999
        conflict["duplicate_id_conflict"] = True
        conflict["comment_text"] = "secret security issue"
        analyzed["comment_inventory"].append(conflict)
        result = build_action_plan(analyzed)
        self.assertEqual(1, result["reviewer_feedback_count"])
        self.assertEqual(1, result["recommendations"][0]["distinct_evidence_item_count"])
        self.assertEqual(1, result["recommendations"][0]["evidence"][0]["comment_id"])

    def test_complete_empty_feedback_is_distinct_from_partial_no_observations(self):
        empty = report([pr(10, 1)])
        empty_result = build_action_plan(empty)
        self.assertEqual("no_feedback_found", empty_result["status"])
        self.assertEqual([], empty_result["recommendations"])
        self.assertIn("No eligible", empty_result["summary"])

        partial = report([pr(10, 1)], partial=("reviews",))
        partial_result = build_action_plan(partial)
        self.assertEqual("incomplete_no_feedback_observed", partial_result["status"])
        self.assertEqual([], partial_result["recommendations"])
        self.assertNotIn("No eligible", partial_result["summary"])

    def test_known_empty_pr_population_has_zero_coverage_counts_not_unknown(self):
        result = build_action_plan(report([]))

        self.assertEqual(0, result["population_pr_count"])
        for source in ("reviews", "review_comments", "issue_comments"):
            with self.subTest(source=source):
                coverage = result["coverage"][source]
                self.assertEqual("complete", coverage["status"])
                self.assertEqual(0, coverage["observed_pr_count"])
                self.assertEqual(0, coverage["selected_pr_count"])
                self.assertEqual(0, coverage["complete_pr_count"])
                self.assertEqual(0, coverage["successful_empty_pr_count"])
                self.assertEqual({}, coverage["pr_count_by_status"])

    def test_unclassified_reviewer_feedback_is_observed_without_recommendations(self):
        analyzed = report(
            [pr(10, 1)],
            [comment(20, 10, 1, "A note without any matching taxonomy phrase")],
        )
        result = build_action_plan(analyzed)
        self.assertEqual(1, result["reviewer_feedback_count"])
        self.assertEqual([], result["recommendations"])
        self.assertEqual("feedback_observed_no_taxonomy_signal", result["status"])
        self.assertIn("Reviewer feedback was observed", result["summary"])
        self.assertIn("no taxonomy recommendation signal matched", result["summary"])
        self.assertEqual([], result["checklist"]["items"])
        self.assertEqual(
            "unavailable_without_categorized_feedback",
            result["checklist"]["type"],
        )

    def test_observed_activity_is_counted_by_source_and_commenter_role(self):
        analyzed = report(
            [pr(10, 1)],
            [
                comment(31, 10, 1, "Please add a test", source="review_comments"),
                comment(32, 10, 1, "I added a test", login="target", source="issue_comments"),
                comment(33, 10, 1, "A review note", login=None, source="reviews"),
            ],
        )

        result = build_action_plan(analyzed)

        self.assertEqual(
            {
                "reviewer_feedback_count": 0,
                "author_response_count": 0,
                "unknown_identity_count": 1,
            },
            result["observed_activity_by_source"]["reviews"],
        )
        self.assertEqual(
            {
                "reviewer_feedback_count": 1,
                "author_response_count": 0,
                "unknown_identity_count": 0,
            },
            result["observed_activity_by_source"]["review_comments"],
        )
        self.assertEqual(
            {
                "reviewer_feedback_count": 0,
                "author_response_count": 1,
                "unknown_identity_count": 0,
            },
            result["observed_activity_by_source"]["issue_comments"],
        )

    def test_unclassified_reviewer_feedback_keeps_partial_coverage_warning(self):
        analyzed = report(
            [pr(10, 1)],
            [comment(21, 10, 1, "A note without any matching taxonomy phrase")],
            partial=("reviews",),
        )
        result = build_action_plan(analyzed)
        self.assertEqual(1, result["reviewer_feedback_count"])
        self.assertEqual([], result["recommendations"])
        self.assertEqual("incomplete_feedback_no_taxonomy_signal", result["status"])
        self.assertIn("Evidence coverage is incomplete", result["summary"])
        self.assertIn("Reviewer feedback was observed", result["summary"])
        self.assertIn("no taxonomy recommendation signal matched", result["summary"])

    def test_partial_coverage_preserves_observed_recommendations_and_warns(self):
        analyzed = report(
            [pr(10, 1)],
            [comment(5, 10, 1, "error handling should be clearer")],
            partial=("reviews",),
        )
        result = build_action_plan(analyzed)
        self.assertEqual("incomplete_with_feedback", result["status"])
        self.assertEqual("incomplete", result["coverage"]["reviews"]["status"])
        self.assertEqual("complete", result["coverage"]["review_comments"]["status"])
        self.assertEqual("exception_handling", result["recommendations"][0]["category"])

    def test_evidence_links_are_safe_and_comments_are_deduplicated_with_stable_fallback(self):
        analyzed = report([pr(10, 1)], [comment(7, 10, 1, "Please add a test")])
        original = analyzed["comment_inventory"][0]
        analyzed["comment_inventory"].append(copy.deepcopy(original))
        # No ID fallback uses a stable signature; the repeated record still emits once.
        fallback = copy.deepcopy(original)
        fallback["comment_id"] = None
        analyzed["comment_inventory"].extend([fallback, copy.deepcopy(fallback)])
        unsafe = copy.deepcopy(original)
        unsafe["comment_id"] = 8
        unsafe["comment_url"] = "javascript:alert(1)"
        unsafe["pull_request_url"] = "https://user:pass@github.example.test/pr/1"
        analyzed["comment_inventory"].append(unsafe)

        result = build_action_plan(analyzed)
        evidence = result["recommendations"][0]["evidence"]
        self.assertEqual(3, len(evidence))
        self.assertEqual(1, sum(item["comment_id"] == 7 for item in evidence))
        self.assertEqual(1, sum(item["comment_id"] is None for item in evidence))
        unsafe_record = next(item for item in evidence if item["comment_id"] == 8)
        self.assertIsNone(unsafe_record["comment_url"])
        self.assertIsNone(unsafe_record["pull_request_url"])
        self.assertTrue(all(
            "javascript:" not in str(item["comment_url"])
            and "user:pass@" not in str(item["pull_request_url"])
            for item in evidence
        ))
        self.assertEqual(
            "Please add a test",
            evidence[0]["comment_text"],
        )

    def test_recommendation_retains_traceable_evidence_and_builds_specific_checklist(self):
        result = build_action_plan(report(
            [pr(10, 1)],
            [comment(12, 10, 1, "Please add a test for this path")],
        ))
        recommendation = result["recommendations"][0]
        source = recommendation["evidence"][0]
        self.assertEqual(12, source["comment_id"])
        self.assertEqual(10, source["pull_request_id"])
        self.assertEqual("Please add a test for this path",
                         source["comment_text"])
        self.assertEqual("review_comments", source["source"])
        self.assertIn("Please add a test for this path",
                      recommendation["what_reviewers_flagged"])
        self.assertIn("1 deduplicated reviewer feedback item",
                      recommendation["what_reviewers_flagged"])
        self.assertIn("rule-based signal", recommendation["what_reviewers_flagged"])
        self.assertEqual(1, recommendation["affected_pr_count"])
        self.assertEqual(1, recommendation["distinct_feedback_count"])
        self.assertEqual("https://github.example.test/acme/repo/pull/1",
                         recommendation["affected_prs"][0]["pull_request_url"])
        self.assertEqual("evidence_based_validation", result["checklist"]["type"])
        self.assertEqual([recommendation["validation"]], result["checklist"]["items"])

    def test_successful_empty_resource_coverage_is_merged_explicitly(self):
        result = build_action_plan([
            report([pr(10, 1)]),
            analyze_comments(
                envelope([pr(20, 2)], owner="acme", name="other"), "target"
            ),
        ])
        coverage = result["coverage"]["review_comments"]
        self.assertEqual("complete", coverage["status"])
        self.assertEqual(2, coverage["observed_pr_count"])
        self.assertEqual(2, coverage["successful_empty_pr_count"])
        self.assertEqual({"complete": 2}, coverage["pr_count_by_status"])

    def test_requested_dashboard_contract_fields_and_observed_role_counts(self):
        analyzed = report(
            [pr(10, 1)],
            [
                comment(1, 10, 1, "Please add a test"),
                comment(2, 10, 1, "I added a test", login="target", source="issue_comments"),
                comment(3, 10, 1, "unattributed note", login=None, source="reviews"),
            ],
        )
        result = build_action_plan(analyzed)
        for key in (
            "target_author", "status", "summary", "recommendations", "checklist",
            "coverage", "population_pr_count", "reviewer_feedback_count",
            "author_response_count", "unknown_author_count", "data_quality",
        ):
            self.assertIn(key, result)
        self.assertEqual("target", result["target_author"])
        self.assertEqual(1, result["population_pr_count"])
        self.assertEqual(1, result["reviewer_feedback_count"])
        self.assertEqual(1, result["author_response_count"])
        self.assertEqual(1, result["unknown_author_count"])
        required = {
            "category", "title", "focus", "priority", "priority_rationale",
            "what_reviewers_flagged", "affected_pr_count", "distinct_feedback_count",
            "action", "validation", "affected_prs", "evidence",
        }
        self.assertTrue(required.issubset(result["recommendations"][0]))
        self.assertTrue({
            "pull_request_number", "comment_text", "source",
            "pull_request_url", "comment_url",
        }.issubset(result["recommendations"][0]["evidence"][0]))

    def test_input_reports_must_agree_on_selected_author(self):
        first = report([pr(10, 1)])
        second = copy.deepcopy(first)
        second["target_author"] = "someone-else"
        with self.assertRaises(ValueError):
            build_action_plan([first, second])


if __name__ == "__main__":
    unittest.main()
