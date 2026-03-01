"use client";

import { useState, useCallback, useRef } from "react";
import Link from "next/link";
import { DrawingViewer, type SnippetData } from "@/components/takeoff/drawing-viewer";
import { SnippetTray } from "@/components/takeoff/snippet-tray";
import { ResultsPanel, type TakeoffResultData } from "@/components/takeoff/results-panel";

type PanelMode = "workspace" | "results";

export default function TakeoffPage() {
  const [snippets, setSnippets] = useState<SnippetData[]>([]);
  const [highlightSnippet, setHighlightSnippet] = useState<SnippetData | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [pipelineStatus, setPipelineStatus] = useState("");
  const [results, setResults] = useState<TakeoffResultData | null>(null);
  const [panelMode, setPanelMode] = useState<PanelMode>("workspace");
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // ── Snippet management ──────────────────────────────────────────────
  const handleSnippetCaptured = useCallback((snippet: SnippetData) => {
    setSnippets((prev) => [...prev, snippet]);
  }, []);

  const handleDeleteSnippet = useCallback((id: string) => {
    setSnippets((prev) => prev.filter((s) => s.id !== id));
  }, []);

  const handleRelabelSnippet = useCallback(
    (id: string, label: string, subLabel: string) => {
      setSnippets((prev) =>
        prev.map((s) =>
          s.id === id ? { ...s, label, sub_label: subLabel } : s
        )
      );
    },
    []
  );

  // ── Run takeoff ─────────────────────────────────────────────────────
  const handleRunTakeoff = useCallback(
    async (mode: string) => {
      if (isRunning) return;

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setIsRunning(true);
      setError(null);
      setPipelineStatus("Initializing pipeline...");
      setPanelMode("results");

      // H3: 5-minute hard timeout on the SSE stream
      let sseTimeoutId: ReturnType<typeof setTimeout> | null = null;

      try {
        const res = await fetch("/api/takeoff", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            snippets: snippets.map((s) => ({
              id: s.id,
              label: s.label,
              sub_label: s.sub_label,
              page_number: s.page_number,
              bbox: s.bbox,
              image_data: s.image_data,
            })),
            mode,
            drawing_name: `takeoff_${Date.now()}`,
          }),
          signal: controller.signal,
        });

        if (!res.ok) throw new Error("Takeoff request failed");

        const reader = res.body?.getReader();
        if (!reader) throw new Error("No response stream");

        // Set 5-minute timeout — aborts fetch if pipeline hangs
        sseTimeoutId = setTimeout(() => {
          setError("Takeoff pipeline timed out after 5 minutes");
          setIsRunning(false);
          setPipelineStatus("");
          controller.abort();
        }, 5 * 60 * 1000);

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const rawLine of lines) {
            const line = rawLine.trim();
            if (!line.startsWith("data:")) continue;

            const jsonStr = line.slice(5).trim();
            if (!jsonStr || jsonStr === "[DONE]") continue;

            try {
              const data = JSON.parse(jsonStr);

              if (data.type === "status") {
                setPipelineStatus(data.message);
              } else if (data.type === "result") {
                setResults(data.data);
                setIsRunning(false);
                setPipelineStatus("");
              } else if (data.type === "done") {
                setIsRunning(false);
                setPipelineStatus("");
              } else if (data.type === "error") {
                setError(data.message);
                setIsRunning(false);
                setPipelineStatus("");
              }
            } catch (parseErr) {
              console.warn("[Takeoff SSE] Failed to parse event JSON:", line, parseErr);
            }
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name !== "AbortError") {
          setError(err.message || "Something went wrong");
          setIsRunning(false);
          setPipelineStatus("");
        }
      } finally {
        if (sseTimeoutId !== null) clearTimeout(sseTimeoutId);
      }
    },
    [isRunning, snippets]
  );

  return (
    <div
      className="flex h-dvh flex-col overflow-hidden"
      style={{ background: "#060606" }}
    >
      {/* ── Top Nav ── */}
      <header
        className="flex shrink-0 items-center justify-between border-b px-6 py-3"
        style={{ borderColor: "#1a1a1a", background: "#0a0a0a" }}
      >
        <div className="flex items-center gap-4">
          <Link
            href="/"
            className="transition-opacity hover:opacity-60"
            style={{
              fontFamily: "var(--font-cinzel)",
              fontSize: "14px",
              letterSpacing: "0.25em",
              color: "#525252",
            }}
          >
            ATLANTIS
          </Link>
          <span style={{ color: "#1a1a1a" }}>/</span>
          <span
            style={{
              fontFamily: "var(--font-cinzel)",
              fontSize: "14px",
              letterSpacing: "0.25em",
              color: "#d4d4d4",
            }}
          >
            TAKEOFF
          </span>
        </div>

        {/* Panel toggle */}
        {results && (
          <div className="flex gap-1">
            <button
              onClick={() => setPanelMode("workspace")}
              className="rounded px-3 py-1.5 text-xs transition-colors"
              style={{
                fontFamily: "var(--font-ibm-plex-mono)",
                fontSize: "10px",
                letterSpacing: "0.1em",
                background: panelMode === "workspace" ? "#1a1a1a" : "transparent",
                color: panelMode === "workspace" ? "#d4d4d4" : "#444",
                border: `1px solid ${panelMode === "workspace" ? "#333" : "#1a1a1a"}`,
              }}
            >
              WORKSPACE
            </button>
            <button
              onClick={() => setPanelMode("results")}
              className="rounded px-3 py-1.5 text-xs transition-colors"
              style={{
                fontFamily: "var(--font-ibm-plex-mono)",
                fontSize: "10px",
                letterSpacing: "0.1em",
                background: panelMode === "results" ? "rgba(220,38,38,0.12)" : "transparent",
                color: panelMode === "results" ? "#dc2626" : "#444",
                border: `1px solid ${panelMode === "results" ? "rgba(220,38,38,0.3)" : "#1a1a1a"}`,
              }}
            >
              RESULTS
            </button>
          </div>
        )}
      </header>

      {/* ── Main Layout ── */}
      <div className="flex min-h-0 flex-1">
        {panelMode === "workspace" ? (
          /* ── Workspace: Drawing Viewer + Snippet Tray ── */
          <>
            {/* Drawing viewer — takes most of the horizontal space */}
            <div className="min-w-0 flex-1">
              <DrawingViewer
                onSnippetCaptured={handleSnippetCaptured}
                highlightSnippet={highlightSnippet}
              />
            </div>

            {/* Snippet tray sidebar — fixed width */}
            <div className="w-72 shrink-0">
              <SnippetTray
                snippets={snippets}
                onDeleteSnippet={handleDeleteSnippet}
                onRelabelSnippet={handleRelabelSnippet}
                onHighlightSnippet={setHighlightSnippet}
                onRunTakeoff={handleRunTakeoff}
                isRunning={isRunning}
              />
            </div>
          </>
        ) : (
          /* ── Results: full-width panel ── */
          <div className="flex-1">
            {error ? (
              <div className="flex h-full items-center justify-center p-8">
                <div className="text-center">
                  <p
                    className="mb-2 text-xs"
                    style={{
                      fontFamily: "var(--font-ibm-plex-mono)",
                      color: "#dc2626",
                      letterSpacing: "0.1em",
                    }}
                  >
                    ERROR
                  </p>
                  <p
                    className="mb-4 text-sm"
                    style={{ fontFamily: "var(--font-ibm-plex-mono)", color: "#525252" }}
                  >
                    {error}
                  </p>
                  <button
                    onClick={() => {
                      setError(null);
                      setPanelMode("workspace");
                    }}
                    className="rounded px-4 py-2 text-xs transition-colors"
                    style={{
                      fontFamily: "var(--font-ibm-plex-mono)",
                      background: "#1a1a1a",
                      color: "#d4d4d4",
                      letterSpacing: "0.1em",
                    }}
                  >
                    BACK TO WORKSPACE
                  </button>
                </div>
              </div>
            ) : (
              <ResultsPanel
                data={
                  results || {
                    fixture_counts: [],
                    grand_total: 0,
                    areas_covered: [],
                    confidence_score: 0,
                    confidence_band: "VERY_LOW",
                    constitutional_violations: [],
                    adversarial_log: [],
                    judge_verdict: "BLOCK",
                    flags: [],
                  }
                }
                pipelineStatus={pipelineStatus}
                isLoading={isRunning}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
