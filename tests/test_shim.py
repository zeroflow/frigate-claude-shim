"""Tests for shim.py (stdlib + pytest)."""
import importlib.util
import json
import pathlib

SHIM = pathlib.Path(__file__).resolve().parent.parent / "shim.py"
spec = importlib.util.spec_from_file_location("shim", SHIM)
assert spec and spec.loader
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)

FRIGATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["observations", "confidence"],
    "properties": {
        "observations": {
            "type": "array", "minItems": 3, "maxItems": 8, "description": "Enumerate.",
            "items": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "format": {"type": "string", "pattern": "^x$"},  # property *named* like a keyword
    },
}


def test_relax_moves_constraints_into_description():
    out = shim.relax_schema(FRIGATE_SCHEMA)
    obs = out["properties"]["observations"]
    assert "minItems" not in obs and "maxItems" not in obs
    assert obs["description"] == "Enumerate. Constraints: minItems=3, maxItems=8. REQUIRED: at least 3 items."
    assert "REQUIRED: never empty, write at least 20 characters." in obs["items"]["description"]
    assert "minLength" not in obs["items"] and "minLength=20" in obs["items"]["description"]
    conf = out["properties"]["confidence"]
    assert "minimum" not in conf and conf["description"] == "Constraints: minimum=0.0, maximum=1.0."
    assert "format" in out["properties"] and "pattern" not in out["properties"]["format"]
    assert out["required"] == ["observations", "confidence"] and out["additionalProperties"] is False


def test_relax_keeps_min_items_0_and_1_and_input_untouched():
    schema = {"type": "array", "minItems": 1, "items": {"type": "string"}}
    assert shim.relax_schema(schema) == schema
    shim.relax_schema(FRIGATE_SCHEMA)
    assert FRIGATE_SCHEMA["properties"]["observations"]["minItems"] == 3


def test_rewrite_body_only_touches_response_format():
    body = {"model": "m", "messages": [{"role": "user", "content": "minItems"}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "r", "strict": True, "schema": FRIGATE_SCHEMA}}}
    out = json.loads(shim.rewrite_body(json.dumps(body).encode()))
    assert out["messages"] == body["messages"] and out["response_format"]["json_schema"]["strict"] is True
    assert "minItems" not in out["response_format"]["json_schema"]["schema"]["properties"]["observations"]
    plain = json.dumps({"model": "m", "messages": []}).encode()
    assert shim.rewrite_body(plain) == plain
    assert shim.rewrite_body(b"not json") == b"not json"


def test_upstream_url_allowlist_and_pinned_host():
    up = shim.UPSTREAM
    assert shim.upstream_url("POST", "/v1/chat/completions") == up + "/v1/chat/completions"
    assert shim.upstream_url("GET", "/v1/models?limit=5") == up + "/v1/models?limit=5"
    for method, path in [("GET", "/v1/chat/completions"), ("POST", "/v1/messages"), ("GET", "/"),
                         ("POST", "@evil.example/v1/chat/completions"), ("GET", "//evil.example/v1/models"),
                         ("POST", "/v1/chat/completions/../../x"), ("GET", "http://evil.example/v1/models")]:
        assert shim.upstream_url(method, path) is None, (method, path)


def test_sun_times_vienna_plausible():
    import calendar
    import datetime
    # Vienna (48.21 N, 16.37 E), almanac values in UTC: 2026-11-20 ~06:10 / ~15:09, 2026-06-21 02:53 / 18:58
    for day, rise_ref, set_ref in [(datetime.date(2026, 11, 20), (6, 10), (15, 9)),
                                   (datetime.date(2026, 6, 21), (2, 53), (18, 58))]:
        rise, sset = shim.sun_times(day, 48.21, 16.37)
        midnight = calendar.timegm(day.timetuple())
        for got, (h, m) in ((rise, rise_ref), (sset, set_ref)):
            assert abs(got - (midnight + h * 3600 + m * 60)) < 300, (day, got)


def test_enrich_only_review_prompt():
    review = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "intro\n## Sequence Details\n\n- Camera: front_door\n"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xx"}}]}]}
    out = json.loads(shim.rewrite_body(json.dumps(review).encode(), lambda: "- Active Frigate profile: away"))
    text = out["messages"][0]["content"][0]["text"]
    assert text.index("Active Frigate profile: away") < text.index("- Camera: front_door")
    other = json.dumps({"messages": [{"role": "user", "content": "describe object"}]}).encode()
    called = []
    assert shim.rewrite_body(other, lambda: called.append(1) or "x") == other and not called


def test_context_lines(monkeypatch):
    monkeypatch.setattr(shim, "LAT", 48.21)
    monkeypatch.setattr(shim, "LON", 16.37)
    monkeypatch.setattr(shim, "LOCAL_TZ", "Europe/Vienna")
    text = shim.context_lines(1795181446, "away (Abwesend)")  # 2026-11-20 13:30:46 UTC
    assert "- Active Frigate profile: away (Abwesend)" in text and "Friday 2026-11-20" in text
    assert "is in UTC" in text and "UTC+01:00 today" in text
    assert "now 13:30 UTC (01:30 PM) = 14:30 local" in text
    assert "sunset today 15:1" in text and "= 16:1" in text  # ~15:11 UTC = 16:11 CET


def test_empty_required_fields_detected():
    body = json.dumps({"response_format": {"type": "json_schema", "json_schema": {"schema": {
        "type": "object", "properties": {"scene": {"type": "string", "minLength": 150},
                                         "title": {"type": "string", "maxLength": 80}}}}}}).encode()
    assert shim.nonempty_fields(body) == ["scene"]
    assert shim.nonempty_fields(b'{"messages": []}') == []
    answer = lambda c: json.dumps({"choices": [{"message": {"content": json.dumps(c)}}]}).encode()  # noqa: E731
    assert shim.empty_fields(answer({"scene": " ", "title": ""}), ["scene"]) == ["scene"]
    assert shim.empty_fields(answer({"scene": "Ein Auto."}), ["scene"]) == []
    assert shim.empty_fields(b"garbage", ["scene"]) == []
