# @crucible/adapters

OpenCode V1 dispatch adapter. Sends one `input_candidate` event to the
loopback Core, waits for the durable admission outcome, then dispatches.

`event_id` is a fresh adapter UUID generated once per dispatch and echoed
by the Core. The native OpenCode message identity stays in `input_id`
and is never used as `event_id`. A success response whose `event_id`
is missing or differs from the sent value fails open as
`CORE_UNAVAILABLE`. Version `1.18.29` is rejected as
`INCOMPATIBLE_OPENCODE`; only exact `1.18.28` is attempted.

## Normal tests

No live runtime required.

```sh
pnpm --filter @crucible/adapters test
pnpm --filter @crucible/adapters typecheck
pnpm --filter @crucible/adapters build
```

## Opt-in live transport probe

Proves admission durability before real `1.18.28` dispatch, steer
attachment to the same active Task, and overlap exclusion, in one
isolated temporary Core/OpenCode pair. Each run uses fresh temporary
directories and sends `noReply: true`, which proves message transport
and native identity observation only. It does not prove tool
execution, terminal ordering, or task completion.

Prerequisites: exact `opencode 1.18.28` binary, Python with the Core
dependencies installed, `git`, and free loopback ports. No providers,
models, or installed-config edits are used.

Windows PowerShell (run from `packages/adapters`):

```powershell
$env:CRUCIBLE_OPENCODE_LIVE = "1"
$env:CRUCIBLE_OPENCODE_BIN = "C:\ProgramData\chocolatey\bin\opencode.exe"
npx tsx --test test/opencode-v1-live.test.ts
```

Without `CRUCIBLE_OPENCODE_LIVE=1` the live file skips and normal CI
stays independent of local runtimes.
