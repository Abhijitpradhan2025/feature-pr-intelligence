import json
import unittest
from unittest.mock import patch

from comment_analysis import analyze_comments
from comment_summary import RESPONSE_VERSION
from local_comment_agent import (
    CONTEXT_SIZE,
    LocalCommentAgentError,
    LocalCommentAgentOutputError,
    LocalCommentAgentUnavailable,
    summarize_comments,
)


def make_report(body="Please add an edge-case test", *, repository_id=42,
                repository_name="repo", comment_id=101, pr_number=1):
    sources = ("reviews", "review_comments", "issue_comments")
    source_counts = {"reviews": 0, "review_comments": 0, "issue_comments": 1}
    return analyze_comments(
        {
            "schema_version": "0.1",
            "repository": {
                "id": repository_id,
                "host_url": "https://example.test",
                "owner": "example",
                "name": repository_name,
                "url": f"https://example.test/example/{repository_name}",
            },
            "requested_window": {
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-01-31T23:59:59Z",
            },
            "filters": {"author": "author"},
            "observation_cutoff": "2026-02-01T00:00:00Z",
            "resources": {
                "pull_requests": {
                    "status": "complete",
                    "count": 1,
                    "pagination_complete": True,
                    "history_complete": True,
                },
                **{
                    source: {
                        "status": "complete",
                        "count": source_counts[source],
                        "pagination_complete": True,
                        "history_complete": True,
                    }
                    for source in sources
                },
            },
            "pull_requests": [{
                "id": repository_id * 10,
                "number": pr_number,
                "title": "Example PR",
                "url": f"https://example.test/example/{repository_name}/pull/{pr_number}",
                "created_at": "2026-01-05T00:00:00Z",
                "author": {"id": 1, "login": "author"},
            }],
            "pull_request_resources": {
                str(repository_id * 10): {
                    source: {
                        "status": "complete",
                        "count": source_counts[source],
                        "pagination_complete": True,
                        "history_complete": True,
                    }
                    for source in sources
                }
            },
            "reviews": [],
            "review_comments": [],
            "issue_comments": [{
                "id": comment_id,
                "repository_id": repository_id,
                "pull_request_id": repository_id * 10,
                "pull_request_number": pr_number,
                "author": {"id": 2, "login": "reviewer"},
                "created_at": "2026-01-06T00:00:00Z",
                "body": body,
                "url": f"https://example.test/example/{repository_name}/issues/{pr_number}#issuecomment-{comment_id}",
            }],
        },
        "author",
    )


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body

    def read(self):
        return json.dumps(self.body).encode("utf-8")


class FakeConnection:
    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.body = None
        self.headers = None
        self.closed = False
        self.status = 200
        self.response_content = None

    def request(self, method, path, body, headers):
        self.method = method
        self.path = path
        self.body = body
        self.headers = headers
        payload = json.loads(body)
        request = json.loads(payload["messages"][1]["content"])
        if request.get("task") == "consolidate_validated_batch_findings":
            output = {"schema_version": RESPONSE_VERSION, "themes": [{
                "theme": "Portfolio summary",
                "summary": "Reviewers raised several evidence-backed concerns across the selected PRs.",
                "evidence_ids": ["F1"],
            }]}
        else:
            evidence = request["evidence"]
            ids = [item["evidence_id"] for item in evidence]
            output = {"schema_version": RESPONSE_VERSION, "themes": [{
                "theme": "Testing", "summary": "Comments request testing improvements.",
                "evidence_ids": ids,
            }]}
        self.response_content = json.dumps(output)

    def getresponse(self):
        if self.status != 200:
            return FakeResponse(self.status, {
                "error": getattr(self, "server_error", "private-body-that-must-not-be-shown")
            })
        return FakeResponse(self.status, {
            "message": {"content": self.response_content or ""},
            "error": "private-body-that-must-not-be-shown",
        })

    def close(self):
        self.closed = True


class InvalidOutputConnection(FakeConnection):
    def request(self, method, path, body, headers):
        self.response_content = "not-json"


class TimeoutConnection(FakeConnection):
    def getresponse(self):
        raise TimeoutError("simulated local inference timeout")


class InvalidReferenceConnection(FakeConnection):
    def request(self, method, path, body, headers):
        self.response_content = json.dumps({
            "schema_version": RESPONSE_VERSION,
            "themes": [{"theme": "Testing", "summary": "A test-related finding.",
                        "evidence_ids": ["not-collected"]}],
        })


class WrongResponseVersionConnection(FakeConnection):
    def request(self, method, path, body, headers):
        super().request(method, path, body, headers)
        output = json.loads(self.response_content)
        output["schema_version"] = "model-emitted-version-is-not-authoritative"
        self.response_content = json.dumps(output)


class FinalTimeoutConnection(FakeConnection):
    def getresponse(self):
        request = json.loads(json.loads(self.body)["messages"][1]["content"])
        if request.get("task") == "consolidate_validated_batch_findings":
            raise TimeoutError("simulated final synthesis timeout")
        return super().getresponse()


class InvalidFinalReferenceConnection(FakeConnection):
    def request(self, method, path, body, headers):
        request = json.loads(json.loads(body)["messages"][1]["content"])
        if request.get("task") == "consolidate_validated_batch_findings":
            self.response_content = json.dumps({
                "schema_version": RESPONSE_VERSION,
                "themes": [{"theme": "Testing", "summary": "A test-related finding.",
                            "evidence_ids": ["unknown-final-id"]}],
            })
        else:
            super().request(method, path, body, headers)


class LocalCommentAgentTests(unittest.TestCase):
    def setUp(self):
        self.connections = []

        def create_connection(host, port, timeout):
            connection = FakeConnection(host, port, timeout)
            self.connections.append(connection)
            return connection

        self.connection_patch = patch(
            "local_comment_agent.http.client.HTTPConnection",
            side_effect=create_connection,
        )
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)

    def test_sends_one_loopback_request_and_validates_evidence_linked_output(self):
        progress = []
        with patch.dict("os.environ", {"GITHUB_TOKEN": "do-not-send-this-token"}):
            result = summarize_comments(
                [make_report()],
                progress_callback=progress.append,
            )
        self.assertEqual(1, len(self.connections))
        connection = self.connections[0]
        self.assertEqual(("127.0.0.1", 11434), (connection.host, connection.port))
        self.assertEqual(("POST", "/api/chat"), (connection.method, connection.path))
        self.assertTrue(connection.closed)
        self.assertNotIn(b"do-not-send-this-token", connection.body)
        sent_request = json.loads(json.loads(connection.body)["messages"][1]["content"])
        self.assertEqual(["E1"], [item["evidence_id"] for item in sent_request["evidence"]])
        self.assertNotIn("https://", sent_request["evidence"][0]["evidence_id"])
        self.assertIn("Summarizing 1 collected comment", progress[0])
        self.assertEqual("Testing", result["themes"][0]["theme"])
        self.assertEqual("local_model", result["generated_by"])
        self.assertTrue(result["uncertain"])
        self.assertEqual("complete", result["synthesis_status"])

    def test_empty_evidence_skips_ollama_and_returns_empty_summary(self):
        report = make_report()
        report["comment_inventory"] = []
        report["comment_activity"]["summary_comment_count"] = 0
        report["comment_activity"]["summary_comment_count_by_source"]["issue_comments"] = 0
        report["comment_activity"]["observed_comment_count"] = 0
        report["comment_activity"]["observed_reviewer_comment_count"] = 0
        report["comment_activity"]["observed_author_response_count"] = 0
        report["comment_activity"]["observed_unknown_author_count"] = 0
        report["comment_activity"]["observed_review_summary_count"] = 0
        report["comment_activity"]["observed_review_comment_count"] = 0
        report["comment_activity"]["observed_issue_comment_count"] = 0
        first_pr_id = next(iter(report["per_pull_request_coverage"]))
        report["per_pull_request_coverage"][first_pr_id]["resources"]["issue_comments"]["count"] = 0
        report["resource_coverage"]["issue_comments"]["successful_empty_pr_count"] = 1
        result = summarize_comments([report])
        self.assertEqual([], result["themes"])
        self.assertEqual([], self.connections)

    def test_http_failure_does_not_echo_local_response_body(self):
        report = make_report()

        def create_failure(host, port, timeout):
            connection = FakeConnection(host, port, timeout)
            connection.status = 404
            self.connections.append(connection)
            return connection

        with patch(
            "local_comment_agent.http.client.HTTPConnection",
            side_effect=create_failure,
        ):
            with self.assertRaises(LocalCommentAgentUnavailable) as raised:
                summarize_comments([report])
        self.assertNotIn("private-body-that-must-not-be-shown", str(raised.exception))

    def test_invalid_model_output_is_never_returned_as_summary(self):
        def create_invalid(host, port, timeout):
            connection = InvalidOutputConnection(host, port, timeout)
            self.connections.append(connection)
            return connection

        with patch(
            "local_comment_agent.http.client.HTTPConnection",
            side_effect=create_invalid,
        ):
            with self.assertRaises(LocalCommentAgentOutputError):
                summarize_comments([make_report()])

    def test_large_utf8_payload_is_losslessly_batched_and_references_original_evidence(self):
        body = "x" * (CONTEXT_SIZE * 2)
        result = summarize_comments([make_report(body)])
        self.assertGreater(result["batch_count"], 1)
        self.assertEqual("complete", result["synthesis_status"])
        self.assertEqual(body, result["evidence"][0]["original_text"])
        self.assertEqual(result["evidence"][0]["evidence_id"], result["themes"][0]["evidence_ids"][0])
        submitted = []
        for connection in self.connections:
            request = json.loads(json.loads(connection.body)["messages"][1]["content"])
            if request.get("evidence"):
                submitted.extend(request["evidence"])
        self.assertEqual(body, "".join(item["original_text"] for item in submitted))
        self.assertTrue(all("url" not in item and "timestamp" not in item for item in submitted))

    def test_more_than_six_prs_are_summarized_by_evidence_batches(self):
        reports = [make_report(
            f"Please add a regression test for boundary {i}; also handle the empty response and verify the retry behavior.",
            repository_id=42 + i,
                               repository_name=f"repo{i}", comment_id=100 + i, pr_number=1)
                   for i in range(8)]
        with patch("local_comment_agent.TARGET_PROMPT_TOKENS", 700):
            result = summarize_comments(reports)
        self.assertEqual("complete", result["synthesis_status"])
        self.assertEqual("completed", result["final_synthesis_status"])
        self.assertEqual(["Portfolio summary"], [theme["theme"] for theme in result["themes"]])
        self.assertEqual(8, result["input_evidence_count"])
        self.assertEqual(8, len(result["evidence"]))
        self.assertGreater(result["batch_count"], 1)
        self.assertEqual(result["batch_count"] + 1, len(self.connections))
        original_ids = {item["evidence_id"] for item in result["evidence"]}
        cited_ids = {evidence_id for theme in result["themes"]
                     for evidence_id in theme["evidence_ids"]}
        cited_ids.update(evidence_id for theme in result["themes"]
                         for claim in theme.get("claims", [])
                         for evidence_id in claim["evidence_ids"])
        self.assertEqual(original_ids, cited_ids)
        self.assertTrue(all(timing["elapsed_ms"] >= 0 for timing in result["timings_ms"]))

    def test_invalid_evidence_references_are_rejected(self):
        def create_invalid_reference(host, port, timeout):
            connection = InvalidReferenceConnection(host, port, timeout)
            self.connections.append(connection)
            return connection
        with patch("local_comment_agent.http.client.HTTPConnection", side_effect=create_invalid_reference):
            with self.assertRaises(LocalCommentAgentOutputError):
                summarize_comments([make_report()])

    def test_adapter_normalizes_model_emitted_version_after_schema_validation(self):
        def create_wrong_version(host, port, timeout):
            connection = WrongResponseVersionConnection(host, port, timeout)
            self.connections.append(connection)
            return connection
        with patch("local_comment_agent.http.client.HTTPConnection", side_effect=create_wrong_version):
            result = summarize_comments([make_report()])
        self.assertEqual("complete", result["synthesis_status"])
        self.assertEqual("local-comment-summary-ui-v2", result["schema_version"])

    def test_synthesis_is_partial_when_collected_source_coverage_is_partial(self):
        report = make_report()
        pr_id = next(iter(report["per_pull_request_coverage"]))
        resource = report["per_pull_request_coverage"][pr_id]["resources"]["issue_comments"]
        resource["status"] = "partial"
        resource["complete_history"] = False
        aggregate = report["resource_coverage"]["issue_comments"]
        aggregate["status"] = "partial"
        aggregate["pr_count_by_status"]["complete"] -= 1
        aggregate["pr_count_by_status"]["partial"] += 1
        aggregate["complete_pr_count"] -= 1
        aggregate["population_complete"] = False
        result = summarize_comments([report])
        self.assertEqual("partial", result["synthesis_status"])
        self.assertEqual("partial", result["collection_coverage_status"])
        self.assertEqual(1, result["completed_batch_count"])
        self.assertIn("incomplete collection coverage", result["synthesis_message"])
        self.assertNotEqual("complete", result["synthesis_status"])

    def test_final_synthesis_timeout_returns_partial_validated_batch_findings(self):
        reports = [make_report(f"Please add test boundary {i}.", repository_id=42 + i,
                               repository_name=f"repo{i}", comment_id=100 + i)
                   for i in range(3)]
        with patch("local_comment_agent.TARGET_PROMPT_TOKENS", 500), patch(
            "local_comment_agent.http.client.HTTPConnection", side_effect=lambda h, p, timeout: (
                self.connections.append(FinalTimeoutConnection(h, p, timeout)) or self.connections[-1]
            )
        ):
            result = summarize_comments(reports)
        self.assertEqual("partial", result["synthesis_status"])
        self.assertEqual("failed", result["final_synthesis_status"])
        self.assertEqual(3, result["input_evidence_count"])
        self.assertEqual(3, result["processed_evidence_count"])
        self.assertTrue(result["themes"])
        self.assertIn("did not finish", result["final_synthesis_failure_reason"])
        self.assertEqual({item["evidence_id"] for item in result["evidence"]},
                         {evidence_id for theme in result["themes"]
                          for evidence_id in theme["evidence_ids"]})

    def test_invalid_final_consolidation_citations_keep_summary_partial(self):
        reports = [make_report(f"Please add test boundary {i}.", repository_id=42 + i,
                               repository_name=f"repo{i}", comment_id=100 + i)
                   for i in range(3)]

        def create_connection(host, port, timeout):
            connection = InvalidFinalReferenceConnection(host, port, timeout)
            self.connections.append(connection)
            return connection

        with patch("local_comment_agent.TARGET_PROMPT_TOKENS", 500), patch(
            "local_comment_agent.http.client.HTTPConnection", side_effect=create_connection
        ):
            result = summarize_comments(reports)
        self.assertEqual("partial", result["synthesis_status"])
        self.assertEqual("failed", result["final_synthesis_status"])
        self.assertIn("invalid evidence references", result["final_synthesis_failure_reason"])
        expected = {item["evidence_id"] for item in result["evidence"]}
        actual = {evidence_id for theme in result["themes"]
                  for evidence_id in theme["evidence_ids"]}
        self.assertEqual(expected, actual)

    def test_batch_timeout_marks_summary_partial_without_dropping_deterministic_path(self):
        reports = [make_report(f"Please add test boundary {i}.", repository_id=42 + i,
                               repository_name=f"repo{i}", comment_id=100 + i)
                   for i in range(3)]
        def create_first_success_then_timeout(host, port, timeout):
            connection = (FakeConnection(host, port, timeout) if not self.connections
                          else TimeoutConnection(host, port, timeout))
            self.connections.append(connection)
            return connection
        with patch("local_comment_agent.TARGET_PROMPT_TOKENS", 500), patch(
            "local_comment_agent.http.client.HTTPConnection", side_effect=create_first_success_then_timeout
        ):
            result = summarize_comments(reports)
        self.assertEqual("partial", result["synthesis_status"])
        self.assertLess(result["processed_evidence_count"], result["input_evidence_count"])
        self.assertTrue(result["themes"])

    def test_ollama_context_overflow_has_safe_diagnostic(self):
        def create_context_failure(host, port, timeout):
            connection = FakeConnection(host, port, timeout)
            connection.status = 500
            connection.server_error = "prompt is too long for the context window: secret prompt text"
            self.connections.append(connection)
            return connection

        with patch(
            "local_comment_agent.http.client.HTTPConnection",
            side_effect=create_context_failure,
        ):
            with self.assertRaises(LocalCommentAgentError) as raised:
                summarize_comments([make_report()])
        self.assertIn("configured local context", str(raised.exception))
        self.assertIn("No comments were truncated", str(raised.exception))
        self.assertNotIn("secret prompt text", str(raised.exception))

    def test_inference_timeout_is_not_reported_as_connection_failure(self):
        def create_timeout(host, port, timeout):
            connection = TimeoutConnection(host, port, timeout)
            self.connections.append(connection)
            return connection

        with patch(
            "local_comment_agent.http.client.HTTPConnection",
            side_effect=create_timeout,
        ):
            with self.assertRaises(LocalCommentAgentUnavailable) as raised:
                summarize_comments([make_report()])
        self.assertIn("did not finish the local summary", str(raised.exception))
        self.assertIn("deterministic recommendations remain available", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
