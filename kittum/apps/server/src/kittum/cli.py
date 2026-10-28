"""Command-line entry point: `kittum <command>`."""

import argparse
import json
import sys
from pathlib import Path


def _verify(bundle: Path, public_key: Path | None) -> int:
    """Check a handover bundle offline. Exit 0 if it verifies, 1 if not."""
    from kittum.ledger import VerifyKey, verify_bundle

    trusted = VerifyKey.from_pem(public_key.read_bytes()) if public_key else None
    report = verify_bundle(bundle, trusted_key=trusted)
    for problem in report.problems:
        print(f"FAIL {problem}")
    if report.key_id:
        pinned = "trusted" if report.key_pinned else "NOT PINNED, compare it with the sender's"
        print(f"key {report.key_id} ({pinned})")
    if report.ok:
        print(f"OK {bundle}: ledger, checkpoint, and files verify through seq {report.cutoff_seq}")
        return 0
    print(f"FAILED {bundle}: {len(report.problems)} problem(s)")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kittum")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("openapi", help="print the OpenAPI document as JSON")
    serve = commands.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8100)
    verify = commands.add_parser("verify", help="check a handover bundle offline")
    verify.add_argument("bundle", type=Path, help="the sealed bundle directory")
    verify.add_argument(
        "--public-key",
        type=Path,
        help="the sender's Ed25519 public key (PEM); without it the bundle's own key is used",
    )
    args = parser.parse_args(argv)

    if args.command == "verify":
        return _verify(args.bundle, args.public_key)
    if args.command == "openapi":
        from kittum.api import create_app

        json.dump(create_app().openapi(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if args.command == "serve":
        import uvicorn

        from kittum.api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
