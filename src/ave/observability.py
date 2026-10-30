"""Tracing: nested spans written to a local JSONL file and, optionally, LangSmith.

Usage:
    tracer = Tracer.for_run(project, run_id)
    with tracer.span("phase:analyze", kind="phase") as sp:
        with tracer.span("tool:ocr", kind="tool", inputs={"frames": 120}) as t:
            t.set_outputs(hits=412)
    tracer.feedback("gate:plan_approval", score=1, comment="approved v2")
    tracer.close()

Each CLI invocation produces its own LangSmith trace. Traces of the same pipeline run are
grouped into one LangSmith thread through the `thread_id` metadata (set to the run id).
"""

from __future__ import annotations

import base64
import functools
import io
import json
import os
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ave.config import Observability

if TYPE_CHECKING:
    import anthropic

    from ave.config import Project

SpanKind = Literal["run", "phase", "agent", "tool", "llm", "gate"]
ImageMode = Literal["full", "thumbnails", "none"]

_LS_RUN_TYPE = {"llm": "llm", "tool": "tool"}  # everything else is a LangSmith "chain"
THUMBNAIL_PX = 256


def _now() -> datetime:
    return datetime.now(UTC)


# --- image redaction -----------------------------------------------------------------


def _thumbnail(b64: str, media_type: str) -> tuple[str, str] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            im.thumbnail((THUMBNAIL_PX, THUMBNAIL_PX))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        return None


def redact_images(obj: Any, mode: ImageMode) -> Any:
    """Return a copy of obj with Claude image blocks kept, shrunk or removed."""
    if mode == "full":
        return obj
    if isinstance(obj, list):
        return [redact_images(x, mode) for x in obj]
    if not isinstance(obj, dict):
        return obj
    src = obj.get("source")
    if obj.get("type") == "image" and isinstance(src, dict) and src.get("type") == "base64":
        thumb = (
            _thumbnail(src.get("data", ""), src.get("media_type", ""))
            if mode == "thumbnails"
            else None
        )
        if thumb is None:
            return {
                "type": "image",
                "source": {"type": "omitted", "media_type": src.get("media_type")},
            }
        data, media_type = thumb
        return {**obj, "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {k: redact_images(v, mode) for k, v in obj.items()}


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


_warned: set[str] = set()


def _langsmith_key() -> bool:
    return bool(os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"))


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(f"ave: {msg}", file=sys.stderr)


# --- spans ---------------------------------------------------------------------------


@dataclass
class Span:
    id: str
    trace_id: str
    parent_id: str | None
    name: str
    kind: SpanKind
    start: datetime
    inputs: dict[str, Any]
    metadata: dict[str, Any]
    outputs: dict[str, Any] = field(default_factory=dict)
    ls_run: Any = None  # langsmith RunTree when LangSmith tracing is on

    def set_outputs(self, **outputs: Any) -> None:
        self.outputs.update(outputs)

    def add_metadata(self, **metadata: Any) -> None:
        self.metadata.update(metadata)
        if self.ls_run is not None:
            self.ls_run.add_metadata(metadata)


_current: ContextVar[Span | None] = ContextVar("ave_current_span", default=None)


class Tracer:
    def __init__(
        self,
        cfg: Observability,
        trace_file: Path | None = None,
        *,
        run_id: str | None = None,
        ls_client: Any = None,
    ):
        self.cfg = cfg
        self.run_id = run_id
        self.trace_file = trace_file if cfg.provider != "none" else None
        self._lock = threading.Lock()
        self._root_ls_run: Any = None
        self._ls_client = ls_client
        if cfg.provider == "langsmith" and self._ls_client is None and not _langsmith_key():
            # without a key every upload fails with 401; keep the local trace and say so once
            _warn_once("LANGSMITH_API_KEY is not set; tracing locally only (runs/<id>/trace.jsonl)")
        elif cfg.provider == "langsmith" and self._ls_client is None:
            import langsmith

            hide = functools.partial(redact_images, mode=cfg.log_images)
            self._ls_client = langsmith.Client(hide_inputs=hide, hide_outputs=hide)

    @classmethod
    def for_run(cls, project: Project, run_id: str | None = None) -> Tracer:
        """Tracer writing to runs/<id>/trace.jsonl (or .ave/trace.jsonl outside a run)."""
        base = project.runs_dir / run_id if run_id else project.state_dir
        return cls(project.config.observability, base / "trace.jsonl", run_id=run_id)

    @property
    def langsmith_enabled(self) -> bool:
        return self._ls_client is not None

    @contextmanager
    def span(
        self,
        name: str,
        kind: SpanKind = "agent",
        inputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        parent = _current.get()
        meta = dict(metadata or {})
        if self.run_id:
            meta.setdefault("ave_run_id", self.run_id)
            meta.setdefault("thread_id", self.run_id)
        sp = Span(
            id=str(uuid.uuid4()),
            trace_id=parent.trace_id if parent else str(uuid.uuid4()),
            parent_id=parent.id if parent else None,
            name=name,
            kind=kind,
            start=_now(),
            inputs=dict(inputs or {}),
            metadata=meta,
        )
        token = _current.set(sp)
        error: BaseException | None = None
        with ExitStack() as stack:
            if self.langsmith_enabled:
                sp.ls_run = self._enter_langsmith(stack, sp, is_root=parent is None)
                # local record only: lets `ave trace --links` find the run in LangSmith
                sp.metadata["ls_run_id"] = str(sp.ls_run.id)
            try:
                yield sp
            except BaseException as e:
                error = e
                raise
            finally:
                _current.reset(token)
                if sp.ls_run is not None:
                    sp.ls_run.add_outputs(_jsonable(sp.outputs))
                self._write_local(sp, error)

    def _enter_langsmith(self, stack: ExitStack, sp: Span, *, is_root: bool) -> Any:
        import langsmith

        if is_root:
            stack.enter_context(
                langsmith.tracing_context(
                    enabled=True, client=self._ls_client, project_name=self.cfg.project
                )
            )
        run = stack.enter_context(
            langsmith.trace(
                sp.name,
                run_type=_LS_RUN_TYPE.get(sp.kind, "chain"),
                inputs=_jsonable(sp.inputs),
                metadata={**sp.metadata, "ave_kind": sp.kind},
                tags=[f"ave:{sp.kind}"],
                project_name=self.cfg.project,
                client=self._ls_client,
            )
        )
        if is_root:
            self._root_ls_run = run
        return run

    def traced(self, name: str | None = None, kind: SpanKind = "agent") -> Callable:
        """Decorator form of span(); records the return value as `result` when it is JSON-able."""

        def deco(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self.span(name or fn.__qualname__, kind=kind) as sp:
                    result = fn(*args, **kwargs)
                    if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
                        sp.set_outputs(result=result)
                    return result

            return wrapper

        return deco

    def _write_local(self, sp: Span, error: BaseException | None) -> None:
        end = _now()
        self.write_record(
            {
                "id": sp.id,
                "trace_id": sp.trace_id,
                "parent_id": sp.parent_id,
                "name": sp.name,
                "kind": sp.kind,
                "start": sp.start.isoformat(),
                "end": end.isoformat(),
                "duration_ms": round((end - sp.start).total_seconds() * 1000, 2),
                "status": "error" if error else "ok",
                "error": repr(error) if error else None,
                "inputs": sp.inputs,
                "outputs": sp.outputs,
                "metadata": sp.metadata,
            }
        )

    def write_record(self, record: dict[str, Any]) -> None:
        """Append one finished span to the local trace file (no-op when tracing is off)."""
        if self.trace_file is None:
            return
        record = {
            **record,
            # local traces never store raw image bytes
            "inputs": redact_images(_jsonable(record.get("inputs") or {}), "none"),
            "outputs": redact_images(_jsonable(record.get("outputs") or {}), "none"),
            "metadata": _jsonable(record.get("metadata") or {}),
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self.trace_file.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def feedback(
        self,
        key: str,
        *,
        score: float | None = None,
        value: Any = None,
        comment: str | None = None,
    ) -> None:
        """Record a user decision (gate approval, change-request choice) against the trace."""
        current = _current.get()
        with self.span(
            f"feedback:{key}", kind="gate", inputs={"score": score, "value": value}
        ) as sp:
            sp.set_outputs(comment=comment)
        target = (current.ls_run if current else None) or self._root_ls_run
        if self.langsmith_enabled and target is not None:
            self._ls_client.create_feedback(
                run_id=target.id,
                trace_id=target.trace_id,
                key=key,
                score=score,
                value=value,
                comment=comment,
            )

    def trace_url(self) -> str | None:
        if self._root_ls_run is None:
            return None
        try:
            return self._root_ls_run.get_url()
        except Exception:
            return None

    def wrap_anthropic(self, client: anthropic.Anthropic) -> anthropic.Anthropic:
        if not self.langsmith_enabled:
            return client
        from langsmith.wrappers import wrap_anthropic

        return wrap_anthropic(client)

    def close(self) -> None:
        if self._ls_client is not None and hasattr(self._ls_client, "flush"):
            self._ls_client.flush()


def read_trace(trace_file: Path) -> list[dict[str, Any]]:
    if not trace_file.is_file():
        return []
    return [json.loads(line) for line in trace_file.read_text().splitlines() if line.strip()]
