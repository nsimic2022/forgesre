import { useState } from "react";
import { THEMES, applyTheme, savedTheme } from "../lib/theme";
import { Card } from "./ui";

/** Settings › Appearance: pick a colour theme for this browser. */
export function AppearancePanel() {
  const [current, setCurrent] = useState(savedTheme);
  const pick = (id: string) => (applyTheme(id), setCurrent(id));
  return (
    <Card title="Theme">
      <span className="small faint">Saved in this browser only, so a wall screen can use high contrast while desks keep their own.</span>
      <div role="radiogroup" aria-label="Theme" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 12 }}>
        {THEMES.map((t) => (
          <button key={t.id} role="radio" aria-checked={current === t.id} className="card" onClick={() => pick(t.id)}
            style={{ textAlign: "left", cursor: "pointer", gap: 8, padding: 14, outline: current === t.id ? "2px solid var(--accent)" : "none", color: "var(--text)" }}>
            <div className="row" style={{ gap: 4 }}>
              {t.swatch.map((c) => <span key={c} aria-hidden="true" style={{ width: 28, height: 20, borderRadius: 4, background: c, border: "1px solid var(--border)" }} />)}
            </div>
            <strong>{t.label}</strong>
            <span className="small muted">{t.note}</span>
          </button>
        ))}
      </div>
    </Card>
  );
}
