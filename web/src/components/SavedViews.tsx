import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";

export type ViewFilters = Record<string, string>;
type View = { id: number; name: string; filters: ViewFilters; team: string; mine: boolean; can_delete: boolean };
type ViewList = { views: View[]; can_share: boolean; can_save: boolean };

/**
 * Saved views of a list: one click applies a named set of filters. Your own views, then the ones
 * an Engineer shared with a team. Saving needs Analyst; sharing with a team needs Engineer.
 */
export function SavedViews({ page, current, teams, onApply }: { page: string; current: ViewFilters; teams: string[]; onApply: (f: ViewFilters) => void }) {
  const [list, setList] = useState<ViewList | null>(null);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");
  const [team, setTeam] = useState("");
  const [error, setError] = useState("");
  const load = () => getJson<ViewList>(`/api/v1/views?page=${page}`).then(setList).catch(() => setList(null));
  useEffect(() => void load(), [page]);
  const views = Array.isArray(list?.views) ? list!.views : [];
  const same = (f: ViewFilters) => Object.keys({ ...f, ...current }).every((k) => (f[k] || "") === (current[k] || ""));
  const save = async () => {
    setError("");
    try {
      await postJson("/api/v1/views", { page, name, team, filters: current });
      setSaving(false);
      setName("");
      setTeam("");
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const remove = async (id: number) => {
    await getJson(`/api/v1/views/${id}`, { method: "DELETE" }).catch((e: Error) => setError(e.message));
    await load();
  };
  if (!list) return null;
  return (
    <div className="row small" style={{ gap: 6, flexWrap: "wrap" }} aria-label="Saved views">
      <span className="faint">Views:</span>
      {views.length === 0 ? <span className="faint">none yet</span> : null}
      {views.map((v) => (
        <span key={v.id} className="row" style={{ gap: 2 }}>
          <button className="btn" type="button" aria-pressed={same(v.filters)} onClick={() => onApply(v.filters)} title={v.team ? `Shared with ${v.team}` : "Only you"}>
            {v.team ? <span className="faint">{v.team} · </span> : null}{v.name}
          </button>
          {v.can_delete ? <button className="btn" type="button" aria-label={`Delete view ${v.name}`} onClick={() => void remove(v.id)}>×</button> : null}
        </span>
      ))}
      {list.can_save && !saving ? <button className="btn" type="button" onClick={() => setSaving(true)}>Save view</button> : null}
      {saving ? (
        <form className="row" style={{ gap: 6 }} onSubmit={(e) => (e.preventDefault(), void save())}>
          <input className="search" aria-label="View name" placeholder="Name, e.g. Network critical" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          {list.can_share ? (
            <select className="search" aria-label="Share with" value={team} onChange={(e) => setTeam(e.target.value)}>
              <option value="">Only me</option>
              {teams.map((t) => <option key={t} value={t}>Team {t}</option>)}
            </select>
          ) : null}
          <button className="btn primary" type="submit" disabled={!name.trim()}>Save</button>
          <button className="btn" type="button" onClick={() => setSaving(false)}>Cancel</button>
        </form>
      ) : null}
      {error ? <span role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
    </div>
  );
}
