"""Are the two forks still close to what they were forked from?

This repository carries two forks: Fluidd, pinned to the commit the printer's
own Fluidd reports, and OrcaSlicer, pinned to the upstream `main` commit the
shipped build was compiled from; see the fork's build notes. Both drift the
moment upstream moves, and the only thing worse than an old fork is an old fork
nobody knew was old.

`forks.json` at the project root is the registry: name, upstream repository,
pinned tag, pinned commit and branch. This module reads it, asks
GitHub what the newest release is and how many commits the pin is behind the
default branch, and caches the answer for six hours. `GET /updates` on the
bridge is a thin wrapper around `UpdateChecker.report()`.

Three rules:

1. **Nothing here touches a git clone.** A web request must not run git against
   a working tree the user may be building in. The clone-side work (fetch, dry
   rebase, which files conflict) is the fork's upstream check, run by hand.
2. **Unauthenticated GitHub is enough.** Three GETs per fork, twice a day, is
   far inside the 60-per-hour anonymous limit. A rate-limit answer is reported
   as such rather than raised, so the page says why it has no numbers.
3. **A failure is data, not an exception.** Every entry carries its own `error`
   and the report as a whole still answers 200, because the card that draws it
   has nothing useful to do with a stack trace.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional

import requests

from .logs import get_logger

log = get_logger()

GITHUB_API = "https://api.github.com"
# Six hours. The forks do not move faster than that, and neither does upstream
# in any way that matters to somebody deciding whether to rebase today.
CACHE_SECONDS = 6 * 3600.0
HTTP_TIMEOUT = 12.0

# GitHub asks for a User-Agent and answers 403 without one.
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "cfsbridge-upstream-check",
}


def registry_path(start: Optional[str] = None) -> str:
    """Where `forks.json` lives: next to the `cfsbridge` package."""
    here = start or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "forks.json")


def load_registry(path: Optional[str] = None) -> list[dict]:
    """The fork list, or [] when the file is missing or unreadable.

    Missing is not an error: a checkout without `forks.json` should still serve
    every other route.
    """
    target = path or registry_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("no fork registry at %s: %s", target, exc)
        return []
    forks = payload.get("forks") if isinstance(payload, dict) else payload
    return [dict(entry) for entry in (forks or []) if isinstance(entry, dict)]


def github_get(url: str, timeout: float = HTTP_TIMEOUT) -> tuple[int, object]:
    """(status, parsed body). Never raises; a transport failure is status 0."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        return 0, {"message": str(exc)}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"message": response.text[:200]}


def _rate_limited(status: int, body: object) -> bool:
    message = ""
    if isinstance(body, dict):
        message = str(body.get("message") or "")
    return status == 403 and "rate limit" in message.lower()


def _short(commit: str) -> str:
    return (commit or "")[:10]


def check_fork(entry: dict, fetch: Callable[..., tuple[int, object]] = github_get) -> dict:
    """One fork's answer. `fetch(url)` is the seam the tests replace."""
    repo = str(entry.get("repo") or "").strip("/")
    pinned_commit = str(entry.get("pinned_commit") or "")
    pinned_tag = str(entry.get("pinned_tag") or "")
    out = {
        "name": entry.get("name") or repo,
        "title": entry.get("title") or entry.get("name") or repo,
        "repo": repo,
        "repo_url": "https://github.com/%s" % repo if repo else "",
        "branch": entry.get("branch") or "",
        "clone": entry.get("clone") or "",
        "doc": entry.get("doc") or "",
        "why": entry.get("why") or "",
        "pinned_tag": pinned_tag,
        "pinned_commit": pinned_commit,
        "pinned_commit_short": _short(pinned_commit),
        "pinned": pinned_tag or _short(pinned_commit),
        "default_branch": "",
        "latest_tag": "",
        "latest_name": "",
        "latest_published_at": "",
        "release_url": "",
        "compare_url": "",
        "behind": None,
        "ahead": None,
        "update_available": False,
        "error": "",
    }
    if not repo:
        out["error"] = "This fork entry has no upstream repository."
        return out

    status, body = fetch("%s/repos/%s" % (GITHUB_API, repo))
    if _rate_limited(status, body):
        out["error"] = ("GitHub's anonymous rate limit is spent. The numbers "
                        "come back within the hour.")
        return out
    if status == 200 and isinstance(body, dict):
        out["default_branch"] = str(body.get("default_branch") or "")
    elif status:
        out["error"] = "GitHub answered HTTP %d for the repository." % status
    else:
        out["error"] = "GitHub could not be reached: %s" % (
            body.get("message") if isinstance(body, dict) else body)
        return out

    # The newest release, falling back to the newest tag for a repository that
    # tags but does not publish releases.
    status, body = fetch("%s/repos/%s/releases/latest" % (GITHUB_API, repo))
    if status == 200 and isinstance(body, dict):
        out["latest_tag"] = str(body.get("tag_name") or "")
        out["latest_name"] = str(body.get("name") or "")
        out["latest_published_at"] = str(body.get("published_at") or "")
        out["release_url"] = str(body.get("html_url") or "")
    elif status == 404:
        status, body = fetch("%s/repos/%s/tags" % (GITHUB_API, repo))
        if status == 200 and isinstance(body, list) and body:
            first = body[0] if isinstance(body[0], dict) else {}
            out["latest_tag"] = str(first.get("name") or "")
            out["release_url"] = ("https://github.com/%s/releases/tag/%s"
                                  % (repo, out["latest_tag"])) if out["latest_tag"] else ""
    elif _rate_limited(status, body):
        out["error"] = ("GitHub's anonymous rate limit is spent. The numbers "
                        "come back within the hour.")
        return out

    # How far the pin is behind the default branch.
    head = out["default_branch"]
    if pinned_commit and head:
        url = "%s/repos/%s/compare/%s...%s" % (GITHUB_API, repo, pinned_commit, head)
        status, body = fetch(url)
        if status == 200 and isinstance(body, dict):
            out["behind"] = int(body.get("ahead_by") or 0)
            out["ahead"] = int(body.get("behind_by") or 0)
            out["compare_url"] = str(body.get("html_url") or "")
        elif _rate_limited(status, body):
            out["error"] = ("GitHub's anonymous rate limit is spent. The "
                            "numbers come back within the hour.")
        elif status:
            out["error"] = out["error"] or (
                "GitHub answered HTTP %d comparing %s with %s."
                % (status, _short(pinned_commit), head))

    # A fork that tracks a branch (OrcaSlicer follows upstream `main`, which is
    # ahead of the last release) is only "behind" when the branch has moved on;
    # the release tag is informational for it. A fork pinned to a release
    # (Fluidd) is behind when either the branch or the release moved.
    tracks = str(entry.get("tracks") or ("release" if pinned_tag else "branch"))
    out["tracks"] = tracks
    behind_branch = (out["behind"] or 0) > 0
    newer_release = bool(out["latest_tag"] and pinned_tag
                         and out["latest_tag"] != pinned_tag)
    # For a release-pinned fork the branch count is informational: develop is
    # always a commit or two past the last tag, and that is not an update.
    out["update_available"] = newer_release if tracks == "release" else behind_branch
    return out


class UpdateChecker:
    """`GET /updates`, cached. One instance lives on the serve facade."""

    def __init__(self, path: Optional[str] = None,
                 fetch: Callable[..., tuple[int, object]] = github_get,
                 ttl: float = CACHE_SECONDS) -> None:
        self.path = path
        self.fetch = fetch
        self.ttl = ttl
        self.lock = threading.RLock()
        self._cached: Optional[dict] = None
        self._at: float = 0.0

    def report(self, refresh: bool = False) -> dict:
        now = time.time()
        with self.lock:
            cached, at = self._cached, self._at
        if cached is not None and not refresh and (now - at) < self.ttl:
            answer = dict(cached)
            answer["age_s"] = now - at
            answer["cached"] = True
            return answer

        forks = [check_fork(entry, self.fetch) for entry in load_registry(self.path)]
        behind = [f for f in forks if (f.get("behind") or 0) > 0]
        answer = {
            "ok": True,
            "checked_at": now,
            "age_s": 0.0,
            "cached": False,
            "ttl_s": self.ttl,
            "forks": forks,
            "behind_count": len(behind),
            "update_available": any(f.get("update_available") for f in forks),
            "note": ("Read only: this route asks GitHub and never runs git "
                     "against a clone. Run the fork's upstream check for the "
                     "dry rebase and the list of files that would conflict."),
        }
        with self.lock:
            self._cached, self._at = answer, now
        return dict(answer)
