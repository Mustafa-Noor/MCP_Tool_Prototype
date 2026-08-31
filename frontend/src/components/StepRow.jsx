const STATUS_ICON = {
  running: "⏳",
  ok: "✓",
  error: "✕",
}

function ParamsLine({ params }) {
  const entries = Object.entries(params || {})
  if (entries.length === 0) return null
  return (
    <div className="step-params">
      {entries.map(([key, value]) => (
        <span key={key} className="param-chip">
          <span className="param-key">{key}</span>=<span className="param-value">{String(value)}</span>
        </span>
      ))}
    </div>
  )
}

export default function StepRow({ step }) {
  return (
    <div className={`step-row step-${step.status}`}>
      <div className="step-head">
        <span className={`step-status-icon status-${step.status}`}>{STATUS_ICON[step.status] || "…"}</span>
        <span className="step-skill">{step.skillId}</span>
      </div>
      <ParamsLine params={step.params} />
      {step.status === "error" && <div className="step-error">{step.error}</div>}
      {step.status === "ok" && step.output != null && (
        <pre className="step-output">{JSON.stringify(step.output, null, 2)}</pre>
      )}
    </div>
  )
}
