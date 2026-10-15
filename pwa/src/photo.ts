/**
 * photo.ts — camera-only capture, content hashing and perceptual hashing.
 *
 * Two hashes are produced for every photo, and they answer different questions:
 *
 *   SHA-256  — exact duplicate detection. Catches the same file submitted twice.
 *   dHash    — near-duplicate detection. Catches the same *scene* re-photographed
 *              (or lightly cropped/compressed) for a different khasra, which is
 *              how a Patwari reuses one visit to close several files.
 *
 * We also refuse gallery uploads. A file picked from the gallery proves nothing
 * about where the officer was standing, and the whole point of this app is to
 * prove exactly that.
 */

import { sha256Hex } from './crypto';

export interface CapturedPhoto {
  file: File;
  dataUrl: string;
  sha256: string;
  dhash: string;
  bytes: number;
  /** EXIF DateTimeOriginal, if the browser exposes it. May be null on iOS. */
  exifDateTime: string | null;
  width: number;
  height: number;
}

const hex = (n: number) => n.toString(16).padStart(2, '0');

async function toBitmap(file: File): Promise<ImageBitmap> {
  if ('createImageBitmap' in window) return createImageBitmap(file);
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    await new Promise((res, rej) => {
      img.onload = res;
      img.onerror = rej;
      img.src = url;
    });
    return img as unknown as ImageBitmap;
  } finally {
    URL.revokeObjectURL(url);
  }
}

/**
 * Difference hash: downscale to 9x8 grayscale, compare each pixel to its right
 * neighbour, emit 64 bits. Cheap, and tolerant of recompression and mild crops.
 */
async function dHash(file: File): Promise<string> {
  const bmp = await toBitmap(file);
  const W = 9, H = 8;
  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return '';
  ctx.drawImage(bmp as CanvasImageSource, 0, 0, W, H);
  const { data } = ctx.getImageData(0, 0, W, H);
  const gray: number[] = [];
  for (let i = 0; i < data.length; i += 4) {
    gray.push(0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]);
  }
  let bits = '';
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W - 1; x++) {
      bits += gray[y * W + x] < gray[y * W + x + 1] ? '1' : '0';
    }
  }
  let out = '';
  for (let i = 0; i < bits.length; i += 4) out += parseInt(bits.slice(i, i + 4), 2).toString(16);
  return out;
}

export function hammingHex(a: string, b: string): number {
  if (a.length !== b.length) return 999;
  let d = 0;
  for (let i = 0; i < a.length; i++) {
    const x = parseInt(a[i], 16) ^ parseInt(b[i], 16);
    d += ((x & 1) + ((x >> 1) & 1) + ((x >> 2) & 1) + ((x >> 3) & 1));
  }
  return d;
}

/** Best-effort EXIF timestamp. Browsers differ; a null result is scored, not fatal. */
function readExifDateTime(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const view = new DataView(reader.result as ArrayBuffer);
        if (view.getUint16(0) !== 0xffd8) return resolve(null);
        let offset = 2;
        while (offset < view.byteLength) {
          if (view.getUint16(offset) !== 0xffe1) {
            offset += 2 + view.getUint16(offset + 2);
            continue;
          }
          const marker = view.getUint32(offset + 4);
          if (marker !== 0x45786966) return resolve(null); // "Exif"
          const tiff = offset + 10;
          const little = view.getUint16(tiff) === 0x4949;
          const ifd = tiff + view.getUint32(tiff + 4, little);
          const count = view.getUint16(ifd, little);
          for (let i = 0; i < count; i++) {
            const entry = ifd + 2 + i * 12;
            if (view.getUint16(entry, little) !== 0x0132) continue; // DateTime
            const size = view.getUint32(entry + 4, little);
            const valueOffset = size > 4 ? tiff + view.getUint32(entry + 8, little) : entry + 8;
            let raw = '';
            for (let k = 0; k < size - 1; k++) raw += String.fromCharCode(view.getUint8(valueOffset + k));
            // "2026:10:15 09:41:03" -> "2026-10-15T09:41:03"
            const iso = raw.replace(/^(\d{4}):(\d{2}):(\d{2})/, '$1-$2-$3').replace(' ', 'T');
            return resolve(iso);
          }
          return resolve(null);
        }
        resolve(null);
      } catch {
        resolve(null);
      }
    };
    reader.onerror = () => resolve(null);
    reader.readAsArrayBuffer(file.slice(0, 128 * 1024));
  });
}

export async function processCapture(file: File): Promise<CapturedPhoto> {
  const buf = await file.arrayBuffer();
  const [sha256, dhash, exifDateTime] = await Promise.all([
    sha256Hex(buf),
    dHash(file),
    readExifDateTime(file),
  ]);
  const bmp = await toBitmap(file);
  const dataUrl = await new Promise<string>((resolve) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.readAsDataURL(file);
  });
  return {
    file,
    dataUrl,
    sha256,
    dhash,
    bytes: file.size,
    exifDateTime,
    width: (bmp as ImageBitmap).width ?? 0,
    height: (bmp as ImageBitmap).height ?? 0,
  };
}
