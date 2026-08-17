#!/usr/bin/env python3
"""Render README.md from README.template.md plus GitHub's *public* record.

Two invariants hold this script honest, and both are tested:

1. **It never authenticates.** No credential is read from the environment and no
   ``Authorization`` header is ever built, so its output cannot grow when it is
   handed a more privileged token. A tripwire aborts the run if a response's
   rate-limit ceiling is higher than an unauthenticated one, which is the only
   way credentials could arrive without this file asking for them.
2. **It emits no free text from the record.** The only strings that reach the
   page are repository names taken from public repository listings, integers,
   dates and URLs derived from those names. Pull request titles, commit
   messages and branch names are read but never rendered, so a title mentioning
   a private repository cannot publish that name.

Any fetch failure aborts before a byte is written, which leaves the committed
README.md at its last good state with its own timestamp on it.

Usage::

    python3 tools/render_readme.py            # render to stdout
    python3 tools/render_readme.py --write    # rewrite README.md in place
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
ACCOUNT = "Jartans-Familiar"
# The employing organisation, not a list of repositories: which repositories
# appear on the page is discovered from the public record on every run.
ORG = "Jartan-LLC"

REPO_SLUG = f"{ACCOUNT}/{ACCOUNT}"
WORKFLOW_FILE = "refresh-activity.yml"

BEGIN_MARKER = "<!-- BEGIN GENERATED -->"
END_MARKER = "<!-- END GENERATED -->"

FEED_LENGTH = 8
MAX_SEARCH_PAGES = 10  # GitHub's search API stops at 1000 results.
USER_AGENT = f"{REPO_SLUG} readme renderer"

# Unauthenticated ceilings, per GitHub's documented rate limits. A response
# claiming more than this means the request carried credentials from somewhere
# this script did not put them, so it stops rather than risk publishing private
# work.
UNAUTHENTICATED_CEILING = {"core": 60, "search": 10}


class FetchError(RuntimeError):
    """A public API read failed and the page must not be rewritten."""


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def _request(url: str) -> urllib.request.Request:
    """Build a request that carries no credentials, by construction."""
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        },
    )


def _check_unauthenticated(headers) -> None:
    resource = headers.get("x-ratelimit-resource")
    limit = headers.get("x-ratelimit-limit")
    if not resource or not limit:
        return
    ceiling = UNAUTHENTICATED_CEILING.get(resource)
    if ceiling is None:
        return
    if int(limit) > ceiling:
        raise FetchError(
            f"refusing to continue: the {resource} rate limit is {limit}, above the "
            f"unauthenticated ceiling of {ceiling}, so these requests are "
            "authenticated and the output could include private work"
        )


def fetch(url: str, *, attempts: int = 4, sleep=time.sleep) -> tuple[object, dict]:
    """GET a public API URL, retrying throttles and transient server errors."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(_request(url), timeout=30) as response:
                headers = {k.lower(): v for k, v in response.headers.items()}
                _check_unauthenticated(headers)
                return json.load(response), headers
        except urllib.error.HTTPError as error:
            last = error
            retryable = error.code in (403, 429) or 500 <= error.code < 600
            if not retryable or attempt == attempts - 1:
                break
            sleep(_backoff(error, attempt))
        except urllib.error.URLError as error:
            last = error
            if attempt == attempts - 1:
                break
            sleep(2**attempt)
    raise FetchError(f"GET {url} failed: {last}")


def _backoff(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("retry-after") if error.headers else None
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 60.0)
    return float(2**attempt)


def _paginate_search(query: str, fetcher=fetch) -> list[dict]:
    """Collect every item of a search query, or fail rather than undercount."""
    items: list[dict] = []
    for page in range(1, MAX_SEARCH_PAGES + 1):
        url = f"{API}/search/issues?q={query}&per_page=100&page={page}"
        payload, _ = fetcher(url)
        total = payload.get("total_count", 0)
        if total > 100 * MAX_SEARCH_PAGES:
            raise FetchError(
                f"{total} results exceed the {100 * MAX_SEARCH_PAGES}-result search cap; "
                "the counting method needs revising before these totals can be trusted"
            )
        items.extend(payload.get("items", []))
        if len(payload.get("items", [])) < 100:
            break
    return items


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------


@dataclass
class RepoActivity:
    full_name: str
    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    last_commit: str | None = None

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"

    @property
    def touched(self) -> bool:
        return self.commits > 0 or self.prs_opened > 0


@dataclass
class Event:
    when: str  # ISO-8601 UTC, as GitHub returns it
    text: str  # already rendered, contains no free text from the record


@dataclass
class Snapshot:
    generated_at: str
    repos: list[RepoActivity] = field(default_factory=list)
    prs_opened: int = 0
    prs_merged: int = 0
    prs_open_now: int = 0
    median_merge_minutes: float | None = None
    events: list[Event] = field(default_factory=list)


def _repo_from_api_url(url: str) -> str:
    return "/".join(url.rstrip("/").split("/")[-2:])


def discover_repositories(fetcher=fetch) -> list[str]:
    """Public repositories the account could plausibly have touched.

    Unauthenticated listings return public repositories only, so a private one
    cannot enter this list however the script is run.
    """
    names: list[str] = []
    for url in (
        f"{API}/orgs/{ORG}/repos?type=public&per_page=100",
        f"{API}/users/{ACCOUNT}/repos?type=owner&per_page=100",
    ):
        payload, _ = fetcher(url)
        for repo in payload:
            if repo["full_name"] not in names:
                names.append(repo["full_name"])
    return names


def count_commits(full_name: str, fetcher=fetch) -> tuple[int, str | None]:
    """Commits by the account on a repository's default branch, and the latest.

    A repository the account has never touched answers 409 (empty) or 404; both
    mean zero rather than a failure.
    """
    url = f"{API}/repos/{full_name}/commits?author={ACCOUNT}&per_page=1"
    try:
        payload, headers = fetcher(url)
    except FetchError as error:
        if "HTTP Error 409" in str(error) or "HTTP Error 404" in str(error):
            return 0, None
        raise
    if not payload:
        return 0, None
    last_page = re.search(r'[?&]page=(\d+)>; rel="last"', headers.get("link", ""))
    count = int(last_page.group(1)) if last_page else len(payload)
    date = payload[0]["commit"]["committer"]["date"][:10]
    return count, date


def collect(now: datetime, fetcher=fetch) -> Snapshot:
    snapshot = Snapshot(generated_at=now.strftime("%Y-%m-%d %H:%M UTC"))
    activity: dict[str, RepoActivity] = {
        name: RepoActivity(full_name=name) for name in discover_repositories(fetcher)
    }

    merge_minutes: list[float] = []
    for item in _paginate_search(f"author:{ACCOUNT}+type:pr", fetcher):
        name = _repo_from_api_url(item["repository_url"])
        repo = activity.setdefault(name, RepoActivity(full_name=name))
        repo.prs_opened += 1
        snapshot.prs_opened += 1
        merged_at = (item.get("pull_request") or {}).get("merged_at")
        if merged_at:
            repo.prs_merged += 1
            snapshot.prs_merged += 1
            merge_minutes.append(
                (_parse(merged_at) - _parse(item["created_at"])).total_seconds() / 60
            )
        elif item.get("state") == "open":
            snapshot.prs_open_now += 1

    for repo in activity.values():
        repo.commits, repo.last_commit = count_commits(repo.full_name, fetcher)

    if merge_minutes:
        snapshot.median_merge_minutes = statistics.median(merge_minutes)

    snapshot.repos = sorted(
        (repo for repo in activity.values() if repo.touched),
        key=lambda r: (r.last_commit or "", r.prs_merged, r.commits, r.full_name),
        reverse=True,
    )
    snapshot.events = collect_events(fetcher)
    return snapshot


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def collect_events(fetcher=fetch) -> list[Event]:
    """The tail of the account's public event feed, rendered without free text."""
    payload, _ = fetcher(f"{API}/users/{ACCOUNT}/events/public?per_page=100")
    events: list[Event] = []
    for raw in payload:
        text = _describe_event(raw)
        if text:
            events.append(Event(when=raw["created_at"], text=text))
        if len(events) == FEED_LENGTH:
            break
    return events


def _describe_event(raw: dict) -> str | None:
    name = raw["repo"]["name"]
    repo_link = f"[{name}](https://github.com/{name})"
    payload = raw.get("payload") or {}
    kind = raw["type"]

    def pull(number: int) -> str:
        return f"[pull request #{number}](https://github.com/{name}/pull/{number})"

    if kind == "PushEvent":
        size = int(payload.get("size") or 0)
        if size < 1:
            return None
        plural = "commit" if size == 1 else "commits"
        return f"pushed {size} {plural} to {repo_link}"
    if kind == "PullRequestEvent":
        number = payload.get("number") or (payload.get("pull_request") or {}).get("number")
        if not number:
            return None
        action = payload.get("action")
        if action == "opened":
            return f"opened {pull(number)} in {repo_link}"
        if action == "reopened":
            return f"reopened {pull(number)} in {repo_link}"
        # The public event feed reports a merge as its own action and trims the
        # pull request object, so the webhook shape (closed plus a merged flag)
        # is handled second rather than first.
        if action == "merged" or (
            action == "closed" and (payload.get("pull_request") or {}).get("merged")
        ):
            return f"merged {pull(number)} in {repo_link}"
        if action == "closed":
            return f"closed {pull(number)} in {repo_link} without merging"
        return None
    if kind == "PullRequestReviewEvent":
        number = (payload.get("pull_request") or {}).get("number")
        return f"reviewed {pull(number)} in {repo_link}" if number else None
    if kind == "IssuesEvent" and payload.get("action") == "opened":
        number = (payload.get("issue") or {}).get("number")
        if not number:
            return None
        return (
            f"opened [issue #{number}](https://github.com/{name}/issues/{number}) "
            f"in {repo_link}"
        )
    return None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def humanise_minutes(minutes: float) -> str:
    if minutes < 90:
        count = max(1, round(minutes))
        return f"{count} minute" if count == 1 else f"{count} minutes"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def _badge() -> str:
    workflow = f"https://github.com/{REPO_SLUG}/actions/workflows/{WORKFLOW_FILE}"
    return f"[![Refreshing this page]({workflow}/badge.svg)]({workflow})"


def render_section(snapshot: Snapshot) -> str:
    lines = [
        "### What it has been doing",
        "",
        _badge(),
        "",
        f"Counted from GitHub's public API at **{snapshot.generated_at}** and refreshed "
        "daily. If that timestamp is more than a day or two old, the refresh above is "
        "broken and these numbers are stale rather than current. Public repositories "
        "only: some of what this account works on is private, and none of it is here.",
        "",
    ]

    if snapshot.prs_opened:
        totals = (
            f"**{snapshot.prs_opened} public pull requests opened, "
            f"{snapshot.prs_merged} merged, {snapshot.prs_open_now} open now.**"
        )
        if snapshot.median_merge_minutes is not None:
            totals += (
                " Median time from opening to merge: "
                f"{humanise_minutes(snapshot.median_merge_minutes)}."
            )
        lines += [totals, ""]

    if snapshot.repos:
        lines += [
            "| Repository | Commits | Pull requests | Merged | Last commit |",
            "| --- | --- | ---: | ---: | --- |",
        ]
        for repo in snapshot.repos:
            lines.append(
                f"| [{repo.full_name}]({repo.url}) | {repo.commits} | "
                f"{repo.prs_opened} | {repo.prs_merged} | {repo.last_commit or '—'} |"
            )
        lines += [
            "",
            "Commits counts this account's commits on each repository's default "
            "branch, so a squashed pull request lands as one.",
            "",
        ]

    if snapshot.events:
        lines += ["**Latest public activity**", ""]
        for event in snapshot.events:
            stamp = event.when.replace("T", " ")[:16] + " UTC"
            lines.append(f"- {stamp} — {event.text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render(template: str, snapshot: Snapshot) -> str:
    if BEGIN_MARKER not in template or END_MARKER not in template:
        raise RuntimeError(f"template is missing {BEGIN_MARKER} / {END_MARKER}")
    head, rest = template.split(BEGIN_MARKER, 1)
    _, tail = rest.split(END_MARKER, 1)
    section = render_section(snapshot)
    return f"{head}{BEGIN_MARKER}\n\n{section}\n{END_MARKER}{tail}"


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite README.md instead of printing to stdout",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root holding README.template.md",
    )
    args = parser.parse_args(argv)

    template = (args.root / "README.template.md").read_text(encoding="utf-8")
    try:
        snapshot = collect(datetime.now(timezone.utc))
    except FetchError as error:
        print(f"error: {error}", file=sys.stderr)
        print(
            "README.md was left untouched, so the published page keeps its last "
            "numbers with their own timestamp on them.",
            file=sys.stderr,
        )
        return 1

    page = render(template, snapshot)
    if args.write:
        (args.root / "README.md").write_text(page, encoding="utf-8")
    else:
        sys.stdout.write(page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
