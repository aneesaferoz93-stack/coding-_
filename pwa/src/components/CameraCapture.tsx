import { useRef, useState } from 'react';
import { processCapture, CapturedPhoto } from '../photo';

interface Props {
  photo: CapturedPhoto | null;
  onCapture: (p: CapturedPhoto) => void;
  onReject: (reason: string) => void;
  takeLabel: string;
  retakeLabel: string;
  galleryLabel: string;
}

/**
 * Camera-only capture.
 *
 * The `capture="environment"` attribute is what makes mobile browsers open the
 * camera directly instead of offering the gallery. It is a hint, not a
 * guarantee — so we also verify afterwards that the file is fresh and image/*
 * typed, and the server independently scores `photo_source`. Defence in depth:
 * no single client-side control can be trusted, and no single server-side
 * control catches every spoof.
 */
export default function CameraCapture({
  photo, onCapture, onReject, takeLabel, retakeLabel, galleryLabel,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  async function handle(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      if (!file.type.startsWith('image/')) {
        onReject(galleryLabel);
        return;
      }
      // A file that was last modified hours before this screen opened is
      // almost certainly a gallery pick, not a fresh capture.
      const ageMs = Date.now() - file.lastModified;
      if (ageMs > 5 * 60 * 1000) {
        onReject(galleryLabel);
        return;
      }
      const captured = await processCapture(file);
      onCapture(captured);
    } catch (err) {
      onReject(String(err));
    } finally {
      setBusy(false);
      // Reset so the same file can be re-selected after a rejection.
      if (inputRef.current) inputRef.current.value = '';
    }
  }

  return (
    <div>
      {photo ? (
        <>
          <img className="photo-preview" src={photo.dataUrl} alt="Site photograph" />
          <div className="kv">
            <span className="k">SHA-256</span>
            <span className="v ltr mono">{photo.sha256.slice(0, 32)}…</span>
          </div>
          <div className="kv">
            <span className="k">dHash</span>
            <span className="v ltr mono">{photo.dhash}</span>
          </div>
          <div className="kv">
            <span className="k">EXIF</span>
            <span className="v ltr">{photo.exifDateTime ?? '—'}</span>
          </div>
          <button
            type="button"
            className="btn secondary small"
            style={{ marginTop: 10 }}
            onClick={() => inputRef.current?.click()}
          >
            {retakeLabel}
          </button>
        </>
      ) : (
        <button type="button" className="btn secondary" disabled={busy} onClick={() => inputRef.current?.click()}>
          {busy ? '…' : takeLabel}
        </button>
      )}
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        capture="environment"
        style={{ display: 'none' }}
        onChange={handle}
      />
    </div>
  );
}
