import type { ReactNode } from "react";
import type { Tone } from "../lib/format";

export function Chip({ tone = "info", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`chip tone-${tone}`}>{children}</span>;
}

export function Dot({ tone }: { tone: Tone }) {
  return <span aria-hidden="true" className="dot" style={{ background: `var(--${tone === "info" ? "accent" : tone})` }} />;
}

export function Card({ title, actions, children }: { title?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {title || actions ? (
        <div className="card-head">
          {title ? <h2>{title}</h2> : <span />}
          {actions ? <div className="row">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function Stat({ label, value, detail, tone }: { label: string; value: string; detail?: string; tone?: Tone }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
        <span className="stat-value" style={tone ? { color: `var(--${tone})` } : undefined}>
          {value}
        </span>
        {detail ? <span className="small faint">{detail}</span> : null}
      </div>
    </div>
  );
}

export function Bar({ percent, tone = "ok" }: { percent: number; tone?: Tone }) {
  return (
    <div className="bar" aria-hidden="true">
      <span style={{ width: `${Math.max(0, Math.min(100, percent))}%`, background: `var(--${tone === "info" ? "accent" : tone})` }} />
    </div>
  );
}
