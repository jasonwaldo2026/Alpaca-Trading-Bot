#!/bin/bash
# Double-click me on a Mac. Opens Terminal and starts the scanner and Studio.
cd "$(dirname "$0")" || exit 1
python3 start.py "$@"
echo
read -r -p "Press Return to close this window."
