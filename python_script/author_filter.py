"""Helpers for validating and matching one or more GitHub author logins."""

from __future__ import annotations

import re


_LOGIN_PATTERN = re.compile(r"[A-Za-z0-9-]+\Z")


def parse_author_logins(value: str | None) -> tuple[str, ...]:
    """Split comma-separated logins, trim whitespace, and deduplicate case-insensitively."""
    if value is None:
        return ()
    logins: list[str] = []
    seen: set[str] = set()
    for raw_login in value.split(","):
        login = raw_login.strip()
        if not login:
            continue
        if len(login) > 39 or not _LOGIN_PATTERN.fullmatch(login):
            raise ValueError("Enter valid GitHub usernames separated by commas.")
        key = login.casefold()
        if key not in seen:
            seen.add(key)
            logins.append(login)
    return tuple(logins)


def normalize_author_filter(value: str | None) -> str | None:
    """Return the canonical comma-separated selection, or None for all authors."""
    logins = parse_author_logins(value)
    return ", ".join(logins) if logins else None
