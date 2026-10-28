"""A backend that replays stored answers, keyed by request hash."""

from kittum.core import content_hash, sha256_hex
from kittum.models import Completion, CompletionRequest


class RecordedBackend:
    def __init__(
        self, answers: dict[str, str] | None = None, *, default: str | None = None
    ) -> None:
        self._answers = answers or {}
        self._default = default
        self.requests: list[CompletionRequest] = []

    @staticmethod
    def key(request: CompletionRequest) -> str:
        return content_hash(request, domain="kittum.completion_request.v1")

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        key = self.key(request)
        text = self._answers.get(key, self._default)
        if text is None:
            raise KeyError(f"no recorded answer for request {key}")
        return Completion(
            text=text,
            model_identity=request.model_id,
            request_hash=key,
            raw_response_hash=sha256_hex(text.encode()),
        )
