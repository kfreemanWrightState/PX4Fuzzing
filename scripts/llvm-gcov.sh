#!/usr/bin/env bash
set -euo pipefail

# lcov calls "gcov" with many flags. This routes it through LLVM's gcov emulation.
exec llvm-cov gcov "$@"
