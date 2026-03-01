/* Shared TypeScript types for the Takeoff workspace. */

export type SnippetLabel =
  | "fixture_schedule"
  | "rcp"
  | "panel_schedule"
  | "plan_notes"
  | "detail"
  | "site_plan";

export interface Snippet {
  id: string;
  label: SnippetLabel;
  sub_label: string;
  page_number: number;
  bbox: { x: number; y: number; width: number; height: number };
  image_data?: string;
}

export type TakeoffMode = "fast" | "strict" | "liability";

export type AppState =
  | "empty"
  | "loaded"
  | "snipping"
  | "ready"
  | "running"
  | "complete";

export interface ReadinessStatus {
  ready: boolean;
  message: string;
  counts: Record<string, number>;
}

export interface FixtureRow {
  type_tag: string;
  description: string;
  total: number;
  revised?: number;
  delta?: number;
  difficulty: string;
  flags?: string[];
  counts_by_area?: Record<string, number>;
  notes?: string;
  accessories?: string[];
}

export interface AdversarialEntry {
  attack_id: string;
  severity: "critical" | "major" | "minor";
  category: string;
  description: string;
  resolution?: string;
  explanation?: string;
  verdict?: string;
}

export interface ConfidenceBreakdown {
  schedule_match?: number;
  area_coverage?: number;
  adversarial_resolved?: number;
  constitutional_clean?: number;
  cross_reference?: number;
  note_compliance?: number;
  reconciler_coverage?: number;
}

export interface Violation {
  rule: string;
  severity: string;
  explanation: string;
}

export type Verdict = "PASS" | "WARN" | "BLOCK";

export interface TakeoffResult {
  job_id?: string;
  drawing_name?: string;
  mode?: string;
  error?: string;
  judge_verdict: Verdict;
  grand_total: number;
  revised_total?: number;
  confidence_score: number;
  confidence_band: string;
  confidence_breakdown?: ConfidenceBreakdown;
  fixture_counts: FixtureRow[];
  areas_covered: string[];
  adversarial_log: AdversarialEntry[];
  constitutional_violations: Violation[];
  flags?: string[];
  ruling_summary?: string;
}

export interface MockPage {
  number: number;
  title: string;
}

export interface PipelineStep {
  id: string;
  label: string;
  detail?: string;
  status: "pending" | "running" | "done" | "error";
}
