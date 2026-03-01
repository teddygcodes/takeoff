"use client";

import { useRef, useState, useCallback, useEffect } from "react";
import {
  Scissors,
  ZoomIn,
  ZoomOut,
  Upload,
  X,
  Maximize2,
} from "lucide-react";
import { SnippingTool, type SnipRect } from "./snipping-tool";
import type { Snippet, PipelineStep } from "@/lib/types";

/* ── Props ────────────────────────────────────────────────────────── */

interface DrawingViewerProps {
  pageCount: number;
  currentPage: number;
  onPageChange: (page: number) => void;
  snippets: Snippet[];
  snipMode: boolean;
  onToggleSnip: () => void;
  onSnipComplete: (bbox: { x: number; y: number; width: number; height: number }) => void;
  onUpload: () => void;
  pdfLoaded: boolean;
  pipelineSteps: PipelineStep[] | null;
  pipelineRunning: boolean;
  snippetFlash: string | null;
}

/* ── Component ────────────────────────────────────────────────────── */

export function DrawingViewer({
  pageCount,
  currentPage,
  onPageChange,
  snippets,
  snipMode,
  onToggleSnip,
  onSnipComplete,
  onUpload,
  pdfLoaded,
  pipelineSteps,
  pipelineRunning,
  snippetFlash,
}: DrawingViewerProps) {
  const [zoom, setZoom] = useState(100);
  const [snipRect, setSnipRect] = useState<SnipRect | null>(null);
  const [isDrawing, setIsDrawing] = useState(false);
  const startRef = useRef<{ x: number; y: number } | null>(null);

  const CANVAS_W = 1200;
  const CANVAS_H = 900;

  /* Zoom controls */
  const zoomIn = () => setZoom((z) => Math.min(z + 25, 300));
  const zoomOut = () => setZoom((z) => Math.max(z - 25, 25));
  const fitPage = () => setZoom(100);
  const fitWidth = () => setZoom(120);

  /* Keyboard shortcuts */
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && snipMode) {
        onToggleSnip();
        setSnipRect(null);
      }
      if (!snipMode) {
        if (e.key === "ArrowRight" && currentPage < pageCount)
          onPageChange(currentPage + 1);
        if (e.key === "ArrowLeft" && currentPage > 1)
          onPageChange(currentPage - 1);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [snipMode, onToggleSnip, currentPage, pageCount, onPageChange]);

  /* Snip mouse handlers */
  const getCanvasPos = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const rect = (e.target as HTMLCanvasElement).getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    },
    []
  );

  const onSnipMouseDown = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const pos = getCanvasPos(e);
      startRef.current = pos;
      setIsDrawing(true);
      setSnipRect({ x: pos.x, y: pos.y, width: 0, height: 0 });
    },
    [getCanvasPos]
  );

  const onSnipMouseMove = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      if (!isDrawing || !startRef.current) return;
      const pos = getCanvasPos(e);
      setSnipRect({
        x: Math.min(startRef.current.x, pos.x),
        y: Math.min(startRef.current.y, pos.y),
        width: Math.abs(pos.x - startRef.current.x),
        height: Math.abs(pos.y - startRef.current.y),
      });
    },
    [isDrawing, getCanvasPos]
  );

  const onSnipMouseUp = useCallback(() => {
    setIsDrawing(false);
    if (snipRect && snipRect.width > 20 && snipRect.height > 20) {
      onSnipComplete(snipRect);
    }
    setSnipRect(null);
    startRef.current = null;
  }, [snipRect, onSnipComplete]);

  /* Current page snippets */
  const pageSnippets = snippets.filter((s) => s.page_number === currentPage);

  /* Snippet counts per page for sidebar badges */
  const snippetsByPage = snippets.reduce<Record<number, number>>((acc, s) => {
    acc[s.page_number] = (acc[s.page_number] || 0) + 1;
    return acc;
  }, {});

  /* ── Page sidebar ───────────────────────────────────────────── */
  const renderSidebar = () => {
    if (!pdfLoaded) return null;
    return (
      <div className="flex w-[140px] shrink-0 flex-col gap-2 overflow-y-auto border-r border-border bg-surface p-3">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Pages
        </span>
        {Array.from({ length: pageCount }, (_, i) => i + 1).map((pg) => (
          <button
            key={pg}
            onClick={() => onPageChange(pg)}
            className={`relative rounded-lg border-2 p-1 transition-all ${
              pg === currentPage
                ? "border-accent bg-red-50"
                : "border-border bg-background hover:border-muted"
            }`}
          >
            <div className="flex h-[72px] w-full items-center justify-center rounded bg-canvas text-[10px] font-mono text-muted-foreground">
              {pg}
            </div>
            <span
              className={`mt-1 block text-center text-[10px] ${
                pg === currentPage
                  ? "font-semibold text-accent"
                  : "text-muted-foreground"
              }`}
            >
              Page {pg}
            </span>
            {snippetsByPage[pg] && (
              <span className="absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full bg-accent text-[9px] font-bold text-white">
                {snippetsByPage[pg]}
              </span>
            )}
          </button>
        ))}
      </div>
    );
  };

  /* ── Toolbar ────────────────────────────────────────────────── */
  const renderToolbar = () => (
    <div className="flex h-10 shrink-0 items-center gap-1 border-b border-border bg-background px-3">
      <button
        onClick={onToggleSnip}
        disabled={!pdfLoaded || pipelineRunning}
        className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
          snipMode
            ? "bg-accent text-white"
            : "text-muted-foreground hover:bg-canvas hover:text-foreground disabled:opacity-40"
        }`}
      >
        <Scissors className="h-3.5 w-3.5" />
        Snip
      </button>

      <div className="mx-2 h-4 w-px bg-border" />

      <button
        onClick={fitPage}
        disabled={!pdfLoaded}
        className="rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-canvas hover:text-foreground disabled:opacity-40"
      >
        <Maximize2 className="inline h-3 w-3 mr-1" />
        Fit
      </button>
      <button
        onClick={fitWidth}
        disabled={!pdfLoaded}
        className="rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-canvas hover:text-foreground disabled:opacity-40"
      >
        Width
      </button>
      <div className="mx-1 h-4 w-px bg-border" />
      <button
        onClick={zoomOut}
        disabled={!pdfLoaded}
        className="rounded-md p-1.5 text-muted-foreground hover:bg-canvas hover:text-foreground disabled:opacity-40"
      >
        <ZoomOut className="h-3.5 w-3.5" />
      </button>
      <span className="w-12 text-center font-mono text-xs text-muted-foreground">
        {zoom}%
      </span>
      <button
        onClick={zoomIn}
        disabled={!pdfLoaded}
        className="rounded-md p-1.5 text-muted-foreground hover:bg-canvas hover:text-foreground disabled:opacity-40"
      >
        <ZoomIn className="h-3.5 w-3.5" />
      </button>

      <div className="ml-auto text-xs text-muted-foreground">
        {pdfLoaded && (
          <span>
            Page {currentPage} of {pageCount}
          </span>
        )}
      </div>
    </div>
  );

  /* ── Snip info banner ───────────────────────────────────────── */
  const renderSnipBanner = () => {
    if (!snipMode) return null;
    return (
      <div className="flex items-center gap-2 border-b border-blue-200 bg-blue-50 px-4 py-2 text-xs text-blue-700">
        <Scissors className="h-3.5 w-3.5" />
        <span>Click and drag to select a region. Press Escape to cancel.</span>
        <button
          onClick={onToggleSnip}
          className="ml-auto rounded p-0.5 hover:bg-blue-100"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  };

  /* ── Pipeline Status Overlay ────────────────────────────────── */
  const renderPipelineOverlay = () => {
    if (!pipelineSteps || !pipelineRunning) return null;
    const currentStep = pipelineSteps.find((s) => s.status === "running");
    const completedCount = pipelineSteps.filter(
      (s) => s.status === "done"
    ).length;
    const progress = (completedCount / pipelineSteps.length) * 100;

    return (
      <div className="absolute inset-0 z-20 flex items-center justify-center bg-foreground/5 backdrop-blur-[1px]">
        <div className="w-[400px] rounded-xl border border-border bg-background p-6 shadow-2xl">
          <div className="mb-4 flex items-center gap-3">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-accent border-t-transparent" />
            <span className="text-sm font-semibold text-foreground">
              Running Takeoff...
            </span>
          </div>
          <div className="mb-5 h-1.5 overflow-hidden rounded-full bg-canvas">
            <div
              className="h-full rounded-full bg-accent transition-all duration-500"
              style={{ width: `${progress}%` }}
            />
          </div>
          <div className="flex flex-col gap-2.5">
            {pipelineSteps.map((step) => (
              <div
                key={step.id}
                className="flex items-center gap-2.5 text-xs animate-progress-step"
              >
                {step.status === "done" && (
                  <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-success text-white">
                    <svg
                      className="h-2.5 w-2.5"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={3}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </span>
                )}
                {step.status === "running" && (
                  <span className="flex h-4 w-4 shrink-0 items-center justify-center">
                    <span className="h-2.5 w-2.5 rounded-full bg-accent pipeline-pulse" />
                  </span>
                )}
                {step.status === "pending" && (
                  <span className="flex h-4 w-4 shrink-0 items-center justify-center">
                    <span className="h-2.5 w-2.5 rounded-full border-2 border-border" />
                  </span>
                )}
                {step.status === "error" && (
                  <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-error text-white text-[8px] font-bold">
                    !
                  </span>
                )}
                <span
                  className={
                    step.status === "done"
                      ? "text-muted-foreground line-through"
                      : step.status === "running"
                        ? "font-medium text-foreground"
                        : "text-muted-foreground"
                  }
                >
                  {step.label}
                  {step.detail && (
                    <span className="text-muted-foreground">
                      {" -- "}
                      {step.detail}
                    </span>
                  )}
                </span>
              </div>
            ))}
          </div>
          {currentStep && (
            <p className="mt-4 text-[11px] text-muted-foreground">
              {currentStep.label}...
            </p>
          )}
        </div>
      </div>
    );
  };

  /* ── Canvas / Empty State ───────────────────────────────────── */
  const renderCanvas = () => {
    if (!pdfLoaded) {
      return (
        <div
          className="flex flex-1 cursor-pointer items-center justify-center bg-canvas"
          onClick={onUpload}
          role="button"
          tabIndex={0}
          aria-label="Upload PDF"
          onKeyDown={(e) => e.key === "Enter" && onUpload()}
        >
          <div className="flex flex-col items-center gap-4 rounded-2xl border-2 border-dashed border-border p-16 transition-colors hover:border-muted">
            <Upload className="h-12 w-12 text-muted-foreground" />
            <div className="text-center">
              <p className="text-sm font-medium text-foreground">
                Drop PDF here or click to upload
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Supports multi-page construction drawing sets
              </p>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div
        className="relative flex-1 overflow-auto bg-canvas"
        onWheel={(e) => {
          if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            setZoom((z) =>
              Math.max(25, Math.min(300, z + (e.deltaY < 0 ? 10 : -10)))
            );
          }
        }}
      >
        {/* Paper sheet on grey canvas */}
        <div
          className="relative mx-auto my-8 bg-background shadow-lg"
          style={{
            width: `${(CANVAS_W * zoom) / 100}px`,
            height: `${(CANVAS_H * zoom) / 100}px`,
            transition: "width 0.2s, height 0.2s",
          }}
        >
          {/* Grid lines */}
          <svg className="absolute inset-0 h-full w-full opacity-[0.06]">
            <defs>
              <pattern
                id="grid"
                width="40"
                height="40"
                patternUnits="userSpaceOnUse"
              >
                <path
                  d="M 40 0 L 0 0 0 40"
                  fill="none"
                  stroke="#111827"
                  strokeWidth="1"
                />
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#grid)" />
          </svg>

          {/* Drawing label */}
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1">
            <span className="relative text-sm font-medium text-muted-foreground">
              Drawing Page {currentPage}
            </span>
            <span className="relative text-[10px] text-muted-foreground">
              E-{String(currentPage).padStart(3, "0")} Lighting Plan
            </span>
          </div>

          {/* Snippet overlays */}
          {pageSnippets.map((s) => (
            <div
              key={s.id}
              className={`absolute border-2 border-dashed border-accent/60 ${
                snippetFlash === s.id
                  ? "animate-snippet-flash bg-accent/20"
                  : ""
              }`}
              style={{
                left: `${(s.bbox.x * zoom) / 100}px`,
                top: `${(s.bbox.y * zoom) / 100}px`,
                width: `${(s.bbox.width * zoom) / 100}px`,
                height: `${(s.bbox.height * zoom) / 100}px`,
              }}
            >
              <span className="absolute -top-5 left-0 rounded bg-accent px-1.5 py-0.5 text-[9px] font-semibold text-white whitespace-nowrap">
                {s.label === "rcp"
                  ? `RCP: ${s.sub_label}`
                  : s.label.replace(/_/g, " ")}
              </span>
            </div>
          ))}

          {/* Snipping tool overlay */}
          <SnippingTool
            active={snipMode}
            rect={snipRect}
            canvasWidth={(CANVAS_W * zoom) / 100}
            canvasHeight={(CANVAS_H * zoom) / 100}
            onMouseDown={onSnipMouseDown}
            onMouseMove={onSnipMouseMove}
            onMouseUp={onSnipMouseUp}
          />
        </div>

        {renderPipelineOverlay()}
      </div>
    );
  };

  /* ── Render ─────────────────────────────────────────────────── */
  return (
    <div className="flex flex-1 overflow-hidden">
      {renderSidebar()}
      <div className="flex flex-1 flex-col overflow-hidden">
        {renderToolbar()}
        {renderSnipBanner()}
        {renderCanvas()}
      </div>
    </div>
  );
}
