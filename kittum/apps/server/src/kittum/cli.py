"""Command-line entry point: `kittum <command>`."""

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kittum.app import AppContext


def _verify(bundle: Path, public_key: Path | None) -> int:
    """Check a handover bundle offline. Exit 0 if it verifies, 1 if not."""
    import kittum.report  # noqa: F401 - registers the checks of the report files
    from kittum.ledger import VerifyKey, verify_bundle

    trusted = VerifyKey.from_pem(public_key.read_bytes()) if public_key else None
    report = verify_bundle(bundle, trusted_key=trusted)
    for problem in report.problems:
        print(f"FAIL {problem}")
    for note in report.notes:
        print(f"NOTE {note}")
    if report.key_id:
        pinned = "trusted" if report.key_pinned else "NOT PINNED, compare it with the sender's"
        print(f"key {report.key_id} ({pinned})")
    if report.ok:
        print(f"OK {bundle}: ledger, checkpoint, and files verify through seq {report.cutoff_seq}")
        return 0
    print(f"FAILED {bundle}: {len(report.problems)} problem(s)")
    return 1


def _context(**overrides: object) -> AppContext:
    import atexit
    from dataclasses import replace

    from kittum.app import AppContext
    from kittum.profiles import load_profiles
    from kittum.settings import Settings

    load_profiles()
    ctx = AppContext.from_settings(replace(Settings.from_env(), **overrides))  # type: ignore[arg-type]
    atexit.register(ctx.db.close)
    return ctx


def _print(value: object) -> None:
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        print(value.model_dump_json(indent=2))
    else:
        print(json.dumps(value, indent=2, default=str))


def _worker(name: str | None, once: bool) -> int:
    import logging
    import os
    import socket

    from kittum.app import job_handlers
    from kittum.runs import PostgresJobQueue, Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ctx = _context()
    handlers = job_handlers(ctx)
    if not handlers:
        print("no job kinds can run: configure a judge (KITTUM_LLM_ENDPOINTS)", file=sys.stderr)
        return 1
    worker = Worker(
        ctx.db,
        PostgresJobQueue(ctx.clock),
        handlers,
        name=name or f"{socket.gethostname()}-{os.getpid()}",
    )
    judge = ctx.judge.config.id if ctx.judge else "none"
    print(f"worker {worker.name}: {', '.join(sorted(handlers))}; judge {judge}", flush=True)
    if once:
        ran = 0
        while worker.run_once():
            ran += 1
        print(f"ran {ran} job(s)")
        return 0
    worker.stop_on_signals()
    worker.run()
    return 0


def _demo_seed(args: argparse.Namespace) -> int:
    from kittum.app import run_judge_jobs, seed_demo

    ctx = _context(judge_backend="endpoint" if args.live else "demo")
    if ctx.judge is None:
        print("--live needs KITTUM_LLM_ENDPOINTS (and KITTUM_JUDGE_MODEL)", file=sys.stderr)
        return 1

    def say(line: str) -> None:
        if not args.json:
            print(line, flush=True)

    demo = seed_demo(ctx, name=args.name)
    say(f"project {demo.project_id}: {demo.records} records, judge {demo.judge_config_id}")
    for name, actor in demo.people.items():
        say(f"  {name}: {actor}")
    if args.no_wait:
        say("judge jobs queued; run `kittum worker` with the same judge settings")
    else:
        say("running the judge ...")
        ran = run_judge_jobs(ctx, progress=lambda n: say(f"  {n} batch(es) judged"))
        say(f"judged in {ran} batch(es); open the workbench and pick a reviewer")
    if args.json:
        print(demo.model_dump_json())
    return 0


def _project(args: argparse.Namespace) -> int:
    from kittum.core import KittumError

    try:
        return _project_step(args)
    except (KittumError, ValueError) as e:
        # A refused step (a rule of the protocol, the project's state): say why, without a trace.
        print(f"kittum project {args.action}: {e}", file=sys.stderr)
        return 1


def _project_step(args: argparse.Namespace) -> int:
    from uuid import UUID

    from kittum import app
    from kittum.search import SearchPlan, import_file

    ctx = _context()
    ledger = ctx.require_ledger()
    with ctx.db.transaction() as tx:
        match args.action:
            case "create":
                project_id, lead = app.create_project(
                    tx, name=args.name, lead_name=args.lead, ledger=ledger
                )
                _print({"project_id": project_id, "lead": lead})
            case "list":
                _print([p.model_dump(mode="json") for p in app.list_projects(tx, ctx.screening())])
            case "add-member":
                _print(
                    app.add_member(
                        tx,
                        UUID(args.project),
                        display_name=args.name,
                        roles=tuple(args.role),
                        added_by=UUID(args.by),
                        ledger=ledger,
                    )
                )
            case "publish-protocol":
                stored = app.publish_protocol(
                    tx,
                    UUID(args.project),
                    Path(args.file).read_text(),
                    approved_by=UUID(args.by),
                    ledger=ledger,
                    reason=args.reason,
                )
                _print({"version_id": stored.id, "content_hash": stored.version.content_hash})
            case "import-search":
                plan = SearchPlan(
                    topic_id=args.topic,
                    source=args.source,
                    platform=args.platform,
                    strategy=args.strategy,
                )
                execution = import_file(
                    tx,
                    UUID(args.project),
                    plan,
                    Path(args.file).read_bytes(),
                    args.format,
                    ledger=ledger,
                    actor_id=UUID(args.by),
                    artifacts=ctx.artifacts,
                    clock=ctx.clock,
                )
                _print({"execution_id": execution.id, "records": execution.retrieved_count})
            case "run-search":
                from kittum.search import CtgovSource, PubMedSource, run

                assert ctx.gateway is not None
                source = (
                    PubMedSource(
                        ctx.gateway,
                        ctx.artifacts,
                        contact_email=ctx.settings.contact_email,
                        ncbi_api_key=ctx.settings.ncbi_api_key,
                        clock=ctx.clock,
                    )
                    if args.source == "pubmed"
                    else CtgovSource(ctx.gateway, ctx.artifacts, clock=ctx.clock)
                )
                plan = SearchPlan(
                    topic_id=args.topic,
                    source=args.source,
                    platform="PubMed" if args.source == "pubmed" else "ClinicalTrials.gov",
                    strategy=args.strategy,
                )
                execution = run(
                    tx,
                    UUID(args.project),
                    plan,
                    source,
                    ledger=ledger,
                    actor_id=UUID(args.by),
                    artifacts=ctx.artifacts,
                )
                _print({"execution_id": execution.id, "records": execution.retrieved_count})
            case "ingest":
                revision = app.ingest_corpus(
                    tx, UUID(args.project), actor_id=UUID(args.by), ledger=ledger
                )
                # As the workbench does: `kittum worker` enriches the new corpus.
                assert ctx.queue is not None
                enrichment = app.request_enrichment(
                    tx,
                    UUID(args.project),
                    actor_id=UUID(args.by),
                    trigger="corpus_built",
                    ledger=ledger,
                    queue=ctx.queue,
                    clock=ctx.clock,
                )
                _print(
                    {
                        "identity_revision_id": revision.identity_revision_id,
                        "occurrences": len(revision.occurrences),
                        "proposals": len(revision.proposals),
                        "enrichment_run_id": enrichment.id,
                    }
                )
            case "enrich":
                assert ctx.queue is not None
                _print(
                    app.request_enrichment(
                        tx,
                        UUID(args.project),
                        actor_id=UUID(args.by),
                        trigger="requested",
                        ledger=ledger,
                        queue=ctx.queue,
                        clock=ctx.clock,
                    )
                )
            case "fetch-open-access":
                assert ctx.queue is not None
                _print(
                    app.request_open_access(
                        tx,
                        UUID(args.project),
                        ctx.screening(),
                        actor_id=UUID(args.by),
                        ledger=ledger,
                        queue=ctx.queue,
                        clock=ctx.clock,
                    )
                )
            case "attach-full-text":
                _print(
                    app.attach_full_text(
                        tx,
                        UUID(args.project),
                        args.item_ref,
                        Path(args.pdf).read_bytes(),
                        actor_id=UUID(args.by),
                        artifacts=ctx.artifacts,
                        ledger=ledger,
                        licence=args.licence,
                    )
                )
            case "open-stage":
                _print(
                    app.open_screening(
                        tx,
                        ctx,
                        UUID(args.project),
                        args.scope,
                        args.stage,
                        actor_id=UUID(args.by),
                        reveal=args.reveal,
                        sentinels=app.load_sentinels(args.sentinels) if args.sentinels else (),
                    )
                )
    return 0


def _verify_project(project: str) -> int:
    from uuid import UUID

    ctx = _context()
    with ctx.db.transaction() as tx:
        report = ctx.require_ledger().verify(tx, UUID(project))
    if report.ok:
        print(f"OK project {project}: chain verifies through seq {report.checked_through_seq}")
        return 0
    print(f"FAILED project {project}: seq {report.first_bad_seq} {report.problem}: {report.reason}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kittum")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("openapi", help="print the OpenAPI document as JSON")
    serve = commands.add_parser("serve", help="run the API server (and the built workbench)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8100)
    serve.add_argument(
        "--web-dist",
        type=Path,
        help="serve the built workbench from this directory (default: KITTUM_WEB_DIST)",
    )
    worker = commands.add_parser("worker", help="run background jobs (the AI judge, ...)")
    worker.add_argument("--name", help="the worker's name in job leases")
    worker.add_argument("--once", action="store_true", help="run due jobs, then exit")
    demo = commands.add_parser("demo", help="demo data")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    seed = demo_commands.add_parser("seed", help="build the demo project")
    seed.add_argument(
        "--live",
        action="store_true",
        help="judge with the model server in KITTUM_LLM_ENDPOINTS instead of recorded answers",
    )
    seed.add_argument(
        "--no-wait", action="store_true", help="queue the judge jobs for `kittum worker`"
    )
    seed.add_argument("--name", default="Nicotine replacement review (demo)")
    seed.add_argument("--json", action="store_true", help="print the project and people as JSON")
    project = commands.add_parser("project", help="project lifecycle steps")
    actions = project.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create")
    create.add_argument("name")
    create.add_argument("--lead", required=True, help="the methods lead's name")
    actions.add_parser("list")

    def step(name: str, *positional: str) -> argparse.ArgumentParser:
        sub = actions.add_parser(name)
        sub.add_argument("project")
        for arg in positional:
            sub.add_argument(arg)
        sub.add_argument("--by", required=True, help="the acting person's actor ID")
        return sub

    member = step("add-member", "name")
    member.add_argument(
        "--role",
        action="append",
        required=True,
        choices=["lead", "reviewer", "auditor", "adjudicator"],
    )
    publish = step("publish-protocol", "file")
    publish.add_argument("--reason")
    imported = step("import-search", "file")
    for flag in ("--topic", "--source", "--platform", "--strategy"):
        imported.add_argument(flag, required=True)
    imported.add_argument("--format", default="ris")
    live = step("run-search")
    live.add_argument("--source", choices=["pubmed", "ctgov"], required=True)
    live.add_argument("--topic", required=True)
    live.add_argument("--strategy", required=True)
    step("ingest")
    step("enrich")  # again; `ingest` queues the first enrichment
    step("fetch-open-access")
    attach = step("attach-full-text", "item_ref", "pdf")
    attach.add_argument("--licence")
    stage = step("open-stage", "scope", "stage")
    stage.add_argument("--reveal", default="on_disagreement")
    stage.add_argument(
        "--sentinels",
        type=Path,
        help="a YAML or JSON list of sentinels (records with known outcomes), as in "
        "examples/sentinels/; needed when the protocol's sentinel_percent is above zero",
    )
    ledger = commands.add_parser("ledger", help="the audit ledger")
    ledger_commands = ledger.add_subparsers(dest="ledger_command", required=True)
    ledger_verify = ledger_commands.add_parser("verify", help="verify a project's hash chain")
    ledger_verify.add_argument("project")
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
        import os

        import uvicorn

        from kittum.api import create_app

        if args.web_dist:
            os.environ["KITTUM_WEB_DIST"] = str(args.web_dist)
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    if args.command == "worker":
        return _worker(args.name, args.once)
    if args.command == "demo":
        return _demo_seed(args)
    if args.command == "project":
        return _project(args)
    if args.command == "ledger":
        return _verify_project(args.project)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
