import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import { createServer, request as httpRequest, type Server } from "node:http";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { runDashboard, resolveDashboardDir } from "../src/commands/dashboard.js";
import {
  createDashboardHandler,
  startDashboardServer,
} from "../src/dashboard/server.js";


function makeAssets(): string {
  const dir = mkdtempSync(join(tmpdir(), "crucible-dashboard-"));
  writeFileSync(join(dir, "index.html"), "<!doctype html><title>dash</title>\n");
  writeFileSync(join(dir, "app.js"), "console.log(1);\n");
  writeFileSync(join(dir, "style.css"), "body{}\n");
  return dir;
}

function listenEphemeral(handler: (req: any, res: any) => void): Promise<{ server: Server; port: number }> {
  const server = createServer(handler);
  return new Promise((resolveStarted, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      assert.ok(address && typeof address === "object");
      resolveStarted({ server, port: (address as { port: number }).port });
    });
  });
}

function rawRequest(port: number, path: string, method = "GET", body?: string): Promise<{ status: number; headers: any; text: string }> {
  return new Promise((resolveReq, reject) => {
    const req = httpRequest({ hostname: "127.0.0.1", port, path, method }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (chunk) => chunks.push(chunk as Buffer));
      res.on("end", () => {
        resolveReq({ status: res.statusCode ?? 0, headers: res.headers, text: Buffer.concat(chunks).toString("utf8") });
      });
    });
    req.on("error", reject);
    if (body !== undefined) {
      req.write(body);
    }
    req.end();
  });
}

test("serves static files with explicit MIME", async (t) => {
  const assetsDir = makeAssets();
  const { server, port } = await listenEphemeral(createDashboardHandler(assetsDir, "http://127.0.0.1:7331"));
  t.after(() => server.close());

  const js = await rawRequest(port, "/app.js");
  assert.equal(js.status, 200);
  assert.match(String(js.headers["content-type"] ?? ""), /javascript/);
  assert.ok(js.text.includes("console.log"));

  const css = await rawRequest(port, "/style.css");
  assert.equal(css.status, 200);
  assert.match(String(css.headers["content-type"] ?? ""), /css/);
});

test("SPA fallback returns index.html for non-v1 routes", async (t) => {
  const assetsDir = makeAssets();
  const { server, port } = await listenEphemeral(createDashboardHandler(assetsDir, "http://127.0.0.1:7331"));
  t.after(() => server.close());

  const res = await rawRequest(port, "/some/client/route");
  assert.equal(res.status, 200);
  assert.match(String(res.headers["content-type"] ?? ""), /html/);
  assert.equal(res.text, readFileSync(join(assetsDir, "index.html"), "utf8"));
});

test("denies path traversal", async (t) => {
  const assetsDir = makeAssets();
  const { server, port } = await listenEphemeral(createDashboardHandler(assetsDir, "http://127.0.0.1:7331"));
  t.after(() => server.close());

  for (const path of ["/%2e%2e/%2e%2e/secret", "/..%2f..%2fsecret", "/%2e%2e%2findex.html"]) {
    const res = await rawRequest(port, path);
    const status: number = res.status;
    assert.ok(status === 403 || status === 400, `${path} -> ${status}`);
    assert.ok(!res.text.includes("<title>dash</title>"), "must not serve index for traversal");
  }
});

test("does not SPA-fallback /v1 routes and proxies core 404", async (t) => {
  const assetsDir = makeAssets();
  const core = await listenEphemeral((_req, res) => {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "error" }));
  });
  t.after(() => core.server.close());

  const { server, port } = await listenEphemeral(
    createDashboardHandler(assetsDir, `http://127.0.0.1:${core.port}`),
  );
  t.after(() => server.close());

  const res = await rawRequest(port, "/v1/status");
  assert.equal(res.status, 404);
  assert.ok(!res.text.includes("<title>dash</title>"));

  const exact = await rawRequest(port, "/v1");
  assert.equal(exact.status, 404);
});

test("proxies method, headers, and body to core and streams the response", async (t) => {
  const assetsDir = makeAssets();
  let seenMethod: string | undefined;
  let seenBody = "";
  let seenHeader: string | undefined;
  const core = await listenEphemeral((req: any, res: any) => {
    seenMethod = req.method as string;
    seenHeader = req.headers["x-test-header"] as string | undefined;
    req.on("data", (chunk: Buffer) => {
      seenBody += String(chunk);
    });
    req.on("end", () => {
      res.writeHead(201, { "content-type": "application/json", "x-echo": "yes" });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  t.after(() => core.server.close());

  const { server, port } = await listenEphemeral(
    createDashboardHandler(assetsDir, `http://127.0.0.1:${core.port}`),
  );
  t.after(() => server.close());

  const res = await new Promise<{ status: number; headers: any; text: string }>((resolveReq, reject) => {
    const req = httpRequest(
      {
        hostname: "127.0.0.1",
        port,
        path: "/v1/items?x=1",
        method: "POST",
        headers: { "content-type": "text/plain", "x-test-header": "abc" },
      },
      (incoming) => {
        const chunks: Buffer[] = [];
        incoming.on("data", (chunk) => chunks.push(chunk as Buffer));
        incoming.on("end", () => {
          resolveReq({
            status: incoming.statusCode ?? 0,
            headers: incoming.headers,
            text: Buffer.concat(chunks).toString("utf8"),
          });
        });
      },
    );
    req.on("error", reject);
    req.write("hello-core");
    req.end();
  });

  assert.equal(res.status, 201);
  assert.equal(res.headers["x-echo"], "yes");
  assert.equal(res.text, JSON.stringify({ ok: true }));
  assert.equal(seenMethod, "POST");
  assert.equal(seenBody, "hello-core");
  assert.equal(seenHeader, "abc");
});

test("core errors become a safe 502 without secrets", async (t) => {
  const assetsDir = makeAssets();
  const core = await listenEphemeral((_req, _res) => {});
  const closedPort = core.port;
  await new Promise<void>((resolveClosed) => core.server.close(() => resolveClosed()));

  const { server, port } = await listenEphemeral(
    createDashboardHandler(assetsDir, `http://127.0.0.1:${closedPort}`),
  );
  t.after(() => server.close());

  const res = await rawRequest(port, "/v1/status");
  assert.equal(res.status, 502);
  assert.ok(res.text.includes("core unavailable"));
  assert.ok(!res.text.includes("ECONNREFUSED"));
});

test("HEAD serves headers without a body and non-GET/HEAD is rejected", async (t) => {
  const assetsDir = makeAssets();
  const { server, port } = await listenEphemeral(createDashboardHandler(assetsDir, "http://127.0.0.1:7331"));
  t.after(() => server.close());

  const head = await rawRequest(port, "/app.js", "HEAD");
  assert.equal(head.status, 200);
  assert.equal(head.text, "");

  const post = await rawRequest(port, "/app.js", "POST", "x");
  assert.equal(post.status, 405);
});

test("startDashboardServer rejects with an actionable error when assets are missing", async () => {
  const missing = join(mkdtempSync(join(tmpdir(), "crucible-missing-")), "nope");
  await assert.rejects(() => startDashboardServer({ assetsDir: missing, port: 0 }), /Dashboard assets are missing/);
});

test("dashboard resolution is adjacent to the CLI package, not the sibling package", () => {
  const cliRoot = resolve(join("repo", "packages", "cli"));
  const fromSrc = resolveDashboardDir(pathToFileURL(join(cliRoot, "src", "commands", "dashboard.ts")).href);
  const fromDist = resolveDashboardDir(pathToFileURL(join(cliRoot, "dist", "commands", "dashboard.js")).href);
  assert.equal(fromSrc, fromDist);
  assert.ok(fromSrc.endsWith(join("packages", "cli", "dashboard")));
});

class FakeServer extends EventEmitter {
  url = "http://127.0.0.1:4200/";
  close(callback?: () => void): void {
    setImmediate(() => {
      this.emit("close");
      callback?.();
    });
  }
}

test("runDashboard is injectable, announces the URL, opens the browser, and resolves after close", async () => {
  const assetsDir = makeAssets();
  const fake = new FakeServer();
  const lines: string[] = [];
  const opened: string[] = [];

  const pending = runDashboard({
    assetsDir,
    open: true,
    stdout: (line) => lines.push(line),
    openBrowser: (url) => {
      opened.push(url);
    },
    serverFactory: (async () => ({ server: fake as unknown as Server, url: fake.url })) as never,
  });
  setImmediate(() => fake.close());
  await pending;

  assert.equal(lines.length, 1);
  assert.ok(lines[0].includes(fake.url));
  assert.deepEqual(opened, [fake.url]);
});

test("runDashboard rejects binding errors actionably and skips the browser", async () => {
  let opened = 0;
  await assert.rejects(
    () =>
      runDashboard({
        assetsDir: makeAssets(),
        openBrowser: () => {
          opened += 1;
        },
        serverFactory: (async () => {
          throw new Error("Cannot start dashboard on 127.0.0.1:4200: EADDRINUSE");
        }) as never,
      }),
    /Cannot start dashboard/,
  );
  assert.equal(opened, 0);
});

test("CLI build stages dashboard assets and packaging allows dashboard/**", async () => {
  const cliPkgUrl = new URL("../package.json", import.meta.url);
  const pkg = JSON.parse(readFileSync(cliPkgUrl, "utf8")) as { scripts: Record<string, string>; files: string[] };
  assert.ok(pkg.scripts.build.includes("stage-dashboard.mjs"));
  assert.ok(pkg.files.includes("dashboard/**"));

  const stageUrl = new URL("../scripts/stage-dashboard.mjs", import.meta.url);
  const stageSource = readFileSync(stageUrl, "utf8");
  assert.ok(stageSource.includes("dist/dashboard/browser"));
  assert.ok(stageSource.includes("Dashboard assets are missing"));
});
