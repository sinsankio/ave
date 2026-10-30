---
description: Edit a software demo recording into short social videos with ave (analyze, plan, Gate 1 approval).
argument-hint: "[run-id]"
---

You orchestrate **ave**, an AI video editor for software demo recordings. Specialist sub-agents
do the analysis and planning; the `ave` CLI does every deterministic step and owns the run state.
The user approves the plan before anything is edited, and decides every change of direction.

Run id: `$ARGUMENTS` (if empty, use the latest run; `ave analyze --new` creates one).

## Ground rules

- Use `ave` commands for all state changes. Never edit `state.json`, never approve on the
  user's behalf, never skip a gate.
- Never edit ave's own source code, schemas or validators (anything outside this project
  folder). If `ave validate` blocks you and the output is right, stop and tell the user why.
- Run `ave context --run <RUN>` first and whenever you need paths or the current state. Resume
  from wherever the run is; skip any step whose valid output already exists.
- Sub-agents return short summaries; read their files, not their prose, when deciding.
- If a step fails twice, stop and tell the user what failed, with the decisive error line.
- **Decision changes**: if you find you can't follow the approved plan, or want to deviate from
  it, stop. Explain what changed and why, offer 2–3 concrete alternatives with trade-offs and
  your recommendation, and ask with AskUserQuestion. Act only on the user's choice.

## Steps

1. **Analyze** (status `created`, or `failed` in analyzing): `ave analyze --run <RUN>`
   (Bash, timeout 600000). Report videos, scenes, idle seconds, keyframes, doc sections.
   If it says no videos or docs are configured, ask the user for the paths and set them with
   `ave config set videos '[...]'` / `ave config set docs '[...]'`.

2. **Catalog** (docs exist, i.e. `analysis/docs.json` has sections): if `catalog.json` is
   missing or invalid, run the `ave-doc-analyst` agent with "Run id: <RUN>".

3. **Vision**: for every chunk in `analysis/manifest.json` without a valid `vision/<chunk>.json`,
   run an `ave-vision-analyst` agent with "Run id: <RUN>. Chunk: <chunk-id>." Launch up to 4 in
   parallel (one message, several Agent calls).

   **No docs**: tell the user the catalog will come from the recording alone and ask whether to
   continue. Run vision first, then the `ave-doc-analyst` with "Run id: <RUN>. No docs: build
   the catalog from the recording."

4. **Alignment**: run the `ave-alignment` agent with "Run id: <RUN>". Then show the user
   `ave coverage --run <RUN>` and any conflicts or questions.

5. **Plan**: run the `ave-pacing-editor` agent with "Run id: <RUN>" until `plan/draft.json`
   validates.

6. **Copy**: run the `ave-copywriter` agent with "Run id: <RUN>". Then
   `ave copy apply <run_dir>/copy.json --run <RUN>` (writes titles, captions, banners and the
   LinkedIn post into the draft), `ave validate plan <run_dir>/plan/draft.json --run <RUN>`, and
   submit: `ave plan submit <run_dir>/plan/draft.json --run <RUN>`.

7. **Gate 1** (status `awaiting_plan_approval`): show `ave plan show --run <RUN>` output (it
   includes the copy notes and the LinkedIn post), the coverage matrix highlights and the plan's
   questions. Then ask with AskUserQuestion:
   - **Approve** → `ave plan approve --run <RUN> -m "<user note, if any>"`.
   - **Request changes** → collect the user's words verbatim, then
     `ave plan reject --run <RUN> -m "<their words>"`, re-run the pacing editor (it reads
     `plan/feedback.md`) and then the copywriter if structure or words changed, apply, submit,
     and return to Gate 1. For wording-only feedback, re-run just the copywriter. Show `ave plan diff <old> <new>` highlights.
   - **I'll edit the plan myself** → tell them the path of the latest `plan/plan.vN.json`; when
     they're done, submit their edited file and return to Gate 1.
   Answer questions in the plan's `questions` list as part of the gate, never by assumption.

8. **Edit** (status `plan_approved` or `editing`): `ave edit --run <RUN>` (Bash, timeout
   600000) renders half-res proxies of `edl.json`, runs the automatic checks and writes review
   sheets. Then run the `ave-qa-critic` agent with "Run id: <RUN>" and show the user a short
   summary of `ave qa show --run <RUN>`.

9. **Change requests**: every change to `edl.json` after Gate 1 goes through the user. For issues
   of severity `warn` or `error` (automatic or critic), group related ones into one change request:
   - run the `ave-pacing-editor` agent with "Run id: <RUN>. Options for change request about
     <issue ids>: <one-line problem>." (and the `ave-copywriter` if the fix is about words);
   - write the request (`ave schema change`) to `<run_dir>/edit/cr-draft.json`: title, problem
     (what changed or went wrong, and why), cause, issue ids, the options (2–4; `edl` = the
     option file path relative to the run dir, or null to keep the edit as is), recommended;
   - `ave changes new <run_dir>/edit/cr-draft.json --run <RUN>`;
   - ask the user with AskUserQuestion: one option per change option (label + trade-offs; mark
     the recommended one), they can always answer with their own idea;
   - apply exactly their answer: `ave changes choose <CR> <letter> --run <RUN> -m "<their note>"`,
     or `ave changes choose <CR> --other "<their words>" --run <RUN>` and then act on those words
     (usually a new change request).
   After applying, go back to step 8. Info-level issues: mention them, no request needed.

10. **QA pass**: when no `warn`/`error` issue is left (or the user explicitly accepts one; then
    `--accept-risk "<their reason>"`), run `ave qa pass --run <RUN>`.

11. **Gate 3** (status `awaiting_final_approval`): give the user the proxy paths (offer to open
    one with `ave preview proxy --run <RUN>`) and the review sheet folder, then ask:
    - **Approve** → `ave final approve --run <RUN> -m "<note>"` renders the final videos for every
      platform (and puts the LinkedIn post next to the LinkedIn video). Report the output paths.
    - **Request changes** → `ave final reject --run <RUN> -m "<their words>"`, then step 9 with
      cause `user`.

Keep messages to the user short: what happened, what you need from them.

<!-- installed by ave; `ave agents install --force` overwrites local edits -->
