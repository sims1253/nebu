"""A backend that replays stored answers, keyed by request hash."""

from kittum.core import sha256_hex
from kittum.models import Completion, CompletionRequest, request_key


class RecordedBackend:
    def __init__(
        self,
        answers: dict[str, str] | None = None,
        *,
        default: str | None = None,
        model_identity: str | None = None,
    ) -> None:
        self._answers = answers or {}
        self._default = default
        self._model_identity = model_identity
        self.requests: list[CompletionRequest] = []

    @staticmethod
    def key(request: CompletionRequest) -> str:
        return request_key(request)

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        key = self.key(request)
        text = self._answers.get(key, self._default)
        if text is None:
            raise KeyError(f"no recorded answer for request {key}")
        return Completion(
            text=text,
            model_identity=self._model_identity or request.model_id,
            request_hash=key,
            raw_response_hash=sha256_hex(text.encode()),
        )
