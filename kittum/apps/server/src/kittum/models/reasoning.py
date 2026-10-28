"""Separating a reasoning model's thinking from its answer.

With a reasoning parser, vLLM and Ollama return thinking in a separate field. Without one, the
thinking arrives in the content before the answer, in a model-specific wrapper:

- Qwen and Nemotron: `<think>...</think>`. The chat template often opens the tag in the prompt,
  so the content may hold only the closing tag.
- Gemma 4: `<|channel>thought\\n...<channel|>`, empty when thinking is off.
- gpt-oss (harmony): `<|channel|>analysis<|message|>...<|channel|>final<|message|>`.

Some models also wrap the answer in a Markdown code fence.
"""

import re

_THINK_CLOSE = "</think>"
_GEMMA_OPEN = "<|channel>thought"
_GEMMA_CLOSE = "<channel|>"
_HARMONY_FINAL = "<|channel|>final<|message|>"
_HARMONY_END = re.compile(r"<\|(?:end|return)\|>\s*$")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)


def split_reasoning(text: str) -> tuple[str | None, str]:
    """Return `(thinking, answer)`. `thinking` is None when the text holds none."""
    text = text.strip()
    reasoning: str | None = None
    if _THINK_CLOSE in text:
        head, _, text = text.rpartition(_THINK_CLOSE)
        reasoning = head.replace("<think>", "").strip()
    elif text.startswith(_GEMMA_OPEN) and _GEMMA_CLOSE in text:
        head, _, text = text.partition(_GEMMA_CLOSE)
        reasoning = head.removeprefix(_GEMMA_OPEN).strip()
    elif _HARMONY_FINAL in text:
        head, _, text = text.rpartition(_HARMONY_FINAL)
        reasoning = re.sub(r"<\|channel\|>analysis<\|message\|>|<\|[a-z]+\|>", " ", head).strip()
        text = _HARMONY_END.sub("", text)
    text = text.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    return (reasoning or None), text
