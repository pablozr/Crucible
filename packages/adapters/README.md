# @pablozrrrr/adapters

OpenCode V1 dispatch adapter. Sends one `input_candidate` event to the
loopback Core, waits for the durable admission outcome, then dispatches.

`event_id` is a fresh adapter UUID generated once per dispatch and echoed
by the Core. The native OpenCode message identity stays in `input_id`
and is never used as `event_id`. A success response whose `event_id`
is missing or differs from the sent value fails open as
`CORE_UNAVAILABLE`. Version `1.18.29` is rejected as
`INCOMPATIBLE_OPENCODE`; only exact `1.18.28` is attempted.

Terminal delivery uses the `TerminalOutbox` facade with separate SQLite
store, HTTP reconciliation/delivery, and timer controller modules. No
file fallback is implemented. Terminal ordering is proven only for the
restricted opt-in live profile below; it is not a universal terminal
contract.

## Normal tests

No live runtime required.

```sh
pnpm --filter @pablozrrrr/adapters test
pnpm --filter @pablozrrrr/adapters typecheck
pnpm --filter @pablozrrrr/adapters build
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

## Opt-in OC-V1-07 terminal ordering probe

Runs exact OpenCode `1.18.28` against a deterministic loopback
OpenAI-compatible provider. The provider requests the built-in `write`
tool once, observes the tool result and the written sentinel before its
final model turn, returns `stop`, and the probe reads the sentinel again
as soon as the synchronous `session.prompt` HTTP response returns.

The tested profile uses a fresh server/session/repository, `--pure`,
`snapshot: false`, `formatter: false`, `lsp: false`, explicit background
subagents disabled, and only the `edit` permission allowed. A PASS proves
the normal prompt return is after the final covered mutation for this
profile. It does not cover shell or detached processes, MCP, external
plugins, subagents, concurrent clients, error/abort paths, or arbitrary
filesystem writers.

Windows PowerShell (run from `packages/adapters`):

```powershell
$env:CRUCIBLE_OPENCODE_TERMINAL_LIVE = "1"
$env:CRUCIBLE_OPENCODE_BIN = "C:\ProgramData\chocolatey\bin\opencode.exe"
npx tsx --test test/opencode-v1-terminal-live.test.ts
```

Without `CRUCIBLE_OPENCODE_TERMINAL_LIVE=1`, this probe skips and normal
CI remains independent of a local OpenCode runtime.

## SOL-04 final-versus-next protocol prototype

Run the deterministic, runtime-independent concurrency proof with:

```powershell
pnpm probe:sol-04
```

The throwaway prototype lives under `test/support` rather than production
code. It proves two protocol interleavings: a timed-out next input fences the
old final-capture generation before being released untracked, so a late final
is discarded and no baseline is assigned; or the final capture wins and its
frozen evidence is reused as the next input's baseline. It does not prove
SQLite durability, process-crash recovery, or the future Core HTTP contract.
