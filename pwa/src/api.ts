/**
 * api.ts — client for the integrity API.
 *
 * All calls are relative ('/v1/...'). The dev server proxies them to FastAPI,
 * and in production the app is served from the same origin. Browser code must
 * never address the API by absolute host: the officer's phone does not know or
 * care where the server lives, and hard-coding a host breaks TLS and offline
 * caching.
 */

const DEVICE_TOKEN_KEY = 'klri.device_token';
const DEVICE_SECRET_KEY = 'klri.device_secret';
const DEVICE_ID_KEY = 'klri.device_id';
const OFFICER_KEY = 'klri.officer_id';

export interface Enrolment {
  deviceId: string;
  officerId: string;
  secret: string;
  token: string;
}

export function loadEnrolment(): Enrolment | null {
  const token = localStorage.getItem(DEVICE_TOKEN_KEY);
  const secret = localStorage.getItem(DEVICE_SECRET_KEY);
  const deviceId = localStorage.getItem(DEVICE_ID_KEY);
  const officerId = localStorage.getItem(OFFICER_KEY);
  if (!token || !secret || !deviceId || !officerId) return null;
  return { deviceId, officerId, secret, token };
}

export function saveEnrolment(e: Enrolment) {
  localStorage.setItem(DEVICE_TOKEN_KEY, e.token);
  localStorage.setItem(DEVICE_SECRET_KEY, e.secret);
  localStorage.setItem(DEVICE_ID_KEY, e.deviceId);
  localStorage.setItem(OFFICER_KEY, e.officerId);
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json', ...extra };
  const e = loadEnrolment();
  if (e) h.Authorization = `Bearer ${e.token}`;
  return h;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { ...init, headers: headers(init.headers as Record<string, string>) });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

export interface Assignment {
  mutation_id: string;
  district_code: string;
  tehsil_code: string;
  mouza_code: string;
  mouza_name: string | null;
  khewat_no: string | null;
  khasra_no: string;
  area_kanal: string;
  transaction_type: string;
  status: string;
  proposed_at: string;
  flags: { rule_id: string; severity: string }[];
  ground_verified: boolean;
}

export interface MouzaGeo {
  code: string;
  name_en: string;
  digitised: boolean;
  boundary_wkt: string | null;
  centroid: { lat: number | null; lon: number | null };
  total_khasras: number | null;
}

export interface TrustSignal {
  code: string;
  passed: boolean;
  detail: string;
  penalty: number;
}

export interface VerificationResult {
  id: string;
  status: 'PENDING' | 'ACCEPTED' | 'REJECTED' | 'SUPERSEDED';
  trust_score: number;
  trust_band: string;
  trust_signals: TrustSignal[];
  inside_mouza: boolean | null;
  distance_to_boundary_m: number | null;
  offline_hours: number | null;
}

export interface Flag {
  id: number;
  rule_id: string;
  rule_name: string;
  severity: 'RED' | 'AMBER' | 'INFO';
  title: string;
  detail: string;
  recommended_action: string | null;
  evidence: Record<string, unknown>;
}

export interface ClearanceResult {
  clearance_id: string;
  decision: string;
  phase: string;
  severity: string;
  score: number;
  reasons: string[];
  required_actions: { action: string; owner_role: string; sla_hours: number | null }[];
  ledger_seq: number | null;
  ledger_hash: string | null;
  findings: Flag[];
}

// --------------------------------------------------------------------------
// Calls
// --------------------------------------------------------------------------

export const api = {
  enrol: (body: { device_id: string; officer_id: string; label?: string; platform?: string }) =>
    request<{ device_id: string; officer_id: string; attestation_secret: string; device_token: string }>(
      '/v1/ground/devices', { method: 'POST', body: JSON.stringify(body) }),

  assignments: (patwariId?: string, district?: string) => {
    const q = new URLSearchParams();
    if (patwariId) q.set('patwari_id', patwariId);
    if (district) q.set('district_code', district);
    return request<Assignment[]>(`/v1/geo/assignments?${q}`);
  },

  mouza: (code: string) => request<MouzaGeo>(`/v1/geo/mouza/${code}`),

  mouzas: (district: string) => request<MouzaGeo[]>(`/v1/geo/district/${district}/mouzas`),

  submitVisit: (payload: Record<string, unknown>) =>
    request<VerificationResult>('/v1/ground/verify', { method: 'POST', body: JSON.stringify(payload) }),

  mutationFlags: (mutationId: string) =>
    request<Flag[]>(`/v1/fraud/flags?khasra_no=` + encodeURIComponent('')).catch(() => [] as Flag[]),

  evaluate: (mutationId: string) =>
    request<{ decision: string; severity: string; score: number; reasons: string[]; findings: Flag[] }>(
      `/v1/mutations/${encodeURIComponent(mutationId)}/evaluate`, { method: 'POST' }),

  clearance: (mutationId: string) =>
    request<ClearanceResult>('/v1/integrations/efard/clearance', {
      method: 'POST',
      body: JSON.stringify({ mutation_id: mutationId }),
    }),

  verifyClearance: (clearanceId: string) =>
    request<Record<string, unknown>>(`/v1/integrations/efard/verify/${clearanceId}`),

  chainVerify: () =>
    request<{ intact: boolean; entries_checked: number; head_hash: string | null;
              findings: { kind: string; seq: number | null; detail: string }[] }>(
      '/v1/ledger/verify'),

  dashboard: () => request<Record<string, unknown>>('/v1/dashboard'),
};
