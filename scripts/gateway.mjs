import http from "node:http";

const HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function forwardedHeaders(headers, apiKey) {
  const result = {};
  for (const [name, value] of Object.entries(headers)) {
    if (!HOP_HEADERS.has(name.toLowerCase()) && name.toLowerCase() !== "host") {
      result[name] = value;
    }
  }
  if (apiKey) result["x-api-key"] = apiKey;
  return result;
}

function copyResponseHeaders(source, response) {
  for (const [name, value] of Object.entries(source)) {
    if (!HOP_HEADERS.has(name.toLowerCase()) && value !== undefined) {
      response.setHeader(name, value);
    }
  }
}

function normalizedPublicOrigin(value, label = "request Origin") {
  try {
    const origin = new URL(value);
    if (origin.protocol !== "http:" && origin.protocol !== "https:") {
      throw new TypeError(`${label} must use http: or https:`);
    }
    return origin.origin;
  } catch (error) {
    if (error instanceof TypeError && error.message.startsWith(label)) throw error;
    throw new TypeError(`${label} must be a valid web origin`, { cause: error });
  }
}

function isAllowedApiOrigin(value, requestHost, allowedOrigins) {
  let origin;
  try {
    origin = normalizedPublicOrigin(value);
  } catch {
    return false;
  }
  if (allowedOrigins.has(origin)) return true;
  if (!requestHost) return false;
  try {
    return origin === new URL(`http://${requestHost}`).origin;
  } catch {
    return false;
  }
}

function httpOrigin(value, label) {
  let origin;
  try {
    origin = new URL(value);
  } catch (error) {
    throw new TypeError(`${label} must be a valid http URL`, { cause: error });
  }
  if (origin.protocol !== "http:") {
    throw new TypeError(`${label} must use http:`);
  }
  return origin;
}

function unavailable(response, error) {
  if (response.destroyed || response.writableEnded) return;
  if (response.headersSent) {
    response.destroy(error);
    return;
  }
  response.writeHead(502, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify({ error: "upstream_unavailable", message: "The local service is unavailable" }));
}

export function createGateway({ apiOrigin, webOrigin, apiKey = "", allowedApiOrigins = [] }) {
  const api = httpOrigin(apiOrigin, "apiOrigin");
  const web = httpOrigin(webOrigin, "webOrigin");
  const allowedOrigins = new Set(
    allowedApiOrigins.map((origin, index) => normalizedPublicOrigin(origin, `allowedApiOrigins[${index}]`)),
  );
  return http.createServer((request, response) => {
    const isApi = request.url === "/api" || request.url?.startsWith("/api/");
    if (isApi && request.headers.origin && !isAllowedApiOrigin(request.headers.origin, request.headers.host, allowedOrigins)) {
      response.writeHead(403, { "content-type": "application/json; charset=utf-8" });
      response.end(JSON.stringify({ error: "forbidden_origin", message: "API access is only allowed from the public web origin" }));
      return;
    }
    const target = isApi ? api : web;
    let upstream;
    try {
      upstream = http.request(
        {
          protocol: target.protocol,
          hostname: target.hostname,
          port: target.port,
          method: request.method,
          path: request.url,
          headers: forwardedHeaders(request.headers, isApi ? apiKey : ""),
        },
        (upstreamResponse) => {
          response.statusCode = upstreamResponse.statusCode ?? 502;
          response.statusMessage = upstreamResponse.statusMessage ?? "";
          copyResponseHeaders(upstreamResponse.headers, response);
          upstreamResponse.on("error", (error) => unavailable(response, error));
          upstreamResponse.pipe(response);
        },
      );
    } catch (error) {
      unavailable(response, error);
      return;
    }
    upstream.on("error", (error) => unavailable(response, error));
    request.on("aborted", () => upstream.destroy());
    request.on("error", () => upstream.destroy());
    response.on("close", () => {
      if (!response.writableEnded) upstream.destroy();
    });
    request.pipe(upstream);
  });
}
