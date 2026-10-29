"""A local model server that streams its answers as server-sent events over a real socket, the
way vLLM, Ollama, and Z.ai's API do with `"stream": true`. Tests start it, point an endpoint at
`url`, and read back what it received and what it sent."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Callable

# What one response sends: chunks of bytes, written as they come, and pauses in seconds between.
type Script = list[bytes | float]


def event(data: dict[str, Any] | str) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"data: {text}\n\n".encode()


def chunks(
    *,
    model: str,
    reasoning: list[str],
    content: list[str],
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> list[bytes]:
    """The events of one streamed answer in Z.ai's form: the thinking in `reasoning_content`
    deltas, then the answer in `content` deltas, `finish_reason` on the last, a usage chunk
    without choices, and `[DONE]`."""

    def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
        choice = {"index": 0, "delta": delta, "finish_reason": finish}
        return event({"id": "chatcmpl-1", "model": model, "choices": [choice]})

    out = [chunk({"role": "assistant", "reasoning_content": r}) for r in reasoning]
    out += [chunk({"content": c}) for c in content[:-1]]
    out.append(chunk({"content": content[-1]}, finish_reason))
    if usage is not None:
        out.append(event({"id": "chatcmpl-1", "model": model, "choices": [], "usage": usage}))
    out.append(event("[DONE]"))
    return out


class StreamingServer:
    """Answers each POST to `/v1/chat/completions` with `answer(body)`, streamed."""

    def __init__(self, answer: Callable[[dict[str, Any]], Script]) -> None:
        self.seen: list[bytes] = []  # each request body as received
        self.sent: list[bytes] = []  # each response body as sent
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["content-length"]))
                server.seen.append(body)
                script = answer(json.loads(body))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()  # HTTP/1.0: the body ends when the connection closes
                sent = b""
                for step in script:
                    if isinstance(step, float):
                        time.sleep(step)
                        continue
                    self.wfile.write(step)
                    self.wfile.flush()
                    sent += step
                server.sent.append(sent)

            def log_message(self, *args: object) -> None:
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._http.server_address[1]}/v1"

    def __enter__(self) -> Self:
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.shutdown()
        self._http.server_close()
