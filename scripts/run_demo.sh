#!/usr/bin/env bash
# GAUNTLET demo runner: one command, existing CLI only, genuine results, nothing faked.
#
#   scripts/run_demo.sh [CANDIDATE_DIR]
#
# Run from the repository root with the virtual environment active and PostgreSQL 16 reachable
# (GAUNTLET_PG_DSN, default postgresql://gauntlet:gauntlet@127.0.0.1:55432/postgres).
# CANDIDATE_DIR defaults to the labelled synthetic fixture backend/tests/candidates/synthetic_stale_backfill.
#
# What it runs:
#   1. server check and frozen-evidence checksums
#   2. a reference implementation and a faulty variant (both hand-written, trusted, part of the repository)
#      on the same adversarial schedule; the independent oracle must pass the reference and fail the variant
#   3. exact replay of the failing run (logical hashes)
#   4. static validation, registration and verification of CANDIDATE_DIR (no candidate code is executed)
#   5. the safety gate: evaluating the candidate WITHOUT a trusted-environment declaration must be refused
#      (exit code 3) before any candidate code is loaded
# It never executes CANDIDATE_DIR's code: process mode is not a security boundary (docs/SECURITY_MODEL.md).
# All output goes to a fresh scratch directory under .gauntlet/ (git-ignored); evidence/ is never opened.

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CAND_DIR="${1:-$ROOT/backend/tests/candidates/synthetic_stale_backfill}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${DEMO_OUT:-$ROOT/.gauntlet/demo-run-$STAMP}"
mkdir -p "$OUT" || exit 1
LOG="$OUT/demo.log"
export GAUNTLET_RESULTS_DB="$OUT/results.sqlite3"
export GAUNTLET_CANDIDATES_DIR="$OUT/candidates"
PASSED=0; FAILED=0

say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
step() { say ""; say "=== $* ==="; }
ok()   { say "  [OK]   $*"; PASSED=$((PASSED + 1)); }
bad()  { say "  [FAIL] $*"; FAILED=$((FAILED + 1)); }
run()  {  # run a command from backend/, tee its output, keep its exit status in RC and output in OUTPUT
    say "\$ $*"
    OUTPUT="$(cd "$ROOT/backend" && "$@" 2>&1)"; RC=$?
    printf '%s\n' "$OUTPUT" | tee -a "$LOG"
}

say "GAUNTLET demo  $(date -Iseconds)  commit $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
say "results: $OUT"
command -v python >/dev/null 2>&1 || { say "python not found: activate the virtual environment first"; exit 2; }

step "1. Environment"
run python -m gauntlet pg check
if [ $RC -eq 0 ] && printf '%s' "$OUTPUT" | grep -q '"is_pg16": true'; then ok "PostgreSQL 16 reachable"
else bad "PostgreSQL 16 not reachable (start it: python -m gauntlet pg start, or set GAUNTLET_PG_DSN)"; exit 1; fi
say "\$ (cd evidence && sha256sum -c *.sha256)"
SUMS="$(cd "$ROOT/evidence" && sha256sum -c ./*.sha256 2>&1)"; SUMS_RC=$?
printf '%s\n' "$SUMS" | tee -a "$LOG"
NFILES="$(ls "$ROOT"/evidence/*.sha256 2>/dev/null | wc -l)"
if [ $SUMS_RC -eq 0 ] && [ "$NFILES" -gt 0 ] && [ "$(printf '%s\n' "$SUMS" | grep -c ': OK$')" -eq "$NFILES" ]; then
    ok "frozen evidence checksums verified ($NFILES files)"
else bad "evidence checksums"; fi

step "2. Reference vs faulty variant on one adversarial schedule (S02, seed 1)"
run python -m gauntlet diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_backfill_vs_legacy_writes --seed 1
MRUN="$(printf '%s\n' "$OUTPUT" | awk '$1 == "m1_stale_backfill" && $2 == "run" {print $3; exit}')"
if [ $RC -eq 0 ] && printf '%s' "$OUTPUT" | grep -q "CONFIRMED_VIOLATION" \
   && printf '%s' "$OUTPUT" | grep -Eq "ref_a_trigger +run [0-9a-f]+ pass" \
   && printf '%s' "$OUTPUT" | grep -Eq "m1_stale_backfill +run [0-9a-f]+ fail"; then
    ok "reference passes; faulty variant fails (confirmed by the independent oracle)"
else bad "differential result not as expected"; fi

step "3. Exact replay of the failing run"
if [ -n "$MRUN" ]; then
    run python -m gauntlet replay "$MRUN"
    if [ $RC -eq 0 ] && [ "$(printf '%s\n' "$OUTPUT" | grep -cE '^(schedule_hash|trace_hash|outcome_hash|verdict) +same ')" -eq 4 ]; then
        ok "schedule, trace, outcome hashes and verdict reproduced"
    else bad "replay differs"; fi
else bad "no failing run id to replay"; fi

step "4. Candidate: static validation, registration, verification (no code executed)"
say "candidate directory: $CAND_DIR"
run python -m gauntlet candidate validate "$CAND_DIR"
if [ $RC -eq 0 ] && printf '%s' "$OUTPUT" | grep -q '"ok": true'; then ok "static validation passed"
else bad "static validation failed (see problems above)"; fi
run python -m gauntlet candidate register "$CAND_DIR"
CID="$(printf '%s\n' "$OUTPUT" | grep -oE 'cand-[0-9a-f]{16}' | head -1)"
if [ $RC -eq 0 ] && [ -n "$CID" ]; then ok "registered as $CID (content-addressed, read-only snapshot)"
else bad "registration failed"; fi
if [ -n "$CID" ]; then
    run python -m gauntlet candidate verify "$CID"
    if [ $RC -eq 0 ] && printf '%s' "$OUTPUT" | grep -q "^ok: "; then ok "snapshot verified against its source hash"
    else bad "verification failed"; fi
fi

step "5. Safety gate: untrusted execution is refused"
if [ -n "${CID:-}" ]; then
    run python -m gauntlet candidate evaluate --id "$CID" --isolation process --scenarios S01_baseline_migration --seeds 1 --refs ref_a_trigger
    if [ $RC -eq 3 ] && printf '%s' "$OUTPUT" | grep -q "BLOCKED: IsolationUnavailable"; then
        ok "evaluation refused without a trusted-environment declaration (exit 3); no candidate code ran"
    else bad "the safety gate did not refuse as expected (exit $RC)"; fi
    run python -m gauntlet candidate evaluate --id "$CID" --isolation strict --scenarios S01_baseline_migration --seeds 1 --refs ref_a_trigger
    if [ $RC -eq 3 ]; then ok "strict (VM/container) isolation is not implemented and fails closed (exit 3)"
    else bad "strict mode did not fail closed (exit $RC)"; fi
fi

step "Summary"
say "checks passed: $PASSED, failed: $FAILED"
say "log: $LOG"
say "results database: $GAUNTLET_RESULTS_DB"
say "Not shown here by design: executing the candidate. process isolation is not a security boundary;"
say "run candidate evaluate only inside a disposable VM (docs/AGENT_EVALUATION_PROTOCOL.md section 7)."
[ $FAILED -eq 0 ]
