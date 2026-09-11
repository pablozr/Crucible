import { spawn } from "node:child_process";


export type OpenBrowserFn = (url: string) => void | Promise<void>;


export function openDashboardBrowser(url: string): void {
  try {
    const platform = process.platform;
    if (platform === "darwin") {
      const child = spawn("open", [url], { detached: true, stdio: "ignore" });
      child.on("error", () => {});
      child.unref();
      return;
    }
    if (platform === "win32") {
      const child = spawn("cmd", ["/c", "start", "", url], {
        detached: true,
        stdio: "ignore",
        windowsHide: true,
      });
      child.on("error", () => {});
      child.unref();
      return;
    }
    const child = spawn("xdg-open", [url], { detached: true, stdio: "ignore" });
    child.on("error", () => {});
    child.unref();
  } catch {
    // Best effort: opening the browser must never fail the command.
  }
}
