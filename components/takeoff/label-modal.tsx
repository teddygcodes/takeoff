"use client";

import { useState } from "react";

interface LabelModalProps {
  onSave: (label: string, subLabel: string) => void;
  onCancel: () => void;
}

const LABEL_OPTIONS = [
  { value: "fixture_schedule", label: "Fixture Schedule" },
  { value: "rcp", label: "RCP (Reflected Ceiling Plan)" },
  { value: "panel_schedule", label: "Panel Schedule" },
  { value: "plan_notes", label: "Plan Notes / Specs" },
  { value: "detail", label: "Detail Drawing" },
  { value: "site_plan", label: "Site Plan" },
];

export function LabelModal({ onSave, onCancel }: LabelModalProps) {
  const [selected, setSelected] = useState("rcp");
  const [areaName, setAreaName] = useState("");

  const handleSave = () => {
    if (selected === "rcp" && !areaName.trim()) return;
    onSave(selected, areaName.trim());
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/20 backdrop-blur-[2px]">
      <div className="w-[320px] rounded-xl border border-border bg-background p-5 shadow-xl">
        <h3 className="mb-1 text-base font-semibold text-foreground">Label this snippet</h3>
        <div className="mb-4 h-px bg-border" />

        {/* Radio options */}
        <div className="mb-4 space-y-2">
          {LABEL_OPTIONS.map((opt) => (
            <label
              key={opt.value}
              className="flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 transition-colors hover:bg-muted"
            >
              <div
                className={`flex h-4 w-4 items-center justify-center rounded-full border-2 transition-colors ${
                  selected === opt.value ? "border-accent bg-accent" : "border-border"
                }`}
              >
                {selected === opt.value && <div className="h-1.5 w-1.5 rounded-full bg-white" />}
              </div>
              <span className="text-sm text-foreground">{opt.label}</span>
            </label>
          ))}
        </div>

        {/* Area name field for RCP */}
        {selected === "rcp" && (
          <div className="mb-4">
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Area name <span className="text-accent">*</span>
            </label>
            <input
              type="text"
              value={areaName}
              onChange={(e) => setAreaName(e.target.value)}
              placeholder="e.g. Floor 2 North Wing"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
              autoFocus
            />
          </div>
        )}

        {/* Footer */}
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={selected === "rcp" && !areaName.trim()}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
