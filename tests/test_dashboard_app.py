"""Focused UI presentation and safe-boundary tests; no network or Streamlit install required."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "dashboard" / "app.py"
fake_streamlit = types.ModuleType("streamlit")
fake_streamlit.column_config = types.SimpleNamespace(LinkColumn=Mock())
with patch.dict(sys.modules, {"streamlit": fake_streamlit}):
    spec = importlib.util.spec_from_file_location("dashboard_app_under_test", APP_PATH)
    dashboard_app = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(dashboard_app)


class DashboardPresentationTests(unittest.TestCase):
    def test_sidebar_navigation_routes_to_all_six_pages(self):
        routes = {
            "Overview": "render_portfolio_page",
            "Repository Analytics": "render_repository_page",
            "People Analytics": "render_people_page",
            "PR Explorer": "render_explorer_page",
            "Quality & Reliability": "render_quality_page",
            "Insights & Recommendations": "render_insights_page",
        }
        self.assertEqual(tuple(routes), dashboard_app.PAGES)
        for page, renderer_name in routes.items():
            with self.subTest(page=page), patch.object(
                dashboard_app, renderer_name
            ) as renderer:
                dashboard_app.render_page(page, {}, {}, [])
                renderer.assert_called_once()

    def test_explorer_page_passes_selected_analysis_to_pr_detail(self):
        envelope = {"pull_requests": []}
        analysis = {"metrics": {"merge_latency": {"evidence": []}}}
        with patch.object(
            dashboard_app, "_selected_result", return_value=("owner/repo", envelope, analysis)
        ), patch.object(dashboard_app.st, "header", Mock(), create=True), patch.object(
            dashboard_app.st, "caption", Mock(), create=True
        ), patch.object(dashboard_app, "render_explorer") as render_explorer:
            dashboard_app.render_explorer_page({"owner/repo": (envelope, analysis)})

        render_explorer.assert_called_once_with(envelope, analysis)

    def test_action_plan_author_is_shared_with_people_analytics(self):
        with patch.object(dashboard_app, "render_people_page") as render_people:
            dashboard_app.render_page(
                "People Analytics", {}, {}, [], target_author="selected-author"
            )

        render_people.assert_called_once_with({}, target_author="selected-author")

    def test_portfolio_overview_uses_complete_real_pr_outcomes_and_timestamps(self):
        results = {
            "owner/repo": (
                {
                    "status": "complete",
                    "resources": {
                        "pull_requests": {
                            "status": "complete",
                            "pagination_complete": True,
                            "count": 3,
                        }
                    },
                    "pull_requests": [
                        {
                            "created_at": "2026-01-03T00:00:00Z",
                            "state": "closed",
                            "merged_at": "2026-02-01T00:00:00Z",
                        },
                        {
                            "created_at": "2026-01-09T00:00:00Z",
                            "state": "closed",
                            "merged_at": None,
                        },
                        {
                            "created_at": "2026-02-15T00:00:00Z",
                            "state": "open",
                            "merged_at": None,
                        },
                    ],
                },
                {"population_count": 3},
            )
        }

        overview = dashboard_app.build_portfolio_overview(results, {})

        self.assertIsNotNone(overview)
        self.assertEqual(
            {"Merged": 1, "Closed unmerged": 1, "Open": 1},
            overview["outcomes"],
        )
        self.assertEqual(3, overview["population_count"])
        self.assertEqual(2, overview["activity_trend_month_count"])
        self.assertIn(
            {"month": "2026-02-01", "series": "Merged", "count": 1},
            overview["trend"],
        )

    def test_portfolio_overview_withholds_charts_for_incomplete_population(self):
        envelope = {
            "status": "partial",
            "resources": {
                "pull_requests": {
                    "status": "partial",
                    "pagination_complete": False,
                    "count": 1,
                }
            },
            "pull_requests": [{"created_at": "2026-01-03T00:00:00Z", "state": "open"}],
        }
        self.assertIsNone(
            dashboard_app.build_portfolio_overview(
                {"owner/repo": (envelope, {"population_count": 1})},
                {},
            )
        )

    def test_collection_status_separates_complete_pr_listing_from_partial_feedback_sources(self):
        envelope = {
            "resources": {
                "pull_requests": {"status": "complete", "pagination_complete": True, "count": 1},
                "reviews": {"status": "complete", "pagination_complete": True},
                "review_comments": {"status": "partial", "pagination_complete": False},
                "issue_comments": {"status": "complete", "pagination_complete": True},
            },
            "pull_requests": [{"number": 1}],
        }
        message = dashboard_app.collection_status_message(
            {"acme/repo": (envelope, {"population_count": 1})},
            {},
            expected_repository_count=1,
        )
        self.assertIn("PR retrieval is complete", message)
        self.assertIn("feedback-source coverage is partial", message)
        self.assertIn("inline comments", message)
        envelope["resources"]["review_comments"] = {
            "status": "complete", "pagination_complete": True,
        }
        self.assertEqual(
            "PR retrieval and feedback-source collection are complete.",
            dashboard_app.collection_status_message(
                {"acme/repo": (envelope, {"population_count": 1})},
                {},
                expected_repository_count=1,
            ),
        )

    def test_portfolio_activity_chart_uses_month_labels_and_hides_single_creation_month_trend(self):
        envelope = {
            "requested_window": {"from": "2026-01-01", "to": "2026-02-28"},
            "resources": {
                "pull_requests": {"status": "complete", "pagination_complete": True, "count": 1},
                "reviews": {"status": "complete", "pagination_complete": True},
                "review_comments": {"status": "complete", "pagination_complete": True},
                "issue_comments": {"status": "complete", "pagination_complete": True},
            },
            "pull_requests": [{
                "number": 1, "created_at": "2026-01-03T00:00:00Z",
                "merged_at": "2026-02-01T00:00:00Z", "state": "closed",
            }],
        }
        results = {"acme/repo": (envelope, {"population_count": 1})}
        calls = {name: Mock() for name in (
            "header", "caption", "columns", "container", "metric", "subheader",
            "success", "warning", "info", "vega_lite_chart",
        )}
        calls["columns"].side_effect = lambda count: [MagicMock() for _ in range(count)]
        calls["container"].return_value = MagicMock()
        patchers = [patch.object(dashboard_app.st, name, mock, create=True) for name, mock in calls.items()]
        patchers.extend([
            patch.object(dashboard_app.st, "session_state", {}, create=True),
            patch.object(dashboard_app, "portfolio_kpi_rows", return_value=[]),
            patch.object(dashboard_app, "render_portfolio"),
        ])
        for patcher in patchers:
            patcher.start()
        try:
            dashboard_app.render_portfolio_page(results, {}, ["acme/repo"])
            self.assertEqual(1, calls["vega_lite_chart"].call_count)  # Outcome chart only.
            self.assertIn("at least two different months", repr(calls["info"].call_args_list))

            envelope["resources"]["pull_requests"]["count"] = 2
            envelope["pull_requests"].append({
                "number": 2, "created_at": "2026-02-12T00:00:00Z",
                "merged_at": None, "state": "open",
            })
            results["acme/repo"] = (envelope, {"population_count": 2})
            calls["vega_lite_chart"].reset_mock()
            dashboard_app.render_portfolio_page(results, {}, ["acme/repo"])
            trend_spec = calls["vega_lite_chart"].call_args_list[0].args[1]
            self.assertEqual("%b %Y", trend_spec["encoding"]["x"]["axis"]["format"])
            self.assertEqual("%b %Y", trend_spec["encoding"]["tooltip"][0]["format"])
            outcome_spec = calls["vega_lite_chart"].call_args_list[1].args[1]
            self.assertEqual(
                220,
                outcome_spec["encoding"]["color"]["legend"]["labelLimit"],
            )
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

    def test_quality_and_insights_pages_do_not_claim_collected_features_are_unavailable(self):
        calls = {"warning": [], "info": []}
        with patch.object(
            dashboard_app.st,
            "title",
            Mock(),
            create=True,
        ), patch.object(
            dashboard_app.st,
            "header",
            Mock(),
            create=True,
        ), patch.object(
            dashboard_app.st,
            "caption",
            Mock(),
            create=True,
        ), patch.object(
            dashboard_app.st,
            "warning",
            Mock(side_effect=lambda message: calls["warning"].append(message)),
            create=True,
        ), patch.object(
            dashboard_app.st,
            "info",
            Mock(side_effect=lambda message: calls["info"].append(message)),
            create=True,
        ):
            dashboard_app.render_quality_page({})
            dashboard_app.render_insights_page({})
        all_labels = " ".join(calls["warning"] + calls["info"])
        self.assertIn("Quality score: unavailable / not implemented", all_labels)
        self.assertNotIn("checks are not requested", all_labels)
        self.assertNotIn("AI analysis and recommendations: unavailable", all_labels)

    def test_portfolio_outcomes_survive_partial_unrelated_resource_when_pr_history_is_complete(self):
        envelope = {
            "status": "partial",
            "requested_window": {"from": "2026-01-01T00:00:00Z", "to": "2026-01-31T23:59:59Z"},
            "resources": {"pull_requests": {"status": "complete", "pagination_complete": True, "count": 1}},
            "pull_requests": [{"created_at": "2026-01-03T00:00:00Z", "state": "closed", "merged_at": "2026-01-10T00:00:00Z"}],
        }
        overview = dashboard_app.build_portfolio_overview(
            {"owner/repo": (envelope, {"population_count": 1})}, {}
        )
        self.assertIsNotNone(overview)
        self.assertEqual(1, overview["outcomes"]["Merged"])

    def test_portfolio_outcome_withholds_invalid_timestamp_with_reason(self):
        envelope = {
            "status": "complete",
            "resources": {"pull_requests": {"status": "complete", "pagination_complete": True, "count": 1}},
            "pull_requests": [{"created_at": "not-a-timestamp", "state": "open", "merged_at": None}],
        }
        results = {"owner/repo": (envelope, {"population_count": 1})}
        self.assertIsNone(dashboard_app.build_portfolio_overview(results, {}))
        self.assertIn("creation timestamp is invalid", dashboard_app.portfolio_overview_unavailable_reason(results, {}))

    def test_portfolio_kpis_expose_observed_reviewer_and_ci_evidence(self):
        envelope = {
            "status": "complete",
            "requested_window": {"from": "2026-01-01", "to": "2026-01-31"},
            "filters": {"author": "dev"},
            "resources": {
                "pull_requests": {"status": "complete", "pagination_complete": True, "count": 1},
                "checks": {"status": "complete"},
            },
            "pull_requests": [{"created_at": "2026-01-03T00:00:00Z", "state": "open", "merged_at": None}],
            "checks": [{"conclusion": "failure"}],
        }
        rows = dashboard_app.portfolio_kpi_rows(
            {"owner/repo": (envelope, {"population_count": 1})}, {},
            {"reviewer_feedback_count": 2, "recommendations": [{}, {}]},
        )
        mapping = {label: value for label, value, _ in rows}
        self.assertEqual(2, mapping["Reviewer feedback"])
        self.assertEqual(2, mapping["Prioritized concerns"])
        self.assertEqual(1, mapping["Failed current-head checks"])

    def test_orchestrated_summary_renders_cited_comment_and_direct_source_links(self):
        calls = {
            name: Mock() for name in (
                "subheader", "caption", "container", "markdown", "write", "expander",
                "text", "link_button", "info", "error",
            )
        }
        calls["container"].return_value = MagicMock()
        calls["expander"].return_value = MagicMock()
        patchers = [patch.object(dashboard_app.st, name, mock, create=True) for name, mock in calls.items()]
        for patcher in patchers:
            patcher.start()
        try:
            dashboard_app._render_orchestrated_summaries([{
                "collection_context": {"repositories": [{"repository": {"owner": "acme", "name": "repo"}}]},
                "evidence": [{
                    "evidence_id": "e1", "repository": {"owner": "acme", "name": "repo"},
                    "pull_request": {"number": 7, "url": "https://example.test/acme/repo/pull/7"},
                    "source": "issue_comments", "source_url": "https://example.test/comment/9",
                    "commenter_role": "reviewer", "original_text": "Please clarify the expected output.",
                    "timestamp_status": "observed", "timestamp": "2026-01-05T12:00:00Z",
                }],
                "themes": [{"theme": "Expected behavior", "summary": "A reviewer requests clarification.",
                            "evidence_ids": ["e1"], "claims": [{"text": "A reviewer asked for clarification.", "evidence_ids": ["e1"]}]}],
            }], {})
        finally:
            for patcher in reversed(patchers):
                patcher.stop()
        self.assertIn("Please clarify the expected output.", repr(calls["text"].call_args_list))
        self.assertIn("A reviewer asked for clarification.", repr(calls["text"].call_args_list))
        self.assertIn("Open PR", repr(calls["link_button"].call_args_list))
        self.assertIn("Open source comment", repr(calls["link_button"].call_args_list))

    def test_orchestrated_summary_reports_synthesis_failure_for_other_repositories(self):
        calls = {name: Mock() for name in ("subheader", "caption", "info", "warning", "markdown", "expander", "dataframe")}
        calls["expander"].return_value = MagicMock()
        patchers = [patch.object(dashboard_app.st, name, mock, create=True) for name, mock in calls.items()]
        for patcher in patchers:
            patcher.start()
        try:
            dashboard_app._render_orchestrated_summaries(
                [{"evidence": [], "themes": [], "collection_context": {"repositories": [{"repository": {"owner": "acme", "name": "ok"}}]}}],
                {"repository_runs": [{"repository": "acme/partial", "status": {
                    "local_language_synthesis": {"status": "failed", "message": "Local Ollama unavailable"}
                }}]},
            )
        finally:
            for patcher in reversed(patchers):
                patcher.stop()
        self.assertIn("acme/partial", repr(calls["warning"].call_args_list))
        self.assertIn("Local Ollama unavailable", repr(calls["warning"].call_args_list))

    def test_latency_distribution_uses_only_valid_timezone_aware_evidence(self):
        observations = dashboard_app.latency_distribution({"evidence": [
            {"number": 1, "created_at": "2026-01-01T00:00:00Z", "submitted_at": "2026-01-01T02:00:00Z"},
            {"number": 2, "created_at": "2026-01-01T00:00:00", "submitted_at": "2026-01-01T02:00:00Z"},
            {"number": 3, "created_at": "2026-01-02T00:00:00Z", "submitted_at": "2026-01-01T23:00:00Z"},
        ]}, "created_at", "submitted_at")
        self.assertEqual([{"PR": 1, "elapsed_hours": 2.0}], observations)

    def test_dashboard_styles_include_narrow_screen_layout_rules(self):
        styles = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("[data-testid=\"stSidebar\"] { background: #f1f4f8;", styles)
        self.assertIn("@media (max-width: 900px)", styles)
        self.assertIn("flex-wrap: wrap", styles)

    def test_main_sections_and_sidebar_filters_are_ordered(self):
        source = APP_PATH.read_text(encoding="utf-8")
        overview_flow = source[source.index('if page == "Overview":'):]
        self.assertLess(overview_flow.index("render_author_action_plan("), overview_flow.index("_render_orchestrated_summaries("))
        self.assertLess(overview_flow.index("_render_orchestrated_summaries("), overview_flow.index('st.expander("Portfolio metrics and trends"'))
        self.assertLess(
            source.index('with st.form("collection_form")'),
            source.index('page = st.radio("Pages"'),
        )
        self.assertLess(
            source.index('st.form_submit_button('),
            source.index('page = st.radio("Pages"'),
        )
        for page in ("Repository Analytics", "People Analytics", "PR Explorer", "Quality & Reliability", "Insights & Recommendations"):
            self.assertIn(page, dashboard_app.PAGES)

    def test_adjacent_activity_gap_is_not_labeled_as_response_time(self):
        self.assertEqual(
            "Adjacent activity gaps",
            dashboard_app.METRIC_TITLES["adjacent_activity_gap"],
        )

    def test_pr_detail_shows_supported_source_evidence_without_pr_body(self):
        envelope = {
            "pull_requests": [{
                "id": 1,
                "number": 8,
                "title": "Visible title",
                "body": "PRIVATE BODY",
                "state": "closed",
                "created_at": "2026-01-01T00:00:00Z",
                "url": "https://github.example/org/repo/pull/8",
                "author": {"login": "dev"},
                "additions": 2,
                "deletions": 1,
                "changed_files": 1,
            }],
            "pull_request_resources": {
                "1": {
                    resource: {"status": "complete", "count": count}
                    for resource, count in (
                        ("pull_request_details", 1),
                        ("reviews", 1),
                        ("review_comments", 0),
                        ("issue_comments", 0),
                    )
                },
            },
            "reviews": [{
                "pull_request_id": 1,
                "url": "https://github.example/org/repo/pull/8#review-1",
                "body": "PRIVATE REVIEW BODY",
            }],
            "review_comments": [],
            "issue_comments": [],
        }
        analysis = {
            "metrics": {
                "first_submitted_review_latency": {
                    "definition": "Earliest eligible submitted review minus PR creation.",
                    "evidence": [{
                        "number": 8,
                        "created_at": "2026-01-01T00:00:00Z",
                        "submitted_at": "2026-01-02T00:00:00Z",
                        "url": "https://github.example/org/repo/pull/8#review-1",
                    }],
                },
            },
        }
        mocks = {
            name: Mock()
            for name in (
                "subheader", "caption", "dataframe", "info", "markdown",
                "write", "link_button",
            )
        }
        mocks["text_input"] = Mock(return_value="")
        mocks["multiselect"] = Mock(side_effect=lambda _label, options, **_kwargs: options)
        mocks["selectbox"] = Mock(side_effect=lambda _label, options, **_kwargs: options[0])
        mocks["columns"] = Mock(return_value=[MagicMock(), MagicMock(), MagicMock()])
        mocks["container"] = Mock(return_value=MagicMock())
        patchers = [
            patch.object(dashboard_app.st, name, value, create=True)
            for name, value in mocks.items()
        ]
        for patcher in patchers:
            patcher.start()
        try:
            dashboard_app.render_explorer(envelope, analysis)
        finally:
            for patcher in reversed(patchers):
                patcher.stop()
        rendered = repr(mocks["dataframe"].call_args_list) + repr(mocks["write"].call_args_list)
        self.assertNotIn("PRIVATE BODY", rendered)
        self.assertNotIn("PRIVATE REVIEW BODY", rendered)
        self.assertIn("submitted at", repr(mocks["write"].call_args_list))
        self.assertGreaterEqual(mocks["link_button"].call_count, 3)

    def test_zero_is_not_rendered_as_unknown(self):
        model = dashboard_app.present_metric({
            "name": "reopened_event_count",
            "status": "complete",
            "value": 0,
            "unit": "events",
            "numerator": 0,
            "denominator": 3,
            "coverage_fraction": 1.0,
            "definition": "Observed reopened events.",
            "evidence": [],
            "invalid_counts": {},
        })
        self.assertEqual(model["value"], "0 observed reopened events")
        self.assertIn("Complete", model["status"])
        self.assertEqual(model["denominator"], "0 observed events / 3 lifecycle-covered PRs")

    def test_unavailable_metric_is_not_presented_as_zero(self):
        model = dashboard_app.present_metric({
            "name": "commit_count",
            "status": "unavailable",
            "value": None,
            "unit": "commits per PR",
            "numerator": 0,
            "denominator": 0,
            "unavailable_reason": "no_complete_commit_histories",
            "definition": "Count commits.",
            "evidence": [],
            "invalid_counts": {},
        })
        self.assertEqual(model["value"], "Unavailable")
        self.assertIn("not a zero", model["status"])

    def test_explorer_excludes_body_and_comment_fields(self):
        rows = dashboard_app.prepare_pr_explorer([{
            "number": 7,
            "title": "Visible title",
            "body": "PRIVATE PR BODY",
            "state": "open",
            "created_at": "2026-01-01T00:00:00Z",
            "additions": 3,
            "deletions": 1,
            "changed_files": 2,
            "url": "https://github.example/owner/repo/pull/7",
            "comments": [{"body": "PRIVATE COMMENT"}],
            "author": {"login": "dev"},
        }])
        self.assertEqual(rows[0]["Title"], "Visible title")
        self.assertEqual(3, rows[0]["Additions"])
        self.assertEqual(1, rows[0]["Deletions"])
        self.assertEqual(2, rows[0]["Changed files"])
        self.assertEqual(
            "https://github.example/owner/repo/pull/7/files",
            rows[0]["Files changed"],
        )
        self.assertNotIn("body", rows[0])
        self.assertNotIn("comments", rows[0])
        self.assertNotIn("PRIVATE", repr(rows))
        self.assertTrue(dashboard_app.safe_https_url("https://github.example/pull/7#discussion_r7"))
        self.assertFalse(dashboard_app.safe_https_url("https://github.example/pull/7?token=value"))
        self.assertFalse(dashboard_app.safe_https_url("https://user:pass@github.example/pull/7"))

    def test_explorer_marks_missing_size_counts_as_unavailable(self):
        rows = dashboard_app.prepare_pr_explorer([{
            "id": 10,
            "number": 12,
            "url": "https://github.example/acme/repo/pull/12",
        }], {
            "10": {
                "pull_request_details": {"status": "unavailable"},
            },
        })
        self.assertEqual("Unavailable", rows[0]["Additions"])
        self.assertEqual("Unavailable", rows[0]["Deletions"])
        self.assertEqual("Unavailable", rows[0]["Changed files"])
        self.assertEqual("Unavailable", rows[0]["Size details"])
        self.assertEqual(
            "https://github.example/acme/repo/pull/12/files",
            rows[0]["Files changed"],
        )

    def test_partial_metric_is_labeled_as_limited(self):
        model = dashboard_app.present_metric({
            "name": "first_submitted_review_latency",
            "status": "partial",
            "value": 3600,
            "unit": "seconds",
            "numerator": 2,
            "denominator": 4,
            "coverage_fraction": 0.5,
            "definition": "Earliest eligible external review.",
            "evidence": [],
            "invalid_counts": {},
            "unknown_pr_count": 1,
        })
        self.assertIn("Partial", model["status"])
        self.assertEqual(model["coverage"], "50% observation coverage")
        self.assertIn("1.0 hours", model["value"])


def action_plan_envelope(*, reviewer_feedback=True, response_count=0, source_status="complete"):
    resources = {
        "reviews": {"status": source_status, "count": 0 if source_status == "complete" else None,
                    "pagination_complete": source_status == "complete",
                    "history_complete": source_status == "complete"},
        "review_comments": {"status": "complete", "count": int(reviewer_feedback),
                            "pagination_complete": True, "history_complete": True},
        "issue_comments": {"status": "complete", "count": response_count,
                           "pagination_complete": True, "history_complete": True},
    }
    per_pr_resources = {
        "10": {
            name: {
                **record,
                "count": (
                    int(reviewer_feedback) if name == "review_comments"
                    else response_count if name == "issue_comments"
                    else record["count"]
                ),
            }
            for name, record in resources.items()
        }
    }
    return {
        "schema_version": "0.1",
        "repository": {
            "id": 99, "owner": "acme", "name": "repo",
            "url": "https://github.example/acme/repo",
        },
        "requested_window": {
            "from": "2026-01-01T00:00:00Z",
            "to": "2026-01-31T23:59:59Z",
        },
        "filters": {"author": "target"},
        "observation_cutoff": "2026-02-10T00:00:00Z",
        "resources": {
            "pull_requests": {
                "status": "complete", "count": 1,
                "pagination_complete": True, "history_complete": True,
            },
            **resources,
        },
        "pull_requests": [{
            "id": 10, "number": 9, "title": "Example PR",
            "created_at": "2026-01-05T00:00:00Z",
            "url": "https://github.example/acme/repo/pull/9",
            "author": {"id": 44, "login": "target"},
        }],
        "pull_request_resources": per_pr_resources,
        "reviews": [],
        "review_comments": (
            [{
                "id": 21, "pull_request_id": 10, "repository_id": 99,
                "pull_request_number": 9,
                "author": {"id": 55, "login": "reviewer"},
                "created_at": "2026-02-02T00:00:00Z",
                "body": "Please add a test for this edge case.",
                "url": "https://github.example/acme/repo/pull/9#discussion_r21",
            }]
            if reviewer_feedback else []
        ),
        "issue_comments": [
            {
                "id": 30 + index, "pull_request_id": 10, "repository_id": 99,
                "pull_request_number": 9,
                "author": {"id": 44, "login": "target"},
                "created_at": f"2026-02-0{index + 2}T00:00:00Z",
                "body": "I updated the implementation.",
                "url": f"https://github.example/acme/repo/pull/9#issuecomment-{30 + index}",
            }
            for index in range(response_count)
        ],
    }


class AuthorActionPlanDashboardTests(unittest.TestCase):
    @staticmethod
    def render_with_mocks(results, failures=None):
        names = (
            "subheader", "caption", "info", "warning", "error", "dataframe",
            "markdown", "write", "spinner", "container", "text", "link_button",
            "selectbox", "expander",
        )
        calls = {name: Mock() for name in names}
        calls["spinner"].return_value = MagicMock()
        calls["container"].return_value = MagicMock()
        calls["expander"].return_value = MagicMock()
        calls["selectbox"].side_effect = lambda _label, options, **_kwargs: options[0]
        patchers = [
            patch.object(dashboard_app.st, name, calls[name], create=True)
            for name in names
        ]
        for patcher in patchers:
            patcher.start()
        try:
            selected = dashboard_app.render_author_action_plan(
                results, failures or {}, collection_complete=True
            )
        finally:
            for patcher in reversed(patchers):
                patcher.stop()
        return selected, calls

    def test_plan_shows_compact_action_why_evidence_validation_and_comment_links(self):
        envelope = action_plan_envelope()
        selected, calls = self.render_with_mocks(
            {"acme/repo": (envelope, {})}
        )

        self.assertEqual("target", selected)
        rendered_markdown = repr(calls["markdown"].call_args_list)
        self.assertIn("Action 1", rendered_markdown)
        self.assertIn("Why:", rendered_markdown)
        self.assertIn("Evidence:", rendered_markdown)
        self.assertIn("Validation:", rendered_markdown)
        rendered_links = repr(calls["link_button"].call_args_list) + rendered_markdown
        self.assertIn("discussion_r21", rendered_links)
        self.assertIn("Please add a test for this edge case.", repr(calls["text"].call_args_list))
        self.assertNotIn("Changes requested before?", rendered_markdown)
        rendered_captions = repr(calls["caption"].call_args_list)
        self.assertIn("1 observed reviewer feedback item(s)", rendered_captions)

    def test_unclassified_reviewer_text_is_shown_without_generic_checklist(self):
        envelope = action_plan_envelope()
        envelope["review_comments"][0]["body"] = (
            "Could we consider this alternative?"
        )
        selected, calls = self.render_with_mocks(
            {"acme/repo": (envelope, {})}
        )

        self.assertEqual("target", selected)
        self.assertIn("Reviewer feedback was observed", repr(calls["info"].call_args_list))
        self.assertNotIn("Action 1", repr(calls["markdown"].call_args_list))
        self.assertNotIn(
            "general checklist, not a finding",
            repr(calls["markdown"].call_args_list),
        )

    def test_four_author_responses_on_pr_nine_are_not_mislabeled_as_feedback(self):
        envelope = action_plan_envelope(reviewer_feedback=False, response_count=4)
        _selected, calls = self.render_with_mocks(
            {"acme/repo": (envelope, {})}
        )

        rendered_info = repr(calls["info"].call_args_list)
        self.assertIn("No reviewer feedback", rendered_info)
        self.assertIn("4 author response(s) were kept separate", rendered_info)
        self.assertNotIn("Action 1", repr(calls["markdown"].call_args_list))
        coverage_rows = calls["dataframe"].call_args.args[0]
        rows_by_source = {
            row["Feedback source"]: row for row in coverage_rows
        }
        self.assertEqual(
            0,
            rows_by_source["Submitted review summaries"]["Reviewer feedback items"],
        )
        self.assertEqual(
            0,
            rows_by_source["Inline review comments"]["Reviewer feedback items"],
        )
        self.assertEqual(
            0,
            rows_by_source["PR conversation comments"]["Reviewer feedback items"],
        )
        self.assertEqual(
            4,
            rows_by_source["PR conversation comments"]["Author responses"],
        )
        self.assertEqual(
            "1 / 1",
            rows_by_source["PR conversation comments"]["Complete / selected histories"],
        )
        self.assertEqual(
            0,
            rows_by_source["PR conversation comments"]["Successfully empty histories"],
        )
        self.assertEqual(0, calls["text"].call_count)

    def test_coverage_table_separates_feedback_items_from_pr_history_counts(self):
        envelope = action_plan_envelope(reviewer_feedback=True, response_count=4)
        _selected, calls = self.render_with_mocks(
            {"acme/repo": (envelope, {})}
        )

        rows_by_source = {
            row["Feedback source"]: row
            for row in calls["dataframe"].call_args.args[0]
        }
        inline_comments = rows_by_source["Inline review comments"]
        self.assertEqual(1, inline_comments["Reviewer feedback items"])
        self.assertEqual(0, inline_comments["Author responses"])
        self.assertEqual("1 / 1", inline_comments["Complete / selected histories"])
        conversation_comments = rows_by_source["PR conversation comments"]
        self.assertEqual(0, conversation_comments["Reviewer feedback items"])
        self.assertEqual(4, conversation_comments["Author responses"])
        self.assertEqual(0, conversation_comments["Successfully empty histories"])

    def test_zero_pr_population_displays_zero_coverage_not_unknown(self):
        envelope = action_plan_envelope(reviewer_feedback=False)
        envelope["pull_requests"] = []
        envelope["resources"]["pull_requests"]["count"] = 0
        envelope["pull_request_resources"] = {}
        for resource in ("reviews", "review_comments", "issue_comments"):
            envelope["resources"][resource]["count"] = 0
        _selected, calls = self.render_with_mocks(
            {"acme/repo": (envelope, {})}
        )

        rows = calls["dataframe"].call_args.args[0]
        for row in rows:
            with self.subTest(source=row["Feedback source"]):
                self.assertEqual("No PRs selected", row["Coverage"])
                self.assertEqual("N/A", row["Reviewer feedback items"])
                self.assertEqual("N/A", row["Author responses"])
                self.assertEqual("N/A", row["Unknown identities"])
                self.assertEqual("0 / 0", row["Complete / selected histories"])
                self.assertEqual(0, row["Successfully empty histories"])
                self.assertEqual(0, row["Partial / unavailable / not requested"])

    def test_incomplete_source_never_claims_zero_reviewer_feedback(self):
        envelope = action_plan_envelope(
            reviewer_feedback=False, source_status="unavailable"
        )
        envelope["resources"]["reviews"]["count"] = None
        _selected, calls = self.render_with_mocks(
            {"acme/repo": (envelope, {})}
        )

        rendered = repr(calls["info"].call_args_list) + repr(calls["warning"].call_args_list)
        self.assertIn("Partial feedback coverage", repr(calls["caption"].call_args_list))
        self.assertNotIn("No reviewer feedback was observed across", rendered)
        self.assertIn("Partial feedback coverage", repr(calls["caption"].call_args_list))

    def test_failed_repository_qualifies_successful_repository_recommendations(self):
        selected, calls = self.render_with_mocks(
            {"acme/repo": (action_plan_envelope(), {})},
            {"acme/other": "collection failed"},
        )

        self.assertEqual("target", selected)
        rendered = repr(calls["warning"].call_args_list) + repr(calls["caption"].call_args_list)
        self.assertIn("Partial collection", rendered)
        self.assertIn("acme/other", rendered)
        self.assertIn("failed repositories and sources are not treated as zero", rendered)


class CommentAnalysisDashboardTests(unittest.TestCase):
    def test_comment_analysis_renders_review_summaries_inline_and_conversation_evidence(self):
        pr_record = {
            "id": 10,
            "number": 12,
            "title": "Example change",
            "created_at": "2026-01-05T00:00:00Z",
            "url": "https://github.example/acme/repo/pull/12",
            "author": {"id": 44, "login": "target"},
        }
        resources = {}
        per_pr_resources = {}
        for name, count in (
            ("reviews", 1),
            ("review_comments", 1),
            ("issue_comments", 1),
        ):
            resources[name] = {
                "status": "complete",
                "count": count,
                "pagination_complete": True,
                "history_complete": True,
            }
            per_pr_resources[name] = {
                "status": "complete",
                "count": count,
                "pagination_complete": True,
                "history_complete": True,
            }
        envelope = {
            "schema_version": "0.1",
            "repository": {
                "id": 99,
                "owner": "acme",
                "name": "repo",
                "url": "https://github.example/acme/repo",
            },
            "requested_window": {
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-01-31T23:59:59Z",
            },
            "filters": {"author": "target"},
            "observation_cutoff": "2026-02-01T00:00:00Z",
            "resources": {
                "pull_requests": {
                    "status": "complete",
                    "count": 1,
                    "pagination_complete": True,
                    "history_complete": True,
                },
                **resources,
            },
            "pull_requests": [pr_record],
            "pull_request_resources": {"10": per_pr_resources},
            "reviews": [{
                "id": 5,
                "pull_request_id": 10,
                "repository_id": 99,
                "author": {"id": 55, "login": "reviewer"},
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-01-06T00:00:00Z",
                "body": "Please add a test for this edge case.",
                "url": "https://github.example/acme/repo/pull/12#review-5",
            }],
            "review_comments": [{
                "id": 6,
                "pull_request_id": 10,
                "repository_id": 99,
                "author": {"id": 55, "login": "reviewer"},
                "created_at": "2026-01-06T01:00:00Z",
                "body": "This may expose a security issue.",
                "path": "src/example.py",
                "line": 7,
                "url": "https://github.example/acme/repo/pull/12#discussion_r6",
            }],
            "issue_comments": [{
                "id": 7,
                "pull_request_id": 10,
                "repository_id": 99,
                "author": {"id": 44, "login": "target"},
                "created_at": "2026-01-06T02:00:00Z",
                "body": "I added the requested test.",
                "url": "https://github.example/acme/repo/pull/12#issuecomment-7",
            }],
        }
        methods = (
            "subheader", "caption", "dataframe", "warning", "info", "write",
            "markdown", "expander", "text", "link_button", "selectbox",
        )
        mocks = {name: Mock() for name in methods}
        mocks["expander"] = Mock(side_effect=lambda *_args, **_kwargs: MagicMock())
        mocks["selectbox"] = Mock(side_effect=lambda _label, options, **_kwargs: options[0])
        patchers = [
            patch.object(dashboard_app.st, name, mocks[name], create=True)
            for name in methods
        ]
        for patcher in patchers:
            patcher.start()
        try:
            dashboard_app.render_comment_analysis(envelope)
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

        rendered_comment_text = "\n".join(
            str(call.args[0]) for call in mocks["text"].call_args_list
        )
        self.assertIn("Please add a test for this edge case.", rendered_comment_text)
        self.assertIn("This may expose a security issue.", rendered_comment_text)
        self.assertIn("I added the requested test.", rendered_comment_text)
        rendered_writes = repr(mocks["write"].call_args_list)
        self.assertIn("Review state", rendered_writes)
        self.assertIn("not_assessed", rendered_writes)
        self.assertEqual(3, mocks["dataframe"].call_count)
        self.assertEqual(6, mocks["link_button"].call_count)

    def test_comment_author_options_lists_unique_pr_authors(self):
        authors = dashboard_app.comment_author_options({
            "pull_requests": [
                {"author": {"login": "zeta"}},
                {"author": {"login": "Alpha"}},
                {"author": {"login": "alpha"}},
                {"author": None},
            ],
        })
        self.assertEqual(["Alpha", "zeta"], authors)

    def test_comment_matrix_marks_incomplete_pr_counts_as_observed_only(self):
        report = {
            "taxonomy": {
                "rules": {"test_coverage": ["add a test"]},
                "unclassified_category": "other_unclassified",
            },
            "comment_inventory": [
                {
                    "pull_request_id": 10,
                    "author_role": "reviewer",
                    "included_in_observed_activity": True,
                    "categories": ["test_coverage"],
                },
                {
                    "pull_request_id": 20,
                    "author_role": "reviewer",
                    "included_in_observed_activity": True,
                    "categories": [],
                },
            ],
            "population": {
                "pull_request_evidence": [
                    {
                        "pull_request_id": 10,
                        "pull_request_number": 1,
                        "title": "Covered PR",
                        "url": "https://github.example/acme/repo/pull/1",
                    },
                    {
                        "pull_request_id": 20,
                        "pull_request_number": 2,
                        "title": "Incomplete PR",
                        "url": "https://github.example/acme/repo/pull/2",
                    },
                ],
            },
            "per_pull_request_coverage": {
                "10": {"all_comment_sources_complete": True},
                "20": {"all_comment_sources_complete": False},
            },
        }
        matrix = dashboard_app.build_comment_matrix(report)
        self.assertEqual(1, matrix[0]["Test Coverage"])
        self.assertEqual("Complete", matrix[0]["History coverage"])
        self.assertEqual(0, matrix[1]["Test Coverage"])
        self.assertEqual("Partial / unavailable", matrix[1]["History coverage"])


class LocalCommentSummaryDashboardTests(unittest.TestCase):
    def test_summary_is_rendered_once_with_all_roles_sources_and_cited_evidence(self):
        evidence = [
            {
                "evidence_id": f"evidence-{index}",
                "repository": {"owner": "acme", "name": "repo"},
                "pull_request": {
                    "number": index + 1,
                    "url": f"https://github.example/acme/repo/pull/{index + 1}",
                },
                "source": source,
                "source_url": f"https://github.example/comments/{index}",
                "timestamp": "2026-01-06T00:00:00Z",
                "timestamp_status": "observed",
                "commenter_role": role,
                "original_text": text,
            }
            for index, (source, role, text) in enumerate((
                ("reviews", "reviewer", "Please add tests."),
                ("review_comments", "author_response", "I added the test."),
                ("issue_comments", "unknown", "Could you clarify the expected result?"),
            ))
        ]
        summary = {
            "generated_by": "local_model",
            "uncertain": True,
            "collection_context": {
                "repositories": [{
                    "population_coverage": {
                        "complete_for_target_selection": False,
                    },
                }],
            },
            "evidence": evidence,
            "themes": [{
                "theme": "Test coverage",
                "summary": "The comments discuss test coverage.",
                "evidence_ids": [item["evidence_id"] for item in evidence],
                "claims": [{
                    "text": "A reviewer asks for tests.",
                    "evidence_ids": [evidence[0]["evidence_id"]],
                }],
            }],
            "source_coverage": {
                "reviews": {
                    "status": "complete",
                    "complete_pr_count": 1,
                    "population_pr_count": 1,
                    "successful_empty_pr_count": 0,
                    "pr_count_by_status": {
                        "complete": 1, "partial": 0, "unavailable": 0,
                        "not_requested": 0, "error": 0,
                    },
                },
                "review_comments": {
                    "status": "complete",
                    "complete_pr_count": 1,
                    "population_pr_count": 1,
                    "successful_empty_pr_count": 0,
                    "pr_count_by_status": {
                        "complete": 1, "partial": 0, "unavailable": 0,
                        "not_requested": 0, "error": 0,
                    },
                },
                "issue_comments": {
                    "status": "complete",
                    "complete_pr_count": 1,
                    "population_pr_count": 1,
                    "successful_empty_pr_count": 0,
                    "pr_count_by_status": {
                        "complete": 1, "partial": 0, "unavailable": 0,
                        "not_requested": 0, "error": 0,
                    },
                },
            },
        }
        calls = {
            name: Mock()
            for name in (
                "subheader", "caption", "dataframe", "warning", "info", "error",
                "text", "container", "expander", "link_button", "button", "status",
            )
        }
        calls["container"].return_value = MagicMock()
        calls["expander"].return_value = MagicMock()
        calls["status"].return_value = MagicMock()
        calls["button"].return_value = False
        session_state = {"collection_generation": 5}
        summary_mock = Mock(return_value=summary)
        patchers = [
            patch.object(dashboard_app.st, name, calls[name], create=True)
            for name in calls
        ]
        patchers.append(patch.object(dashboard_app.st, "session_state", session_state, create=True))
        patchers.append(patch.object(dashboard_app, "summarize_comments", summary_mock))
        for patcher in patchers:
            patcher.start()
        try:
            for _ in range(2):
                dashboard_app._render_local_comment_summary(
                    [{"target_author": "target"}],
                    "target",
                    failures={},
                    report_errors=[],
                )
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

        self.assertEqual(1, summary_mock.call_count)
        rendered = repr(calls["dataframe"].call_args_list)
        for text in (
            "Please add tests.",
            "I added the test.",
            "Could you clarify the expected result?",
        ):
            self.assertIn(text, rendered)
        self.assertIn("1 reviewer", repr(calls["caption"].call_args_list))
        self.assertIn("partial summary", repr(calls["warning"].call_args_list))
        self.assertEqual(16, calls["link_button"].call_count)


class DashboardSafetyTests(unittest.TestCase):
    def test_missing_token_fails_before_client_or_network_construction(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            dashboard_app, "GitHubClient"
        ) as client:
            with self.assertRaises(dashboard_app.DashboardInputError):
                dashboard_app.collect_metrics(
                    owner="owner",
                    repository="repo",
                    from_day=date(2026, 1, 1),
                    to_day=date(2026, 1, 2),
                    author=None,
                )
            client.assert_not_called()

    def test_collection_passes_only_agreed_resource_set_to_backend(self):
        token = "test-only-token-placeholder"
        envelope = {"status": "complete", "pull_requests": []}
        analysis = {"metrics": {}}
        with patch.dict(os.environ, {}, clear=True), patch.object(
            dashboard_app, "GitHubClient"
        ) as client_class, patch.object(
            dashboard_app, "fetch_repository", return_value=envelope
        ) as fetch, patch.object(
            dashboard_app, "analyze_repository", return_value=analysis
        ):
            result_envelope, result_analysis = dashboard_app.collect_metrics(
                owner="owner",
                repository="repo",
                from_day=date(2026, 1, 1),
                to_day=date(2026, 1, 2),
                author="dev",
                token=token,
            )
        client_class.assert_called_once_with(dashboard_app.DEFAULT_API_URL, token)
        self.assertEqual(fetch.call_args.kwargs["include"], dashboard_app.METRICS_RESOURCES)
        self.assertIs(result_envelope, envelope)
        self.assertIs(result_analysis, analysis)

    def test_repository_discovery_uses_explicit_session_token_without_environment(self):
        token = "user-provided-token"
        with patch.dict(os.environ, {}, clear=True), patch.object(
            dashboard_app, "GitHubClient"
        ) as client_class, patch.object(
            dashboard_app, "discover_accessible_repositories", return_value=[]
        ) as discover:
            self.assertEqual([], dashboard_app.discover_repositories(token))
        client_class.assert_called_once_with(dashboard_app.DEFAULT_API_URL, token)
        discover.assert_called_once_with(client_class.return_value)

    def test_token_change_clears_previous_token_data_without_storing_fingerprint(self):
        new_token = "new-session-token"
        session_state = {
            "github_token_input": new_token,
            "discovered_repositories": [{"full_name": "old/private-repo"}],
            "portfolio_results": {"old/private-repo": ({}, {})},
            "portfolio_failures": {"old/private-repo": "old result"},
            "portfolio_repositories": ["old/private-repo"],
            "runtime_orchestration": {"summary": "old"},
            "current_action_plan": {"plan": "old"},
            "local_comment_summary": {"summary": "old"},
            "collection_complete": True,
            "collection_generation": 4,
            "selected_repositories": [dashboard_app.AVAILABLE_REPOSITORIES[0], "old/private-repo"],
        }
        with patch.object(dashboard_app.st, "session_state", session_state, create=True):
            dashboard_app._handle_github_token_change()

        for key in (
            "discovered_repositories", "portfolio_results", "portfolio_failures",
            "portfolio_repositories", "runtime_orchestration", "current_action_plan",
            "local_comment_summary",
        ):
            self.assertNotIn(key, session_state)
        self.assertFalse(session_state["collection_complete"])
        self.assertEqual(5, session_state["collection_generation"])
        self.assertEqual([dashboard_app.AVAILABLE_REPOSITORIES[0]],
                         session_state["selected_repositories"])
        self.assertNotIn("_github_token_fingerprint", session_state)

    def test_authentication_errors_are_helpful_without_echoing_token_or_body(self):
        token = "private-token-value"
        invalid = dashboard_app.safe_error_message(
            dashboard_app.GitHubAPIError(401, f"invalid token {token}")
        )
        insufficient = dashboard_app.safe_error_message(
            dashboard_app.GitHubAPIError(403, f"scope failure {token}")
        )
        self.assertIn("rejected", invalid)
        self.assertIn("read access", insufficient)
        self.assertNotIn(token, invalid + insufficient)
        self.assertNotIn("scope failure", insufficient)

    def test_error_display_does_not_echo_token_or_exception_body(self):
        token = "sensitive-test-token"
        message = dashboard_app.safe_error_message(RuntimeError(f"{token} private response"))
        self.assertNotIn(token, message)
        self.assertNotIn("private response", message)

    def test_token_is_process_side_and_not_passed_to_ui_renderers(self):
        token = "sensitive-test-token"
        envelope = {
            "status": "complete",
            "repository": {"owner": "owner", "name": "repo"},
            "requested_window": {"from": "a", "to": "b"},
            "filters": {"author": None},
            "observation_cutoff": "c",
            "warnings": [],
            "pull_requests": [],
        }
        analysis = {"population_count": 0, "metrics": {}}
        with patch.object(dashboard_app.st, "success", create=True) as success, patch.object(
            dashboard_app.st, "caption", create=True
        ) as caption, patch.object(dashboard_app.st, "metric", create=True), patch.object(
            dashboard_app.st, "info", create=True
        ), patch.object(
            dashboard_app.st, "columns", return_value=[Mock(), Mock()], create=True
        ), patch.object(
            dashboard_app.st, "subheader", create=True
        ):
            dashboard_app.render_result(envelope, analysis)
        rendered = repr(success.call_args_list) + repr(caption.call_args_list)
        self.assertNotIn(token, rendered)

    def test_repository_author_and_api_validation(self):
        self.assertEqual(dashboard_app.parse_repository("some-owner/project_1"), ("some-owner", "project_1"))
        with self.assertRaises(ValueError):
            dashboard_app.parse_repository("owner/repo/extra")
        self.assertIsNone(dashboard_app.validate_author("  "))
        self.assertEqual("author1", dashboard_app.validate_author("author1"))
        self.assertEqual("author1, author2, author3", dashboard_app.validate_author(
            " author1, , author2, author1, author3, "
        ))
        self.assertEqual("Author1, author2", dashboard_app.validate_author("Author1, author2, author1"))
        with self.assertRaises(dashboard_app.DashboardInputError):
            dashboard_app.validate_author("invalid/login")
    def test_configured_api_url_is_fixed_when_environment_is_unset_or_conflicting(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dashboard_app.DEFAULT_API_URL, dashboard_app.configured_api_url())
        with patch.dict(os.environ, {"GITHUB_API_URL": "https://conflict.example/api/v3"}):
            self.assertEqual(dashboard_app.DEFAULT_API_URL, dashboard_app.configured_api_url())

class PortfolioDashboardTests(unittest.TestCase):
    @staticmethod
    def result(
        owner: str,
        name: str,
        *,
        envelope_status: str = "complete",
        metric_status: str = "complete",
        population: int = 2,
        reopened: int = 3,
        lifecycle_covered: int = 2,
        commits: int = 7,
        commit_covered: int = 2,
    ):
        envelope = {
            "status": envelope_status,
            "repository": {"owner": owner, "name": name},
            "requested_window": {"from": "2026-01-01", "to": "2026-01-31"},
            "observation_cutoff": "2026-02-01T00:00:00Z",
            "filters": {"author": "dev"},
            "resources": {
                "pull_requests": {
                    "status": "complete",
                    "count": population,
                    "pagination_complete": True,
                }
            },
        }
        metrics = {
            "reopened_event_count": {
                "name": "reopened_event_count",
                "status": metric_status,
                "definition": "Observed reopened lifecycle events.",
                "population_count": population,
                "numerator": reopened,
                "denominator": lifecycle_covered,
                "unit": "events",
            },
            "commit_count": {
                "name": "commit_count",
                "status": metric_status,
                "definition": "Valid timestamped commits per covered PR.",
                "population_count": population,
                "numerator": commits,
                "total_valid_commits": commits,
                "denominator": commit_covered,
                "unit": "commits",
                "median_commits_per_covered_pr": 3.5,
            },
            "pr_size": {
                "name": "pr_size",
                "status": "complete",
                "value": 12,
                "unit": "changed lines",
            },
            "merge_latency": {
                "name": "merge_latency",
                "status": "complete",
                "value": 999,
                "unit": "seconds",
            },
        }
        return envelope, {"population_count": population, "metrics": metrics}

    def test_multi_repository_collection_keeps_success_when_another_fails(self):
        first = self.result("team", "good")
        token = "test-only-token-placeholder"
        progress_messages = []

        def collect(*, repository, **kwargs):
            self.assertEqual(kwargs["from_day"], date(2026, 1, 1))
            self.assertEqual(kwargs["to_day"], date(2026, 1, 31))
            self.assertEqual(kwargs["author"], "same-login")
            if repository == "bad":
                raise RuntimeError("private response text")
            return first

        with patch.object(dashboard_app, "collect_metrics", side_effect=collect):
            results, failures = dashboard_app.collect_portfolio_metrics(
                ["team/good", "team/bad"],
                from_day=date(2026, 1, 1),
                to_day=date(2026, 1, 31),
                author="same-login",
                token=token,
                progress_callback=progress_messages.append,
            )
        self.assertEqual(list(results), ["team/good"])
        self.assertIn("team/bad", failures)
        self.assertNotIn("private response text", failures["team/bad"])
        self.assertTrue(any("Starting collection for team/good" in message
                            for message in progress_messages))
        self.assertTrue(any("Collected " in message and "PR(s) and " in message
                            for message in progress_messages))
        self.assertTrue(any("Collection failed for team/bad" in message
                            for message in progress_messages))

    def test_portfolio_sums_only_exact_counts_and_never_medians_or_durations(self):
        first = self.result("team", "one", reopened=2, commits=4)
        second = self.result("team", "two", reopened=5, commits=8)
        aggregate = dashboard_app.aggregate_portfolio_metrics(
            {"team/one": first, "team/two": second}
        )
        self.assertEqual(aggregate["status"], "complete")
        self.assertEqual(aggregate["population_count"], 4)
        self.assertEqual(aggregate["metrics"]["reopened_event_count"]["value"], 7)
        self.assertEqual(aggregate["metrics"]["reopened_event_count"]["denominator"], 4)
        self.assertEqual(aggregate["metrics"]["commit_count"]["total_valid_commits"], 12)
        self.assertNotIn("median_commits_per_covered_pr", aggregate["metrics"]["commit_count"])
        self.assertNotIn("merge_latency", aggregate["metrics"])
        self.assertNotIn("pr_size", aggregate["metrics"])

    def test_status_mismatch_keeps_exact_counts_as_partial(self):
        complete = self.result("team", "one", metric_status="complete")
        partial = self.result("team", "two", metric_status="partial")
        aggregate = dashboard_app.aggregate_portfolio_metrics(
            {"team/one": complete, "team/two": partial}
        )
        self.assertEqual("partial", aggregate["metrics"]["reopened_event_count"]["status"])
        self.assertEqual(6, aggregate["metrics"]["reopened_event_count"]["value"])
        self.assertEqual(4, aggregate["metrics"]["reopened_event_count"]["denominator"])

    def test_incomplete_unrelated_resource_does_not_suppress_complete_pr_population_or_metric(self):
        complete = self.result("team", "one")
        partial = self.result("team", "two", envelope_status="partial")
        aggregate = dashboard_app.aggregate_portfolio_metrics(
            {"team/one": complete, "team/two": partial}
        )
        self.assertEqual(aggregate["status"], "partial")
        self.assertEqual(4, aggregate["population_count"])
        self.assertEqual(14, aggregate["metrics"]["commit_count"].get("total_valid_commits"))
        self.assertEqual(6, aggregate["metrics"]["reopened_event_count"]["value"])

    def test_failed_repository_makes_portfolio_partial_but_preserves_count(self):
        one = self.result("team", "one")
        aggregate = dashboard_app.aggregate_portfolio_metrics(
            {"team/one": one}, {"team/two": "safe failure"}
        )
        self.assertEqual(aggregate["status"], "partial")
        self.assertEqual(aggregate["population_count"], 2)

    def test_discovered_metadata_and_default_branch_are_used_without_fallback(self):
        metadata = {
            "owner": "team",
            "name": "service",
            "full_name": "team/service",
            "default_branch": "release",
            "visibility": "private",
            "language": "Python",
        }
        self.assertEqual(
            dashboard_app.repository_identity(metadata),
            ("team", "service", "team/service"),
        )
        caption = dashboard_app.repository_metadata_caption(metadata)
        self.assertIn("Default branch: release", caption)
        self.assertIn("Visibility: private", caption)
        self.assertIn("Language: Python", caption)
        self.assertNotIn("main", caption)
        self.assertEqual(
            dashboard_app.repository_metadata_caption({"default_branch": None}),
            "",
        )


if __name__ == "__main__":
    unittest.main()
