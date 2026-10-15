import { useEffect, useMemo } from 'react';
import { MapContainer, TileLayer, Polygon, Circle, Marker, Tooltip, useMap } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { Ring, parsePolygonWkt } from '../geo';

function FitBounds({ ring, point }: { ring: Ring[]; point: Ring }) {
  const map = useMap();
  useEffect(() => {
    const bounds = L.latLngBounds([...ring.map((r) => [r.lat, r.lon] as [number, number]),
                                   [point.lat, point.lon] as [number, number]]);
    map.fitBounds(bounds, { padding: [24, 24], maxZoom: 17 });
  }, [map, ring, point]);
  return null;
}

interface Props {
  wkt: string | null;
  fix: { latitude: number; longitude: number; accuracyM: number | null } | null;
}

/**
 * The officer's own view of whether they are standing on the parcel.
 *
 * This is feedback, not evidence: the authoritative geofence runs server-side
 * against PostGIS (or shapely on SQLite) because only the server can be trusted
 * to evaluate it. The app shows the same answer so the Patwari knows *before*
 * they submit, and can walk fifty metres rather than waste a day.
 */
export default function MapView({ wkt, fix }: Props) {
  const ring = useMemo(() => parsePolygonWkt(wkt), [wkt]);

  if (!ring || !fix) {
    return (
      <div className="map" style={{ display: 'grid', placeItems: 'center', background: '#eef3f0', color: '#5d6b63', fontSize: 13 }}>
        {ring ? 'No GPS fix yet' : 'No digitised boundary for this mouza'}
      </div>
    );
  }

  const marker = L.divIcon({
    className: '',
    html: '<div style="width:16px;height:16px;border-radius:50%;background:#0b5c3f;border:3px solid #fff;box-shadow:0 0 0 2px rgba(11,92,63,.4)"></div>',
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });

  return (
    <div className="map">
      <MapContainer
        center={[fix.latitude, fix.longitude]}
        zoom={16}
        style={{ height: '100%', width: '100%' }}
        scrollWheelZoom={false}
      >
        <TileLayer
          attribution='&copy; OpenStreetMap'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          maxZoom={19}
        />
        <Polygon positions={ring.map((r) => [r.lat, r.lon] as [number, number])}
                 pathOptions={{ color: '#0b5c3f', fillOpacity: 0.08, weight: 2 }} />
        {fix.accuracyM != null && (
          <Circle
            center={[fix.latitude, fix.longitude]}
            radius={fix.accuracyM}
            pathOptions={{ color: '#b8860b', fillOpacity: 0.08, weight: 1, dashArray: '4 4' }}
          >
            <Tooltip direction="top">±{fix.accuracyM.toFixed(0)} m</Tooltip>
          </Circle>
        )}
        <Marker position={[fix.latitude, fix.longitude]} icon={marker}>
          <Tooltip direction="top" permanent>Your fix</Tooltip>
        </Marker>
        <FitBounds ring={ring} point={{ lat: fix.latitude, lon: fix.longitude }} />
      </MapContainer>
    </div>
  );
}
