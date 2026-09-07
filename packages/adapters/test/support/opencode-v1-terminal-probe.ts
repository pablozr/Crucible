import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { createServer as createNetServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";

const SUPPORTED_VERSION = "1.18.28";
const STARTUP_TIMEOUT_MS = 20_000;
const PROMPT_TIMEOUT_MS = 30_000;
const STOP_TIMEOUT_MS = 5_000;
const SENTINEL_CONTENT = "oc-v1-07-mutation-complete\n";

interface ProbeOptions {
  binary: string;
}

export interface TerminalProbeObservation {
  opencodeVersion: string;
  toolName: "write";
  mainModelTurns: number;
  toolResultObservedByProvider: boolean;
  mutationObservedBeforeFinalModelTurn: boolean;
  sentinelContentAtPromptReturn: string;
  promptFinish: string | undefined;
}

interface ProviderState {
  mainModelTurns: number;
  toolResultObservedByProvider: boolean;
  mutationObservedBeforeFinalModelTurn: boolean;
}

interface OpenAiCompatibleRequest {
  model?: string;
  messages?: Array<{ role?: string }>;
  tools?: Array<{ function?: { name?: string } }>;
}

function freePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createNetServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => {
        if (address && typeof address === "object") {
          resolvePort(address.port);
          return;
        }
        reject(new Error("NO_FREE_PORT"));
      });
    });
  });
}

async function requestBody(request: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString("utf8");
}

function streamChunk(
  response: ServerResponse,
  body: Record<string, unknown>,
): void {
  response.write(`data: ${JSON.stringify(body)}\n\n`);
}

function finishStream(response: ServerResponse): void {
  response.end("data: [DONE]\n\n");
}

function completionChunk(input: {
  id: string;
  model: string;
  delta: Record<string, unknown>;
  finishReason: "stop" | "tool_calls";
}): Record<string, unknown> {
  return {
    id: input.id,
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1000),
    model: input.model,
    choices: [
      {
        index: 0,
        delta: input.delta,
        finish_reason: input.finishReason,
      },
    ],
    usage: {
      prompt_tokens: 1,
      completion_tokens: 1,
      total_tokens: 2,
    },
  };
}

function respondWithText(
  response: ServerResponse,
  model: string,
  content: string,
): void {
  response.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });
  streamChunk(
    response,
    completionChunk({
      id: `chatcmpl_${randomUUID()}`,
      model,
      delta: { role: "assistant", content },
      finishReason: "stop",
    }),
  );
  finishStream(response);
}

function respondWithWriteCall(
  response: ServerResponse,
  model: string,
  sentinelPath: string,
): void {
  response.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });
  streamChunk(
    response,
    completionChunk({
      id: `chatcmpl_${randomUUID()}`,
      model,
      delta: {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            index: 0,
            id: "call_oc_v1_07_write",
            type: "function",
            function: {
              name: "write",
              arguments: JSON.stringify({
                filePath: sentinelPath,
                content: SENTINEL_CONTENT,
              }),
            },
          },
        ],
      },
      finishReason: "tool_calls",
    }),
  );
  finishStream(response);
}

async function startProvider(
  port: number,
  sentinelPath: string,
  state: ProviderState,
): Promise<ReturnType<typeof createServer>> {
  const server = createServer((request, response) => {
    void (async () => {
      if (request.method === "GET" && request.url === "/v1/models") {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(
          JSON.stringify({
            object: "list",
            data: [{ id: "terminal-probe", object: "model" }],
          }),
        );
        return;
      }

      if (request.method !== "POST" || request.url !== "/v1/chat/completions") {
        response.writeHead(404, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: "NOT_FOUND" }));
        return;
      }

      const payload = JSON.parse(
        await requestBody(request),
      ) as OpenAiCompatibleRequest;
      const model = payload.model ?? "terminal-probe";
      const hasWriteTool =
        payload.tools?.some((tool) => tool.function?.name === "write") ?? false;

      // OpenCode may make auxiliary title/summary calls. They do not receive
      // the write tool and are kept outside the two model turns under test.
      if (!hasWriteTool) {
        respondWithText(response, model, "OC V1 terminal probe");
        return;
      }

      state.mainModelTurns += 1;
      const hasToolResult =
        payload.messages?.some((message) => message.role === "tool") ?? false;
      if (!hasToolResult) {
        respondWithWriteCall(response, model, sentinelPath);
        return;
      }

      state.toolResultObservedByProvider = true;
      state.mutationObservedBeforeFinalModelTurn =
        existsSync(sentinelPath) &&
        readFileSync(sentinelPath, "utf8") === SENTINEL_CONTENT;
      respondWithText(response, model, "terminal probe complete");
    })().catch((error: unknown) => {
      if (!response.headersSent) {
        response.writeHead(500, { "content-type": "application/json" });
      }
      response.end(
        JSON.stringify({ error: (error as Error)?.message ?? String(error) }),
      );
    });
  });

  await new Promise<void>((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolveListen);
  });
  return server;
}

async function closeServer(server: ReturnType<typeof createServer>): Promise<void> {
  if (!server.listening) {
    return;
  }
  await new Promise<void>((resolveClose, reject) => {
    server.close((error) => (error ? reject(error) : resolveClose()));
    server.closeAllConnections();
  });
}

async function waitForHealth(url: string): Promise<void> {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, {
        signal: AbortSignal.timeout(2_000),
      });
      if (response.ok) {
        return;
      }
    } catch {
      // The process is still starting.
    }
    await delay(200);
  }
  throw new Error(`STARTUP_TIMEOUT: ${url}`);
}

function isChildAlive(child: ChildProcess): boolean {
  return child.exitCode === null && child.signalCode === null;
}

function waitForChildExit(child: ChildProcess): Promise<boolean> {
  if (!isChildAlive(child)) {
    return Promise.resolve(true);
  }
  return new Promise((resolveExit) => {
    const timer = setTimeout(() => resolveExit(false), STOP_TIMEOUT_MS);
    const done = () => {
      clearTimeout(timer);
      resolveExit(true);
    };
    child.once("exit", done);
    child.once("close", done);
    child.once("error", done);
  });
}

async function stopOwnedChild(child: ChildProcess | undefined): Promise<void> {
  if (!child || !isChildAlive(child) || child.pid === undefined) {
    return;
  }
  if (process.platform === "win32") {
    try {
      execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
        timeout: STOP_TIMEOUT_MS,
        stdio: "ignore",
      });
    } catch (error) {
      if (isChildAlive(child)) {
        throw error;
      }
      return;
    }
  } else {
    child.kill("SIGTERM");
  }
  if (!(await waitForChildExit(child))) {
    if (process.platform !== "win32") {
      child.kill("SIGKILL");
      if (await waitForChildExit(child)) {
        return;
      }
    }
    throw new Error(`CLEANUP_TIMEOUT: process ${child.pid}`);
  }
}

function removeOwnedTempDirectory(path: string, prefix: string): void {
  const absolute = resolve(path);
  if (dirname(absolute) !== resolve(tmpdir()) || !basename(absolute).startsWith(prefix)) {
    throw new Error(`REFUSED_TEMP_CLEANUP: ${absolute}`);
  }
  rmSync(absolute, { recursive: true, force: true });
}

export async function runOpenCodeV1TerminalProbe(
  options: ProbeOptions,
): Promise<TerminalProbeObservation> {
  let version = "";
  try {
    version = execFileSync(options.binary, ["--version"], {
      encoding: "utf8",
      timeout: 10_000,
    }).trim();
  } catch {
    throw new Error(`OPENCODE_BINARY_UNAVAILABLE: ${options.binary}`);
  }
  if (version !== SUPPORTED_VERSION) {
    throw new Error(`INCOMPATIBLE_OPENCODE: expected ${SUPPORTED_VERSION}, got ${version}`);
  }

  const repo = mkdtempSync(join(tmpdir(), "crucible-terminal-repo-"));
  const configRoot = mkdtempSync(join(tmpdir(), "crucible-terminal-conf-"));
  const dataRoot = mkdtempSync(join(tmpdir(), "crucible-terminal-data-"));
  const stateRoot = mkdtempSync(join(tmpdir(), "crucible-terminal-state-"));
  const cacheRoot = mkdtempSync(join(tmpdir(), "crucible-terminal-cache-"));
  const sentinelPath = join(repo, "oc-v1-07-sentinel.txt");
  const configPath = join(configRoot, "opencode-probe.json");
  const state: ProviderState = {
    mainModelTurns: 0,
    toolResultObservedByProvider: false,
    mutationObservedBeforeFinalModelTurn: false,
  };

  let provider: ReturnType<typeof createServer> | undefined;
  let opencode: ChildProcess | undefined;
  let opencodeLogs = "";
  let bodyError: unknown;

  try {
    execFileSync("git", ["init", "--quiet", repo], { timeout: 10_000 });
    const providerPort = await freePort();
    const opencodePort = await freePort();
    provider = await startProvider(providerPort, sentinelPath, state);

    writeFileSync(
      configPath,
      JSON.stringify({
        $schema: "https://opencode.ai/config.json",
        snapshot: false,
        formatter: false,
        lsp: false,
        permission: {
          "*": "deny",
          edit: "allow",
        },
        provider: {
          "crucible-probe": {
            npm: "@ai-sdk/openai-compatible",
            name: "Crucible deterministic probe",
            options: {
              apiKey: "local-probe-key",
              baseURL: `http://127.0.0.1:${providerPort}/v1`,
            },
            models: {
              "terminal-probe": {
                name: "Terminal probe",
                tool_call: true,
                limit: { context: 32_000, output: 1_000 },
              },
            },
          },
        },
      }),
    );

    opencode = spawn(
      options.binary,
      ["serve", "--pure", "--port", String(opencodePort), "--hostname", "127.0.0.1"],
      {
        env: {
          ...process.env,
          OPENCODE_CONFIG: configPath,
          OPENCODE_DISABLE_AUTOUPDATE: "1",
          OPENCODE_EXPERIMENTAL: "false",
          OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS: "false",
          XDG_CONFIG_HOME: configRoot,
          XDG_DATA_HOME: dataRoot,
          XDG_STATE_HOME: stateRoot,
          XDG_CACHE_HOME: cacheRoot,
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    const appendLog = (chunk: Buffer) => {
      opencodeLogs = (opencodeLogs + chunk.toString("utf8")).slice(-12_000);
    };
    opencode.stdout?.on("data", appendLog);
    opencode.stderr?.on("data", appendLog);

    const opencodeUrl = `http://127.0.0.1:${opencodePort}`;
    await waitForHealth(`${opencodeUrl}/global/health`);

    const sessionResponse = await fetch(`${opencodeUrl}/session`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-opencode-directory": repo,
      },
      body: JSON.stringify({ title: "OC-V1-07 terminal probe" }),
      signal: AbortSignal.timeout(PROMPT_TIMEOUT_MS),
    });
    if (!sessionResponse.ok) {
      throw new Error(`SESSION_CREATE_FAILED ${sessionResponse.status}: ${await sessionResponse.text()}`);
    }
    const session = (await sessionResponse.json()) as { id?: string };
    if (!session.id?.startsWith("ses_")) {
      throw new Error(`INVALID_SESSION_RESPONSE: ${JSON.stringify(session)}`);
    }

    const promptResponse = await fetch(
      `${opencodeUrl}/session/${session.id}/message`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-opencode-directory": repo,
        },
        body: JSON.stringify({
          model: {
            providerID: "crucible-probe",
            modelID: "terminal-probe",
          },
          parts: [
            {
              type: "text",
              text: "Use the write tool once, then finish.",
            },
          ],
        }),
        signal: AbortSignal.timeout(PROMPT_TIMEOUT_MS),
      },
    );
    if (!promptResponse.ok) {
      throw new Error(`PROMPT_FAILED ${promptResponse.status}: ${await promptResponse.text()}`);
    }
    const prompt = (await promptResponse.json()) as {
      info?: { finish?: string };
    };

    return {
      opencodeVersion: version,
      toolName: "write",
      mainModelTurns: state.mainModelTurns,
      toolResultObservedByProvider: state.toolResultObservedByProvider,
      mutationObservedBeforeFinalModelTurn: state.mutationObservedBeforeFinalModelTurn,
      sentinelContentAtPromptReturn: existsSync(sentinelPath)
        ? readFileSync(sentinelPath, "utf8")
        : "",
      promptFinish: prompt.info?.finish,
    };
  } catch (error) {
    bodyError = error;
    const detail = opencodeLogs.trim();
    if (detail) {
      throw new Error(`${(error as Error)?.message ?? String(error)}\nOpenCode logs:\n${detail}`);
    }
    throw error;
  } finally {
    const cleanupErrors: string[] = [];
    try {
      await stopOwnedChild(opencode);
    } catch (error) {
      cleanupErrors.push((error as Error)?.message ?? String(error));
    }
    try {
      if (provider) {
        await closeServer(provider);
      }
    } catch (error) {
      cleanupErrors.push((error as Error)?.message ?? String(error));
    }
    for (const [path, prefix] of [
      [repo, "crucible-terminal-repo-"],
      [configRoot, "crucible-terminal-conf-"],
      [dataRoot, "crucible-terminal-data-"],
      [stateRoot, "crucible-terminal-state-"],
      [cacheRoot, "crucible-terminal-cache-"],
    ] as const) {
      try {
        removeOwnedTempDirectory(path, prefix);
      } catch (error) {
        cleanupErrors.push((error as Error)?.message ?? String(error));
      }
    }
    if (!bodyError && cleanupErrors.length > 0) {
      throw new Error(`CLEANUP_FAILED: ${cleanupErrors.join("; ")}`);
    }
    if (bodyError && cleanupErrors.length > 0) {
      console.error(`OC-V1-07 cleanup failures: ${cleanupErrors.join("; ")}`);
    }
  }
}
