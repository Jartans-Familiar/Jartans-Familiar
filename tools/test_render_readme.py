#!/usr/bin/env python3
"""Tests for the README renderer. Standard library only::

    python3 -m unittest discover -s tools -v

The four that matter are the ones tied to how this page can go wrong:
credentials reaching the fetch layer, free text from the record reaching the
page, a failure rewriting the page with nothing in it, and a stale number
rendered as if it were current.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import render_readme as rr

NOW = datetime(2026, 8, 17, 8, 30, tzinfo=timezone.utc)

# A title, a commit message and a branch name that must never reach the page.
# They stand in for the real risk: a public pull request whose title mentions a
# repository that is not public.
SENTINEL = "zzz-private-project-name"


def _pr(repo: str, number: int, created: str, merged: str | None, state: str = "closed") -> dict:
    return {
        "repository_url": f"https://api.github.com/repos/{repo}",
        "number": number,
        "title": f"fix the thing in {SENTINEL}",
        "state": state,
        "created_at": created,
        "pull_request": {"merged_at": merged},
    }


class FakeAPI:
    """A stand-in for GitHub's public API, driven by fixtures."""

    def __init__(self, *, org_repos, own_repos, prs, commits, events, search_total=None):
        self.org_repos = org_repos
        self.own_repos = own_repos
        self.prs = prs
        self.commits = commits
        self.events = events
        self.search_total = search_total if search_total is not None else len(prs)
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        if "/orgs/" in url:
            return [{"full_name": name} for name in self.org_repos], {}
        if url.endswith("type=owner&per_page=100"):
            return [{"full_name": name} for name in self.own_repos], {}
        if "/search/issues" in url:
            return {"total_count": self.search_total, "items": self.prs}, {}
        if "/commits?" in url:
            repo = url.split("/repos/", 1)[1].split("/commits", 1)[0]
            count, date = self.commits.get(repo, (0, None))
            if count == 0:
                return [], {}
            commit = [{"commit": {"committer": {"date": f"{date}T09:00:00Z"},
                                  "message": SENTINEL}}]
            link = f'<...&page={count}>; rel="last"' if count > 1 else ""
            return commit, {"link": link}
        if "/events/public" in url:
            return self.events, {}
        raise AssertionError(f"unexpected URL: {url}")


def sample_api(**overrides) -> FakeAPI:
    defaults = dict(
        org_repos=["Acme/alpha", "Acme/beta", "Acme/a-fork"],
        own_repos=["Jartans-Familiar/Jartans-Familiar"],
        prs=[
            _pr("Acme/alpha", 7, "2026-08-16T10:00:00Z", "2026-08-16T10:10:00Z"),
            _pr("Acme/alpha", 8, "2026-08-16T11:00:00Z", "2026-08-16T11:30:00Z"),
            _pr("Acme/beta", 3, "2026-08-17T07:00:00Z", None, state="open"),
        ],
        commits={"Acme/alpha": (5, "2026-08-16"), "Acme/beta": (2, "2026-08-17")},
        events=[
            {
                "type": "PullRequestEvent",
                "created_at": "2026-08-17T07:28:00Z",
                "repo": {"name": "Acme/beta"},
                "payload": {"action": "opened", "number": 3,
                            "pull_request": {"title": SENTINEL}},
            },
            {
                "type": "PushEvent",
                "created_at": "2026-08-17T07:20:00Z",
                "repo": {"name": "Acme/alpha"},
                "payload": {"size": 3, "ref": f"refs/heads/{SENTINEL}"},
            },
            {
                "type": "DeleteEvent",
                "created_at": "2026-08-17T07:19:00Z",
                "repo": {"name": "Acme/alpha"},
                "payload": {"ref_type": "branch", "ref": SENTINEL},
            },
            # As the public feed reports a merge: its own action, and a pull
            # request object trimmed of the title.
            {
                "type": "PullRequestEvent",
                "created_at": "2026-08-16T11:30:00Z",
                "repo": {"name": "Acme/alpha"},
                "payload": {"action": "merged", "number": 8,
                            "pull_request": {"number": 8}},
            },
            # As a webhook reports the same thing.
            {
                "type": "PullRequestEvent",
                "created_at": "2026-08-16T10:10:00Z",
                "repo": {"name": "Acme/alpha"},
                "payload": {"action": "closed", "number": 7,
                            "pull_request": {"number": 7, "merged": True,
                                             "title": SENTINEL}},
            },
            {
                "type": "PullRequestEvent",
                "created_at": "2026-08-16T09:00:00Z",
                "repo": {"name": "Acme/alpha"},
                "payload": {"action": "closed", "number": 6,
                            "pull_request": {"number": 6, "merged": False}},
            },
        ],
    )
    defaults.update(overrides)
    return FakeAPI(**defaults)


TEMPLATE = (
    "## Heading\n\nProse that names nothing.\n\n"
    f"{rr.BEGIN_MARKER}\n{rr.END_MARKER}\n\n### Tail\n\nMore prose.\n"
)


class CredentialsNeverLeave(unittest.TestCase):
    def test_no_authorization_header_even_with_tokens_in_the_environment(self):
        with mock.patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "ghp_secret", "GH_TOKEN": "ghp_secret", "GH_ENTERPRISE_TOKEN": "x"},
        ):
            request = rr._request(f"{rr.API}/users/{rr.ACCOUNT}/events/public")
        header_blob = " ".join(f"{k}:{v}" for k, v in request.header_items()).lower()
        self.assertNotIn("authorization", header_blob)
        self.assertNotIn("ghp_secret", header_blob)
        self.assertIsNone(request.data)

    def test_source_reads_no_credential_from_the_environment(self):
        source = Path(rr.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "os.environ",
            "getenv",
            "netrc",
            "add_header",
            "add_password",
            '"Authorization"',
            "'Authorization'",
        ):
            self.assertNotIn(forbidden, source, f"{forbidden} must not appear")

    def test_authenticated_rate_limit_ceiling_aborts_the_run(self):
        with self.assertRaises(rr.FetchError) as caught:
            rr._check_unauthenticated({"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000"})
        self.assertIn("private work", str(caught.exception))

    def test_unauthenticated_rate_limit_headers_pass(self):
        rr._check_unauthenticated({"x-ratelimit-resource": "core", "x-ratelimit-limit": "60"})
        rr._check_unauthenticated({"x-ratelimit-resource": "search", "x-ratelimit-limit": "10"})
        rr._check_unauthenticated({})


class NoFreeTextReachesThePage(unittest.TestCase):
    def test_titles_commit_messages_and_branch_names_are_not_rendered(self):
        page = rr.render(TEMPLATE, rr.collect(NOW, sample_api()))
        self.assertNotIn(SENTINEL, page)

    def test_only_repository_names_from_public_listings_appear(self):
        import re

        api = sample_api()
        page = rr.render(TEMPLATE, rr.collect(NOW, api))
        known = set(api.org_repos) | set(api.own_repos)
        found = set(re.findall(r"(?:Acme|Jartans-Familiar)/[A-Za-z0-9._-]+", page))
        self.assertTrue(found, "expected the page to name at least one repository")
        self.assertEqual(found - known, set())


class OnlyProvenPublicRepositoriesAreNamed(unittest.TestCase):
    """The event feed is not trusted as a source of repository names.

    Every other name on the page comes from an unauthenticated listing or an
    unauthenticated search, neither of which can return a private repository.
    GitHub documents the event endpoint as public-only, which is its behaviour
    rather than something a run can check, so it is checked against the rest.
    """

    UNLISTED = {
        "type": "PushEvent",
        "created_at": "2026-08-17T07:30:00Z",
        "repo": {"name": f"Acme/{SENTINEL}"},
        "payload": {"size": 4},
    }

    def test_an_event_outside_the_proven_set_is_dropped(self):
        api = sample_api(events=[self.UNLISTED, *sample_api().events])
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            page = rr.render(TEMPLATE, rr.collect(NOW, api))
        self.assertNotIn(SENTINEL, page)
        self.assertIn("dropped 1 event", stderr.getvalue())

    def test_the_drop_is_counted_and_the_repository_is_not_named(self):
        # The workflow log of a public repository is public, so the note must
        # not carry the name it stopped from reaching the page.
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rr.collect_events({"Acme/alpha"}, sample_api(events=[self.UNLISTED]))
        self.assertIn("did not prove public", stderr.getvalue())
        self.assertNotIn(SENTINEL, stderr.getvalue())

    def test_a_clean_feed_says_nothing(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rr.collect_events({"Acme/alpha", "Acme/beta"}, sample_api())
        self.assertEqual(stderr.getvalue(), "")

    def test_a_repository_found_only_by_search_still_counts_as_proven(self):
        # A public pull request opened against a repository outside the
        # organisation is proven public by the search that returned it, so its
        # events belong on the page.
        api = sample_api(
            prs=[_pr("Upstream/tool", 4, "2026-08-17T06:00:00Z", "2026-08-17T06:20:00Z")],
            commits={"Upstream/tool": (1, "2026-08-17")},
            events=[
                {
                    "type": "PullRequestEvent",
                    "created_at": "2026-08-17T06:20:00Z",
                    "repo": {"name": "Upstream/tool"},
                    "payload": {"action": "merged", "number": 4,
                                "pull_request": {"number": 4}},
                }
            ],
        )
        page = rr.render(TEMPLATE, rr.collect(NOW, api))
        self.assertIn("Upstream/tool", page)
        self.assertIn("merged [pull request #4]", page)


class ThePageChangesWithTheRecord(unittest.TestCase):
    def test_a_changed_record_changes_the_page(self):
        before = rr.render(TEMPLATE, rr.collect(NOW, sample_api()))
        after = rr.render(
            TEMPLATE,
            rr.collect(NOW, sample_api(commits={"Acme/alpha": (9, "2026-08-17"),
                                                "Acme/beta": (2, "2026-08-17")})),
        )
        self.assertNotEqual(before, after)
        self.assertIn("| 9 |", after)
        self.assertNotIn("| 9 |", before)

    def test_the_same_record_renders_identically(self):
        first = rr.render(TEMPLATE, rr.collect(NOW, sample_api()))
        second = rr.render(TEMPLATE, rr.collect(NOW, sample_api()))
        self.assertEqual(first, second)

    def test_an_untouched_repository_is_omitted(self):
        page = rr.render(TEMPLATE, rr.collect(NOW, sample_api()))
        self.assertIn("Acme/alpha", page)
        self.assertNotIn("Acme/a-fork", page)

    def test_totals_and_median_are_counted_not_typed(self):
        snapshot = rr.collect(NOW, sample_api())
        self.assertEqual((snapshot.prs_opened, snapshot.prs_merged, snapshot.prs_open_now), (3, 2, 1))
        self.assertEqual(snapshot.median_merge_minutes, 20.0)
        self.assertIn("20 minutes", rr.render_section(snapshot))

    def test_the_timestamp_of_the_data_is_on_the_page(self):
        self.assertIn("2026-08-17 08:30 UTC", rr.render_section(rr.collect(NOW, sample_api())))

    def test_repositories_are_ordered_by_most_recent_commit(self):
        snapshot = rr.collect(NOW, sample_api())
        self.assertEqual([r.full_name for r in snapshot.repos][0], "Acme/beta")


class FailureLeavesThePageAlone(unittest.TestCase):
    def _root(self, directory: str) -> Path:
        root = Path(directory)
        (root / "README.template.md").write_text(TEMPLATE, encoding="utf-8")
        (root / "README.md").write_text("last good page, as of 2026-08-16\n", encoding="utf-8")
        return root

    def test_a_fetch_failure_exits_nonzero_and_writes_nothing(self):
        with TemporaryDirectory() as directory:
            root = self._root(directory)
            with mock.patch.object(rr, "collect", side_effect=rr.FetchError("no network")):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = rr.main(["--write", "--root", str(root)])
            survived = (root / "README.md").read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertEqual(survived, "last good page, as of 2026-08-16\n")
        self.assertIn("last numbers with their own timestamp", stderr.getvalue())

    def test_a_successful_run_writes_the_page(self):
        with TemporaryDirectory() as directory:
            root = self._root(directory)
            with mock.patch.object(rr, "collect", return_value=rr.collect(NOW, sample_api())):
                code = rr.main(["--write", "--root", str(root)])
            written = (root / "README.md").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("What it has been doing", written)
        self.assertIn("### Tail", written)

    def test_stdout_mode_leaves_the_file_alone(self):
        with TemporaryDirectory() as directory:
            root = self._root(directory)
            with mock.patch.object(rr, "collect", return_value=rr.collect(NOW, sample_api())):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = rr.main(["--root", str(root)])
            survived = (root / "README.md").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("What it has been doing", out.getvalue())
        self.assertEqual(survived, "last good page, as of 2026-08-16\n")

    def test_a_search_result_set_beyond_the_cap_fails_rather_than_undercounts(self):
        api = sample_api(search_total=5000)
        with self.assertRaises(rr.FetchError) as caught:
            rr.collect(NOW, api)
        self.assertIn("search cap", str(caught.exception))

    def test_an_empty_repository_counts_as_zero_rather_than_failing(self):
        def fetcher(url, **kwargs):
            raise rr.FetchError("GET ... failed: HTTP Error 409: Conflict")

        self.assertEqual(rr.count_commits("Acme/empty", fetcher), (0, None))

    def test_a_real_commit_failure_still_propagates(self):
        def fetcher(url, **kwargs):
            raise rr.FetchError("GET ... failed: HTTP Error 502: Bad Gateway")

        with self.assertRaises(rr.FetchError):
            rr.count_commits("Acme/alpha", fetcher)

    def test_a_template_without_markers_is_rejected(self):
        with self.assertRaises(RuntimeError):
            rr.render("## No markers here\n", rr.collect(NOW, sample_api()))

    def test_retries_then_gives_up_loudly(self):
        import urllib.error

        attempts = []

        def flaky(request, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(request.full_url, 403, "rate limited", {}, None)

        with mock.patch.object(rr.urllib.request, "urlopen", flaky):
            with self.assertRaises(rr.FetchError):
                rr.fetch(f"{rr.API}/x", attempts=3, sleep=lambda _: None)
        self.assertEqual(len(attempts), 3)


class Formatting(unittest.TestCase):
    def test_durations_read_in_the_unit_a_reader_can_check(self):
        self.assertEqual(rr.humanise_minutes(1.0), "1 minute")
        self.assertEqual(rr.humanise_minutes(9.4), "9 minutes")
        self.assertEqual(rr.humanise_minutes(89), "89 minutes")
        self.assertEqual(rr.humanise_minutes(180), "3.0 hours")
        self.assertEqual(rr.humanise_minutes(60 * 72), "3.0 days")

    def test_a_merge_reads_as_a_merge_in_both_event_shapes(self):
        feed = rr.collect_events({"Acme/alpha", "Acme/beta"}, sample_api())
        rendered = [event.text for event in feed]
        self.assertIn("merged [pull request #8](https://github.com/Acme/alpha/pull/8) "
                      "in [Acme/alpha](https://github.com/Acme/alpha)", rendered)
        self.assertIn("merged [pull request #7](https://github.com/Acme/alpha/pull/7) "
                      "in [Acme/alpha](https://github.com/Acme/alpha)", rendered)
        self.assertTrue(any("without merging" in text for text in rendered))
        self.assertTrue(any(text.startswith("pushed 3 commits") for text in rendered))
        self.assertFalse(any(SENTINEL in text for text in rendered))

    def test_the_refresh_badge_points_at_the_workflow(self):
        self.assertIn(rr.WORKFLOW_FILE, rr._badge())


if __name__ == "__main__":
    unittest.main()
