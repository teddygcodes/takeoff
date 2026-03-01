"use client";

interface PipelineStep {
  label: string;
  status: "done" | "running" | "pending";
  detail?: string;
}

interface PipelineOverlayProps {
  steps: PipelineStep[];
  progress: number;
  currentMessage: string;
}

export function PipelineOverlay({ steps, progress, currentMessage }: PipelineOverlayProps) {
  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-foreground/10 backdrop-blur-[2px]">
      <div className="w-[400px] rounded-xl border border-border bg-background p-6 shadow-2xl">
        {/* Header */}
        <div className="mb-4 flex items-center gap-3">
          <svg className="h-5 w-5 animate-spin text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" />
          </svg>
          <h3 className="text-base font-semibold text-foreground">Running Takeoff...</h3>
        </div>

        {/* Progress bar */}
        <div className="mb-5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-accent transition-all duration-500"
            style={{ width: `${progress}%` }}
          />
        </div>

        {/* Steps */}
        <div className="mb-4 space-y-2.5">
          {steps.map((step, i) => (
            <div key={i} className="flex items-start gap-3">
              {/* Status icon */}
              <div className="mt-0.5 flex-shrink-0">
                {step.status === "done" && (
                  <div className="flex h-5 w-5 items-center justify-center rounded-full bg-green-100 text-green-600">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                  </div>
                )}
                {step.status === "running" && (
                  <div className="flex h-5 w-5 items-center justify-center rounded-full bg-red-100">
                    <div className="h-2 w-2 animate-pulse rounded-full bg-accent" />
                  </div>
                )}
                {step.status === "pending" && (
                  <div className="flex h-5 w-5 items-center justify-center rounded-full border border-border">
                    <div className="h-1.5 w-1.5 rounded-full bg-muted-foreground/30" />
                  </div>
                )}
              </div>

              {/* Step label */}
              <div>
                <p
                  className={`text-sm ${
                    step.status === "done"
                      ? "text-green-600"
                      : step.status === "running"
                      ? "font-medium text-foreground"
                      : "text-muted-foreground"
                  }`}
                >
                  {step.label}
                  {step.detail && step.status === "done" && (
                    <span className="ml-1 text-muted-foreground">{"\u2014"} {step.detail}</span>
                  )}
                </p>
              </div>
            </div>
          ))}
        </div>

        {/* Current message */}
        <p className="text-xs text-muted-foreground">{currentMessage}</p>
      </div>
    </div>
  );
}
