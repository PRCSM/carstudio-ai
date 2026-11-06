import json

import pytest

pytest.importorskip("httpx")
from app.services.vision import VisionClient  # noqa: E402


class Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, by_model):
        self.by_model = by_model
        self.calls = []

    def post(self, url, headers, json):
        model = url.split("/models/")[1].split(":")[0]
        self.calls.append(model)
        assert "key=" not in url and headers["x-goog-api-key"] == "k"
        return self.by_model[model]


def ok(payload):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def test_skips_retired_models_and_remembers_working_one():
    http = FakeHttp({"old-flash": Resp(404), "new-flash": ok({"verdict": "same_vehicle"})})
    v = VisionClient("k", "old-flash, new-flash", client=http)
    assert v.compare(b"a", b"b") == {"verdict": "same_vehicle"}
    assert v.profile(b"a") == {"verdict": "same_vehicle"}
    assert http.calls == ["old-flash", "new-flash", "new-flash"]
    assert v.model == "new-flash"


def test_other_errors_are_advisory_not_fatal():
    http = FakeHttp({"m": Resp(500)})
    v = VisionClient("k", "m", client=http)
    assert v.profile(b"a") is None
    assert v.model == "m"  # a 500 doesn't retire the model


def test_all_models_missing():
    v = VisionClient("k", "a,b", client=FakeHttp({"a": Resp(404), "b": Resp(404)}))
    assert v.profile(b"x") is None and v.model is None
