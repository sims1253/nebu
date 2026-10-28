// What the AI judge read in each mock record. The mock screening service compares these with the
// reviewer's provisional decision; the queue never sees them.
import type { CriterionStatus, CriterionVerdict } from "../schema.ts";

const KEYS = {
  P1: "C_POP_ADV_BTC",
  P2: "C_POP_PRETREATED",
  I1: "C_INT_ARMS",
  S1: "C_DES_RCT",
  X1: "C_PUB_PRIMARY",
} as const;
type Key = keyof typeof KEYS;
type Reading = readonly [status: CriterionStatus, quote: string, rationale: string];

const met = (quote: string, rationale: string): Reading => ["met", quote, rationale];
const notMet = (quote: string, rationale: string): Reading => ["not_met", quote, rationale];
const unclear = (quote: string, rationale: string): Reading => ["unclear", quote, rationale];

const READINGS: Record<string, Partial<Record<Key, Reading>>> = {
  "32416072": {
    P1: met("advanced, IDH1-mutant cholangiocarcinoma", "Advanced cholangiocarcinoma."),
    P2: met("who had progressed on previous therapy", "Progressed after prior therapy."),
    I1: met("oral ivosidenib 500 mg or matched placebo", "Ivosidenib is the intervention."),
    S1: met("randomised, double-blind, placebo-controlled, phase 3 study", "Randomised."),
  },
  "33798493": {
    P1: met("locally advanced or metastatic biliary tract cancer", "Advanced biliary tract cancer."),
    P2: met(
      "documented radiological disease progression to first-line cisplatin and gemcitabine",
      "Progressed on first-line chemotherapy.",
    ),
    I1: met("active symptom control (ASC) and FOLFOX or ASC alone", "FOLFOX and active symptom control."),
    S1: met("randomly assigned (1:1) centrally", "Randomised 1:1."),
  },
  "20375404": {
    P1: met("locally advanced or metastatic cholangiocarcinoma", "Advanced biliary tract cancer."),
    P2: unclear(
      "We randomly assigned 410 patients with locally advanced or metastatic cholangiocarcinoma, gallbladder cancer, or ampullary cancer",
      "The abstract does not say whether patients had prior systemic therapy, so this criterion cannot be ruled out here.",
    ),
    S1: met("We randomly assigned 410 patients", "Randomised."),
  },
  "34656226": {
    P1: met("metastatic biliary tract cancer", "Metastatic biliary tract cancer."),
    P2: met("progressed on first-line gemcitabine plus cisplatin", "Progressed on first-line therapy."),
    I1: met("leucovorin and fluorouracil only every 2 weeks", "Fluorouracil and leucovorin control arm."),
    S1: met("randomly assigned (1:1) using permuted blocks", "Randomised."),
  },
  "37075781": {
    P2: notMet(
      "had previously untreated, unresectable, locally advanced or metastatic biliary tract cancer",
      "Participants were previously untreated, and the criterion asks for progression after prior therapy.",
    ),
  },
  "36652354": {
    S1: notMet("open-label, single-group, phase 2 study", "A single-group study has no randomised comparison."),
  },
  "30922733": {
    P1: notMet(
      "had undergone a macroscopically complete resection",
      "Patients had resected disease and received adjuvant treatment, not advanced disease.",
    ),
  },
  "26615328": {
    P1: notMet("metastatic pancreatic ductal adenocarcinoma", "Pancreatic cancer, not biliary tract cancer."),
  },
  "40910862": {
    X1: notMet(
      "A systematic review and meta-analysis, registered with PROSPERO",
      "A review; its trials are screened as their own records.",
    ),
  },
  "29955136": {
    P2: met("ABTC patients progressed on GP", "Progressed on gemcitabine and cisplatin."),
    I1: unclear(
      "irinotecan 180 mg/m2 on day 1 plus capecitabine 1000 mg/m2 twice daily",
      "Capecitabine is an oral fluoropyrimidine; whether it counts as fluorouracil-based chemotherapy is for the protocol to say.",
    ),
    S1: met("Sixty-four GP refractory ABTC patients were randomised", "Randomised."),
  },
  "32464280": {
    P1: met("nonresectable/metastatic biliary tract cancer", "Advanced biliary tract cancer."),
    P2: met("progressed after gemcitabine/platinum chemotherapy", "Progressed after first-line therapy."),
    I1: met("best supportive care plus either regorafenib", "Best supportive care in both arms."),
    S1: met("double-blind, placebo-controlled, randomized phase II study", "Randomised."),
  },
  "34554208": {
    P1: met("unresectable or metastatic cholangiocarcinoma with IDH1 mutation", "Advanced cholangiocarcinoma."),
    P2: met("whose disease progressed with prior therapy", "Progressed after prior therapy."),
    I1: met("ivosidenib, 500 mg, once daily or matched placebo", "Ivosidenib is the intervention."),
    S1: met("randomized, double-blind, placebo-controlled, clinical phase 3 trial", "Randomised."),
  },
  "37901200": {
    P1: notMet("Adults with sorafenib-treated advanced HCC", "Hepatocellular carcinoma, not biliary tract cancer."),
  },
  "41264278": {
    S1: unclear(
      "Final Results From HERIZON-BTC-01",
      "The record has no abstract, and the design cannot be read from the title.",
    ),
  },
  "36951834": {
    P1: met("Patients with advanced BTC", "Advanced biliary tract cancer."),
    P2: met(
      "whose disease progressed while receiving first-line gemcitabine plus cisplatin",
      "Progressed on first-line therapy.",
    ),
    S1: met("randomized, multicenter, open-label, phase 2b clinical trial", "Randomised."),
  },
  "38870977": {
    P1: met("metastatic biliary tract cancer", "Metastatic biliary tract cancer."),
    P2: met("progression on gemcitabine-based therapy", "Progressed on first-line therapy."),
    I1: met("fluorouracil (2400 mg/m2) plus leucovorin (400 mg/m2) every 2 weeks (control group)", "Fluorouracil control arm."),
    S1: met("multicentre, open-label, randomised, phase 2 trial", "Randomised."),
  },
  "34973222": {
    X1: notMet("Author's reply", "A letter replying to correspondence, not a trial report."),
  },
  "38838500": {
    S1: notMet("open-label, single-arm, phase II FIGHT-202 study", "A single-arm study has no randomised comparison."),
  },
  "37751561": {
    S1: notMet("open-label phase II basket study", "A basket study without randomisation."),
  },
  "39438001": {
    X1: notMet(
      "We performed individual patient-level meta-analysis",
      "A meta-analysis; its trials are screened as their own records.",
    ),
  },
  "35510484": {
    X1: notMet(
      "Here we describe the rationale and design of the phase II HERB trial",
      "A trial protocol without results.",
    ),
  },
  "41349554": {
    S1: notMet(
      "Patients were assigned to one of four cohorts on the basis of FGFR status",
      "Cohorts by biomarker, without randomisation.",
    ),
  },
  "40631456": {
    S1: notMet("open-label, single-arm, phase II trial", "A single-arm study has no randomised comparison."),
  },
  "38319896": {
    P2: notMet(
      "previously untreated unresectable or metastatic biliary tract cancer",
      "Participants were previously untreated, and the criterion asks for progression after prior therapy.",
    ),
  },
  "41646284": {
    S1: met("phase III ClarIDHy study of ivosidenib versus placebo", "A secondary analysis of a randomised trial."),
  },
  "38353044": {
    X1: notMet(
      "A systematic review and meta-analysis were performed",
      "A review; its trials are screened as their own records.",
    ),
  },
  "38278871": {
    S1: met("randomized at a 2:1 ratio to receive ivosidenib or matched placebo", "Randomised."),
  },
  "40147791": {
    X1: notMet(
      "Individual patient data were pooled from the intention-to-treat populations of the NIFTY and NALIRICC trials",
      "A pooled analysis of two trials that are screened as their own records.",
    ),
  },
};

/** The judge's verdict on every criterion of one record; unread criteria are `unclear`. */
export const verdictsFor = (pmid: string, criterionIds: ReadonlyArray<string>): CriterionVerdict[] => {
  const readings = READINGS[pmid] ?? {};
  const byId = new Map<string, Reading>(
    (Object.entries(readings) as Array<[Key, Reading]>).map(([key, reading]) => [KEYS[key], reading]),
  );
  return criterionIds.map((criterion_id) => {
    const reading = byId.get(criterion_id);
    return {
      criterion_id,
      status: reading?.[0] ?? "unclear",
      quote: reading?.[1] ?? null,
      rationale: reading?.[2] ?? "The record does not say enough to decide this criterion.",
      quote_verified: reading !== undefined,
      downgraded_from: null,
    };
  });
};
