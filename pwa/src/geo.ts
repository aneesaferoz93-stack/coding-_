/**
 * geo.ts — location capture and WKT handling.
 *
 * HONEST STATEMENT OF WHAT THIS CAN AND CANNOT PROVE
 * --------------------------------------------------
 * A browser cannot prove that a GPS fix is real. Android's mock-location flag
 * (`Location.isFromMockProvider`) is not exposed to web code, and a determined
 * user with a rooted phone can feed any coordinate to any app. Anyone who tells
 * you their web app "cannot be GPS-spoofed" is selling something.
 *
 * So the PWA does the things it *can* do, and the server does the rest:
 *
 *  - HERE (client):  request a high-accuracy fix with maximumAge=0 so it must
 *                    be a fresh measurement; record accuracy, altitude,
 *                    heading/speed; reject absurd coordinates; compute a
 *                    perceptual hash of the photo so a reused picture is
 *                    detectable even if the metadata was stripped.
 *  - SERVER:         physics (travel speed between fixes), geofence against the
 *                    cadastral polygon, EXIF-to-fix correlation, photo reuse
 *                    across the whole corpus, device binding, and the offline
 *                    window.
 *
 * For Phase 2 the recommendation is a thin Android wrapper (or Play Integrity
 * attestation via a native shell) that can report the mock flag and a device
 * integrity verdict. The server already has a slot for it
 * (`integrity_token`), and the scoring model already treats it as one signal
 * among twelve rather than as a gate.
 */

export interface Fix {
  latitude: number;
  longitude: number;
  accuracyM: number | null;
  altitudeM: number | null;
  heading: number | null;
  speed: number | null;
  provider: string;
  capturedAt: Date;
  monotonicMs: number;
  /** True when the browser reports impossible values, or the fix is absurdly precise. */
  suspicious: boolean;
  suspiciousReason?: string;
}

const KP_BBOX = { minLat: 31.3, maxLat: 36.9, minLon: 69.2, maxLon: 74.1 };

export function insideKp(lat: number, lon: number): boolean {
  return lat >= KP_BBOX.minLat && lat <= KP_BBOX.maxLat && lon >= KP_BBOX.minLon && lon <= KP_BBOX.maxLon;
}

export async function captureFix(timeoutMs = 20000): Promise<Fix> {
  if (!('geolocation' in navigator)) {
    throw new Error('This device has no location hardware or it is switched off.');
  }
  const pos = await new Promise<GeolocationPosition>((resolve, reject) => {
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: true,
      // maximumAge=0 forces a fresh measurement rather than a cached one.
      maximumAge: 0,
      timeout: timeoutMs,
    });
  });

  const c = pos.coords;
  const capturedAt = new Date(pos.timestamp);
  let suspicious = false;
  let reason: string | undefined;

  if (!insideKp(c.latitude, c.longitude)) {
    suspicious = true;
    reason = 'Fix is outside Khyber Pakhtunkhwa.';
  }
  if (c.accuracy != null && c.accuracy > 100) {
    suspicious = true;
    reason = `Accuracy ±${Math.round(c.accuracy)} m is too coarse to place you on a parcel.`;
  }
  if (c.accuracy != null && c.accuracy < 1) {
    suspicious = true;
    reason = 'Accuracy is implausibly precise for a handset outdoors.';
  }

  return {
    latitude: c.latitude,
    longitude: c.longitude,
    accuracyM: c.accuracy ?? null,
    altitudeM: c.altitude ?? null,
    heading: c.heading ?? null,
    speed: c.speed ?? null,
    provider: 'browser-geolocation',
    capturedAt,
    monotonicMs: Math.round(performance.now()),
    suspicious,
    suspiciousReason: reason,
  };
}

export function accuracyBand(accuracyM: number | null): 'good' | 'ok' | 'poor' | 'bad' {
  if (accuracyM == null) return 'bad';
  if (accuracyM <= 10) return 'good';
  if (accuracyM <= 30) return 'ok';
  if (accuracyM <= 100) return 'poor';
  return 'bad';
}

// --------------------------------------------------------------------------
// WKT parsing (the server stores cadastral boundaries as WKT so the same data
// works with or without PostGIS).
// --------------------------------------------------------------------------

export interface Ring {
  lat: number;
  lon: number;
}

function parsePoint(text: string): Ring {
  const [lon, lat] = text.trim().split(/\s+/).map(Number);
  return { lat, lon };
}

export function parsePolygonWkt(wkt: string | null): Ring[] | null {
  if (!wkt) return null;
  const m = wkt.match(/POLYGON\s*\(\(([^)]*)\)\)/i);
  if (!m) return null;
  return m[1]
    .split(',')
    .map(parsePoint)
    .filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon));
}

export function centroid(ring: Ring[]): Ring {
  const lat = ring.reduce((a, p) => a + p.lat, 0) / ring.length;
  const lon = ring.reduce((a, p) => a + p.lon, 0) / ring.length;
  return { lat, lon };
}

/** Ray-casting point-in-polygon. Used only for the officer's own on-screen
 *  feedback; the authoritative check runs server-side against PostGIS. */
export function pointInRing(point: Ring, ring: Ring[]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i].lon, yi = ring[i].lat;
    const xj = ring[j].lon, yj = ring[j].lat;
    const intersect = (yi > point.lat) !== (yj > point.lat) &&
      point.lon < ((xj - xi) * (point.lat - yi)) / (yj - yi) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}
