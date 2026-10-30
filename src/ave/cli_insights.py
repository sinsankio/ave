"""`ave trace` (span tree of a run, LangSmith links) and `ave cost` (token usage per agent)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.tree import Tree

from ave import tracetree, usage
from ave.cli import EXIT_USAGE, _ctx, app, console, err_console
from ave.pipeline import PipelineError, RunContext, open_run

RunOpt = Annotated[str | None, typer.Option("--run", help="Run id; default: latest.")]
JsonFlag = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
SAME_NAME_SHOWN = 3  # siblings with the same name shown before collapsing the rest


def _rc(ctx: typer.Context, run_id: str | None) -> RunContext:
    try:
        return open_run(_ctx(ctx).project(), run_id)
    except PipelineError as e:
        err_console.print(f"[red]error:[/] {e}")
        raise typer.Exit(EXIT_USAGE) from e


_KIND_STYLE = {
    "run": "bold",
    "phase": "bold cyan",
    "agent": "magenta",
    "tool": "",
    "gate": "yellow",
    "llm": "green",
}


def _label(node: tracetree.Node) -> str:
    r = node.rec
    style = _KIND_STYLE.get(r["kind"], "")
    raw = escape(r["name"])  # names like tool:Bash[ave edit] are not Rich markup
    name = f"[{style}]{raw}[/]" if style else raw
    dur = tracetree.fmt_ms(r.get("duration_ms"))
    err = escape(str(r.get("error") or "").splitlines()[0][:80]) if r.get("error") else ""
    mark = f" [red]✗ {err}[/]" if r.get("status") == "error" else ""
    out = r.get("outputs") or {}
    extra = ""
    if r["kind"] == "gate" and out.get("comment"):
        extra = f" [dim]“{escape(str(out['comment'])[:60])}”[/]"
    return f"{name} [dim]{dur}[/]{mark}{extra}"


def _add(tree: Tree, node: tracetree.Node, depth: int) -> None:
    branch = tree.add(_label(node))
    if depth <= 1:
        if node.children:
            branch.add(f"[dim]… {len(node.children)} child spans (--depth to expand)[/]")
        return
    counts = Counter(c.name for c in node.children)
    shown: Counter[str] = Counter()
    for child in node.children:
        shown[child.name] += 1
        if shown[child.name] <= SAME_NAME_SHOWN or child.rec.get("status") == "error":
            _add(branch, child, depth - 1)
        elif shown[child.name] == counts[child.name]:
            hidden = [c for c in node.children if c.name == child.name][SAME_NAME_SHOWN:]
            total = sum(c.rec.get("duration_ms") or 0 for c in hidden)
            branch.add(f"[dim]… {len(hidden)} more {child.name} ({tracetree.fmt_ms(total)})[/]")


def _ls_links(rc: RunContext, roots: list[tracetree.Node]) -> list[tuple[str, str]]:
    cfg = rc.project.config.observability
    if cfg.provider != "langsmith":
        return []
    import os

    if not os.environ.get("LANGSMITH_API_KEY"):
        err_console.print("[yellow]LANGSMITH_API_KEY not set; no links[/]")
        return []
    import langsmith

    client = langsmith.Client()
    links = []
    for root in roots:
        run_id = root.rec.get("metadata", {}).get("ls_run_id")
        if not run_id:
            continue
        try:
            run = client.read_run(run_id)
            links.append((root.name, client.get_run_url(run=run, project_name=cfg.project)))
        except Exception as e:  # noqa: BLE001 - a missing run must not hide the others
            links.append((root.name, f"(not found: {e.__class__.__name__})"))
    return links


@app.command(name="trace")
def trace_cmd(
    ctx: typer.Context,
    run_id: RunOpt = None,
    depth: Annotated[int, typer.Option(min=1, max=20, help="Levels to expand.")] = 3,
    links: Annotated[bool, typer.Option(help="Look up LangSmith links of each root span.")] = False,
    errors_only: Annotated[bool, typer.Option("--errors", help="Only failed spans.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Span tree of a run: phases, agents, tools and gate decisions, with durations."""
    rc = _rc(ctx, run_id)
    roots = tracetree.build(rc.dir / "trace.jsonl")
    if not roots:
        console.print(
            f"no trace for run {rc.state.id} (observability.provider is "
            f"{rc.project.config.observability.provider})"
        )
        return
    if errors_only:
        failed = tracetree.errors(roots)
        if as_json:
            print(json.dumps([n.to_dict(1) for n in failed], indent=2))
        else:
            for n in failed:
                console.print(f"[red]✗[/] {n.name}: {n.rec.get('error')}", highlight=False)
            if not failed:
                console.print("[green]no failed spans[/]")
        return
    found = _ls_links(rc, roots) if links else []
    if as_json:
        print(
            json.dumps(
                {
                    "roots": [r.to_dict(depth) for r in roots],
                    "links": [{"span": s, "url": u} for s, u in found],
                },
                indent=2,
            )
        )
        return
    tree = Tree(f"[bold]run {rc.state.id}[/] · {rc.state.status.value}")
    for root in roots:
        _add(tree, root, depth)
    console.print(tree)
    for span, url in found:
        console.print(f"LangSmith {span}: {url}", highlight=False)
    if rc.project.config.observability.provider == "langsmith" and not links:
        console.print(
            f"[dim]LangSmith project '{rc.project.config.observability.project}', "
            f"thread {rc.state.id} (`--links` for direct links)[/]"
        )


@app.command(name="cost")
def cost_cmd(
    ctx: typer.Context,
    run_id: RunOpt = None,
    session: Annotated[
        list[Path] | None,
        typer.Option("--session", help="Extra Claude Code transcript (.jsonl) to include."),
    ] = None,
    as_json: JsonFlag = False,
) -> None:
    """Token usage per agent and model, from the Claude Code sessions that worked on the run."""
    rc = _rc(ctx, run_id)
    sessions = usage.find_sessions(rc.project.root, rc.state.id, rc.dir)
    sessions += [s for s in (session or []) if s not in sessions]
    rows = usage.run_usage(sessions)
    total = usage.Usage()
    for r in rows:
        total.merge(r.usage)
    if as_json:
        print(
            json.dumps(
                {
                    "sessions": [str(s) for s in sessions],
                    "rows": [r.to_dict() for r in rows],
                    "total": usage.asdict(total),
                },
                indent=2,
            )
        )
        return
    if not rows:
        console.print(
            f"no Claude Code sessions found for run {rc.state.id} "
            f"(looked in {usage.claude_projects_dir() / usage.project_slug(rc.project.root)})"
        )
        return

    def k(n: int) -> str:
        return f"{n / 1000:,.1f}k" if n >= 1000 else str(n)

    def money(c: float | None) -> str:
        return "?" if c is None else f"${c:,.2f}"

    table = Table(
        "agent", "model", "calls", "input", "cache read", "cache write", "output", "≈ API $"
    )
    for r in rows:
        u = r.usage
        table.add_row(
            r.agent,
            r.model,
            str(u.messages),
            k(u.input),
            k(u.cache_read),
            k(u.cache_write_5m + u.cache_write_1h),
            k(u.output),
            money(u.cost),
        )
    table.add_row(
        "[bold]total[/]",
        "",
        str(total.messages),
        k(total.input),
        k(total.cache_read),
        k(total.cache_write_5m + total.cache_write_1h),
        k(total.output),
        f"[bold]{money(total.cost)}[/]",
        end_section=True,
    )
    console.print(table)
    console.print(
        f"[dim]{len(sessions)} session(s). On a subscription you pay for the plan; "
        "the $ column is what the same tokens would cost on the API.[/]"
    )
