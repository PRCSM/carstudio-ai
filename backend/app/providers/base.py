from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """Base for provider failures. `fallback` means: try the next provider in the chain."""
    fallback = True
    circuit = False  # open the circuit breaker for this provider


class CreditExhausted(ProviderError):
    """Out of credit / billing problem / auth failure — skip this provider for a while."""
    circuit = True


class QuotaExhausted(ProviderError):
    """Rate limit or daily quota hit."""
    circuit = True


class TransientProviderError(ProviderError):
    """Timeout / 5xx — fall back now; the provider may be fine on the next job."""


class PolicyRejected(ProviderError):
    """Provider refused the content. Never route around this by trying another provider."""
    fallback = False


@dataclass
class EditRequest:
    image: bytes            # normalized source JPEG
    prompt: str
    style: str
    width: int
    height: int
    image_url: str | None = None  # short-lived signed URL, for providers that prefer URLs


@dataclass
class EditResult:
    image: bytes
    provider: str
    model_id: str
    cost_usd: float
    meta: dict = field(default_factory=dict)


class ImageProvider(Protocol):
    name: str
    cost_per_call_usd: float
    shares_data_for_training: bool

    def is_configured(self) -> bool: ...

    def edit(self, req: EditRequest) -> EditResult: ...
