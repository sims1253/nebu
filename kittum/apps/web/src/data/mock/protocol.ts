// The mock projects: one review with data, two that show the empty states.
import type { Criterion, ProjectSummary, ProtocolVersion, StageInfo } from "../schema.ts";

export const SCOPE_A = "POP-A";
/** The reviewer the mock answers for; `test/app.tsx` signs in as them. */
export const REVIEWER = "0192f0c4-5a10-7d1e-9c3a-00000000000a";
const LEAD = "0192f0c4-5a10-7d1e-9c3a-0000000000ff";

const member = (
  actor_id: string,
  display_name: string,
  roles: ProjectSummary["members"][0]["roles"],
) => ({
  actor_id,
  display_name,
  roles,
});

const stage = (projectId: string, kind: StageInfo["stage"] = "title_abstract"): StageInfo => ({
  stage_id: `${projectId}-${kind}`,
  project_id: projectId,
  scope_id: SCOPE_A,
  stage: kind,
  topic_id: "T2-indirect",
  mode: "M1",
  reveal: "on_disagreement",
  protocol_hash: "9f2c1e7a54b0d3c8e61f0a2b7d94c5e3a18f6b2d0c7e9a4f5b3d1c8e2a6f0b94",
  judge_config_id: "mock",
  screeners: [REVIEWER, "0192f0c4-5a10-7d1e-9c3a-00000000000b"],
  auditor_id: "0192f0c4-5a10-7d1e-9c3a-00000000000c",
  adjudicators: [LEAD],
});

const project = (
  id: string,
  name: string,
  title: string,
  stages: ReadonlyArray<StageInfo>,
): ProjectSummary => ({
  id,
  name,
  title,
  created_at: "2026-10-01T09:00:00Z",
  protocol: stages.length
    ? {
        version_id: `${id}-v1`,
        version_label: "1.2",
        content_hash: "9f2c1e7a54b0d3c8e61f0a2b7d94c5e3a18f6b2d0c7e9a4f5b3d1c8e2a6f0b94",
        profile: null,
      }
    : null,
  members: [
    member(LEAD, "Morgan Lee", ["lead", "adjudicator"]),
    member(REVIEWER, "Sam Okafor", ["reviewer"]),
  ],
  stages,
});

export const PROJECTS: ReadonlyArray<ProjectSummary> = [
  project(
    "ivosidenib-cca",
    "Second-line therapy review, version 2",
    "Ivosidenib for IDH1-mutant cholangiocarcinoma after prior therapy",
    [stage("ivosidenib-cca"), stage("ivosidenib-cca", "full_text")],
  ),
  project(
    "zanidatamab-btc",
    "HER2-targeted therapy review, version 1",
    "Zanidatamab for HER2-positive biliary tract cancer after prior therapy",
    [],
  ),
  project(
    "durvalumab-btc-jca",
    "First-line immunotherapy review, questions 1 to 3",
    "Durvalumab for biliary tract cancer, first line",
    [],
  ),
];

const both = ["title_abstract", "full_text"] as const;

export const CRITERIA: ReadonlyArray<Criterion> = [
  {
    id: "C_POP_ADV_BTC",
    slot: "population",
    text_en:
      "Adults with locally advanced or metastatic biliary tract cancer: cholangiocarcinoma, gallbladder or ampullary carcinoma",
    text_de:
      "Erwachsene mit lokal fortgeschrittenem oder metastasiertem biliärem Karzinom: Cholangiokarzinom, Gallenblasen- oder Ampullenkarzinom",
    decidable_at: both,
    applies_to: [SCOPE_A],
    polarity: "include",
    keywords: [],
    positive_examples: ["unresectable or metastatic cholangiocarcinoma"],
    negative_examples: ["resected biliary tract cancer", "hepatocellular carcinoma"],
  },
  {
    id: "C_POP_PRETREATED",
    slot: "population",
    text_en: "Disease progressed after at least one prior systemic therapy for advanced disease",
    text_de:
      "Progress nach mindestens einer systemischen Vortherapie der fortgeschrittenen Erkrankung",
    decidable_at: both,
    applies_to: [SCOPE_A],
    polarity: "include",
    keywords: [],
    positive_examples: ["progressed on first-line gemcitabine plus cisplatin"],
    negative_examples: ["previously untreated"],
  },
  {
    id: "C_INT_ARMS",
    slot: "intervention",
    text_en:
      "Ivosidenib, or a protocol comparator in at least one arm: FOLFOX, fluorouracil-based chemotherapy, or best supportive care",
    text_de:
      "Ivosidenib oder ein Vergleich des Protokolls in mindestens einem Arm: FOLFOX, Fluorouracil-basierte Chemotherapie oder Best Supportive Care",
    decidable_at: both,
    applies_to: [SCOPE_A],
    polarity: "include",
    keywords: [],
    positive_examples: ["active symptom control", "fluorouracil and leucovorin"],
    negative_examples: ["gemcitabine and cisplatin in both arms"],
  },
  {
    id: "C_DES_RCT",
    slot: "study_type",
    text_en: "Randomised controlled trial",
    text_de: "Randomisierte kontrollierte Studie",
    decidable_at: both,
    applies_to: [SCOPE_A],
    polarity: "include",
    keywords: [],
    positive_examples: ["randomly assigned (1:1)"],
    negative_examples: ["single-arm", "retrospective cohort"],
  },
  {
    id: "C_PUB_PRIMARY",
    slot: "other",
    text_en:
      "Report or secondary analysis of a trial; not a review, meta-analysis, pooled analysis, protocol, or letter",
    text_de:
      "Bericht oder Sekundäranalyse einer Studie; keine Übersicht, Metaanalyse, gepoolte Analyse, kein Protokoll oder Leserbrief",
    decidable_at: both,
    applies_to: [SCOPE_A],
    polarity: "include",
    keywords: [],
    positive_examples: [],
    negative_examples: ["systematic review and meta-analysis"],
  },
  {
    id: "C_OUT_RELEVANT",
    slot: "outcome",
    text_en:
      "Reports a patient-relevant endpoint: overall survival, symptoms, health-related quality of life, or adverse events",
    text_de:
      "Berichtet einen patientenrelevanten Endpunkt: Gesamtüberleben, Symptomatik, gesundheitsbezogene Lebensqualität oder unerwünschte Ereignisse",
    decidable_at: ["full_text"],
    applies_to: [SCOPE_A],
    polarity: "include",
    keywords: [],
    positive_examples: ["overall survival"],
    negative_examples: ["pharmacokinetics only"],
  },
];

export const PROTOCOL: ProtocolVersion = {
  version_id: "ivosidenib-cca-v1",
  protocol: {
    title: "Ivosidenib for IDH1-mutant cholangiocarcinoma after prior therapy",
    version_label: "1.2",
    questions: [
      {
        id: SCOPE_A,
        label: "A: adults with IDH1-mutant cholangiocarcinoma after at least one prior therapy",
        population: "Adults with IDH1-mutant cholangiocarcinoma after at least one prior therapy",
        intervention: "Ivosidenib",
        comparators: ["Best supportive care"],
      },
    ],
    outcomes: [{ id: "os", category: "primary", name: "Overall survival" }],
    topics: [
      {
        id: "T2-indirect",
        label: "RCTs for indirect comparisons",
        kind: "rct_indirect",
        mode: "M1",
        mode_rationale: null,
        scopes: [
          {
            id: SCOPE_A,
            label: "A: adults with IDH1-mutant cholangiocarcinoma after at least one prior therapy",
          },
        ],
        criteria: CRITERIA,
        sentinel_percent: "2",
        random_audit_percent: "10",
        sources: [{ id: "medline", kind: "bibliographic", name: "MEDLINE" }],
      },
    ],
  },
  content_hash: "9f2c1e7a54b0d3c8e61f0a2b7d94c5e3a18f6b2d0c7e9a4f5b3d1c8e2a6f0b94",
  published_at: "2026-10-01T09:00:00Z",
  // The neutral profile. A regulatory profile supplies its own words for the same concepts.
  profile: {
    id: null,
    labels: {
      scope: "Population",
      population: "Population",
      intervention: "Intervention",
      comparator: "Comparator",
      outcome: "Outcome",
      study_type: "Study design",
      other: "Publication",
      report: "Report",
      study: "Study",
      search_topic: "Search topic",
    },
  },
};
