#!/bin/bash
# Double-click launcher for macOS Finder. Runs the full pipeline in Terminal.
cd "$(dirname "$0")"
bash run_jobbot.sh "$@"
