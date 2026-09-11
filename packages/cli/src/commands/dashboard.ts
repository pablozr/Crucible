import type { Server } from "node:http";

import { openDashboardBrowser, type OpenBrowserFn } from "../dashboard/open-browser.js";

export type { OpenBrowserFn };
import {
  CORE_ORIGIN,
  DASHBOARD_HOST,
  DASHBOARD_PORT,
  resolveDashboardDir,
  startDashboardServer,
  type StartedDashboardServer,
} from "../dashboard/server.js";


export { resolveDashboardDir };

export type DashboardServerFactory = (
  options: { assetsDir: string; host: string; port: number; coreOrigin: string },
) => Promise<StartedDashboardServer>;

export type DashboardDeps = {
  assetsDir?: string;
  dashboardDir?: string;
  host?: string;
  port?: number;
  coreOrigin?: string;
  open?: boolean;
  serverFactory?: DashboardServerFactory;
  openBrowser?: OpenBrowserFn;
  stdout?: (message: string) => void;
};


export function resolveNgBin(): string {
  throw new Error(
    'Dashboard no longer uses Angular CLI. Build the dashboard with "pnpm --filter @pablozrrrr/dashboard build" and rebuild the CLI so assets are staged under packages/cli/dashboard.',
  );
}

function waitForClose(server: Server): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.once("close", () => resolve());
  });
}

export async function runDashboard(deps: DashboardDeps = {}): Promise<void> {
  const assetsDir = deps.assetsDir ?? deps.dashboardDir ?? resolveDashboardDir();
  const host = deps.host ?? DASHBOARD_HOST;
  const port = deps.port ?? DASHBOARD_PORT;
  const coreOrigin = deps.coreOrigin ?? CORE_ORIGIN;
  const shouldOpen = deps.open ?? true;
  const factory = deps.serverFactory ?? startDashboardServer;
  const openBrowser = deps.openBrowser ?? openDashboardBrowser;
  const stdout = deps.stdout ?? console.log;

  const { server, url } = await factory({ assetsDir, host, port, coreOrigin });
  stdout(`Dashboard available at ${url}`);

  if (shouldOpen) {
    try {
      await openBrowser(url);
    } catch {
      // Best effort: browser failure is nonfatal.
    }
  }

  const closed = waitForClose(server);
  const shutdown = (): void => {
    server.close(() => {});
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);

  try {
    await closed;
  } finally {
    process.removeListener("SIGINT", shutdown);
    process.removeListener("SIGTERM", shutdown);
  }
}
