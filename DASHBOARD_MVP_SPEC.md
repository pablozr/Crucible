# Dashboard MVP Specification

## Goal

Provide a separate, read-only Angular dashboard for local developers to inspect
Crucible Tasks. It must make capture status, failure context, admitted Inputs,
file evidence, and the stored Task Diff legible without changing Core state.

## Scope

- Angular application in `packages/dashboard`, served separately in development.
- Routes: `/tasks` and `/tasks/:taskId`; `/` redirects to `/tasks`.
- Cursor-paginated Task list using `GET /v1/tasks`.
- Task detail using `GET /v1/tasks/{task_id}`.
- Task Diff is loaded only after an explicit user action.
- Local proxy sends `/v1` to `http://127.0.0.1:7331`; do not add permissive
  Core CORS for the development workflow.

## Core Contract

`GET /v1/tasks/{task_id}?include_diff=false` omits the stored `task_diff`
blob while preserving the existing detail response shape with `task_diff: null`.
The default remains `include_diff=true` for compatibility. The dashboard loads
detail with `include_diff=false`, then repeats the request with the default
only when the user selects **Load diff**.

## Information Architecture

The application follows the existing template convention:

```text
src/app/
  modules/
    global/                 # shell, shared layout, primitives and errors
    tasks/
      pages/                # list and detail route orchestrators
      components/           # task row, status badge, evidence/file views
      services/             # Core HTTP API
      interfaces/           # API contracts
```

Standalone Angular components and lazy route loading are required. Modules own
their API service, components and interfaces; `global` owns only reusable app
shell concerns. Do not introduce state libraries, SSR, a UI kit, polling, or
write operations.

## Design Direction

**Mesa de investigação**: a modern, minimalist dark operating surface. It is
for deliberate local investigation, not a generic admin template.

- Near-black graphite background with restrained cool-slate surfaces.
- One electric indigo/cyan accent for selection and primary actions; status
  colors communicate state, never decoration.
- A compact system-sans UI face; monospace only for hashes, paths, IDs, diff
  and measurements.
- Thin dividers, subtle elevation, tabular numerals, visible focus rings, and
  a clear selected-Task state.
- The desktop layout is a navigation/list rail plus an evidence detail pane;
  mobile stacks the same content without hiding state or actions.
- No invented metrics, charts, dashboard KPIs, user accounts, or telemetry.

## Required States

- Initial load, empty list, page-load failure and retry.
- List pagination without losing prior rows.
- Detail loading, Task-not-found, Core failure and retry.
- Running/finalizing/completed/failed status, with failure code/message shown
  prominently where present.
- Inputs, evidence completeness, baseline/final metadata, changed files, and
  evidence reasons. Missing values render as `Not recorded` or `Unavailable`.
- Diff closed by default, loading/error/empty states, and text-only rendering
  in an accessible scrollable `<pre>`.

## Acceptance Criteria

- Lists never request a detail per row and preserve opaque cursors unchanged.
- No diff is requested in the initial detail call; it is never rendered as HTML.
- Core error envelopes show their code and safe message.
- The API layer has typed nullability matching Core schemas.
- `pnpm dev:dashboard` serves the UI with the `/v1` proxy.
- Root typecheck, test and build include the dashboard.
- Tests cover API handling, list pagination/error, detail/not-found and
  on-demand diff behavior.
