// Per-browser alert preferences: a short sound and/or a desktop notification for a new critical.
export type NotifyPrefs = { sound: boolean; desktop: boolean };
const KEY = "forgesre.notify";

export function notifyPrefs(): NotifyPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
    return { sound: raw.sound === true, desktop: raw.desktop === true };
  } catch {
    return { sound: false, desktop: false };
  }
}

export function saveNotifyPrefs(prefs: NotifyPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    /* private mode */
  }
  window.dispatchEvent(new Event("forgesre-notify"));
}

/** Two short tones made in the browser (no sound file, nothing downloaded). */
export function beep(): void {
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    [0, 0.25].forEach((at, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = i ? 660 : 880;
      gain.gain.setValueAtTime(0.15, ctx.currentTime + at);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + at + 0.2);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + at);
      osc.stop(ctx.currentTime + at + 0.2);
    });
    window.setTimeout(() => void ctx.close(), 800);
  } catch {
    /* audio blocked until the user clicks once; nothing else to do */
  }
}

const b64ToBytes = (b64: string) => {
  const raw = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};

/** This browser's push subscription, or null (no service worker / push support / not subscribed). */
export async function currentPush(): Promise<PushSubscription | null> {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return null;
  const reg = await navigator.serviceWorker.getRegistration("/app/");
  return reg ? reg.pushManager.getSubscription() : null;
}

/** Subscribe this browser with the appliance's VAPID key; returns the subscription JSON to store. */
export async function subscribePush(publicKey: string): Promise<PushSubscriptionJSON> {
  const reg = (await navigator.serviceWorker.getRegistration("/app/")) ?? (await navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }));
  await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(publicKey) });
  return sub.toJSON();
}
