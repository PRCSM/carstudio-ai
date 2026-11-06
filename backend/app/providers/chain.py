"""Provider fallback chain: paid -> free -> local composite.

A provider is skipped when it is not configured, its circuit is open (recent credit/quota/outage
failure), the monthly budget can't cover it, or it may train on inputs and the customer hasn't
consented to that. Policy rejections stop the chain instead of being routed around."""
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from app.providers.base import EditRequest, EditResult, ImageProvider, PolicyRejected, ProviderError

log = logging.getLogger(__name__)


class Budget(Protocol):
    def can_spend(self, amount_usd: float) -> bool: ...


class CircuitStore(Protocol):
    def is_open(self, provider: str) -> bool: ...

    def open(self, provider: str, seconds: int) -> None: ...


class MemoryCircuits:
    def __init__(self, clock=time.monotonic):
        self._until: dict[str, float] = {}
        self._clock = clock

    def is_open(self, provider: str) -> bool:
        return self._until.get(provider, 0) > self._clock()

    def open(self, provider: str, seconds: int) -> None:
        self._until[provider] = self._clock() + seconds


class RedisCircuits:
    def __init__(self, redis_client, prefix: str = "circuit:"):
        self.r = redis_client
        self.prefix = prefix

    def is_open(self, provider: str) -> bool:
        return bool(self.r.exists(self.prefix + provider))

    def open(self, provider: str, seconds: int) -> None:
        self.r.set(self.prefix + provider, "1", ex=seconds)


class AllProvidersFailed(RuntimeError):
    def __init__(self, attempts: list[dict]):
        super().__init__("all image providers failed or were skipped")
        self.attempts = attempts


@dataclass
class ChainOutcome:
    result: EditResult
    attempts: list[dict] = field(default_factory=list)


class ProviderChain:
    def __init__(self, providers: list[ImageProvider], circuits: CircuitStore, circuit_seconds: int = 300):
        self.providers = providers
        self.circuits = circuits
        self.circuit_seconds = circuit_seconds

    def run(self, req: EditRequest, *, budget: Budget, allow_training_providers: bool) -> ChainOutcome:
        attempts: list[dict] = []
        for p in self.providers:
            skip = self._skip_reason(p, budget, allow_training_providers)
            if skip:
                attempts.append({"provider": p.name, "skipped": skip})
                continue
            started = time.monotonic()
            try:
                result = p.edit(req)
            except PolicyRejected as e:
                attempts.append({"provider": p.name, "error": "policy_rejected"})
                log.warning("policy rejection from %s: %s", p.name, e)
                raise
            except ProviderError as e:
                attempts.append({"provider": p.name, "error": type(e).__name__, "detail": str(e)[:200]})
                if e.circuit:
                    self.circuits.open(p.name, self.circuit_seconds)
                log.warning("provider %s failed (%s), falling back", p.name, type(e).__name__)
                continue
            except Exception as e:  # unexpected bug in a provider: don't take the job down
                attempts.append({"provider": p.name, "error": "unexpected", "detail": type(e).__name__})
                log.exception("unexpected error in provider %s", p.name)
                continue
            attempts.append({"provider": p.name, "ok": True,
                             "ms": int((time.monotonic() - started) * 1000)})
            return ChainOutcome(result=result, attempts=attempts)
        raise AllProvidersFailed(attempts)

    def _skip_reason(self, p: ImageProvider, budget: Budget, allow_training: bool) -> str | None:
        if not p.is_configured():
            return "not_configured"
        if self.circuits.is_open(p.name):
            return "circuit_open"
        if p.shares_data_for_training and not allow_training:
            return "no_training_consent"
        if not budget.can_spend(p.cost_per_call_usd):
            return "budget_exhausted"
        return None


def build_providers(settings) -> list[ImageProvider]:
    from app.providers.local_composite import LocalComposite

    registry = {}

    def fal():
        from app.providers.fal_provider import FalFluxKontext
        return FalFluxKontext(settings.fal_key, settings.fal_model, settings.price_fal_flux_kontext)

    def gemini_paid():
        from app.providers.gemini_provider import GeminiImage
        return GeminiImage(name="gemini_paid", api_key=settings.gemini_paid_api_key,
                           model=settings.gemini_image_model, cost_usd=settings.price_gemini_paid,
                           free_tier=False)

    def gemini_free():
        from app.providers.gemini_provider import GeminiImage
        return GeminiImage(name="gemini_free", api_key=settings.gemini_free_api_key,
                           model=settings.gemini_image_model, cost_usd=0.0, free_tier=True)

    registry.update(fal_flux_kontext=fal, gemini_paid=gemini_paid, gemini_free=gemini_free,
                    local_composite=LocalComposite)
    unknown = [n for n in settings.chain if n not in registry]
    if unknown:
        raise ValueError(f"unknown providers in IMAGE_PROVIDER_CHAIN: {unknown}")
    return [registry[n]() for n in settings.chain]
