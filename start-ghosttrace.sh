#!/bin/sh
# Starts GhostTrace on this computer and opens it in your web browser.
# Nothing is uploaded: the app answers only on this computer.
cd "$(dirname "$0")" || exit 1
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    exec "$candidate" code/main.py serve --open "$@"
  fi
done
echo "GhostTrace needs Python 3.10 or newer: https://www.python.org/downloads/"
exit 1
