import { useEffect, useMemo, useState } from "react"
import { fetchSkills } from "../api"

export default function Sidebar() {
  const [skills, setSkills] = useState([])
  const [error, setError] = useState(null)
  const [query, setQuery] = useState("")

  useEffect(() => {
    fetchSkills()
      .then((data) => setSkills(data.skills || []))
      .catch((err) => setError(err.message))
  }, [])

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = q
      ? skills.filter(
          (s) =>
            s.id.toLowerCase().includes(q) ||
            s.name.toLowerCase().includes(q) ||
            s.description.toLowerCase().includes(q)
        )
      : skills

    const byCategory = new Map()
    for (const skill of filtered) {
      const key = skill.category || "other"
      if (!byCategory.has(key)) byCategory.set(key, [])
      byCategory.get(key).push(skill)
    }
    return [...byCategory.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [skills, query])

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <h2>Skills</h2>
        <span className="skill-count">{skills.length}</span>
      </div>
      <input
        className="sidebar-search"
        type="text"
        placeholder="Search skills…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {error && <p className="sidebar-error">{error}</p>}
      <div className="sidebar-list">
        {grouped.map(([category, items]) => (
          <div key={category} className="sidebar-category">
            <div className="sidebar-category-title">{category.replace(/_/g, " ")}</div>
            {items.map((skill) => (
              <div key={skill.id} className="skill-item" title={skill.description}>
                <span className="skill-name">{skill.name}</span>
                <span className="skill-id">{skill.id}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </aside>
  )
}
