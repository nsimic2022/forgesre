import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getJson } from "../lib/api";
import { loadIncidentList, TAB_LABEL } from "../lib/incidentList";
import { shortNumber } from "../lib/format";

type Neighbour = { number: string; title: string } | null;
type Neighbours = { number: string; tab: string; position: number | null; total: number; prev: Neighbour; next: Neighbour };

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return Boolean(el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)));
}

/**
 * Bar at the top of an incident: back to the incident list (same tab, filters and page), and
 * arrows to the previous / next incident of that list, newest first. ← and → do the same.
 */
export function IncidentListNav({ number }: { number: string }) {
  const navigate = useNavigate();
  const ctx = loadIncidentList();
  const [data, setData] = useState<Neighbours | null>(null);
  const params = new URLSearchParams({ tab: ctx.tab, severity: ctx.severity, q: ctx.q, date_from: ctx.date_from, date_to: ctx.date_to, team: ctx.team });
  const query = params.toString();
  useEffect(() => {
    setData(null);
    getJson<Neighbours>(`/api/v1/incidents/${encodeURIComponent(number)}/neighbours?${query}`).then(setData).catch(() => setData(null));
  }, [number, query]);
  const go = (n: Neighbour) => n && navigate(`/incidents/${encodeURIComponent(n.number)}`);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || typing(e.target) || !data) return;
      if (e.key === "ArrowLeft" && data.prev) (e.preventDefault(), go(data.prev));
      if (e.key === "ArrowRight" && data.next) (e.preventDefault(), go(data.next));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  const filtered = Boolean(ctx.severity || ctx.q || ctx.date_from || ctx.date_to || ctx.team);
  const where = `${TAB_LABEL[data?.tab ?? ctx.tab] ?? "All"}${filtered ? ", filtered" : ""}`;
  return (
    <nav className="incident-nav" aria-label="Incident list">
      <button type="button" className="btn" disabled={!data?.prev} onClick={() => go(data?.prev ?? null)}
        title={data?.prev ? `${shortNumber(data.prev.number)} · ${data.prev.title} (←)` : "This is the first incident of the list"}>
        ‹ Previous
      </button>
      <Link to="/incidents" className="btn incident-nav-back">
        Back to incidents
        <span className="small faint">
          {data ? (data.position ? ` · ${data.position} of ${data.total} · ${where}` : ` · not in ${where} any more`) : ""}
        </span>
      </Link>
      <button type="button" className="btn" disabled={!data?.next} onClick={() => go(data?.next ?? null)}
        title={data?.next ? `${shortNumber(data.next.number)} · ${data.next.title} (→)` : "This is the last incident of the list"}>
        Next ›
      </button>
    </nav>
  );
}
