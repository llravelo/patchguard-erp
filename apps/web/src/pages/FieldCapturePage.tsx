import { useCallback, useEffect, useRef, useState } from 'react'
import { MapContainer, Marker, Popup, TileLayer, useMapEvents } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { fetchDamageReport } from '../lib/api'
import type { BBox, DamageReportItem } from '../lib/types'


const FALLBACK_CENTER: [number, number] = [-33.882, 151.197]
const FALLBACK_ZOOM = 14

function mobilePin() {
  return L.divIcon({
    className: 'dmg-pin',
    html: '<div class="dmg-pin-inner mobile"><span>●</span></div>',
    iconSize: [22, 22],
    iconAnchor: [11, 22],
    popupAnchor: [0, -20],
  })
}

function bboxFromMap(map: L.Map): BBox {
  const b = map.getBounds()
  return { lonMin: b.getWest(), latMin: b.getSouth(), lonMax: b.getEast(), latMax: b.getNorth() }
}

function ViewportWatcher({ onBBox }: { onBBox: (bbox: BBox) => void }) {
  const map = useMapEvents({
    moveend: () => onBBox(bboxFromMap(map)),
    zoomend: () => onBBox(bboxFromMap(map)),
  })
  useEffect(() => { onBBox(bboxFromMap(map)) }, [map, onBBox])
  return null
}

export function FieldCapturePage() {
  const [pins, setPins] = useState<DamageReportItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [modalItem, setModalItem] = useState<DamageReportItem | null>(null)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const activeDateFrom = useRef('')
  const activeDateTo = useRef('')

  const abortRef = useRef<AbortController | null>(null)
  const timerRef = useRef<number | null>(null)

  const lastBBoxRef = useRef<BBox | null>(null)

  const refetch = useCallback(async (bbox: BBox) => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setLoading(true)
    setError(null)
    try {
      setPins(await fetchDamageReport(bbox, ctrl.signal, {
        source: 'mobile',
        dateFrom: activeDateFrom.current || undefined,
        dateTo: activeDateTo.current || undefined,
      }))
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') return
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  const onBBox = useCallback((bbox: BBox) => {
    lastBBoxRef.current = bbox
    if (timerRef.current) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => refetch(bbox), 300)
  }, [refetch])


  useEffect(() => () => { abortRef.current?.abort(); if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const damagePins = pins.filter((it) => it.damages.length > 0)

  return (
    <div className="dashboard-shell">
      <aside className="sidebar">
        <div className="card">
          <div className="card-title">
            Field Captures
            <span className="badge">{damagePins.length} pin{damagePins.length === 1 ? '' : 's'}{loading ? ' · loading' : ''}</span>
          </div>
          <p style={{ fontSize: 12, color: '#94a3b8', margin: '0 0 12px' }}>
            Real-world captures from mobile inspectors. Pan the map to load pins in the current view.
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <label style={{ fontSize: 11, color: '#94a3b8', width: 32, flexShrink: 0 }}>From</label>
              <input
                type="date"
                value={dateFrom}
                onChange={(e) => setDateFrom(e.target.value)}
                style={{ flex: 1, background: '#1e293b', border: '1px solid #334155', borderRadius: 4, color: 'inherit', padding: '4px 6px', fontSize: 12 }}
              />
            </div>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <label style={{ fontSize: 11, color: '#94a3b8', width: 32, flexShrink: 0 }}>To</label>
              <input
                type="date"
                value={dateTo}
                onChange={(e) => setDateTo(e.target.value)}
                style={{ flex: 1, background: '#1e293b', border: '1px solid #334155', borderRadius: 4, color: 'inherit', padding: '4px 6px', fontSize: 12 }}
              />
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                type="button"
                className="job-panel-submit"
                style={{ flex: 1 }}
                onClick={() => {
                  activeDateFrom.current = dateFrom
                  activeDateTo.current = dateTo
                  if (lastBBoxRef.current) refetch(lastBBoxRef.current)
                }}
              >
                Apply
              </button>
              {(dateFrom || dateTo) && (
                <button
                  type="button"
                  className="job-panel-submit"
                  style={{ flex: 1, background: '#334155' }}
                  onClick={() => {
                    setDateFrom(''); setDateTo('')
                    activeDateFrom.current = ''
                    activeDateTo.current = ''
                    if (lastBBoxRef.current) refetch(lastBBoxRef.current)
                  }}
                >
                  Clear
                </button>
              )}
            </div>
          </div>
          {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
        </div>
      </aside>

      <div className="map-wrap">
        <MapContainer center={FALLBACK_CENTER} zoom={FALLBACK_ZOOM} scrollWheelZoom style={{ height: '100%', width: '100%' }}>
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          />
          <ViewportWatcher onBBox={onBBox} />
          {damagePins.map((it) => (
            <Marker
              key={it.image_id}
              position={[it.latitude, it.longitude]}
              icon={mobilePin()}
              eventHandlers={{ click: () => setModalItem(it) }}
            >
              <Popup>
                <div className="popup">
                  <strong>{it.damages.length} damage{it.damages.length === 1 ? '' : 's'}</strong>
                  <div className="popup-meta">{new Date(it.captured_at).toLocaleString()}</div>
                  {it.vision_description && <div className="popup-vision">"{it.vision_description}"</div>}
                  <button className="popup-btn" onClick={() => setModalItem(it)}>View details</button>
                </div>
              </Popup>
            </Marker>
          ))}
        </MapContainer>
      </div>

      {modalItem && <DamageDetailModal item={modalItem} onClose={() => setModalItem(null)} />}
    </div>
  )
}

function DamageDetailModal({ item, onClose }: { item: DamageReportItem; onClose: () => void }) {
  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onEsc)
    return () => window.removeEventListener('keydown', onEsc)
  }, [onClose])
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <strong>{item.damages.length} damage{item.damages.length === 1 ? '' : 's'} detected</strong>
            <div className="modal-meta">
              {item.latitude.toFixed(5)}, {item.longitude.toFixed(5)} · {new Date(item.captured_at).toLocaleString()}
            </div>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <img className="modal-image" src={item.annotated_image_url} alt="Annotated road damage" />
        {item.vision_description && <div className="modal-vision">{item.vision_description}</div>}
        <ul className="damage-list">
          {item.damages.map((d) => (
            <li key={d.id} className="damage-row">
              <span className={`damage-tag ${d.damage_class.replace(/\s+/g, '-')}`}>{d.damage_class}</span>
              <span className="damage-conf">{(d.confidence * 100).toFixed(0)}%</span>
              <span className="damage-box">({d.bbox_x1},{d.bbox_y1})–({d.bbox_x2},{d.bbox_y2})</span>
              <span className="damage-model">{d.model_version}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
