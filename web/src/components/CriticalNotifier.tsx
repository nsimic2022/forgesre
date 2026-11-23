import { useEffect, useRef, useState } from "react";
import { getJson } from "../lib/api";
import { beep, notifyPrefs } from "../lib/notify";

type NewCritical = { latest: number; items: { number: string; title: string; host: string }[] };
const EVERY_MS = 30_000;

/**
 * Rings and/or shows a desktop notification when a new CRITICAL incident opens (not one
 * suppressed under a parent). Off until turned on in Settings › Appearance, per browser.
 * The first poll only learns the newest incident, so opening a tab never rings for old ones.
 */
export function CriticalNotifier() {
  const [prefs, setPrefs] = useState(notifyPrefs);
  const after = useRef(-1); // -1: first poll only learns the newest incident id
  useEffect(() => {
    const changed = () => setPrefs(notifyPrefs());
    window.addEventListener("forgesre-notify", changed);
    return () => window.removeEventListener("forgesre-notify", changed);
  }, []);
  useEffect(() => {
    if (!prefs.sound && !prefs.desktop) return;
    let stopped = false;
    const poll = async () => {
      try {
        const got = await getJson<NewCritical>(after.current < 0 ? "/api/v1/notify/critical" : `/api/v1/notify/critical?after=${after.current}`);
        if (stopped || typeof got?.latest !== "number") return;
        const items = Array.isArray(got.items) ? got.items : [];
        after.current = Math.max(after.current, got.latest);
        if (!items.length) return;
        if (prefs.sound) beep();
        if (prefs.desktop && "Notification" in window && Notification.permission === "granted") {
          for (const it of items.slice(0, 3)) {
            const n = new Notification(`CRITICAL ${it.number.split("_")[0]}`, { body: [it.host, it.title].filter(Boolean).join(" · "), tag: it.number });
            n.onclick = () => (window.focus(), (window.location.href = `/app/incidents/${encodeURIComponent(it.number)}`));
          }
        }
      } catch {
        /* signed out or Core restarting: try again next round */
      }
    };
    void poll();
    const t = window.setInterval(poll, EVERY_MS);
    return () => ((stopped = true), window.clearInterval(t));
  }, [prefs]);
  return null;
}
