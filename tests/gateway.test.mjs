import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import http from "node:http";
import test from "node:test";
import { createGateway } from "../scripts/gateway.mjs";
import { defaultPublicOrigins, resolveStartConfig, superviseProduction } from "../scripts/start.mjs";

function listen(server) {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
}

function close(server) {
  return new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

test("production gateway streams multi-megabyte API bodies and injects the server key", async () => {
  const payload = Buffer.alloc(2 * 1024 * 1024 + 17, 0x5a);
  let received = 0;
  let key = "";
  const api = http.createServer((request, response) => {
    key = request.headers["x-api-key"] ?? "";
    request.on("data", (chunk) => { received += chunk.length; });
    request.on("end", () => {
      response.writeHead(201, { "content-type": "application/json" });
      response.end('{"ok":true}');
    });
  });
  const web = http.createServer((_request, response) => response.end("web"));
  const apiPort = await listen(api);
  const webPort = await listen(web);
  const testApiKey = ["test", "gateway", "key"].join("-");
  const gateway = createGateway({
    apiOrigin: `http://127.0.0.1:${apiPort}`,
    webOrigin: `http://127.0.0.1:${webPort}`,
    apiKey: testApiKey,
  });
  const gatewayPort = await listen(gateway);
  try {
    const response = await fetch(`http://127.0.0.1:${gatewayPort}/api/assets`, {
      method: "POST",
      headers: { "content-length": String(payload.length), "content-type": "application/octet-stream" },
      body: payload,
    });
    assert.equal(response.status, 201);
    assert.equal(received, payload.length);
    assert.equal(key, testApiKey);
  } finally {
    await Promise.all([close(gateway), close(api), close(web)]);
  }
});

test("production gateway routes non-API requests to the UI", async () => {
  const api = http.createServer((_request, response) => response.end("api"));
  const web = http.createServer((_request, response) => response.end("web"));
  const apiPort = await listen(api);
  const webPort = await listen(web);
  const gateway = createGateway({
    apiOrigin: `http://127.0.0.1:${apiPort}`,
    webOrigin: `http://127.0.0.1:${webPort}`,
  });
  const gatewayPort = await listen(gateway);
  try {
    const response = await fetch(`http://127.0.0.1:${gatewayPort}/`);
    assert.equal(await response.text(), "web");
  } finally {
    await Promise.all([close(gateway), close(api), close(web)]);
  }
});

test("production gateway accepts public-origin writes and rejects cross-origin writes", async () => {
  const seen = [];
  const api = http.createServer((request, response) => {
    seen.push(`${request.method} ${request.url}`);
    request.resume();
    request.on("end", () => {
      response.writeHead(request.method === "OPTIONS" ? 204 : 200);
      response.end();
    });
  });
  const web = http.createServer((_request, response) => response.end("web"));
  const apiPort = await listen(api);
  const webPort = await listen(web);
  const gateway = createGateway({
    apiOrigin: `http://127.0.0.1:${apiPort}`,
    webOrigin: `http://127.0.0.1:${webPort}`,
    allowedApiOrigins: ["http://localhost:3013"],
  });
  const gatewayPort = await listen(gateway);
  try {
    for (const method of ["POST", "PATCH", "OPTIONS"]) {
      const response = await fetch(`http://127.0.0.1:${gatewayPort}/api/write`, {
        method,
        headers: { origin: `http://127.0.0.1:${gatewayPort}` },
        ...(method === "OPTIONS" ? {} : { body: "{}" }),
      });
      assert.notEqual(response.status, 403);
    }
    const localhostAlias = await fetch(`http://127.0.0.1:${gatewayPort}/api/write`, {
      method: "POST",
      headers: { origin: "http://localhost:3013" },
      body: "{}",
    });
    assert.equal(localhostAlias.status, 200);

    const internalUiOrigin = await fetch(`http://127.0.0.1:${gatewayPort}/api/write`, {
      method: "POST",
      headers: { origin: `http://127.0.0.1:${webPort}` },
      body: "{}",
    });
    assert.equal(internalUiOrigin.status, 403);
    const hostile = await fetch(`http://127.0.0.1:${gatewayPort}/api/write`, {
      method: "POST",
      headers: { origin: "https://attacker.example" },
      body: "{}",
    });
    assert.equal(hostile.status, 403);
    assert.deepEqual(seen, ["POST /api/write", "PATCH /api/write", "OPTIONS /api/write", "POST /api/write"]);
  } finally {
    await Promise.all([close(gateway), close(api), close(web)]);
  }
});

test("production gateway rejects unsupported origins before it can listen", () => {
  assert.throws(
    () => createGateway({ apiOrigin: "https://127.0.0.1:6020", webOrigin: "http://127.0.0.1:3014" }),
    /apiOrigin must use http:/,
  );
  assert.throws(
    () => createGateway({ apiOrigin: "http://127.0.0.1:6020", webOrigin: "file:///tmp/ui" }),
    /webOrigin must use http:/,
  );
  assert.throws(
    () => createGateway({ apiOrigin: "not a URL", webOrigin: "http://127.0.0.1:3014" }),
    /apiOrigin must be a valid http URL/,
  );
  assert.throws(
    () => createGateway({
      apiOrigin: "http://127.0.0.1:6020",
      webOrigin: "http://127.0.0.1:3014",
      allowedApiOrigins: ["file:///tmp/ui"],
    }),
    /allowedApiOrigins\[0\] must use http: or https:/,
  );
});

test("production gateway converts an upstream connection failure to 502 and stays alive", async () => {
  const unavailable = http.createServer();
  const unavailablePort = await listen(unavailable);
  await close(unavailable);
  const web = http.createServer((_request, response) => response.end("still-running"));
  const webPort = await listen(web);
  const gateway = createGateway({
    apiOrigin: `http://127.0.0.1:${unavailablePort}`,
    webOrigin: `http://127.0.0.1:${webPort}`,
  });
  const gatewayPort = await listen(gateway);
  try {
    const failed = await fetch(`http://127.0.0.1:${gatewayPort}/api/health`);
    assert.equal(failed.status, 502);
    assert.deepEqual(await failed.json(), {
      error: "upstream_unavailable",
      message: "The local service is unavailable",
    });
    const healthy = await fetch(`http://127.0.0.1:${gatewayPort}/`);
    assert.equal(await healthy.text(), "still-running");
  } finally {
    await Promise.all([close(gateway), close(web)]);
  }
});

test("production start configuration validates both ports before spawning", () => {
  const resolved = resolveStartConfig({
    argv: ["node", "start.mjs", "--hostname", "0.0.0.0", "--port", "4100"],
    env: { H3_STUDIO_INTERNAL_WEB_PORT: "4101" },
  });
  assert.equal(resolved.host, "0.0.0.0");
  assert.equal(resolved.port, 4100);
  assert.equal(resolved.internalPort, 4101);
  assert.deepEqual(resolved.allowedApiOrigins, ["http://0.0.0.0:4100"]);

  assert.deepEqual(defaultPublicOrigins("127.0.0.1", 3013), [
    "http://127.0.0.1:3013",
    "http://localhost:3013",
    "http://[::1]:3013",
  ]);
  assert.deepEqual(
    resolveStartConfig({ argv: ["node", "start.mjs"], env: { H3_STUDIO_ALLOWED_API_ORIGINS: "https://studio.example, http://localhost:3013" } }).allowedApiOrigins,
    ["https://studio.example", "http://localhost:3013"],
  );

  for (const value of ["0", "65536", "1.5", "nope"]) {
    assert.throws(
      () => resolveStartConfig({ argv: ["node", "start.mjs", "--port", value], env: {} }),
      /public port must be an integer from 1 to 65535/,
    );
    assert.throws(
      () => resolveStartConfig({ argv: ["node", "start.mjs"], env: { H3_STUDIO_INTERNAL_WEB_PORT: value } }),
      /internal port must be an integer from 1 to 65535/,
    );
  }
  assert.throws(
    () => resolveStartConfig({ argv: ["node", "start.mjs", "--port", "4100"], env: { H3_STUDIO_INTERNAL_WEB_PORT: "4100" } }),
    /public and internal ports must be different/,
  );
  assert.throws(
    () => resolveStartConfig({ argv: ["node", "start.mjs", "--port", "65535"], env: {} }),
    /internal port must be an integer from 1 to 65535/,
  );
});

function lifecycleFixture() {
  const gateway = new EventEmitter();
  let closeCalls = 0;
  gateway.close = (callback) => {
    closeCalls += 1;
    queueMicrotask(() => callback());
  };
  const child = new EventEmitter();
  const signals = [];
  child.kill = (signal) => { signals.push(signal); return true; };
  const exits = [];
  let resolveExit;
  const exited = new Promise((resolve) => { resolveExit = resolve; });
  const lifecycle = superviseProduction({
    gateway,
    child,
    exit: (code) => { exits.push(code); resolveExit(code); },
    logger: { log() {}, error() {} },
    timeoutMs: 1_000,
  });
  return { gateway, child, signals, exits, exited, lifecycle, closeCalls: () => closeCalls };
}

test("production lifecycle closes the gateway for clean and failed UI exits", async (t) => {
  for (const code of [0, 7]) {
    await t.test(`child exit ${code}`, async () => {
      const fixture = lifecycleFixture();
      fixture.child.emit("exit", code, null);
      assert.equal(await fixture.exited, code);
      assert.equal(fixture.closeCalls(), 1);
      assert.deepEqual(fixture.exits, [code]);
      assert.deepEqual(fixture.signals, []);
    });
  }
});

test("production shutdown is idempotent and waits for both child and gateway", async () => {
  const fixture = lifecycleFixture();
  fixture.lifecycle.shutdown("SIGTERM");
  fixture.lifecycle.shutdown("SIGTERM");
  assert.equal(fixture.closeCalls(), 1);
  assert.deepEqual(fixture.signals, ["SIGTERM"]);
  assert.deepEqual(fixture.exits, []);

  fixture.child.emit("exit", null, "SIGTERM");
  assert.equal(await fixture.exited, 0);
  assert.deepEqual(fixture.exits, [0]);
  assert.equal(fixture.closeCalls(), 1);
});
