#!/bin/sh
# Builds the release files of one skill from the committed tree. Nothing is uploaded.
#   sh scripts/build_release.sh [skill]     skill is tgdc-data-card (the default) or tgdc-clean-copy
#   dist/<skill>-v<version>.tar.gz          git archive of skills/<skill> under the prefix <skill>/
#   dist/<skill>-v<version>.zip             the same as a ZIP file (tgdc-clean-copy only, as in its earlier releases)
#   dist/SHA256SUMS                         checksums of these archives
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"
name_of_skill=${1:-tgdc-data-card}
skill=skills/$name_of_skill
if [ ! -f "$skill/SKILL.md" ]; then
  echo "No skill $name_of_skill in skills/" >&2
  exit 1
fi
version=$(sed -n 's/^  version: "\(.*\)"$/\1/p' "$skill/SKILL.md")
if [ -z "$version" ]; then
  echo "No version found in $skill/SKILL.md" >&2
  exit 1
fi
if [ -n "$(git status --porcelain -- "$skill")" ]; then
  echo "Uncommitted changes in $skill. Commit them first: the archive is built from HEAD." >&2
  exit 1
fi

name="$name_of_skill-v$version.tar.gz"
mkdir -p dist
# Every entry is stamped with the commit time (--mtime needs Git 2.38 or newer) and gzip -n omits its own timestamp,
# so the same commit gives the same bytes.
stamp=$(git log -1 --format=%cI HEAD)
if ! git archive --format=tar --mtime="$stamp" --prefix="$name_of_skill/" "HEAD:$skill" | gzip -n -9 > "dist/$name"; then
  echo "git archive failed; Git 2.38 or newer is required." >&2
  exit 1
fi
files=$name
if [ "$name_of_skill" = tgdc-clean-copy ]; then
  files="$name $name_of_skill-v$version.zip"
  git archive --format=zip --mtime="$stamp" --prefix="$name_of_skill/" -o "dist/$name_of_skill-v$version.zip" "HEAD:$skill"
fi
cd dist
if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 $files > SHA256SUMS
else
  sha256sum $files > SHA256SUMS
fi
echo "Built dist/$name"
cat SHA256SUMS
