#!/bin/bash
# Tests for pipeline_lock.sh - the locking run_pipeline.sh and sync_data.sh
# rely on. Run from anywhere:  bash tests/test_pipeline_lock.sh   (~40s)
# Needs macOS/BSD or Linux with python3 (the lock is flock(2) via fcntl).
# LOCK_IMPL=<file> runs the same suite against another implementation.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOCK_IMPL="${LOCK_IMPL:-$REPO/pipeline_lock.sh}"
WORK="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT
cp "$LOCK_IMPL" "$WORK/pipeline_lock.sh"
cd "$WORK"
export PIPELINE_LOCK_POLL=0.2   # poll fast so the suite stays short

PASS=0; FAIL=0
ok()   { echo "  ok    $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL  $1"; echo "        events: $(tr '\n' ' ' < events.log 2>/dev/null)"; FAIL=$((FAIL + 1)); }
reset() { rm -rf events.log ./*.out .run_pipeline.* .sync_data.*; }
starts()     { grep -c '^start' events.log 2>/dev/null || true; }
max_active() { awk '/^start/{a++; if (a > m) m = a} /^end/{a--} END{print m + 0}' events.log; }
order()      { awk '{print $1, $2}' events.log | tr '\n' ' ' | sed 's/ $//'; }

# worker.sh JOB ID HOLD - enters exactly the way run_pipeline.sh does, then
# "works" for HOLD seconds, logging start/end with a timestamp.
cat > worker.sh <<'EOF'
#!/bin/bash
cd "$(dirname "$0")"
. ./pipeline_lock.sh
pipeline_enter "$1"
case $? in
  0)  ;;
  75) echo "skip $2" >> events.log; exit 0 ;;
  *)  echo "error $2" >> events.log; exit 1 ;;
esac
echo "start $2 $(date +%s)" >> events.log
sleep "$3"
echo "end $2 $(date +%s)" >> events.log
EOF

# syncworker.sh ID - sync_data.sh's lock: every caller must run, one at a time.
cat > syncworker.sh <<'EOF'
#!/bin/bash
cd "$(dirname "$0")"
. ./pipeline_lock.sh
lock_wait 7 ".sync_data.flock" || { echo "error $1" >> events.log; exit 1; }
echo "start $1 $(date +%s)" >> events.log
sleep 1
echo "end $1 $(date +%s)" >> events.log
EOF

echo "pipeline_lock tests ($LOCK_IMPL)"

# 1. Five different jobs fired at the same moment run strictly one at a time.
reset
for i in 1 2 3 4 5; do bash worker.sh "job$i" "w$i" 1 & done; wait
if [ "$(starts)" -eq 5 ] && [ "$(max_active)" -eq 1 ]; then ok "5 simultaneous jobs run one at a time"
else fail "5 simultaneous jobs run one at a time"; fi

# 2. A long-running holder is never "taken over" by a waiter
#    (the old lock stole it once the lock was STALE_AFTER old - 307 times).
reset
bash worker.sh jobA A 6 & sleep 1
bash worker.sh jobB B 1 &
wait
if [ "$(order)" = "start A end A start B end B" ]; then ok "long-running holder is not taken over"
else fail "long-running holder is not taken over"; fi

# 3. A holder killed with kill -9 frees the lock at once, and of two waiters
#    exactly one gets in at a time (the old takeover let both in together).
reset
set -m; bash worker.sh jobA A 30 & apid=$!; set +m
sleep 1
bash worker.sh jobB B 2 & bash worker.sh jobC C 2 &
sleep 1
killed=$(date +%s); kill -9 -- "-$apid" 2>/dev/null
wait
first=$(awk '/^start [BC]/{print $3; exit}' events.log)
waiters_overlap=$(awk '/^start [BC] /{a++; if (a > m) m = a} /^end [BC] /{a--} END{print m + 0}' events.log)
# (A never logs "end" - it was killed - so judge overlap among B and C only)
if [ "$(starts)" -eq 3 ] && [ "$waiters_overlap" -eq 1 ] && [ -n "$first" ] && [ $((first - killed)) -le 2 ]; then
  ok "kill -9 frees the lock immediately; waiters still one at a time"
else fail "kill -9 frees the lock immediately; waiters still one at a time"; fi

# 4. At most one queued run per job: a third trigger of a job that already
#    has a run waiting bows out; other jobs still queue normally.
reset
bash worker.sh core A 4 & sleep 0.5
bash worker.sh core B 1 & sleep 0.5
bash worker.sh core C 1 & sleep 0.5
bash worker.sh accounts D 1 &
wait
if grep -q '^skip C' events.log && [ "$(starts)" -eq 3 ] && grep -q '^start B' events.log \
   && grep -q '^start D' events.log && [ "$(max_active)" -eq 1 ]; then
  ok "duplicate queued trigger is skipped, other jobs still queue"
else fail "duplicate queued trigger is skipped, other jobs still queue"; fi

# 5. Once the queued run starts, a new trigger of that job queues again
#    (data may have changed after the running job's sync).
reset
bash worker.sh core A 2 & sleep 0.5
bash worker.sh core B 2 &
for _ in $(seq 1 100); do grep -q '^start B' events.log 2>/dev/null && break; sleep 0.1; done
bash worker.sh core E 1 &
wait
if grep -q '^start E' events.log && ! grep -q '^skip' events.log && [ "$(max_active)" -eq 1 ]; then
  ok "trigger arriving mid-run queues behind it"
else fail "trigger arriving mid-run queues behind it"; fi

# 6. If the lock can't be taken at all (python3 broken), the job must NOT
#    run unlocked and must not be mistaken for a redundant trigger.
reset
mkdir -p fakebin; printf '#!/bin/sh\nexit 1\n' > fakebin/python3; chmod +x fakebin/python3
PATH="$WORK/fakebin:/bin:/usr/bin" bash worker.sh core X 0
if grep -q '^error X' events.log && ! grep -q -E '^(start|skip) X' events.log; then
  ok "lock failure stops the job instead of running unlocked"
else fail "lock failure stops the job instead of running unlocked"; fi

# 7. A waiter that waits longer than PIPELINE_LOCK_WARN_AFTER warns once.
reset
bash worker.sh core A 3 & sleep 0.3
PIPELINE_LOCK_WARN_AFTER=1 bash worker.sh accounts B 0 > b.out 2>&1
wait
if [ "$(grep -c 'has waited over' b.out)" -eq 1 ] && grep -q '^start B' events.log; then ok "long wait warns exactly once"
else fail "long wait warns exactly once"; echo "        b.out: $(cat b.out)"; fi

# 8. The holder records who it is (PID + job) for diagnosis.
reset
bash worker.sh daily A 2 & apid=$!
sleep 0.6
if grep -q "^$apid daily " .run_pipeline.holder 2>/dev/null; then ok "holder file names the running PID and job"
else fail "holder file names the running PID and job"; fi
wait

# 9. sync_data's lock: three concurrent syncs all run, one at a time.
reset
for i in 1 2 3; do bash syncworker.sh "s$i" & done; wait
if [ "$(starts)" -eq 3 ] && [ "$(max_active)" -eq 1 ]; then ok "concurrent syncs all run, one at a time"
else fail "concurrent syncs all run, one at a time"; fi

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
