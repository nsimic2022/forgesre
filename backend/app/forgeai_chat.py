"""ForgeAI chat: conversations with the model, private to the person who started them.

A chat may be about one incident (the model then gets that incident's facts, like "Ask ForgeAI") or
about anything else (general NOC questions; see forgeai.CHAT_RULES). Each question runs as a
background job on the LLM lane; the answer is stored as a ForgeAiChatMessage that points at the
logged ForgeAiCall, so every prompt stays visible in Investigations.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import ForgeAiChat, ForgeAiChatMessage, ForgeAiSkill, Incident, Job, utcnow

JOB_KIND = "forgeai_chat"
HISTORY_TURNS = 6
PENDING_MAX = 3
TEXT_MAX = 2000
# Skills a person may pick in the composer. Incident-only tasks need a chat about an incident.
INCIDENT_TASKS = {"incident", "explain", "postmortem", "investigate", "ask"}


def _number(incident: Incident | None) -> str:
    return incident.number.split("_")[0] if incident is not None else ""


def chat_as_dict(chat: ForgeAiChat, incident: Incident | None = None, messages: list[ForgeAiChatMessage] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": chat.id, "title": chat.title, "incident": incident.number if incident is not None else None,
        "incident_label": f"{_number(incident)} {incident.title}" if incident is not None else "",
        "created_at": chat.created_at.isoformat() if chat.created_at else None,
        "updated_at": chat.updated_at.isoformat() if chat.updated_at else None,
    }
    if messages is not None:
        data["messages"] = [message_as_dict(m) for m in messages]
    return data


def message_as_dict(m: ForgeAiChatMessage) -> dict[str, Any]:
    return {"id": m.id, "role": m.role, "text": m.text, "skill": m.skill, "cloud": bool(m.cloud), "status": m.status,
            "call_id": m.call_id, "at": m.at.isoformat() if m.at else None}


def list_chats(db: Session, owner: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query(ForgeAiChat).filter_by(owner=owner).order_by(ForgeAiChat.updated_at.desc(), ForgeAiChat.id.desc()).limit(limit).all()
    ids = {r.incident_id for r in rows if r.incident_id}
    incidents = {i.id: i for i in db.query(Incident).filter(Incident.id.in_(ids)).all()} if ids else {}
    return [chat_as_dict(r, incidents.get(r.incident_id)) for r in rows]


def get_chat(db: Session, chat_id: int, owner: str) -> ForgeAiChat | None:
    """The chat if it belongs to ``owner``; other people's chats are not found, not forbidden."""
    chat = db.get(ForgeAiChat, chat_id)
    return chat if chat is not None and chat.owner == owner else None


def chat_detail(db: Session, chat: ForgeAiChat) -> dict[str, Any]:
    incident = db.get(Incident, chat.incident_id) if chat.incident_id else None
    messages = db.query(ForgeAiChatMessage).filter_by(chat_id=chat.id).order_by(ForgeAiChatMessage.id).all()
    return chat_as_dict(chat, incident, messages)


def open_chat(db: Session, owner: str, *, title: str = "", incident: Incident | None = None, reuse: bool = False) -> ForgeAiChat:
    """Start a chat. ``reuse`` returns the owner's latest chat about the same incident instead, so the
    incident page always shows one running conversation."""
    if reuse and incident is not None:
        found = (db.query(ForgeAiChat).filter_by(owner=owner, incident_id=incident.id)
                 .order_by(ForgeAiChat.updated_at.desc()).first())
        if found is not None:
            return found
    default = f"{_number(incident)} {incident.title}" if incident is not None else "New chat"
    chat = ForgeAiChat(owner=owner, title=(" ".join(title.split()) or default)[:255],
                       incident_id=incident.id if incident is not None else None)
    db.add(chat)
    db.commit()
    return chat


def skills_for(db: Session, incident: bool) -> list[dict[str, Any]]:
    """Skills the composer offers: chat skills always, incident skills only in a chat about an incident."""
    from app.forgeai import TASKS, seed_skills

    seed_skills(db)
    rows = db.query(ForgeAiSkill).filter_by(enabled=True).order_by(ForgeAiSkill.task, ForgeAiSkill.priority, ForgeAiSkill.name).all()
    allowed = {"chat"} | (INCIDENT_TASKS - {"ask"} if incident else set())
    return [{"slug": s.slug, "name": s.name, "task": s.task, "task_label": TASKS.get(s.task, {}).get("label", s.task)}
            for s in rows if s.task in allowed]


def pending_count(db: Session, owner: str) -> int:
    return (db.query(ForgeAiChatMessage).join(ForgeAiChat, ForgeAiChat.id == ForgeAiChatMessage.chat_id)
            .filter(ForgeAiChat.owner == owner, ForgeAiChatMessage.status == "pending").count())


def ask(db: Session, chat: ForgeAiChat, text: str, *, skill: ForgeAiSkill | None, cloud: bool, actor: str) -> tuple[ForgeAiChatMessage, ForgeAiChatMessage]:
    """Store the question and a pending answer, and queue the model call (LLM lane).

    The caller checks roles, the cloud switch and PENDING_MAX. Raises ValueError for a skill that
    does not fit this chat.
    """
    from app.jobs import enqueue

    text = text.strip()[:TEXT_MAX]
    if not text and (skill is None or skill.task == "chat"):
        raise ValueError("type a message")
    if skill is not None and skill.task in INCIDENT_TASKS and not chat.incident_id:
        raise ValueError("this skill needs a chat about an incident")
    shown = text or f"/{skill.slug}"
    first = db.query(ForgeAiChatMessage).filter_by(chat_id=chat.id, role="user").count() == 0
    question = ForgeAiChatMessage(chat_id=chat.id, role="user", text=shown, skill=skill.slug if skill else "", cloud=cloud)
    answer = ForgeAiChatMessage(chat_id=chat.id, role="assistant", text="", skill=skill.slug if skill else "", cloud=cloud, status="pending")
    db.add_all([question, answer])
    if first and chat.title == "New chat":
        chat.title = shown[:60]
    chat.updated_at = utcnow()
    db.flush()
    enqueue(db, JOB_KIND, str(answer.id), object_type="forgeai_chat",
            payload={"chat_id": chat.id, "question_id": question.id, "question": text, "actor": actor, "cloud": cloud})
    db.commit()
    return question, answer


def format_answer(task: str, answer: dict[str, Any]) -> str:
    """Turn a structured answer (cause, first_checks, …) into readable chat text."""
    if task == "chat":
        return str(answer.get("answer") or "")
    names = {"cause": "Cause", "first_checks": "First checks", "for_shift_lead": "For the shift lead",
             "explanation": "", "why_it_matters": "Why it matters", "paragraph": "", "answer": "",
             "not_in_facts": "Not in the facts", "confidence": "Confidence", "next_steps": "Next steps"}
    parts: list[str] = []
    for key, value in answer.items():
        if key in {"evidence", "extras"} or value in (None, "", []):
            continue
        label = names.get(key, key.replace("_", " ").capitalize())
        if isinstance(value, list):
            items = "\n".join(f"{n}. {v}" for n, v in enumerate(value, 1))
            parts.append(f"{label}:\n{items}" if label else items)
        else:
            parts.append(f"{label}: {value}" if label else str(value))
    return "\n\n".join(parts)


def run_answer(db: Session, row: Job) -> dict[str, Any]:
    """Job handler: ask the model for one pending answer and store it in the chat.

    A failed or rejected call still finishes the message (status error, with the reason), so the chat
    never waits for ever.
    """
    from app import forgeai

    payload = row.payload or {}
    answer = db.get(ForgeAiChatMessage, int(row.object_id))
    chat = db.get(ForgeAiChat, int(payload.get("chat_id") or 0))
    question = db.get(ForgeAiChatMessage, int(payload.get("question_id") or 0))
    if answer is None or chat is None or question is None:
        return {"status": "gone"}
    incident = (db.query(Incident).options(joinedload(Incident.asset)).filter_by(id=chat.incident_id).first()
                if chat.incident_id else None)
    skill = db.query(ForgeAiSkill).filter_by(slug=answer.skill).first() if answer.skill else None
    task = skill.task if skill is not None else "chat"
    text = str(payload.get("question") or "")
    earlier = (db.query(ForgeAiChatMessage).filter(ForgeAiChatMessage.chat_id == chat.id, ForgeAiChatMessage.id < question.id,
                                                    ForgeAiChatMessage.status == "done")
               .order_by(ForgeAiChatMessage.id.desc()).limit(HISTORY_TURNS).all())
    history = [{"role": m.role, "text": m.text} for m in reversed(earlier)]
    actor = str(payload.get("actor") or chat.owner)
    try:
        if task == "chat":
            call = forgeai.run(db, "chat", incident, skill_slug=skill.slug if skill else "", actor=actor, question=text,
                               cloud=bool(answer.cloud), history=history)
        else:
            if task == "ask" and not text:
                raise ValueError("type a question for this skill")
            call = forgeai.run(db, task, incident, skill_slug=skill.slug, actor=actor, trial=True, question=text,
                               cloud=bool(answer.cloud) and task != "investigate")
    except (ValueError, LookupError) as exc:
        answer.status, answer.text = "error", f"ForgeAI could not answer: {exc}"
    else:
        answer.call_id = call.id
        if call.status in {"trial", "used"}:
            answer.status, answer.text = "done", format_answer(task, call.answer or {}) or "(empty answer)"
        else:
            answer.status, answer.text = "error", f"ForgeAI could not answer: {call.reason or call.status}"
    answer.at = utcnow()
    chat.updated_at = utcnow()
    db.commit()
    return {"status": answer.status, "call_id": answer.call_id}


def fail_answer(db: Session, row: Job, why: str) -> None:
    """Mark a pending answer as failed when its job gave up (crash or timeout)."""
    try:
        answer = db.get(ForgeAiChatMessage, int(row.object_id))
    except (TypeError, ValueError):
        return
    if answer is not None and answer.status == "pending":
        answer.status, answer.text = "error", f"ForgeAI could not answer: {why[:300]}"
        db.commit()

