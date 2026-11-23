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
