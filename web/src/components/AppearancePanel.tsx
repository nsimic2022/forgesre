import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { beep, currentPush, notifyPrefs, saveNotifyPrefs, subscribePush, type NotifyPrefs } from "../lib/notify";
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

type PushInfo = { enabled: boolean; public_key: string; devices: number };

/**
 * Settings › Appearance › Push to this device: new CRITICAL incidents also when ForgeSRE is
 * closed (phone app or browser). Shown only when an Admin turned on notifications.push.enabled.
 */
export function PushPrefs() {
  const [info, setInfo] = useState<PushInfo | null>(null);
  const [on, setOn] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    getJson<PushInfo>("/api/v1/push").then(setInfo).catch(() => setInfo(null));
    currentPush().then((s) => setOn(Boolean(s))).catch(() => setOn(false));
  }, []);
  if (!info?.enabled) return null;
  const supported = "serviceWorker" in navigator && "PushManager" in window;
  const toggle = async (want: boolean) => {
    setBusy(true);
    setNote("");
    try {
      if (want) {
        const answer = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
        if (answer !== "granted") throw new Error("The browser blocked notifications; allow them for this site and try again.");
        const sub = await subscribePush(info.public_key);
        await postJson("/api/v1/push/subscribe", { endpoint: sub.endpoint, keys: sub.keys });
      } else {
        const sub = await currentPush();
        if (sub) {
          await postJson("/api/v1/push/unsubscribe", { endpoint: sub.endpoint, keys: {} });
          await sub.unsubscribe();
        }
      }
      setOn(want);
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Push to this device">
      <span className="small faint">A new CRITICAL incident pops up on this phone or computer even when ForgeSRE is closed. On an iPhone, install the app first (Share › Add to Home Screen), then turn this on inside it. Messages pass encrypted through the browser maker's push service.</span>
      {supported ? (
        <label className="row small" style={{ gap: 8 }}>
          <input type="checkbox" checked={on} disabled={busy} onChange={(e) => void toggle(e.target.checked)} /> Push to this device
        </label>
      ) : <span className="small">This browser has no push support (on an iPhone: open the installed app).</span>}
      {on ? <button className="btn" type="button" onClick={() => void postJson("/api/v1/push/test", {}).then(() => setNote("Test sent; it arrives within a minute.")).catch((e: Error) => setNote(e.message))}>Send a test</button> : null}
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
