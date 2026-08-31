const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000"
const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8000/ws/plan"

export async function fetchSkills() {
  const res = await fetch(`${API_BASE}/api/skills`)
  if (!res.ok) throw new Error(`Failed to load skills (${res.status})`)
  return res.json()
}

export async function fetchHealth() {
  const res = await fetch(`${API_BASE}/api/health`)
  if (!res.ok) throw new Error(`Backend unreachable (${res.status})`)
  return res.json()
}

export { WS_URL }
