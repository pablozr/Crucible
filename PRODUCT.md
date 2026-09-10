# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Local developers investigating one continuous coding-agent execution. They need
to understand what the agent changed, whether provenance capture succeeded, and
why a Task failed without leaving their local environment.

## Product Purpose

Crucible preserves durable local provenance for coding-agent work: admitted
Inputs, Task boundaries, Git evidence, final diffs, and terminal diagnostics.
The first dashboard makes that existing evidence readable through a Task list
and Task detail view.

## Positioning

Crucible attributes a continuous agent execution to frozen local Git evidence,
rather than treating each chat message or later repository state as the record
of work.

## Operating Context

The dashboard is a separate Angular application for local development. It
consumes the Core's loopback `/v1/tasks` and `/v1/tasks/{task_id}` APIs. Its
first user is a developer reviewing local agent activity; the Task detail shows
status, admitted Input IDs, evidence metadata, changed files, and loads the
stored Task Diff only when requested.

## Capabilities and Constraints

- The dashboard is read-only in this MVP.
- Task lists are cursor-paginated and must not load heavy evidence.
- Task Diff is detail evidence and is revealed on demand in the UI.
- The Core currently exposes only `/v1` APIs; Angular runs as a separate app.
- Supported OpenCode terminal behavior remains exact-version, restricted, and
  opt-in; the dashboard must not imply universal compatibility.
- No product dashboard existed before this work; do not fabricate metrics,
  users, performance claims, or operational data.

## Evidence on Hand

- Core task summary and detail response schemas are implemented in
  `core/src/crucible_core/schemas/admissions.py`.
- Core routes exist in
  `core/src/crucible_core/routes/admissions.py`.
- No visual assets, existing dashboard, or product analytics are available.

## Product Principles

- Preserve the distinction between Task, Input, event, and evidence.
- Make local provenance legible before adding product breadth.
- Surface failures plainly, with their recovery context.
- Keep expensive evidence deliberate and on demand.
- Read-only diagnostics must never mutate capture state.
