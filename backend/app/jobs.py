"""Background jobs. Postgres durability, no Redis/Celery.

Periodic SNMP / unmanaged reachability runs on the event loop
(``run_periodic_infrastructure_check``): own AsyncSession, overlap lock,
``asyncio.gather`` for the probes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload, selectinload

from app.journal import report
from app.models import Asset, Incident, Job, utcnow

log = logging.getLogger("forgesre")

DISCOVERY_SCAN_KIND = "discovery_scan"
DISCOVERY_SCAN_OBJECT_ID = "scan"
# Re-running these is safe: builtin RCA returns the existing result, an LLM rewrite only adds
# a new Investigation row, a discovery scan upserts candidates. None of them sends mail.
FORGEAI_DIGEST_KIND = "forgeai_digest"
RESTART_RETRY_KINDS = ("investigate", DISCOVERY_SCAN_KIND)
RESTART_MAX_ATTEMPTS = 3
INTERRUPTED_NOTE = "interrupted by Core restart"


def enqueue(db: Session, kind: str, object_id: str, object_type: str = "incident", payload: dict | None = None) -> Job | None:
    existing = (
        db.query(Job)
        .filter(
            Job.kind == kind,
            Job.object_id == object_id,
            Job.status.in_(["pending", "running"]),
        )
        .first()
    )
    if existing:
        return existing
    row = Job(
        kind=kind,
        status="pending",
        object_type=object_type,
        object_id=object_id,
        payload=payload or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def active_discovery_scan(db: Session) -> Job | None:
    """Pending or running discovery probe, if any."""
    return (
        db.query(Job)
        .filter(
            Job.kind == DISCOVERY_SCAN_KIND,
            Job.object_id == DISCOVERY_SCAN_OBJECT_ID,
            Job.status.in_(["pending", "running"]),
        )
        .order_by(Job.id.desc())
        .first()
    )


def latest_discovery_scan(db: Session) -> Job | None:
    """Newest finished LibreNMS read (done or error), for "last read" on Add devices."""
    return (
        db.query(Job)
        .filter(
            Job.kind == DISCOVERY_SCAN_KIND,
            Job.object_id == DISCOVERY_SCAN_OBJECT_ID,
            Job.status.in_(["done", "error"]),
        )
        .order_by(Job.id.desc())
        .first()
    )


def enqueue_discovery_scan(db: Session, *, actor: str = "system") -> Job | None:
    """Queue a read of LibreNMS's devices and links (deduped while one is pending or running)."""
    return enqueue(
        db,
        DISCOVERY_SCAN_KIND,
        DISCOVERY_SCAN_OBJECT_ID,
        object_type="discovery",
        payload={"actor": actor},
    )


def recover_interrupted_jobs(db: Session) -> dict[str, int]:
    """Core startup, before the jobs loop: a `running` row belonged to the old process and nobody will finish it.

    Retryable kinds go back to pending (until RESTART_MAX_ATTEMPTS); the rest become error so a new
    job can queue. Either way the row stops blocking enqueue() and the "LLM pending" pill.
    """
    rows = db.query(Job).filter(Job.status == "running").order_by(Job.id).all()
    requeued = failed = 0
    for row in rows:
        if row.kind in RESTART_RETRY_KINDS and int(row.attempts or 0) < RESTART_MAX_ATTEMPTS:
            row.status = "pending"
            row.started_at = None
            row.error = f"{INTERRUPTED_NOTE}; queued again"
            requeued += 1
        else:
            row.status = "error"
            row.finished_at = utcnow()
            row.error = f"{INTERRUPTED_NOTE}; not retried (attempt {int(row.attempts or 0)})"
            failed += 1
            if row.kind == "forgeai_chat":
                from app.forgeai_chat import fail_answer

                fail_answer(db, row, INTERRUPTED_NOTE)
    if rows:
        db.commit()
        log.warning("jobs: %d requeued, %d marked error after restart", requeued, failed)
        report(
            db,
            "jobs",
            "recover",
            "error" if failed else "ok",
            summary=f"{len(rows)} job(s) were running when Core stopped: {requeued} queued again, {failed} marked error",
            detail=", ".join(f"#{row.id} {row.kind} {row.object_id} → {row.status}" for row in rows)[:2000],
        )
    return {"requeued": requeued, "failed": failed}


def _json_safe(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, default=str))


LLM_KINDS = (FORGEAI_DIGEST_KIND, "forgeai", "llm_download", "forgeai_replay", "forgeai_chat", "forgeai_ping", "forgeai_golden", "forgeai_compare", "flow_ai")


def job_is_llm(row: Job) -> bool:
    """True when this investigate job will wait on llama.cpp."""
    if row.kind in LLM_KINDS:
        return True
    if row.kind != "investigate":
        return False
    payload = row.payload or {}
    return payload.get("use_llm", True) is not False


def llm_order(rows: list[Job]) -> list[Job]:
    """The order the ForgeAI lane works in: chat answers (someone waits), then critical incidents,
    then high, then the rest, oldest first within each."""
    rank = {"CRITICAL": 1, "HIGH": 2}

    def key(row: Job) -> tuple[int, int]:
        if row.kind == "forgeai_chat":
            return (0, row.id)
        if row.kind == "investigate":
            return (rank.get(str((row.payload or {}).get("severity") or "").upper(), 3), row.id)
        return (3, row.id)
    return sorted(rows, key=key)


LANE_DEFAULT = "default"
LANE_LLM = "llm"


def next_pending_job(db: Session, lane: str | None = None) -> Job | None:
    """Oldest pending job for this lane.

    lane "llm" = ForgeAI rewrites only, lane "default" = everything else, None = both
    (builtin RCA before LLM). The LLM lane has its own thread so a slow model never
    holds RCA, discovery or reports.
    """
    if lane == LANE_LLM:
        # filtered in SQL, so a backlog of ordinary jobs can never hide a waiting chat answer
        rows = (db.query(Job).filter(Job.status == "pending", Job.kind.in_((*LLM_KINDS, "investigate")))
                .order_by(Job.id).limit(64).all())
        return next(iter(llm_order([row for row in rows if job_is_llm(row)])), None)
    pending = db.query(Job).filter_by(status="pending").order_by(Job.id).limit(32).all()
    if lane == LANE_DEFAULT:
        return next((row for row in pending if not job_is_llm(row)), None)
    if not pending:
        return None
    for row in pending:
        if not job_is_llm(row):
            return row
    return pending[0]


def llm_lane(db: Session) -> dict:
    """What the ForgeAI lane is doing: waiting jobs, the one running now and for how long."""
    rows = (db.query(Job).filter(Job.status.in_(("pending", "running")), Job.kind.in_((*LLM_KINDS, "investigate")))
            .order_by(Job.id).limit(200).all())
    rows = [r for r in rows if job_is_llm(r)]
    running = next((r for r in rows if r.status == "running"), None)
    now = utcnow()

    def age(at) -> int:
        if at is None:
            return 0
        if at.tzinfo is None:
            at = at.replace(tzinfo=now.tzinfo)
        return max(0, int((now - at).total_seconds()))

    return {"pending": sum(r.status == "pending" for r in rows),
            "pending_ids": [r.id for r in rows if r.status == "pending"],
            "running": {"id": running.id, "kind": running.kind, "seconds": age(running.started_at)} if running else None}


def claim_job(db: Session, row: Job) -> bool:
    """Atomically move one job from pending to running.

    A conditional UPDATE, so when several Core processes poll the same table only one
    of them gets the job; the others see rowcount 0 and skip it.
    """
    result = db.execute(
        update(Job)
        .where(Job.id == row.id, Job.status == "pending")
        .values(status="running", started_at=utcnow(), attempts=Job.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:
        return False
    db.refresh(row)
    return True


def run_pending_jobs(db: Session, limit: int = 8, lane: str | None = None) -> int:
    """Claim and run up to ``limit`` pending jobs of one lane (see next_pending_job).

    Each job is claimed with claim_job, so running this from several processes is safe.
    A failing job is marked "error" and reported to the journal; the loop goes on.
    """
    from app.services import queue_llm_rewrite, run_investigation

    done = 0
    for _ in range(limit):
        row = next_pending_job(db, lane)
        if row is None:
            break
        if lane is None and job_is_llm(row) and done > 0:
            # Single-lane mode: leave the rewrite pending so reports go out first.
            break
        if not claim_job(db, row):
            continue
        try:
            use_llm = False
            incident = None
            if row.kind == "investigate":
                incident = (
                    db.query(Incident)
                    .options(joinedload(Incident.asset), selectinload(Incident.evidence))
                    .filter_by(number=row.object_id)
                    .first()
                )
                if incident is None:
                    raise RuntimeError(f"incident {row.object_id} not found")
                payload = row.payload or {}
                use_llm = payload.get("use_llm", True) is not False
                actor = str(payload.get("actor") or "system")
                if use_llm:
                    # ForgeRCA stays rule-based; ForgeAI then asks the model once with the matching skill.
                    from app import forgeai

                    run_investigation(db, incident, actor=actor, force=False, use_llm=False)
                    call = forgeai.run(db, "incident", incident, skill_slug=str(payload.get("skill") or ""), actor=actor)
                    row.payload = {**payload, "call_id": call.id, "status": call.status}
                else:
                    run_investigation(db, incident, actor=actor, force=bool(payload.get("force")), use_llm=False)
            elif row.kind == DISCOVERY_SCAN_KIND:
                from app.librenms_discovery import not_ready
                from app.topology import sync_librenms

                # Discovery is LibreNMS's: read its devices and links (HTTP timeouts and backoff
                # in app.librenms). ForgeSRE never sweeps a network itself.
                why = not_ready()
                if why:
                    raise RuntimeError(why)
                payload = dict(row.payload or {})
                payload["result"] = sync_librenms(db)
                row.payload = payload
            elif row.kind == FORGEAI_DIGEST_KIND:
                from app.services import send_forgeai_digest

                # Facts from the database, then one ForgeAI shift summary; LLM lane, SMTP has its own timeout.
                payload = row.payload or {}
                send_forgeai_digest(
                    db,
                    recipients=list(payload.get("recipients") or []),
                    actor=str(payload.get("actor") or "system"),
                    name=str(payload.get("name") or "forgeai-digest"),
                    hours=int(payload.get("hours") or 24),
                )
            elif row.kind == "k8s_sync":
                from app.kubernetes import sync_all

                # Read-only GETs to every configured cluster, 10 s timeout each.
                payload = dict(row.payload or {})
                payload["result"] = sync_all(db)
                row.payload = payload
            elif row.kind == "flow_check":
                from app.flows import notice_changes, previous_result, run_check

                # Network steps (3 s each) and read-only API calls with each integration's timeout.
                payload = dict(row.payload or {})
                before = previous_result(db, row.object_id, before_job=row.id)
                payload["result"] = run_check(db, row.object_id)
                # a block that broke since the last check goes to the journal (UI badge) and to ForgeAI
                payload["broke"] = notice_changes(db, row.object_id, before, payload["result"])
                row.payload = payload
            elif row.kind == "flow_ai":
                from app.flows import run_ai

                # One ForgeAI analysis of a new break; the model call has its own timeout.
                payload = dict(row.payload or {})
                payload["result"] = run_ai(db, row.object_id, str(payload.get("question") or ""))
                row.payload = payload
            elif row.kind == "health_checks":
                from app.health_checks import run_checks

                # TLS handshakes (10 s timeout each), backup age, heartbeats; alerts via ingest.
                payload = dict(row.payload or {})
                payload["result"] = run_checks(db)
                row.payload = payload
            elif row.kind == "topology_sync":
                from app.topology import sync_librenms

                # LibreNMS LLDP/CDP links; the client has its own timeouts and backoff.
                payload = dict(row.payload or {})
                payload["result"] = sync_librenms(db)
                row.payload = payload
            elif row.kind == "backup_create":
                from app.backup import create_backup
                from app.journal import report as journal_report

                payload = dict(row.payload or {})
                result = create_backup(include_secrets=payload.get("include_secrets", True) is not False,
                                       include_models=bool(payload.get("include_models")))
                payload["result"] = result.name
                row.payload = payload
                journal_report(db, "backup", "create", "ok", summary=f"Wrote {result.name}", object_type="backup", object_id=result.name)
            elif row.kind == "backup_restore":
                from app.backup import CONFIRM_WORD, backup_ident, restore_archive, resolve_archive
                from app.journal import report as journal_report

                # Replaces the database: this job row may not exist afterwards, so the outcome goes to the journal.
                path = resolve_archive(row.object_id)
                actor = str((row.payload or {}).get("actor") or "system")
                try:
                    outcome = restore_archive(path, confirm=CONFIRM_WORD, stop_core=False)
                except Exception as exc:
                    db.rollback()
                    journal_report(db, "backup", "restore", "error", summary=f"Restore of {row.object_id} failed", detail=str(exc)[:2000])
                    raise
                db.rollback()
                journal_report(db, "backup", "restore", "ok", summary=f"Restored {backup_ident(path)} (by {actor})",
                               detail="\n".join(outcome.get("notes") or []), object_type="backup", object_id=backup_ident(path))
                continue
            elif row.kind == "llm_download":
                from app.llm_models import run_download

                # A model download (minutes to an hour) on the LLM lane: alarms and reports keep running.
                result = run_download(db, row)
                row.payload = {**(row.payload or {}), "result": result}
            elif row.kind == "forgeai":
                from app import forgeai

                # Explain, postmortem, shift summary or a skill trial; one model call with its own timeout.
                payload = dict(row.payload or {})
                target = None
                if payload.get("task") != "shift":
                    number = row.object_id.split("#", 1)[0]
                    target = db.query(Incident).options(joinedload(Incident.asset)).filter_by(number=number).first()
                    if target is None:
                        raise RuntimeError(f"incident {row.object_id} not found")
                if payload.get("task") == "critic":
                    # second opinion from the cloud model on one earlier answer (forgeai.critique)
                    from app.models import ForgeAiCall

                    reviewed = db.get(ForgeAiCall, int(payload.get("call_id") or 0))
                    if reviewed is None:
                        raise RuntimeError("the answer to review is gone")
                    call = forgeai.critique(db, reviewed, actor=str(payload.get("actor") or "system"))
                else:
                    call = forgeai.run(db, str(payload.get("task") or "incident"), target, skill_slug=str(payload.get("skill") or ""),
                                       actor=str(payload.get("actor") or "system"), trial=bool(payload.get("trial")),
                                       instructions=payload.get("instructions"), question=str(payload.get("question") or ""),
                                       cloud=bool(payload.get("cloud")))
                payload.pop("instructions", None)
                payload["result"] = {"call_id": call.id, "status": call.status}
                row.payload = payload
            elif row.kind == "forgeai_golden":
                from app.forgeai_golden import run as golden_run

                # every golden case once, as trial calls; each has the model's own timeout
                done = golden_run(db, skill=str((row.payload or {}).get("skill") or ""), actor=str((row.payload or {}).get("actor") or "system"))
                row.payload = {**(row.payload or {}), "result": {"run_id": done.id, "passed": done.passed, "total": done.total}}
            elif row.kind == "forgeai_compare":
                from app.forgeai_golden import compare

                # golden set on model A, then B, then the first model back; holds the LLM lane meanwhile
                p = row.payload or {}
                cid = compare(db, str(p.get("a") or ""), str(p.get("b") or ""), skill=str(p.get("skill") or ""), actor=str(p.get("actor") or "system"))
                row.payload = {**p, "result": {"compare": cid}}
            elif row.kind == "forgeai_ping":
                from app.forgeai import ping

                # One tiny timed question to the local model (Settings and chat status line).
                row.payload = {**(row.payload or {}), "result": ping(db)}
            elif row.kind == "forgeai_chat":
                from app.forgeai_chat import run_answer

                # One chat answer (app.forgeai_chat); the model call has its own timeout.
                row.payload = {**(row.payload or {}), "result": run_answer(db, row)}
            elif row.kind == "doc_index":
                from app.doc_search import index_doc

                # Text extraction and local embeddings (each call has its own timeout).
                payload = dict(row.payload or {})
                payload["result"] = index_doc(db, int(row.object_id))
                row.payload = payload
            elif row.kind == "onboard_identify":
                from app.onboarding import identify

                # One address: TCP and exporter probes (0.5 s each), one SNMP GET (1.5 s).
                payload = dict(row.payload or {})
                snmp = payload.pop("snmp", None) or {}
                payload["result"] = _json_safe(identify(str(payload.get("ip") or ""), snmp, int(payload.get("snmp_port") or 161)))
                row.payload = payload  # the SNMP community / v3 passwords are not kept on the job
            elif row.kind == "asset_verify":
                from app.onboarding import verify

                payload = dict(row.payload or {})
                payload["result"] = _json_safe(verify(db, row.object_id))
                row.payload = payload
            elif row.kind == "forgeai_replay":
                from app.forgeai_tuning import replay

                # One model call per incident, one at a time, within a 15 minute budget; every answer is a trial.
                payload = dict(row.payload or {})
                payload["result"] = replay(db, row.object_id, limit=int(payload.get("limit") or 10),
                                           instructions=payload.pop("instructions", None), actor=str(payload.get("actor") or "system"))
                row.payload = payload
            elif row.kind == "demo_run":
                from app.demo_view import run_scenario

                # Lab incident on a forge-demo-* asset: ForgeRCA and the first escalation step (mail has its timeout).
                payload = dict(row.payload or {})
                payload["result"] = run_scenario(db, row.object_id)
                row.payload = payload
            elif row.kind == "incident_mail":
                from app.incident_mail import run_mail

                # One incident report per address; SMTP has its own timeout. Each is an outbox row.
                payload = dict(row.payload or {})
                payload["result"] = run_mail(db, row.object_id.split("#", 1)[0], payload)
                payload.pop("body", None)
                row.payload = payload
            elif row.kind == "asset_import":
                from app.asset_import import run_import

                # CSV rows checked by the preview; the create arguments are dropped once done.
                payload = dict(row.payload or {})
                payload["result"] = _json_safe(run_import(db, list(payload.pop("rows", None) or []), str(payload.get("actor") or "system")))
                row.payload = payload
            elif row.kind == "asset_capacity":
                from app.asset_capacity import run_capacity

                # ~30 days of disk and interface receive; 3 s per Prometheus query, Zabbix its own timeout.
                payload = dict(row.payload or {})
                payload["result"] = _json_safe(run_capacity(db, row.object_id))
                row.payload = payload
            elif row.kind == "snmp_apply":
                from app.snmp_apply import apply_auths

                # Rewrites the auth block in the generated snmp.yml, then POST /-/reload (5 s timeout).
                payload = dict(row.payload or {})
                payload["result"] = apply_auths(db)
                row.payload = payload
            elif row.kind == "integration_sync":
                from app.integrations_view import run_sync

                # Zabbix / GLPI / NetBox / LibreNMS read with their own client timeouts.
                payload = dict(row.payload or {})
                payload["result"] = _json_safe(run_sync(db, row.object_id))
                row.payload = payload
            elif row.kind == "report_send":
                from app.models import ScheduledReport
                from app.services import _report_job, _send_report_job

                # "Send now" from Reports: SMTP and Prometheus have their own timeouts; Next is not moved.
                scheduled = db.get(ScheduledReport, int(row.object_id))
                if scheduled is None:
                    raise RuntimeError(f"report {row.object_id} not found")
                _send_report_job(db, _report_job(scheduled), str((row.payload or {}).get("actor") or "system"))
            elif row.kind == "sms_send":
                from app.sms import deliver as sms_deliver

                # One SMS through the gateway, 10 s timeout; the result is on the Notification row.
                sms_deliver(db, int(row.object_id))
            elif row.kind in {"ticket_create", "ticket_sync", "ticket_webhook"}:
                from app import tickets

                # GLPI ticket / status sync / outbound webhook; every call has a 10 s timeout.
                if row.kind == "ticket_sync":
                    result = tickets.sync_glpi_tickets(db)
                elif row.kind == "ticket_webhook":
                    result = {"ok": tickets.deliver_webhook(db, int(row.object_id))}
                else:
                    result = tickets.run_job(db, row.payload or {}, str((row.payload or {}).get("actor") or "system"))
                row.payload = {**(row.payload or {}), "result": result}
            elif row.kind == "push_send":
                from app.web_push import run_job as push_run

                # encrypted web push to each subscription, 10 s timeout per push service call
                row.payload = {**(row.payload or {}), "result": push_run(db, row.payload or {})}
            elif row.kind == "oxidized_sync":
                from app.oxidized import run_job as oxidized_run

                # Oxidized /nodes.json, 5 s timeout; new config versions become changes.
                row.payload = {**(row.payload or {}), "result": oxidized_run(db)}
            elif row.kind == "chat_post":
                from app.chat import deliver

                # Slack / Teams / Discord webhook, 10 s timeout; the result is on the Notification row.
                deliver(db, int(row.object_id))
            else:
                raise RuntimeError(f"unknown job kind {row.kind}")
            row.status = "done"
            row.finished_at = utcnow()
            row.error = ""
            db.commit()
            done += 1
            if row.kind == "investigate" and incident is not None and not use_llm:
                queue_llm_rewrite(
                    db,
                    incident,
                    actor=str((row.payload or {}).get("actor") or "system"),
                )
        except Exception as exc:
            log.exception("job %s %s failed", row.kind, row.object_id)
            if row.kind == "onboard_identify":
                row.payload = {k: v for k, v in (row.payload or {}).items() if k != "snmp"}  # never keep SNMP secrets
            row.status = "error"
            row.finished_at = utcnow()
            row.error = str(exc)[:2000]
            db.commit()
            if row.kind == "forgeai_chat":
                from app.forgeai_chat import fail_answer

                fail_answer(db, row, str(exc))  # the chat shows the error instead of waiting
            module = "discovery" if row.kind == DISCOVERY_SCAN_KIND else "rca"
            action = "scan" if row.kind == DISCOVERY_SCAN_KIND else "job"
            report(
                db,
                module,
                action,
                "error",
                summary=f"Job {row.kind} failed for {row.object_id}",
                detail=str(exc),
                object_type=row.object_type,
                object_id=row.object_id,
            )
    return done


def job_counts(db: Session) -> dict[str, int]:
    """Number of jobs per status, for the CLI and System Health."""
    from sqlalchemy import func

    rows = db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    return {str(status): int(count) for status, count in rows}


def list_jobs(db: Session, limit: int = 50) -> list[Job]:
    return db.query(Job).order_by(Job.id.desc()).limit(limit).all()


INFRA_SCAN_INTERVAL_SECONDS = 30
# Skip a new cycle when the previous gather is still waiting on slow ICMP/SNMP.
_infrastructure_scan_lock = asyncio.Lock()


def _infra_scan_under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def infrastructure_scan_asset(asset: Asset) -> bool:
    """Network / SNMP / unmanaged rows with an IP. Lab demo rows stay out."""
    from app.demo_ids import is_lab_inventory_row
    from app.inventory import asset_kind, is_snmp_asset

    if is_lab_inventory_row(asset):
        return False
    if not (asset.ip or "").strip():
        return False
    if is_snmp_asset(asset):
        return True
    kind = asset_kind(asset.type or "", asset.monitoring_profile or "")
    extras = asset.extras if isinstance(asset.extras, dict) else {}
    if kind == "network":
        return True
    return str(extras.get("snmp_poll") or "").strip() in {"1", "true", "yes", "on"}


async def run_periodic_infrastructure_check(
    *,
    force: bool = False,
    probe_fn: Callable[..., Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """One scan cycle: own AsyncSession, parallel probes, one commit.

    Overlap: if the previous cycle still holds ``_infrastructure_scan_lock``,
    this call returns immediately so jobs cannot pile up and exhaust the pool.
    Network work is ``asyncio.gather`` + ``to_thread`` (probe_target is sync).
    Session writes stay sequential after the gather — one AsyncSession is not
    safe for concurrent awaits.
    """
    from app.asset_probe import LIST_PROBE_TIMEOUT, apply_probe_to_asset, asset_as_probe_item, probe_target
    from app.db import AsyncSessionLocal

    if _infrastructure_scan_lock.locked():
        log.warning("infrastructure scan still running; skip this cycle")
        return {"skipped": True, "reason": "overlap", "scanned": 0, "updated": 0}
    if _infra_scan_under_pytest() and not force:
        return {"skipped": True, "reason": "pytest", "scanned": 0, "updated": 0}

    async with _infrastructure_scan_lock:
        session = AsyncSessionLocal()
        scanned = 0
        updated = 0
        errors = 0
        try:
            rows = list(await session.scalars(select(Asset).where(Asset.ip != "")))
            assets = [row for row in rows if infrastructure_scan_asset(row)]
            if not assets:
                await session.commit()
                return {"skipped": False, "reason": "", "scanned": 0, "updated": 0, "errors": 0}
            scanned = len(assets)
            log.info("infrastructure scan start count=%s", scanned)
            wait = LIST_PROBE_TIMEOUT if timeout is None else timeout
            run = probe_fn or probe_target

            async def _probe_one(asset: Asset):
                item = asset_as_probe_item(asset)
                return asset, await asyncio.to_thread(run, item, timeout=wait)

            gathered = await asyncio.gather(
                *[_probe_one(asset) for asset in assets],
                return_exceptions=True,
            )
            for item in gathered:
                if isinstance(item, BaseException):
                    errors += 1
                    log.exception("infrastructure probe failed: %s", item)
                    continue
                asset, probe = item
                before = (asset.ping_status, asset.exporter_status)
                apply_probe_to_asset(asset, probe)
                reachable = getattr(probe.icmp, "ok", None) is True or getattr(probe.metrics, "ok", None) is True
                state = "UP" if reachable else "DOWN"
                if before != (asset.ping_status, asset.exporter_status):
                    updated += 1
                    log.warning(
                        "infrastructure %s %s (%s) ping=%s",
                        state,
                        asset.hostname,
                        asset.ip,
                        asset.ping_status,
                    )
            await session.commit()
            log.info("infrastructure scan done scanned=%s updated=%s errors=%s", scanned, updated, errors)
            return {"skipped": False, "reason": "", "scanned": scanned, "updated": updated, "errors": errors}
        except Exception:
            await session.rollback()
            log.exception("infrastructure scan failed")
            raise
        finally:
            await session.close()


async def start_infrastructure_scheduler(
    *,
    interval: float = INFRA_SCAN_INTERVAL_SECONDS,
    stop: asyncio.Event | None = None,
) -> None:
    """Cron-like loop on the FastAPI event loop. Each cycle is a fire-and-forget task."""
    log.info("infrastructure scan scheduler started interval=%s", interval)
    while stop is None or not stop.is_set():
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)
        from app.leader import leader

        # Blocking DB call: keep it off the event loop. Non-leaders do not scan.
        if not await asyncio.to_thread(leader.ensure):
            continue
        if _infrastructure_scan_lock.locked():
            log.warning("infrastructure scan still running; skip this cycle")
            continue
        asyncio.create_task(run_periodic_infrastructure_check())
