#!/usr/bin/env bash
# Pre-vendor wheels so `docker build --network=none .` works.
# Run ONCE with network access, before building. Targets the runtime image:
# distroless python3-debian12 = CPython 3.11, linux x86_64 (override with PLATFORM=...).
set -euo pipefail
cd "$(dirname "$0")/.."
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"
rm -rf wheels && mkdir wheels
pip download --dest wheels --only-binary=:all: \
  --python-version 3.11 --implementation cp --abi cp311 \
  --platform "$PLATFORM" --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 \
  "mcp>=1.2,<2" "pydantic>=2" "httpx>=0.27"
echo "vendored $(ls wheels | wc -l) wheels into ./wheels"
