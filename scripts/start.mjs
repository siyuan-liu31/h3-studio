import { spawn } from "node:child_process";
import process from "node:process";
import { pathToFileURL } from "node:url";
import { createGateway } from "./gateway.mjs";

function option(argv, name, fallback) {
  const index = argv.indexOf(name);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
}

function portNumber(value, label) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${label} must be an integer from 1 to 65535`);
  }
  return port;
}

function formattedHost(host) {
  return host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
}

export function defaultPublicOrigins(host, port) {
  const hosts = new Set([host]);
  if (["127.0.0.1", "localhost", "::1"].includes(host)) {
    hosts.add("127.0.0.1");
    hosts.add("localhost");
    hosts.add("::1");
  }
  return [...hosts].map((name) => `http://${formattedHost(name)}:${port}`);
}

function configuredPublicOrigins(value, host, port) {
  const configured = value?.split(",").map((origin) => origin.trim()).filter(Boolean) ?? [];
  return configured.length ? configured : defaultPublicOrigins(host, port);
}

export function resolveStartConfig({ argv = process.argv, env = process.env } = {}) {
  const host = option(argv, "--hostname", env.H3_STUDIO_WEB_HOST ?? "127.0.0.1");
  const port = portNumber(option(argv, "--port", env.PORT ?? "3013"), "public port");
  const internalPort = portNumber(env.H3_STUDIO_INTERNAL_WEB_PORT ?? port + 1, "internal port");
  if (internalPort === port) throw new Error("public and internal ports must be different");
  return {
    host,
    port,
    internalPort,
    apiOrigin: env.H3_STUDIO_API_PROXY ?? "http://127.0.0.1:6020",
    apiKey: env.H3_STUDIO_PROXY_API_KEY ?? "",
    allowedApiOrigins: configuredPublicOrigins(env.H3_STUDIO_ALLOWED_API_ORIGINS, host, port),
  };
}

export function superviseProduction({
  gateway,
  child,
  exit = (code) => process.exit(code),
  logger = console,
  timeoutMs = 5_000,
  schedule = setTimeout,
  cancelSchedule = clearTimeout,
}) {
  let finishing = false;
  let gatewayClosed = false;
  let childExited = false;
  let exitCode = 0;
  let exited = false;
  let forceTimer;

  const finish = () => {
    if (exited || !gatewayClosed || !childExited) return;
    exited = true;
    if (forceTimer) cancelSchedule(forceTimer);
    exit(exitCode);
  };
  const closeGateway = () => {
    gateway.close((error) => {
      if (error && error.code !== "ERR_SERVER_NOT_RUNNING") {
        logger.error(`[h3-studio] Gateway close failed: ${error.message}`);
        exitCode = 1;
      }
      gatewayClosed = true;
      finish();
    });
  };
  const begin = (code, signal) => {
    if (finishing) return;
    finishing = true;
    exitCode = code;
    forceTimer = schedule(() => {
      if (!childExited) {
        try { child.kill("SIGKILL"); } catch { /* Process exit below is the final fallback. */ }
      }
      if (!exited) {
        exited = true;
        exit(1);
      }
    }, timeoutMs);
    forceTimer.unref?.();
    if (signal && !childExited) {
      try {
        child.kill(signal);
      } catch (error) {
        logger.error(`[h3-studio] UI server signal failed: ${error instanceof Error ? error.message : error}`);
        exitCode = 1;
      }
    }
    closeGateway();
    finish();
  };
  const shutdown = (signal) => begin(0, signal);

  child.once("error", (error) => {
    childExited = true;
    logger.error(`[h3-studio] UI server failed to start: ${error.message}`);
    begin(1);
    finish();
  });
  child.once("exit", (code, signal) => {
    childExited = true;
    if (!finishing) {
      if (code !== 0 || signal) logger.error(`[h3-studio] UI server exited (${signal ?? code})`);
      begin(signal ? 1 : code ?? 1);
    }
    finish();
  });
  gateway.once("error", (error) => {
    logger.error(`[h3-studio] Gateway failed: ${error.message}`);
    begin(1, "SIGTERM");
  });
  return { shutdown };
}

export function startProduction({
  argv = process.argv,
  env = process.env,
  spawnProcess = spawn,
  gatewayFactory = createGateway,
  exit,
  logger = console,
  timeoutMs = 5_000,
} = {}) {
  const config = resolveStartConfig({ argv, env });
  const gateway = gatewayFactory({
    apiOrigin: config.apiOrigin,
    webOrigin: `http://127.0.0.1:${config.internalPort}`,
    apiKey: config.apiKey,
    allowedApiOrigins: config.allowedApiOrigins,
  });
  const child = spawnProcess(
    process.execPath,
    ["node_modules/vinext/dist/cli.js", "start", "--hostname", "127.0.0.1", "--port", String(config.internalPort)],
    { stdio: "inherit", env },
  );
  const lifecycle = superviseProduction({ gateway, child, exit, logger, timeoutMs });
  gateway.listen(config.port, config.host, () => {
    logger.log(`[h3-studio] Production gateway running at http://${config.host}:${config.port}`);
  });
  return { gateway, child, lifecycle, config };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { lifecycle } = startProduction();
  process.on("SIGINT", () => lifecycle.shutdown("SIGINT"));
  process.on("SIGTERM", () => lifecycle.shutdown("SIGTERM"));
}
