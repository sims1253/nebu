"""The one write path: receipt check, pure decider, events, rows, jobs, receipt."""

from datetime import UTC
from typing import TYPE_CHECKING

from kittum.core import Clock, SystemClock
from kittum.runs.model import CommandIdReused, Receipt

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.db import Tx
    from kittum.ledger import Ledger
    from kittum.runs.model import Apply, Command, Decider, JobQueue, ReceiptStore


class Commands:
    """Runs commands against one ledger, queue, and receipt store.

        receipt = commands.execute(tx, command, decide, load=load_state, apply=write_rows)

    Everything happens in the caller's `tx`, so the events, the module's rows, the jobs, and the
    receipt commit together or not at all. A command whose receipt exists returns that receipt
    and writes nothing, so clients and workers may retry freely with the same `command_id`.
    """

    def __init__(
        self,
        ledger: Ledger,
        queue: JobQueue,
        receipts: ReceiptStore,
        clock: Clock | None = None,
    ) -> None:
        self._ledger = ledger
        self._queue = queue
        self._receipts = receipts
        self._clock = clock or SystemClock()

    def execute[S, T](
        self,
        tx: Tx,
        command: Command,
        decide: Decider[S, T],
        *,
        load: Callable[[Tx, Command], S],
        apply: Apply[T] | None = None,
    ) -> Receipt:
        """Decide `command` and commit the outcome through `tx`.

        `load` reads the state the decider needs (`lambda tx, command: None` if none). It runs
        after the receipt check, which holds a lock on the command ID until commit, so two
        concurrent retries decide only once. `apply` writes the module's rows and gets the
        appended events, so rows can reference them.
        """
        stored = self._receipts.find(tx, command.command_id)
        if stored is not None:
            if (stored.project_id, stored.type) != (command.project_id, command.type):
                raise CommandIdReused(
                    f"command {command.command_id} was already used for {stored.type}"
                )
            return stored

        decision = decide(command, load(tx, command))
        for draft in decision.events:
            if draft.project_id != command.project_id:
                raise ValueError("a command may only write events for its own project")
        events = tuple(self._ledger.append(tx, draft) for draft in decision.events)
        if apply is not None:
            apply(tx, decision, events)
        for spec in decision.jobs:
            self._queue.enqueue(tx, spec)
        receipt = Receipt(
            command_id=command.command_id,
            project_id=command.project_id,
            type=command.type,
            accepted_at=self._clock.now().astimezone(UTC),
            event_seqs=tuple(e.seq for e in events),
        )
        self._receipts.save(tx, receipt)
        return receipt
