/**
 * Next.js API Route: /api/takeoff
 *
 * Proxies requests to the Python FastAPI takeoff backend running at
 * TAKEOFF_API_URL (default: http://localhost:8001).
 *
 * The Python server streams SSE events; we forward the stream verbatim.
 *
 * POST /api/takeoff
 *   Body: { snippets, mode, drawing_name }
 *   Response: SSE stream (text/event-stream)
 *     data: {"type":"status","message":"Extracting fixture schedule..."}
 *     data: {"type":"result","data":{...TakeoffResult}}
 *     data: {"type":"done"}
 *     data: {"type":"error","message":"..."}
 */

import { NextRequest } from "next/server";

const TAKEOFF_API_URL = (() => {
  const raw = process.env.TAKEOFF_API_URL || "http://localhost:8001";
  try {
    new URL(raw);
  } catch {
    throw new Error(`TAKEOFF_API_URL is not a valid URL: "${raw}"`);
  }
  return raw;
})();

const VALID_MODES = new Set(["fast", "strict", "liability"]);

function isValidSnippet(s: unknown): boolean {
  if (typeof s !== "object" || s === null) return false;
  const o = s as Record<string, unknown>;
  return typeof o.label === "string" && typeof o.bbox === "object" && o.bbox !== null;
}

export async function POST(req: NextRequest) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return new Response(
      `data: ${JSON.stringify({ type: "error", message: "Invalid JSON in request body" })}\n\n`,
      { status: 400, headers: { "Content-Type": "text/event-stream" } }
    );
  }

  const b = body as Record<string, unknown>;
  const drawingName = b.drawing_name;
  if (
    typeof body !== "object" ||
    body === null ||
    !Array.isArray(b.snippets) ||
    typeof b.mode !== "string" ||
    !VALID_MODES.has(b.mode as string) ||
    (drawingName !== undefined && drawingName !== null && (typeof drawingName !== "string" || (drawingName as string).length > 255))
  ) {
    return new Response(
      `data: ${JSON.stringify({ type: "error", message: "Request must include snippets (array) and mode (fast|strict|liability); drawing_name must be a string ≤255 chars" })}\n\n`,
      { status: 400, headers: { "Content-Type": "text/event-stream" } }
    );
  }

  const badSnippetIdx = (b.snippets as unknown[]).findIndex((s) => !isValidSnippet(s));
  if (badSnippetIdx >= 0) {
    return new Response(
      `data: ${JSON.stringify({ type: "error", message: `Snippet at index ${badSnippetIdx} is missing required fields (label, bbox)` })}\n\n`,
      { status: 400, headers: { "Content-Type": "text/event-stream" } }
    );
  }

  // "duplex: half" is required by Node.js fetch when sending a body while streaming
  // the response. Not part of the standard RequestInit type, hence the cast.
  const fetchOptions = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(300_000),
    duplex: "half",
  } as RequestInit & { duplex: string };

  let upstreamRes: Response;
  try {
    upstreamRes = await fetch(`${TAKEOFF_API_URL}/takeoff/run`, fetchOptions);
  } catch (err) {
    // Backend is unreachable (not running, wrong port, etc.) — return a readable
    // SSE error event instead of letting the exception become an opaque HTTP 500.
    const detail = err instanceof Error ? err.message : String(err);
    const sseError = JSON.stringify({
      type: "error",
      message: `Cannot reach the Takeoff backend at ${TAKEOFF_API_URL}. Make sure the Python server is running (./run_api.sh). Detail: ${detail}`,
    });
    const sseDone = JSON.stringify({ type: "done" });
    return new Response(`data: ${sseError}\n\ndata: ${sseDone}\n\n`, {
      status: 200,
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  }

  if (!upstreamRes.ok) {
    const text = await upstreamRes.text().catch(() => "Unknown error");
    return new Response(
      `data: ${JSON.stringify({ type: "error", message: `Backend error ${upstreamRes.status}: ${text}` })}\n\n`,
      {
        status: 200, // Keep 200 so the client can read the error event
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          "X-Accel-Buffering": "no",
        },
      }
    );
  }

  // Pass the stream through unchanged
  if (!upstreamRes.body) {
    return new Response(
      `data: ${JSON.stringify({ type: "error", message: "Upstream returned no body" })}\n\n`,
      {
        status: 200,
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          "X-Accel-Buffering": "no",
        },
      }
    );
  }
  return new Response(upstreamRes.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
      Connection: "keep-alive",
    },
  });
}

/**
 * GET /api/takeoff/health — proxies to Python health check
 */
export async function GET() {
  try {
    const res = await fetch(`${TAKEOFF_API_URL}/takeoff/health`, {
      cache: "no-store",
    });
    const json = await res.json();
    return Response.json(json, { status: res.status });
  } catch (err) {
    return Response.json(
      { status: "unreachable", detail: err instanceof Error ? err.message : String(err) },
      { status: 503 }
    );
  }
}
