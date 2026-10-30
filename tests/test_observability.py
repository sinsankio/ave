import base64
import io
from pathlib import Path

import langsmith
import pytest
from PIL import Image

from ave.config import Observability
from ave.observability import THUMBNAIL_PX, Tracer, read_trace, redact_images


def _image_block(size=(1920, 1080)) -> dict:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


class RecordingClient(langsmith.Client):
    """Real LangSmith client with the network calls replaced by an in-memory log."""

    def __init__(self):
        super().__init__(api_key="fake", api_url="http://127.0.0.1:9", auto_batch_tracing=False)
        self.calls: list[tuple] = []

    def create_run(self, **kw):
        self.calls.append(
            ("create", kw["name"], kw.get("parent_run_id"), kw.get("extra"), kw.get("id"))
        )

    def update_run(self, run_id, **kw):
        self.calls.append(("update", str(run_id), kw.get("outputs"), kw.get("error")))

    def create_feedback(self, **kw):
        self.calls.append(("feedback", kw["key"], kw["score"], kw["run_id"], kw["comment"]))


def local_tracer(tmp_path: Path, **kw) -> Tracer:
    return Tracer(Observability(provider="local"), tmp_path / "trace.jsonl", **kw)


def test_local_spans_nest(tmp_path: Path):
    t = local_tracer(tmp_path, run_id="r-0001")
    with t.span("phase:analyze", kind="phase") as root:
        with t.span("tool:ocr", kind="tool", inputs={"frames": 3}) as child:
            child.set_outputs(hits=7)
    recs = {r["name"]: r for r in read_trace(t.trace_file)}
    assert recs["tool:ocr"]["parent_id"] == root.id
    assert recs["tool:ocr"]["trace_id"] == recs["phase:analyze"]["trace_id"]
    assert recs["phase:analyze"]["parent_id"] is None
    assert recs["tool:ocr"]["outputs"] == {"hits": 7}
    assert recs["tool:ocr"]["metadata"]["thread_id"] == "r-0001"
    # children finish first
    assert [r["name"] for r in read_trace(t.trace_file)] == ["tool:ocr", "phase:analyze"]


def test_error_recorded_and_reraised(tmp_path: Path):
    t = local_tracer(tmp_path)
    with pytest.raises(ValueError), t.span("agent:boom"):
        raise ValueError("bad frame")
    (rec,) = read_trace(t.trace_file)
    assert rec["status"] == "error"
    assert "bad frame" in rec["error"]


def test_sibling_roots_get_new_traces(tmp_path: Path):
    t = local_tracer(tmp_path)
    with t.span("a"):
        pass
    with t.span("b"):
        pass
    a, b = read_trace(t.trace_file)
    assert a["trace_id"] != b["trace_id"]


def test_provider_none_writes_nothing(tmp_path: Path):
    t = Tracer(Observability(provider="none"), tmp_path / "trace.jsonl")
    with t.span("x") as sp:
        sp.set_outputs(ok=True)
    assert not (tmp_path / "trace.jsonl").exists()
    assert not t.langsmith_enabled


def test_traced_decorator(tmp_path: Path):
    t = local_tracer(tmp_path)

    @t.traced(kind="tool")
    def add(a, b):
        return a + b

    assert add(2, 3) == 5
    (rec,) = read_trace(t.trace_file)
    assert rec["name"].endswith("add")
    assert rec["kind"] == "tool"
    assert rec["outputs"] == {"result": 5}


def test_redact_modes():
    payload = {"messages": [{"content": [_image_block(), {"type": "text", "text": "hi"}]}]}
    assert redact_images(payload, "full") is payload

    none = redact_images(payload, "none")
    blocks = none["messages"][0]["content"]
    assert blocks[0]["source"] == {"type": "omitted", "media_type": "image/png"}
    assert blocks[1] == {"type": "text", "text": "hi"}

    thumbs = redact_images(payload, "thumbnails")
    src = thumbs["messages"][0]["content"][0]["source"]
    assert src["media_type"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(src["data"]))) as im:
        assert max(im.size) <= THUMBNAIL_PX
    # original payload untouched
    assert payload["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_redact_thumbnail_of_garbage_falls_back_to_omitted():
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "!!"}}
    assert redact_images(block, "thumbnails")["source"]["type"] == "omitted"


def test_local_trace_never_stores_image_bytes(tmp_path: Path):
    t = local_tracer(tmp_path)
    with t.span("agent:vision", inputs={"content": [_image_block()]}):
        pass
    (rec,) = read_trace(t.trace_file)
    assert rec["inputs"]["content"][0]["source"]["type"] == "omitted"


def test_langsmith_spans_and_feedback(tmp_path: Path):
    client = RecordingClient()
    t = Tracer(
        Observability(provider="langsmith"), tmp_path / "t.jsonl", run_id="r-0007", ls_client=client
    )
    with t.span("phase:plan", kind="phase") as root:
        with t.span("agent:pacing_editor") as child:
            child.set_outputs(duration=52.4)
        t.feedback("gate:plan_approval", score=1, comment="approved")

    assert child.ls_run.parent_run_id == root.ls_run.id
    creates = [c for c in client.calls if c[0] == "create"]
    assert creates[0][1] == "phase:plan" and creates[0][2] is None
    assert creates[0][3]["metadata"]["thread_id"] == "r-0007"
    assert creates[1][1] == "agent:pacing_editor"
    assert ("update", str(child.ls_run.id), {"duration": 52.4}, None) in client.calls
    (fb,) = [c for c in client.calls if c[0] == "feedback"]
    assert fb[1:3] == ("gate:plan_approval", 1)
    assert fb[3] == root.ls_run.id
    # local copy still written alongside LangSmith
    assert {r["name"] for r in read_trace(t.trace_file)} >= {"phase:plan", "agent:pacing_editor"}


def test_langsmith_error_propagates(tmp_path: Path):
    client = RecordingClient()
    t = Tracer(Observability(provider="langsmith"), None, ls_client=client)
    with pytest.raises(RuntimeError), t.span("tool:render", kind="tool"):
        raise RuntimeError("ffmpeg exited 1")
    (update,) = [c for c in client.calls if c[0] == "update"]
    assert "ffmpeg exited 1" in update[3]


def test_langsmith_client_applies_image_mode(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "fake")
    t = Tracer(Observability(provider="langsmith", log_images="none"))
    hidden = t._ls_client._hide_run_inputs({"content": [_image_block((32, 32))]})
    assert hidden["content"][0]["source"]["type"] == "omitted"


def test_wrap_anthropic_noop_without_langsmith(tmp_path: Path):
    import anthropic

    client = anthropic.Anthropic(api_key="test")
    assert local_tracer(tmp_path).wrap_anthropic(client) is client


def test_wrap_anthropic_with_langsmith():
    import anthropic

    t = Tracer(Observability(provider="langsmith"), None, ls_client=RecordingClient())
    wrapped = t.wrap_anthropic(anthropic.Anthropic(api_key="test"))
    assert hasattr(wrapped, "messages")


def test_langsmith_without_key_falls_back_to_local(tmp_path: Path, capsys, monkeypatch):
    import ave.observability as obs

    monkeypatch.setattr(obs, "_warned", set())
    t = Tracer(Observability(provider="langsmith"), tmp_path / "t.jsonl")
    assert not t.langsmith_enabled
    with t.span("x"):
        pass
    assert read_trace(tmp_path / "t.jsonl")[0]["name"] == "x"
    assert "LANGSMITH_API_KEY is not set" in capsys.readouterr().err
