// Stands in for the server's protocol-term and entity tagging (PubTator or local taggers). The
// terms come from the protocol, never from the AI judge, so they may show before the decision.
import { utf16ToCodePoint } from "../../lib/codepoints.ts";
import type { Highlight } from "../schema.ts";

const PROTOCOL_TERMS: Record<string, ReadonlyArray<string>> = {
  C_POP_ADV_BTC: [
    "biliary tract cancer",
    "biliary cancer",
    "biliary tumors",
    "cholangiocarcinoma",
    "gallbladder cancer",
    "gallbladder carcinoma",
    "ampullary cancer",
    "ampullary carcinoma",
    "BTC",
    "ABTC",
    "mBTC",
    "locally advanced",
    "metastatic",
    "unresectable",
    "nonresectable",
    "resected",
    "resection",
  ],
  C_POP_PRETREATED: [
    "previously treated",
    "previously untreated",
    "second-line",
    "first-line",
    "pretreated",
    "pre-treated",
    "refractory",
    "progressed on",
    "progressed after",
    "progression on",
    "progression to",
    "after failure",
    "prior therapy",
    "previous therapy",
  ],
  C_INT_ARMS: [
    "ivosidenib",
    "FOLFOX",
    "fluorouracil",
    "leucovorin",
    "folinic acid",
    "5-FU",
    "best supportive care",
    "active symptom control",
    "placebo",
  ],
  C_DES_RCT: [
    "randomised",
    "randomized",
    "randomly assigned",
    "randomisation",
    "randomization",
    "single-arm",
    "single-group",
    "single arm",
    "double-blind",
    "open-label",
    "placebo-controlled",
  ],
  C_PUB_PRIMARY: [
    "systematic review",
    "meta-analysis",
    "pooled analysis",
    "rationale and design",
    "Author's reply",
  ],
};

const ENTITIES: Record<string, ReadonlyArray<string>> = {
  chemical: [
    "cisplatin",
    "gemcitabine",
    "oxaliplatin",
    "irinotecan",
    "nal-IRI",
    "capecitabine",
    "pembrolizumab",
    "durvalumab",
    "futibatinib",
    "pemigatinib",
    "regorafenib",
    "zanidatamab",
    "tucatinib",
    "trastuzumab",
    "trastuzumab deruxtecan",
    "sunitinib",
    "tinengotinib",
    "sorafenib",
  ],
  disease: [
    "hepatocellular carcinoma",
    "HCC",
    "pancreatic ductal adenocarcinoma",
    "pancreatic cancer",
    "intrahepatic cholangiocarcinoma",
    "ICC",
    "CCA",
  ],
  gene: ["IDH1", "mIDH1", "FGFR2", "FGFR", "HER2"],
};

const escape = (term: string) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const pattern = (term: string) =>
  // Acronyms match case-sensitively; words match in any case.
  new RegExp(`(?<![\\w-])${escape(term)}(?![\\w])`, term === term.toUpperCase() ? "g" : "gi");

const RULES: ReadonlyArray<readonly [RegExp, string]> = [
  ...Object.entries(PROTOCOL_TERMS).flatMap(([criterion, terms]) =>
    terms.map((term) => [pattern(term), `protocol:${criterion}`] as const),
  ),
  ...Object.entries(ENTITIES).flatMap(([type, terms]) =>
    terms.map((term) => [pattern(term), `entity:${type}`] as const),
  ),
];

/** Every term match in `text`, as code-point ranges sorted by start. */
export const tag = (text: string): Highlight[] => {
  const found: Highlight[] = [];
  for (const [regex, label] of RULES) {
    for (const match of text.matchAll(regex)) {
      const start = utf16ToCodePoint(text, match.index);
      found.push([start, start + [...match[0]].length, label]);
    }
  }
  return found.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
};
