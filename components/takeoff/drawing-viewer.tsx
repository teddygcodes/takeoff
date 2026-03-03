"use client";

import { useRef, useState, useCallback, useEffect, useMemo } from "react";
import * as pdfjsLib from "pdfjs-dist";
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

// pdfjs worker — CDN avoids Turbopack worker-bundling issues
pdfjsLib.GlobalWorkerOptions.workerSrc = `https://unpkg.com/pdfjs-dist@${pdfjsLib.version}/build/pdf.worker.min.mjs`;

export type { Snippet as SnippetData };

const CANVAS_W = 1200; // baseline CSS width at zoom=100
const THUMB_W = 110;   // sidebar thumbnail CSS width

/* ── Props ────────────────────────────────────────────────────────── */

interface DrawingViewerProps {
  pageCount: number;
  currentPage: number;
  onPageChange: (page: number) => void;
  snippets: Snippet[];
  snipMode: boolean;
  onToggleSnip: () => void;
  onSnipComplete: (
    bbox: { x: number; y: number; width: number; height: number },
    imageData: string
  ) => void;
  onPdfLoaded: (pageCount: number) => void;
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
  onPdfLoaded,
  pdfLoaded,
  pipelineSteps,
  pipelineRunning,
  snippetFlash,
}: DrawingViewerProps) {
  const [zoom, setZoom] = useState(100);
  const [pdfPageCssHeight, setPdfPageCssHeight] = useState(900);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [snipRect, setSnipRect] = useState<SnipRect | null>(null);
  const snipRectRef = useRef<SnipRect | null>(null);
  const [isDrawing, setIsDrawing] = useState(false);
  const isDrawingRef = useRef(false);
  const setIsDrawingWithRef = useCallback((val: boolean) => {
    isDrawingRef.current = val;
    setIsDrawing(val);
  }, []);
  const startRef = useRef<{ x: number; y: number } | null>(null);

  // Pending rect: drawn but not yet confirmed; user can drag corners to resize
  const [pendingRect, setPendingRect] = useState<SnipRect | null>(null);
  const pendingRectRef = useRef<SnipRect | null>(null);
  const dragHandleRef = useRef<number>(-1); // -1=none, 0=TL,1=TR,2=BL,3=BR
  const [snipCursor, setSnipCursor] = useState<string>("crosshair");

  // Scroll container ref — used for non-passive wheel listener to intercept browser zoom
  const scrollContainerRef = useRef<HTMLDivElement>(null);

  // PDF refs
  const pdfCanvasRef = useRef<HTMLCanvasElement>(null);
  const pdfDocRef = useRef<pdfjsLib.PDFDocumentProxy | null>(null);
  const renderTaskRef = useRef<pdfjsLib.RenderTask | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const thumbnailRefs = useRef<Record<number, HTMLCanvasElement | null>>({});

  // Generation counter — incremented each time a new PDF is loaded; lets thumbnail
  // renders bail out early if a new PDF is loaded while they are still in flight
  const generationRef = useRef(0);

  // Keep a stable ref to the current zoom for use inside async callbacks
  const zoomRef = useRef(zoom);
  useEffect(() => { zoomRef.current = zoom; }, [zoom]);

  // Stable ref to snipMode — lets pan handlers read current value without dep-array churn
  const snipModeRef = useRef(snipMode);
  useEffect(() => { snipModeRef.current = snipMode; }, [snipMode]);

  // Clear pending rect when snip mode is toggled off or page changes.
  // setState calls here are intentional: they sync derived cleanup state
  // (pending selection must be null when mode/page changes).
  useEffect(() => {
    if (!snipMode) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPendingRect(null);
      pendingRectRef.current = null;
      dragHandleRef.current = -1;
      setSnipCursor("crosshair");
      return;
    }
    // When snip mode is active, listen for mouseup on window so that releasing
    // the mouse button outside the canvas doesn't leave isDrawing stuck as true.
    window.addEventListener("mouseup", onSnipMouseUp);
    return () => window.removeEventListener("mouseup", onSnipMouseUp);
  }, [snipMode, onSnipMouseUp]);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPendingRect(null);
    pendingRectRef.current = null;
    dragHandleRef.current = -1;
    setSnipCursor("crosshair");
  }, [currentPage]);

  // Pan state — isPanning drives grab/grabbing cursor; pan coords are local to the effect
  const [isPanning, setIsPanning] = useState(false);

  /* Zoom controls */
  const zoomIn = () => setZoom((z) => Math.min(z + 25, 400));
  const zoomOut = () => setZoom((z) => Math.max(z - 25, 25));
  const fitPage = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) { setZoom(100); return; }
    const zW = (el.clientWidth / CANVAS_W) * 100;
    const zH = (el.clientHeight / pdfPageCssHeight) * 100;
    setZoom(Math.round(Math.min(400, Math.max(25, Math.min(zW, zH) * 0.95))));
  }, [pdfPageCssHeight]);
  const fitWidth = useCallback(() => {
    const w = scrollContainerRef.current?.clientWidth ?? CANVAS_W;
    setZoom(Math.round(Math.min(400, Math.max(25, (w / CANVAS_W) * 100))));
  }, []);

  /* ── Main PDF rendering ─────────────────────────────────────── */

  const renderPage = useCallback(async (pageNum: number, zoomVal: number) => {
    if (!pdfDocRef.current || !pdfCanvasRef.current) return;
    renderTaskRef.current?.cancel();
    const page = await pdfDocRef.current.getPage(pageNum);
    const baseVp = page.getViewport({ scale: 1 });
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const displayW = (CANVAS_W * zoomVal) / 100;
    // Render at display size × DPR: pixel-perfect on retina, no CSS upscaling ever
    const scale = (displayW / baseVp.width) * dpr;
    const viewport = page.getViewport({ scale });
    const canvas = pdfCanvasRef.current;
    canvas.width = viewport.width;    // native px = displayW * dpr
    canvas.height = viewport.height;  // native px = displayH * dpr
    canvas.style.width = `${displayW}px`;
    canvas.style.height = `${viewport.height / dpr}px`;
    setPdfPageCssHeight(viewport.height / dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const task = page.render({ canvasContext: ctx, viewport });
    renderTaskRef.current = task;
    try {
      await task.promise;
    } catch {
      // RenderingCancelledException — a newer render took over; safe to ignore
    }
  }, []);

  // Cancel any in-progress render task on unmount to avoid canvas-after-unmount errors
  useEffect(() => {
    return () => {
      renderTaskRef.current?.cancel();
      pdfDocRef.current?.destroy();
    };
  }, []);

  // Immediate re-render on page change
  useEffect(() => {
    if (pdfLoaded) renderPage(currentPage, zoomRef.current);
  }, [pdfLoaded, currentPage, renderPage]);

  // Debounced re-render on zoom change (200 ms — avoids re-rendering on every increment)
  useEffect(() => {
    if (!pdfLoaded) return;
    const timer = setTimeout(() => renderPage(currentPage, zoom), 200);
    return () => clearTimeout(timer);
  }, [zoom, pdfLoaded, currentPage, renderPage]);

  /* ── Thumbnail rendering ────────────────────────────────────── */

  const renderThumbnail = useCallback(async (pageNum: number) => {
    if (!pdfDocRef.current) return;
    const canvas = thumbnailRefs.current[pageNum];
    if (!canvas) return;
    const gen = generationRef.current;
    const page = await pdfDocRef.current.getPage(pageNum);
    if (generationRef.current !== gen) return; // PDF was swapped
    const baseVp = page.getViewport({ scale: 1 });
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    // Low scale (~0.5–0.8×) × DPR for sharp thumbnails at sidebar size
    const scale = (THUMB_W / baseVp.width) * dpr;
    const viewport = page.getViewport({ scale });
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    canvas.style.width = `${THUMB_W}px`;
    canvas.style.height = `${viewport.height / dpr}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    await page.render({ canvasContext: ctx, viewport }).promise;
    if (generationRef.current !== gen) return; // PDF was swapped after render
  }, []);

  // Render all thumbnails after PDF loads; blank any stale ones from a previously-loaded PDF
  useEffect(() => {
    if (!pdfLoaded || pageCount === 0) return;
    // Clear canvases for pages beyond the new page count (handles PDF swap: 10-page → 5-page)
    const refs = thumbnailRefs.current;
    const maxPrev = Math.max(...Object.keys(refs).map(Number), 0);
    for (let pg = pageCount + 1; pg <= maxPrev; pg++) {
      const canvas = refs[pg];
      if (canvas) canvas.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
    }
    // Defer thumbnails so main page gets uncontested pdfjs render time first
    const id = setTimeout(() => {
      for (let pg = 1; pg <= pageCount; pg++) {
        renderThumbnail(pg);
      }
    }, 400);
    return () => clearTimeout(id);
  }, [pdfLoaded, pageCount, renderThumbnail]);

  /* ── PDF loading ────────────────────────────────────────────── */

  const loadPdf = useCallback(
    async (file: File) => {
      setPdfLoading(true);
      const buffer = await file.arrayBuffer();
      pdfDocRef.current?.destroy();
      generationRef.current++;
      const doc = await pdfjsLib.getDocument({ data: buffer }).promise;
      pdfDocRef.current = doc;
      onPdfLoaded(doc.numPages);
      setPdfLoading(false);
    },
    [onPdfLoaded]
  );

  const handleFileSelect = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file?.type === "application/pdf") await loadPdf(file);
      e.target.value = "";
    },
    [loadPdf]
  );

  const handleDrop = useCallback(
    async (e: React.DragEvent) => {
      e.preventDefault();
      const file = e.dataTransfer.files?.[0];
      if (file?.type === "application/pdf") await loadPdf(file);
    },
    [loadPdf]
  );

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

  /* Combined native-DOM effect: wheel zoom + click-drag pan
     Both need the scroll container, which only exists when pdfLoaded=true.
     Native listeners bypass React event delegation, which doesn't reliably
     receive pointer events after setPointerCapture is called.             */
  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el || !pdfLoaded) return;

    // ── Wheel: intercept ctrl+scroll / pinch before browser zoom ──
    const handleWheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        setZoom((z) => Math.max(25, Math.min(400, z + (e.deltaY < 0 ? 10 : -10))));
      }
    };

    // ── Pan: click-drag to scroll ──
    let ps: { x: number; y: number; sl: number; st: number } | null = null;

    const onPanDown = (e: PointerEvent) => {
      if (snipModeRef.current || e.button !== 0) return;
      el.setPointerCapture(e.pointerId); // lock events to this element during drag
      ps = { x: e.clientX, y: e.clientY, sl: el.scrollLeft, st: el.scrollTop };
      setIsPanning(true);
      e.preventDefault();
    };

    const onPanMove = (e: PointerEvent) => {
      if (!ps) return;
      el.scrollLeft = ps.sl - (e.clientX - ps.x);
      el.scrollTop  = ps.st - (e.clientY - ps.y);
    };

    const onPanEnd = () => { ps = null; setIsPanning(false); };

    el.addEventListener("wheel",         handleWheel, { passive: false });
    el.addEventListener("pointerdown",   onPanDown);
    el.addEventListener("pointermove",   onPanMove, { passive: true });
    el.addEventListener("pointerup",     onPanEnd);
    el.addEventListener("pointercancel", onPanEnd);

    return () => {
      el.removeEventListener("wheel",         handleWheel);
      el.removeEventListener("pointerdown",   onPanDown);
      el.removeEventListener("pointermove",   onPanMove);
      el.removeEventListener("pointerup",     onPanEnd);
      el.removeEventListener("pointercancel", onPanEnd);
    };
  }, [pdfLoaded]);

  // Auto-fit to container width when PDF first loads
  useEffect(() => {
    if (!pdfLoaded || !scrollContainerRef.current) return;
    const w = scrollContainerRef.current.clientWidth;
    setZoom(Math.round(Math.min(400, Math.max(25, (w / CANVAS_W) * 100))));
  }, [pdfLoaded]);

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
      // If a pending rect exists, check if click is on a corner handle
      if (pendingRectRef.current) {
        const r = pendingRectRef.current;
        const corners = [
          { x: r.x, y: r.y },
          { x: r.x + r.width, y: r.y },
          { x: r.x, y: r.y + r.height },
          { x: r.x + r.width, y: r.y + r.height },
        ];
        for (let i = 0; i < 4; i++) {
          const dx = pos.x - corners[i].x;
          const dy = pos.y - corners[i].y;
          if (Math.sqrt(dx * dx + dy * dy) <= 12) {
            dragHandleRef.current = i;
            return; // start handle drag — don't clear pending
          }
        }
        // Clicked away from all handles → clear pending and start new draw
        setPendingRect(null);
        pendingRectRef.current = null;
      }
      startRef.current = pos;
      setIsDrawingWithRef(true);
      setSnipRect({ x: pos.x, y: pos.y, width: 0, height: 0 });
    },
    [getCanvasPos, setIsDrawingWithRef]
  );

  const HANDLE_CURSORS = ["nwse-resize", "nesw-resize", "nesw-resize", "nwse-resize"] as const;

  const onSnipMouseMove = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const pos = getCanvasPos(e);

      // Handle drag: resize the pending rect via corner handle
      if (dragHandleRef.current >= 0 && pendingRectRef.current) {
        const h = dragHandleRef.current;
        let { x, y, width, height } = pendingRectRef.current;
        if (h === 0) { width += x - pos.x; x = pos.x; height += y - pos.y; y = pos.y; }
        else if (h === 1) { width = pos.x - x; height += y - pos.y; y = pos.y; }
        else if (h === 2) { width += x - pos.x; x = pos.x; height = pos.y - y; }
        else { width = pos.x - x; height = pos.y - y; }
        if (width < 20 || height < 20) return;
        const updated = { x, y, width, height };
        pendingRectRef.current = updated;
        setPendingRect({ ...updated });
        return;
      }

      // Cursor hint: show resize cursor when hovering near a pending rect corner
      if (!isDrawingRef.current && pendingRectRef.current) {
        const r = pendingRectRef.current;
        const corners = [
          { x: r.x, y: r.y },
          { x: r.x + r.width, y: r.y },
          { x: r.x, y: r.y + r.height },
          { x: r.x + r.width, y: r.y + r.height },
        ];
        let found = false;
        for (let i = 0; i < 4; i++) {
          const dx = pos.x - corners[i].x;
          const dy = pos.y - corners[i].y;
          if (Math.sqrt(dx * dx + dy * dy) <= 12) {
            setSnipCursor(HANDLE_CURSORS[i]);
            found = true;
            break;
          }
        }
        if (!found) setSnipCursor("crosshair");
        return;
      }

      // Normal draw
      if (!isDrawingRef.current || !startRef.current) return;
      const rect = {
        x: Math.min(startRef.current.x, pos.x),
        y: Math.min(startRef.current.y, pos.y),
        width: Math.abs(pos.x - startRef.current.x),
        height: Math.abs(pos.y - startRef.current.y),
      };
      snipRectRef.current = rect;
      setSnipRect(rect);
    },
    [getCanvasPos]
  );

  const onSnipMouseUp = useCallback(() => {
    // End handle drag
    if (dragHandleRef.current >= 0) {
      dragHandleRef.current = -1;
      return;
    }
    // End initial draw → go to pending state (user can resize before confirming)
    setIsDrawingWithRef(false);
    const rect = snipRectRef.current;
    if (rect && rect.width > 20 && rect.height > 20) {
      setPendingRect(rect);
      pendingRectRef.current = rect;
    }
    snipRectRef.current = null;
    setSnipRect(null);
    startRef.current = null;
    setSnipCursor("crosshair");
  }, [setIsDrawingWithRef]);

  const captureAndConfirm = useCallback(() => {
    const rect = pendingRectRef.current;
    if (!rect) return;
    let imageData = "";
    const canvas = pdfCanvasRef.current;
    if (canvas && canvas.width > 0 && canvas.height > 0) {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const sx = Math.round(rect.x * dpr);
      const sy = Math.round(rect.y * dpr);
      const sw = Math.max(1, Math.round(rect.width * dpr));
      const sh = Math.max(1, Math.round(rect.height * dpr));
      const tmp = document.createElement("canvas");
      tmp.width = sw;
      tmp.height = sh;
      const tmpCtx = tmp.getContext("2d");
      if (!tmpCtx) return;
      tmpCtx.drawImage(canvas, sx, sy, sw, sh, 0, 0, sw, sh);
      // Cap at 1600px on longest side to reduce Claude Vision token cost
      const MAX_PX = 1600;
      let finalCanvas: HTMLCanvasElement = tmp;
      if (sw > MAX_PX || sh > MAX_PX) {
        const scale = MAX_PX / Math.max(sw, sh);
        const scaled = document.createElement("canvas");
        scaled.width = Math.round(sw * scale);
        scaled.height = Math.round(sh * scale);
        const scaledCtx = scaled.getContext("2d");
        if (!scaledCtx) return;
        scaledCtx.drawImage(tmp, 0, 0, scaled.width, scaled.height);
        finalCanvas = scaled;
      }
      imageData = finalCanvas.toDataURL("image/jpeg", 0.85).split(",")[1] ?? "";
    }
    // Normalise from display-pixel space → zoom=100 space
    const z = zoom / 100;
    const normBbox = {
      x: rect.x / z,
      y: rect.y / z,
      width: rect.width / z,
      height: rect.height / z,
    };
    setPendingRect(null);
    pendingRectRef.current = null;
    setSnipCursor("crosshair");
    onSnipComplete(normBbox, imageData);
  }, [onSnipComplete, zoom]);

  /* Current page snippets */
  const pageSnippets = useMemo(
    () => snippets.filter((s) => s.page_number === currentPage),
    [snippets, currentPage]
  );

  /* Snippet counts per page for sidebar badges */
  const snippetsByPage = useMemo(
    () => snippets.reduce<Record<number, number>>((acc, s) => {
      acc[s.page_number] = (acc[s.page_number] || 0) + 1;
      return acc;
    }, {}),
    [snippets]
  );

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
            aria-label={`Go to page ${pg}`}
            aria-current={pg === currentPage ? "page" : undefined}
            className={`relative rounded-lg border-2 p-1 transition-all ${
              pg === currentPage
                ? "border-accent bg-red-50"
                : "border-border bg-background hover:border-muted"
            }`}
          >
            {/* DPR-aware thumbnail canvas */}
            <canvas
              ref={(el) => { thumbnailRefs.current[pg] = el; }}
              style={{ width: `${THUMB_W}px`, display: "block", borderRadius: "4px" }}
            />
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
        title={!pdfLoaded ? "Upload a PDF first" : pipelineRunning ? "Cannot snip while pipeline is running" : undefined}
        className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 ${
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
        title={!pdfLoaded ? "Upload a PDF first" : undefined}
        className="rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-canvas hover:text-foreground disabled:opacity-40"
      >
        <Maximize2 className="inline h-3 w-3 mr-1" />
        Fit
      </button>
      <button
        onClick={fitWidth}
        disabled={!pdfLoaded}
        title={!pdfLoaded ? "Upload a PDF first" : undefined}
        className="rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-canvas hover:text-foreground disabled:opacity-40"
      >
        Width
      </button>
      <div className="mx-1 h-4 w-px bg-border" />
      <button
        onClick={zoomOut}
        disabled={!pdfLoaded}
        title={!pdfLoaded ? "Upload a PDF first" : undefined}
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
        title={!pdfLoaded ? "Upload a PDF first" : undefined}
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
    if (pdfLoading) {
      return (
        <div className="flex flex-1 items-center justify-center bg-canvas">
          <div className="flex flex-col items-center gap-3 text-muted-foreground">
            <svg className="h-6 w-6 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" />
            </svg>
            <p className="text-sm">Loading blueprint...</p>
          </div>
        </div>
      );
    }

    if (!pdfLoaded) {
      return (
        <>
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf"
            style={{ display: "none" }}
            onChange={handleFileSelect}
          />
          <div
            className="flex flex-1 cursor-pointer items-center justify-center bg-canvas"
            onClick={() => fileInputRef.current?.click()}
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleDrop}
            role="button"
            tabIndex={0}
            aria-label="Upload PDF"
            onKeyDown={(e) =>
              (e.key === "Enter" || e.key === " ") &&
              fileInputRef.current?.click()
            }
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
        </>
      );
    }

    return (
      <div
        ref={scrollContainerRef}
        className="relative flex-1 overflow-auto bg-canvas"
        style={{
          cursor: pdfLoaded && !snipMode
            ? (isPanning ? "grabbing" : "grab")
            : undefined,
        }}
      >
        {/* Paper sheet — sized to match rendered PDF canvas */}
        <div
          className="relative mx-auto my-8 bg-background shadow-lg overflow-hidden"
          style={{
            width: `${(CANVAS_W * zoom) / 100}px`,
            height: `${pdfPageCssHeight}px`,
            transition: "width 0.2s",
          }}
        >
          {/* PDF canvas — CSS size set explicitly in renderPage, never CSS-upscaled */}
          <canvas ref={pdfCanvasRef} role="img" aria-label={`PDF drawing page ${currentPage}`} />

          {/* Snippet overlays — positioned in zoom=100 space, scaled by zoom */}
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
            rect={isDrawing ? snipRect : pendingRect}
            pending={!isDrawing && !!pendingRect}
            cursor={snipCursor}
            canvasWidth={(CANVAS_W * zoom) / 100}
            canvasHeight={pdfPageCssHeight}
            onMouseDown={onSnipMouseDown}
            onMouseMove={onSnipMouseMove}
            onMouseUp={onSnipMouseUp}
          />

          {/* Confirm / Cancel buttons shown while a pending rect awaits confirmation */}
          {snipMode && pendingRect && !isDrawing && (
            <div
              style={{
                position: "absolute",
                left: `${Math.min(pendingRect.x + pendingRect.width, (CANVAS_W * zoom) / 100 - 144)}px`,
                top: `${Math.min(pendingRect.y + pendingRect.height + 6, pdfPageCssHeight - 40)}px`,
                zIndex: 20,
                display: "flex",
                gap: "6px",
                pointerEvents: "auto",
              }}
            >
              <button
                onClick={captureAndConfirm}
                className="rounded bg-accent px-2.5 py-1 text-xs font-semibold text-white shadow hover:bg-accent-hover"
              >
                Confirm
              </button>
              <button
                onClick={() => {
                  setPendingRect(null);
                  pendingRectRef.current = null;
                  setSnipCursor("crosshair");
                }}
                className="rounded border border-border bg-background px-2.5 py-1 text-xs text-muted-foreground shadow"
              >
                Cancel
              </button>
            </div>
          )}
        </div>

        {renderPipelineOverlay()}
      </div>
    );
  };

  /* ── Render ─────────────────────────────────────────────────── */
  return (
    <div className="flex h-full overflow-hidden">
      {renderSidebar()}
      <div className="flex flex-1 flex-col overflow-hidden">
        {renderToolbar()}
        {renderSnipBanner()}
        {renderCanvas()}
      </div>
    </div>
  );
}
