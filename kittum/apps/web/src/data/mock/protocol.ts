// The mock projects: one review with data, two that show the empty states.
import type { Criterion, ProjectSummary, ProtocolVersion } from "../schema.ts";

export const SCOPE_A = "POP-A";

export const PROJECTS: ReadonlyArray<ProjectSummary> = [
  {
    id: "ivosidenib-cca",
    active_substance: "Ivosidenib",
    indication: "IDH1-mutant cholangiocarcinoma after prior therapy",
    review: "Second-line therapy review, version 2",
    reveal: "on_disagreement",
    scope_id: SCOPE_A,
  },
  {
    id: "zanidatamab-btc",
    active_substance: "Zanidatamab",
    indication: "HER2-positive biliary tract cancer after prior therapy",
    review: "HER2-targeted therapy review, version 1",
    reveal: "on_disagreement",
    scope_id: null,
  },
  {
    id: "durvalumab-btc-jca",
    active_substance: "Durvalumab",
    indication: "Biliary tract cancer, first line",
    review: "First-line immunotherapy review, questions 1 to 3",
    reveal: "never",
    scope_id: null,
  },
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
    positive_examples: ["overall survival"],
    negative_examples: ["pharmacokinetics only"],
  },
];

export const PROTOCOL: ProtocolVersion = {
  protocol: {
    active_substance: "Ivosidenib",
    version_label: "1.2",
    topics: [
      {
        id: "T2-indirect",
        kind: "rct_indirect",
        mode: "M1",
        scopes: [
          {
            id: SCOPE_A,
            label: "A: adults with IDH1-mutant cholangiocarcinoma after at least one prior therapy",
          },
        ],
        criteria: CRITERIA,
      },
    ],
  },
  content_hash: "9f2c1e7a54b0d3c8e61f0a2b7d94c5e3a18f6b2d0c7e9a4f5b3d1c8e2a6f0b94",
  // The neutral profile. A regulatory profile supplies its own words for the same concepts.
  profile: {
    id: "generic",
    labels: {
      scope: "Population",
      population: "Population",
      intervention: "Intervention",
      comparator: "Comparator",
      outcome: "Outcome",
      study_type: "Study design",
      other: "Publication",
      subgroup: "Subgroup",
      report: "Report",
    },
  },
};

/** A project whose protocol is still a draft: no topics yet, and its own profile's words. */
export const draftProtocol = (project: ProjectSummary): ProtocolVersion => ({
  protocol: {
    active_substance: project.active_substance,
    version_label: "0.1 (draft)",
    topics: [],
  },
  content_hash: "0000000000000000000000000000000000000000000000000000000000000000",
  profile:
    project.id === "durvalumab-btc-jca"
      ? {
          id: "pico-sets",
          labels: {
            ...PROTOCOL.profile.labels,
            scope: "PICO question",
            report: "Assessment report",
          },
        }
      : PROTOCOL.profile,
});
