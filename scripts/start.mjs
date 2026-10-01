import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const extra = process.argv.slice(2);
const hosted = Boolean(process.env.ZEABUR || process.env.ZEABUR_SERVICE_ID);
const hasFlag = (name) =>
  extra.some((arg) => arg === `--${name}` || arg.startsWith(`--${name}=`));

const args = [
  "--import",
  fileURLToPath(new URL("./sites-env.mjs", import.meta.url)),
  fileURLToPath(new URL("../node_modules/wrangler/bin/wrangler.js", import.meta.url)),
  "dev",
  "--config",
  "dist/server/wrangler.json",
  "--local",
  "--persist-to",
  ".wrangler/state",
  "--inspector-port",
  "0",
];

if (!hasFlag("ip")) {
  args.push("--ip", hosted || process.env.HOST === "0.0.0.0" ? "0.0.0.0" : "127.0.0.1");
}

if (!hasFlag("port")) {
  args.push("--port", process.env.PORT || process.env.WEB_PORT || "8787");
}

args.push(...extra);

const child = spawn(process.execPath, args, { stdio: "inherit" });
child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
