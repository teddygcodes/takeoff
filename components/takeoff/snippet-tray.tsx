"use client";

import { useState, useCallback } from "react";
import type { Snippet, ReadinessStatus } from "@/lib/types";

interface SnippetTrayProps {
  snippets: Snippet[];
  onDeleteSnippet: (id: string) => void;
  onRelabelSnippet: (id: string, label: string, subLabel: string) => void;
  onHighlightSnippet: (snippet: Snippet | null) => void;
  onRunTakeoff: (mode: string) => void;
  isRunning: boolean;
  hasPdf: boolean;
}

const LABEL_OPTIONS = [
  { value: "fixture_schedule", label: "Fixture Schedule", icon: "\u{1F4CB}" },
  { value: "rcp", label: "RCP", icon: "\u{1F3D7}" },
  { value: "panel_schedule", label: "Panel Schedule", icon: "\u26A1" },
  { value: "plan_notes", label: "Plan Notes", icon: "\u{1F4DD}" },
  { value: "detail", label: "Detail Drawing", icon: "\u{1F4D0}" },
  { value: "site_plan", label: "Site Plan", icon: "\u{1F5FA}" },
];

const LABEL_DISPLAY: Record<string, { title: string; icon: string }> = {
  fixture_schedule: { title: "FIXTURE SCHEDULES", icon: "\u{1F4CB}" },
  rcp: { title: "RCP AREAS", icon: "\u{1F3D7}" },
  panel_schedule: { title: "PANEL SCHEDULES", icon: "\u26A1" },
  plan_notes: { title: "PLAN NOTES", icon: "\u{1F4DD}" },
  detail: { title: "DETAIL DRAWINGS", icon: "\u{1F4D0}" },
  site_plan: { title: "SITE PLANS", icon: "\u{1F5FA}" },
};

const MODE_INFO: Record<string, { label: string; desc: string }> = {
  fast: { label: "Fast", desc: "Quick check, skip reconciliation (~40s)" },
  strict: { label: "Strict", desc: "Full adversarial review (~70s)" },
  liability: { label: "Liability", desc: "Maximum scrutiny for bids (~90s)" },
};

function getReadiness(snippets: Snippet[]): ReadinessStatus {
  const counts: Record<string, number> = {};
  for (const s of snippets) {
    counts[s.label] = (counts[s.label] || 0) + 1;
  }
  const hasSchedule = (counts["fixture_schedule"] || 0) >= 1;
  const hasRcp = (counts["rcp"] || 0) >= 1;

  if (!hasSchedule && !hasRcp) {
    return { ready: false, message: "Need 1 Fixture Schedule + 1 RCP to run", counts };
  }
  if (!hasSchedule) {
    return { ready: false, message: "Need 1 Fixture Schedule snippet", counts };
  }
  if (!hasRcp) {
    return { ready: false, message: "Need at least 1 RCP snippet", counts };
  }

  const parts: string[] = [];
  if (counts.rcp) parts.push(`${counts.rcp} RCP${counts.rcp > 1 ? "s" : ""}`);
  if (counts.fixture_schedule) parts.push(`${counts.fixture_schedule} Schedule`);
  if (counts.panel_schedule) parts.push(`${counts.panel_schedule} Panel`);

  return { ready: true, message: parts.join(" \u00B7 "), counts };
}

export function SnippetTray({
  snippets,
  onDeleteSnippet,
  onRelabelSnippet,
  onHighlightSnippet,
  onRunTakeoff,
  isRunning,
  hasPdf,
}: SnippetTrayProps) {
  const [mode, setMode] = useState("strict");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editLabel, setEditLabel] = useState("");
  const [editSubLabel, setEditSubLabel] = useState("");
  const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>({});

  const { ready, message, counts } = getReadiness(snippets);

  const startEdit = useCallback((snippet: Snippet) => {
    setEditingId(snippet.id);
    setEditLabel(snippet.label);
    setEditSubLabel(snippet.sub_label);
  }, []);

  const confirmEdit = useCallback(
    (id: string) => {
      onRelabelSnippet(id, editLabel, editSubLabel);
      setEditingId(null);
    },
    [editLabel, editSubLabel, onRelabelSnippet]
  );

  const toggleSection = useCallback((key: string) => {
    setCollapsedSections((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  const grouped = snippets.reduce<Record<string, Snippet[]>>((acc, s) => {
    if (!acc[s.label]) acc[s.label] = [];
    acc[s.label].push(s);
    return acc;
  }, {});

  const labelOrder = ["fixture_schedule", "rcp", "panel_schedule", "plan_notes", "detail", "site_plan"];

  return (
    <div className="flex h-full flex-col border-l border-border bg-background">
      {/* Header */}
      <div className="border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-foreground">Snippets</h2>
          {snippets.length > 0 && (
            <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-accent px-1.5 font-mono text-xs font-medium text-white">
              {snippets.length}
            </span>
          )}
        </div>
      </div>

      {/* Snippet list */}
      <div className="flex-1 overflow-y-auto">
        {!hasPdf ? (
          <div className="flex h-full items-center justify-center p-6">
            <p className="text-center text-sm italic text-muted-foreground">
              Upload a PDF to get started
            </p>
          </div>
        ) : snippets.length === 0 ? (
          <div className="flex h-full items-center justify-center p-6">
            <p className="text-center text-sm italic text-muted-foreground">
              Use the Snip tool to capture regions from the drawing
            </p>
          </div>
        ) : (
          <div className="p-3">
            {labelOrder.map((labelKey) => {
              const group = grouped[labelKey];
              const count = group ? group.length : 0;
              const info = LABEL_DISPLAY[labelKey];
              const collapsed = collapsedSections[labelKey];

              return (
                <div key={labelKey} className="mb-3">
                  {/* Section header */}
                  <button
                    onClick={() => toggleSection(labelKey)}
                    className="flex w-full items-center gap-2 px-1 py-1.5 text-left"
                  >
                    <span className="text-xs text-muted-foreground">
                      {collapsed ? "\u25B6" : "\u25BC"}
                    </span>
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {info?.icon} {info?.title} ({count})
                    </span>
                  </button>

                  {!collapsed && (
                    <>
                      {count === 0 ? (
                        <p className="px-3 py-2 text-xs italic text-muted-foreground/60">
                          {"No " + (info?.title.toLowerCase() || labelKey) + " yet"}
                        </p>
                      ) : (
                        group?.map((snippet) => (
                          <div
                            key={snippet.id}
                            className="group mb-1.5 rounded-lg border border-border bg-background transition-colors hover:bg-muted/50"
                          >
                            {editingId === snippet.id ? (
                              <div className="p-3">
                                <select
                                  value={editLabel}
                                  onChange={(e) => setEditLabel(e.target.value)}
                                  className="mb-2 w-full rounded-md border border-border bg-background px-2 py-1.5 font-sans text-xs text-foreground"
                                >
                                  {LABEL_OPTIONS.map((opt) => (
                                    <option key={opt.value} value={opt.value}>
                                      {opt.label}
                                    </option>
                                  ))}
                                </select>
                                <input
                                  type="text"
                                  value={editSubLabel}
                                  onChange={(e) => setEditSubLabel(e.target.value)}
                                  placeholder="Area name (optional)"
                                  className="mb-2 w-full rounded-md border border-border bg-background px-2 py-1.5 font-sans text-xs text-foreground placeholder:text-muted-foreground"
                                />
                                <div className="flex gap-2">
                                  <button
                                    onClick={() => confirmEdit(snippet.id)}
                                    className="flex-1 rounded-md bg-accent py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover"
                                  >
                                    Save
                                  </button>
                                  <button
                                    onClick={() => setEditingId(null)}
                                    className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted"
                                  >
                                    Cancel
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <div
                                className="relative flex cursor-pointer items-start gap-3 p-2.5"
                                onMouseEnter={() => onHighlightSnippet(snippet)}
                                onMouseLeave={() => onHighlightSnippet(null)}
                                onClick={() => onHighlightSnippet(snippet)}
                              >
                                {/* Thumbnail */}
                                <div className="h-10 w-16 flex-shrink-0 overflow-hidden rounded border border-border bg-muted">
                                  {snippet.image_data ? (
                                    <img
                                      src={snippet.image_data}
                                      alt={snippet.label}
                                      className="h-full w-full object-cover"
                                    />
                                  ) : (
                                    <div className="flex h-full items-center justify-center">
                                      <span className="text-xs text-muted-foreground">{info?.icon}</span>
                                    </div>
                                  )}
                                </div>

                                {/* Info */}
                                <div className="min-w-0 flex-1">
                                  <p className="truncate text-sm font-medium text-foreground">
                                    {snippet.sub_label || LABEL_DISPLAY[snippet.label]?.title || snippet.label}
                                  </p>
                                  <p className="text-xs text-muted-foreground">Page {snippet.page_number}</p>
                                </div>

                                {/* Actions (on hover) */}
                                <div className="flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                                  <button
                                    onClick={(e) => { e.stopPropagation(); startEdit(snippet); }}
                                    className="rounded p-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                                    title="Relabel"
                                    aria-label="Relabel snippet"
                                  >
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
                                  </button>
                                  <button
                                    onClick={(e) => { e.stopPropagation(); onDeleteSnippet(snippet.id); }}
                                    className="rounded p-1 text-xs text-muted-foreground transition-colors hover:bg-red-50 hover:text-accent"
                                    title="Delete"
                                    aria-label="Delete snippet"
                                  >
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                                  </button>
                                </div>
                              </div>
                            )}
                          </div>
                        ))
                      )}
                    </>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Bottom run section */}
      {hasPdf && (
        <div className="border-t border-border bg-sidebar p-4">
          {/* Status */}
          {snippets.length > 0 && (
            <p className="mb-3 text-center font-mono text-xs text-muted-foreground">{message}</p>
          )}

          {/* Mode selector */}
          <div className="mb-3">
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground"
            >
              {Object.entries(MODE_INFO).map(([key, info]) => (
                <option key={key} value={key}>
                  {info.label}
                </option>
              ))}
            </select>
            <p className="mt-1.5 text-xs text-muted-foreground">
              {MODE_INFO[mode].desc}
            </p>
          </div>

          {/* Run button */}
          <button
            onClick={() => ready && !isRunning && onRunTakeoff(mode)}
            disabled={!ready || isRunning}
            className="flex w-full items-center justify-center gap-2 rounded-lg py-2.5 text-sm font-semibold text-white transition-colors disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground"
            style={{
              backgroundColor: ready && !isRunning ? "var(--accent)" : undefined,
            }}
            title={!ready ? "Add at least 1 fixture schedule and 1 RCP area" : undefined}
          >
            {isRunning ? (
              <>
                <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" /></svg>
                Running...
              </>
            ) : (
              <>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                Run Takeoff
              </>
            )}
          </button>
        </div>
      )}
    </div>
  );
}
