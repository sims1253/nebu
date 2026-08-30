// Generates src/comparison-specification.generated.ts from the canonical
// JSON Schema, so the TypeScript contract cannot drift from the published
// schema. Every property, required flag, enum, and nested shape is derived
// by walking the schema. Only the conditional rules (if/then) have no
// TS representation; the server enforces those against the same schema
// file when it compiles a specification.
//
// Run from packages/contracts via `bun run contracts:generate`. Freshness
// is checked by `bun run contracts:check` (part of typecheck).
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const schemaPath = `${root}/schema/comparison-specification.v1.json`;
const outputPath = `${root}/src/comparison-specification.generated.ts`;
const schema = JSON.parse(readFileSync(schemaPath, "utf8"));
const defs = schema.$defs;

const union = (values) => values.map((item) => JSON.stringify(item)).join(" | ");

// Named unions for the schema's enum-valued properties. The type names are
// the generator's public vocabulary; the values always come from the schema.
const NAMED_ENUMS = [
  ["SpecificationValueType", defs.field.properties.value.properties.type.enum],
  ["SpecificationCompoundShape", defs.field.properties.value.properties.source_shape.enum],
  ["SpecificationNormalizer", defs.comparison.properties.normalizers.items.enum],
  ["SpecificationOperator", defs.comparison.properties.operator.enum],
  ["SpecificationSeverity", defs.comparison.properties.severity.enum],
];
const namedEnumFor = (values) =>
  NAMED_ENUMS.find(([, named]) => JSON.stringify(named) === JSON.stringify(values))?.[0];

const defName = (key) => defs[key].title ?? key;

function tsType(node, indent) {
  if (node.$ref) {
    const key = node.$ref.split("/").pop();
    // Object defs with a title become named interfaces; scalars inline.
    return defs[key].title ? defName(key) : tsType(defs[key], indent);
  }
  if (node.const !== undefined) return JSON.stringify(node.const);
  if (node.enum) {
    const named = namedEnumFor(node.enum);
    return named ?? union(node.enum);
  }
  switch (node.type) {
    case "array":
      return `${tsType(node.items, indent)}[]`;
    case "string":
      return "string";
    case "integer":
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "object":
      if (!node.properties || Object.keys(node.properties).length === 0) {
        return "Record<string, unknown>";
      }
      return renderObject(node, indent);
    default:
      return "unknown";
  }
}

function renderObject(node, indent) {
  const inner = " ".repeat(indent + 2);
  const closing = " ".repeat(indent);
  const required = new Set(node.required ?? []);
  const lines = Object.entries(node.properties).map(
    ([name, property]) =>
      `${inner}${name}${required.has(name) ? "" : "?"}: ${tsType(property, indent + 2)};`,
  );
  return `{\n${lines.join("\n")}\n${closing}}`;
}

const chunks = [
  `/**
 * Generated from schema/comparison-specification.v1.json. Do not edit.
 * Regenerate with \`bun run contracts:generate\`.
 *
 * Conditional constraints (value.component per source_shape, tolerances only
 * with the numeric operator) are enforced against the same schema by the
 * server and are not expressible in these static types.
 */`,
  ...NAMED_ENUMS.map(([name, values]) => `export type ${name} = ${union(values)};`),
  `export interface ComparisonSpecificationV1 ${renderObject(
    { required: schema.required, properties: schema.properties },
    0,
  )}`,
  ...Object.entries(defs)
    .filter(([, node]) => node.type === "object" && node.title)
    .map(([key, node]) => `export interface ${defName(key)} ${renderObject(node, 0)}`),
  "",
];

const output = chunks.join("\n\n");

if (process.argv.includes("--check")) {
  const current = readFileSync(outputPath, "utf8");
  if (current !== output) {
    process.stderr.write(
      "Generated comparison specification types are stale; run `bun run contracts:generate`.\n",
    );
    process.exit(1);
  }
} else {
  writeFileSync(outputPath, output);
}
