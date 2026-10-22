"""Independent correctness oracle.

Inputs: the client-side ledger, the harness's raw snapshots, the initial
state, and the strategy's *declared* protocol contract (which columns must
agree when). The oracle imports no implementation code, and it never takes
an implementation's final totals as ground truth: expected values are
derived from the ledger with exact integer arithmetic.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

from gauntlet.money import MoneyError, decimal_to_cents, parse_cents
from gauntlet.oracle.invariants import BY_ID, INVARIANT_IDS, Status
from gauntlet.oracle.linearizability import (
    ABSENT, CORRUPT, INF, RELAXATIONS, History, Item, check, first_conflict,
)

CLIENT_WRITES = {"create_order": "create", "set_total": "set", "add_charge": "add"}
SCHEMA_SQLSTATES = {"42703", "42P01", "23502"}
MAX_STEP_ATTEMPTS = 3


def _result(inv_id: str, status: str, summary: str, evidence: list | None = None,
            trigger_seq: int | None = None) -> dict:
    d = BY_ID[inv_id]
    return {"id": inv_id, "category": d.category, "title": d.title, "status": status, "summary": summary,
            "evidence": evidence or [], "trigger_seq": trigger_seq}


def _cents_from_text(text) -> object:
    if text is None:
        return CORRUPT
    try:
        return decimal_to_cents(Decimal(text))
    except (InvalidOperation, MoneyError):
        return CORRUPT


class RunOracle:
    def __init__(self, *, strategy: str, initial_orders: dict, attempts: list[dict], snapshots: list[dict],
                 repeats: list[dict], target_stage: str = "contracted", inconclusive: list[str] | None = None):
        self.strategy = strategy
        self.initial = {int(k): parse_cents(v) for k, v in initial_orders.items()}
        self.attempts = attempts
        self.snapshots = snapshots
        self.repeats = repeats
        self.target_stage = target_stage
        self.inconclusive = inconclusive or []
        self.results: dict[str, dict] = {}
        self.key_conflicts: dict[int, list[dict]] = {}

    # -- migration progress as seen by clients ----------------------------------
    def _acked_before(self, op: str, q: int) -> bool:
        return any(a["op"] == op and a["role"] == "migrator" and a["status"] == "ok"
                   and a["complete_seq"] is not None and a["complete_seq"] < q for a in self.attempts)

    def _maybe_before(self, op: str, q: int) -> bool:
        return any(a["op"] == op and a["role"] == "migrator" and a["invoke_seq"] < q
                   and (a["status"] in ("uncertain", "open") or a["complete_seq"] is None or a["complete_seq"] > q)
                   for a in self.attempts)

    def _authority(self, snap: dict) -> list[str]:
        cols, q = snap["columns"], snap["seq"]
        if "total" not in cols:
            return ["total_cents"]
        if "total_cents" not in cols:
            return ["total"]
        if self._acked_before("finalize", q):
            return ["total_cents"]
        if self._maybe_before("finalize", q):
            return ["total", "total_cents"]
        return ["total"]

    @staticmethod
    def _row_value(row: dict, col: str) -> object:
        if col == "total":
            return _cents_from_text(row.get("total"))
        v = row.get("total_cents")
        return CORRUPT if v is None else int(v)

    # -- histories -------------------------------------------------------------
    def build_histories(self) -> tuple[dict[int, History], dict[str, int], list[dict]]:
        universe = set(self.initial)
        syntax_violations: list[dict] = []
        effects: dict[str, dict] = {}
        observations: dict[int, list[Item]] = {}
        obs_seq: dict[str, int] = {}
        for a in self.attempts:
            if a["role"] not in ("v1", "v2"):
                continue
            op = a["op"]
            oid = a["args"].get("order_id")
            if oid is None:
                continue
            oid = int(oid)
            universe.add(oid)
            status = "uncertain" if a["status"] == "open" else a["status"]
            end = INF if a["complete_seq"] is None else 2 * a["complete_seq"]
            window = (2 * a["invoke_seq"], end)
            result = a.get("result") or {}
            out = None
            if status == "ok" and "total" in result:
                try:
                    out = parse_cents(result["total"])
                    if not isinstance(result["total"], str) or "." not in result["total"] \
                            or len(result["total"].split(".")[1]) != 2:
                        raise MoneyError("not canonical two-decimal form")
                except (MoneyError, TypeError) as exc:
                    syntax_violations.append({"attempt_id": a["attempt_id"], "value": result["total"],
                                              "error": str(exc)})
                    out = None
            if op == "get_order":
                if status == "ok" and out is not None:
                    outs = frozenset({out})
                elif status == "failed" and (a.get("error") or {}).get("code") == "not_found":
                    outs = frozenset({ABSENT})
                else:
                    continue
                it = Item(id=a["attempt_id"], kind="read", outs=outs, intervals=(window,),
                          label=f"read {a['attempt_id']} by {a['actor']}")
                observations.setdefault(oid, []).append(it)
                obs_seq[it.id] = a["complete_seq"]
                continue
            if op not in CLIENT_WRITES:
                continue
            try:
                arg = parse_cents(a["args"]["amount" if op == "add_charge" else "total"])
            except MoneyError:
                continue  # invalid input: must be rejected; not modelled as a write
            # A logical write is identified by its idempotency key: every submission carrying the
            # same key is a retry of one operation, whatever internal id the client gave it.
            key = a["args"].get("key")
            group = ("key", key) if key is not None else ("logical_id", a["logical_id"])
            request = (CLIENT_WRITES[op], oid, arg)
            eff = effects.setdefault(group, {
                "key": key, "logical_ids": [], "order_id": oid, "kind": request[0], "arg": arg,
                "requests": set(), "attempts": [],
            })
            eff["requests"].add(request)
            if a["logical_id"] not in eff["logical_ids"]:
                eff["logical_ids"].append(a["logical_id"])
            eff["attempts"].append({"attempt_id": a["attempt_id"], "status": status, "window": window,
                                    "out": out if op == "add_charge" else None})
        key_conflicts: dict[int, list[dict]] = {}
        for eff in effects.values():
            eff["logical_id"] = "+".join(eff["logical_ids"])
            if len(eff.pop("requests")) > 1:
                # The same key was reused for a different request. Which submission the server
                # honoured cannot be inferred, so the history is not modelled (reported inconclusive).
                attempts_by_id = {x["attempt_id"]: x for x in self.attempts}
                orders = {int(attempts_by_id[t["attempt_id"]]["args"]["order_id"]) for t in eff["attempts"]}
                for o in orders:
                    key_conflicts.setdefault(o, []).append({
                        "key": eff["key"], "logical_ids": eff["logical_ids"],
                        "attempts": [{"attempt_id": t["attempt_id"], "status": t["status"],
                                      "request": attempts_by_id[t["attempt_id"]]["args"]} for t in eff["attempts"]]})
                eff["conflict"] = True
        self.key_conflicts = key_conflicts
        for snap in self.snapshots:
            universe.update(r["id"] for r in snap["rows"])
        histories: dict[int, History] = {}
        for oid in sorted(universe):
            histories[oid] = History(order_id=oid, initial=self.initial.get(oid, ABSENT))
        for eff in effects.values():
            if not eff.get("conflict"):
                histories[eff["order_id"]].effects.append(eff)
        for oid, obs in observations.items():
            histories[oid].observations.extend(obs)
        for idx, snap in enumerate(self.snapshots):
            q = snap["seq"]
            cols = self._authority(snap)
            rows = {r["id"]: r for r in snap["rows"]}
            for oid, h in histories.items():
                row = rows.get(oid)
                outs = frozenset({ABSENT}) if row is None else frozenset(self._row_value(row, c) for c in cols)
                it = Item(id=f"snap{idx}:{oid}", kind="read", outs=outs, intervals=((2 * q - 1, 2 * q + 1),),
                          label=f"snapshot '{snap['label']}' ({'/'.join(cols)})")
                h.observations.append(it)
                obs_seq[it.id] = q
        return histories, obs_seq, syntax_violations

    # -- invariants ---------------------------------------------------------------
    def evaluate(self) -> list[dict]:
        self._check_linearizability()
        self._check_agreement()
        self._check_records()
        self._check_recovery()
        self._check_compatibility()
        return [self.results[i] for i in INVARIANT_IDS]

    def _check_linearizability(self) -> None:
        histories, obs_seq, syntax = self.build_histories()
        failing, unknown = [], []
        explained: dict[str, list[dict]] = {k: [] for k in RELAXATIONS}
        first_trigger = None
        unknown_reasons: list[dict] = []
        for oid, h in histories.items():
            if oid in self.key_conflicts:
                unknown.append(oid)
                unknown_reasons.append({"order_id": oid, "reason": "idempotency key reused for a different request",
                                        "conflicts": self.key_conflicts[oid]})
                continue
            if not h.effects and not h.observations:
                continue
            res = check(h)
            if res.ok is True:
                continue
            if res.ok is None:
                unknown.append(oid)
                unknown_reasons.append({"order_id": oid, "reason": "search budget exhausted"})
                continue
            conflict = first_conflict(h)
            seq = obs_seq.get(conflict.id) if conflict else None
            by = []
            for relax in RELAXATIONS:
                r = check(h, relax=relax)
                if r.ok:
                    by.append(relax)
            ev = {
                "order_id": oid,
                "initial": h.initial if h.initial == ABSENT else h.initial,
                "explained_by": by,
                "first_conflict": None if conflict is None else {
                    "observation": conflict.label, "seq": seq,
                    "observed": sorted(str(v) for v in conflict.outs)},
                "writes": [{"logical_id": e["logical_id"], "kind": e["kind"], "arg_cents": e["arg"],
                            "attempts": [{"attempt_id": x["attempt_id"], "status": x["status"],
                                          "out_cents": x["out"]} for x in e["attempts"]]}
                           for e in h.effects],
            }
            failing.append(ev)
            for relax in by:
                explained[relax].append(ev)
            if seq is not None and (first_trigger is None or seq < first_trigger):
                first_trigger = seq

        def trig(evs):
            seqs = [e["first_conflict"]["seq"] for e in evs if e["first_conflict"] and e["first_conflict"]["seq"]]
            return min(seqs) if seqs else None

        if failing:
            self.results["S7_LINEARIZABLE"] = _result(
                "S7_LINEARIZABLE", Status.FAIL,
                f"{len(failing)} order(s) have non-linearizable histories: "
                + ", ".join(str(e["order_id"]) for e in failing)
                + (f" (orders {unknown} not checked: see evidence)" if unknown else ""),
                failing + unknown_reasons, first_trigger)
        elif unknown:
            self.results["S7_LINEARIZABLE"] = _result(
                "S7_LINEARIZABLE", Status.INCONCLUSIVE,
                f"orders {unknown} could not be checked: "
                + "; ".join(sorted({r["reason"] for r in unknown_reasons})), unknown_reasons)
        else:
            self.results["S7_LINEARIZABLE"] = _result(
                "S7_LINEARIZABLE", Status.PASS,
                f"all {len(histories)} order histories are linearizable")
        s7 = self.results["S7_LINEARIZABLE"]["status"]

        def derived(inv_id: str, relax: str, what: str) -> None:
            evs = explained[relax]
            if evs:
                self.results[inv_id] = _result(
                    inv_id, Status.FAIL,
                    f"{len(evs)} order(s) become linearizable only if {RELAXATIONS[relax]} "
                    f"(observations consistent with {what})", evs, trig(evs))
            elif s7 == Status.PASS:
                self.results[inv_id] = _result(inv_id, Status.PASS, "implied by S7 (history linearizable)")
            else:
                self.results[inv_id] = _result(
                    inv_id, Status.INCONCLUSIVE,
                    f"S7 is {s7}; the relaxation '{RELAXATIONS[relax]}' does not explain the failure, so this "
                    "invariant can be neither confirmed nor refuted")

        derived("S1_ACKED_DURABLE", "lost_ack", "a lost acknowledged write")
        derived("S4_NO_DUPLICATION", "duplicate", "duplicate application")
        derived("S6_FAILED_NOT_APPLIED", "failed_applied", "a failed operation taking effect")
        derived("S2_EXACT_CENTS", "rounding", "a one-cent rounding error")
        if syntax:
            prev = self.results["S2_EXACT_CENTS"]
            self.results["S2_EXACT_CENTS"] = _result(
                "S2_EXACT_CENTS", Status.FAIL,
                f"{len(syntax)} response amount(s) not in exact two-decimal form"
                + ("" if prev["status"] != Status.FAIL else "; " + prev["summary"]),
                syntax + prev["evidence"], prev["trigger_seq"])

    def _contract(self, q: int) -> tuple[bool, bool] | None:
        """(agree where both non-NULL, total_cents must be non-NULL) required at seq q."""
        if self._acked_before("contract", q) or self._maybe_before("contract", q):
            return None
        if self.strategy == "A":
            if not self._acked_before("expand", q):
                return None
            fin = self._acked_before("finalize", q)
            return True, fin
        if self.strategy == "B":
            if self._acked_before("finalize", q):
                return True, True
            return None
        return None

    def _check_agreement(self) -> None:
        violations, checked = [], 0
        for snap in self.snapshots:
            req = self._contract(snap["seq"])
            if req is None or "total" not in snap["columns"] or "total_cents" not in snap["columns"]:
                continue
            checked += 1
            agree, nonnull = req
            bad = []
            for r in snap["rows"]:
                t, c = _cents_from_text(r["total"]), r["total_cents"]
                if c is None:
                    if nonnull:
                        bad.append({"id": r["id"], "total": r["total"], "total_cents": None, "problem": "NULL"})
                elif agree and t != c:
                    bad.append({"id": r["id"], "total": r["total"], "total_cents": c, "problem": "disagree"})
            if bad:
                violations.append({"snapshot": snap["label"], "seq": snap["seq"], "rows": bad})
        if violations:
            self.results["S3_REPR_AGREEMENT"] = _result(
                "S3_REPR_AGREEMENT", Status.FAIL,
                f"representations disagree in {len(violations)} snapshot(s)", violations, violations[0]["seq"])
        elif checked:
            self.results["S3_REPR_AGREEMENT"] = _result(
                "S3_REPR_AGREEMENT", Status.PASS, f"{checked} snapshot(s) checked under strategy {self.strategy}")
        else:
            self.results["S3_REPR_AGREEMENT"] = _result(
                "S3_REPR_AGREEMENT", Status.NOT_APPLICABLE,
                "no snapshot fell in a window where both representations must agree")

    def _check_records(self) -> None:
        created_any = {int(a["args"]["order_id"]) for a in self.attempts
                       if a["op"] == "create_order" and a["role"] in ("v1", "v2")}
        violations = []
        for snap in self.snapshots:
            q = snap["seq"]
            required = set(self.initial) | {
                int(a["args"]["order_id"]) for a in self.attempts
                if a["op"] == "create_order" and a["status"] == "ok" and a["complete_seq"] < q}
            present = {r["id"]: r for r in snap["rows"]}
            missing = sorted(required - set(present))
            unexpected = sorted(set(present) - set(self.initial) - created_any)
            cols = self._authority(snap)
            corrupt = sorted(oid for oid, r in present.items()
                             if all(self._row_value(r, c) == CORRUPT for c in cols))
            if missing or unexpected or corrupt:
                violations.append({"snapshot": snap["label"], "seq": q, "missing": missing,
                                   "unexpected": unexpected, "null_authoritative": corrupt})
        if violations:
            self.results["S5_RECORDS_PRESERVED"] = _result(
                "S5_RECORDS_PRESERVED", Status.FAIL, f"record violations in {len(violations)} snapshot(s)",
                violations, violations[0]["seq"])
        elif self.snapshots:
            self.results["S5_RECORDS_PRESERVED"] = _result(
                "S5_RECORDS_PRESERVED", Status.PASS, f"{len(self.snapshots)} snapshot(s) checked")
        else:
            self.results["S5_RECORDS_PRESERVED"] = _result(
                "S5_RECORDS_PRESERVED", Status.INCONCLUSIVE, "no snapshots were captured")

    def _migration_attempts(self, op: str | None = None) -> list[dict]:
        return [a for a in self.attempts if a["role"] == "migrator" and (op is None or a["op"] == op)]

    def _check_recovery(self) -> None:
        # R1: interrupted steps resume.
        by_logical: dict[str, list[dict]] = {}
        for a in self._migration_attempts():
            by_logical.setdefault(a["logical_id"], []).append(a)
        r1_ev, r1_fail, r1_open = [], [], []
        for lid, atts in by_logical.items():
            crashed = [i for i, a in enumerate(atts) if a["reason"] == "crashed"]
            if not crashed:
                continue
            later = atts[crashed[0] + 1:]
            ok = [a for a in later if a["status"] == "ok"]
            entry = {"logical_id": lid, "op": atts[0]["op"],
                     "attempts": [{"attempt_id": a["attempt_id"], "status": a["status"], "reason": a["reason"],
                                   "error": a["error"]} for a in atts]}
            r1_ev.append(entry)
            if ok and atts.index(ok[0]) < MAX_STEP_ATTEMPTS + crashed[0] + 1:
                continue
            (r1_fail if later else r1_open).append(entry)
        if not r1_ev:
            self.results["R1_INTERRUPTED_RESUMES"] = _result(
                "R1_INTERRUPTED_RESUMES", Status.NOT_APPLICABLE, "no migration step was interrupted by a crash")
        elif r1_fail:
            self.results["R1_INTERRUPTED_RESUMES"] = _result(
                "R1_INTERRUPTED_RESUMES", Status.FAIL,
                f"{len(r1_fail)} interrupted step(s) did not resume within {MAX_STEP_ATTEMPTS} attempts", r1_fail)
        elif r1_open:
            self.results["R1_INTERRUPTED_RESUMES"] = _result(
                "R1_INTERRUPTED_RESUMES", Status.INCONCLUSIVE, "interrupted step(s) were never retried", r1_open)
        else:
            self.results["R1_INTERRUPTED_RESUMES"] = _result(
                "R1_INTERRUPTED_RESUMES", Status.PASS, f"{len(r1_ev)} interrupted step(s) resumed", r1_ev)

        # R2: repeated completed steps are harmless.
        attempts = {a["attempt_id"]: a for a in self.attempts}
        r2_fail, r2_unknown, r2_ev = [], [], []
        for rep in self.repeats:
            a = attempts.get(rep["attempt_id"])
            pre, post = self.snapshots[rep["pre"]], self.snapshots[rep["post"]]
            keys = ("columns", "not_null", "triggers", "rows")
            changed = [k for k in keys if pre[k] != post[k]]
            entry = {"logical_id": rep["logical_id"], "op": rep["op"], "attempt_id": rep["attempt_id"],
                     "status": a["status"] if a else None, "error": a["error"] if a else None,
                     "changed": changed, "pre_seq": pre["seq"], "post_seq": post["seq"]}
            r2_ev.append(entry)
            if a is None or a["status"] in ("uncertain", "open"):
                r2_unknown.append(entry)
            elif changed or (a["status"] == "failed" and (a["error"] or {}).get("kind") != "app"):
                r2_fail.append(entry)
        if not self.repeats:
            self.results["R2_REPEAT_SAFE"] = _result("R2_REPEAT_SAFE", Status.NOT_APPLICABLE,
                                                     "no completed step was repeated")
        elif r2_fail:
            self.results["R2_REPEAT_SAFE"] = _result(
                "R2_REPEAT_SAFE", Status.FAIL, f"{len(r2_fail)} repeated step(s) changed state or errored",
                r2_fail, r2_fail[0]["post_seq"])
        elif r2_unknown:
            self.results["R2_REPEAT_SAFE"] = _result("R2_REPEAT_SAFE", Status.INCONCLUSIVE,
                                                     "repeated step outcome unknown", r2_unknown)
        else:
            self.results["R2_REPEAT_SAFE"] = _result(
                "R2_REPEAT_SAFE", Status.PASS, f"{len(r2_ev)} repeated step(s) left state unchanged", r2_ev)

        # R3: backfill retry safety.
        bfs = self._migration_attempts("backfill_batch")
        interrupted = [i for i, a in enumerate(bfs) if a["status"] in ("uncertain", "open")]
        repeated_bf = [r for r in self.repeats if r["op"] == "backfill_batch"]
        if not interrupted and not repeated_bf:
            self.results["R3_BACKFILL_RETRY_SAFE"] = _result(
                "R3_BACKFILL_RETRY_SAFE", Status.NOT_APPLICABLE, "no backfill batch was interrupted or repeated")
        else:
            start = interrupted[0] if interrupted else 0
            n_rows = max((len(s["rows"]) for s in self.snapshots), default=0)
            size = max((int(a["args"].get("batch_size", 1)) for a in bfs), default=1)
            bound = math.ceil(n_rows / size) + 3
            after = bfs[start + 1:]
            done_idx = next((i for i, a in enumerate(after)
                             if a["status"] == "ok" and (a["result"] or {}).get("done")), None)
            fin_ok = any(a["status"] == "ok" for a in self._migration_attempts("finalize"))
            structural = [i for i in ("S3_REPR_AGREEMENT", "S5_RECORDS_PRESERVED")
                          if self.results.get(i, {}).get("status") == Status.FAIL]
            ev = [{"interrupted_attempts": [bfs[i]["attempt_id"] for i in interrupted],
                   "batches_after_interruption": len(after), "bound": bound,
                   "done_after": None if done_idx is None else done_idx + 1,
                   "finalize_acknowledged": fin_ok, "structural_failures": structural}]
            if done_idx is not None and done_idx + 1 <= bound and fin_ok and not structural:
                self.results["R3_BACKFILL_RETRY_SAFE"] = _result(
                    "R3_BACKFILL_RETRY_SAFE", Status.PASS,
                    f"backfill completed {done_idx + 1} batch(es) after interruption (bound {bound})", ev)
            else:
                why = []
                if done_idx is None:
                    why.append("backfill never reported completion after the interruption")
                elif done_idx + 1 > bound:
                    why.append(f"completion took {done_idx + 1} batches (bound {bound})")
                if not fin_ok:
                    why.append("finalize was never acknowledged afterwards")
                if structural:
                    why.append(f"structural invariants failed: {structural}")
                self.results["R3_BACKFILL_RETRY_SAFE"] = _result(
                    "R3_BACKFILL_RETRY_SAFE", Status.FAIL, "; ".join(why), ev)

        # R4: terminal state.
        final = self.snapshots[-1] if self.snapshots else None
        contract_ok = any(a["status"] == "ok" for a in self._migration_attempts("contract"))
        failed_steps = [{"attempt_id": a["attempt_id"], "op": a["op"], "status": a["status"],
                         "error": a["error"]} for a in self._migration_attempts()
                        if a["status"] != "ok"]
        if self.target_stage != "contracted":
            self.results["R4_TERMINAL_STATE"] = _result("R4_TERMINAL_STATE", Status.NOT_APPLICABLE,
                                                        "scenario does not target the contracted state")
        elif final is None:
            self.results["R4_TERMINAL_STATE"] = _result("R4_TERMINAL_STATE", Status.INCONCLUSIVE,
                                                        "no final snapshot")
        else:
            problems = []
            if not contract_ok:
                problems.append("contract was never acknowledged")
            if "total" in final["columns"]:
                problems.append("legacy column total still present")
            if "total_cents" not in final["columns"] or not final["not_null"].get("total_cents"):
                problems.append("total_cents missing or nullable")
            if final["triggers"]:
                problems.append(f"triggers remain: {final['triggers']}")
            if any(r.get("total_cents") is None for r in final["rows"]):
                problems.append("rows with NULL total_cents")
            ev = [{"final_columns": final["columns"], "final_not_null": final["not_null"],
                   "final_triggers": final["triggers"], "non_ok_migration_attempts": failed_steps}]
            if not problems:
                self.results["R4_TERMINAL_STATE"] = _result(
                    "R4_TERMINAL_STATE", Status.PASS, "contracted state reached and verified by introspection", ev)
            elif self.inconclusive:
                self.results["R4_TERMINAL_STATE"] = _result(
                    "R4_TERMINAL_STATE", Status.INCONCLUSIVE,
                    "terminal state not reached, but the run was incomplete: " + "; ".join(self.inconclusive), ev)
            else:
                self.results["R4_TERMINAL_STATE"] = _result(
                    "R4_TERMINAL_STATE", Status.FAIL, "; ".join(problems), ev, final["seq"])

    def _check_compatibility(self) -> None:
        app = [a for a in self.attempts if a["role"] in ("v1", "v2")]
        bad = [a for a in app if a["status"] == "failed" and a["registered"]
               and (a["error"] or {}).get("sqlstate") in SCHEMA_SQLSTATES]
        if not app:
            self.results["C1_COMPATIBILITY"] = _result("C1_COMPATIBILITY", Status.NOT_APPLICABLE,
                                                       "no application requests")
        elif bad:
            ev = [{"attempt_id": a["attempt_id"], "actor": a["actor"], "role": a["role"], "op": a["op"],
                   "error": a["error"], "seq": a["complete_seq"]} for a in bad]
            self.results["C1_COMPATIBILITY"] = _result(
                "C1_COMPATIBILITY", Status.FAIL,
                f"{len(bad)} request(s) from live instances failed with schema errors", ev, ev[0]["seq"])
        else:
            self.results["C1_COMPATIBILITY"] = _result(
                "C1_COMPATIBILITY", Status.PASS, f"{len(app)} application request(s), none broken by schema changes")


def verdict(results: list[dict], infra_error: str | None, inconclusive: list[str]) -> str:
    if infra_error:
        return Status.INFRA_ERROR
    statuses = [r["status"] for r in results]
    if Status.FAIL in statuses:
        return Status.FAIL
    if inconclusive or Status.INCONCLUSIVE in statuses or Status.INFRA_ERROR in statuses:
        return Status.INCONCLUSIVE
    if not any(s == Status.PASS for s in statuses):
        return Status.INCONCLUSIVE  # nothing was actually verified
    return Status.PASS


def infra_results(message: str) -> list[dict]:
    return [_result(i, Status.INFRA_ERROR, f"not evaluated: {message}") for i in INVARIANT_IDS]
