#!/usr/bin/env bash
set -euo pipefail

source_root="${PANOPTICON_SOURCE_ROOT:-/ctx/panopticon-source/panopticon}"
if [[ ! -d "${source_root}" ]]; then
  exit 0
fi

purelib="${PANOPTICON_PURELIB:-$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')}"
# This asset runs in Linux images and in the host-side package replacement test. macOS's BSD
# userland does not implement GNU's --recursive/--force/--archive spellings; -rf/-a preserve the
# same remove-then-copy semantics on both hosts.
rm -rf -- "${purelib}/panopticon"
cp -a "${source_root}" "${purelib}/panopticon"
