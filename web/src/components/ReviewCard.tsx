import { useState, type FormEvent } from "react";
import { postJson, type Rating, type Review } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

export const RATING_TONE: Record<string, "ok" | "warn" | "crit" | "off"> = { correct: "ok", partly: "warn", wrong: "crit", "": "off" };
export const RATING_LABEL: Record<string, string> = { correct: "Correct", partly: "Partly", wrong: "Wrong", "": "Not rated" };

function RatingPicker({ name, label, value, onChange }: { name: string; label: string; value: Rating; onChange: (v: Rating) => void }) {
  return (
    <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
      <legend className="small faint" style={{ marginBottom: 6 }}>{label}</legend>
      <div className="row" role="radiogroup">
        {(["correct", "partly", "wrong", ""] as Rating[]).map((r) => (
          <label key={r || "none"} className="btn" style={{ display: "inline-flex", gap: 6, alignItems: "center", borderColor: value === r ? "var(--accent)" : undefined }}>
            <input type="radio" name={name} checked={value === r} onChange={() => onChange(r)} style={{ accentColor: "var(--accent)" }} />
            {RATING_LABEL[r]}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** Confirm the real cause and rate ForgeRCA / ForgeAI. Feeds accuracy and the knowledge base. */
export function ReviewCard({ number, review, canEdit, hasAi, onSaved }: { number: string; review: Review; canEdit: boolean; hasAi: boolean; onSaved: () => void }) {
  const [form, setForm] = useState({ root_cause: review.root_cause, rca_rating: review.rca_rating, ai_rating: review.ai_rating });
  const [editing, setEditing] = useState(!review.root_cause);
  const [error, setError] = useState("");
  const save = (e: FormEvent) => {
    e.preventDefault();
    postJson(`/api/v1/incidents/${encodeURIComponent(number)}/review`, form)
      .then(() => (setEditing(false), setError(""), onSaved()))
      .catch((err: Error) => setError(err.message));
  };
  if (!canEdit || !editing) {
    return (
      <Card title="Confirmed cause" actions={canEdit && review.root_cause ? <button className="btn" onClick={() => setEditing(true)}>Change</button> : null}>
        {review.root_cause ? (
          <>
            <p style={{ margin: 0 }}>{review.root_cause}</p>
            <div className="row">
              <span className="small faint">ForgeRCA</span><Chip tone={RATING_TONE[review.rca_rating]}>{RATING_LABEL[review.rca_rating]}</Chip>
              <span className="small faint">ForgeAI</span><Chip tone={RATING_TONE[review.ai_rating]}>{RATING_LABEL[review.ai_rating]}</Chip>
            </div>
            <span className="small faint">{review.reviewed_by} · {clock(review.reviewed_at)}</span>
          </>
        ) : (
          <span className="muted">Not reviewed yet.</span>
        )}
      </Card>
    );
  }
  return (
    <Card title="Confirm the cause">
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        <label className="cell-stack small faint">
          What really caused it
          <textarea className="search" rows={3} style={{ padding: 10, minHeight: 70 }} value={form.root_cause} onChange={(e) => setForm({ ...form, root_cause: e.target.value })} />
        </label>
        <RatingPicker name="rca" label="Was ForgeRCA right?" value={form.rca_rating} onChange={(v) => setForm({ ...form, rca_rating: v })} />
        {hasAi ? <RatingPicker name="ai" label="Was ForgeAI right?" value={form.ai_rating} onChange={(v) => setForm({ ...form, ai_rating: v })} /> : null}
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div><button className="btn primary" type="submit" disabled={!form.root_cause.trim()}>Save review</button></div>
        <span className="small faint">Reviews measure how often ForgeRCA and ForgeAI are right, and teach the similar-incident search.</span>
      </form>
    </Card>
  );
}
