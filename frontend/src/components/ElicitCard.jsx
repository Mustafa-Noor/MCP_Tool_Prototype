import { useState } from "react"

export default function ElicitCard({ elicit, onRespond }) {
  const [text, setText] = useState("")
  const [formValues, setFormValues] = useState({})

  if (!elicit) return null

  const decline = () => onRespond(elicit.id, "decline")

  if (elicit.kind === "choice") {
    return (
      <div className="elicit-card">
        <p className="elicit-message">{elicit.message}</p>
        <div className="elicit-choices">
          {elicit.choices.map((choice) => (
            <button
              key={choice}
              className="elicit-choice-btn"
              onClick={() => onRespond(elicit.id, "accept", choice)}
            >
              {choice}
            </button>
          ))}
        </div>
        <button className="elicit-decline" onClick={decline}>
          Cancel
        </button>
      </div>
    )
  }

  if (elicit.kind === "form") {
    const submit = (e) => {
      e.preventDefault()
      onRespond(elicit.id, "accept", formValues)
    }
    return (
      <form className="elicit-card" onSubmit={submit}>
        <p className="elicit-message">{elicit.message}</p>
        {elicit.fields.map((field) => (
          <label key={field.name} className="elicit-field">
            <span>
              {field.label}
              {field.required ? " *" : " (optional)"}
            </span>
            <input
              type="text"
              value={formValues[field.name] || ""}
              onChange={(e) => setFormValues((v) => ({ ...v, [field.name]: e.target.value }))}
              required={field.required}
            />
          </label>
        ))}
        <div className="elicit-actions">
          <button type="submit" className="elicit-submit">
            Submit
          </button>
          <button type="button" className="elicit-decline" onClick={decline}>
            Cancel
          </button>
        </div>
      </form>
    )
  }

  // text
  const submit = (e) => {
    e.preventDefault()
    onRespond(elicit.id, "accept", text)
  }
  return (
    <form className="elicit-card" onSubmit={submit}>
      <p className="elicit-message">{elicit.message}</p>
      <div className="elicit-actions">
        <input
          className="elicit-text-input"
          type="text"
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button type="submit" className="elicit-submit">
          Submit
        </button>
        <button type="button" className="elicit-decline" onClick={decline}>
          Cancel
        </button>
      </div>
    </form>
  )
}
