/** Incidents per day, stacked by severity (critical at the bottom). Shared by Reports and TV mode. */
export type Day = { day: string; critical: number; high: number; other: number };

export function DailyBars({ daily, height = 150, label = true, axis = false, unit = "day" }: { daily: Day[]; height?: number; label?: boolean; axis?: boolean; unit?: string }) {
  const peak = Math.max(1, ...daily.map((d) => d.critical + d.high + d.other));
  const total = daily.reduce((s, d) => s + d.critical + d.high + d.other, 0);
  return (
    <div className="cell-stack" style={{ gap: 8 }}>
      <div role="img" aria-label={`Incidents per ${unit} over ${daily.length} ${unit}s: ${total} in total, highest ${peak} in one ${unit}`} style={{ display: "flex", alignItems: "flex-end", gap: 3, height, borderBottom: "1px solid var(--border)" }}>
        {daily.map((d) => (
          <div key={d.day} title={`${d.day}: ${d.critical} critical, ${d.high} high, ${d.other} other`} style={{ flex: "1 1 0", display: "flex", flexDirection: "column", justifyContent: "flex-end", height: "100%" }}>
            <div style={{ height: `${(d.other / peak) * 100}%`, background: "var(--accent)" }} />
            <div style={{ height: `${(d.high / peak) * 100}%`, background: "var(--high)" }} />
            <div style={{ height: `${(d.critical / peak) * 100}%`, background: "var(--crit)" }} />
          </div>
        ))}
      </div>
      {axis ? (
        <div style={{ display: "flex", gap: 3 }} aria-hidden="true" className="small faint">
          {daily.map((d, i) => {
            const every = Math.max(1, Math.ceil(daily.length / 8));
            return <span key={d.day} style={{ flex: "1 1 0", textAlign: "center", whiteSpace: "nowrap", overflow: "visible" }}>{i % every === 0 || i === daily.length - 1 ? (unit === "day" ? d.day.slice(5).split("-").reverse().join(".") : d.day) : ""}</span>;
          })}
        </div>
      ) : null}
      {label ? (
        <div className="row small muted">
          <span className="row" style={{ gap: 6 }}><span style={{ width: 10, height: 10, background: "var(--crit)" }} />Critical</span>
          <span className="row" style={{ gap: 6 }}><span style={{ width: 10, height: 10, background: "var(--high)" }} />High</span>
          <span className="row" style={{ gap: 6 }}><span style={{ width: 10, height: 10, background: "var(--accent)" }} />Other</span>
        </div>
      ) : null}
    </div>
  );
}
