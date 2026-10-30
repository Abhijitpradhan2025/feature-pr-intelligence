from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import Mock

from python_script.orchestrator import (
    CodeQualityTestReliabilityAgent,
    DashboardUIUXAgent,
    GitHubDataAgent,
    Orchestrator,
    PRAnalyticsReviewIntelligenceAgent,
    ValidationAgent,
    _safe_failure,
)
from python_script.fetch_github_pr_data import GitHubAPIError


class RuntimeOrchestratorTests(unittest.TestCase):
    def envelope(self):
        return {
            "status": "complete", "resources": {"checks": {"status": "not_requested"}},
            "pull_requests": [], "checks": [],
        }

    def analytics(self):
        return {
            "schema_version": PRAnalyticsReviewIntelligenceAgent.output_schema,
            "metrics": {"population_count": 0, "metrics": {}},
            "comment_report": {
                "comment_inventory": [], "comment_activity": {
                    "observed_reviewer_comment_count": 0,
                    "observed_author_response_count": 0,
                    "observed_unknown_author_count": 0,
                },
                "data_quality": {}, "evidence": [], "resource_coverage": {},
            },
            "action_plan": {"status": "no_feedback_found"},
        }

    def test_data_agent_reuses_collector_and_requests_ci_evidence(self):
        client = Mock()
        client_factory = Mock(return_value=client)
        envelope = self.envelope()
        with unittest.mock.patch("python_script.orchestrator.fetch_repository", return_value=envelope) as fetch:
            agent = GitHubDataAgent(client_factory)
            result = agent.run({
                "schema_version": agent.input_schema, "owner": "team", "repository": "repo",
                "from_day": "2026-01-01", "to_day": "2026-01-31", "author": "dev, other",
                "token": "test-token",
            })
        self.assertEqual(envelope, result["envelope"])
        self.assertIn("checks", fetch.call_args.kwargs["include"])
        self.assertEqual("dev, other", fetch.call_args.kwargs["author"])
        client_factory.assert_called_once_with(
            "https://github.azc.ext.hp.com/api/v3", "test-token"
        )

    def test_authentication_failures_are_helpful_and_redacted(self):
        secret = "must-never-be-shown"
        invalid = _safe_failure(GitHubAPIError(401, f"token rejected {secret}"))
        insufficient = _safe_failure(GitHubAPIError(403, f"scope missing {secret}"))
        self.assertIn("rejected the token", invalid)
        self.assertIn("token scopes", insufficient)
        self.assertNotIn(secret, invalid + insufficient)
        self.assertNotIn("scope missing", insufficient)

    def test_required_specialists_are_invoked_and_results_validated(self):
        data = Mock()
        data.output_schema = GitHubDataAgent.output_schema
        data.run.return_value = {"schema_version": GitHubDataAgent.output_schema,
                                 "envelope": self.envelope()}
        analytics = Mock()
        analytics.output_schema = PRAnalyticsReviewIntelligenceAgent.output_schema
        analytics.run.return_value = self.analytics()
        quality = Mock()
        quality.output_schema = CodeQualityTestReliabilityAgent.output_schema
        quality.run.return_value = {"schema_version": quality.output_schema, "checks": {}}
        ui = Mock()
        ui.output_schema = DashboardUIUXAgent.output_schema
        ui.run.return_value = {"schema_version": ui.output_schema, "sections": []}
        validator = Mock()
        validator.output_schema = ValidationAgent.output_schema
        validator.run.return_value = {"schema_version": validator.output_schema, "valid": True}
        model = Mock(return_value=None)

        result = Orchestrator(data_agent=data, analytics_agent=analytics,
                              quality_agent=quality, ui_agent=ui,
                              validation_agent=validator, summary_fn=model).run(
            owner="team", repository="repo", from_day=date(2026, 1, 1),
            to_day=date(2026, 1, 31), author="dev",
            token="test-token")
        for agent in (data, analytics, quality, ui, validator):
            agent.run.assert_called_once()
        model.assert_not_called()
        self.assertTrue(result["status"]["validation"]["status"] == "completed")
        self.assertEqual("completed", result["status"]["dashboard_ui_ux"]["status"])
        self.assertEqual("skipped", result["status"]["local_language_synthesis"]["status"])

    def test_model_failure_keeps_deterministic_analytics_and_reports_failure(self):
        data = Mock()
        data.output_schema = GitHubDataAgent.output_schema
        data.run.return_value = {"schema_version": data.output_schema,
                                 "envelope": self.envelope()}
        analytics = Mock()
        analytics.output_schema = PRAnalyticsReviewIntelligenceAgent.output_schema
        report = self.analytics()["comment_report"]
        report["evidence"] = [{"evidence_id": "e1"}]
        report["comment_inventory"] = [{"included_in_summary": True,
                                         "summary_text": "A comment."}]
        report["comment_activity"]["summary_comment_count"] = 1
        report["comment_activity"]["observed_reviewer_comment_count"] = 1
        analytics.run.return_value = self.analytics()
        analytics.run.return_value["comment_report"] = report
        quality = Mock()
        quality.output_schema = CodeQualityTestReliabilityAgent.output_schema
        quality.run.return_value = {"schema_version": quality.output_schema, "checks": {}}
        model = Mock(side_effect=RuntimeError("model unavailable"))
        result = Orchestrator(data_agent=data, analytics_agent=analytics,
                              quality_agent=quality, summary_fn=model).run(
            owner="team", repository="repo", from_day=date(2026, 1, 1),
            to_day=date(2026, 1, 31), author="dev",
            token="test-token")
        self.assertIsNotNone(result["analytics"]["action_plan"])
        self.assertEqual("failed", result["status"]["local_language_synthesis"]["status"])
        self.assertEqual("completed", result["status"]["pr_analytics_review_intelligence"]["status"])
        model.assert_called_once()

    def test_malformed_agent_output_fails_stage(self):
        data = Mock()
        data.output_schema = GitHubDataAgent.output_schema
        data.run.return_value = {"schema_version": "wrong", "envelope": self.envelope()}
        result = Orchestrator(data_agent=data).run(
            owner="team", repository="repo", from_day=date(2026, 1, 1),
            to_day=date(2026, 1, 31), author="dev",
            token="test-token")
        self.assertEqual("failed", result["status"]["github_data"]["status"])
        self.assertEqual({}, result["results"])

    def test_validation_accepts_deterministic_result_when_optional_summary_is_skipped(self):
        report = {
            "comment_inventory": [],
            "comment_activity": {
                "observed_reviewer_comment_count": 0,
                "observed_author_response_count": 0,
                "observed_unknown_author_count": 0,
            },
        }
        result = ValidationAgent().run({
            "schema_version": ValidationAgent.input_schema,
            "comment_report": report,
            "summary": None,
        })
        self.assertTrue(result["valid"])
        self.assertEqual("skipped", result["summary_status"])
        self.assertEqual(0, result["evidence_count"])

    def test_summary_runs_for_observed_text_inventory_without_taxonomy_evidence(self):
        data = Mock()
        data.output_schema = GitHubDataAgent.output_schema
        data.run.return_value = {"schema_version": data.output_schema,
                                 "envelope": self.envelope()}
        analytics = Mock()
        analytics.output_schema = PRAnalyticsReviewIntelligenceAgent.output_schema
        report = self.analytics()["comment_report"]
        report["comment_inventory"] = [{
            "included_in_summary": True, "summary_text": "Please clarify the response.",
            "author_role": "reviewer", "comment_source": "issue_comments",
        }]
        report["comment_activity"]["summary_comment_count"] = 1
        report["comment_activity"]["observed_reviewer_comment_count"] = 1
        analytics.run.return_value = self.analytics()
        analytics.run.return_value["comment_report"] = report
        quality = Mock()
        quality.output_schema = CodeQualityTestReliabilityAgent.output_schema
        quality.run.return_value = {"schema_version": quality.output_schema, "checks": {}}
        ui = Mock()
        ui.output_schema = DashboardUIUXAgent.output_schema
        ui.run.return_value = {"schema_version": ui.output_schema, "sections": []}
        validator = Mock()
        validator.output_schema = ValidationAgent.output_schema
        validator.run.return_value = {"schema_version": validator.output_schema, "valid": True}
        summary = {"evidence": [{"evidence_id": "comment-1"}], "themes": [],
                   "synthesis_status": "complete"}
        summary_fn = Mock(return_value=summary)

        result = Orchestrator(data_agent=data, analytics_agent=analytics,
                              quality_agent=quality, ui_agent=ui,
                              validation_agent=validator,
                              summary_fn=summary_fn).run(
            owner="team", repository="repo", from_day=date(2026, 1, 1),
            to_day=date(2026, 1, 31), author="dev",
            token="test-token")
        summary_fn.assert_called_once()
        self.assertEqual("completed", result["status"]["local_language_synthesis"]["status"])
        self.assertEqual("completed", result["status"]["validation"]["status"])

    def test_summary_without_explicit_complete_status_fails_closed(self):
        data = Mock()
        data.output_schema = GitHubDataAgent.output_schema
        data.run.return_value = {"schema_version": data.output_schema,
                                 "envelope": self.envelope()}
        analytics = Mock()
        analytics.output_schema = PRAnalyticsReviewIntelligenceAgent.output_schema
        report = self.analytics()["comment_report"]
        report["comment_inventory"] = [{
            "included_in_summary": True, "summary_text": "Please clarify the response.",
            "author_role": "reviewer", "comment_source": "issue_comments",
        }]
        report["comment_activity"]["summary_comment_count"] = 1
        report["comment_activity"]["observed_reviewer_comment_count"] = 1
        analytics.run.return_value = self.analytics()
        analytics.run.return_value["comment_report"] = report
        summary = {"evidence": [{"evidence_id": "comment-1"}], "themes": []}

        result = Orchestrator(data_agent=data, analytics_agent=analytics,
                              summary_fn=Mock(return_value=summary)).run(
            owner="team", repository="repo", from_day=date(2026, 1, 1),
            to_day=date(2026, 1, 31), author="dev", token="test-token")
        self.assertEqual("failed", result["status"]["local_language_synthesis"]["status"])
        self.assertIsNone(result["status"]["local_language_synthesis"].get("summary_status"))
        self.assertEqual("Local synthesis was incomplete.",
                         result["status"]["local_language_synthesis"]["message"])

    def test_partial_model_summary_remains_visible_but_stage_is_not_completed(self):
        data = Mock()
        data.output_schema = GitHubDataAgent.output_schema
        data.run.return_value = {"schema_version": data.output_schema,
                                 "envelope": self.envelope()}
        analytics = Mock()
        analytics.output_schema = PRAnalyticsReviewIntelligenceAgent.output_schema
        report = self.analytics()["comment_report"]
        report["comment_inventory"] = [{
            "included_in_summary": True, "summary_text": "Please clarify the response.",
            "author_role": "reviewer", "comment_source": "issue_comments",
        }]
        report["comment_activity"]["summary_comment_count"] = 1
        report["comment_activity"]["observed_reviewer_comment_count"] = 1
        analytics.run.return_value = self.analytics()
        analytics.run.return_value["comment_report"] = report
        quality = Mock()
        quality.output_schema = CodeQualityTestReliabilityAgent.output_schema
        quality.run.return_value = {"schema_version": quality.output_schema, "checks": {}}
        ui = Mock()
        ui.output_schema = DashboardUIUXAgent.output_schema
        ui.run.return_value = {"schema_version": ui.output_schema, "sections": []}
        validator = Mock()
        validator.output_schema = ValidationAgent.output_schema
        validator.run.return_value = {"schema_version": validator.output_schema, "valid": True}
        summary = {"evidence": [{"evidence_id": "comment-1"}], "themes": [],
                   "synthesis_status": "partial",
                   "synthesis_message": "One evidence batch failed."}
        result = Orchestrator(data_agent=data, analytics_agent=analytics,
                              quality_agent=quality, ui_agent=ui,
                              validation_agent=validator,
                              summary_fn=Mock(return_value=summary)).run(
            owner="team", repository="repo", from_day=date(2026, 1, 1),
            to_day=date(2026, 1, 31), author="dev", token="test-token")
        self.assertEqual(summary, result["summary"])
        self.assertEqual("failed", result["status"]["local_language_synthesis"]["status"])
        self.assertEqual("partial", result["status"]["local_language_synthesis"]["summary_status"])
        self.assertEqual("One evidence batch failed.", result["status"]["local_language_synthesis"]["message"])
        self.assertEqual("completed", result["status"]["validation"]["status"])
        self.assertIsNotNone(result["analytics"]["action_plan"])

    def test_malformed_dashboard_agent_output_is_recorded_without_skipping_validation(self):
        data = Mock()
        data.output_schema = GitHubDataAgent.output_schema
        data.run.return_value = {"schema_version": data.output_schema,
                                 "envelope": self.envelope()}
        analytics = Mock()
        analytics.output_schema = PRAnalyticsReviewIntelligenceAgent.output_schema
        analytics.run.return_value = self.analytics()
        quality = Mock()
        quality.output_schema = CodeQualityTestReliabilityAgent.output_schema
        quality.run.return_value = {"schema_version": quality.output_schema, "checks": {}}
        ui = Mock()
        ui.output_schema = DashboardUIUXAgent.output_schema
        ui.run.return_value = {"schema_version": "wrong", "sections": []}
        validator = Mock()
        validator.output_schema = ValidationAgent.output_schema
        validator.run.return_value = {"schema_version": validator.output_schema, "valid": True}
        result = Orchestrator(data_agent=data, analytics_agent=analytics,
                              quality_agent=quality, ui_agent=ui,
                              validation_agent=validator, summary_fn=Mock()).run(
            owner="team", repository="repo", from_day=date(2026, 1, 1),
            to_day=date(2026, 1, 31), author="dev",
            token="test-token")
        self.assertEqual("failed", result["status"]["dashboard_ui_ux"]["status"])
        self.assertEqual("completed", result["status"]["validation"]["status"])
        validator.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
