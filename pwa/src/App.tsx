import { useCallback, useEffect, useMemo, useState } from 'react';
import { LANGS, Lang, makeT, setLang, detectLang } from './i18n';
import { api, Assignment, MouzaGeo, Enrolment, loadEnrolment, saveEnrolment, ClearanceResult } from './api';
import { captureFix, Fix, accuracyBand, pointInRing, parsePolygonWkt } from './geo';
import { CapturedPhoto } from './photo';
import { attestationBody, hmacHex, randomId, randomNonce } from './crypto';
import * as queue from './queue';
import CameraCapture from './components/CameraCapture';
import MapView from './components/MapView';

type Tab = 'visit' | 'queue' | 'map';

export default function App() {
  const [lang, setLangState] = useState<Lang>(detectLang());
  const T = useMemo(() => makeT(lang), [lang]);
  const [tab, setTab] = useState<Tab>('visit');
  const [online, setOnline] = useState(navigator.onLine);
  const [enrolment, setEnrolment] = useState<Enrolment | null>(loadEnrolment());

  useEffect(() => {
    setLang(lang);
  }, [lang]);

  useEffect(() => {
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener('online', on);
    window.addEventListener('offline', off);
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off); };
  }, []);

  if (!enrolment) {
    return (
      <Shell lang={lang} setLangState={setLangState} online={online} T={T} tab={tab} setTab={setTab}>
        <EnrolCard T={T} onEnrolled={setEnrolment} />
      </Shell>
    );
  }

  return (
    <Shell lang={lang} setLangState={setLangState} online={online} T={T} tab={tab} setTab={setTab}>
      {tab === 'visit' && <VisitTab T={T} enrolment={enrolment} online={online} />}
      {tab === 'queue' && <QueueTab T={T} enrolment={enrolment} online={online} />}
      {tab === 'map' && <MapTab T={T} />}
    </Shell>
  );
}

// --------------------------------------------------------------------------
// Shell
// --------------------------------------------------------------------------

function Shell({ children, lang, setLangState, online, T, tab, setTab }: {
  children: React.ReactNode;
  lang: Lang; setLangState: (l: Lang) => void; online: boolean;
  T: (k: string) => string; tab: Tab; setTab: (t: Tab) => void;
}) {
  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-row">
          <div>
            <h1>{T('app_title')}</h1>
            <p>{T('app_subtitle')}</p>
          </div>
          <span className={`pill ${online ? '' : 'offline'}`}>
            {online ? '●' : '○'} {online ? T('online') : T('offline')}
          </span>
        </div>
        <div className="langbar">
          {LANGS.map((l) => (
            <button key={l.code} aria-pressed={lang === l.code} onClick={() => setLangState(l.code)}>
              {l.label}
            </button>
          ))}
        </div>
      </header>
      <nav className="tabs">
        <button aria-selected={tab === 'visit'} onClick={() => setTab('visit')}>{T('tab_visit')}</button>
        <button aria-selected={tab === 'queue'} onClick={() => setTab('queue')}>{T('tab_queue')}</button>
        <button aria-selected={tab === 'map'} onClick={() => setTab('map')}>{T('tab_map')}</button>
      </nav>
      <main>{children}</main>
    </div>
  );
}

// --------------------------------------------------------------------------
// Enrolment
// --------------------------------------------------------------------------

function EnrolCard({ T, onEnrolled }: { T: (k: string) => string; onEnrolled: (e: Enrolment) => void }) {
  const [officerId, setOfficerId] = useState('');
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const deviceId = useMemo(() => {
    let id = localStorage.getItem('klri.hwid');
    if (!id) { id = 'PWA-' + randomId().slice(0, 12).toUpperCase(); localStorage.setItem('klri.hwid', id); }
    return id;
  }, []);

  async function enrol() {
    setBusy(true); setError(null);
    try {
      const res = await api.enrol({ device_id: deviceId, officer_id: officerId, label, platform: 'pwa' });
      const e: Enrolment = { deviceId, officerId, secret: res.attestation_secret, token: res.device_token };
      saveEnrolment(e);
      setSecret(res.attestation_secret);
      onEnrolled(e);
    } catch (err) {
      setError(String(err));
    } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <h2>{T('enrol_title')}</h2>
      <p className="hint">{T('enrol_help')}</p>
      <div className="kv"><span className="k">Device ID</span><span className="v ltr mono">{deviceId}</span></div>
      <label>{T('officer_id')}</label>
      <input type="text" value={officerId} onChange={(e) => setOfficerId(e.target.value)} placeholder="PAT-PSH-07" />
      <label>{T('device_label')}</label>
      <input type="text" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Field handset" />
      {error && <div className="banner bad">{error}</div>}
      <div style={{ marginTop: 14 }}>
        <button className="btn" disabled={busy || !officerId} onClick={enrol}>{T('enrol_button')}</button>
      </div>
      {secret && <div className="banner warn">
        {T('enrol_help')}
        <div className="ltr mono" style={{ marginTop: 8 }}>{secret}</div>
      </div>}
    </div>
  );
}

// --------------------------------------------------------------------------
// Site visit
// --------------------------------------------------------------------------

function VisitTab({ T, enrolment, online }: { T: (k: string) => string; enrolment: Enrolment; online: boolean }) {
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [selected, setSelected] = useState<Assignment | null>(null);
  const [mouza, setMouza] = useState<MouzaGeo | null>(null);
  const [fix, setFix] = useState<Fix | null>(null);
  const [photo, setPhoto] = useState<CapturedPhoto | null>(null);
  const [witnesses, setWitnesses] = useState<string[]>(['', '']);
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ status: string; score: number; band: string; signals: { code: string; passed: boolean; detail: string }[] } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const rows = await api.assignments(enrolment.officerId);
      setAssignments(rows);
    } catch (err) {
      setLoadError(String(err));
    }
  }, [enrolment.officerId]);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    if (!selected) return;
    api.mouza(selected.mouza_code).then(setMouza).catch(() => setMouza(null));
    setFix(null); setPhoto(null); setResult(null);
  }, [selected]);

  async function doFix() {
    setBusy(true); setError(null);
    try {
      const f = await captureFix();
      setFix(f);
    } catch (err) {
      setError(String(err));
    } finally { setBusy(false); }
  }

  async function submit() {
    if (!selected || !fix || !photo) return;
    setBusy(true); setError(null);
    try {
      const submissionId = 'GV-' + randomId();
      const nonce = randomNonce();
      const hmac = await hmacHex(
        enrolment.secret,
        attestationBody(nonce, fix.latitude, fix.longitude, fix.capturedAt, photo.sha256),
      );
      const payload = {
        client_submission_id: submissionId,
        mutation_id: selected.mutation_id,
        district_code: selected.district_code,
        mouza_code: selected.mouza_code,
        khasra_no: selected.khasra_no,
        officer_id: enrolment.officerId,
        device_id: enrolment.deviceId,
        app_version: '1.0.0',
        latitude: fix.latitude,
        longitude: fix.longitude,
        accuracy_m: fix.accuracyM,
        altitude_m: fix.altitudeM,
        provider: fix.provider,
        satellites: null,
        monotonic_ms: fix.monotonicMs,
        captured_at: fix.capturedAt.toISOString(),
        photo_sha256: photo.sha256,
        photo_phash: photo.dhash,
        photo_bytes: photo.bytes,
        photo_exif_datetime: photo.exifDateTime,
        photo_source: 'camera',
        witness_cnics: witnesses.filter((w) => w.trim()).map((w) => w.replace(/\D/g, '')),
        notes,
        nonce,
        hmac,
      };

      if (!online) {
        await queue.enqueue(payload);
        setResult(null);
        setError(null);
        alert(T('submit_queued'));
        setFix(null); setPhoto(null); setWitnesses(['', '']); setNotes('');
        return;
      }

      const res = await api.submitVisit(payload);
      setResult({ status: res.status, score: res.trust_score, band: res.trust_band, signals: res.trust_signals });
      setFix(null); setPhoto(null); setWitnesses(['', '']); setNotes('');
      refresh();
    } catch (err) {
      // Even a rejected HTTP response is worth keeping: the officer did the
      // work, and the evidence should be re-syncable rather than lost.
      setError(String(err));
    } finally { setBusy(false); }
  }

  const band = accuracyBand(fix?.accuracyM ?? null);
  const ring = parsePolygonWkt(mouza?.boundary_wkt ?? null);
  const inside = fix && ring ? pointInRing({ lat: fix.latitude, lon: fix.longitude }, ring) : null;

  return (
    <>
      <div className="card">
        <h2>{T('select_assignment')}</h2>
        {loadError && <div className="banner warn">{loadError}</div>}
        <select
          value={selected?.mutation_id ?? ''}
          onChange={(e) => setSelected(assignments.find((a) => a.mutation_id === e.target.value) ?? null)}
        >
          <option value="">—</option>
          {assignments.map((a) => (
            <option key={a.mutation_id} value={a.mutation_id}>
              {a.mouza_name ?? a.mouza_code} · {T('khasra')} {a.khasra_no}
              {a.ground_verified ? ' ✓' : ''}
            </option>
          ))}
        </select>

        {selected && (
          <>
            <div className="kv"><span className="k">{T('khasra')}</span><span className="v ltr">{selected.khasra_no}</span></div>
            <div className="kv"><span className="k">{T('khewat')}</span><span className="v ltr">{selected.khewat_no ?? '—'}</span></div>
            <div className="kv"><span className="k">{T('area')}</span><span className="v ltr">{selected.area_kanal}</span></div>
            <div className="kv"><span className="k">{T('status')}</span>
              <span className="v"><span className={`sev ${selected.flags.some((f) => f.severity === 'RED') ? 'RED' : selected.flags.length ? 'AMBER' : 'INFO'}`}>{selected.status}</span></span>
            </div>
            {selected.flags.length > 0 && (
              <div className="banner warn">
                {selected.flags.map((f, i) => (
                  <div key={i}><strong className="ltr">{f.rule_id}</strong> · <span className={`sev ${f.severity}`}>{f.severity}</span></div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {selected && (
        <div className="card">
          <h2>{T('capture_gps')}</h2>
          <button className="btn secondary" disabled={busy} onClick={doFix}>
            {busy ? T('capturing_gps') : fix ? T('gps_ok') : T('capture_gps')}
          </button>
          {fix && (
            <>
              <div className="kv"><span className="k">Lat/Lon</span><span className="v ltr">{fix.latitude.toFixed(6)}, {fix.longitude.toFixed(6)}</span></div>
              <div className="kv"><span className="k">{T('accuracy')}</span><span className="v ltr">±{fix.accuracyM?.toFixed(1) ?? '—'} m</span></div>
              <div className="kv"><span className="k">{T('captured_at')}</span><span className="v ltr">{fix.capturedAt.toLocaleTimeString()}</span></div>
              {band === 'good' || band === 'ok'
                ? <div className="banner good">{T('gps_ok')}</div>
                : <div className="banner warn">{T('gps_poor')} ({band})</div>}
              {fix.suspicious && <div className="banner bad">{fix.suspiciousReason}</div>}
              {ring ? (
                <div className={`banner ${inside ? 'good' : 'warn'}`}>
                  {inside ? T('inside_boundary') : T('outside_boundary')}
                </div>
              ) : (
                <div className="banner info">
                  {T('boundary_unknown')} — {T('boundary_unknown_help')}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {selected && fix && (
        <div className="card">
          <h2>{T('take_photo')}</h2>
          <CameraCapture
            photo={photo}
            onCapture={setPhoto}
            onReject={(r) => setError(`${T('gallery_blocked')} — ${r}`)}
            takeLabel={T('take_photo')}
            retakeLabel={T('retake_photo')}
            galleryLabel={T('gallery_blocked')}
          />
        </div>
      )}

      {selected && fix && photo && (
        <div className="card">
          <h2>{T('witnesses')}</h2>
          {witnesses.map((w, i) => (
            <div key={i} style={{ marginBottom: 8 }}>
              <label>{T('witness_cnic')} {i + 1}</label>
              <input
                className="ltr"
                type="text"
                inputMode="numeric"
                pattern="[0-9\-]*"
                value={w}
                onChange={(e) => {
                  const next = [...witnesses];
                  next[i] = e.target.value.replace(/[^\d-]/g, '').slice(0, 15);
                  setWitnesses(next);
                }}
                placeholder="17301-1234567-1"
              />
            </div>
          ))}
          <button className="btn ghost small" onClick={() => setWitnesses([...witnesses, ''])}>
            + {T('add_witness')}
          </button>

          <label>{T('notes')}</label>
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} />

          {error && <div className="banner bad">{error}</div>}

          <div style={{ marginTop: 14 }}>
            <button className="btn" disabled={busy} onClick={submit}>{T('submit')}</button>
          </div>
          <p className="hint">{T('help_text')}</p>
        </div>
      )}

      {result && (
        <div className="card">
          <h2>{T('trust_score')}</h2>
          <div className="score-ring">
            <span className="num ltr">{result.score}</span>
            <div>
              <div><strong>{result.band}</strong></div>
              <div className="hint">{result.status}</div>
            </div>
          </div>
          <h3>{T('flags')}</h3>
          {result.signals.map((s) => (
            <div key={s.code} className={`signal ${s.passed ? 'pass' : 'fail'}`}>
              <span className="dot" />
              <span><span className="code ltr">{s.code}</span> — {s.detail}</span>
            </div>
          ))}
        </div>
      )}

      <div className="card">
        <h2>{T('help')}</h2>
        <p className="hint">{T('help_text')}</p>
        <div className="banner info">{T('danger_help')}</div>
      </div>
    </>
  );
}

// --------------------------------------------------------------------------
// Queue
// --------------------------------------------------------------------------

function QueueTab({ T, enrolment, online }: { T: (k: string) => string; enrolment: Enrolment; online: boolean }) {
  const [items, setItems] = useState<queue.QueuedVisit[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [log, setLog] = useState<string[]>([]);

  const refresh = useCallback(async () => { setItems(await queue.all()); }, []);
  useEffect(() => { refresh(); }, [refresh]);

  async function sync() {
    setSyncing(true); setLog([]);
    const pendingItems = await queue.pending();
    for (const item of pendingItems) {
      try {
        await api.submitVisit(item.payload as Record<string, unknown>);
        await queue.markSynced(item.submissionId);
        setLog((l) => [...l, `✓ ${item.submissionId.slice(0, 18)}`]);
      } catch (err) {
        await queue.markFailed(item.submissionId, String(err));
        setLog((l) => [...l, `✗ ${item.submissionId.slice(0, 18)}: ${String(err).slice(0, 90)}`]);
      }
    }
    await refresh();
    setSyncing(false);
  }

  const pendingCount = items.filter((i) => !i.syncedAt).length;

  return (
    <>
      <div className="card">
        <div className="kv">
          <span className="k">{T('pending')}</span>
          <span className="v ltr">{pendingCount}</span>
        </div>
        <div style={{ marginTop: 12 }}>
          <button className="btn" disabled={syncing || !online || pendingCount === 0} onClick={sync}>
            {syncing ? T('syncing') : `${T('sync_now')} (${pendingCount})`}
          </button>
        </div>
        {!online && <div className="banner warn">{T('offline')}</div>}
        {log.map((l, i) => <div key={i} className="hint mono">{l}</div>)}
      </div>

      {items.length === 0 ? (
        <div className="empty">{T('queue_empty')}</div>
      ) : items.map((it) => (
        <div className="queue-item" key={it.submissionId}>
          <div className="meta">
            <div className="t ltr">
              {(it.payload.mouza_code as string) ?? '—'} · {T('khasra')} {(it.payload.khasra_no as string) ?? '—'}
            </div>
            <div className="id ltr">
              {it.submissionId.slice(0, 18)} · {new Date(it.createdAt).toLocaleString()}
              {it.syncedAt ? ` · ${T('synced')}` : ` · ${T('pending')}`}
            </div>
            {it.lastError && <div className="hint mono">{it.lastError.slice(0, 80)}</div>}
          </div>
          <button
            className="btn danger small"
            onClick={async () => { await queue.remove(it.submissionId); refresh(); }}
          >
            {T('delete')}
          </button>
        </div>
      ))}
    </>
  );
}

// --------------------------------------------------------------------------
// Map tab: an officer-facing view of record integrity
// --------------------------------------------------------------------------

function MapTab({ T }: { T: (k: string) => string }) {
  const [chain, setChain] = useState<{ intact: boolean; entries_checked: number; head_hash: string | null;
    findings: { kind: string; seq: number | null; detail: string }[] } | null>(null);
  const [mutationId, setMutationId] = useState('MUT-KP-1001');
  const [clearance, setClearance] = useState<ClearanceResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { api.chainVerify().then(setChain).catch((e) => setError(String(e))); }, []);

  async function check() {
    setError(null); setClearance(null);
    try { setClearance(await api.clearance(mutationId)); }
    catch (err) { setError(String(err)); }
  }

  return (
    <>
      <div className="card">
        <h2>{T('map_title')}</h2>
        <p className="hint">
          {T('proof_hash')}: <span className="ltr mono">{chain?.head_hash?.slice(0, 32) ?? '—'}…</span>
        </p>
        {chain && (
          <div className={`banner ${chain.intact ? 'good' : 'bad'}`}>
            {chain.intact ? 'Chain intact' : 'TAMPERING DETECTED'} · {chain.entries_checked} entries
          </div>
        )}
        {chain?.findings.map((f, i) => (
          <div key={i} className="banner bad">
            <strong className="ltr">{f.kind}</strong> @ seq {f.seq}: {f.detail}
          </div>
        ))}
      </div>

      <div className="card">
        <h2>{T('view_result')}</h2>
        <label>Mutation ID</label>
        <input className="ltr" type="text" value={mutationId} onChange={(e) => setMutationId(e.target.value)} />
        <div style={{ marginTop: 12 }}>
          <button className="btn" onClick={check}>{T('view_result')}</button>
        </div>
        {error && <div className="banner bad">{error}</div>}

        {clearance && (
          <>
            <div className="kv"><span className="k">Decision</span>
              <span className="v"><span className={`sev ${clearance.severity}`}>{clearance.decision}</span></span></div>
            <div className="kv"><span className="k">Phase</span><span className="v ltr">{clearance.phase}</span></div>
            <div className="kv"><span className="k">Score</span><span className="v ltr">{clearance.score}</span></div>
            <div className="kv"><span className="k">{T('proof_hash')}</span>
              <span className="v ltr mono">{clearance.ledger_hash?.slice(0, 24) ?? '—'}…</span></div>

            <h3>{T('flags')}</h3>
            {clearance.findings.length === 0 && <div className="banner good">{T('no_flags')}</div>}
            {clearance.findings.map((f, i) => (
              <div className="flag" key={i}>
                <div className="t">
                  <span className={`sev ${f.severity}`}>{f.severity}</span>
                  <span className="ltr">{f.rule_id}</span> {f.title}
                </div>
                <div className="d">{f.detail}</div>
                {f.recommended_action && <div className="a">{f.recommended_action}</div>}
              </div>
            ))}

            {clearance.required_actions.length > 0 && (
              <>
                <h3>{T('required_actions')}</h3>
                <ul className="actions">
                  {clearance.required_actions.map((a, i) => (
                    <li key={i}>{a.action} <em>({a.owner_role})</em></li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </div>
    </>
  );
}
