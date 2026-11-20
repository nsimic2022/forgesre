"""ForgeAI digest: the morning mail of a scheduled report of kind "forgeai".

The mail has two parts. The facts come from the database and are always complete: incidents of the
last day (or of the report's interval, at most a week) by severity, what is still open and who
has it, the noisiest assets. Then ForgeAI's shift summary (task ``shift`` with the chosen skill),
asked once through ``forgeai.run`` so it passes the same checks and is kept in the ForgeAI log.
When ForgeAI is off, fails or its answer is rejected, the mail says so and the facts stand alone.

Sent by the ``forgeai_digest`` job (LLM lane), never inside a web request.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.email_html import esc, html_list, prose_to_html, render_email
from app.models import Incident, utcnow

ACTIVE = ("OPEN", "ACK", "INVESTIGATING", "ESCALATED")


def digest_facts(db: Session, hours: int) -> dict[str, Any]:
    """Counts and lists for the window. DEMO lab incidents are left out."""
    from app.services import is_demo_incident

    since = utcnow() - timedelta(hours=hours)
    rows = [
        i for i in db.query(Incident).options(joinedload(Incident.asset))
        .filter((Incident.started_at >= since) | Incident.status.in_(ACTIVE))
        .order_by(Incident.started_at.desc()).limit(500).all()
        if not is_demo_incident(i)
    ]
    new = [i for i in rows if i.started_at and _aware(i.started_at) >= since]
    still = [i for i in rows if (i.status or "").upper() in ACTIVE]
    severity = Counter((i.severity or "").upper() for i in new)
    hosts = Counter(i.asset.hostname for i in new if i.asset is not None)
    return {
        "hours": hours,
        "new": len(new),
        "critical": severity.get("CRITICAL", 0),
        "high": severity.get("HIGH", 0),
        "other": len(new) - severity.get("CRITICAL", 0) - severity.get("HIGH", 0),
        "resolved": sum(1 for i in new if (i.status or "").upper() in {"RESOLVED", "CLOSED"}),
        "open": [_line(i, owner=True) for i in still[:15]],
        "open_count": len(still),
        "noisy": [f"{host}: {count}" for host, count in hosts.most_common(5) if count > 1],
        "latest": [_line(i) for i in new[:10]],
    }


def _aware(value):
    from datetime import timezone

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _line(i: Incident, *, owner: bool = False) -> str:
    from app.services import compact_when_text

    host = i.asset.hostname if i.asset is not None else ""
    parts = [i.number.split("_")[0], (i.severity or "").upper(), i.title or "", host, compact_when_text(i.started_at)]
    if owner:
        parts.append(f"{i.status}, {('ack ' + i.ack_by) if i.ack_by else 'not acknowledged'}")
    return " · ".join(p for p in parts if p)


def ask_forgeai(db: Session, hours: int, actor: str) -> dict[str, Any]:
    """One shift-summary call. Returns {"answer": {...}} when used, else {"reason": "..."}."""
    from app import forgeai
    from app.settings import settings

    if not (settings.ai_enabled and settings.llm_url):
        return {"reason": "ForgeAI is off"}
    try:
        call = forgeai.run(db, "shift", None, actor=actor, hours=hours)
    except Exception as exc:  # the facts still go out
        return {"reason": f"ForgeAI could not be asked: {exc}"}
    if call.status == "used":
        return {"answer": dict(call.answer or {}), "call_id": call.id}
    return {"reason": f"ForgeAI's answer was not used ({call.status}: {call.reason or 'no reason'})"}


def digest_message(name: str, facts: dict[str, Any], ai: dict[str, Any] | None) -> tuple[str, str, str]:
    """(subject, text, html). ``ai`` None = preview: the ForgeAI part is described, not written."""
    window = "last 24 hours" if facts["hours"] == 24 else f"last {facts['hours'] // 24} days" if facts["hours"] % 24 == 0 else f"last {facts['hours']} hours"
    subject = f"ForgeSRE ForgeAI digest — {name}"
    head = (f"{facts['new']} new incidents in the {window} ({facts['critical']} critical, {facts['high']} high, "
            f"{facts['other']} other), {facts['resolved']} of them resolved. {facts['open_count']} open now.")
    answer = (ai or {}).get("answer") or {}
    if ai is None:
        ai_text = ["ForgeAI writes its summary here when the mail is sent (shift summary skill)."]
    elif answer:
        ai_text = [answer.get("summary", "")]
        ai_text += [f"Still open: {x}" for x in answer.get("still_open") or []]
        ai_text += [f"Watch: {x}" for x in answer.get("watch") or []]
    else:
        ai_text = [f"{ai.get('reason') or 'No ForgeAI summary'}. The facts above are complete."]
    text = [subject, "", head, "", "Open now:", *([f"- {x}" for x in facts["open"]] or ["- nothing"])]
    if facts["noisy"]:
        text += ["", "Most incidents:", *[f"- {x}" for x in facts["noisy"]]]
    text += ["", "Latest:", *([f"- {x}" for x in facts["latest"]] or ["- none"]), "", "ForgeAI (local model, from these facts):", *ai_text]
    sections = [
        ("At a glance", prose_to_html(head), True),
        ("Open now", html_list(facts["open"]) if facts["open"] else esc("Nothing is open."), False),
    ]
    if facts["noisy"]:
        sections.append(("Most incidents", html_list(facts["noisy"]), False))
    sections.append(("Latest", html_list(facts["latest"]) if facts["latest"] else esc("No new incidents."), False))
    sections.append(("ForgeAI", prose_to_html("\n".join(ai_text)), True))
    html = render_email(kicker="Digest", heading=subject, meta_rows=[("Window", esc(window)), ("Open now", esc(str(facts["open_count"])))],
                        sections=sections, footer="", kind="performance-report")
    return subject, "\n".join(text) + "\n", html


def send_digest(db: Session, *, recipients: list[str], actor: str, name: str, hours: int = 24, ask: bool = True):
    """Build the digest once and mail it to every address. Raises RuntimeError listing failed addresses."""
    from app.journal import report
    from app.services import send_outbound_mail

    addresses = [str(a).strip() for a in recipients or [] if str(a).strip()]
    if not addresses:
        raise ValueError("Need a valid email address")
    facts = digest_facts(db, hours)
    ai = ask_forgeai(db, hours, actor) if ask else {"reason": "ForgeAI was not asked"}
    subject, text, html = digest_message(name, facts, ai)
    last, problems = None, []
    for email in addresses:
        try:
            last = send_outbound_mail(db, target=email, subject=subject, body=text, actor=actor, step_key="report", html=html)
        except Exception as exc:
            problems.append(f"{email}: {exc}")
    report(db, "forgeai", "digest", "error" if problems else "ok",
           summary=f"ForgeAI digest {name} mailed to {len(addresses) - len(problems)}/{len(addresses)}"
                   f" ({'with' if ai.get('answer') else 'without'} the ForgeAI summary)",
           detail="; ".join(problems)[:2000] or str(ai.get("reason") or ""), object_type="insights", object_id=name)
    if problems:
        raise RuntimeError("; ".join(problems)[:400])
    return last
