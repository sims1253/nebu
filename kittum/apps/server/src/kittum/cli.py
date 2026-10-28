"""Command-line entry point: `kittum <command>`."""

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kittum")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("openapi", help="print the OpenAPI document as JSON")
    serve = commands.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)

    from kittum.api import create_app

    if args.command == "openapi":
        json.dump(create_app().openapi(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
