// Typed client for the Gauntlet FastAPI backend. Every value shown in the UI
// comes from these endpoints; nothing is computed or invented client-side
// beyond presentation (counts, grouping).

export type Status = "pass" | "fail" | "inconclusive" | "not_applicable" | "infra_error";
export type Verdict = "pass" | "fail" | "inconclusive" | "infra_error";

export interface InvariantResult {
  id: string;
  category: string;
  title: string;
  status: Status;
  summary: string;
  evidence: unknown[];
  trigger_seq: number | null;
}

export interface TraceEvent {
  seq: number;
  directive: number;
  kind: string;
  actor: string | null;
  t_ms: number;
  [k: string]: unknown;
}

export interface Attempt {
  attempt_id: string;
  logical_id: string;
  actor: string;
  role: string;
  op: string;
  args: Record<string, unknown>;
  registered: boolean;
  invoke_seq: number;
  complete_seq: number | null;
  status: "ok" | "failed" | "uncertain" | "open";
  reason: string | null;
  result: unknown;
  error: { code?: string; sqlstate?: string; message?: string; kind?: string } | null;
}

export interface Snapshot {
  label: string;
  seq: number;
  columns: string[];
  not_null: Record<string, boolean>;
  triggers: string[];
  rows: { id: number; total?: string | null; total_cents?: number | null }[];
  impl_reported_stage: string | null;
}

export interface Metrics {
  events: number;
  statements: number;
  attempts: number;
  acknowledged: number;
  failed: number;
  uncertain: number;
  faults_injected: number;
  crashes: number;
  lock_waits: number;
  recoveries: number;
  snapshots: number;
  invariants: Record<string, number>;
}

export interface RunSummary {
  origin?: { kind: string; id?: string; of?: string } | null;
  schedule_source?: string;
  id: string;
  created_at: string;
  scenario_id: string;
  impl_id: string;
  impl_kind: string;
  seed: number;
  status: "queued" | "running" | "completed";
  verdict: Verdict | null;
  schedule_hash: string | null;
  trace_hash: string | null;
  outcome_hash: string | null;
  duration_ms: number | null;
  benchmark_id: string | null;
  metrics: Metrics | null;
  failed_invariants: string[];
  infra_error: string | null;
}

export interface RunDetail {
  origin?: { kind: string; id?: string; of?: string } | null;
  schedule_source?: string;
  id: string;
  status: string;
  created_at: string;
  benchmark_version: string;
  scenario_id: string;
  impl_id: string;
  impl_kind: string;
  strategy: string;
  seed: number;
  verdict: Verdict;
  schedule_hash: string;
  trace_hash: string | null;
  outcome_hash: string;
  duration_ms: number;
  environment: Record<string, string>;
  initial_state: Record<string, string>;
  attempts: Attempt[];
  snapshots: Snapshot[];
  invariants: InvariantResult[];
  infra_error: string | null;
  inconclusive: string[];
  notes: string[];
  metrics: Metrics;
}

export interface Trace {
  id: string;
  events: TraceEvent[];
  attempts: Attempt[];
  snapshots: Snapshot[];
  directives: Record<string, unknown>[];
}

export interface Scenario {
  id: string;
  title: string;
  purpose: string;
  preconditions: string[];
  faults: string[];
  expected_behavior: string;
  target_stage: string;
}

export interface Implementation {
  id: string;
  kind: "reference" | "mutant";
  strategy: string;
  title: string;
  description: string;
  base: string | null;
  mutation: string | null;
  intended_invariant: string | null;
  intended_scenarios: string[];
}

export interface InvariantDef {
  id: string;
  category: string;
  title: string;
  statement: string;
  procedure: string;
}

export interface Fault {
  id: string;
  title: string;
  preconditions: string;
  injection_point: string;
  expected_behavior: string;
  expected_invariants: string;
  recovery_obligation: string;
  directives: string[];
  scenarios: string[];
}

export interface Headline {
  benchmark_id: string;
  runs_completed: number;
  verdicts: Record<Verdict, number>;
  invariant_violations: number;
  mutants_total: number;
  mutants_detected: number;
  mutants_killed_as_intended: number;
  inconclusive_runs: number;
  infrastructure_failures: number;
  scenarios_executed: number;
  seeds: number[];
  reproducibility: { checked: number; reproduced: number };
  finished_at: string;
}

export interface Meta {
  benchmark_version: string;
  counts: Record<string, number>;
  latest_run: RunSummary | null;
  latest_benchmark: Headline | null;
  active_runs: number;
}

export interface MatrixCell {
  runs: number;
  fail: number;
  pass: number;
  inconclusive: number;
  infra_error: number;
  failed_invariants: Record<string, number>;
}

export interface MutantValidation {
  impl_id: string;
  intended_invariant: string;
  intended_scenarios: string[];
  detected: boolean;
  killed_as_intended: boolean;
  intended_detection: string;
  intended_detection_rate: number | null;
  runs_failing: number;
  runs_total: number;
  runs_failing_without_intended_invariant: number;
  other_invariants_failed: string[];
  infra_errors: number;
  // failed_tests lists actual failures only (since v0.3.0); infrastructure errors are listed separately.
  // Summaries written before v0.3.0 lack infra_error_tests/outcome; per-test statuses are authoritative.
  conventional: { counts: Record<string, number>; failed_tests: string[]; infra_error_tests?: string[];
                  outcome?: "passed" | "failed" | "inconclusive" } | null;
  passes_conventional_fails_adversarial: boolean;
  conventional_inconclusive_fails_adversarial?: boolean;
}

export interface BenchmarkSummary extends Omit<Headline, "reproducibility" | "finished_at"> {
  implementations: number;
  references: { impl_id: string; runs: number; passed: number; non_pass: unknown[]; conventional: Record<string, number> | null }[];
  mutants: MutantValidation[];
  matrix: Record<string, Record<string, MatrixCell>>;
  reproducibility: { checked: number; reproduced: number; details: Record<string, unknown>[] };
  conventional: Record<string, { counts: Record<string, number>; tests: { id: string; title: string; status: string; message: string }[] }>;
  conventional_tests: { id: string; title: string }[];
  environment: Record<string, string>;
}

export interface Benchmark {
  id: string;
  created_at: string;
  finished_at: string | null;
  status: string;
  config: Record<string, unknown>;
  summary: BenchmarkSummary | null;
}

export interface Comparison {
  a: Record<string, unknown>;
  b: Record<string, unknown>;
  same_scenario: boolean;
  same_implementation: boolean;
  same_benchmark_version: boolean;
  outcome_differences_total?: number;
  same_seed: boolean;
  same_schedule: boolean;
  same_trace: boolean;
  same_outcome: boolean;
  event_kinds: { a: Record<string, number>; b: Record<string, number> };
  first_divergence: { index: number; a: Record<string, unknown> | null; b: Record<string, unknown> | null } | null;
  invariants: { id: string; a: Status | null; b: Status | null; differs: boolean }[];
  outcome_differences: { attempt_id: string; op: string; actor: string; a: unknown; b: unknown }[];
  final_state_differences: { id: number; a: unknown; b: unknown }[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  } catch (e) {
    throw new ApiError(0, `Backend unreachable (${(e as Error).message}). Is \`python -m gauntlet serve\` running?`);
  }
  const text = await res.text();
  if (!res.ok) {
    let detail = text;
    try {
      const j = JSON.parse(text);
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* plain text */
    }
    throw new ApiError(res.status, `${res.status}: ${detail}`);
  }
  const ct = res.headers.get("content-type") ?? "";
  return (ct.includes("application/json") ? JSON.parse(text) : text) as T;
}

export const api = {
  health: () => request<{ ok: boolean; postgres: { server_version: string } | null; error?: string }>("/api/health"),
  meta: () => request<Meta>("/api/meta"),
  scenarios: () => request<Scenario[]>("/api/scenarios"),
  scenario: (id: string, seed: number) =>
    request<Scenario & { schedule_preview: { seed: number; schedule_hash: string; directives: Record<string, unknown>[]; initial_orders: Record<string, string> } }>(
      `/api/scenarios/${id}?seed=${seed}`),
  implementations: () => request<Implementation[]>("/api/implementations"),
  invariants: () => request<InvariantDef[]>("/api/invariants"),
  faults: () => request<Fault[]>("/api/faults"),
  runs: (limit = 300) => request<RunSummary[]>(`/api/runs?limit=${limit}`),
  run: (id: string) => request<RunDetail & { status: string }>(`/api/runs/${id}`),
  trace: (id: string) => request<Trace>(`/api/runs/${id}/trace`),
  report: (id: string) => request<string>(`/api/runs/${id}/report`),
  startRun: (body: { scenario_id: string; impl_id: string; seed: number }) =>
    request<{ id: string; status: string }>("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  compare: (a: string, b: string) => request<Comparison>(`/api/compare?a=${a}&b=${b}`),
  latestBenchmark: () => request<Benchmark>("/api/benchmarks/latest"),
  latestBenchmarkReport: () => request<string>("/api/benchmarks/latest/report"),
  benchmarks: () => request<{ id: string; status: string; finished_at: string | null }[]>("/api/benchmarks"),
  // -- analyses: differential / exploration / minimization / replay / experiment --------------------
  fixtures: () => request<Scenario[]>("/api/fixtures"),
  analyses: (kind: AnalysisKind, limit = 100) => request<AnalysisSummary[]>(`/api/analyses?kind=${kind}&limit=${limit}`),
  cancelAnalysis: (id: string) => request<{ id: string }>(`/api/analyses/${id}/cancel`, { method: "POST" }),
  differentialRules: () => request<{ normalization_rules: NormRule[]; classifications: Record<string, string> }>("/api/differential/rules"),
  startDifferential: (body: { impl_ids: string[]; scenario_id: string; seed: number; schedule_from_run?: string }) =>
    request<{ id: string }>("/api/differential", { method: "POST", body: JSON.stringify(body) }),
  differential: (id: string) => request<Analysis<DifferentialResult>>(`/api/differential/${id}`),
  replayDifferential: (id: string) => request<{ id: string }>(`/api/differential/${id}/replay`, { method: "POST" }),
  replayRun: (runId: string) => request<{ id: string }>(`/api/runs/${runId}/replay`, { method: "POST" }),
  replay: (id: string) => request<Analysis<ReplayResult>>(`/api/replays/${id}`),
  startExploration: (body: { scenario_id: string; impl_id: string; seed: number; limits: Record<string, number> }) =>
    request<{ id: string }>("/api/explorations", { method: "POST", body: JSON.stringify(body) }),
  exploration: (id: string) => request<Analysis<ExplorationResult>>(`/api/explorations/${id}`),
  exportSchedule: (id: string, n: number) => request<Record<string, unknown>>(`/api/explorations/${id}/schedules/${n}/export`),
  startMinimization: (body: { run_id: string; invariant?: string; budget: Record<string, number | string> }) =>
    request<{ id: string }>("/api/minimizations", { method: "POST", body: JSON.stringify(body) }),
  minimization: (id: string) => request<Analysis<MinimizationResult>>(`/api/minimizations/${id}`),
  createExperiment: (config: Record<string, unknown>) =>
    request<{ id: string }>("/api/experiments", { method: "POST", body: JSON.stringify({ config }) }),
  experiment: (id: string) => request<Analysis<{ reports?: { at: string; completeness: string; markdown_sha256: string }[] }>>(`/api/experiments/${id}`),
  reviseExperiment: (id: string, changes: Record<string, unknown>, reason: string | null) =>
    request<unknown>(`/api/experiments/${id}`, { method: "PUT", body: JSON.stringify({ changes, reason }) }),
  experimentReport: (id: string) => request<ExperimentReport>(`/api/experiments/${id}/report`, { method: "POST" }),
  experimentExport: (id: string) => request<Record<string, unknown>>(`/api/experiments/${id}/export`),
  verifyEvidence: (id: string) => request<EvidenceCheck>(`/api/experiments/${id}/verify`),
  isolation: () => request<IsolationCaps>("/api/isolation"),
  candidates: () => request<CandidateSummary[]>("/api/candidates"),
  startEvaluation: (body: { candidate_id: string; scenarios: string[]; seeds: number[]; isolation: { mode: string; trusted_environment: boolean };
    explore: { max_schedules: number; max_preemptions: number } | null; minimize_max_attempts: number | null }) =>
    request<{ id: string }>("/api/evaluations", { method: "POST", body: JSON.stringify(body) }),
  evaluation: (id: string) => request<Analysis<EvaluationResult>>(`/api/evaluations/${id}`),
};

export type AnalysisKind = "differential" | "exploration" | "minimization" | "experiment" | "replay" | "evaluation";

export interface CandidateSummary {
  candidate_id: string; impl_id: string; source_hash: string; contract_version: string; registered_at: string;
  source_dir_name: string; name: string; strategy: string; producer: Record<string, unknown> | null;
}

export interface IsolationCaps {
  platform: string; strict: { available: boolean; reason: string };
  process: { available: boolean; security_boundary: boolean; process_tree_control: string; resource_limits: string };
}

export interface EvaluationResult {
  blocked?: string; summary_markdown?: string;
  steps?: {
    benchmark?: { comparisons: { id: string; scenario_id: string; seed: number; classification: string; candidate_run: string }[] };
    exploration?: { id?: string };
    minimization?: { minimizations?: { id: string }[] };
    report?: { experiment_id?: string };
  };
}

export interface AnalysisSummary {
  id: string; kind: AnalysisKind; created_at: string; updated_at: string; status: string; config: Record<string, any>;
}

export interface Analysis<R> extends AnalysisSummary { result: R | null }

export interface NormRule { id: string; rule: string; why: string }

export interface NormalizedAttempt { op: string; actor: string; role: string; status: string; error_code: string | null; result: unknown }

export interface DiffPair {
  a: string; b: string; differs: boolean;
  attempt_differences: { attempt_id: string; a: NormalizedAttempt | null; b: NormalizedAttempt | null }[];
  attempt_differences_total: number;
  final_columns: { a: string[] | null; b: string[] | null; same: boolean };
  final_metadata?: { a: Record<string, unknown> | null; b: Record<string, unknown> | null; same: boolean };
  final_row_differences: { id: number; a: unknown; b: unknown }[];
  first_meaningful_divergence: null | {
    attempt_id: string; a: NormalizedAttempt | null; b: NormalizedAttempt | null;
    a_complete_seq: number | null; b_complete_seq: number | null;
    a_context: Record<string, unknown>[]; b_context: Record<string, unknown>[];
  };
  first_statement_level_divergence_index: number | null;
}

export interface DiffRun {
  id: string; impl_id: string; impl_kind: string; strategy: string; verdict: string; schedule_hash: string;
  trace_hash: string | null; outcome_hash: string; duration_ms: number; database: string | null; error_class: string | null;
  infra_error: string | null; failed_invariants: string[];
  provenance: { schedule_id: string; initial_state_fingerprint: string; oracle_fingerprint: string;
    implementation: { impl_id: string; class: string; source_sha256: string }; git: { head: string | null; backend_modified: boolean | null } } | null;
}

export interface DifferentialResult {
  classification: "agreement" | "confirmed_violation" | "unconfirmed_disagreement" | "inconclusive";
  reason: string; shared_violations: Record<string, string[]> | null;
  oracle: Record<string, { run_id: string; verdict: string; outcome: string; failed_invariants: string[]; inconclusive_invariants: string[] }>;
  runs: DiffRun[]; pairs: DiffPair[];
  equivalence: { same_schedule_hash: boolean; same_initial_state_fingerprint: boolean; isolated_databases: boolean; databases: Record<string, string> };
  normalization_rules: NormRule[];
  replay_check?: { reproduced: boolean; replay_of: string };
}

export interface ReplayResult {
  original_id?: string; replay_id?: string; reproduced: boolean; same?: Record<string, boolean>; outcome?: string;
  per_implementation?: Record<string, Record<string, boolean>>; same_classification?: boolean; replay_of?: string;
  stop_reason?: string;
}

export interface ExploredSchedule {
  n: number; run_id: string; prefix_len: number; choices: string[]; steps: number; preemptions: number; outcome: string;
  verdict: string; retries: number; failed_invariants: string[]; duration_ms: number; infra_error: string | null;
  schedule_id: string; failure_class?: string; note?: string;
}

export interface FailureClass {
  id: string; failed_invariants: string[]; diagnosis_categories: string[]; count: number; schedule_numbers: number[];
  representative: { n: number; run_id: string; schedule_id: string; choices: string[] };
  replay?: { run_id?: string; reproduced: boolean | null; same_trace?: boolean; same_outcome?: boolean; same_choices?: boolean;
    skipped?: boolean; reason?: string | null; note?: string };
  flaky?: boolean;
}

export interface ExplorationResult {
  status: string; stop_reason: string | null; schedules_executed: number; outcomes: Record<string, number>;
  unexplored_known: number; unexplored_note: string | null; exhaustive_within_bounds: boolean; exhaustive_claim: string | null;
  counters: Record<string, number>; schedules: ExploredSchedule[]; failure_classes: FailureClass[];
  unique_failure_classes: number; grouping: string; elapsed_s: number;
}

export interface MinUnit { id: string; kind: string; desc: string }

export interface MinimizationResult {
  status: string; stop_reason: string | null; budget_exhausted?: boolean; one_minimal?: boolean; minimality: string | null;
  units_total: number; units_kept: number; attempts_used: number; elapsed_s: number; original_unchanged?: boolean;
  attempts: { n: number; phase: string; kept_units: number; removed: string[]; executed: boolean; result: string; reason: string;
    run_ids?: string[]; outcome?: string; failed_invariants?: string[] }[];
  retained_units: MinUnit[]; removed_units: MinUnit[];
  original: { run_id: string; size: Record<string, number | null>; signature: Signature };
  reproduction?: { run_id: string; outcome: string; match: boolean }[];
  minimized?: { schedule: Record<string, unknown>; schedule_id: string; run_id: string; size: Record<string, number | null>;
    signature: Signature | null; replay_note?: string;
    replay: { run_id: string; outcome: string; signature_match: boolean; same_trace_as_accepted: boolean; same_outcome_as_accepted: boolean } | null };
  reduction?: Record<string, [number, number]>;
  note?: string;
}

export interface Signature { invariant: string; status: string; category: string; diagnosis_categories: string[]; orders: number[] }

export interface Metric {
  id: string; name: string; numerator?: number; denominator?: number; value?: number | null; value_note?: string;
  unit_of_analysis?: string; inclusion?: string; treatment_of_non_conclusive?: string; interval_95?: number[] | null;
  interval_method?: string; small_sample?: string; note?: string; numerator_ids?: string[]; denominator_ids?: string[];
  counts?: Record<string, number>; ids?: Record<string, string[]>; excluded?: { run_id: string; reason: string }[];
  [k: string]: unknown;
}

export interface EvidenceCheck { ok: boolean; checked: number; missing: string[]; modified: string[] }

export interface ExperimentReport {
  evidence_check_against_previous_report?: EvidenceCheck;
  experiment_id: string; config_version: number; report_version: string; markdown: string; markdown_sha256: string;
  results: { metrics: Metric[]; paired: Record<string, string | number>[]; failure_examples: Record<string, unknown>[];
    completeness: { status: string; reason?: string; cells_required?: number; cells_conclusive?: number;
      missing_cells?: unknown[][]; unresolved_cells?: unknown[][] } };
  manifest: { artifacts: Record<string, unknown>[]; runs_selected: number; runs_referenced_by_metrics: number; runs_not_referenced: string[];
    code: Record<string, unknown> };
}
