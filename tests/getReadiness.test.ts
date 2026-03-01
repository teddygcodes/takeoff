import { describe, it, expect } from "vitest";
import { getReadiness } from "@/components/takeoff/snippet-tray";
import type { Snippet } from "@/lib/types";

function makeSnippet(label: Snippet["label"], id = label, subLabel = ""): Snippet {
  return {
    id,
    label,
    sub_label: subLabel,
    page_number: 1,
    bbox: { x: 0, y: 0, width: 100, height: 100 },
  };
}

describe("getReadiness", () => {
  it("not ready when no snippets — message mentions both Fixture Schedule and RCP", () => {
    const r = getReadiness([]);
    expect(r.ready).toBe(false);
    expect(r.message).toContain("Fixture Schedule");
    expect(r.message).toContain("RCP");
    expect(r.counts).toEqual({});
  });

  it("not ready when only RCP present — message asks for Fixture Schedule", () => {
    const r = getReadiness([makeSnippet("rcp")]);
    expect(r.ready).toBe(false);
    expect(r.message).toContain("Fixture Schedule");
    expect(r.counts["rcp"]).toBe(1);
  });

  it("not ready when only fixture_schedule present — message asks for RCP", () => {
    const r = getReadiness([makeSnippet("fixture_schedule")]);
    expect(r.ready).toBe(false);
    expect(r.message).toContain("RCP");
    expect(r.counts["fixture_schedule"]).toBe(1);
  });

  it("ready with 1 fixture_schedule + 1 rcp — message has both counts", () => {
    const r = getReadiness([
      makeSnippet("fixture_schedule"),
      makeSnippet("rcp"),
    ]);
    expect(r.ready).toBe(true);
    expect(r.message).toContain("1 RCP");
    expect(r.message).toContain("1 Schedule");
  });

  it("pluralizes RCPs when multiple RCP snippets", () => {
    const r = getReadiness([
      makeSnippet("fixture_schedule"),
      makeSnippet("rcp", "rcp-1"),
      makeSnippet("rcp", "rcp-2"),
      makeSnippet("rcp", "rcp-3"),
    ]);
    expect(r.ready).toBe(true);
    expect(r.message).toContain("3 RCPs");
    expect(r.counts["rcp"]).toBe(3);
  });

  it("includes panel schedule count in ready message when present", () => {
    const r = getReadiness([
      makeSnippet("fixture_schedule"),
      makeSnippet("rcp"),
      makeSnippet("panel_schedule"),
    ]);
    expect(r.ready).toBe(true);
    expect(r.message).toContain("1 Panel");
    expect(r.counts["panel_schedule"]).toBe(1);
  });

  it("does not include plan_notes, detail, site_plan in the ready message", () => {
    const r = getReadiness([
      makeSnippet("fixture_schedule"),
      makeSnippet("rcp"),
      makeSnippet("plan_notes"),
      makeSnippet("detail"),
      makeSnippet("site_plan"),
    ]);
    expect(r.ready).toBe(true);
    expect(r.counts["plan_notes"]).toBe(1);
    expect(r.counts["detail"]).toBe(1);
    expect(r.counts["site_plan"]).toBe(1);
    // Non-message labels should not appear in the summary message
    expect(r.message).not.toMatch(/notes|detail|site/i);
  });

  it("accumulates counts correctly for duplicate labels", () => {
    const snippets = [
      makeSnippet("fixture_schedule", "fs1"),
      makeSnippet("fixture_schedule", "fs2"),
      makeSnippet("rcp", "r1"),
      makeSnippet("rcp", "r2"),
      makeSnippet("rcp", "r3"),
    ];
    const r = getReadiness(snippets);
    expect(r.counts["fixture_schedule"]).toBe(2);
    expect(r.counts["rcp"]).toBe(3);
    expect(r.ready).toBe(true);
    expect(r.message).toContain("2 Schedule");
  });

  it("ready message uses middot (U+00B7) separator between parts", () => {
    const r = getReadiness([
      makeSnippet("fixture_schedule"),
      makeSnippet("rcp"),
      makeSnippet("panel_schedule"),
    ]);
    expect(r.ready).toBe(true);
    expect(r.message).toContain("\u00B7");
  });
});
