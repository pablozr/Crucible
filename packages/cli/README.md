# @pablozrrrr/crucible-cli

Crucible CLI — local-first task-level code provenance for coding agents.

> **Prerelease.** Version `0.1.0` is not published to npm yet. Once published,
> install it with `npm install -g @pablozrrrr/crucible-cli`.

## Requirements

- Node.js `>=22.14.0`
- Python `3.12+` with `crucible-core` installed (required by `serve`)
- Git (required by `init` and task capture)

## Usage

```sh
crucible init /path/to/your/repository
crucible serve
crucible status [--json]
crucible dashboard
```

`serve` starts the local Core service at `http://127.0.0.1:7331` (APIs under
`/v1` only). The bundled dashboard is a separate app that proxies `/v1` to
the Core; it is not served by the Core itself.

## Platform runtimes

The CLI resolves a prebuilt `crucible-core` runtime from its optional
dependencies (`@pablozrrrr/crucible-core-win32-x64`, `@pablozrrrr/crucible-core-darwin-x64`,
`@pablozrrrr/crucible-core-darwin-arm64`, `@pablozrrrr/crucible-core-linux-x64-gnu`, glibc only on
Linux). Each runtime package carries a `runtime-manifest.json` pinning the
product version, target, executable, and sha256.

## License

MIT — see [LICENSE](./LICENSE).
