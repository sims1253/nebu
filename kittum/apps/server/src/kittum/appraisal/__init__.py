"""Risk-of-bias appraisal (DESIGN.md, "Appraisal").

Forms are data. The core instruments are RoB 2 and ROBINS-I V2; profiles add their own with
`register_instrument` under IDs of the form `<profile id>.<name>`. The AI only assembles quotes per
signalling question; humans answer.

Owner: WP-12. Shipped now: the types and the instrument registry. Still to build: the core
instrument definitions, quote assembly, and rendering.
"""

from typing import Literal, get_args

from kittum.core import Id, Record

type CoreInstrument = Literal["rob2", "robins_i_v2"]
type Instrument = str  # a CoreInstrument, or "<profile id>.<name>" from a profile
type Answer = Literal["yes", "probably_yes", "probably_no", "no", "no_information"]


class SignallingQuestion(Record):
    id: str
    text_de: str
    text_en: str


class InstrumentDefinition(Record):
    id: Instrument
    title: str
    questions: tuple[SignallingQuestion, ...]


class QuestionDraft(Record):
    question_id: str
    quote_anchor_ids: tuple[Id, ...]
    answer: Answer | None = None  # set by a human only


class AppraisalDraft(Record):
    study_id: Id
    instrument: Instrument
    questions: tuple[QuestionDraft, ...]


CORE_INSTRUMENTS: tuple[CoreInstrument, ...] = get_args(CoreInstrument.__value__)
_profile_instruments: dict[Instrument, InstrumentDefinition] = {}


def register_instrument(definition: InstrumentDefinition) -> None:
    """Add a profile's instrument. Called from the entry points, once per process."""
    profile, dot, name = definition.id.partition(".")
    if not (profile and dot and name):
        raise ValueError(
            f"profile instruments are named '<profile id>.<name>', got {definition.id!r}"
        )
    if definition.id in _profile_instruments:
        raise ValueError(f"instrument {definition.id!r} is already registered")
    _profile_instruments[definition.id] = definition


def instruments() -> tuple[Instrument, ...]:
    return (*CORE_INSTRUMENTS, *sorted(_profile_instruments))


def draft(study_id: Id, instrument: Instrument) -> AppraisalDraft:
    raise NotImplementedError("WP-12")


__all__ = [
    "CORE_INSTRUMENTS",
    "Answer",
    "AppraisalDraft",
    "CoreInstrument",
    "Instrument",
    "InstrumentDefinition",
    "QuestionDraft",
    "SignallingQuestion",
    "draft",
    "instruments",
    "register_instrument",
]
