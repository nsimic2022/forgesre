import { Card } from "../components/ui";
import { useNoc } from "./Noc";
import { IncidentsNav } from "./Incidents";

// On-call tab. Shows who is on call now; the week grid and automatic messages come with
// the escalation rework (roadmap Phase 3). Editing stays on the classic Escalation page.
export function OnCallPage() {
  const { data } = useNoc();
  return (
    <>
      <header className="page-head">
        <div>
          <h1>On-call</h1>
          <span className="muted">Who answers now</span>
        </div>
        <a className="btn" href="/escalation">Edit rota (classic)</a>
      </header>
      <IncidentsNav />
      <Card title="On call now">
        {data?.oncall ? (
          <div className="cell-stack">
            <span style={{ fontWeight: 600 }}>{data.oncall.name}</span>
            <span className="small muted">{[data.oncall.phone, data.oncall.email].filter(Boolean).join(" · ")}</span>
          </div>
        ) : (
          <span className="muted">Nobody on the roster for this hour.</span>
        )}
      </Card>
    </>
  );
}
