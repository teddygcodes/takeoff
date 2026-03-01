"use client";

import { useState, useCallback } from "react";
import type { TakeoffResult } from "@/lib/types";

interface ResultsPanelProps {
  data: TakeoffResult;
  pipelineStatus?: string;
  isLoading?: boolean;
  onClose: () => void;
}

function exportCSV(data: TakeoffResult) {
  const rows = [
    ["TYPE", "DESCRIPTION", "COUNT", "REVISED", "DIFF", "DIFFICULTY"],
    ...data.fixture_counts.map((f) => [
      f.type_tag,
      f.description,
      String(f.total),
      String(f.revised ?? f.total),
      f.delta ? (f.delta > 0 ? `+${f.delta}` : String(f.delta)) : "0",
      f.difficulty,
    ]),
    [],
    ["TOTAL", "", String(data.grand_total), String(data.revised_total ?? data.grand_total)],
    ["Confidence", `${(data.confidence_score * 100).toFixed(0)}% (${data.confidence_band})`],
    ["Verdict", data.judge_verdict],
  ];
  const csv = rows.map((r) => r.map((c) => `"${c}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `takeoff_${data.drawing_name || "results"}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function exportJSON(data: TakeoffResult) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `takeoff_${data.drawing_name || "results"}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

function copyTable(data: TakeoffResult) {
  const header = "TYPE\tDESCRIPTION\tCOUNT\tREVISED\tDIFF\tDIFFICULTY";
  const rows = data.fixture_counts.map(
    (f) =>
      `${f.type_tag}\t${f.description}\t${f.total}\t${f.revised ?? f.total}\t${f.delta ? (f.delta > 0 ? "+" + f.delta : f.delta) : "—"}\t${f.difficulty}`
  );
  const total = `TOTAL\t\t${data.grand_total}\t${data.revised_total ?? data.grand_total}`;
  navigator.clipboard.writeText([header, ...rows, total].join("\n"));
}

const VERDICT_STYLES: Record<string, { bg: string; border: string; text: string; icon: string; label: string }> = {
  PASS: { bg: "bg-green-50", border: "border-l-green-600", text: "text-green-700", icon: "\u2713", label: "Takeoff Approved" },
  WARN: { bg: "bg-amber-50", border: "border-l-amber-500", text: "text-amber-700", icon: "\u26A0", label: "Approved with Warnings" },
  BLOCK: { bg: "bg-red-50", border: "border-l-red-600", text: "text-red-700", icon: "\u2717", label: "Takeoff Blocked \u2014 constitutional violations" },
};

const SEVERITY_STYLES: Record<string, { bg: string; text: string }> = {
  critical: { bg: "bg-red-100", text: "text-red-700" },
  major: { bg: "bg-amber-100", text: "text-amber-700" },
  minor: { bg: "bg-blue-100", text: "text-blue-700" },
};

const RESOLUTION_STYLES: Record<string, { text: string; label: string }> = {
  CONCEDED: { text: "text-green-600", label: "CONCEDED \u2713" },
  DEFENDED: { text: "text-muted-foreground", label: "DEFENDED \u2717" },
  PARTIAL: { text: "text-amber-600", label: "PARTIAL ~" },
};

const DIFF_LABELS: Record<string, string> = {
  S: "Standard",
  M: "Moderate",
  D: "Difficult",
  E: "Extreme",
};

export function ResultsPanel({ data, pipelineStatus, isLoading, onClose }: ResultsPanelProps) {
  const [activeTab, setActiveTab] = useState<"counts" | "adversarial" | "confidence" | "export">("counts");
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    copyTable(data);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [data]);

  if (data.error) {
    return (
      <div className="flex h-full items-center justify-center bg-background p-8">
        <div className="text-center">
          <p className="mb-2 font-mono text-xs font-semibold uppercase tracking-wider text-accent">Pipeline Error</p>
          <p className="max-w-md text-sm text-muted-foreground">{data.error}</p>
        </div>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 bg-background p-8">
        <div className="h-2 w-2 animate-pulse rounded-full bg-accent" />
        <p className="font-mono text-xs tracking-wide text-muted-foreground">
          {pipelineStatus || "RUNNING PIPELINE..."}
        </p>
      </div>
    );
  }

  const verdict = VERDICT_STYLES[data.judge_verdict] || VERDICT_STYLES.WARN;
  const tabs = [
    { key: "counts" as const, label: "Fixture Counts" },
    { key: "adversarial" as const, label: "Adversarial Log" },
    { key: "confidence" as const, label: "Confidence" },
    { key: "export" as const, label: "Export" },
  ];

  return (
    <div className="flex h-full flex-col bg-background">
      {/* Close/minimize bar */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <h2 className="text-sm font-semibold text-foreground">Takeoff Results</h2>
        <button
          onClick={onClose}
          className="rounded p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          aria-label="Close results"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/></svg>
        </button>
      </div>

      {/* Verdict banner */}
      <div className={`border-l-4 px-4 py-2.5 ${verdict.bg} ${verdict.border}`}>
        <p className={`text-sm font-semibold ${verdict.text}`}>
          {verdict.icon} {verdict.label}
        </p>
        {data.ruling_summary && (
          <p className="mt-0.5 text-xs text-muted-foreground">{data.ruling_summary}</p>
        )}
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-sm transition-colors ${
              activeTab === tab.key
                ? "border-b-2 border-accent font-semibold text-foreground"
                : "text-muted-foreground hover:text-foreground"
            }`}
            style={{ marginBottom: activeTab === tab.key ? "-1px" : undefined }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto">
        {/* FIXTURE COUNTS */}
        {activeTab === "counts" && (
          <div>
            <table className="w-full">
              <thead>
                <tr className="border-b border-border bg-sidebar">
                  {["TYPE", "DESCRIPTION", "COUNT", "REVISED", "DIFF", "DIFFICULTY"].map((h) => (
                    <th
                      key={h}
                      className="px-4 py-2.5 text-left font-mono text-[11px] font-semibold uppercase tracking-wider text-muted-foreground"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.fixture_counts.map((f) => {
                  const hasDelta = f.delta && f.delta !== 0;
                  const isExpanded = expandedRow === f.type_tag;

                  return (
                    <tr key={f.type_tag} className="group" >
                      <td colSpan={6} className="p-0">
                        <div
                          className={`flex cursor-pointer items-center border-b transition-colors ${
                            hasDelta ? "bg-amber-50/50" : "bg-background"
                          } hover:bg-muted/50`}
                          onClick={() => setExpandedRow(isExpanded ? null : f.type_tag)}
                        >
                          <div className="w-[80px] px-4 py-3">
                            <span className="rounded bg-red-50 px-2 py-0.5 font-mono text-xs font-bold text-accent">
                              {f.type_tag}
                            </span>
                          </div>
                          <div className="flex-1 px-4 py-3">
                            <p className="text-sm text-foreground">{f.description}</p>
                            {(f.flags || []).length > 0 && (
                              <p className="mt-0.5 text-xs text-amber-600">
                                {"\u26A0"} {f.flags?.join("; ")}
                              </p>
                            )}
                          </div>
                          <div className="w-[80px] px-4 py-3 text-right font-mono text-sm font-medium text-foreground">
                            {f.total}
                          </div>
                          <div className="w-[80px] px-4 py-3 text-right font-mono text-sm font-medium text-foreground">
                            {f.revised ?? f.total}
                          </div>
                          <div className="w-[80px] px-4 py-3 text-right font-mono text-sm">
                            {hasDelta ? (
                              <span className={f.delta! > 0 ? "font-semibold text-amber-600" : "text-green-600"}>
                                {f.delta! > 0 ? `+${f.delta} \u25B2` : `${f.delta} \u25BC`}
                              </span>
                            ) : (
                              <span className="text-muted-foreground">{"\u2014"}</span>
                            )}
                          </div>
                          <div className="w-[100px] px-4 py-3 font-mono text-xs text-muted-foreground">
                            {DIFF_LABELS[f.difficulty] || f.difficulty}
                          </div>
                        </div>

                        {/* Expanded row */}
                        {isExpanded && (
                          <div className="border-b border-border bg-sidebar px-6 py-3">
                            {Object.keys(f.counts_by_area || {}).length > 0 && (
                              <div className="mb-2">
                                <p className="mb-1.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                                  Per-Area Breakdown
                                </p>
                                <div className="grid grid-cols-3 gap-2">
                                  {Object.entries(f.counts_by_area || {}).map(([area, count]) => (
                                    <div key={area} className="flex items-center justify-between rounded border border-border bg-background px-3 py-1.5">
                                      <span className="text-xs text-muted-foreground">{area}</span>
                                      <span className="font-mono text-xs font-medium text-foreground">{count}</span>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            {f.notes && (
                              <p className="text-xs text-muted-foreground">
                                <span className="font-semibold">Notes:</span> {f.notes}
                              </p>
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr className="border-t-2 border-border bg-sidebar">
                  <td className="px-4 py-3" />
                  <td className="px-4 py-3">
                    <span className="font-mono text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Grand Total
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-base font-bold text-foreground">
                    {data.grand_total}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-base font-bold text-foreground">
                    {data.revised_total ?? data.grand_total}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-sm font-semibold">
                    {(data.revised_total ?? data.grand_total) !== data.grand_total ? (
                      <span className="text-amber-600">
                        +{(data.revised_total ?? data.grand_total) - data.grand_total}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">{"\u2014"}</span>
                    )}
                  </td>
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>
        )}

        {/* ADVERSARIAL LOG */}
        {activeTab === "adversarial" && (
          <div className="p-4">
            {/* Summary */}
            <div className="mb-4 rounded-lg border border-border bg-sidebar p-3">
              <p className="text-sm text-muted-foreground">
                <span className="font-semibold text-foreground">{data.adversarial_log.length} attacks</span> found:
                {" "}{data.adversarial_log.filter((a) => a.severity === "critical").length} critical,
                {" "}{data.adversarial_log.filter((a) => a.severity === "major").length} major,
                {" "}{data.adversarial_log.filter((a) => a.severity === "minor").length} minor
              </p>
            </div>

            {data.adversarial_log.length === 0 ? (
              <p className="py-8 text-center text-sm italic text-muted-foreground">
                No adversarial challenges in this run
              </p>
            ) : (
              data.adversarial_log.map((entry, i) => {
                const sev = SEVERITY_STYLES[entry.severity] || SEVERITY_STYLES.minor;
                const res = RESOLUTION_STYLES[entry.resolution || ""] || { text: "text-muted-foreground", label: entry.resolution };

                return (
                  <div
                    key={i}
                    className="mb-3 rounded-lg border border-border bg-background p-4"
                  >
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold uppercase ${sev.bg} ${sev.text}`}>
                        {entry.severity}
                      </span>
                      {entry.category && (
                        <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
                          {entry.category.replace(/_/g, " ")}
                        </span>
                      )}
                      <span className="font-mono text-[11px] text-muted-foreground">{entry.attack_id}</span>
                    </div>

                    <p className="mb-2 text-sm leading-relaxed text-foreground">{entry.description}</p>

                    {entry.explanation && (
                      <div className="rounded-md border border-border bg-sidebar p-3">
                        <span className={`mr-2 font-mono text-xs font-bold ${res.text}`}>
                          {res.label}
                        </span>
                        <span className="text-xs leading-relaxed text-muted-foreground">{entry.explanation}</span>
                      </div>
                    )}
                  </div>
                );
              })
            )}

            {/* Constitutional violations */}
            {data.constitutional_violations.length > 0 && (
              <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4">
                <p className="mb-3 font-mono text-[11px] font-semibold uppercase tracking-wider text-red-700">
                  Constitutional Violations
                </p>
                {data.constitutional_violations.map((v, i) => (
                  <div key={i} className="mb-2 last:mb-0">
                    <p className="text-xs font-semibold text-red-600">
                      {v.severity} {"\u2014"} {v.rule}
                    </p>
                    <p className="text-xs text-red-700/70">{v.explanation}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* CONFIDENCE */}
        {activeTab === "confidence" && (
          <div className="p-4">
            {/* Overall score */}
            <div className="mb-6 rounded-lg border border-border bg-sidebar p-5">
              <div className="mb-3 flex items-baseline gap-3">
                <span className="font-mono text-4xl font-bold text-foreground">
                  {(data.confidence_score * 100).toFixed(0)}%
                </span>
                <span
                  className="rounded-full px-2.5 py-0.5 text-xs font-semibold"
                  style={{
                    backgroundColor:
                      data.confidence_band === "HIGH" ? "#dcfce7" :
                      data.confidence_band === "MODERATE" ? "#fef3c7" :
                      data.confidence_band === "LOW" ? "#ffedd5" : "#fef2f2",
                    color:
                      data.confidence_band === "HIGH" ? "#16a34a" :
                      data.confidence_band === "MODERATE" ? "#d97706" :
                      data.confidence_band === "LOW" ? "#ea580c" : "#dc2626",
                  }}
                >
                  {data.confidence_band}
                </span>
              </div>

              {/* Overall bar */}
              <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${data.confidence_score * 100}%`,
                    backgroundColor:
                      data.confidence_band === "HIGH" ? "#16a34a" :
                      data.confidence_band === "MODERATE" ? "#d97706" :
                      data.confidence_band === "LOW" ? "#ea580c" : "#dc2626",
                  }}
                />
              </div>
            </div>

            {/* Feature breakdown */}
            {data.confidence_breakdown && (
              <div className="mb-6">
                <h3 className="mb-3 font-mono text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Feature Breakdown
                </h3>

                {Object.entries(data.confidence_breakdown).map(([feature, value]) => {
                  const displayNames: Record<string, string> = {
                    schedule_match: "Schedule Match",
                    area_coverage: "Area Coverage",
                    adversarial_resolved: "Adversarial Resolved",
                    constitutional_clean: "Constitutional",
                    cross_reference: "Panel Cross-Ref",
                    note_compliance: "Note Compliance",
                    reconciler_coverage: "Reconciler Coverage",
                  };
                  const numVal = value as number;
                  const pct = Math.round(numVal * 100);
                  const barColor =
                    pct >= 85 ? "#16a34a" :
                    pct >= 65 ? "#d97706" :
                    pct >= 40 ? "#ea580c" : "#dc2626";

                  return (
                    <div key={feature} className="mb-3">
                      <div className="mb-1 flex items-center justify-between">
                        <span className="text-xs text-muted-foreground">
                          {displayNames[feature] || feature.replace(/_/g, " ")}
                        </span>
                        <span className="font-mono text-xs font-semibold" style={{ color: barColor }}>
                          {pct >= 0 ? `${pct}%` : numVal === 1 ? "Clean \u2713" : `${pct}%`}
                        </span>
                      </div>
                      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${Math.max(0, pct)}%`,
                            backgroundColor: barColor,
                          }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Legend */}
            <div className="rounded-lg border border-border bg-sidebar p-4">
              <p className="mb-2 font-mono text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                Confidence Bands
              </p>
              {[
                { band: "HIGH", range: "85\u2013100%", color: "#16a34a" },
                { band: "MODERATE", range: "65\u201384%", color: "#d97706" },
                { band: "LOW", range: "40\u201364%", color: "#ea580c" },
                { band: "VERY LOW", range: "0\u201339%", color: "#dc2626" },
              ].map(({ band, range, color }) => (
                <div key={band} className="flex items-center justify-between py-0.5">
                  <span className="text-xs font-medium" style={{ color }}>{band}</span>
                  <span className="font-mono text-xs text-muted-foreground">{range}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* EXPORT */}
        {activeTab === "export" && (
          <div className="p-6">
            <div className="mb-6 flex flex-wrap gap-3">
              <button
                onClick={() => exportCSV(data)}
                className="flex items-center gap-2 rounded-lg border border-border bg-background px-4 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-muted"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>
                Download CSV
              </button>
              <button
                onClick={() => exportJSON(data)}
                className="flex items-center gap-2 rounded-lg border border-border bg-background px-4 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-muted"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>
                Download JSON
              </button>
              <button
                onClick={handleCopy}
                className="flex items-center gap-2 rounded-lg border border-border bg-background px-4 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-muted"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
                {copied ? "Copied!" : "Copy to Clipboard"}
              </button>
            </div>

            {/* Preview */}
            <div className="rounded-lg border border-border bg-sidebar p-4">
              <p className="mb-2 font-mono text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                Export Preview
              </p>
              <pre className="overflow-x-auto whitespace-pre font-mono text-xs leading-relaxed text-muted-foreground">
{`TYPE  DESCRIPTION                        COUNT  REVISED  DIFF
${"\u2500".repeat(68)}
${data.fixture_counts
  .map(
    (f) =>
      `${f.type_tag.padEnd(6)}${f.description.padEnd(35)}${String(f.total).padStart(5)}  ${String(f.revised ?? f.total).padStart(7)}  ${f.delta ? (f.delta > 0 ? "+" + f.delta : String(f.delta)) : "\u2014"}`
  )
  .join("\n")}
${"\u2500".repeat(68)}
${"TOTAL".padEnd(41)}${String(data.grand_total).padStart(5)}  ${String(data.revised_total ?? data.grand_total).padStart(7)}  ${(data.revised_total ?? data.grand_total) !== data.grand_total ? "+" + ((data.revised_total ?? data.grand_total) - data.grand_total) : ""}`}
              </pre>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
