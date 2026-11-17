#!/bin/sh
# Builds and publishes a release from this Mac (there is no CI):  tools/release.sh vX.Y.Z [NOTES_FILE]
#
# Refuses unless the working tree is clean, HEAD is the tag, the tag's commit is on origin/main, the version is
# the one in cockpit/__init__.py, tools/check.sh passes and origin's tag of that name is the same commit. Then it
# builds dist/vX.Y.Z/: the payload (git archive of cockpit, bin, share and LICENSE at the tag), install.sh
# rendered by tools/build_release.py, SHA256SUMS and notes.md (the install line, NOTES_FILE if given, the
# checksums). The notes go through the leak guard.
# Publishing is the owner's call: it shows what would go out and asks on the terminal, default no. Without a
# terminal or without a yes nothing is published (exit 1); the built files stay in dist/ to look at.
set -eu
cd "$(dirname "$0")/.."

ONE_LINE='curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh | sh'
USAGE="usage: tools/release.sh vX.Y.Z [NOTES_FILE]"

fail() { echo "release: $*" >&2; exit 1; }

tag=${1:-}
notes_extra=${2:-}
version=${tag#v}
case $tag in v*) ;; *) fail "$USAGE" ;; esac
case $version in "" | *[!0-9.]* | .* | *. | *..*) fail "$USAGE" ;; esac
[ "$(printf '%s' "$version" | tr -cd . | wc -c)" -eq 2 ] || fail "$USAGE"
[ -z "$notes_extra" ] || [ -r "$notes_extra" ] || fail "cannot read the notes file $notes_extra"
payload="moshi-cockpit-$version.tar.gz"
dist="dist/$tag"

python3 tools/leak_check.py --refs -- "$tag" || fail "the tag name failed the leak guard"
[ -z "$(git status --porcelain)" ] || fail "the working tree is not clean: commit or stash first"
commit=$(git rev-parse -q --verify "refs/tags/$tag^{commit}") || fail "tag $tag does not exist"
[ "$(git rev-parse HEAD)" = "$commit" ] \
  || fail "HEAD is not at the tag: check out $tag first (the checks run on the tree that ships)"
git fetch -q origin main || fail "could not fetch origin/main"
git merge-base --is-ancestor "$commit" origin/main || fail "$tag is not on origin/main: merge first, then tag"
grep -q "^__version__ = \"$version\"\$" cockpit/__init__.py \
  || fail "cockpit/__init__.py has a different __version__ than $version"
sh tools/check.sh || fail "tools/check.sh failed: not released"

rm -rf "$dist"
mkdir -p "$dist"
git archive --format=tar.gz --prefix="moshi-cockpit-$version/" -o "$dist/$payload" "$tag" cockpit bin share LICENSE
python3 tools/build_release.py --version "$version" --payload "$dist/$payload" --out "$dist/install.sh"
(cd "$dist" && shasum -a 256 install.sh "$payload" > SHA256SUMS)

fence='```'
{
  printf 'Install or update on a Mac with Apple silicon (Herdr from https://herdr.dev installed first):\n\n'
  printf '%s\n%s\n%s\n\n' "${fence}sh" "$ONE_LINE" "$fence"
  if [ -n "$notes_extra" ]; then
    cat "$notes_extra"
    printf '\n'
  fi
  printf 'SHA-256:\n\n%s\n' "$fence"
  cat "$dist/SHA256SUMS"
  printf '%s\n' "$fence"
} > "$dist/notes.md"
python3 tools/leak_check.py --text "$dist/notes.md" \
  || fail "the release notes failed the leak guard ($dist/notes.md): fix them, then run this again"

# gh's --verify-tag only asks whether origin has a tag of that name: it must also be this commit.
remote_tag=$(git ls-remote origin "refs/tags/$tag" "refs/tags/$tag^{}") || fail "could not ask origin for tag $tag"
remote_commit=$(printf '%s\n' "$remote_tag" | awk -v ref="refs/tags/$tag^{}" '$2 == ref { print $1 }')
[ -n "$remote_commit" ] || remote_commit=$(printf '%s\n' "$remote_tag" | awk -v ref="refs/tags/$tag" '$2 == ref { print $1 }')
[ -n "$remote_commit" ] || fail "tag $tag is not on origin: git push origin $tag, then run this again"
[ "$remote_commit" = "$commit" ] \
  || fail "tag $tag on origin is $remote_commit, not $commit: fix the tag on origin, then run this again"

echo "Ready to publish $tag (commit $commit) from $dist:"
echo "  install.sh  $payload  SHA256SUMS"
echo "Release notes:"
sed 's/^/  | /' "$dist/notes.md"
[ -t 0 ] || fail "Not published: no terminal to ask on. Run this in a terminal to publish $tag"
printf 'Publish %s? [y/N] ' "$tag"
answer=""
read -r answer || answer=""
case $answer in
  y | Y | yes | Yes | YES) ;;
  *) fail "Not published" ;;
esac

cd "$dist"
gh release create "$tag" install.sh "$payload" SHA256SUMS --verify-tag --notes-file notes.md
echo "Published $tag"
