"""ForgeAI: answer language, new read-only tools, storm summary and the golden test set."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app import forgeai
from app.alarm_noise import STORM_FINGERPRINT
from app.cli_ops import format_golden
from app.db import Base, SessionLocal, engine
from app.forgeai_golden import add_case, expected_words, run, score
from app.forgeai_investigator import PROTOCOL, run_tool
from app.models import Asset, ForgeAiCall, GoldenCase, GoldenRun, Incident, Job, KnownError, utcnow
from app.seed import seed
from app.settings import settings
from test_incident_tools import _client


class Fake:
    """Fake model answering every call with the same JSON and recording the system prompts."""

    def __init__(self, answer):
        self.answer, self.systems = answer, []
        self.last_error, self.last_raw, self.last_ms = "", json.dumps(answer), 300

    def get_name(self):
        return "openai-compatible"

    def get_model(self):
        return "qwen3-4b"

    def complete_json(self, system, user, max_tokens=512):
        self.systems.append(system)
        return self.answer


@pytest.fixture
def db(monkeypatch):
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    seed(session)
    monkeypatch.setattr("app.settings.Settings.ai_enabled", True)
    yield session
    for model in (GoldenRun, GoldenCase):
        session.query(model).delete()
    session.query(KnownError).filter(KnownError.title.like("fm-%")).delete(synchronize_session=False)
    session.query(Job).filter(Job.object_id.like("INC-FM%")).delete(synchronize_session=False)
    for inc in session.query(Incident).filter(Incident.number.like("INC-FM%")).order_by(Incident.id.desc()).all():
        session.query(ForgeAiCall).filter_by(incident_id=inc.id).delete()
        session.delete(inc)
    session.query(Asset).filter(Asset.asset_id.like("fm-%")).delete(synchronize_session=False)
    session.commit()
    session.close()


def _inc(db, title="fm disk full", cause="", asset=None, **kw) -> Incident:
    row = Incident(number=f"INC-FM{uuid4().hex[:6]}", title=title, severity="HIGH", status=kw.pop("status", "RESOLVED"),
                   fingerprint=kw.pop("fingerprint", f"FmAlert:{uuid4().hex}"), started_at=utcnow() - timedelta(hours=1),
                   root_cause=cause, asset_id=asset.id if asset else None, **kw)
    db.add(row)
    db.commit()
    return row


def test_language_rule_goes_into_every_prompt(db, monkeypatch):
    assert forgeai.with_language("x - Plain English, short") == "x - Plain English, short"
    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {**(settings.yaml.get("ai") or {}), "forgeai": {"language": "sr"}}})
    assert forgeai.language() == "sr"
    system = forgeai.with_language(forgeai.BASE_RULES.format(shape="{}"))
    assert "Plain English" not in system and "Serbian, Latin script" in system
    model = Fake({"cause": "Arhivski logovi nisu obrisani.", "first_checks": ["Proveri", "Proveri", "Proveri"], "for_shift_lead": "Disk."})
    monkeypatch.setattr(forgeai, "_provider", lambda: model)
    call = forgeai.run(db, "incident", _inc(db), trial=True)
    assert call.status == "trial" and "Serbian" in model.systems[0]
    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {"forgeai": {"language": "xx"}}})
    assert forgeai.language() == "en"


def test_new_tools_are_listed_and_answer(db):
    for name in ("recent", "known_errors", "neighbours", "network", "power"):
        assert f"  {name} " in PROTOCOL
    a = Asset(asset_id="fm-db", hostname="fm-db", ip="10.88.0.1", type="Linux", extras={"site": "FM", "rack": "R9"})
    b = Asset(asset_id="fm-app", hostname="fm-app", ip="10.88.0.2", type="Linux", extras={"site": "FM", "rack": "R9"})
    db.add_all([a, b])
    db.commit()
    _inc(db, "fm earlier", cause="fm cause one", asset=a)
    _inc(db, "fm neighbour down", asset=b, status="OPEN")
    now = _inc(db, "fm now", asset=a, status="OPEN")
    db.add(KnownError(title="fm-battery", pattern="fm now", workaround="fm wait"))
    db.commit()
    assert "fm cause one" in run_tool(db, now, "recent", {})[1]
    assert run_tool(db, now, "known_errors", {})[1].startswith("fm-battery")
    src, text = run_tool(db, now, "neighbours", {})
    assert src == "neighbours" and "rack R9" in text and "fm-app" in text
    assert run_tool(db, now, "network", {})[0] == "network"
    assert run_tool(db, now, "power", {})[0] == "power"


def test_storm_summary_is_queued_and_written_to_the_timeline(db, monkeypatch):
    storm = _inc(db, "Alarm storm", fingerprint=f"{STORM_FINGERPRINT}-{uuid4().hex}", status="OPEN")
    sw = Asset(asset_id="fm-sw", hostname="fm-sw", ip="10.88.1.1", type="Switch")
    db.add(sw)
    db.commit()
    for i in range(3):
        host = Asset(asset_id=f"fm-h{i}", hostname=f"fm-h{i}", ip=f"10.88.1.{10 + i}", type="Linux", parent_id=sw.id, extras={"site": "FM", "rack": "R2"})
        db.add(host)
        db.commit()
        _inc(db, f"fm host {i} down", asset=host, caused_by_id=storm.id, status="OPEN")
    forgeai.queue_storm_summary(db, storm, opened=True)
    db.commit()
    assert db.query(Job).filter_by(object_id=f"{storm.number}#storm", kind="forgeai").count() == 1
    forgeai.queue_storm_summary(db, storm, opened=True)
    assert db.query(Job).filter_by(object_id=f"{storm.number}#storm").count() == 1
    ctx = forgeai._storm_context(db, storm)
    assert ctx["storm"]["linked_incidents"] == 3
    assert ctx["most_common"]["parent"] == [{"value": "fm-sw", "incidents": 3}]
    answer = {"summary": "Three hosts behind fm-sw went down together.", "common_cause": "fm-sw lost power or its uplink.",
              "first_checks": ["Check fm-sw", "Check the rack R2 PDU", "Check the uplink"]}
    monkeypatch.setattr(forgeai, "_provider", lambda: Fake(answer))
    call = forgeai.run(db, "storm", storm)
    assert call.status == "used"
    db.refresh(storm)
    assert any("Likely shared cause: fm-sw" in str(t.get("detail")) for t in storm.timeline or [])


def test_golden_set_scores_runs(db, monkeypatch):
    good = _inc(db, "fm disk", cause="Archive logs were not purged by the cron job")
    other = _inc(db, "fm cpu", cause="")
    with pytest.raises(ValueError):
        add_case(db, other.number, "", "eng@dc.local")
    add_case(db, good.number, "", "eng@dc.local")
    add_case(db, other.number, "batch, report", "eng@dc.local")
    with pytest.raises(ValueError):
        add_case(db, good.number, "", "eng@dc.local")
    case = db.query(GoldenCase).filter_by(incident_id=good.id).one()
    assert expected_words(case, good)[:3] == ["archive", "logs", "purged"]
    assert score("trial", {"cause": "Archive logs filled it"}, ["archive"]) == (True, "names 'archive'")
    assert score("rejected", {"cause": "archive"}, ["archive"])[0] is False
    monkeypatch.setattr(forgeai, "_provider", lambda: Fake({"cause": "Archive logs filled the disk.", "first_checks": ["a", "b", "c"], "for_shift_lead": "x"}))
    done = run(db, actor="eng@dc.local")
    assert (done.passed, done.total) == (1, 2) and done.model == "qwen3-4b"
    data = _client("viewer").get("/api/v1/forgeai/golden").json()
    assert data["runs"][0]["passed"] == 1 and len(data["cases"]) == 2 and data["can_edit"] is False
    text = format_golden(data)
    assert "1/2" in text and "PASS" in text and "FAIL" in text


def test_golden_api_roles(db):
    inc = _inc(db, "fm api", cause="Fan failed in the PSU")
    assert _client("analyst").post("/api/v1/forgeai/golden", json={"number": inc.number}).status_code == 403
    eng = _client("engineer")
    assert eng.post("/api/v1/forgeai/golden", json={"number": "INC-NOPE-404"}).status_code == 404
    case = eng.post("/api/v1/forgeai/golden", json={"number": inc.number}).json()
    started = eng.post("/api/v1/forgeai/golden/run", json={}).json()
    assert started["job"]
    db.query(Job).filter_by(id=started["job"]).delete()
    db.commit()
    assert eng.delete(f"/api/v1/forgeai/golden/{case['id']}").status_code == 200


def test_compare_two_models_runs_both_and_puts_the_first_back(db, monkeypatch, tmp_path):
    from app import llm_models
    from app.forgeai_golden import compare, overview

    monkeypatch.setenv("FORGESRE_MODELS_DIR", str(tmp_path))
    for name in ("Qwen3-8B-Q4_K_M.gguf", "OpsLLM-7B.i1-Q4_K_M.gguf"):
        (tmp_path / name).write_bytes(b"GGUF" + b"\0" * 60)
    (tmp_path / "active").write_text("Qwen3-8B-Q4_K_M.gguf\n")
    monkeypatch.setattr("app.settings.Settings.llm_url", "http://llm:8088/v1")
    inc = _inc(db, "fm raid degraded", cause="fm controller cache battery failed")
    add_case(db, inc.number, "battery", "t")
    answers = {"Qwen3-8B-Q4_K_M.gguf": {"cause": "The cache battery failed.", "first_checks": ["Check the controller log"], "for_shift_lead": "x"},
               "OpsLLM-7B.i1-Q4_K_M.gguf": {"cause": "A disk failed.", "first_checks": ["Check the disks"], "for_shift_lead": "x"}}
    monkeypatch.setattr(forgeai, "_provider", lambda: Fake(answers[llm_models.active_name(tmp_path)]))
    waited = []
    settings_set = []
    monkeypatch.setattr("app.system_settings.set_setting", lambda db, key, raw, actor: settings_set.append((key, raw)))
    cid = compare(db, "OpsLLM-7B.i1-Q4_K_M.gguf", "Qwen3-8B-Q4_K_M.gguf", actor="eng", wait=lambda name: waited.append(name) or True)
    runs = db.query(GoldenRun).filter_by(compare=cid).order_by(GoldenRun.id).all()
    assert [r.model_file for r in runs] == ["OpsLLM-7B.i1-Q4_K_M.gguf", "Qwen3-8B-Q4_K_M.gguf"]
    assert [r.passed for r in runs] == [0, 1] and runs[0].model == "OpsLLM 7B (i1-Q4_K_M)"
    assert llm_models.active_name(tmp_path) == "Qwen3-8B-Q4_K_M.gguf" and waited[-1] == "Qwen3-8B-Q4_K_M.gguf"
    assert ("ai.llm.thinking", False) in settings_set  # OpsLLM turned it off, then the old value came back last
    pair = overview(db)["comparisons"][0]
    assert pair["id"] == cid and [r["rejected"] for r in pair["runs"]] == [0, 0] and pair["runs"][0]["avg_ms"] >= 0
    text = format_golden(overview(db) | {"cases": [{"incident": inc.number, "title": "x", "expect": []}]})
    assert "Last model comparison:" in text and "OpsLLM 7B (i1-Q4_K_M)" in text and "rejected 0" in text
    # a model that does not load is recorded as such, and the first model still comes back
    cid2 = compare(db, "OpsLLM-7B.i1-Q4_K_M.gguf", "Qwen3-8B-Q4_K_M.gguf", actor="eng", wait=lambda name: name != "OpsLLM-7B.i1-Q4_K_M.gguf")
    first = db.query(GoldenRun).filter_by(compare=cid2).order_by(GoldenRun.id).first()
    assert first.results[0]["why"] == "the model did not load in time"
    assert llm_models.active_name(tmp_path) == "Qwen3-8B-Q4_K_M.gguf"


def test_compare_api(db, monkeypatch, tmp_path):
    monkeypatch.setenv("FORGESRE_MODELS_DIR", str(tmp_path))
    for name in ("a.gguf", "b.gguf"):
        (tmp_path / name).write_bytes(b"GGUF" + b"\0" * 60)
    eng = _client("engineer")
    assert _client("analyst").post("/api/v1/forgeai/golden/compare", json={"a": "a.gguf", "b": "b.gguf"}).status_code == 403
    assert eng.post("/api/v1/forgeai/golden/compare", json={"a": "a.gguf", "b": "a.gguf"}).status_code == 400
    assert eng.post("/api/v1/forgeai/golden/compare", json={"a": "a.gguf", "b": "zzz.gguf"}).status_code == 404
    assert eng.post("/api/v1/forgeai/golden/compare", json={"a": "a.gguf", "b": "b.gguf"}).status_code == 400  # empty golden set
    inc = _inc(db, "fm cmp", cause="fm psu failed")
    add_case(db, inc.number, "", "t")
    job = eng.post("/api/v1/forgeai/golden/compare", json={"a": "a.gguf", "b": "b.gguf"}).json()["job"]
    assert db.get(Job, job).kind == "forgeai_compare"
    assert _client("viewer").get("/api/v1/forgeai/golden").json()["comparing"] is True
    db.query(Job).filter_by(id=job).delete()
    db.commit()


def test_known_error_starter_pack(db):
    from app.known_errors import known_errors_for
    from app.known_errors_pack import PACK

    eng = _client("engineer")
    assert _client("analyst").post("/api/v1/known-errors/starter-pack").status_code == 403
    first = eng.post("/api/v1/known-errors/starter-pack").json()
    try:
        assert len(first["added"]) + len(first["skipped"]) == len(PACK)
        assert eng.post("/api/v1/known-errors/starter-pack").json()["added"] == []
        cases = {"RAID array degraded": "Virtual disk 0 degraded on fm-esx", "Power supply failed or lost input": "fm-sw01: PSU 2 failed",
                 "UPS on battery": "UPSOnBattery fm-ups-a", "Network link down or flapping": "Gi1/0/14 flapping on fm-sw",
                 "Datastore or volume almost full": "Datastore fm-ds01 is 95% full", "Clock drift (NTP)": "NTP offset too high on fm-dc",
                 "Certificate expiring or expired": "SSL certificate expires in 5 days", "Temperature too high": "Inlet temperature high fm-r12"}
        for title, alarm in cases.items():
            hits = [k["title"] for k in known_errors_for(db, _inc(db, alarm, status="OPEN"))]
            assert title in hits, (title, alarm, hits)
        assert not known_errors_for(db, _inc(db, "fm cpu load high", status="OPEN"))
    finally:
        db.query(KnownError).filter(KnownError.title.in_([p["title"] for p in PACK])).delete(synchronize_session=False)
        db.commit()
