"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import Link from "next/link";
import { DrawingViewer, type SnippetData } from "@/components/takeoff/drawing-viewer";
import { SnippetTray } from "@/components/takeoff/snippet-tray";
import { ResultsPanel, type TakeoffResultData } from "@/components/takeoff/results-panel";
import type { SnippetLabel } from "@/lib/types";

const VALID_SNIPPET_LABELS: readonly string[] = [
  "fixture_schedule", "rcp", "panel_schedule", "plan_notes", "detail", "site_plan",
];

function isSnippetLabel(v: string): v is SnippetLabel {
  return VALID_SNIPPET_LABELS.includes(v);
}

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
  const snippetSeqRef = useRef(0);

  // DrawingViewer controlled state
  const [pageCount, setPageCount] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [snipMode, setSnipMode] = useState(false);
  const [pdfLoaded, setPdfLoaded] = useState(false);

  // Backend health — null=checking, true=online, false=offline
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);

  // Abort any in-flight SSE stream when the component unmounts
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // Poll /api/takeoff (health check) every 10s so user knows if backend is down
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const res = await fetch("/api/takeoff", { method: "GET", cache: "no-store" });
        if (!cancelled) setBackendOnline(res.ok);
      } catch {
        if (!cancelled) setBackendOnline(false);
      }
    };
    check();
    const interval = setInterval(check, 10_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  // Auto-activate snip mode once PDF loads (guides user straight into first capture)
  useEffect(() => {
    if (pdfLoaded && snippets.length === 0) {
      setSnipMode(true);
    }
  }, [pdfLoaded, snippets.length]); // eslint-disable-line react-hooks/exhaustive-deps

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
          s.id === id ? { ...s, label: isSnippetLabel(label) ? label : s.label, sub_label: subLabel } : s
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

        if (!res.ok) throw new Error(`Takeoff request failed: ${res.status} ${res.statusText}`);

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
        let gotResult = false;

        try {
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
                  gotResult = true;
                  setResults(data.data);
                  setIsRunning(false);
                  setPipelineStatus("");
                } else if (data.type === "done") {
                  gotResult = true;
                  setIsRunning(false);
                  setPipelineStatus("");
                } else if (data.type === "error") {
                  gotResult = true;
                  setError(data.message);
                  setIsRunning(false);
                  setPipelineStatus("");
                }
              } catch {
                // Ignore unparseable SSE lines (keep-alives, partial frames)
              }
            }
          }

          // Stream ended cleanly but no result event was received
          if (!gotResult) {
            setError("Stream ended without a result. Please try again.");
            setIsRunning(false);
            setPipelineStatus("");
          }
        } catch (readerErr) {
          // Re-throw so outer catch handles AbortError vs real errors
          throw readerErr;
        } finally {
          reader.cancel();
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") {
          setIsRunning(false);
          setPipelineStatus("Cancelled");
        } else {
          setError((err instanceof Error ? err.message : String(err)) || "Something went wrong");
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
    <div className="flex h-dvh flex-col overflow-hidden bg-background">
      {/* ── Top Nav ── */}
      <header className="flex shrink-0 items-center justify-between border-b border-border bg-surface px-6 py-3">
        <div className="flex items-center gap-4">
          <Link
            href="/"
            className="transition-opacity hover:opacity-60"
            style={{
              fontSize: "13px",
              letterSpacing: "0.2em",
              color: "var(--color-muted-foreground)",
              fontWeight: 600,
            }}
          >
            ATLANTIS
          </Link>
          <span className="text-border">/</span>
          <span
            style={{
              fontSize: "13px",
              letterSpacing: "0.2em",
              color: "var(--color-foreground)",
              fontWeight: 600,
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
              aria-pressed={panelMode === "workspace"}
              className="rounded px-3 py-1.5 text-xs font-medium tracking-widest transition-colors"
              style={{
                background: panelMode === "workspace" ? "var(--color-canvas)" : "transparent",
                color: panelMode === "workspace" ? "var(--color-foreground)" : "var(--color-muted-foreground)",
                border: `1px solid ${panelMode === "workspace" ? "var(--color-border)" : "transparent"}`,
              }}
            >
              WORKSPACE
            </button>
            <button
              onClick={() => setPanelMode("results")}
              aria-pressed={panelMode === "results"}
              className="rounded px-3 py-1.5 text-xs font-medium tracking-widest transition-colors"
              style={{
                background: panelMode === "results" ? "rgba(220,38,38,0.08)" : "transparent",
                color: panelMode === "results" ? "#dc2626" : "var(--color-muted-foreground)",
                border: `1px solid ${panelMode === "results" ? "rgba(220,38,38,0.25)" : "transparent"}`,
              }}
            >
              RESULTS
            </button>
          </div>
        )}
      </header>

      {/* ── Backend offline banner ── */}
      {backendOnline === false && (
        <div
          role="alert"
          className="flex shrink-0 items-center gap-2 border-b border-amber-300 bg-amber-50 px-6 py-2 text-xs font-medium text-amber-800"
          style={{ letterSpacing: "0.05em" }}
        >
          <span>⚠</span>
          <span>
            Python backend is offline — start it with{" "}
            <code className="rounded bg-amber-100 px-1 py-0.5 font-mono">./run_api.sh</code>
          </span>
        </div>
      )}

      {/* ── Main Layout ── */}
      <div className="flex min-h-0 flex-1">
        {panelMode === "workspace" ? (
          /* ── Workspace: Drawing Viewer + Snippet Tray ── */
          <>
            {/* Drawing viewer — takes most of the horizontal space */}
            <div className="min-w-0 flex-1">
              <DrawingViewer
                pageCount={pageCount}
                currentPage={currentPage}
                onPageChange={setCurrentPage}
                snippets={snippets}
                snipMode={snipMode}
                onToggleSnip={() => setSnipMode((m) => !m)}
                onSnipComplete={(bbox, imageData) => {
                  // Default to "rcp" once a fixture schedule exists — guides user through step 2
                  const hasSchedule = snippets.some((s) => s.label === "fixture_schedule");
                  const defaultLabel = hasSchedule ? "rcp" : "fixture_schedule";
                  const snippet: SnippetData = {
                    id: `s${Date.now()}_${++snippetSeqRef.current}`,
                    label: defaultLabel,
                    sub_label: "",
                    page_number: currentPage,
                    bbox,
                    image_data: imageData,
                  };
                  handleSnippetCaptured(snippet);
                  setSnipMode(false);
                }}
                onPdfLoaded={(count) => {
                  setPageCount(count);
                  setCurrentPage(1);
                  setPdfLoaded(true);
                }}
                pdfLoaded={pdfLoaded}
                pipelineSteps={null}
                pipelineRunning={isRunning}
                snippetFlash={highlightSnippet?.id ?? null}
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
                onActivateSnip={() => setSnipMode(true)}
                isRunning={isRunning}
                hasPdf={pdfLoaded}
              />
            </div>
          </>
        ) : (
          /* ── Results: full-width panel ── */
          <div className="flex-1">
            {error ? (
              <div className="flex h-full items-center justify-center p-8">
                <div className="text-center">
                  <p className="mb-2 font-mono text-xs font-semibold tracking-widest text-accent">
                    ERROR
                  </p>
                  <p className="mb-4 text-sm text-muted-foreground">
                    {error}
                  </p>
                  <button
                    onClick={() => {
                      setError(null);
                      setPanelMode("workspace");
                    }}
                    className="rounded border border-border bg-canvas px-4 py-2 text-xs font-medium tracking-widest text-foreground transition-colors hover:bg-muted"
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
                onClose={() => setPanelMode("workspace")}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
