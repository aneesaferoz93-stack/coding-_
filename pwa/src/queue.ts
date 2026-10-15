/**
 * queue.ts — the offline store.
 *
 * A Patwari in Matta, Kohistan or Upper Dir will lose signal for hours or days.
 * If the app refuses to work without connectivity, field staff will go back to
 * paper within a fortnight and the whole verification layer dies. So every
 * visit is written to IndexedDB the moment it is captured, with:
 *
 *   - the device's own wall-clock time at capture (never "now" at sync time),
 *   - a monotonic reading (performance.now) so we can tell a clock jump from
 *     the passage of time,
 *   - a chain hash linking each queued item to the previous one, so a queue
 *     that was edited on the handset can be detected server-side.
 *
 * The sync button is deliberately explicit rather than automatic: on 2G an
 * automatic retry storm burns the officer's battery and their data bundle.
 */

import { sha256Hex } from './crypto';

export interface QueuedVisit {
  submissionId: string;
  createdAt: string; // ISO, device wall clock
  monotonicMs: number; // performance.now() at capture
  prevQueueHash: string;
  queueHash: string;
  attempts: number;
  lastError?: string;
  syncedAt?: string;
  payload: Record<string, unknown>;
}

const DB_NAME = 'klri-ground';
const DB_VERSION = 1;
const STORE = 'visits';
const GENESIS = '0'.repeat(64);

let dbPromise: Promise<IDBDatabase> | null = null;

function open(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const store = db.createObjectStore(STORE, { keyPath: 'submissionId' });
        store.createIndex('createdAt', 'createdAt');
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

async function tx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open();
  return new Promise<T>((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const req = fn(t.objectStore(STORE));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function all(): Promise<QueuedVisit[]> {
  const rows = await tx<QueuedVisit[]>('readonly', (s) => s.getAll() as IDBRequest<QueuedVisit[]>);
  return rows.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
}

export async function pending(): Promise<QueuedVisit[]> {
  return (await all()).filter((v) => !v.syncedAt);
}

export async function tailHash(): Promise<string> {
  const rows = await all();
  return rows.length ? rows[rows.length - 1].queueHash : GENESIS;
}

export async function enqueue(payload: Record<string, unknown>): Promise<QueuedVisit> {
  const prev = await tailHash();
  const createdAt = new Date().toISOString();
  const submissionId = payload.client_submission_id as string;
  const body = JSON.stringify({ prev, createdAt, payload });
  const queueHash = await sha256Hex(body);
  const row: QueuedVisit = {
    submissionId,
    createdAt,
    monotonicMs: Math.round(performance.now()),
    prevQueueHash: prev,
    queueHash,
    attempts: 0,
    payload,
  };
  await tx('readwrite', (s) => s.put(row));
  return row;
}

export async function markSynced(id: string) {
  const db = await open();
  const existing = await tx<QueuedVisit>('readonly', (s) => s.get(id) as IDBRequest<QueuedVisit>);
  existing.syncedAt = new Date().toISOString();
  delete existing.lastError;
  await new Promise<void>((resolve, reject) => {
    const t = db.transaction(STORE, 'readwrite');
    t.objectStore(STORE).put(existing);
    t.oncomplete = () => resolve();
    t.onerror = () => reject(t.error);
  });
}

export async function markFailed(id: string, error: string) {
  const db = await open();
  const existing = await tx<QueuedVisit>('readonly', (s) => s.get(id) as IDBRequest<QueuedVisit>);
  existing.attempts += 1;
  existing.lastError = error;
  await new Promise<void>((resolve, reject) => {
    const t = db.transaction(STORE, 'readwrite');
    t.objectStore(STORE).put(existing);
    t.oncomplete = () => resolve();
    t.onerror = () => reject(t.error);
  });
}

export async function remove(id: string) {
  await tx('readwrite', (s) => s.delete(id));
}

export async function clearSynced() {
  const rows = await all();
  for (const r of rows) if (r.syncedAt) await remove(r.submissionId);
}
