// Extraction proposals for ABC-06 from both paths, anchored on the rendered fixture. The Janus
// path and the LLM path agree on most fields; the hazard ratio shows a real failure mode, where
// the LLM path picked the post-hoc sensitivity analysis instead of the primary one.
import type { FieldSpec, Policy, Proposal, ReviewField, SemanticKey } from "../schema.ts";
import { SCOPE_A } from "./protocol.ts";

export const STUDY_ID = "0192f0c4-5a10-7d1e-9c3a-abc06abc0601";
export const REPORT_ID = "0192f0c4-5a10-7d1e-9c3a-abc06abc0602";

type Path = "janus" | "llm";
type Read = readonly [value: string | null, unit: string | null, anchor: string | null];

interface FieldDef {
  readonly endpoint: string;
  readonly arm: string;
  readonly timepoint: string;
  readonly quantity: string;
  readonly unit: string | null;
  readonly policy: Policy;
  readonly description: string;
  readonly analysisSet?: string;
  readonly janus: Read | null;
  readonly llm: Read | null;
  readonly status?: Partial<Record<Path, Proposal["status"]>>;
  readonly failed?: Partial<Record<Path, string>>;
  readonly accepted?: Path;
}

const FOLFOX = "ASC plus FOLFOX";
const ASC = "ASC alone";
const BOTH = "FOLFOX versus ASC";
const TOTAL = "All randomised";
const FINAL = "Final analysis";

const DEFS: ReadonlyArray<FieldDef> = [
  {
    endpoint: "Participants",
    arm: FOLFOX,
    timepoint: "Randomisation",
    quantity: "n_randomized",
    unit: "patients",
    policy: "critical",
    description: "Patients randomised",
    janus: ["81", "patients", "n_folfox"],
    llm: ["81", "patients", "n_folfox"],
    accepted: "janus",
  },
  {
    endpoint: "Participants",
    arm: ASC,
    timepoint: "Randomisation",
    quantity: "n_randomized",
    unit: "patients",
    policy: "critical",
    description: "Patients randomised",
    janus: ["81", "patients", "n_asc"],
    llm: ["81", "patients", "n_asc"],
  },
  {
    endpoint: "Participants",
    arm: TOTAL,
    timepoint: "Randomisation",
    quantity: "n_randomized",
    unit: "patients",
    policy: "critical",
    description: "Patients randomised",
    janus: ["162", "patients", "n_total"],
    llm: ["162", "patients", "n_total"],
  },
  {
    endpoint: "Follow-up",
    arm: TOTAL,
    timepoint: "Data cut-off",
    quantity: "median_follow_up",
    unit: "months",
    policy: "standard",
    description: "Median follow-up",
    janus: null,
    llm: ["21.7", "months", "follow_up"],
  },
  {
    endpoint: "Overall survival",
    arm: FOLFOX,
    timepoint: FINAL,
    quantity: "median",
    unit: "months",
    policy: "critical",
    description: "Median overall survival",
    janus: ["6.2", "months", "os_median_folfox"],
    llm: ["6.2", "months", "os_median_folfox"],
  },
  {
    endpoint: "Overall survival",
    arm: FOLFOX,
    timepoint: FINAL,
    quantity: "ci_lower",
    unit: "months",
    policy: "critical",
    description: "95% CI, lower bound",
    janus: ["5.4", "months", "os_median_folfox"],
    llm: ["5.4", "months", "os_median_folfox"],
  },
  {
    endpoint: "Overall survival",
    arm: FOLFOX,
    timepoint: FINAL,
    quantity: "ci_upper",
    unit: "months",
    policy: "critical",
    description: "95% CI, upper bound",
    janus: ["7.6", "months", "os_median_folfox"],
    llm: ["7.6", "months", "os_median_folfox"],
  },
  {
    endpoint: "Overall survival",
    arm: ASC,
    timepoint: FINAL,
    quantity: "median",
    unit: "months",
    policy: "critical",
    description: "Median overall survival",
    janus: ["5.3", "months", "os_median_asc"],
    llm: ["5.3", "months", "os_median_asc"],
  },
  {
    endpoint: "Overall survival",
    arm: BOTH,
    timepoint: FINAL,
    quantity: "hazard_ratio",
    unit: null,
    policy: "critical",
    description: "Adjusted hazard ratio",
    janus: ["0.69", null, "os_hr"],
    llm: ["0.68", null, "os_hr_sensitivity"],
  },
  {
    endpoint: "Overall survival",
    arm: BOTH,
    timepoint: FINAL,
    quantity: "ci_lower",
    unit: null,
    policy: "critical",
    description: "95% CI, lower bound",
    janus: ["0.50", null, "os_hr"],
    llm: ["0.49", null, "os_hr_sensitivity"],
  },
  {
    endpoint: "Overall survival",
    arm: BOTH,
    timepoint: FINAL,
    quantity: "ci_upper",
    unit: null,
    policy: "critical",
    description: "95% CI, upper bound",
    janus: ["0.97", null, "os_hr"],
    llm: ["0.95", null, "os_hr_sensitivity"],
  },
  {
    endpoint: "Overall survival",
    arm: BOTH,
    timepoint: FINAL,
    quantity: "p_value",
    unit: null,
    policy: "critical",
    description: "p-value",
    janus: ["0.031", null, "os_hr"],
    llm: ["0.031", null, "os_hr"],
  },
  {
    endpoint: "Overall survival",
    arm: FOLFOX,
    timepoint: "6 months",
    quantity: "survival_rate",
    unit: "%",
    policy: "critical",
    description: "Overall survival rate",
    janus: ["50.6", "%", "os6_folfox"],
    llm: ["50.6", "%", "os6_folfox"],
  },
  {
    endpoint: "Overall survival",
    arm: ASC,
    timepoint: "6 months",
    quantity: "survival_rate",
    unit: "%",
    policy: "critical",
    description: "Overall survival rate",
    janus: ["35.5", "%", "os6_asc"],
    llm: ["35.5", "%", "os6_asc"],
  },
  {
    endpoint: "Overall survival",
    arm: FOLFOX,
    timepoint: "12 months",
    quantity: "survival_rate",
    unit: "%",
    policy: "critical",
    description: "Overall survival rate",
    janus: ["25.9", "%", "os12_folfox"],
    llm: ["25.9", "%", "os12_folfox"],
  },
  {
    endpoint: "Overall survival",
    arm: ASC,
    timepoint: "12 months",
    quantity: "survival_rate",
    unit: "%",
    policy: "critical",
    description: "Overall survival rate",
    janus: ["11.4", "%", "os12_asc"],
    llm: ["11.4", "months", "os12_asc"],
    status: { llm: "invalid" },
    failed: { llm: "The unit is months, but a survival rate is a percentage." },
  },
  {
    endpoint: "Adverse events, grade 3 to 5",
    arm: FOLFOX,
    timepoint: FINAL,
    quantity: "events",
    unit: "patients",
    policy: "critical",
    description: "Patients with an event",
    analysisSet: "Safety",
    janus: ["56", "patients", "ae35_folfox"],
    llm: ["56", "patients", "ae35_folfox"],
  },
  {
    endpoint: "Adverse events, grade 3 to 5",
    arm: ASC,
    timepoint: FINAL,
    quantity: "events",
    unit: "patients",
    policy: "critical",
    description: "Patients with an event",
    analysisSet: "Safety",
    janus: ["42", "patients", "ae35_asc"],
    llm: ["42", "patients", "ae35_asc"],
  },
  {
    endpoint: "Health-related quality of life",
    arm: BOTH,
    timepoint: FINAL,
    quantity: "qlq_c30_global",
    unit: "points",
    policy: "critical",
    description: "EORTC QLQ-C30 global health status",
    janus: [null, null, null],
    llm: [null, null, "qol_missing"],
    status: { janus: "missing", llm: "missing" },
  },
  // Standard fields both paths agree on, last in the list: a group accept takes them once seen.
  {
    endpoint: "Adverse events, any grade",
    arm: FOLFOX,
    timepoint: FINAL,
    quantity: "events",
    unit: "patients",
    policy: "standard",
    description: "Patients with any adverse event",
    janus: ["79", "patients", "ae35_folfox"],
    llm: ["79", "patients", "ae35_folfox"],
  },
  {
    endpoint: "Adverse events, any grade",
    arm: ASC,
    timepoint: FINAL,
    quantity: "events",
    unit: "patients",
    policy: "standard",
    description: "Patients with any adverse event",
    janus: ["78", "patients", "ae35_asc"],
    llm: ["78", "patients", "ae35_asc"],
  },
];

const VALIDATORS: Record<string, ReadonlyArray<string>> = {
  hazard_ratio: ["ci_brackets_estimate", "ratio_log_symmetric"],
  events: ["events_at_most_n", "percent_recomputes"],
  n_randomized: ["arms_sum_to_total"],
  survival_rate: ["unit_matches_field"],
};

export const extractionFields = (anchorId: (name: string) => string): ReviewField[] =>
  DEFS.map((def, index) => {
    const key: SemanticKey = {
      study_id: STUDY_ID,
      report_id: REPORT_ID,
      subpopulation: SCOPE_A,
      analysis_set: def.analysisSet ?? "Intention to treat",
      data_cut: "4 January 2019",
      arm: def.arm,
      endpoint: def.endpoint,
      timepoint: def.timepoint,
      quantity: def.quantity,
    };
    const spec: FieldSpec = {
      quantity: def.quantity,
      unit: def.unit,
      policy: def.policy,
      description: def.description,
    };
    const proposal = (path: Path, read: Read): Proposal => ({
      key,
      value: read[0],
      unit: read[1],
      status: def.status?.[path] ?? "extracted",
      path,
      anchor_ids: read[2] ? [anchorId(read[2])] : [],
      validators: (VALIDATORS[def.quantity] ?? []).map((validator) => {
        const message = validator === "unit_matches_field" ? def.failed?.[path] : undefined;
        return { validator, passed: message === undefined, message: message ?? null };
      }),
    });
    const proposals = [
      ...(def.janus ? [proposal("janus", def.janus)] : []),
      ...(def.llm ? [proposal("llm", def.llm)] : []),
    ];
    const [janus, llm] = [def.janus, def.llm];
    const agreement: ReviewField["agreement"] =
      janus && llm
        ? janus[0] === llm[0] && janus[1] === llm[1]
          ? "agree"
          : "disagree"
        : janus
          ? "janus_only"
          : llm
            ? "llm_only"
            : "neither";
    return {
      id: `field-${String(index + 1).padStart(2, "0")}`,
      spec,
      key,
      proposals,
      accepted: def.accepted ?? null,
      labels: { endpoint: def.endpoint, timepoint: def.timepoint, arm: def.arm },
      agreement,
      gate:
        def.policy === "critical"
          ? "human_verify"
          : def.policy === "standard" && agreement === "agree"
            ? "group_accept"
            : "human_review",
      second_extractor: def.policy === "critical" && agreement === "disagree",
      recheck: false,
      acceptance: null,
    };
  });
