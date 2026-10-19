from types import SimpleNamespace

from musicfinder import llm


class FakeClient:
    def __init__(self, text, stop="end_turn"):
        self.calls = []
        self.text, self.stop = text, stop
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop, stop_details=None,
                               content=[SimpleNamespace(type="text", text=self.text)])


def test_json_call_request_shape_and_cache(monkeypatch):
    fake = FakeClient('{"events": []}')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(llm, "_client", fake)
    schema = {"type": "object"}
    out = llm.json_call(system="s", prompt="p", schema=schema, images=[(b"img", "image/png")])
    assert out == {"events": []}
    kw = fake.calls[0]
    assert kw["model"] == llm.MODEL
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert kw["messages"][0]["content"][0]["type"] == "image"
    assert kw["extra_body"] == {"fallbacks": "default"}
    llm.json_call(system="s", prompt="p", schema=schema, images=[(b"img", "image/png")])
    assert len(fake.calls) == 1  # cached


def test_json_call_refusal_and_no_fallback_model(monkeypatch):
    fake = FakeClient("", stop="refusal")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(llm, "MODEL", "claude-haiku-5-5")
    assert llm.json_call(system="s", prompt="q", schema={}) is None
    assert "extra_body" not in fake.calls[0] and "betas" not in fake.calls[0]
