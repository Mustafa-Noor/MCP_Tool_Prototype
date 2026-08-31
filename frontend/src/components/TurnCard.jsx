import StepRow from "./StepRow"
import ElicitCard from "./ElicitCard"

export default function TurnCard({ turn, onRespondElicit }) {
  const hasSteps = turn.plan && turn.plan.length > 0

  return (
    <div className="turn">
      <div className="turn-user-bubble">{turn.query}</div>

      <div className="turn-assistant">
        {turn.subtasks && turn.subtasks.length > 1 && (
          <div className="subtasks-row">
            {turn.subtasks.map((s, i) => (
              <span key={i} className="subtask-chip">
                {s}
              </span>
            ))}
          </div>
        )}

        {turn.candidates && turn.candidates.length > 0 && (
          <details className="candidates-block">
            <summary>{turn.candidates.length} candidate skill(s) retrieved</summary>
            <ul>
              {turn.candidates.map((c) => (
                <li key={c.id}>
                  <span className="candidate-score">{c.similarity.toFixed(3)}</span>
                  <span className="candidate-id">{c.id}</span>
                  <span className="candidate-name">{c.name}</span>
                </li>
              ))}
            </ul>
          </details>
        )}

        {hasSteps && (
          <div className="steps-block">
            {turn.steps.map((step) => (
              <StepRow key={step.index} step={step} />
            ))}
          </div>
        )}

        {turn.status === "running" && !hasSteps && !turn.elicit && (
          <div className="turn-thinking">
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
          </div>
        )}

        {turn.elicit && <ElicitCard elicit={turn.elicit} onRespond={onRespondElicit} />}

        {turn.reply && <div className="turn-reply">{turn.reply}</div>}

        {turn.error && <div className="turn-error">{turn.error}</div>}
      </div>
    </div>
  )
}
