# Contributing to ave

Thanks for stopping by! `ave` turns software demo recordings into short social videos, and it is young enough that one good contribution can change what it does. If you have ever cut a demo by hand and thought "this should be easier", you already have the right background.

You don't need to know video editing or Claude Code to help. The project is plain Python, the agents are Markdown files, and there are small jobs and big ones.

## Ways to contribute

- **Try it on a real demo.** Run `ave` on your own recording and tell us where it got the edit wrong. Reports from real videos are the most useful thing you can send.
- **Fix bugs.** Anything `ave doctor`, a render or a gate gets wrong.
- **Improve the agents.** The prompts are in [`src/ave/assets/claude/`](src/ave/assets/claude/). A better prompt can improve every edit.
- **Add QA checks.** If you spot a flaw a human editor would catch, teach [`qa.py`](src/ave/qa.py) to catch it.
- **Improve the docs.** If a step confused you, it will confuse the next person too.

### Good places to start

These are known gaps, and any of them would be a welcome pull request:

- **`api` inference backend.** `inference.backend: api` is accepted and `ave doctor` checks the credentials, but nothing runs the agents through the Anthropic API yet.
- **Background music.** The pipeline has no support for a music bed, including ducking under speech and mixing it into the loudness-normalized export.
- **Other platforms.** Presets live in `PRESETS` in [`config.py`](src/ave/config.py). YouTube Shorts or TikTok would be a natural next step.
- **macOS and Windows.** CI only covers Ubuntu, so we need to hear what breaks elsewhere.
- **A stale `doctor` message.** It still says Playwright is needed for banners, but they now render with Pillow. This one is a nice first fix.

Have a different idea? Please bring it. For anything large, open an issue first so we can agree on the shape before you spend time on it.

## Setting up

You need [uv](https://docs.astral.sh/uv/), Python 3.13 or later, and [ffmpeg](https://ffmpeg.org/) 6 or later with `ffprobe`. [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) is only needed if you want to run the agents end to end.

```bash
git clone <repository-url>
cd ai-video-editor
uv sync --extra ocr      # includes the dev tools; OCR models download on first use
uv run ave doctor
```

## Finding your way around

`ave` itself is deterministic: it analyzes, validates, renders and exports, but never reasons. All judgment lives in the Claude Code agents, which talk to `ave` through its commands. Keeping that line clear is the most important design rule in the project.

| Where | What is there |
| --- | --- |
| `src/ave/analysis/` | Signals, keyframes, OCR, doc sections and evidence. No LLM calls |
| `src/ave/edl.py` | The edit decision list, the single description of an edit |
| `src/ave/render.py`, `camera.py`, `overlays.py` | EDL to video: clips, virtual camera, text and banners |
| `src/ave/qa.py` | Deterministic checks of an edit, per platform |
| `src/ave/runs.py` | Run state machine and approval gates |
| `src/ave/pipeline.py`, `editing.py` | Run-level steps, from analysis to the final gate |
| `src/ave/schemas.py` | Contracts for everything agents write |
| `src/ave/observability.py`, `hooks.py`, `tracetree.py`, `usage.py` | Traces and token usage |
| `src/ave/cli*.py`, `shell.py` | Command line and interactive shell |
| `src/ave/assets/claude/` | Agent and `/ave-edit` templates |

> [!IMPORTANT]
> Edit agents in `src/ave/assets/claude/`, not in `.claude/`. The `.claude/` files are rendered from those templates by `ave agents install`, so changes made there never reach other users.

## Making a change

1. Create a branch from the default branch.
2. Make your change, and keep it focused on one thing.
3. Add or update tests.
4. Update [`README.md`](README.md) if behavior or commands changed.
5. Run the checks below, then open a pull request that says what changed and why. For anything visible in a video, a screenshot or short clip helps a lot.

### Running the checks

CI runs all of these on every pull request:

```bash
uv run pytest -m "not ffmpeg and not ocr"   # fast unit tests
uv run pytest -m "ffmpeg or ocr"            # real ffmpeg and OCR
uv run ruff check && uv run ruff format --check
```

### Tests

- Tests that should keep running belong in `tests/` and run in CI. Scratch scripts belong in `tmp/`, which is gitignored.
- Tests get a throwaway `HOME` and no credentials, so they must not need network access or a real API key.
- Mark tests that need real binaries or models with `@pytest.mark.ffmpeg` or `@pytest.mark.ocr`.
- Don't commit video fixtures. [`tests/synth.py`](tests/synth.py) generates a synthetic recording with known events, so you can extend it and assert on exact timings.

### Code style

Ruff handles formatting and linting, with a 100-character line length. Beyond that, match the code around you: short docstrings that explain why, pydantic models for anything that crosses a file boundary, and no LLM calls inside `ave` itself.

### Changing what agents write

If you change an agent's output, update the three places that have to agree: the template in `src/ave/assets/claude/`, its schema in [`schemas.py`](src/ave/schemas.py), and any cross-file rules behind `ave validate`.

The approval gates are the core promise of this tool, so please don't add a path that lets an edit or an export skip one.

## Reporting a bug

Please include:

- What you ran and what you expected
- Your `ave --version` and the output of `ave doctor`
- For a run problem, `ave status` and `ave trace --errors`

> [!CAUTION]
> `ave doctor` and traces can reveal local paths, and recordings can show private data. Check them before pasting, and never post API keys or `~/.config/ave/env`. If you find a security problem, please contact the maintainer privately instead of opening a public issue.

## Thank you

Every issue, fix and "this confused me" makes `ave` better for the next person. We're glad you're here.
