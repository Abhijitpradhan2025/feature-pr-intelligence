import io
import json
import os
import secrets
import unittest
from datetime import datetime, timezone
from contextlib import redirect_stdout
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from fetch_github_pr_data import (
    AVAILABLE_REPOSITORIES,
    DEFAULT_API_URL,
    DEFAULT_REPOSITORY,
    TARGET_AUTHOR,
    GitHubAPIError,
    GitHubClient,
    GitHubPaginationError,
    discover_accessible_repositories,
    fetch_repository,
    main,
    normalize_event,
    normalize_pull_request,
    parse_utc,
    select_repositories,
    select_resources,
    timestamp_at_or_before,
)


class GitHubClientTests(unittest.TestCase):
    def test_enterprise_defaults_match_configured_repository(self):
        self.assertEqual("https://github.azc.ext.hp.com/api/v3", DEFAULT_API_URL)
        self.assertEqual(
            (
                "abhijit-pradhan1/abhi_sandbox",
                "Ascendion-AAVA-Sandbox/workpath-services-dune",
                "karthikeyan-subhash/hp-expensemanager",
                "Ascendion-AAVA-Sandbox/webjetadmin-development",
            ),
            AVAILABLE_REPOSITORIES,
        )
        self.assertEqual("abhijit-pradhan1/abhi_sandbox", DEFAULT_REPOSITORY)
        client = GitHubClient(DEFAULT_API_URL, "test-token")
        self.assertEqual("https://github.azc.ext.hp.com/api/v3/", client.api_url)

    def test_api_url_rejects_embedded_credentials_query_and_fragment(self):
        for url in (
            "https://user:password@github.example/api/v3",
            "https://github.example/api/v3?token=value",
            "https://github.example/api/v3#fragment",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "without embedded credentials|no embedded credentials"):
                GitHubClient(url, "test-token")

    def test_paginate_follows_link_header(self):
        client = GitHubClient(DEFAULT_API_URL, "test-token")
        responses = iter(
            [
                ([{"id": 1}], {"link": '<https://github.azc.ext.hp.com/api/v3/next?page=2>; rel="next"'}),
                ([{"id": 2}], {}),
            ]
        )
        with patch.object(client, "_request", side_effect=lambda url: next(responses)) as request:
            self.assertEqual([{"id": 1}, {"id": 2}], client.paginate("repos/a/b/issues"))
        self.assertEqual(2, request.call_count)

    def test_pagination_stays_under_enterprise_api_root(self):
        client = GitHubClient(DEFAULT_API_URL, "test-token")
        enterprise_next = f"{DEFAULT_API_URL}/repos/a/b/issues?page=2"
        responses = iter([
            ([{"id": 1}], {"link": f'<{enterprise_next}>; rel="next"'}),
            ([{"id": 2}], {}),
        ])
        requested = []
        with patch.object(client, "_request", side_effect=lambda url: (requested.append(url), next(responses))[1]):
            self.assertEqual([{"id": 1}, {"id": 2}], client.paginate("repos/a/b/issues"))
        self.assertEqual([f"{DEFAULT_API_URL}/repos/a/b/issues", enterprise_next], requested)

        for outside_root in (
            "https://github.azc.ext.hp.com/api/other?page=2",
            "https://github.azc.ext.hp.com/api/v3-malicious?page=2",
        ):
            with self.subTest(url=outside_root), patch.object(
                client, "_request", return_value=([], {"link": f'<{outside_root}>; rel="next"'})
            ), self.assertRaisesRegex(GitHubPaginationError, "outside the configured HTTPS API root"):
                client.paginate("repos/a/b/issues")

    def test_paginate_preserves_completed_pages_when_a_later_page_fails(self):
        client = GitHubClient(DEFAULT_API_URL, "test-token")
        first_page = (
            [{"id": 1}],
            {"link": '<https://github.azc.ext.hp.com/api/v3/next?page=2>; rel="next"'},
        )
        with patch.object(
            client,
            "_request",
            side_effect=[first_page, GitHubAPIError(403, "Resource not accessible")],
        ):
            with self.assertRaises(GitHubPaginationError) as raised:
                client.paginate("repos/a/b/issues")
        self.assertEqual([{"id": 1}], raised.exception.items)
        self.assertEqual(403, raised.exception.status)

    def test_check_runs_pagination_reads_named_array(self):
        client = GitHubClient(DEFAULT_API_URL, "test-token")
        with patch.object(client, "_request", return_value=({"check_runs": [{"id": 7}]}, {})):
            self.assertEqual(
                [{"id": 7}],
                client.paginate("repos/a/b/check-runs", data_key="check_runs"),
            )

    def test_http_errors_are_explicit(self):
        client = GitHubClient(DEFAULT_API_URL, "test-token")
        with patch.object(client, "_request", side_effect=GitHubAPIError(403, "Resource not accessible")):
            with self.assertRaisesRegex(GitHubAPIError, "Resource not accessible"):
                client.paginate("repos/a/b/pulls")

    def test_rate_limit_retries_then_succeeds(self):
        client = GitHubClient(DEFAULT_API_URL, "synthetic-token", max_retries=1)
        headers = Message()
        headers["Retry-After"] = "1"
        limited = HTTPError(
            "https://github.azc.ext.hp.com/api/v3/test", 429, "Rate limited",
            headers, io.BytesIO(b'{"message":"rate limit"}'),
        )
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.headers.items.return_value = []
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with patch("fetch_github_pr_data.urlopen", side_effect=[limited, context]), patch(
            "fetch_github_pr_data.time.sleep"
        ) as sleep:
            payload, _ = client._request("https://github.azc.ext.hp.com/api/v3/test")
        self.assertEqual({"ok": True}, payload)
        self.assertEqual(1, sleep.call_count)

    def test_pagination_refuses_cross_host_links(self):
        client = GitHubClient(DEFAULT_API_URL, "test-token")
        with patch.object(
            client,
            "_request",
            return_value=([], {"link": '<https://attacker.example/steal>; rel="next"'}),
        ):
            with self.assertRaisesRegex(
                GitHubAPIError, "outside the configured HTTPS API root"
            ):
                client.paginate("repos/a/b/pulls")

    def test_request_uses_bearer_header_and_does_not_echo_token_in_errors(self):
        token = "synthetic-token-" + secrets.token_hex(24)
        client = GitHubClient(DEFAULT_API_URL, token)
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.headers.items.return_value = []
        response_context = MagicMock()
        response_context.__enter__.return_value = response
        response_context.__exit__.return_value = False
        requests = []

        def successful_request(request, timeout):
            requests.append(request)
            return response_context

        with patch("fetch_github_pr_data.urlopen", side_effect=successful_request):
            self.assertEqual({"ok": True}, client._request("https://github.azc.ext.hp.com/api/v3/test")[0])
        self.assertEqual(f"Bearer {token}", requests[0].get_header("Authorization"))

        body = json.dumps({"message": f"server echoed {token}"}).encode()
        error = HTTPError(
            "https://github.azc.ext.hp.com/api/v3/test",
            403,
            "Forbidden",
            Message(),
            io.BytesIO(body),
        )
        with patch("fetch_github_pr_data.urlopen", side_effect=error):
            with self.assertRaises(GitHubAPIError) as raised:
                client._request("https://github.azc.ext.hp.com/api/v3/test")
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_select_repositories_supports_multi_and_rejects_invalid_or_duplicates(self):
        self.assertEqual(
            ["first/repo", "second/repo"],
            select_repositories(["first/repo", "second/repo"]),
        )
        self.assertEqual(list(AVAILABLE_REPOSITORIES), select_repositories([], all_repositories=True))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            select_repositories(["Owner/Repo", "owner/repo"])
        with self.assertRaisesRegex(ValueError, "OWNER/REPOSITORY"):
            select_repositories(["not-a-repository"])
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            select_repositories(["owner/repo"], all_repositories=True)

    def test_main_invokes_each_requested_repository_and_emits_independent_envelopes(self):
        token = "synthetic-token-" + secrets.token_hex(24)
        fetched = []
        api_urls = []

        def fake_fetch(client, owner, repository, *args, **kwargs):
            api_urls.append(client.api_url)
            fetched.append((owner, repository, kwargs.get("include")))
            return {
                "schema_version": "0.1",
                "status": "complete",
                "repository": {"owner": owner, "name": repository},
            }

        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_TOKEN": token, "GITHUB_API_URL": "https://conflict.example/api/v3"}), patch(
            "fetch_github_pr_data.fetch_repository", side_effect=fake_fetch
        ), redirect_stdout(output):
            result = main(
                [
                    "first/repo",
                    "second/repo",
                    "--from",
                    "2026-01-01",
                    "--to",
                    "2026-01-31",
                    "--include",
                    "pull_requests",
                    "reviews",
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual(
            [("first", "repo", ["pull_requests", "reviews"]),
             ("second", "repo", ["pull_requests", "reviews"])],
            fetched,
        )
        self.assertEqual(
            [("first", "repo"), ("second", "repo")],
            [(item["repository"]["owner"], item["repository"]["name"]) for item in json.loads(output.getvalue())],
        )
        self.assertEqual([DEFAULT_API_URL + "/"] * 2, api_urls)
        self.assertNotIn(token, output.getvalue())

    def test_repository_discovery_is_offline_and_needs_no_token(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            result = main(["--list-repositories"])
        self.assertEqual(0, result)
        self.assertEqual(list(AVAILABLE_REPOSITORIES), output.getvalue().splitlines())

    def test_authenticated_repository_discovery_paginates_and_normalizes_metadata(self):
        client = GitHubClient("https://enterprise.example/api/v3", "synthetic-token")
        first = {
            "id": 5,
            "owner": {"login": "team"},
            "name": "private-repo",
            "full_name": "team/private-repo",
            "html_url": "https://enterprise.example/team/private-repo",
            "default_branch": "trunk",
            "language": "Python",
            "visibility": "private",
            "permissions": {"pull": True, "push": False, "admin": False},
        }
        second = {
            "id": 6,
            "owner": {"login": "team"},
            "name": "public-repo",
            "full_name": "team/public-repo",
            "html_url": "https://enterprise.example/team/public-repo",
            "default_branch": "main",
            "language": None,
            "private": False,
            "permissions": {"pull": True},
        }
        responses = iter(
            [
                ([first], {"link": '<https://enterprise.example/api/v3/user/repos?page=2>; rel="next"'}),
                ([second], {}),
            ]
        )
        requested_urls = []

        def fake_request(url):
            requested_urls.append(url)
            return next(responses)

        with patch.object(client, "_request", side_effect=fake_request):
            repositories = discover_accessible_repositories(client)

        self.assertEqual(2, len(requested_urls))
        self.assertTrue(all(url.startswith("https://enterprise.example/api/v3/user/repos") for url in requested_urls))
        self.assertEqual(
            [
                {
                    "id": 5,
                    "owner": "team",
                    "name": "private-repo",
                    "full_name": "team/private-repo",
                    "url": "https://enterprise.example/team/private-repo",
                    "default_branch": "trunk",
                    "language": "Python",
                    "visibility": "private",
                    "permissions": {"pull": True, "push": False, "admin": False},
                },
                {
                    "id": 6,
                    "owner": "team",
                    "name": "public-repo",
                    "full_name": "team/public-repo",
                    "url": "https://enterprise.example/team/public-repo",
                    "default_branch": "main",
                    "language": None,
                    "visibility": "public",
                    "permissions": {"pull": True},
                },
            ],
            repositories,
        )
        self.assertNotIn("synthetic-token", json.dumps(repositories))


class NormalizationTests(unittest.TestCase):
    @staticmethod
    def pull_request(pr_id=100, number=12):
        return {
            "id": pr_id, "number": number,
            "created_at": "2026-01-05T00:00:00Z",
            "user": {"id": 44, "login": TARGET_AUTHOR},
            "base": {"ref": "main"},
            "head": {"ref": "feature", "sha": "head-sha"},
            "labels": [],
            "html_url": f"https://github.example/a/b/pull/{number}",
            "url": f"https://github.example/api/repos/a/b/pulls/{number}",
        }

    def test_resource_selection_validation_and_default(self):
        self.assertEqual(
            ["pull_requests", "pull_request_details", "lifecycle_events", "commits", "reviews",
             "review_comments", "issue_comments", "review_threads", "checks"],
            select_resources(None),
        )
        self.assertEqual(
            ["pull_requests", "reviews"],
            select_resources(["reviews", "pull_requests"]),
        )
        for invalid, text in [
            (["pull_requests", "unknown"], "unknown resource"),
            (["pull_requests", "pull_requests"], "duplicate"),
            (["reviews"], "required"),
        ]:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, text):
                select_resources(invalid)

    def test_fetch_filters_prs_to_multiple_selected_authors_without_duplicates(self):
        first = self.pull_request(100, 12)
        first["user"]["login"] = "author1"
        second = self.pull_request(101, 13)
        second["user"]["login"] = "AUTHOR2"
        other = self.pull_request(102, 14)
        other["user"]["login"] = "author3"

        class Client:
            api_url = "https://enterprise.example/api/v3"

            def get(self, path):
                return ({"id": 9}, {})

            def paginate(self, path, params=None, *, data_key=None):
                return [first, second, other]

        result = fetch_repository(
            Client(), "a", "b", parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True), parse_utc("2026-02-01"),
            author="author1, author2, AUTHOR1", include=["pull_requests"],
        )
        self.assertEqual([12, 13], [pr["number"] for pr in result["pull_requests"]])
        self.assertEqual(2, len({pr["id"] for pr in result["pull_requests"]}))

    def test_excluded_resources_are_not_called_and_not_empty_success(self):
        pr = self.pull_request()

        class Client:
            api_url = "https://github.example/api/"
            def __init__(self): self.paths = []
            def get(self, path):
                return ({"id": 9, "html_url": "https://github.example/a/b"}, {})
            def paginate(self, path, params=None, *, data_key=None):
                self.paths.append(path)
                return [pr] if path.endswith("/pulls") else []

        client = Client()
        result = fetch_repository(
            client, "a", "b", parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True), parse_utc("2026-02-01"),
            include=["pull_requests"],
        )
        self.assertEqual(["repos/a/b/pulls"], client.paths)
        self.assertNotIn("reviews", result)
        self.assertEqual("not_requested", result["resources"]["reviews"]["status"])
        self.assertIsNone(result["resources"]["reviews"]["count"])
        self.assertEqual(9, result["pull_requests"][0]["repository_id"])
        self.assertEqual({"owner": "a", "name": "b"}, result["pull_requests"][0]["repository"])
        self.assertEqual(
            {"status": "complete", "count": 1, "pagination_complete": True,
             "history_complete": True, "coverage_start": pr["created_at"],
             "coverage_end": "2026-02-01T00:00:00Z"},
            result["pull_request_resources"]["100"]["pull_requests"],
        )

    def test_pull_request_detail_fetch_populates_size_counts_and_tracks_coverage(self):
        pr = self.pull_request()

        class Client:
            api_url = "https://github.example/api/"

            def __init__(self, detail_fails=False, omit_counts=False):
                self.detail_fails = detail_fails
                self.omit_counts = omit_counts
                self.get_paths = []

            def get(self, path):
                self.get_paths.append(path)
                if path == "repos/a/b":
                    return ({"id": 9, "html_url": "https://github.example/a/b"}, {})
                if self.detail_fails:
                    raise GitHubAPIError(403, "Resource not accessible")
                if self.omit_counts:
                    return ({"id": 100}, {})
                return ({"additions": 25, "deletions": 4, "changed_files": 3}, {})

            def paginate(self, path, params=None, *, data_key=None):
                return [pr]

        args = (
            "a",
            "b",
            parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True),
            parse_utc("2026-02-01"),
        )
        client = Client()
        result = fetch_repository(
            client,
            *args,
            include=["pull_requests", "pull_request_details"],
        )
        enriched = result["pull_requests"][0]
        self.assertEqual(
            (25, 4, 3),
            (enriched["additions"], enriched["deletions"], enriched["changed_files"]),
        )
        self.assertIn("repos/a/b/pulls/12", client.get_paths)
        self.assertEqual(
            "complete",
            result["pull_request_resources"]["100"]["pull_request_details"]["status"],
        )
        self.assertEqual(
            "complete",
            result["resources"]["pull_request_details"]["status"],
        )

        incomplete = fetch_repository(
            Client(omit_counts=True),
            *args,
            include=["pull_requests", "pull_request_details"],
        )
        self.assertEqual(
            "partial",
            incomplete["pull_request_resources"]["100"]["pull_request_details"]["status"],
        )
        self.assertIsNone(
            incomplete["pull_request_resources"]["100"]["pull_request_details"]["count"]
        )
        self.assertEqual(
            "partial",
            incomplete["resources"]["pull_request_details"]["status"],
        )

        failed = fetch_repository(
            Client(detail_fails=True),
            *args,
            include=["pull_requests", "pull_request_details"],
        )
        self.assertEqual(
            (None, None, None),
            (
                failed["pull_requests"][0]["additions"],
                failed["pull_requests"][0]["deletions"],
                failed["pull_requests"][0]["changed_files"],
            ),
        )
        self.assertEqual(
            "unavailable",
            failed["pull_request_resources"]["100"]["pull_request_details"]["status"],
        )
        self.assertIsNone(failed["pull_request_resources"]["100"]["pull_request_details"]["count"])
        self.assertEqual("unavailable", failed["resources"]["pull_request_details"]["status"])

    def test_every_activity_type_has_stable_repository_and_pr_identity(self):
        pr = self.pull_request()

        class Client:
            api_url = "https://github.example/api/"
            def get(self, path): return ({"id": 9}, {})
            def paginate(self, path, params=None, *, data_key=None):
                if path.endswith("/pulls"): return [pr]
                if path.endswith("/timeline"):
                    return [{"id": 1, "event": "reopened", "created_at": "2026-01-06T00:00:00Z"}]
                if path.endswith("/commits"):
                    return [{"sha": "abc", "commit": {"author": {"date": "2026-01-06T00:00:00Z"}}}]
                if path.endswith("/reviews"):
                    return [{"id": 2, "submitted_at": "2026-01-06T00:00:00Z"}]
                if path.endswith("/comments") and "/pulls/" in path:
                    return [{"id": 3, "created_at": "2026-01-06T00:00:00Z"}]
                if path.endswith("/comments"):
                    return [{"id": 4, "created_at": "2026-01-06T00:00:00Z"}]
                if path.endswith("/check-runs"):
                    return [{"id": 5, "name": "CI"}]
                return []

        result = fetch_repository(
            Client(), "a", "b", parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True), parse_utc("2026-02-01"),
            include=["pull_requests", "lifecycle_events", "commits", "reviews",
                     "review_comments", "issue_comments", "checks"],
        )
        self.assertEqual(9, result["repository"]["id"])
        records = [
            result["events"][0], result["commits"][0], result["reviews"][0],
            result["review_comments"][0], result["issue_comments"][0], result["checks"][0],
        ]
        for record in records:
            self.assertEqual(9, record["repository_id"])
            self.assertEqual({"owner": "a", "name": "b"}, record["repository"])
            self.assertEqual(100, record["pull_request_id"])
            self.assertEqual(12, record["pull_request_number"])
        self.assertEqual("abc", result["commits"][0]["sha"])
        self.assertEqual(1, result["events"][0]["id"])
        self.assertEqual("partial", result["pull_request_resources"]["100"]["checks"]["status"])
        self.assertFalse(result["pull_request_resources"]["100"]["checks"]["history_complete"])

    def test_invalid_activity_timestamps_are_preserved_for_metric_quality_counts(self):
        pr = self.pull_request()
        invalid_timestamp = "not-a-timestamp"

        class Client:
            api_url = "https://github.example/api/"
            def get(self, path): return ({"id": 9}, {})
            def paginate(self, path, params=None, *, data_key=None):
                if path.endswith("/pulls"): return [pr]
                if path.endswith("/reviews"):
                    return [{"id": 12, "submitted_at": invalid_timestamp}]
                return []

        result = fetch_repository(
            Client(), "a", "b", parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True), parse_utc("2026-02-01"),
            include=["pull_requests", "reviews"],
        )
        self.assertEqual(invalid_timestamp, result["reviews"][0]["submitted_at"])
        self.assertEqual(
            "complete",
            result["pull_request_resources"]["100"]["reviews"]["status"],
        )
        self.assertTrue(timestamp_at_or_before(invalid_timestamp, parse_utc("2026-02-01")))
        malformed_closed = normalize_event(
            {"id": 9, "event": "closed", "created_at": invalid_timestamp},
            12,
            "also-invalid",
        )
        self.assertEqual("closed", malformed_closed["kind"])
        self.assertEqual(invalid_timestamp, malformed_closed["occurred_at"])

    def test_per_pr_empty_success_differs_from_permission_failure(self):
        pr = self.pull_request()

        class Client:
            api_url = "https://github.example/api/"
            def __init__(self, fail): self.fail = fail
            def get(self, path): return ({"id": 9}, {})
            def paginate(self, path, params=None, *, data_key=None):
                if path.endswith("/pulls"): return [pr]
                if self.fail: raise GitHubAPIError(403, "Resource not accessible")
                return []

        args = (
            "a", "b", parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True), parse_utc("2026-02-01"),
        )
        success = fetch_repository(
            Client(False), *args, include=["pull_requests", "reviews"]
        )
        failed = fetch_repository(
            Client(True), *args, include=["pull_requests", "reviews"]
        )
        good = success["pull_request_resources"]["100"]["reviews"]
        bad = failed["pull_request_resources"]["100"]["reviews"]
        self.assertEqual(("complete", 0, True),
                         (good["status"], good["count"], good["history_complete"]))
        self.assertEqual(("unavailable", None, False),
                         (bad["status"], bad["count"], bad["history_complete"]))
        self.assertEqual("unavailable", failed["resources"]["reviews"]["status"])

    def test_partial_feedback_pages_are_retained_with_partial_coverage(self):
        pr = self.pull_request()

        class Client:
            api_url = "https://github.example/api/"

            def get(self, path):
                return ({"id": 9}, {})

            def paginate(self, path, params=None, *, data_key=None):
                if path.endswith("/pulls"):
                    return [pr]
                if path.endswith("/reviews"):
                    return self.partial_page([{
                        "id": 21,
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-01-06T00:00:00Z",
                        "user": {"id": 77, "login": "reviewer"},
                        "body": "Please add a test",
                    }])
                if path.endswith("/comments") and "/pulls/" in path:
                    return self.partial_page([{
                        "id": 22,
                        "created_at": "2026-01-07T00:00:00Z",
                        "user": {"id": 77, "login": "reviewer"},
                        "body": "Handle the edge case",
                    }])
                if path.endswith("/comments"):
                    return self.partial_page([{
                        "id": 23,
                        "created_at": "2026-01-08T00:00:00Z",
                        "user": {"id": 44, "login": TARGET_AUTHOR},
                        "body": "I updated the implementation",
                    }])
                return []

            @staticmethod
            def partial_page(items):
                raise GitHubPaginationError(403, "page two unavailable", items)

        result = fetch_repository(
            Client(), "a", "b", parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True), parse_utc("2026-02-01"),
            include=["pull_requests", "reviews", "review_comments", "issue_comments"],
        )
        for source in ("reviews", "review_comments", "issue_comments"):
            self.assertEqual(1, len(result[source]))
            coverage = result["pull_request_resources"]["100"][source]
            self.assertEqual("partial", coverage["status"])
            self.assertEqual(1, coverage["count"])
            self.assertFalse(coverage["pagination_complete"])
            self.assertEqual("partial", result["resources"][source]["status"])

    def test_partial_pull_request_listing_keeps_observed_prs_and_marks_population_partial(self):
        pr = self.pull_request()

        class Client:
            api_url = "https://github.example/api/"

            def get(self, path):
                return ({"id": 9}, {})

            def paginate(self, path, params=None, *, data_key=None):
                raise GitHubPaginationError(403, "later page unavailable", [pr])

        result = fetch_repository(
            Client(), "a", "b", parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True), parse_utc("2026-02-01"),
            include=["pull_requests"],
        )
        self.assertEqual(1, len(result["pull_requests"]))
        self.assertEqual("partial", result["resources"]["pull_requests"]["status"])
        self.assertEqual(1, result["resources"]["pull_requests"]["count"])
        self.assertFalse(result["resources"]["pull_requests"]["pagination_complete"])
        self.assertEqual("partial", result["status"])

    def test_all_feedback_sources_are_paginated_and_include_post_window_activity(self):
        pr = self.pull_request()
        cutoff = parse_utc("2026-02-10")
        requested = []
        progress_messages = []

        class Client:
            api_url = "https://enterprise.example/api/v3/"

            def get(self, path):
                return ({"id": 9, "html_url": "https://enterprise.example/a/b"}, {})

            def paginate(self, path, params=None, *, data_key=None):
                requested.append((path, params))
                if path.endswith("/pulls"):
                    return [pr]
                if path.endswith("/reviews"):
                    return [{
                        "id": 21, "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-02-02T12:00:00Z",
                        "user": {"id": 77, "login": "reviewer"},
                        "body": "Please add a test",
                    }]
                if path.endswith("/comments") and "/pulls/" in path:
                    return [{
                        "id": 22, "created_at": "2026-02-03T12:00:00Z",
                        "user": {"id": 77, "login": "reviewer"},
                        "body": "Handle this edge case",
                    }]
                if path.endswith("/comments"):
                    return [{
                        "id": 23, "created_at": "2026-02-04T12:00:00Z",
                        "user": {"id": 44, "login": TARGET_AUTHOR},
                        "body": "I added the test",
                    }]
                return []

        result = fetch_repository(
            Client(), "a", "b",
            parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True),
            cutoff,
            author=TARGET_AUTHOR,
            include=["pull_requests", "reviews", "review_comments", "issue_comments"],
            progress_callback=progress_messages.append,
        )

        self.assertEqual(
            {
                "repos/a/b/pulls/12/reviews",
                "repos/a/b/pulls/12/comments",
                "repos/a/b/issues/12/comments",
            },
            {path for path, _ in requested if path != "repos/a/b/pulls"},
        )
        feedback_requests = [
            (path, params) for path, params in requested
            if path != "repos/a/b/pulls"
        ]
        self.assertTrue(all(params == {"per_page": 100} for _, params in feedback_requests))
        self.assertEqual(
            ["2026-02-02T12:00:00Z"],
            [item["submitted_at"] for item in result["reviews"]],
        )
        self.assertEqual(
            ["2026-02-03T12:00:00Z"],
            [item["created_at"] for item in result["review_comments"]],
        )
        self.assertEqual(
            ["2026-02-04T12:00:00Z"],
            [item["created_at"] for item in result["issue_comments"]],
        )
        self.assertTrue(all(
            result["pull_request_resources"]["100"][name]["status"] == "complete"
            for name in ("reviews", "review_comments", "issue_comments")
        ))
        self.assertTrue(any("Found 1 matching pull request" in message
                            for message in progress_messages))
        self.assertTrue(any("Loaded 1 reviews record" in message
                            for message in progress_messages))
        self.assertTrue(any("Loaded 1 review comments record" in message
                            for message in progress_messages))
        self.assertTrue(any("Loaded 1 issue comments record" in message
                            for message in progress_messages))

    def test_one_feedback_source_failure_is_not_reported_as_empty(self):
        pr = self.pull_request()

        class Client:
            api_url = "https://enterprise.example/api/v3/"

            def get(self, path):
                return ({"id": 9}, {})

            def paginate(self, path, params=None, *, data_key=None):
                if path.endswith("/pulls"):
                    return [pr]
                if path.endswith("/reviews"):
                    return [{
                        "id": 31, "state": "COMMENTED",
                        "submitted_at": "2026-01-06T00:00:00Z",
                        "user": {"id": 77, "login": "reviewer"},
                        "body": "Please add a test",
                    }]
                if path.endswith("/comments") and "/pulls/" in path:
                    return []
                if path.endswith("/comments"):
                    raise GitHubAPIError(403, "Resource not accessible")
                return []

        result = fetch_repository(
            Client(), "a", "b",
            parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True),
            parse_utc("2026-02-01"),
            author=TARGET_AUTHOR,
            include=["pull_requests", "reviews", "review_comments", "issue_comments"],
        )

        self.assertEqual(1, len(result["reviews"]))
        self.assertEqual(0, len(result["review_comments"]))
        self.assertEqual("complete", result["resources"]["review_comments"]["status"])
        self.assertEqual("unavailable", result["resources"]["issue_comments"]["status"])
        self.assertIsNone(result["resources"]["issue_comments"]["count"])
        self.assertEqual(
            ("unavailable", None, False),
            (
                result["pull_request_resources"]["100"]["issue_comments"]["status"],
                result["pull_request_resources"]["100"]["issue_comments"]["count"],
                result["pull_request_resources"]["100"]["issue_comments"]["history_complete"],
            ),
        )
        self.assertEqual("partial", result["status"])

    def test_prior_pr_success_then_later_failure_is_partial_not_empty(self):
        prs = [self.pull_request(100, 12), self.pull_request(101, 13)]

        class Client:
            api_url = "https://github.example/api/"
            def get(self, path): return ({"id": 9}, {})
            def paginate(self, path, params=None, *, data_key=None):
                if path.endswith("/pulls"): return prs
                if path.endswith("/reviews") and "/12/" in path: return []
                if path.endswith("/reviews"): raise GitHubAPIError(403, "permission denied")
                return []

        result = fetch_repository(
            Client(), "a", "b", parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True), parse_utc("2026-02-01"),
            include=["pull_requests", "reviews"],
        )
        self.assertEqual("partial", result["resources"]["reviews"]["status"])
        self.assertEqual(0, result["resources"]["reviews"]["count"])
        self.assertFalse(result["resources"]["reviews"]["history_complete"])
        self.assertEqual("complete", result["pull_request_resources"]["100"]["reviews"]["status"])
        self.assertEqual("unavailable", result["pull_request_resources"]["101"]["reviews"]["status"])
        self.assertEqual("partial", result["status"])

    def test_fetch_envelope_distinguishes_empty_from_unavailable(self):
        pull_request = {
            "id": 1,
            "number": 12,
            "created_at": "2026-01-05T00:00:00Z",
            "user": {"id": 44, "login": TARGET_AUTHOR},
            "base": {"ref": "main"},
            "head": {"ref": "feature", "sha": "abc"},
            "labels": [],
            "html_url": "https://github.azc.ext.hp.com/a/b/pull/12",
            "url": "https://github.azc.ext.hp.com/api/v3/repos/a/b/pulls/12",
        }

        class FakeClient:
            api_url = "https://github.azc.ext.hp.com/api/v3/"

            def get(self, path):
                return ({"html_url": "https://github.azc.ext.hp.com/a/b"}, {})

            def paginate(self, path, params=None, *, data_key=None):
                if path.endswith("/pulls"):
                    return [pull_request]
                return []

        result = fetch_repository(
            FakeClient(),
            "a",
            "b",
            parse_utc("2026-01-01"),
            parse_utc("2026-01-31", date_end=True),
            parse_utc("2026-02-01"),
        )
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["resources"]["pull_requests"]["count"])
        self.assertEqual("complete", result["resources"]["reviews"]["status"])
        self.assertEqual(0, result["resources"]["reviews"]["count"])
        self.assertEqual("unavailable", result["resources"]["review_threads"]["status"])

    def test_normalizes_pull_request_and_provenance(self):
        result = normalize_pull_request(
            {
                "id": 1,
                "number": 12,
                "user": {"id": 44, "login": TARGET_AUTHOR},
                "base": {"ref": "main"},
                "head": {"ref": "feature"},
                "labels": [{"name": "bug"}],
                "html_url": "https://github.azc.ext.hp.com/a/b/pull/12",
                "url": "https://github.azc.ext.hp.com/api/v3/repos/a/b/pulls/12",
            }
        )
        self.assertEqual(44, result["author"]["id"])
        self.assertEqual("main", result["base_ref"])
        self.assertEqual(["bug"], result["labels"])
        self.assertEqual(
            "https://github.azc.ext.hp.com/api/v3/repos/a/b/pulls/12",
            result["source_url"],
        )

    def test_normalizes_closed_timeline_event_as_merge_at_known_merge_timestamp(self):
        result = normalize_event(
            {"id": 4, "event": "closed", "commit_id": "abc", "created_at": "2026-01-01T00:00:00Z"},
            12,
            "2026-01-01T00:00:00Z",
        )
        self.assertEqual("merged", result["kind"])
        self.assertEqual(12, result["pull_request_number"])

    def test_date_bounds_are_utc_and_inclusive_day_end(self):
        start = parse_utc("2026-01-01")
        end = parse_utc("2026-01-01", date_end=True)
        self.assertEqual(timezone.utc, start.tzinfo)
        self.assertEqual(datetime(2026, 1, 1, 23, 59, 59, 999999, tzinfo=timezone.utc), end)


if __name__ == "__main__":
    unittest.main()
