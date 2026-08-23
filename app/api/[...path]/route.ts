import type { NextRequest } from "next/server";

/**
 * Production same-origin gateway for the Python orchestration API.
 *
 * Vite's development proxy handles `/api` while running `vinext dev`, but it
 * is not part of the production server.  Keeping this route in the app makes
 * uploads, streaming previews and downloads work identically in both modes.
 * The optional API key is injected server-side and is never shipped in the
 * browser bundle.
 */
export const dynamic = "force-dynamic";

const BODYLESS_METHODS = new Set(["GET", "HEAD"]);
const REQUEST_HOP_HEADERS = [
  "connection",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "x-api-key",
];
const RESPONSE_HOP_HEADERS = [
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

function apiTarget(request: NextRequest) {
  const base = new URL(process.env.H3_STUDIO_API_PROXY ?? "http://127.0.0.1:6020");
  const incoming = new URL(request.url);
  base.pathname = incoming.pathname;
  base.search = incoming.search;
  return base;
}

async function proxy(request: NextRequest) {
  const headers = new Headers(request.headers);
  for (const name of REQUEST_HOP_HEADERS) headers.delete(name);
  const apiKey = process.env.H3_STUDIO_PROXY_API_KEY;
  if (apiKey) headers.set("X-API-Key", apiKey);

  try {
    const upstream = await fetch(apiTarget(request), {
      method: request.method,
      headers,
      body: BODYLESS_METHODS.has(request.method) ? undefined : request.body,
      redirect: "manual",
      // Node requires duplex for a streamed request body; workers ignore it.
      ...(!BODYLESS_METHODS.has(request.method) ? { duplex: "half" } : {}),
    } as RequestInit & { duplex?: "half" });
    const responseHeaders = new Headers(upstream.headers);
    for (const name of RESPONSE_HOP_HEADERS) responseHeaders.delete(name);
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    return Response.json(
      {
        error: "api_unavailable",
        message: error instanceof Error ? error.message : "The generation API is unavailable",
      },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const HEAD = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
