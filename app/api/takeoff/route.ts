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

const TAKEOFF_API_URL =
  process.env.TAKEOFF_API_URL || "http://localhost:8001";

export async function POST(req: NextRequest) {
  const body = await req.json();

  // Forward to Python backend
  const upstreamRes = await fetch(`${TAKEOFF_API_URL}/takeoff/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    // @ts-ignore — Node.js fetch supports duplex streaming
    duplex: "half",
  }).catch((err) => {
    throw new Error(`Cannot reach takeoff backend at ${TAKEOFF_API_URL}: ${err.message}`);
  });

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
      { status: "unreachable", detail: (err as Error).message },
      { status: 503 }
    );
  }
}
