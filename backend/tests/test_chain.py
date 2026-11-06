import pytest

from app.providers.base import (CreditExhausted, EditRequest, EditResult, PolicyRejected, QuotaExhausted,
                                TransientProviderError)
from app.providers.chain import AllProvidersFailed, MemoryCircuits, ProviderChain


class Fake:
    def __init__(self, name, cost=0.0, behaviour=None, configured=True, trains=False):
        self.name, self.cost_per_call_usd = name, cost
        self.behaviour, self.configured = behaviour, configured
        self.shares_data_for_training = trains
        self.calls = 0

    def is_configured(self):
        return self.configured

    def edit(self, req):
        self.calls += 1
        if self.behaviour:
            raise self.behaviour("boom")
        return EditResult(image=b"img", provider=self.name, model_id="m", cost_usd=self.cost_per_call_usd)


class Budget:
    def __init__(self, remaining):
        self.remaining = remaining

    def can_spend(self, amount):
        return amount <= self.remaining


REQ = EditRequest(image=b"x", prompt="p", style="white_studio", width=1, height=1)


def chain(*providers, clock=None):
    return ProviderChain(list(providers), MemoryCircuits(clock or (lambda: 0.0)), circuit_seconds=300)


def test_paid_used_first_when_healthy():
    paid, free, local = Fake("paid", 0.04), Fake("free", trains=True), Fake("local")
    out = chain(paid, free, local).run(REQ, budget=Budget(5), allow_training_providers=True)
    assert out.result.provider == "paid" and free.calls == 0 and local.calls == 0


def test_credit_exhausted_falls_back_and_opens_circuit():
    t = [0.0]
    paid = Fake("paid", 0.04, behaviour=CreditExhausted)
    free = Fake("free", trains=True)
    c = chain(paid, free, clock=lambda: t[0])
    assert c.run(REQ, budget=Budget(5), allow_training_providers=True).result.provider == "free"
    # second job: paid is skipped without being called while the circuit is open
    out = c.run(REQ, budget=Budget(5), allow_training_providers=True)
    assert paid.calls == 1 and {"provider": "paid", "skipped": "circuit_open"} in out.attempts
    t[0] = 301  # circuit closes again
    c.run(REQ, budget=Budget(5), allow_training_providers=True)
    assert paid.calls == 2


def test_budget_exhausted_skips_paid_without_calling_it():
    paid, local = Fake("paid", 0.04), Fake("local")
    out = chain(paid, local).run(REQ, budget=Budget(0.01), allow_training_providers=False)
    assert out.result.provider == "local" and paid.calls == 0
    assert out.attempts[0]["skipped"] == "budget_exhausted"


def test_free_tier_requires_training_consent():
    paid = Fake("paid", 0.04, behaviour=QuotaExhausted)
    free, local = Fake("free", trains=True), Fake("local")
    out = chain(paid, free, local).run(REQ, budget=Budget(5), allow_training_providers=False)
    assert out.result.provider == "local" and free.calls == 0


def test_policy_rejection_stops_chain():
    paid, local = Fake("paid", 0.04, behaviour=PolicyRejected), Fake("local")
    with pytest.raises(PolicyRejected):
        chain(paid, local).run(REQ, budget=Budget(5), allow_training_providers=True)
    assert local.calls == 0


def test_transient_error_does_not_open_circuit():
    paid = Fake("paid", 0.04, behaviour=TransientProviderError)
    local = Fake("local")
    c = chain(paid, local)
    c.run(REQ, budget=Budget(5), allow_training_providers=True)
    c.run(REQ, budget=Budget(5), allow_training_providers=True)
    assert paid.calls == 2


def test_unexpected_exception_is_contained():
    paid, local = Fake("paid", 0.04, behaviour=KeyError), Fake("local")
    assert chain(paid, local).run(REQ, budget=Budget(5), allow_training_providers=True).result.provider == "local"


def test_all_failed_reports_attempts():
    a = Fake("a", configured=False)
    b = Fake("b", behaviour=TransientProviderError)
    with pytest.raises(AllProvidersFailed) as e:
        chain(a, b).run(REQ, budget=Budget(5), allow_training_providers=True)
    assert e.value.attempts[0]["skipped"] == "not_configured"
    assert e.value.attempts[1]["error"] == "TransientProviderError"


def test_outage_classification():
    from importlib import util

    if util.find_spec("sqlalchemy") is None:
        pytest.skip("pipeline imports ORM models; runs in CI")
    from app.workers.pipeline import is_temporary_outage

    assert is_temporary_outage([{"provider": "fal", "error": "QuotaExhausted"},
                                {"provider": "local_composite", "error": "TransientProviderError"}])
    assert not is_temporary_outage([{"provider": "fal", "skipped": "not_configured"},
                                    {"provider": "local_composite", "error": "TransientProviderError"}])
