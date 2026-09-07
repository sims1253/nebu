"""Extract PDF data or compare a saved extraction with reference data."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from janus.comparison.reference import ReferenceSource, normalize_reference
from janus.extraction import (
    CheckRule,
    ExtractionSpecification,
    StructuredExtraction,
    compare,
    extract,
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="janus")
    commands = parser.add_subparsers(dest="command", required=True)
    extraction = commands.add_parser("extract", help="Extract structured data from a PDF")
    extraction.add_argument("document", type=Path)
    extraction.add_argument("specification", type=Path)
    extraction.add_argument(
        "--data-only", action="store_true", help="Emit only data; exit 2 if any field is unresolved"
    )
    comparison = commands.add_parser(
        "compare", help="Compare saved extraction JSON with reference data"
    )
    comparison.add_argument("extraction", type=Path)
    comparison.add_argument("reference", type=Path)
    comparison.add_argument("rules", type=Path)
    schema = commands.add_parser("schema", help="Print the extraction specification JSON schema")
    schema.set_defaults(command="schema")
    args = parser.parse_args()
    try:
        if args.command == "schema":
            output = ExtractionSpecification.model_json_schema()
            code = 0
        elif args.command == "extract":
            spec = ExtractionSpecification.model_validate_json(args.specification.read_bytes())
            result = extract(args.document, spec)
            output = result.data if args.data_only else result.model_dump(mode="json")
            code = 2 if any(f.status != "extracted" for f in result.fields) else 0
        else:
            result = StructuredExtraction.model_validate_json(args.extraction.read_bytes())
            reference, _ = normalize_reference(ReferenceSource.from_path(args.reference))
            rules = TypeAdapter(list[CheckRule]).validate_json(args.rules.read_bytes())
            checks = compare(result, reference, rules)
            output = [check.model_dump(mode="json") for check in checks]
            code = 2 if any(c.status != "match" for c in checks) else 0
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return code
    except (OSError, ValueError, ValidationError) as exc:
        print(f"janus: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
