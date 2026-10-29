"""Reading a chat completion streamed as server-sent events.

With `"stream": true`, vLLM, Ollama, and hosted OpenAI-compatible APIs send the answer in chunks,
one `data:` line each. A chunk is a JSON object whose `choices[0].delta` adds to the answer
(`content`) or to the thinking (`reasoning_content`, or `reasoning` in vLLM 0.11 and Ollama). The
last chunk with a choice carries `finish_reason`. With `stream_options.include_usage`, one more
chunk without choices carries `usage`. `data: [DONE]` ends the stream. A server reports a failure
midway as an `event: error`, or as a chunk with a top-level `error` object.

`assemble` adds the chunks up to the body a non-streamed call returns, so whatever reads a stored
response reads both forms the same way.
"""

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

_FIELDS = (b"data:", b":", b"event:", b"id:", b"retry:")
_LINES = re.compile(r"\r\n|\r|\n")


def is_event_stream(data: bytes) -> bool:
    """Whether a stored response is a server-sent event stream rather than one JSON body."""
    return data.lstrip().startswith(_FIELDS)


def _events(text: str) -> Iterator[tuple[str, str]]:
    """The type and the data of each event: its `event:` field ("message" without one) and its
    `data:` lines joined. Comments and other fields are left out."""
    kind, lines = "message", []
    for line in _LINES.split(text):
        if not line:
            if lines:
                yield kind, "\n".join(lines)
            kind, lines = "message", []
        elif line.startswith("event:"):
            kind = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            lines.append(line.removeprefix("data:").removeprefix(" "))
    if lines:
        yield kind, "\n".join(lines)


def assemble(data: bytes) -> dict[str, Any]:
    """The chat completion body the stream adds up to. Raises ValueError for a stream that is not
    a chat completion, reports an error, or ends before the answer does."""
    model: str | None = None
    content: list[str] = []
    reasoning: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    done = False
    choices = 0
    for kind, event in _events(data.decode()):
        if kind == "error":
            raise ValueError(f"the server reported an error: {event[:300]}")
        if event.strip() == "[DONE]":
            done = True
            break
        chunk = json.loads(event)
        if not isinstance(chunk, dict):
            raise ValueError(f"a chunk is not a JSON object: {event[:200]}")
        if chunk.get("error"):
            raise ValueError(f"the server reported an error: {json.dumps(chunk['error'])[:300]}")
        model = model or chunk.get("model")
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or ():
            if choice.get("index", 0) != 0:
                continue
            choices += 1
            delta = choice.get("delta") or {}
            content.append(delta.get("content") or "")
            reasoning.append(delta.get("reasoning_content") or delta.get("reasoning") or "")
            finish_reason = choice.get("finish_reason") or finish_reason
    if not choices:
        raise ValueError("the stream holds no answer")
    if not done and finish_reason is None:
        raise ValueError("the stream ended before the answer did")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if any(reasoning):
        message["reasoning"] = "".join(reasoning)
    body: dict[str, Any] = {
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        body["usage"] = usage
    return body
