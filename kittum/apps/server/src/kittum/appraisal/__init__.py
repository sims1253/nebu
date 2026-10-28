"""Risk-of-bias appraisal (DESIGN.md, "Appraisal").

Forms are data: the G-BA Bewertungsbogen for Anhang 4-F, RoB 2, and ROBINS-I V2. The AI only
assembles quotes per signalling question; humans answer.

Owner: WP-12. Shipped now: the types. Still to build: instrument definitions, quote assembly, and
rendering.
"""

from typing import Literal

from kittum.core import Id, Record

type Instrument = Literal["gba_bewertungsbogen", "rob2", "robins_i_v2"]
type Answer = Literal["yes", "probably_yes", "probably_no", "no", "no_information"]


class SignallingQuestion(Record):
    id: str
    text_de: str
    text_en: str


class QuestionDraft(Record):
    question_id: str
    quote_anchor_ids: tuple[Id, ...]
    answer: Answer | None = None  # set by a human only


class AppraisalDraft(Record):
    study_id: Id
    instrument: Instrument
    questions: tuple[QuestionDraft, ...]


def draft(study_id: Id, instrument: Instrument) -> AppraisalDraft:
    raise NotImplementedError("WP-12")


__all__ = ["Answer", "AppraisalDraft", "Instrument", "QuestionDraft", "SignallingQuestion", "draft"]
