#!/bin/sh
# Builds the release files of the tgdc-data-card skill from the committed tree. Nothing is uploaded.
#   dist/tgdc-data-card-v<version>.tar.gz   git archive of skills/tgdc-data-card under the prefix tgdc-data-card/
#   dist/SHA256SUMS                         checksum of that archive
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"
skill=skills/tgdc-data-card
version=$(sed -n 's/^  version: "\(.*\)"$/\1/p' "$skill/SKILL.md")
if [ -z "$version" ]; then
  echo "No version found in $skill/SKILL.md" >&2
  exit 1
fi
if [ -n "$(git status --porcelain -- "$skill")" ]; then
  echo "Uncommitted changes in $skill. Commit them first: the archive is built from HEAD." >&2
  exit 1
fi

name="tgdc-data-card-v$version.tar.gz"
mkdir -p dist
# Every entry is stamped with the commit time (--mtime needs Git 2.38 or newer) and gzip -n omits its own timestamp,
# so the same commit gives the same bytes.
stamp=$(git log -1 --format=%cI HEAD)
if ! git archive --format=tar --mtime="$stamp" --prefix=tgdc-data-card/ "HEAD:$skill" | gzip -n -9 > "dist/$name"; then
  echo "git archive failed; Git 2.38 or newer is required." >&2
  exit 1
fi
cd dist
if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$name" > SHA256SUMS
else
  sha256sum "$name" > SHA256SUMS
fi
echo "Built dist/$name"
cat SHA256SUMS
