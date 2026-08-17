# How this page is built

`README.md` is generated. Edit `README.template.md`; the section between its
`BEGIN GENERATED` and `END GENERATED` markers is written by
`render_readme.py` and overwritten on every refresh.

| Command | What it does |
| --- | --- |
| `python3 tools/render_readme.py` | prints the page it would write |
| `python3 tools/render_readme.py --write` | rewrites `README.md` |
| `python3 -m unittest discover -s tools -t tools` | runs the tests |
| `tools/verify_public_only.sh` | proves the page carries no private work |

`.github/workflows/refresh-activity.yml` runs the renderer daily, and commits
`README.md` only when the public record has moved.

## Three decisions worth knowing

**The renderer never authenticates.** It reads no token from the environment
and builds no `Authorization` header, so its output cannot grow when it is
handed more access. This is what keeps private repository names and private
pull requests off a public page — not a filter, which would have to list the
private names in this repository to work.

The cost is GitHub's unauthenticated rate limit of 60 requests an hour per
address. A refresh spends about 15. A run that gets throttled retries, then
fails; the page keeps its last numbers.

**Nothing free-form from the record reaches the page.** Repository names come
from public repository listings; everything else rendered is an integer, a
date, or a URL built from those names. Pull request titles, commit messages and
branch names are read and never printed, so a title mentioning a private
project cannot publish it.

That is also why the table names repositories without describing them: a
repository's own public description is free text, and one of ours names two
repositories that are not public.

**A failure leaves the page stale rather than wrong.** Any failed read aborts
before a byte is written, so `README.md` keeps its last good content — with the
timestamp it was counted at, on the page, where a reader can see it is old. The
workflow then opens an issue here, and the badge on the page turns red.

The remaining gap: GitHub disables scheduled workflows in repositories that go
60 days without activity, and a disabled schedule fails nothing. The visible
timestamp is what covers that case.
