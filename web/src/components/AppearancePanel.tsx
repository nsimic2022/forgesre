import { useState } from "react";
import { beep, notifyPrefs, saveNotifyPrefs, type NotifyPrefs } from "../lib/notify";
import { THEMES, applyTheme, savedTheme } from "../lib/theme";
import { Card } from "./ui";

/** Settings › Appearance: sound and desktop notification for a new critical, in this browser. */
export function AlertPrefs() {
  const [prefs, setPrefs] = useState<NotifyPrefs>(notifyPrefs);
  const [note, setNote] = useState("");
  const update = async (next: NotifyPrefs) => {
    if (next.desktop && !prefs.desktop) {
      if (!("Notification" in window)) return setNote("This browser has no desktop notifications.");
      const answer = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      if (answer !== "granted") return setNote("The browser blocked notifications; allow them for this site and try again.");
    }
    if (next.sound && !prefs.sound) beep();
    setNote("");
    setPrefs(next);
    saveNotifyPrefs(next);
  };
  return (
    <Card title="Alerts in this browser">
      <span className="small faint">A new CRITICAL incident (not one suppressed under a parent) rings and/or pops up while ForgeSRE is open in this browser. Checked every 30 s.</span>
      <label className="row small" style={{ gap: 8 }}>
        <input type="checkbox" checked={prefs.sound} onChange={(e) => void update({ ...prefs, sound: e.target.checked })} /> Play a sound
      </label>
      <label className="row small" style={{ gap: 8 }}>
        <input type="checkbox" checked={prefs.desktop} onChange={(e) => void update({ ...prefs, desktop: e.target.checked })} /> Desktop notification
      </label>
      {note ? <span className="small" role="status">{note}</span> : null}
    </Card>
  );
}

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
