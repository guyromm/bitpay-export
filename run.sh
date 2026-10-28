#!/bin/bash
# Run the exporter, log to run.log, and tell the Claude pane given as $1 how it ended.
cd "$(dirname "$0")"
./bitpay_exporter.py "${@:2}" 2>&1 | tee run.log
code=${PIPESTATUS[0]}
if [ -n "$1" ]; then
    tmux send-keys -t "$1" "bitpay-exporter run ended (exit $code), see run.log" && sleep 0.5 &&
        tmux send-keys -t "$1" Enter && sleep 0.5 && tmux send-keys -t "$1" Enter
fi
