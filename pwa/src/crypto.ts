/**
 * crypto.ts — hashing and HMAC in the browser.
 *
 * The device enrolment secret never leaves the handset except as an HMAC over
 * a submission. That means a captured request can be replayed (we defend
 * against that with the nonce and the server's timestamp checks) but it cannot
 * be *forged* by someone who does not hold the phone.
 */

const encoder = new TextEncoder();

export async function sha256Hex(data: ArrayBuffer | string): Promise<string> {
  const buf = typeof data === 'string' ? encoder.encode(data) : data;
  const digest = await crypto.subtle.digest('SHA-256', buf);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function hmacHex(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign('HMAC', key, encoder.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * The exact string the server HMACs too. Must stay byte-identical with
 * `app/services/ground.py::attestation_body`:
 *
 *   nonce : lat(7dp) : lon(7dp) : UTC seconds : photo_sha256
 *
 * Whole seconds and 7 decimal places are intentional: they are the same in
 * Python, TypeScript and on every handset locale.
 */
export function attestationBody(
  nonce: string,
  lat: number,
  lon: number,
  capturedAt: Date,
  photoSha256: string | null,
): string {
  const stamp = capturedAt.toISOString().slice(0, 19); // YYYY-MM-DDTHH:MM:SS (UTC)
  return `${nonce}:${lat.toFixed(7)}:${lon.toFixed(7)}:${stamp}:${photoSha256 ?? ''}`;
}

export function randomId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export function randomNonce(): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}
