"""The extraction tables in a handover bundle, as of the bundle's cutoff."""

import json
from typing import TYPE_CHECKING

from kittum.ledger import register_exporter
from kittum.models import is_event_stream

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Tx
    from kittum.ledger import BundleWriter


NOT_STORED = "not in the artifact store when the bundle was sealed"


class ExtractionExporter:
    """Runs requested, fields and proposals recorded, and acceptances made by `cutoff_seq`, with
    the anchors they point at and the artifacts they name: proposal sets, agent transcripts, and
    the request and response of every model call a transcript lists."""

    name = "extraction"

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:
        runs = tx.execute(
            "select id, layer_id, document_id, study_id, report_id, report, schema, schema_hash,"
            " agent, requested_by, requested_seq, created_at,"
            " case when recorded_seq <= %(cut)s then janus_version end as janus_version,"
            " case when recorded_seq <= %(cut)s then model_identity end as model_identity,"
            " case when recorded_seq <= %(cut)s then transcript_hash end as transcript_hash,"
            " case when recorded_seq <= %(cut)s then output_hash end as output_hash,"
            " case when recorded_seq <= %(cut)s then recorded_seq end as recorded_seq"
            " from extraction_run where project_id = %(project)s and requested_seq <= %(cut)s"
            " order by requested_seq",
            {"project": project_id, "cut": cutoff_seq},
        ).fetchall()
        fields = tx.execute(
            "select id, run_id, ordinal, key, key_hash, spec, kind, labels, agreement, gate,"
            " second_extractor, recheck, recorded_seq from extraction_field"
            " where project_id = %s and recorded_seq <= %s order by run_id, ordinal",
            (project_id, cutoff_seq),
        ).fetchall()
        proposals = tx.execute(
            "select p.id, p.field_id, p.run_id, p.path, p.value, p.unit, p.status, p.message,"
            " p.quote, p.anchor_ids, p.validators, p.recorded_seq"
            " from extraction_proposal p join extraction_field f on f.id = p.field_id"
            " where f.project_id = %s and p.recorded_seq <= %s order by p.field_id, p.path",
            (project_id, cutoff_seq),
        ).fetchall()
        acceptances = tx.execute(
            "select id, field_id, run_id, key_hash, path, edited, grouped, value, unit, anchor_id,"
            " reviewer_id, source_viewed_ms, recheck, accepted_at, event_seq"
            " from extraction_acceptance where project_id = %s and event_seq <= %s"
            " order by event_seq",
            (project_id, cutoff_seq),
        ).fetchall()
        anchor_ids = sorted(
            {a for p in proposals for a in p["anchor_ids"]} | {a["anchor_id"] for a in acceptances}
        )
        anchors = tx.execute(
            "select id, layer_id, document_id, quote, text_hash, start_offset, end_offset, boxes,"
            " cells from anchor where id = any(%s) order by id",
            (anchor_ids,),
        ).fetchall()
        for name, rows in (
            ("extraction_run", runs),
            ("extraction_field", fields),
            ("extraction_proposal", proposals),
            ("extraction_acceptance", acceptances),
            ("extraction_anchor", anchors),
        ):
            columns = list(rows[0]) if rows else ["id"]
            bundle.table(name, columns)
            bundle.insert(
                name,
                [
                    {**r, "anchor_ids": list(map(str, r["anchor_ids"]))} if "anchor_ids" in r else r
                    for r in rows
                ],
            )
        for run in runs:
            for column in ("transcript_hash", "output_hash"):
                if run[column]:
                    bundle.artifact(run[column], media_type="application/json")
            # The LLM path's calls: the exact request and response of each, as the transcript
            # names them. One the store lacks is listed by hash with the reason.
            data = bundle.read(run["transcript_hash"]) if run["transcript_hash"] else None
            for call in json.loads(data).get("calls", []) if data else []:
                for h, response in (
                    (call.get("raw_request_hash"), False),
                    (call.get("raw_response_hash"), True),
                ):
                    if not h:
                        continue
                    # A streamed response is the server's event stream, not one JSON body.
                    found = bundle.read(h) if response else None
                    kind = (
                        "text/event-stream"
                        if found and is_event_stream(found)
                        else "application/json"
                    )
                    try:
                        bundle.artifact(h, media_type=kind)
                    except ValueError:
                        bundle.artifact(h, media_type=kind, withheld_reason=NOT_STORED)


EXPORTER = register_exporter(ExtractionExporter())
