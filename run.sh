#!/bin/bash
# Run one pipeline step detached, with a hard memory cap (only the step is killed if it is exceeded).
#   ./run.sh match            -> venv/bin/python pipeline/match.py, log data/logs/match.log, pid data/logs/match.pid
#   MEM=4G ./run.sh tiles     (default cap: 10G)
#   wait:  while kill -0 $(cat data/logs/match.pid) 2>/dev/null; do sleep 10; done
cd "$(dirname "$0")"
step=$1; shift
mkdir -p data/logs
setsid nohup bash -c 'echo $$ > "data/logs/$0.pid"; exec systemd-run --user --scope -q -p MemoryMax="$1" -p MemorySwapMax=0 venv/bin/python "pipeline/$0.py" "${@:2}"' \
  "$step" "${MEM:-10G}" "$@" > "data/logs/$step.log" 2>&1 < /dev/null &
sleep 0.5
echo "started $step (pid $(cat data/logs/$step.pid)), log: $PWD/data/logs/$step.log"
