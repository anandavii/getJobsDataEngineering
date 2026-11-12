#!/bin/bash
set -e
cd "$(dirname "$0")"

# Resolve the pinned runtime dependencies and run in uv's managed environment.
uv run --python 3.12 --with-requirements requirements.txt tools/orchestrate.py
