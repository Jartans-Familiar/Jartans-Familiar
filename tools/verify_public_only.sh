#!/usr/bin/env bash
# Checks that the rendered page cannot carry private work.
#
#   1. Renders with no credentials in the environment.
#   2. Renders again with this machine's GitHub token exported, and diffs the
#      two. A renderer whose output grows when handed more access fails here.
#   3. Lists the organisation's private repositories through the token and
#      searches the rendered page for each name. Any hit fails.
#
# Step 3 needs a token, so it is the one part that cannot run unauthenticated.
# The private names are never written into this repository: they are read from
# the API at check time.
#
# Costs about 30 unauthenticated API requests against a limit of 60 per hour,
# so two runs in the same hour on the same address will be throttled.
#
# Usage: tools/verify_public_only.sh

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

renderer=(python3 "$root/tools/render_readme.py" --root "$root")

echo "1. Rendering with no credentials."
env -u GITHUB_TOKEN -u GH_TOKEN -u GITHUB_API_URL -u GH_HOST \
  "${renderer[@]}" >"$work/public.md"

if ! command -v gh >/dev/null 2>&1 || ! token="$(gh auth token 2>/dev/null)" || [ -z "$token" ]; then
  echo "FAIL: no GitHub token available, so steps 2 and 3 cannot run." >&2
  echo "Run 'gh auth login' and try again; a pass without them means nothing." >&2
  exit 2
fi

echo "2. Rendering with a token exported, and comparing."
GITHUB_TOKEN="$token" GH_TOKEN="$token" "${renderer[@]}" >"$work/privileged.md"

# The timestamp differs by design if a minute rolls over between the two runs.
strip_timestamp() { sed -E 's/at \*\*[0-9-]+ [0-9:]+ UTC\*\*/at **TIMESTAMP**/' "$1"; }

if ! diff -u <(strip_timestamp "$work/public.md") <(strip_timestamp "$work/privileged.md"); then
  echo "FAIL: the output changed when a token was present." >&2
  exit 1
fi
echo "   Identical."

echo "3. Searching the rendered page for private repository names."
org="$(python3 -c "import sys; sys.path.insert(0, '$root/tools'); import render_readme; print(render_readme.ORG)")"
names="$(gh api "orgs/$org/repos?per_page=100" --paginate --jq '.[] | select(.private) | .name')"
names="$names
$(gh api "user/repos?per_page=100&affiliation=owner" --paginate --jq '.[] | select(.private) | .name')"

hits=0
count=0
while read -r name; do
  [ -z "$name" ] && continue
  count=$((count + 1))
  if grep -Fqi -- "$name" "$work/public.md"; then
    echo "FAIL: the page names the private repository '$name'." >&2
    hits=$((hits + 1))
  fi
done <<<"$names"

if [ "$count" -eq 0 ]; then
  echo "FAIL: no private repositories were listed, so this step proved nothing." >&2
  exit 2
fi
if [ "$hits" -ne 0 ]; then
  exit 1
fi

echo "   $count private repository names checked, none on the page."
echo "PASS"
