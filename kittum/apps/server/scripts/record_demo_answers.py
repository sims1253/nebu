"""Records a live model's answers for `kittum demo seed` to replay.

Seed a demo project with `kittum demo seed --live`, let the worker judge it, then run from
kittum/apps/server:

    uv run python scripts/record_demo_answers.py <project id>

It writes every completion of the project's verdicts, keyed by stage and record title, to
src/kittum/app/demo_data/judge_answers.json (merged with what is there), and prints how the
model answered: statuses, empty quotes, and what the judge's filter downgraded.
"""

import json
import sys
from collections import Counter
from pathlib import Path

from kittum.app import AppContext
from kittum.settings import Settings

OUT = Path(__file__).parents[1] / "src" / "kittum" / "app" / "demo_data" / "judge_answers.json"


def main(project_id: str) -> None:
    ctx = AppContext.from_settings(Settings.from_env())
    answers = json.loads(OUT.read_text()) if OUT.exists() else {}
    stats: Counter[str] = Counter()
    with ctx.db.transaction() as tx:
        rows = tx.execute(
            "select distinct on (v.item_ref, v.stage) v.stage, v.criteria, v.raw_output_hash,"
            " i.title from verdict v join screening_item i on i.item_ref = v.item_ref"
            " where v.project_id = %s order by v.item_ref, v.stage",
            (project_id,),
        ).fetchall()
    for row in rows:
        answers[f"{row['stage']}|{row['title']}"] = ctx.artifacts.get(row["raw_output_hash"]).decode()
        for c in row["criteria"]:
            stats[f"{c['criterion_id']} {c.get('downgraded_from') or c['status']}"] += 1
            if c.get("downgraded_from"):
                stats[f"downgraded: {c.get('failed_check')}"] += 1
            if not (c.get("quote") or ""):
                stats["empty quote"] += 1
    OUT.write_text(json.dumps(dict(sorted(answers.items())), indent=1, ensure_ascii=False) + "\n")
    print(f"{len(rows)} verdicts recorded to {OUT}")
    for key, n in sorted(stats.items()):
        print(f"  {key}: {n}")
    ctx.db.close()


if __name__ == "__main__":
    main(sys.argv[1])
