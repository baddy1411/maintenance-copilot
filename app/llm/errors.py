class LLMError(RuntimeError):
    """Base class for gateway failures the caller can handle (degraded mode, refusal, retry later)."""


class SpendLimitExceeded(LLMError):
    def __init__(self, scope: str, spent: float, estimate: float, cap: float) -> None:
        self.scope = scope
        super().__init__(f"{scope} spend cap: ${spent:.4f} spent + ${estimate:.4f} estimated > ${cap:.2f}")


class CreditsExhausted(LLMError):
    """402 from the provider. No fallback: another model on the same account fails the same way."""


class ModelUnavailable(LLMError):
    """The primary model, and the fallback if there is one, failed after retries."""


class UnsupportedModel(LLMError):
    """A required parameter (e.g. response_format) isn't supported by the model's endpoints."""


class StructuredOutputError(LLMError):
    """The model didn't produce valid JSON for the schema within the retry budget."""
