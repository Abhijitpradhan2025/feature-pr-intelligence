"""Fetch read-only GitHub pull-request data as a versioned JSON envelope."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, time as datetime_time, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

try:
    from .author_filter import parse_author_logins
except ImportError:
    from author_filter import parse_author_logins


API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://github.azc.ext.hp.com/api/v3"
AVAILABLE_REPOSITORIES = (
    "abhijit-pradhan1/abhi_sandbox",
    "Ascendion-AAVA-Sandbox/workpath-services-dune",
    "karthikeyan-subhash/hp-expensemanager",
    "Ascendion-AAVA-Sandbox/webjetadmin-development",
)
DEFAULT_REPOSITORY = AVAILABLE_REPOSITORIES[0]
TARGET_AUTHOR = "abhijit-pradhan1"
USER_AGENT = "pr-intelligence-dashboard/0.1"
UTC = timezone.utc
SUPPORTED_RESOURCES = (
    "pull_requests",
    "pull_request_details",
    "lifecycle_events",
    "commits",
    "reviews",
    "review_comments",
    "issue_comments",
    "review_threads",
    "checks",
)


def select_resources(requested: list[str] | tuple[str, ...] | None) -> list[str]:
    """Validate a selection; default to the existing full collection behavior."""
    resources = list(SUPPORTED_RESOURCES if requested is None else requested)
    if "pull_requests" not in resources:
        raise ValueError("pull_requests is a required resource")
    seen: set[str] = set()
    for resource in resources:
        if resource not in SUPPORTED_RESOURCES:
            raise ValueError(f"unknown resource: {resource}")
        if resource in seen:
            raise ValueError(f"duplicate resource: {resource}")
        seen.add(resource)
    return [name for name in SUPPORTED_RESOURCES if name in seen]


class GitHubAPIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class GitHubPaginationError(GitHubAPIError):
    def __init__(self, status: int, message: str, items: list[dict[str, Any]]):
        super().__init__(status, message)
        self.items = items


def parse_utc(value: str, *, date_end: bool = False) -> datetime:
    """Parse an ISO date or timestamp, returning an aware UTC datetime."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = date.fromisoformat(value)
            parsed = datetime.combine(
                day,
                datetime_time.max if date_end else datetime_time.min,
                tzinfo=UTC,
            )
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp must include a timezone")
            parsed = parsed.astimezone(UTC)
    except ValueError as exc:
        raise ValueError(
            f"invalid ISO date/timestamp {value!r}: {exc}"
        ) from exc
    return parsed


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp_at_or_before(value: Any, cutoff: datetime) -> bool:
    """Keep unparseable timestamps as evidence so analytics can count them as invalid."""
    if value is None:
        return True
    try:
        return parse_utc(value) <= cutoff
    except (TypeError, ValueError):
        return True


class GitHubClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        parsed = urlparse(api_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "GitHub API URL must be HTTPS and contain no embedded credentials, query, or fragment"
            )
        self.api_url = api_url.rstrip("/") + "/"
        if not token or token.strip() != token:
            raise ValueError("GitHub token must be non-empty and have no surrounding whitespace.")
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries

    def _redact(self, message: Any) -> str:
        """Prevent a server or transport error from echoing the configured credential."""
        return str(message).replace(self.token, "[REDACTED]") if self.token else str(message)

    def _request(self, url: str) -> tuple[Any, dict[str, str]]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        for attempt in range(self.max_retries + 1):
            request = Request(url, headers=headers)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    try:
                        payload = json.loads(response.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise GitHubAPIError(
                            0, "GitHub returned a response that was not valid JSON"
                        ) from exc
                    return payload, {k.lower(): v for k, v in response.headers.items()}
            except HTTPError as exc:
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                finally:
                    exc.close()
                try:
                    detail = json.loads(body).get("message", body)
                except (json.JSONDecodeError, AttributeError):
                    detail = body
                detail = self._redact(detail)
                rate_limited = (
                    exc.code == 429
                    or (
                        exc.code == 403
                        and (
                            exc.headers.get("X-RateLimit-Remaining") == "0"
                            or "rate limit" in str(detail).lower()
                        )
                    )
                )
                if rate_limited and attempt < self.max_retries:
                    delay = self._retry_delay(exc.headers)
                    time.sleep(delay)
                    continue
                raise GitHubAPIError(exc.code, str(detail)) from exc
            except (URLError, TimeoutError) as exc:
                raise GitHubAPIError(0, self._redact(f"GitHub request failed: {exc}")) from exc
        raise GitHubAPIError(0, "GitHub request retries exhausted")

    @staticmethod
    def _retry_delay(headers: Any) -> int:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return max(1, int(retry_after))
            except ValueError:
                try:
                    return max(
                        1,
                        int((parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds()),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        reset = headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(1, int(reset) - int(time.time()))
            except ValueError:
                pass
        return 5

    def get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        url = urljoin(self.api_url, path.lstrip("/"))
        if params:
            url += "?" + urlencode(params)
        return self._request(url)

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        data_key: str | None = None,
    ) -> list[dict[str, Any]]:
        url = urljoin(self.api_url, path.lstrip("/"))
        if params:
            url += "?" + urlencode(params)
        result: list[dict[str, Any]] = []
        successful_pages = 0
        while url:
            try:
                payload, headers = self._request(url)
            except GitHubAPIError as error:
                if successful_pages:
                    raise GitHubPaginationError(
                        error.status,
                        str(error),
                        result,
                    ) from error
                raise
            page = payload.get(data_key, []) if data_key else payload
            if not isinstance(page, list):
                error = GitHubAPIError(0, f"Expected a list from endpoint {path}")
                if successful_pages:
                    raise GitHubPaginationError(error.status, str(error), result) from error
                raise error
            result.extend(item for item in page if isinstance(item, dict))
            successful_pages += 1
            next_url = self._next_link(headers.get("link", ""))
            if next_url:
                next_origin = urlparse(next_url)
                api_origin = urlparse(self.api_url)
                if (
                    next_origin.scheme != "https"
                    or next_origin.netloc.lower() != api_origin.netloc.lower()
                    or not (
                        next_origin.path.rstrip("/") == api_origin.path.rstrip("/")
                        or next_origin.path.startswith(api_origin.path.rstrip("/") + "/")
                    )
                ):
                    error = GitHubAPIError(
                        0,
                        "Refusing pagination link outside the configured HTTPS API root",
                    )
                    if successful_pages:
                        raise GitHubPaginationError(
                            error.status,
                            str(error),
                            result,
                        ) from error
                    raise error
            url = next_url
        return result

    @staticmethod
    def _next_link(link_header: str) -> str | None:
        for item in link_header.split(","):
            match = re.match(r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"', item)
            if match and match.group(2) == "next":
                return match.group(1)
        return None


def user_identity(user: Any) -> dict[str, Any] | None:
    if not isinstance(user, dict):
        return None
    return {"id": user.get("id"), "login": user.get("login")}


def repository_identity(owner: str, name: str) -> dict[str, str]:
    return {"owner": owner, "name": name}


def activity_identity(repository_id: Any, owner: str, repository: str,
                      pr_id: Any, number: int) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "repository": repository_identity(owner, repository),
        "pull_request_id": pr_id,
        "pull_request_number": number,
    }


def normalize_pull_request(
    pr: dict[str, Any], repository_id: Any = None, owner: str | None = None,
    repository: str | None = None,
) -> dict[str, Any]:
    result = {
        "id": pr.get("id"),
        "number": pr.get("number"),
        "title": pr.get("title"),
        "body": pr.get("body"),
        "state": pr.get("state"),
        "author": user_identity(pr.get("user")),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "closed_at": pr.get("closed_at"),
        "merged_at": pr.get("merged_at"),
        "base_ref": (pr.get("base") or {}).get("ref"),
        "head_ref": (pr.get("head") or {}).get("ref"),
        "draft": pr.get("draft"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "labels": [label.get("name") for label in pr.get("labels", []) if isinstance(label, dict)],
        "url": pr.get("html_url"),
        "source_url": pr.get("url"),
    }
    if owner is not None and repository is not None:
        result["repository_id"] = repository_id
        result["repository"] = repository_identity(owner, repository)
    return result


def normalize_review(item: dict[str, Any], number: int, repository_id: Any = None,
                     owner: str | None = None, repository: str | None = None,
                     pr_id: Any = None) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        **(activity_identity(repository_id, owner, repository, pr_id, number)
           if owner is not None and repository is not None else {"pull_request_number": number}),
        "state": item.get("state"),
        "author": user_identity(item.get("user")),
        "submitted_at": item.get("submitted_at"),
        "body": item.get("body"),
        "url": item.get("html_url"),
        "source_url": item.get("url"),
    }


def normalize_comment(item: dict[str, Any], number: int, repository_id: Any = None,
                      owner: str | None = None, repository: str | None = None,
                      pr_id: Any = None) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        **(activity_identity(repository_id, owner, repository, pr_id, number)
           if owner is not None and repository is not None else {"pull_request_number": number}),
        "author": user_identity(item.get("user")),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "body": item.get("body"),
        "url": item.get("html_url"),
        "source_url": item.get("url"),
    }


def normalize_commit(item: dict[str, Any], number: int, repository_id: Any = None,
                     owner: str | None = None, repository: str | None = None,
                     pr_id: Any = None) -> dict[str, Any]:
    commit = item.get("commit") or {}
    author = commit.get("author") or {}
    return {
        "sha": item.get("sha"),
        **(activity_identity(repository_id, owner, repository, pr_id, number)
           if owner is not None and repository is not None else {"pull_request_number": number}),
        "author": user_identity(item.get("author")),
        "commit_author": author.get("name"),
        "committed_at": author.get("date"),
        "message": commit.get("message"),
        "url": item.get("html_url"),
        "source_url": item.get("url"),
    }


def normalize_event(
    item: dict[str, Any], number: int, merged_at: str | None = None,
    repository_id: Any = None, owner: str | None = None,
    repository: str | None = None, pr_id: Any = None,
) -> dict[str, Any]:
    event_kind = item.get("event")
    if event_kind == "closed" and merged_at and item.get("created_at"):
        try:
            if parse_utc(item["created_at"]) == parse_utc(merged_at):
                event_kind = "merged"
        except (TypeError, ValueError):
            # Preserve the source event unchanged; analytics can classify its timestamp.
            pass
    return {
        "id": item.get("id"),
        **(activity_identity(repository_id, owner, repository, pr_id, number)
           if owner is not None and repository is not None else {"pull_request_number": number}),
        "kind": event_kind,
        "actor": user_identity(item.get("actor")),
        "occurred_at": item.get("created_at"),
        "url": item.get("html_url"),
        "source_url": item.get("url"),
        "payload": {
            key: item.get(key)
            for key in ("commit_id", "label", "milestone", "rename", "assignee")
            if item.get(key) is not None
        },
    }


def normalize_check(item: dict[str, Any], number: int, repository_id: Any = None,
                    owner: str | None = None, repository: str | None = None,
                    pr_id: Any = None) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        **(activity_identity(repository_id, owner, repository, pr_id, number)
           if owner is not None and repository is not None else {"pull_request_number": number}),
        "name": item.get("name"),
        "status": item.get("status"),
        "conclusion": item.get("conclusion"),
        "started_at": item.get("started_at"),
        "completed_at": item.get("completed_at"),
        "url": item.get("html_url"),
        "source_url": item.get("url"),
    }


def fetch_repository(
    client: GitHubClient,
    owner: str,
    repository: str,
    from_utc: datetime,
    to_utc: datetime,
    observation_cutoff: datetime | None,
    author: str | None = TARGET_AUTHOR,
    include: list[str] | tuple[str, ...] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def report_progress(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    selected = select_resources(include)
    selected_authors = {login.casefold() for login in parse_author_logins(author)}
    collection_started = datetime.now(UTC)
    observation_cutoff = observation_cutoff or collection_started
    repo_path = f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    report_progress(f"Connecting to {owner}/{repository}…")
    repo_data, _ = client.get(repo_path)
    repository_id = repo_data.get("id")
    all_prs: list[dict[str, Any]] = []
    warnings: list[str] = []
    pull_request_listing_complete = True
    if "pull_requests" in selected:
        report_progress(f"Loading pull requests for {owner}/{repository}…")
        try:
            listed_prs = client.paginate(
                f"{repo_path}/pulls",
                {"state": "all", "sort": "created", "direction": "desc", "per_page": 100},
            )
        except GitHubPaginationError as error:
            listed_prs = error.items
            pull_request_listing_complete = False
            warnings.append(
                f"Pull request listing is partial after a pagination error "
                f"(HTTP {error.status}): {getattr(client, '_redact', str)(error)}"
            )
            report_progress(
                f"Pull request listing is partial for {owner}/{repository}; "
                "continuing with pages already retrieved."
            )
        for pr in listed_prs:
            created = parse_utc(pr["created_at"])
            if created < from_utc:
                break
            pr_author = (pr.get("user") or {}).get("login", "")
            if created <= to_utc and (
                author is None or pr_author.casefold() in selected_authors
            ):
                all_prs.append(pr)
    report_progress(
        f"Found {len(all_prs)} matching pull request(s) in {owner}/{repository}."
    )

    resources: dict[str, dict[str, Any]] = {}
    for name in SUPPORTED_RESOURCES:
        resources[name] = ({
            "status": "complete", "count": 0, "pagination_complete": True,
            "history_complete": True,
        } if name in selected else {
            "status": "not_requested", "count": None,
            "pagination_complete": False, "history_complete": False,
            "coverage_start": None, "coverage_end": None,
        })
    output_names = {
        "pull_requests": "pull_requests", "lifecycle_events": "events",
        "commits": "commits", "reviews": "reviews",
        "review_comments": "review_comments", "issue_comments": "issue_comments",
        "checks": "checks",
    }
    output = {
        output_names[name]: [] for name in selected if name in output_names
    }
    cutoff_text = iso_utc(observation_cutoff)
    pull_request_detail_status: dict[str, dict[str, Any]] = {}
    if "pull_request_details" in selected:
        for pr in all_prs:
            pr_id = pr.get("id")
            number = pr["number"]
            detail_status = {
                "status": "unavailable",
                "count": None,
                "pagination_complete": False,
                "history_complete": False,
                "coverage_start": pr.get("created_at"),
                "coverage_end": cutoff_text,
            }
            try:
                report_progress(
                    f"Loading PR #{number} size details in {owner}/{repository}…"
                )
                detail, _ = client.get(f"{repo_path}/pulls/{number}")
                if not isinstance(detail, dict):
                    raise GitHubAPIError(
                        0, f"Expected pull-request detail object for PR #{number}"
                    )
                for field in ("additions", "deletions", "changed_files"):
                    pr[field] = detail.get(field)
                counts_available = all(
                    isinstance(detail.get(field), int)
                    and not isinstance(detail.get(field), bool)
                    and detail[field] >= 0
                    for field in ("additions", "deletions", "changed_files")
                )
                detail_status.update({
                    "status": "complete" if counts_available else "partial",
                    "count": 1 if counts_available else None,
                    "pagination_complete": True,
                    "history_complete": counts_available,
                })
                if not counts_available:
                    warnings.append(
                        f"PR size details unavailable for PR #{number}: "
                        "the detail response omitted one or more non-negative counts."
                    )
            except GitHubAPIError as exc:
                warnings.append(
                    f"PR size details unavailable for PR #{number} (HTTP {exc.status}): "
                    f"{getattr(client, '_redact', str)(exc)}"
                )
            finally:
                report_progress(
                    f"Finished PR #{number} size details in {owner}/{repository}."
                )
            pull_request_detail_status[str(pr_id)] = detail_status

    detail_statuses = [
        item["status"] for item in pull_request_detail_status.values()
    ]
    resources["pull_request_details"] = {
        "status": (
            "complete" if not detail_statuses or set(detail_statuses) == {"complete"}
            else "partial" if "complete" in detail_statuses or "partial" in detail_statuses
            else "unavailable"
        ),
        "count": sum(
            item["count"] for item in pull_request_detail_status.values()
            if item["count"] is not None
        ),
        "pagination_complete": all(
            item["pagination_complete"] for item in pull_request_detail_status.values()
        ),
        "history_complete": all(
            item["history_complete"] for item in pull_request_detail_status.values()
        ),
        "coverage_start": min(
            (item["coverage_start"] for item in pull_request_detail_status.values()
             if item["coverage_start"]),
            default=None,
        ),
        "coverage_end": cutoff_text if pull_request_detail_status else None,
    } if "pull_request_details" in selected else {
        "status": "not_requested",
        "count": None,
        "pagination_complete": False,
        "history_complete": False,
        "coverage_start": None,
        "coverage_end": None,
    }
    if "review_threads" in selected:
        resources["review_threads"].update({
            "status": "unavailable", "count": 0, "pagination_complete": False,
            "history_complete": False,
        })
        warnings.append(
            "Review-thread resolution state is unavailable: this REST-only collector "
            "does not query GraphQL reviewThreads."
        )
    if "checks" in selected:
        warnings.append(
            "Checks are queried for each PR's current head commit only; historical check "
            "runs across earlier heads are not covered."
        )
    output["pull_requests"] = [
        normalize_pull_request(pr, repository_id, owner, repository) for pr in all_prs
    ]
    resources["pull_requests"].update({
        "status": "complete" if pull_request_listing_complete else "partial",
        "count": len(all_prs),
        "pagination_complete": pull_request_listing_complete,
        "history_complete": pull_request_listing_complete,
        "coverage_start": iso_utc(from_utc), "coverage_end": iso_utc(to_utc),
    })

    endpoints = {
        "lifecycle_events": ("timeline", "events"),
        "commits": ("commits", "commits"),
        "reviews": ("reviews", "reviews"),
        "review_comments": ("comments", "review_comments"),
        "issue_comments": ("comments", "issue_comments"),
    }
    pull_request_resources: dict[str, dict[str, dict[str, Any]]] = {}
    for pr in all_prs:
        pr_id = pr.get("id")
        number = pr["number"]
        per_pr: dict[str, dict[str, Any]] = {}
        for resource in selected:
            if resource == "pull_requests":
                per_pr[resource] = {
                    "status": "complete", "count": 1,
                    "pagination_complete": True, "history_complete": True,
                    "coverage_start": pr.get("created_at"),
                    "coverage_end": cutoff_text,
                }
                continue
            if resource == "pull_request_details":
                per_pr[resource] = pull_request_detail_status.get(
                    str(pr_id),
                    {
                        "status": "unavailable",
                        "count": None,
                        "pagination_complete": False,
                        "history_complete": False,
                        "coverage_start": pr.get("created_at"),
                        "coverage_end": cutoff_text,
                    },
                )
                continue
            supported = resource != "review_threads"
            per_pr[resource] = {
                "status": "complete" if supported else "unavailable",
                "count": 0 if supported else None,
                "pagination_complete": supported,
                "history_complete": supported,
                "coverage_start": pr.get("created_at"),
                "coverage_end": cutoff_text,
            }
        pull_request_resources[str(pr_id)] = per_pr
        pr_path = f"{repo_path}/pulls/{number}"
        issue_path = f"{repo_path}/issues/{number}"
        for resource, (suffix, output_key) in endpoints.items():
            if resource not in selected:
                continue
            path = f"{issue_path}/{suffix}" if resource in ("lifecycle_events", "issue_comments") else f"{pr_path}/{suffix}"
            try:
                report_progress(
                    f"Loading PR #{number} {resource.replace('_', ' ')} in {owner}/{repository}…"
                )
                pagination_complete = True
                try:
                    values = client.paginate(path, {"per_page": 100})
                except GitHubPaginationError as error:
                    values = error.items
                    pagination_complete = False
                    warnings.append(
                        f"{resource} is partial for PR #{number} after a pagination error "
                        f"(HTTP {error.status}): {getattr(client, '_redact', str)(error)}"
                    )
                if resource == "lifecycle_events":
                    normalized = [
                        normalize_event(value, number, pr.get("merged_at"),
                                        repository_id, owner, repository, pr_id)
                        for value in values
                    ]
                    filtered = [
                        value for value in normalized
                        if timestamp_at_or_before(value["occurred_at"], observation_cutoff)
                    ]
                elif resource == "commits":
                    normalized = [normalize_commit(value, number, repository_id, owner, repository, pr_id) for value in values]
                    filtered = [
                        value for value in normalized
                        if timestamp_at_or_before(value["committed_at"], observation_cutoff)
                    ]
                elif resource == "reviews":
                    normalized = [normalize_review(value, number, repository_id, owner, repository, pr_id) for value in values]
                    filtered = [
                        value for value in normalized
                        if timestamp_at_or_before(value["submitted_at"], observation_cutoff)
                    ]
                else:
                    normalized = [normalize_comment(value, number, repository_id, owner, repository, pr_id) for value in values]
                    filtered = [
                        value for value in normalized
                        if timestamp_at_or_before(value["created_at"], observation_cutoff)
                    ]
                output[output_key].extend(filtered)
                per_pr[resource].update({
                    "status": "complete" if pagination_complete else "partial",
                    "count": len(filtered),
                    "pagination_complete": pagination_complete,
                    "history_complete": pagination_complete,
                })
                report_progress(
                    f"Loaded {len(filtered)} {resource.replace('_', ' ')} record(s)"
                    f"{' from partial pages' if not pagination_complete else ''} "
                    f"for PR #{number} in {owner}/{repository}."
                )
            except GitHubAPIError as exc:
                per_pr[resource].update({
                    "status": "unavailable", "count": None,
                    "pagination_complete": False, "history_complete": False,
                })
                warnings.append(
                    f"{resource} unavailable for PR #{number} (HTTP {exc.status}): "
                    f"{getattr(client, '_redact', str)(exc)}"
                )
                report_progress(
                    f"{resource.replace('_', ' ').title()} unavailable for PR #{number} "
                    f"in {owner}/{repository}; continuing with remaining sources."
                )

        head_sha = ((pr.get("head") or {}).get("sha"))
        if "checks" in selected and head_sha:
            try:
                report_progress(
                    f"Loading current checks for PR #{number} in {owner}/{repository}…"
                )
                pagination_complete = True
                try:
                    check_runs = client.paginate(
                        f"{repo_path}/commits/{quote(head_sha, safe='')}/check-runs",
                        {"per_page": 100},
                        data_key="check_runs",
                    )
                except GitHubPaginationError as error:
                    check_runs = error.items
                    pagination_complete = False
                    warnings.append(
                        f"checks are partial for PR #{number} after a pagination error "
                        f"(HTTP {error.status}): {getattr(client, '_redact', str)(error)}"
                    )
                checks = [
                    normalize_check(item, number, repository_id, owner, repository, pr_id)
                    for item in check_runs
                ]
                output["checks"].extend(checks)
                per_pr["checks"]["count"] = len(checks)
                # The endpoint is scoped to the current PR head, not historical heads.
                per_pr["checks"].update({
                    "status": "partial",
                    "pagination_complete": pagination_complete,
                    "history_complete": False,
                })
                report_progress(
                    f"Loaded current checks for PR #{number} in {owner}/{repository}."
                )
            except GitHubAPIError as exc:
                per_pr["checks"].update({
                    "status": "unavailable", "count": None,
                    "pagination_complete": False, "history_complete": False,
                })
                warnings.append(
                    f"checks unavailable for PR #{number} (HTTP {exc.status}): "
                    f"{getattr(client, '_redact', str)(exc)}"
                )
                report_progress(
                    f"Checks unavailable for PR #{number} in {owner}/{repository}; "
                    "continuing with remaining sources."
                )
        elif "checks" in selected:
            per_pr["checks"].update({
                "status": "unavailable", "count": None,
                "pagination_complete": False, "history_complete": False,
            })
            warnings.append(f"checks unavailable for PR #{number}: no head commit SHA was returned.")

    for resource in selected:
        value = resources[resource]
        if resource == "pull_requests":
            continue
        per_pr_values = [item[resource] for item in pull_request_resources.values()]
        counts = [item["count"] for item in per_pr_values]
        value["count"] = sum(count for count in counts if count is not None)
        value["pagination_complete"] = all(item["pagination_complete"] for item in per_pr_values)
        value["history_complete"] = all(item["history_complete"] for item in per_pr_values)
        statuses = {item["status"] for item in per_pr_values}
        value["status"] = (
            "complete" if not statuses or statuses == {"complete"}
            else "partial" if "complete" in statuses or "partial" in statuses
            else "unavailable"
        )
        value["coverage_start"] = min(
            (item["coverage_start"] for item in per_pr_values if item["coverage_start"]),
            default=None,
        )
        value["coverage_end"] = cutoff_text
        key = output_names.get(resource)
        if key and value["status"] in {"complete", "partial"}:
            value["count"] = len(output[key])
        elif value["status"] == "unavailable":
            value["count"] = None
        if resource == "review_threads":
            value["status"] = "unavailable"
            value["count"] = None
            value["pagination_complete"] = False
            value["history_complete"] = False

    fetched_at = datetime.now(UTC)
    if observation_cutoff < collection_started.replace(microsecond=0):
        warnings.append(
            "Historical observation cutoff requested: GitHub REST returns current PR metadata; "
            "this fetch cannot reconstruct the PR's full metadata as of that earlier time."
        )
    statuses = {resources[name]["status"] for name in selected}
    overall_status = "partial" if "partial" in statuses or "unavailable" in statuses or warnings else "complete"
    return {
        "schema_version": "0.1",
        "repository": {
            "id": repository_id,
            "owner": owner,
            "name": repository,
            "host_url": urlparse(client.api_url).scheme + "://" + urlparse(client.api_url).netloc,
            "url": repo_data.get("html_url"),
        },
        "requested_window": {"from": iso_utc(from_utc), "to": iso_utc(to_utc)},
        "filters": {"author": author},
        "fetched_at": iso_utc(fetched_at),
        "observation_cutoff": cutoff_text,
        "status": overall_status,
        "resources": resources,
        "warnings": warnings,
        "pull_request_resources": pull_request_resources,
        **output,
    }


def parse_repository(value: str) -> tuple[str, str]:
    """Validate an explicit OWNER/REPOSITORY argument."""
    parts = value.split("/")
    if len(parts) != 2 or any(
        not part or part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
        for part in parts
    ):
        raise ValueError(f"invalid repository {value!r}; expected OWNER/REPOSITORY")
    return parts[0], parts[1]


def select_repositories(
    requested: list[str],
    *,
    all_repositories: bool = False,
) -> list[str]:
    """Resolve CLI selection and reject ambiguous duplicate repository requests."""
    if all_repositories and requested:
        raise ValueError("positional repositories cannot be combined with --all-repositories")
    repositories = list(AVAILABLE_REPOSITORIES) if all_repositories else (
        requested or [DEFAULT_REPOSITORY]
    )
    seen: set[str] = set()
    for value in repositories:
        parse_repository(value)
        key = value.casefold()
        if key in seen:
            raise ValueError(f"duplicate repository requested: {value}")
        seen.add(key)
    return repositories


def normalize_discovered_repository(item: dict[str, Any]) -> dict[str, Any]:
    """Return repository metadata useful to authenticated discovery consumers."""
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    visibility = item.get("visibility")
    if visibility is None:
        visibility = "private" if item.get("private") else "public"
    return {
        "id": item.get("id"),
        "owner": owner.get("login"),
        "name": item.get("name"),
        "full_name": item.get("full_name"),
        "url": item.get("html_url"),
        "default_branch": item.get("default_branch"),
        "language": item.get("language"),
        "visibility": visibility,
        "permissions": item.get("permissions"),
    }


def discover_accessible_repositories(client: GitHubClient) -> list[dict[str, Any]]:
    """Discover repositories visible to the authenticated user on the configured host."""
    repositories = client.paginate(
        "user/repos",
        {
            "affiliation": "owner,collaborator,org_member",
            "per_page": 100,
            "sort": "full_name",
            "direction": "asc",
        },
    )
    return [normalize_discovered_repository(item) for item in repositories]


def error_result(
    error: Exception,
    repository: str,
    api_url: str,
) -> dict[str, Any]:
    owner, name = parse_repository(repository)
    parsed_api_url = urlparse(api_url)
    return {
        "schema_version": "0.1",
        "status": "error",
        "repository": {
            "owner": owner,
            "name": name,
            "host_url": f"{parsed_api_url.scheme}://{parsed_api_url.netloc}",
            "url": None,
        },
        "error": {
            "status_code": getattr(error, "status", None),
            "message": str(error),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch read-only pull-request data from GitHub into a JSON contract envelope."
    )
    parser.add_argument(
        "repository",
        nargs="*",
        help=(
            "one or more GitHub repositories in OWNER/REPOSITORY form "
            f"(default: {DEFAULT_REPOSITORY})"
        ),
    )
    parser.add_argument(
        "--all-repositories",
        action="store_true",
        help="collect each configured repository (cannot be combined with positional repositories)",
    )
    parser.add_argument(
        "--list-repositories",
        action="store_true",
        help="list configured repositories offline; does not discover accessible repositories",
    )
    parser.add_argument(
        "--discover-repositories",
        action="store_true",
        help="list repositories accessible to GITHUB_TOKEN on the configured API host",
    )
    parser.add_argument(
        "--author",
        default=TARGET_AUTHOR,
        help=f"Limit PRs to this author login (default: {TARGET_AUTHOR})",
    )
    parser.add_argument(
        "--all-authors",
        action="store_true",
        help="Fetch PRs from all authors instead of the configured target author",
    )
    parser.add_argument("--from", dest="from_date", help="Inclusive PR creation date (ISO date or timestamp)")
    parser.add_argument("--to", dest="to_date", help="Inclusive PR creation date (ISO date or timestamp)")
    parser.add_argument("--as-of", help="UTC activity cutoff; defaults to the collection start")
    parser.add_argument(
        "--include",
        nargs="+",
        choices=SUPPORTED_RESOURCES,
        default=None,
        help="resources to collect (pull_requests is required; default: all supported resources)",
    )
    parser.add_argument("--output", help="Write JSON to this path instead of stdout")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_repositories:
        if args.repository or args.all_repositories or args.output or args.discover_repositories or args.include:
            parser.error("--list-repositories cannot be combined with repository selection, --discover-repositories, or --output")
        print("\n".join(AVAILABLE_REPOSITORIES))
        return 0
    if args.discover_repositories:
        if args.repository or args.all_repositories or args.from_date or args.to_date or args.as_of or args.include:
            parser.error("--discover-repositories cannot be combined with repository selections or date filters")
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            parser.error("GITHUB_TOKEN is not set; accessible repository discovery requires authentication.")
        try:
            client = GitHubClient(
                DEFAULT_API_URL,
                token,
                timeout=args.timeout,
                max_retries=args.max_retries,
            )
            result = discover_accessible_repositories(client)
        except (ValueError, GitHubAPIError) as exc:
            parser.error(str(exc))
        rendered = json.dumps(result, indent=2, ensure_ascii=False)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        else:
            print(rendered)
        return 0
    try:
        include = select_resources(args.include)
        repositories = select_repositories(
            args.repository,
            all_repositories=args.all_repositories,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not args.from_date or not args.to_date:
        parser.error("--from and --to are required unless --list-repositories is used")

    try:
        from_utc = parse_utc(args.from_date)
        to_utc = parse_utc(args.to_date, date_end=True)
        if from_utc > to_utc:
            parser.error("--from must be earlier than or equal to --to")
        observation_cutoff = parse_utc(args.as_of) if args.as_of else None
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is not set; provide a fresh read-only token via the environment.")
        client = GitHubClient(
            DEFAULT_API_URL,
            token,
            timeout=args.timeout,
            max_retries=args.max_retries,
        )
    except (ValueError, GitHubAPIError) as exc:
        results = [error_result(exc, repository, DEFAULT_API_URL) for repository in repositories]
        rendered = json.dumps(results[0] if len(results) == 1 else results, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        else:
            print(rendered)
        return 1

    results: list[dict[str, Any]] = []
    for repository in repositories:
        owner, name = parse_repository(repository)
        try:
            results.append(
                fetch_repository(
                    client,
                    owner,
                    name,
                    from_utc,
                    to_utc,
                    observation_cutoff,
                    author=None if args.all_authors else args.author,
                    include=include,
                )
            )
        except (ValueError, GitHubAPIError) as exc:
            results.append(error_result(exc, repository, DEFAULT_API_URL))

    rendered = json.dumps(
        results[0] if len(results) == 1 else results,
        indent=2,
        ensure_ascii=False,
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    else:
        print(rendered)
    if any(result["status"] == "error" for result in results):
        return 1
    return 0 if all(result["status"] == "complete" for result in results) else 2


if __name__ == "__main__":
    sys.exit(main())
