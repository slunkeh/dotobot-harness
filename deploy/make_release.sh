#!/bin/sh
# Cut a harness release from the reviewed source tree and print its manifest.
#
#   deploy/make_release.sh [out-dir]
#
# Produces out-dir/harness-<version>.tar.gz (version read from
# harness/version.py) plus a manifest.json template. Upload the tarball
# first, then the manifest — hosts converge on the manifest, so it must
# only ever reference an already-uploaded tarball.
set -eu

OUT="${1:-dist}"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' harness/version.py)
[ -n "$VERSION" ] || { echo "could not read version from harness/version.py" >&2; exit 1; }

mkdir -p "$OUT"
TARBALL="$OUT/harness-$VERSION.tar.gz"
SHA256=$(python3 deploy/public_export.py --archive "$TARBALL")

cat > "$OUT/manifest.json" <<EOF
{
  "version": "$VERSION",
  "url": "https://releases.example.com/harness/harness-$VERSION.tar.gz",
  "sha256": "$SHA256",
  "rollout_percent": 5,
  "min_app_version": null,
  "latest_app_version": null,
  "app_download_url": null
}
EOF

echo "release: $TARBALL"
echo "sha256:  $SHA256"
echo "manifest template: $OUT/manifest.json (edit url/rollout, upload tarball FIRST)"
