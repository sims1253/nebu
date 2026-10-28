"""A parser that needs no PDF, for tests in modules that consume layers."""

from kittum.documents.layout import PageInput, PBlock, PWord, assemble
from kittum.documents.model import ParserRecipe, RawLayer

_CHAR = 5.0  # points per character
_LEADING = 12.0


class TextParser:
    """Lays out the given page texts in a fixed-width grid on A4 pages: one block per paragraph
    (blank line), one printed line per text line. It ignores the PDF bytes it is given."""

    def __init__(self, pages: list[str], *, name: str = "text") -> None:
        self._pages = pages
        self.recipe = ParserRecipe(engine=f"fake-{name}", engine_version="1")

    def parse(self, pdf: bytes) -> RawLayer:
        inputs = []
        for number, text in enumerate(self._pages, start=1):
            blocks: list[PBlock] = []
            y = 72.0
            for paragraph in text.split("\n\n"):
                lines = []
                for raw in paragraph.splitlines():
                    x = 72.0
                    words = []
                    for token in raw.split():
                        words.append(PWord(text=token, rect=(x, y, x + _CHAR * len(token), y + 10)))
                        x += _CHAR * (len(token) + 1)
                    if words:
                        lines.append(words)
                    y += _LEADING
                if lines:
                    blocks.append(PBlock(lines=lines))
                y += _LEADING
            inputs.append(
                PageInput(number=number, width=595.0, height=842.0, rotation=0, blocks=blocks)
            )
        return assemble(inputs)
