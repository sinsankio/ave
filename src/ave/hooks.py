"""Claude Code hook handler: turns agent activity into spans in the run's trace.

Claude Code runs `python -m ave hook` (async) on session, prompt, tool and sub-agent events and
passes the event as JSON on stdin. Each event is a separate process, so span ids and LangSmith
parent links are kept in small files under runs/<id>/.hooks/.

Span tree per Claude Code session:
    claude:session
    ├─ user:prompt           (zero-length marker: what the user typed, e.g. a gate decision)
    ├─ tool:<name>           (tool calls made by the orchestrator)
    └─ agent:<type>          (one per sub-agent)
       └─ tool:<name>        (tool calls made by that sub-agent)

The handler must never break the session: every error is swallowed.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ave.config import ConfigError, Project, find_project_file, load_env_file, load_project
from ave.observability import Tracer
from ave.runs import RunStore

MAX_TEXT = 2000


def _now() -> datetime:
    return datetime.now(UTC)


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + f"… [{len(value)} chars]"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value[:50]]
    return value


def _tool_label(payload: dict) -> str:
    name = payload.get("tool_name", "tool")
    inp = payload.get("tool_input") or {}
    if name == "Bash" and isinstance(inp.get("command"), str):
        words = inp["command"].split()
        head = " ".join(words[:2]) if words and words[0] == "ave" else (words[0] if words else "")
        return f"tool:Bash[{head}]" if head else "tool:Bash"
    if name in ("Agent", "Task") and inp.get("subagent_type"):
        return f"tool:{name}[{inp['subagent_type']}]"
    return f"tool:{name}"


class HookTracer:
    def __init__(self, project: Project, run_id: str, tracer: Tracer):
        self.project = project
        self.run_id = run_id
        self.tracer = tracer
        self.state_dir = project.runs_dir / run_id / ".hooks"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # --- tiny persistent state -------------------------------------------------------

    def _get(self, key: str) -> dict | None:
        f = self.state_dir / f"{key}.json"
        try:
            return json.loads(f.read_text()) if f.is_file() else None
        except (OSError, json.JSONDecodeError):
            return None

    def _put(self, key: str, value: dict) -> None:
        (self.state_dir / f"{key}.json").write_text(json.dumps(value))

    def _pop(self, key: str) -> dict | None:
        v = self._get(key)
        (self.state_dir / f"{key}.json").unlink(missing_ok=True)
        return v

    # --- spans -----------------------------------------------------------------------

    def _meta(self, payload: dict, ls_run_id: str | None = None) -> dict:
        meta = {
            "ave_run_id": self.run_id,
            "thread_id": self.run_id,
            "claude_session_id": payload.get("session_id"),
            "agent_type": payload.get("agent_type"),
        }
        if ls_run_id:
            meta["ls_run_id"] = ls_run_id
        return meta

    def session(self, payload: dict) -> dict:
        sid = payload.get("session_id") or "unknown"
        key = f"session-{sid}"
        rec = self._get(key)
        if rec:
            return rec
        rec = {"id": str(uuid.uuid4()), "trace_id": str(uuid.uuid4()), "start": _now().isoformat()}
        if self.tracer.langsmith_enabled:
            rt = self._ls_run(
                "claude:session", "chain", {"cwd": payload.get("cwd")}, payload, None, _now()
            )
            rt.post()
            rec |= {
                "ls_headers": rt.to_headers(),
                "ls_id": str(rt.id),
                "trace_id": str(rt.trace_id),
                "id": str(rt.id),
            }
        self._put(key, rec)
        # open the session span right away (rewritten at every Stop), so the tree still groups
        # its children when a session is killed before it ever stops
        self.tracer.write_record(
            {
                "id": rec["id"],
                "trace_id": rec["trace_id"],
                "parent_id": None,
                "name": "claude:session",
                "kind": "run",
                "start": rec["start"],
                "end": rec["start"],
                "duration_ms": 0.0,
                "status": "ok",
                "error": None,
                "inputs": {"cwd": payload.get("cwd")},
                "outputs": {},
                "metadata": self._meta(payload, rec["id"] if rec.get("ls_headers") else None),
            }
        )
        return rec

    def _ls_run(self, name, run_type, inputs, payload, parent_headers, start):
        from langsmith.run_trees import RunTree

        meta = {"metadata": self._meta(payload)}
        if parent_headers:
            parent = RunTree.from_headers(parent_headers, ls_client=self.tracer._ls_client)
            return parent.create_child(
                name=name, run_type=run_type, inputs=inputs, start_time=start, extra=meta
            )
        return RunTree(
            name=name,
            run_type=run_type,
            inputs=inputs,
            start_time=start,
            extra=meta,
            project_name=self.project.config.observability.project,
            ls_client=self.tracer._ls_client,
        )

    def _parent(self, payload: dict) -> dict:
        agent_id = payload.get("agent_id")
        if agent_id and (rec := self._get(f"agent-{agent_id}")):
            return rec
        return self.session(payload)

    def _emit(
        self, *, name, kind, parent, start, end, inputs, outputs, payload, error=None, span_id=None
    ) -> None:
        span_id = span_id or str(uuid.uuid4())
        self.tracer.write_record(
            {
                "id": span_id,
                "trace_id": parent["trace_id"],
                "parent_id": parent["id"],
                "name": name,
                "kind": kind,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "duration_ms": round((end - start).total_seconds() * 1000, 2),
                "status": "error" if error else "ok",
                "error": error,
                "inputs": _clip(inputs),
                "outputs": _clip(outputs),
                "metadata": self._meta(payload),
            }
        )
        if self.tracer.langsmith_enabled and parent.get("ls_headers"):
            run_type = {"tool": "tool"}.get(kind, "chain")
            rt = self._ls_run(name, run_type, _clip(inputs), payload, parent["ls_headers"], start)
            rt.end(outputs=_clip(outputs), error=error, end_time=end)
            rt.post()

    # --- events ----------------------------------------------------------------------

    def handle(self, payload: dict) -> None:
        ev = payload.get("hook_event_name")
        now = _now()
        if ev == "SessionStart":
            self.session(payload)
        elif ev == "UserPromptSubmit":
            text = payload.get("prompt_text") or payload.get("prompt") or ""
            self._emit(
                name="user:prompt",
                kind="gate",
                parent=self.session(payload),
                start=now,
                end=now,
                inputs={"prompt": text},
                outputs={},
                payload=payload,
            )
        elif ev == "PreToolUse" and payload.get("tool_use_id"):
            self._put(f"tool-{payload['tool_use_id']}", {"start": now.isoformat()})
        elif ev in ("PostToolUse", "PostToolUseFailure"):
            started = self._pop(f"tool-{payload.get('tool_use_id')}") or {}
            start = datetime.fromisoformat(started["start"]) if "start" in started else now
            failed = ev == "PostToolUseFailure"
            err = (
                str(payload.get("error") or payload.get("tool_response") or "failed")
                if failed
                else None
            )
            self._emit(
                name=_tool_label(payload),
                kind="tool",
                parent=self._parent(payload),
                start=start,
                end=now,
                inputs=payload.get("tool_input") or {},
                outputs={"response": payload.get("tool_response")},
                payload=payload,
                error=_clip(err),
            )
        elif ev == "SubagentStart" and payload.get("agent_id"):
            parent = self.session(payload)
            rec = {
                "id": str(uuid.uuid4()),
                "trace_id": parent["trace_id"],
                "start": now.isoformat(),
                "parent": parent["id"],
                "type": payload.get("agent_type"),
            }
            if self.tracer.langsmith_enabled and parent.get("ls_headers"):
                rt = self._ls_run(
                    f"agent:{payload.get('agent_type')}",
                    "chain",
                    {},
                    payload,
                    parent["ls_headers"],
                    now,
                )
                rt.post()
                rec |= {"id": str(rt.id), "ls_headers": rt.to_headers()}
            self._put(f"agent-{payload['agent_id']}", rec)
        elif ev == "SubagentStop" and payload.get("agent_id"):
            rec = self._get(f"agent-{payload['agent_id']}")
            if not rec:
                return
            start = datetime.fromisoformat(rec["start"])
            out = {"last_message": payload.get("last_assistant_message")}
            self.tracer.write_record(
                {
                    "id": rec["id"],
                    "trace_id": rec["trace_id"],
                    "parent_id": rec["parent"],
                    "name": f"agent:{rec.get('type')}",
                    "kind": "agent",
                    "start": rec["start"],
                    "end": now.isoformat(),
                    "duration_ms": round((now - start).total_seconds() * 1000, 2),
                    "status": "ok",
                    "error": None,
                    "inputs": {},
                    "outputs": _clip(out),
                    "metadata": self._meta(payload, rec["id"] if rec.get("ls_headers") else None),
                }
            )
            if self.tracer.langsmith_enabled and rec.get("ls_headers"):
                self.tracer._ls_client.update_run(
                    rec["id"],
                    end_time=now,
                    outputs=_clip(out),
                    trace_id=rec["trace_id"],
                    dotted_order=rec["ls_headers"].get("langsmith-trace"),
                )
        elif ev == "Stop":
            rec = self.session(payload)
            start = datetime.fromisoformat(rec["start"])
            out = {"last_message": payload.get("last_assistant_message")}
            self.tracer.write_record(
                {
                    "id": rec["id"],
                    "trace_id": rec["trace_id"],
                    "parent_id": None,
                    "name": "claude:session",
                    "kind": "run",
                    "start": rec["start"],
                    "end": now.isoformat(),
                    "duration_ms": round((now - start).total_seconds() * 1000, 2),
                    "status": "ok",
                    "error": None,
                    "inputs": {},
                    "outputs": _clip(out),
                    "metadata": self._meta(payload, rec["id"] if rec.get("ls_headers") else None),
                }
            )
            if self.tracer.langsmith_enabled and rec.get("ls_headers"):
                self.tracer._ls_client.update_run(
                    rec["id"],
                    end_time=now,
                    outputs=_clip(out),
                    trace_id=rec["trace_id"],
                    dotted_order=rec["ls_headers"].get("langsmith-trace"),
                )


def run(stdin_text: str, ls_client: Any = None) -> None:
    """Entry for `ave hook`. Never raises."""
    try:
        load_env_file()
        payload = json.loads(stdin_text or "{}")
        cwd = Path(payload.get("cwd") or ".")
        file = find_project_file(cwd)
        if file is None:
            return
        project = load_project(file)
        if project.config.observability.provider == "none":
            return
        latest = RunStore(project.runs_dir).latest()
        if latest is None:
            return
        tracer = Tracer(
            project.config.observability,
            project.runs_dir / latest.id / "trace.jsonl",
            run_id=latest.id,
            ls_client=ls_client,
        )
        try:
            HookTracer(project, latest.id, tracer).handle(payload)
        finally:
            tracer.close()
    except (ConfigError, Exception) as e:  # noqa: BLE001 - a hook must not break Claude Code
        print(f"ave hook: {e}", file=sys.stderr)
