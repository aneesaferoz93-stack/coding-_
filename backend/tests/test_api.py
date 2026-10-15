"""End-to-end tests: API → rules → ledger → tamper detection."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import Settings
from app.db.bootstrap import create_schema, install_append_only, seed_reference_data
from app.db.session import build_engine, build_sessionmaker
from app.main import create_app


@pytest.fixture
async def env(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        ledger_signing_key="test-key", env="dev", phase="PHASE_1_DUAL_RUN",
        pilot_districts=["PESHAWAR", "MARDAN", "SWAT"])
    engine = build_engine(settings)
    await create_schema(engine)
    Session = build_sessionmaker(engine)
    async with Session() as s:
        await install_append_only(s)
        await seed_reference_data(s, settings)

    app = create_app()
    # Point the app at our test database rather than whatever the environment says.
    from app.db import session as db_session
    db_session._engine = engine
    db_session._sessionmaker = Session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, Session, settings, str(tmp_path / "test.db")
    await engine.dispose()


# --------------------------------------------------------------------------
# Clearance pipeline
# --------------------------------------------------------------------------

async def test_clearance_flags_a_dead_seller(env):
    client, _, _, _ = env
    r = await client.post("/v1/integrations/efard/clearance",
                          json={"mutation_id": "MUT-KP-1004"})
    assert r.status_code == 200
    d = r.json()
    assert d["decision"] == "HOLD"        # Phase 1 holds; it does not refuse
    assert d["binding"] is False
    assert any("deceased" in f["title"].lower() for f in d["findings"])
    assert d["ledger_seq"] and d["ledger_hash"]


async def test_clearance_flags_benami_undervaluation(env):
    client, _, _, _ = env
    d = (await client.post("/v1/integrations/efard/clearance",
                           json={"mutation_id": "MUT-KP-1003"})).json()
    assert any(f["rule_id"] == "R2" and f["severity"] == "RED" for f in d["findings"])


async def test_clearance_flags_rapid_remutation(env):
    client, _, _, _ = env
    d = (await client.post("/v1/integrations/efard/clearance",
                           json={"mutation_id": "MUT-KP-1002"})).json()
    assert any(f["rule_id"] == "R1" and f["severity"] == "RED" for f in d["findings"])


async def test_clearance_flags_unverified_poa(env):
    client, _, _, _ = env
    d = (await client.post("/v1/integrations/efard/clearance",
                           json={"mutation_id": "MUT-KP-1005"})).json()
    assert any(f["rule_id"] == "R4" and f["severity"] == "RED" for f in d["findings"])


async def test_non_pilot_district_passes_straight_through(env):
    """Nobody outside the pilot is slowed down. That is the point of phasing."""
    client, Session, settings, _ = env
    async with Session() as s:
        from app.api.routers.integrations import get_efard_port
        from app.integrations.efard import EfardMutation
        port = get_efard_port(settings)
        assert hasattr(port, "add_mutation")
        port.add_mutation(EfardMutation(
            mutation_id="MUT-KP-2001", district_code="BANNU", tehsil_code="BANNU",
            mouza_code="MZ-BNN-001", mouza_name="Bannu City", khasra_no="1",
            area_kanal=Decimal("4"), declared_price_pkr=Decimal("1000"),
            sellers=[{"name": "X", "cnic": "1730111111111", "nadra_status": "DECEASED"}]))
        await s.commit()
    d = (await client.post("/v1/integrations/efard/clearance",
                           json={"mutation_id": "MUT-KP-2001"})).json()
    assert d["decision"] == "CLEAR"
    assert d["phase"] == "NOT_PILOT"
    assert d["findings"] == []


async def test_ground_verification_turns_a_hold_into_a_clear(env):
    """The point of the whole field-app: evidence resolves the flag."""
    client, Session, settings, _ = env

    # 1. Before the visit: flagged for having no site evidence.
    before = (await client.post("/v1/integrations/efard/clearance",
                                json={"mutation_id": "MUT-KP-1001"})).json()
    assert any(f["rule_id"] == "R6" for f in before["findings"])

    # 2. Enrol the handset.
    enrol = (await client.post("/v1/ground/devices", json={
        "device_id": "DEV-TEST-1", "officer_id": "PAT-PSH-07",
        "platform": "android", "app_version": "1.0.0"})).json()
    secret = enrol["attestation_secret"]

    # 3. A genuine visit, inside the mouza, camera photo, two witnesses.
    from app.services.ground import attestation_body

    captured = datetime.now(timezone.utc) - timedelta(minutes=25)
    nonce = "abc123"
    body = attestation_body(nonce=nonce, latitude=34.045, longitude=71.625,
                            captured_at=captured, photo_sha256="a" * 64)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    resp = await client.post("/v1/ground/verify", json={
        "mutation_id": "MUT-KP-1001", "district_code": "PESHAWAR",
        "mouza_code": "MZ-PSH-001", "khasra_no": "441",
        "officer_id": "PAT-PSH-07", "device_id": "DEV-TEST-1",
        "app_version": "1.0.0",
        "latitude": 34.045, "longitude": 71.625,
        "accuracy_m": 6.0, "provider": "gps", "is_mock": False, "satellites": 9,
        "captured_at": captured.isoformat(),
        "photo_sha256": "a" * 64, "photo_source": "camera",
        "photo_exif_datetime": captured.isoformat(), "photo_bytes": 1_200_000,
        "witness_cnics": ["1730111111111", "1730122222222"],
        "nonce": nonce, "hmac": sig})
    assert resp.status_code == 200, resp.text
    gv = resp.json()
    assert gv["trust_band"] in ("HIGH", "MEDIUM"), gv
    assert gv["status"] == "ACCEPTED"
    assert gv["inside_mouza"] is True

    # 4. Re-evaluate: the R6 flag is gone.
    after = (await client.post("/v1/mutations/MUT-KP-1001/evaluate")).json()
    assert not any(f["rule_id"] == "R6" and f["severity"] == "RED"
                   for f in after["findings"]), after["findings"]


async def test_spoofed_visit_is_rejected(env):
    client, _, settings, _ = env
    await client.post("/v1/ground/devices", json={
        "device_id": "DEV-TEST-2", "officer_id": "PAT-PSH-07"})
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1007"})
    now = datetime.now(timezone.utc)
    r = await client.post("/v1/ground/verify", json={
        "mutation_id": "MUT-KP-1007", "district_code": "SWAT",
        "mouza_code": "MZ-SWT-002", "khasra_no": "1401",
        "officer_id": "PAT-PSH-07", "device_id": "DEV-TEST-2",
        "latitude": 34.045, "longitude": 71.625,
        "accuracy_m": 5.0, "provider": "gps", "is_mock": True,   # spoofed
        "captured_at": now.isoformat(), "photo_sha256": "c" * 64,
        "photo_source": "camera", "photo_exif_datetime": now.isoformat(),
        "witness_cnics": ["1730111111111", "1730122222222"]})
    assert r.status_code == 200
    assert r.json()["status"] == "REJECTED"
    assert r.json()["trust_band"] == "REJECTED"


async def test_visit_can_be_recorded_without_a_mutation_id(env):
    """A Patwari can inspect a parcel before the mutation is drafted — or
    offline, before the assignment list has synced."""
    client, _, _, _ = env
    now = datetime.now(timezone.utc)
    r = await client.post("/v1/ground/verify", json={
        "district_code": "SWAT", "mouza_code": "MZ-SWT-002", "khasra_no": "1401",
        "device_id": "DEV-TEST-2", "latitude": 35.05, "longitude": 72.62,
        "accuracy_m": 8.0, "provider": "gps", "satellites": 8,
        "captured_at": now.isoformat(), "photo_sha256": "f" * 64,
        "photo_source": "camera", "photo_exif_datetime": now.isoformat(),
        "witness_cnics": ["1730111111111", "1730122222222"]})
    assert r.status_code == 200, r.text


async def test_gallery_photo_is_rejected(env):
    client, _, _, _ = env
    now = datetime.now(timezone.utc)
    r = await client.post("/v1/ground/verify", json={
        "district_code": "SWAT", "mouza_code": "MZ-SWT-002", "khasra_no": "1401",
        "device_id": "DEV-TEST-2", "latitude": 35.05, "longitude": 72.62,
        "accuracy_m": 5.0, "provider": "gps", "captured_at": now.isoformat(),
        "photo_sha256": "d" * 64, "photo_source": "gallery",
        "witness_cnics": ["1730111111111", "1730122222222"]})
    assert r.json()["status"] == "REJECTED"


async def test_offline_sync_is_accepted_but_scored(env):
    """Two days offline in a remote valley is terrain, not fraud."""
    client, _, _, _ = env
    captured = datetime.now(timezone.utc) - timedelta(hours=40)
    r = await client.post("/v1/ground/verify", json={
        "district_code": "SWAT", "mouza_code": "MZ-SWT-002", "khasra_no": "1401",
        "device_id": "DEV-TEST-2", "latitude": 35.05, "longitude": 72.62,
        "accuracy_m": 8.0, "provider": "gps", "satellites": 7,
        "captured_at": captured.isoformat(),
        "photo_sha256": "e" * 64, "photo_source": "camera",
        "photo_exif_datetime": captured.isoformat(),
        "witness_cnics": ["1730111111111", "1730122222222"]})
    body = r.json()
    assert body["offline_hours"] > 39
    assert body["trust_score"] < 100      # the delay costs trust, as it should


# --------------------------------------------------------------------------
# Ledger / tamper detection
# --------------------------------------------------------------------------

async def test_ledger_verifies_clean(env):
    client, _, _, _ = env
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1001"})
    v = (await client.get("/v1/ledger/verify")).json()
    assert v["intact"] is True
    assert v["entries_checked"] >= 2
    assert v["findings"] == []


async def test_database_refuses_to_rewrite_history(env):
    client, Session, _, _ = env
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1001"})
    async with Session() as s:
        with pytest.raises(Exception) as exc:
            await s.execute(text("UPDATE audit_log SET entry_hash = 'x' WHERE seq = 1"))
            await s.commit()
        assert "append-only" in str(exc.value).lower()
        await s.rollback()


async def test_database_refuses_to_delete_history(env):
    client, Session, _, _ = env
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1001"})
    async with Session() as s:
        with pytest.raises(Exception) as exc:
            await s.execute(text("DELETE FROM audit_log WHERE seq = 1"))
            await s.commit()
        assert "append-only" in str(exc.value).lower()
        await s.rollback()


async def test_tampering_is_detected_once_the_guard_is_removed(env):
    """The realistic attack: an insider with DBA rights disables the trigger,
    edits a payload, and re-enables it. Verification still catches it."""
    client, _, _, path = env
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1002"})

    con = sqlite3.connect(path)
    con.execute("DROP TRIGGER trg_audit_log_no_update")
    con.execute("UPDATE audit_log SET payload = "
                "json_set(payload, '$.declared_price_pkr', '999999999') "
                "WHERE event_type = 'MUTATION_DRAFTED'")
    con.commit()
    con.close()

    v = (await client.get("/v1/ledger/verify")).json()
    assert v["intact"] is False
    kinds = {f["kind"] for f in v["findings"]}
    assert "HASH_MISMATCH" in kinds


async def test_deleted_row_is_detected_once_the_guard_is_removed(env):
    client, _, _, path = env
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1001"})
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1003"})

    con = sqlite3.connect(path)
    con.execute("DROP TRIGGER trg_audit_log_no_delete")
    con.execute("DELETE FROM audit_log WHERE seq = 2")
    con.commit()
    con.close()

    v = (await client.get("/v1/ledger/verify")).json()
    assert v["intact"] is False
    assert "GAP" in {f["kind"] for f in v["findings"]}


async def test_anchor_is_signed_and_detectable_after_editing(env):
    client, Session, settings, path = env
    for m in ("MUT-KP-1001", "MUT-KP-1003"):
        await client.post("/v1/integrations/efard/clearance", json={"mutation_id": m})

    a = (await client.post("/v1/ledger/anchors",
                           json={"published_ref": "BoR Gazette 2026/114"})).json()
    assert a["merkle_root"] and len(a["merkle_root"]) == 64
    assert a["signature"]

    # Editing an entry inside the anchored range breaks the published root.
    con = sqlite3.connect(path)
    con.execute("DROP TRIGGER trg_audit_log_no_update")
    con.execute("UPDATE audit_log SET payload = json_set(payload, '$.area_kanal', '999') "
                "WHERE seq = 1")
    con.commit()
    con.close()

    v = (await client.get("/v1/ledger/verify")).json()
    kinds = {f["kind"] for f in v["findings"]}
    assert "ANCHOR_MISMATCH" in kinds


async def test_merkle_inclusion_proof(env):
    client, _, _, _ = env
    for m in ("MUT-KP-1001", "MUT-KP-1003"):
        await client.post("/v1/integrations/efard/clearance", json={"mutation_id": m})
    await client.post("/v1/ledger/anchors", json={"published_ref": "test"})
    p = (await client.get("/v1/ledger/entry/1/proof")).json()
    assert p["anchored"] is True
    assert p["proof_verified"] is True


async def test_history_endpoint_reports_per_record_integrity(env):
    client, _, _, _ = env
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1001"})
    h = (await client.get("/v1/ledger/history/mutation/MUT-KP-1001")).json()
    assert h["intact"] is True
    assert len(h["entries"]) >= 1


# --------------------------------------------------------------------------
# Override & review (the dual-run escape hatch)
# --------------------------------------------------------------------------

async def test_override_is_recorded_and_requires_a_reason(env):
    client, _, _, _ = env
    d = (await client.post("/v1/integrations/efard/clearance",
                           json={"mutation_id": "MUT-KP-1004"})).json()
    cid = d["clearance_id"]

    r = await client.post(f"/v1/fraud/clearances/{cid}/override?reason=too-short")
    assert r.status_code == 422          # a bare reason is not acceptable

    r = await client.post(
        f"/v1/fraud/clearances/{cid}/override",
        params={"reason": "Succession certificate verified in person by Tehsildar."})
    assert r.status_code == 200
    assert r.json()["ledger_seq"]

    history = (await client.get(
        f"/v1/ledger/history/clearance_override/{cid}")).json()
    assert history["entries"]


async def test_dismissing_a_flag_requires_a_written_note(env):
    client, _, _, _ = env
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1004"})
    flags = (await client.get("/v1/fraud/flags?severity=RED")).json()
    assert flags
    r = await client.patch(f"/v1/fraud/flags/{flags[0]['id']}",
                           json={"status": "DISMISSED", "note": "short"})
    assert r.status_code == 422
    r = await client.patch(
        f"/v1/fraud/flags/{flags[0]['id']}",
        json={"status": "DISMISSED",
              "note": "NADRA death record was a duplicate; verified alive in person."})
    assert r.status_code == 200
    assert r.json()["reviewed_by"]


# --------------------------------------------------------------------------
# Ownership / double sale
# --------------------------------------------------------------------------

async def test_share_register_is_seeded_and_queryable(env):
    client, _, _, _ = env
    s = (await client.get(
        "/v1/mutations/khasra/PESHAWAR/MZ-PSH-001/512/shares")).json()
    assert s["oversubscribed"] is False
    assert len(s["owners"]) == 2    # deliberately split holding


async def test_conveying_more_than_held_is_blocking(env):
    """Khasra 512: the seller holds half, the mutation conveys the whole."""
    client, _, _, _ = env
    d = (await client.post("/v1/integrations/efard/clearance",
                           json={"mutation_id": "MUT-KP-1002"})).json()
    r7 = [f for f in d["findings"] if f["rule_id"] == "R7"]
    assert r7 and r7[0]["severity"] == "RED"
    assert "more than they hold" in r7[0]["title"]


# --------------------------------------------------------------------------
# USSD
# --------------------------------------------------------------------------

async def test_ussd_replays_the_whole_path(env):
    client, _, _, _ = env
    root = (await client.get("/v1/ussd/preview?path=")).json()
    assert "1" in root["message"]
    step = (await client.get("/v1/ussd/preview?path=1")).json()
    assert step["state"] == "STATUS_KHASRA"
    result = (await client.get("/v1/ussd/preview?path=1*512*MZ-PSH-001")).json()
    assert result["continue_session"] is False
    assert "KHASRA" not in result["message"]


async def test_ussd_defaults_to_pashto(env):
    client, _, _, _ = env
    r = (await client.get("/v1/ussd/preview?path=")).json()
    assert r["language"] == "ps"


async def test_ussd_fraud_report_creates_a_flag(env):
    client, _, _, _ = env
    r = (await client.post("/v1/ussd/gateway", json={
        "sessionId": "ussd-test-1", "msisdn": "03001234567",
        "text": "2*77*MZ-MRD-003*1710133344455"})).json()
    assert r["continueSession"] is False
    assert "RPT-" in r["message"]
    flags = (await client.get("/v1/fraud/flags?rule_id=CITIZEN")).json()
    assert flags


# --------------------------------------------------------------------------
# Health & config
# --------------------------------------------------------------------------

async def test_ready_reports_append_only_is_active(env):
    client, _, _, _ = env
    r = (await client.get("/v1/ready")).json()
    assert r["status"] == "ok"
    assert r["append_only"]["append_only"] is True


async def test_config_exposes_the_active_phase_policy(env):
    client, _, _, _ = env
    c = (await client.get("/v1/config")).json()
    assert c["phase"] == "PHASE_1_DUAL_RUN"
    assert c["phase_policy"]["binding"] is False
    assert c["rules"] == ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"]
    assert c["ussd_shortcode"] == "*444#"


async def test_dashboard_rollup(env):
    client, _, _, _ = env
    await client.post("/v1/integrations/efard/clearance",
                      json={"mutation_id": "MUT-KP-1004"})
    d = (await client.get("/v1/dashboard")).json()
    assert d["phase"] == "PHASE_1_DUAL_RUN"
    assert d["chain"]["intact"] is True
    assert d["flags_by_severity"].get("RED", 0) >= 1


async def test_efard_mirror_writes_to_the_chain(env):
    client, _, _, _ = env
    r = await client.post("/v1/integrations/efard/mirror", json={
        "fard_id": "FARD-1", "district_code": "PESHAWAR",
        "mouza_code": "MZ-PSH-001", "khasra_no": "441", "channel": "DASTAK"})
    assert r.status_code == 200
    assert r.json()["ledger_seq"]
    h = (await client.get("/v1/ledger/history/fard/FARD-1")).json()
    assert h["intact"] and h["entries"]
