import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { request as httpRequest } from "node:http";
import { existsSync, statSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, join, normalize, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";


export const DASHBOARD_HOST = "127.0.0.1";
export const DASHBOARD_PORT = 4200;
export const CORE_ORIGIN = "http://127.0.0.1:7331";


export type DashboardServerOptions = {
  assetsDir: string;
  host?: string;
  port?: number;
  coreOrigin?: string;
};

export type StartedDashboardServer = {
  server: Server;
  url: string;
};

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ttf": "font/ttf",
};

function mimeFor(filePath: string): string {
  const dot = filePath.lastIndexOf(".");
  const ext = dot >= 0 ? filePath.slice(dot).toLowerCase() : "";
  return MIME[ext] ?? "application/octet-stream";
}

function sendText(res: ServerResponse, status: number, message: string): void {
  const body = `${message}\n`;
  res.writeHead(status, {
    "content-type": "text/plain; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function hasTraversal(rawPath: string): boolean {
  const decoded = safeDecode(rawPath);
  if (decoded === null || decoded.includes("\0")) {
    return true;
  }
  return decoded.split("/").includes("..") || decoded.split("\\").includes("..");
}

function isV1Path(pathname: string): boolean {
  return pathname === "/v1" || pathname.startsWith("/v1/");
}

function rawPathOf(url: string | undefined): string {
  const raw = (url ?? "/").split("?")[0].split("#")[0];
  return raw === "" ? "/" : raw;
}

function safeDecode(pathname: string): string | null {
  try {
    return decodeURIComponent(pathname);
  } catch {
    return null;
  }
}

function resolveAssetPath(assetsDir: string, pathname: string): string | null {
  const decoded = safeDecode(pathname);
  if (decoded === null) {
    return null;
  }
  const withoutLeading = decoded.replace(/^\/+/, "");
  const joined = join(assetsDir, withoutLeading);
  const normalized = normalize(joined);
  const root = resolve(assetsDir);
  const target = resolve(normalized);
  if (target !== root && !target.startsWith(root + sep)) {
    return null;
  }
  const rel = relative(root, target);
  if (rel === ".." || rel.startsWith(`..${sep}`)) {
    return null;
  }
  return target;
}

async function serveFile(
  req: IncomingMessage,
  res: ServerResponse,
  filePath: string,
): Promise<void> {
  const data = await readFile(filePath);
  const headers: Record<string, string> = {
    "content-type": mimeFor(filePath),
    "content-length": String(data.length),
  };
  res.writeHead(200, headers);
  if (req.method === "HEAD") {
    res.end();
    return;
  }
  res.end(data);
}

function proxyToCore(
  req: IncomingMessage,
  res: ServerResponse,
  coreOrigin: string,
  targetPath: string,
): void {
  let core: URL;
  try {
    core = new URL(coreOrigin);
  } catch {
    sendText(res, 502, "Dashboard proxy: core origin is not configured.");
    return;
  }

  const headers = { ...req.headers };
  delete headers["connection"];
  headers["host"] = core.host;

  const proxyReq = httpRequest(
    {
      protocol: core.protocol,
      hostname: core.hostname,
      port: core.port,
      path: targetPath,
      method: req.method,
      headers,
    },
    (proxyRes) => {
      res.writeHead(proxyRes.statusCode ?? 502, proxyRes.headers);
      proxyRes.pipe(res);
    },
  );

  proxyReq.on("error", () => {
    if (res.headersSent) {
      res.destroy();
      return;
    }
    sendText(res, 502, "Dashboard proxy: core unavailable at http://127.0.0.1:7331.");
  });

  req.pipe(proxyReq);
}

export function createDashboardHandler(assetsDir: string, coreOrigin: string) {
  return (req: IncomingMessage, res: ServerResponse): void => {
    let pathname = "/";
    let targetPath = "/";
    try {
      const parsed = new URL(req.url ?? "/", "http://127.0.0.1");
      pathname = parsed.pathname;
      targetPath = `${parsed.pathname}${parsed.search}`;
    } catch {
      sendText(res, 400, "Bad request.");
      return;
    }

    if (isV1Path(pathname)) {
      proxyToCore(req, res, coreOrigin, targetPath);
      return;
    }

    const method = (req.method ?? "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      sendText(res, 405, "Method not allowed.");
      return;
    }

    if (hasTraversal(rawPathOf(req.url))) {
      sendText(res, 403, "Forbidden.");
      return;
    }

    const resolved = resolveAssetPath(assetsDir, pathname);
    if (resolved === null) {
      sendText(res, 403, "Forbidden.");
      return;
    }

    void (async () => {
      try {
        const stat = statSync(resolved, { throwIfNoEntry: false });
        if (stat?.isFile()) {
          await serveFile(req, res, resolved);
          return;
        }
      } catch {
        sendText(res, 403, "Forbidden.");
        return;
      }

      const indexFile = join(resolve(assetsDir), "index.html");
      try {
        await serveFile(req, res, indexFile);
      } catch {
        sendText(res, 500, "Dashboard assets are missing. Rebuild the CLI so staged dashboard assets are included.");
      }
    })();
  };
}

export function resolveDashboardDir(fromUrl: string = import.meta.url): string {
  const dir = dirname(fileURLToPath(fromUrl));
  return join(dir, "..", "..", "dashboard");
}

export function startDashboardServer(options: DashboardServerOptions): Promise<StartedDashboardServer> {
  const host = options.host ?? DASHBOARD_HOST;
  const port = options.port ?? DASHBOARD_PORT;
  const coreOrigin = options.coreOrigin ?? CORE_ORIGIN;
  const assetsDir = resolve(options.assetsDir);

  const indexFile = join(assetsDir, "index.html");
  if (!existsSync(indexFile)) {
    return Promise.reject(
      new Error(
        `Dashboard assets are missing at ${assetsDir} (expected index.html). Build the dashboard with "pnpm --filter @pablozrrrr/crucible-dashboard build" and rebuild the CLI so assets are staged under packages/cli/dashboard.`,
      ),
    );
  }

  const server = createServer(createDashboardHandler(assetsDir, coreOrigin));

  return new Promise<StartedDashboardServer>((resolveStarted, reject) => {
    const onError = (error: unknown): void => {
      server.removeListener("error", onError);
      const detail = error instanceof Error ? error.message : String(error);
      reject(new Error(`Cannot start dashboard on ${host}:${port}: ${detail}`));
    };
    server.once("error", onError);
    server.listen(port, host, () => {
      server.removeListener("error", onError);
      resolveStarted({ server, url: `http://${host}:${port}/` });
    });
  });
}
