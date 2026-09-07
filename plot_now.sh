#!/bin/bash
# One-click dashboard launcher.
#
# Starts the local HTTP server (if not already running) and opens the
# dashboard in the default browser, pointed at whichever data_<machine>/
# directory actually exists here -- so the same script works unmodified
# on any machine this repo is cloned onto, without hardcoding a hostname.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8000

cd "$SCRIPT_DIR"

echo "Looking for this machine's data_HC/ or data_JC/ in $SCRIPT_DIR ..."

HC_EXISTS=0; [ -d "data_HC" ] && HC_EXISTS=1
JC_EXISTS=0; [ -d "data_JC" ] && JC_EXISTS=1

if [ "$HC_EXISTS" = 1 ] && [ "$JC_EXISTS" = 1 ]; then
    echo "Both data_HC/ and data_JC/ exist here -- can't tell which is this machine's own data." >&2
    echo "Run: $0 data_HC   (or data_JC)" >&2
    if [ -n "$1" ]; then
        DATA_DIR="$1"
        echo "Using data dir given on the command line: $DATA_DIR"
    else
        exit 1
    fi
elif [ "$HC_EXISTS" = 1 ]; then
    DATA_DIR="data_HC"
    echo "Found data_HC/ -- using that."
elif [ "$JC_EXISTS" = 1 ]; then
    DATA_DIR="data_JC"
    echo "Found data_JC/ -- using that."
else
    echo "Neither data_HC/ nor data_JC/ found in $SCRIPT_DIR -- has the daemon run yet on this machine?" >&2
    exit 1
fi

URL="http://localhost:$PORT/web/?data_dir=$DATA_DIR"

echo "Checking whether the dashboard server is already running on port $PORT ..."
if ! curl -s -o /dev/null "http://localhost:$PORT/web/"; then
    echo "Not running -- starting: python3 -m http.server $PORT (log: /tmp/temperature-monitor-http.log)"
    nohup python3 -m http.server "$PORT" >/tmp/temperature-monitor-http.log 2>&1 &
    disown
    sleep 1
else
    echo "Already running."
fi

echo "Opening dashboard: $URL"
xdg-open "$URL" >/dev/null 2>&1 &
