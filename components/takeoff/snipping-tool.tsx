"use client";

/**
 * SnippingTool — Rectangle selection overlay for the drawing canvas.
 *
 * This component renders an absolutely-positioned <canvas> overlay on top of
 * the PDF rendering canvas.  The parent (DrawingViewer) drives all state;
 * this component only owns the visual drawing of the selection rectangle.
 *
 * Usage:
 *   <SnippingTool
 *     active={mode === "snip"}
 *     rect={snipRect}          // { x, y, width, height } in canvas-pixel coords
 *     canvasWidth={canvas.width}
 *     canvasHeight={canvas.height}
 *   />
 *
 * The overlay forwards all mouse events to the parent via onMouseDown /
 * onMouseMove / onMouseUp so the parent can update `rect` on each frame.
 */

import { useRef, useEffect } from "react";

export interface SnipRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface SnippingToolProps {
  active: boolean;
  rect: SnipRect | null;
  canvasWidth: number;
  canvasHeight: number;
  onMouseDown?: (e: React.MouseEvent<HTMLCanvasElement>) => void;
  onMouseMove?: (e: React.MouseEvent<HTMLCanvasElement>) => void;
  onMouseUp?: (e: React.MouseEvent<HTMLCanvasElement>) => void;
}

export function SnippingTool({
  active,
  rect,
  canvasWidth,
  canvasHeight,
  onMouseDown,
  onMouseMove,
  onMouseUp,
}: SnippingToolProps) {
  const overlayRef = useRef<HTMLCanvasElement>(null);

  // Redraw on every rect / active change
  useEffect(() => {
    const canvas = overlayRef.current;
    if (!canvas) return;

    canvas.width = canvasWidth;
    canvas.height = canvasHeight;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.clearRect(0, 0, canvasWidth, canvasHeight);

    if (!active || !rect || rect.width < 2 || rect.height < 2) return;

    // Dim everything outside the selection
    ctx.fillStyle = "rgba(0, 0, 0, 0.35)";
    ctx.fillRect(0, 0, canvasWidth, canvasHeight);

    // Cut out the selected region (show original drawing through)
    ctx.clearRect(rect.x, rect.y, rect.width, rect.height);

    // Selection border
    ctx.strokeStyle = "#dc2626";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(rect.x, rect.y, rect.width, rect.height);

    // Corner handles
    const handleSize = 7;
    ctx.fillStyle = "#dc2626";
    ctx.setLineDash([]);
    const corners = [
      { x: rect.x, y: rect.y },
      { x: rect.x + rect.width, y: rect.y },
      { x: rect.x, y: rect.y + rect.height },
      { x: rect.x + rect.width, y: rect.y + rect.height },
    ];
    for (const c of corners) {
      ctx.fillRect(
        c.x - handleSize / 2,
        c.y - handleSize / 2,
        handleSize,
        handleSize
      );
    }

    // Dimension label
    if (rect.width > 60 && rect.height > 20) {
      const label = `${Math.round(rect.width)} × ${Math.round(rect.height)}`;
      ctx.font = "10px 'IBM Plex Mono', monospace";
      ctx.fillStyle = "#dc2626";
      ctx.fillText(label, rect.x + 6, rect.y - 6 > 10 ? rect.y - 6 : rect.y + 16);
    }
  }, [active, rect, canvasWidth, canvasHeight]);

  if (!active) return null;

  return (
    <canvas
      ref={overlayRef}
      width={canvasWidth}
      height={canvasHeight}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        cursor: "crosshair",
        // Pointer events are ON here — this canvas sits on top and captures mouse
        pointerEvents: "auto",
        zIndex: 10,
      }}
    />
  );
}
