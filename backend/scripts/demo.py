"""
demo.py — set up the pilot corpus and walk the whole system end to end.

Run:  python scripts/demo.py            (from the backend directory)

This is the script to run in front of a Board of Revenue review. It does not
print "hello world"; it performs the five things this system claims to do and
shows the evidence for each:

  1. Flags a fraudulent mutation before it is attested.
  2. Clears a legitimate one once the Patwari has actually stood on the land.
  3. Rejects a spoofed site visit.
  4. Detects tampering with the record, after the fact.
  5. Serves the same result to a farmer on a feature phone over USSD.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.bootstrap import create_schema, install_append_only, seed_reference_data  # noqa: E402
from app.db.session import build_engine, build_sessionmaker  # noqa: E402
from app.main import create_app  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[1] / "klri_demo.db"

H = {"X-API-Key": "dev-efard-key"}
W = 78


def rule(title: str = "", ch: str = "─") -> None:
    if title:
        print(f"\n\033[1m{title}\033[0m")
        print(ch * W)
    else:
        print(ch * W)


def say(text: str, indent: int = 2) -> None:
    print(" " * indent + text)


def badge(sev: str) -> str:
    return {"RED": "\033[41;97m RED \033[0m", "AMBER": "\033[43;30m AMBER \033[0m",
            "INFO": "\033[46;30m INFO \033[0m"}.get(sev, sev)


def decision_badge(d: str) -> str:
    colours = {"BLOCK": "\033[41;97m", "HOLD": "\033[43;30m",
               "CLEAR": "\033[42;30m", "ADVISORY": "\033[44;97m", "DEGRADED": "\033[45;97m"}
    return f"{colours.get(d, '')} {d} \033[0m"


async def main() -> int:
    DB_PATH.unlink(missing_ok=True)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{DB_PATH}",
        ledger_signing_key="demo-signing-key", env="dev",
        phase="PHASE_1_DUAL_RUN", pilot_districts=["PESHAWAR", "MARDAN", "SWAT"])
    engine = build_engine(settings)
    await create_schema(engine)
    Session = build_sessionmaker(engine)
    async with Session() as s:
        await install_append_only(s)
        seeded = await seed_reference_data(s, settings)
    say(f"pilot reference data seeded: {seeded}")

    from app.db import session as db_session
    db_session._engine = engine
    db_session._sessionmaker = Session

    app = create_app()
    # The app resolves its Settings through a cached dependency. Override it so
    # the endpoints use the *same* signing key and phase as the seeded data —
    # otherwise CNIC hashes computed at seed time will not match the ones
    # computed during clearance, and every seller looks like they hold nothing.
    from app.config import get_settings as _get_settings
    app.dependency_overrides[_get_settings] = lambda: settings
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://demo") as c:
        await scene_1_rules(c)
        await scene_2_ground(c, settings)
        await scene_3_spoof(c)
        await scene_4_ussd(c)
        await scene_5_tamper(c, settings)
        await scene_6_anchor(c)

    rule("DONE")
    say(f"demo database: {DB_PATH}")
    say("open /docs for the full API, or run the Patwari PWA against this service.")
    await engine.dispose()
    return 0


async def scene_1_rules(c: AsyncClient) -> None:
    rule("SCENE 1 — e-Fard asks us before it issues. What do we say?")
    say("Integration: POST /v1/integrations/efard/clearance")
    say("Phase: PHASE_1_DUAL_RUN (advisory — the manual register still runs)\n")

    mutations = (await c.get("/v1/integrations/efard/demo/mutations")).json()
    for m in mutations:
        r = (await c.post("/v1/integrations/efard/clearance",
                          json={"mutation_id": m["mutation_id"]}, headers=H)).json()
        print(f"\n  {m['mutation_id']}  {m['district']:<9} {m['mouza']:<11} "
              f"khasra {m['khasra_no']:<5} {decision_badge(r['decision'])}  score {r['score']}")
        say(f"expected: {m['expected']}", 4)
        for f in r["findings"]:
            print(f"      {badge(f['severity'])} {f['rule_id']}  {f['title']}")
            if f["severity"] == "RED":
                print(f"              → {f['recommended_action']}")
        say(f"ledger seq {r['ledger_seq']}  hash {r['ledger_hash'][:16]}…")


async def scene_2_ground(c: AsyncClient, settings: Settings) -> None:
    rule("SCENE 2 — the Patwari actually goes to the land")
    say("A legitimate sale is flagged only because nobody has verified it on site.")
    say("Enrol the handset, capture GPS + camera + two neighbours, re-evaluate.\n")

    enrol = (await c.post("/v1/ground/devices", json={
        "device_id": "DEMO-HANDSET-01", "officer_id": "PAT-PSH-07",
        "platform": "android", "app_version": "1.0.0"})).json()
    say(f"handset enrolled to {enrol['officer_id']} ({enrol['device_id']})")

    import hashlib
    import hmac as _h

    captured = datetime.now(timezone.utc) - timedelta(minutes=25)
    nonce = "demo-nonce-0001"
    lat, lon = 34.045, 71.625
    sha = hashlib.sha256(b"demo-photo-bytes").hexdigest()
    body = f"{nonce}:{lat:.7f}:{lon:.7f}:{captured.strftime('%Y-%m-%dT%H:%M:%S')}:{sha}"
    sig = _h.new(enrol["attestation_secret"].encode(), body.encode(), hashlib.sha256).hexdigest()

    r = await c.post("/v1/ground/verify", json={
        "mutation_id": "MUT-KP-1001", "district_code": "PESHAWAR",
        "mouza_code": "MZ-PSH-001", "khasra_no": "441",
        "officer_id": "PAT-PSH-07", "device_id": "DEMO-HANDSET-01",
        "app_version": "1.0.0", "latitude": lat, "longitude": lon,
        "accuracy_m": 6.0, "provider": "gps", "is_mock": False, "satellites": 9,
        "captured_at": captured.isoformat(), "photo_sha256": sha,
        "photo_source": "camera", "photo_exif_datetime": captured.isoformat(),
        "photo_bytes": 1_400_000,
        "witness_cnics": ["1730111222333", "1730144455667"],
        "nonce": nonce, "hmac": sig})
    gv = r.json()
    say(f"site visit {gv['id']}")
    say(f"trust score {gv['trust_score']}/100  ({gv['trust_band']})  status {gv['status']}")
    say(f"inside mouza: {gv['inside_mouza']}   signed {gv['offline_hours']}h before sync")
    failed = [s["code"] for s in gv["trust_signals"] if not s["passed"]]
    say(f"failed signals: {failed or 'none'}")

    after = (await c.post("/v1/mutations/MUT-KP-1001/evaluate")).json()
    say("")
    print(f"  re-evaluated MUT-KP-1001 → {decision_badge(after['decision'])} "
          f"score {after['score']}")
    for f in after["findings"]:
        print(f"      {badge(f['severity'])} {f['rule_id']}  {f['title']}")
    if after["decision"] == "CLEAR":
        say("The flag is resolved by evidence, not by an override. This is the")
        say("whole point of the field app: it gives an honest Patwari a way to")
        say("clear their own name in minutes instead of days.")


async def scene_3_spoof(c: AsyncClient) -> None:
    rule("SCENE 3 — the same app, used dishonestly")
    say("Three ways a site visit gets faked. Each is caught by a different signal.\n")

    cases = [
        ("Mock GPS provider",
         {"is_mock": True, "provider": "gps", "accuracy_m": 5.0, "photo_source": "camera"}),
        ("Photo uploaded from the gallery",
         {"is_mock": False, "provider": "gps", "accuracy_m": 5.0, "photo_source": "gallery"}),
        ("Teleport: 150 km from the last fix in 2 minutes",
         {"is_mock": False, "provider": "gps", "accuracy_m": 5.0, "photo_source": "camera",
          "teleport": True}),
        ("Fix outside the mouza boundary",
         {"is_mock": False, "provider": "gps", "accuracy_m": 5.0, "photo_source": "camera",
          "outside": True}),
        ("Reused photograph (same SHA-256 as an earlier visit)",
         {"is_mock": False, "provider": "gps", "accuracy_m": 5.0, "photo_source": "camera",
          "reuse": True}),
        ("Unregistered handset, no attestation",
         {"is_mock": False, "provider": "gps", "accuracy_m": 5.0, "photo_source": "camera",
          "rogue_device": True}),
    ]

    for label, cfg in cases:
        now = datetime.now(timezone.utc)
        payload = {
            "district_code": "SWAT", "mouza_code": "MZ-SWT-002", "khasra_no": "1401",
            "device_id": "ROGUE-DEVICE" if cfg.get("rogue_device") else "DEMO-HANDSET-01",
            "officer_id": "PAT-SWT-04",
            "latitude": 31.55 if cfg.get("outside") else 35.05,
            "longitude": 74.34 if cfg.get("outside") else 72.62,
            "accuracy_m": cfg["accuracy_m"], "provider": cfg["provider"],
            "is_mock": cfg["is_mock"], "satellites": 8,
            "captured_at": (now - timedelta(minutes=2)).isoformat()
            if cfg.get("teleport") else now.isoformat(),
            "photo_sha256": hashlib_reuse() if cfg.get("reuse")
            else f"{abs(hash(label)):064x}",
            "photo_source": cfg["photo_source"],
            "photo_exif_datetime": now.isoformat(),
            "witness_cnics": ["1730111222333", "1730144455667"],
        }
        if cfg.get("teleport"):
            # The previous fix for DEMO-HANDSET-01 is in Peshawar (scene 2).
            payload["latitude"], payload["longitude"] = 31.55, 74.34
        res = (await c.post("/v1/ground/verify", json=payload)).json()
        failed = [s["code"] for s in res["trust_signals"] if not s["passed"]]
        verdict = "REJECTED" if res["status"] == "REJECTED" else res["status"]
        colour = "\033[42;30m ACCEPTED \033[0m" if verdict == "ACCEPTED" \
            else "\033[41;97m REJECTED \033[0m"
        print(f"  {colour}  score {res['trust_score']:>3}   {label}")
        if failed:
            print(f"                failed: {', '.join(failed)}")

    say("")
    say("Note what is NOT here: no single check is treated as proof. Each one")
    say("costs points, and the visit is rejected on the combined score — because")
    say("in rural KP a weak fix or a two-day offline window is usually terrain,")
    say("not fraud, and a rule that punishes terrain gets switched off.")


def hashlib_reuse() -> str:
    import hashlib
    return hashlib.sha256(b"demo-photo-bytes").hexdigest()


async def scene_4_ussd(c: AsyncClient) -> None:
    rule("SCENE 4 — the same answer, on a Rs 2,000 phone with no data")
    say("USSD *444# — no app, no login, no internet. Pashto by default.\n")

    steps = ["", "1", "1*512", "1*512*MZ-PSH-001"]
    for s in steps:
        r = (await c.get("/v1/ussd/preview", params={"path": s})).json()
        print(f"  dial *444#{s}  →")
        for line in r["message"].split("\n"):
            print(f"      {line}")
        print()

    say("Fraud report from a citizen (no smartphone, no CNIC card to hand):")
    r = (await c.get("/v1/ussd/preview", params={
        "path": "2*77*MZ-MRD-003*1710133344455", "msisdn": "03009998887"})).json()
    for line in r["message"].split("\n"):
        print(f"      {line}")


async def scene_5_tamper(c: AsyncClient, settings: Settings) -> None:
    rule("SCENE 5 — six months later, someone edits the record")
    say("The realistic attack is not a hacker. It is an insider with database")
    say("access who drops the append-only trigger, changes one number, and puts")
    say("the trigger back. Here is what happens.\n")

    before = (await c.get("/v1/ledger/verify")).json()
    say(f"chain before: {before['entries_checked']} entries, intact={before['intact']}")
    say(f"head hash: {before['head_hash'][:32]}…")

    con = sqlite3.connect(DB_PATH)
    con.execute("DROP TRIGGER trg_audit_log_no_update")
    con.execute("UPDATE audit_log SET payload = "
                "json_set(payload, '$.declared_price_pkr', '999999999') "
                "WHERE event_type = 'MUTATION_DRAFTED' "
                "AND json_extract(payload, '$.khasra_no') = '1902'")
    con.commit()
    con.close()

    after = (await c.get("/v1/ledger/verify")).json()
    tampered = "\033[41;97m TAMPERED \033[0m" if not after["intact"] else ""
    print(f"\n  chain after:  intact={after['intact']} {tampered}")
    for f in after["findings"]:
        print(f"      [{f['kind']}] seq {f['seq']}: {f['detail']}")

    report = (await c.get("/v1/dashboard/tamper-report")).json()
    say("")
    say("The report a court or an audit team gets:")
    for line in report["report"].split("\n"):
        print(f"      {line}")


async def scene_6_anchor(c: AsyncClient) -> None:
    rule("SCENE 6 — publishing the checkpoint")
    say("Signing an anchor is easy. Publishing it is what makes it worth")
    say("anything: an attacker who rewrites the database still cannot rewrite")
    say("every published copy of the Merkle root.\n")

    a = (await c.post("/v1/ledger/anchors",
                      json={"published_ref": "BoR Gazette notification KP/Rev/2026/114"})).json()
    say(f"range      : seq {a['from_seq']}–{a['to_seq']} ({a['entry_count']} entries)")
    say(f"merkle root: {a['merkle_root']}")
    say(f"signed by  : {a['signed_by']}")
    say(f"published  : {a['published_ref']}")

    p = (await c.get("/v1/ledger/entry/1/proof")).json()
    say("")
    say(f"Merkle inclusion proof for seq 1: anchored={p['anchored']}, "
        f"verified={p.get('proof_verified')}")
    say("A citizen can hold this proof in their pocket and re-check it against")
    say("the published root in ten years without trusting us or the database.")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
