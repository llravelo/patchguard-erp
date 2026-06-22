import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

// Glides a marker smoothly between successive target positions instead of teleporting.
// Each new target eases over `durationMs` using ease-out cubic.
function useSmoothedPosition(
  target: [number, number] | null,
  durationMs = 1200,
): [number, number] | null {
  const [pos, setPos] = useState<[number, number] | null>(target)
  const fromRef = useRef<[number, number] | null>(target)

  useEffect(() => {
    if (!target) {
      setPos(null)
      fromRef.current = null
      return
    }
    if (!fromRef.current) {
      setPos(target)
      fromRef.current = target
      return
    }
    const from = fromRef.current
    const to = target
    const t0 = performance.now()
    let frame = 0

    function step(now: number) {
      const t = Math.min(1, (now - t0) / durationMs)
      const eased = 1 - Math.pow(1 - t, 3)
      setPos([
        from[0] + (to[0] - from[0]) * eased,
        from[1] + (to[1] - from[1]) * eased,
      ])
      if (t < 1) {
        frame = requestAnimationFrame(step)
      } else {
        fromRef.current = to
      }
    }
    frame = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame)
  }, [target, durationMs])

  return pos
}
import {
  MapContainer,
  Marker,
  Polyline,
  Popup,
  TileLayer,
  useMap,
  useMapEvents,
} from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { fetchDamageReport } from '../lib/api'
import type { BBox, DamageClass, DamageReportItem } from '../lib/types'
import { PointPicker } from '../components/PointPicker'
import { AgentView } from '../components/AgentView'
import { ReportPanel } from '../components/ReportPanel'
import { openJobEvents, type JobEvent } from '../lib/jobApi'
import { createInspection, finishInspection, generateReport, type ReportOut } from '../lib/erpApi'

const FALLBACK_CENTER: [number, number] = [-33.882, 151.197]
const FALLBACK_ZOOM = 14

const DAMAGE_KEYS: Record<DamageClass, string> = {
  'longitudinal crack': 'longitudinal',
  'transverse crack':   'transverse',
  'alligator crack':    'alligator',
  'Pothole':            'pothole',
  'other corruption':   'other',
}
const DAMAGE_INITIAL: Record<DamageClass, string> = {
  'longitudinal crack': 'L',
  'transverse crack':   'T',
  'alligator crack':    'A',
  'Pothole':            'P',
  'other corruption':   'O',
}

function bboxFromMap(map: L.Map): BBox {
  const b = map.getBounds()
  return {
    lonMin: b.getWest(),
    latMin: b.getSouth(),
    lonMax: b.getEast(),
    latMax: b.getNorth(),
  }
}

function damageIconFor(item: DamageReportItem) {
  const top = item.damages.length
    ? [...item.damages].sort((a, b) => b.confidence - a.confidence)[0]!
    : null
  const cls = top ? (DAMAGE_KEYS[top.damage_class] ?? 'other') : 'other'
  const letter = top ? (DAMAGE_INITIAL[top.damage_class] ?? '?') : '·'
  const count = item.damages.length
  return L.divIcon({
    className: 'dmg-pin',
    html: `<div class="dmg-pin-inner ${cls}"><span>${count > 1 ? count : letter}</span></div>`,
    iconSize: [22, 22],
    iconAnchor: [11, 22],
    popupAnchor: [0, -20],
  })
}

const LIVE_ICON = L.divIcon({
  className: 'live-agent-marker',
  html: '<div class="live-agent-pulse"></div><div class="live-agent-dot"></div>',
  iconSize: [24, 24],
  iconAnchor: [12, 12],
})

const FLAG_START = L.divIcon({
  className: 'flag-marker',
  html: '<div class="flag start">A</div>',
  iconSize: [28, 28],
  iconAnchor: [14, 28],
})
const FLAG_END = L.divIcon({
  className: 'flag-marker',
  html: '<div class="flag end">B</div>',
  iconSize: [28, 28],
  iconAnchor: [14, 28],
})

function ViewportWatcher({
  onBBox,
  fitBounds,
  followPos,
  onMapClick,
}: {
  onBBox: (bbox: BBox) => void
  fitBounds: [number, number][] | null
  followPos: [number, number] | null
  onMapClick: (latlng: [number, number]) => void
}) {
  const map = useMap()
  useEffect(() => {
    map.invalidateSize()
    onBBox(bboxFromMap(map))
    const onResize = () => map.invalidateSize()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (fitBounds && fitBounds.length > 1) {
      const b = L.latLngBounds(fitBounds.map(([la, ln]) => L.latLng(la, ln)))
      map.fitBounds(b, { padding: [50, 50] })
    }
  }, [fitBounds, map])
  useEffect(() => {
    if (!followPos) return
    const px = map.latLngToContainerPoint(L.latLng(followPos[0], followPos[1]))
    const size = map.getSize()
    const margin = 80
    if (px.x < margin || px.x > size.x - margin || px.y < margin || px.y > size.y - margin) {
      map.panTo(L.latLng(followPos[0], followPos[1]), { animate: true })
    }
  }, [followPos, map])
  useMapEvents({
    moveend: () => onBBox(bboxFromMap(map)),
    zoomend: () => onBBox(bboxFromMap(map)),
    click: (e) => onMapClick([e.latlng.lat, e.latlng.lng]),
  })
  return null
}

type Stats = Record<DamageClass, number>
const EMPTY_STATS: Stats = {
  'longitudinal crack': 0,
  'transverse crack':   0,
  'alligator crack':    0,
  'Pothole':            0,
  'other corruption':   0,
}

type LiveJobState = {
  state: 'idle' | 'planning' | 'running' | 'done' | 'failed'
  label: string
  totalWaypoints: number
  currentIndex: number
  captured: number
  skipped: number
}
const IDLE_JOB: LiveJobState = {
  state: 'idle', label: '', totalWaypoints: 0, currentIndex: 0, captured: 0, skipped: 0,
}

type LatestImage = {
  index: number
  url: string
  damages: number
  description: string | null
}

type LivePreview = {
  index: number
  latLng: [number, number]
  b64: string
}

export function DashboardPage() {
  const [items, setItems] = useState<DamageReportItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<DamageReportItem | null>(null)
  const [start, setStart] = useState<[number, number] | null>(null)
  const [end, setEnd] = useState<[number, number] | null>(null)
  const [plannedRoute, setPlannedRoute] = useState<[number, number][]>([])
  const [traveled, setTraveled] = useState<[number, number][]>([])
  const [agentPos, setAgentPos] = useState<[number, number] | null>(null)
  const [routeToFit, setRouteToFit] = useState<[number, number][] | null>(null)
  const [job, setJob] = useState<LiveJobState>(IDLE_JOB)
  const [latest, setLatest] = useState<LatestImage | null>(null)
  const [livePreview, setLivePreview] = useState<LivePreview | null>(null)
  const [report, setReport] = useState<ReportOut | null>(null)
  const [reportBusy, setReportBusy] = useState(false)
  const currentJobIdRef = useRef<string | null>(null)
  const [feed, setFeed] = useState<{ id: number; cls: string; text: string }[]>([])
  const lastBBoxRef = useRef<BBox | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const bboxTimerRef = useRef<number | null>(null)
  const feedIdRef = useRef(0)
  const closeWsRef = useRef<(() => void) | null>(null)

  useEffect(() => () => closeWsRef.current?.(), [])

  const refetch = useCallback(async (bbox: BBox) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError(null)
    try {
      const data = await fetchDamageReport(bbox, controller.signal, { source: 'worker' })
      setItems(data)
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') return
      setError(err instanceof Error ? err.message : String(err))
      setItems([])
    } finally {
      setLoading(false)
    }
  }, [])

  const onBBox = useCallback(
    (bbox: BBox) => {
      lastBBoxRef.current = bbox
      if (bboxTimerRef.current) window.clearTimeout(bboxTimerRef.current)
      bboxTimerRef.current = window.setTimeout(() => refetch(bbox), 300)
    },
    [refetch],
  )

  useEffect(() => {
    return () => {
      abortRef.current?.abort()
      if (bboxTimerRef.current) window.clearTimeout(bboxTimerRef.current)
    }
  }, [])

  const stats = useMemo(() => {
    const s: Stats = { ...EMPTY_STATS }
    for (const it of items) for (const d of it.damages) s[d.damage_class] = (s[d.damage_class] ?? 0) + 1
    return s
  }, [items])
  const totalDamages = useMemo(() => Object.values(stats).reduce((a, b) => a + b, 0), [stats])

  function pushFeed(cls: string, text: string) {
    feedIdRef.current += 1
    setFeed((f) => [{ id: feedIdRef.current, cls, text }, ...f.slice(0, 49)])
  }
  const onLog = useCallback((cls: string, text: string) => pushFeed(cls, text), [])

  const onMapClick = useCallback(
    ([la, ln]: [number, number]) => {
      if (job.state === 'running' || job.state === 'planning') return
      if (!start) {
        setStart([la, ln])
        return
      }
      if (!end) {
        setEnd([la, ln])
        return
      }
      // Both already set — start over with this click as the new start.
      setStart([la, ln])
      setEnd(null)
      setPlannedRoute([])
      setTraveled([])
      setAgentPos(null)
      setRouteToFit(null)
      setJob(IDLE_JOB)
      setLatest(null)
    },
    [start, end, job.state],
  )

  const onClear = useCallback(() => {
    setStart(null); setEnd(null)
    setPlannedRoute([]); setTraveled([])
    setAgentPos(null); setRouteToFit(null)
    setJob(IDLE_JOB); setLatest(null); setLivePreview(null); setFeed([])
  }, [])

  function handleEvent(ev: JobEvent) {
    switch (ev.t) {
      case 'snapshot':
        pushFeed('t-tool', `snapshot · ${ev.state} · ${ev.total_waypoints} wp`)
        break
      case 'route':
        setPlannedRoute(ev.polyline)
        setRouteToFit(ev.polyline)
        setJob((j) => ({ ...j, state: 'running', totalWaypoints: ev.polyline.length }))
        pushFeed('t-route', `route · ${ev.polyline.length} points`)
        break
      case 'progress':
        setTraveled((prev) => [...prev, [ev.waypoint.lat, ev.waypoint.lng]])
        setAgentPos([ev.waypoint.lat, ev.waypoint.lng])
        setJob((j) => ({ ...j, state: 'running', currentIndex: ev.index + 1 }))
        if (ev.preview_b64) {
          setLivePreview({
            index: ev.index,
            latLng: [ev.waypoint.lat, ev.waypoint.lng],
            b64: ev.preview_b64,
          })
        }
        break
      case 'batch_uploaded':
        pushFeed('t-batch', `batch · ${ev.from_index}…${ev.to_index} (${ev.count})`)
        setJob((j) => ({ ...j, captured: j.captured + ev.count }))
        if (lastBBoxRef.current) refetch(lastBBoxRef.current)
        break
      case 'batch_failed':
        pushFeed('t-error', `batch failed · ${ev.from_index}…${ev.to_index} · HTTP ${ev.status}`)
        break
      case 'step_image':
        setLatest({
          index: ev.index,
          url: ev.image_url,
          damages: ev.damages,
          description: ev.vision_description,
        })
        if (ev.damages > 0) {
          pushFeed('t-batch', `#${ev.index + 1} · ${ev.damages} damage${ev.damages === 1 ? '' : 's'}${ev.vision_description ? ' · ' + ev.vision_description : ''}`)
        }
        break
      case 'waypoint_failed':
        pushFeed('t-error', `waypoint ${ev.index} skipped: ${ev.reason}`)
        setJob((j) => ({ ...j, skipped: j.skipped + 1 }))
        break
      case 'tool':
        pushFeed('t-tool', `tool · ${ev.name}`)
        break
      case 'done':
        pushFeed('t-done', `done · captured ${ev.captured} · skipped ${ev.skipped}`)
        setJob((j) => ({ ...j, state: 'done' }))
        if (lastBBoxRef.current) refetch(lastBBoxRef.current)
        if (currentJobIdRef.current) {
          finishInspection(currentJobIdRef.current, {
            status: 'done', captured: ev.captured, skipped: ev.skipped,
          }).catch((err) => console.warn('finishInspection failed', err))
        }
        break
    }
  }

  const onJobQueued = useCallback((jobId: string, label: string) => {
    setJob({ ...IDLE_JOB, state: 'planning', label })
    setTraveled([]); setAgentPos(null); setLatest(null); setLivePreview(null); setFeed([])
    setReport(null)
    currentJobIdRef.current = jobId
    pushFeed('t-tool', `→ planning: ${label}`)
    // Register the inspection in the ERP so uploads link to it and reports can be generated.
    if (start && end) {
      createInspection({ job_id: jobId, start, end })
        .catch((err) => pushFeed('t-error', `inspection register failed: ${err.message ?? err}`))
    }
    closeWsRef.current?.()
    closeWsRef.current = openJobEvents(jobId, handleEvent)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start, end])

  const onGenerateReport = useCallback(async () => {
    if (!currentJobIdRef.current) return
    setReportBusy(true)
    try {
      setReport(await generateReport(currentJobIdRef.current))
    } catch (err) {
      pushFeed('t-error', `report failed: ${err instanceof Error ? err.message : err}`)
    } finally {
      setReportBusy(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const progressPct = job.totalWaypoints
    ? Math.min(100, Math.round((job.currentIndex / job.totalWaypoints) * 100))
    : 0

  const smoothAgentPos = useSmoothedPosition(agentPos, 1200)

  // Polyline's leading edge tracks the smoothed dot so the line draws continuously
  // instead of jumping in waypoint-sized chunks.
  const livePolyline = useMemo<[number, number][]>(() => {
    if (!smoothAgentPos || traveled.length === 0) return traveled
    return [...traveled.slice(0, -1), smoothAgentPos]
  }, [traveled, smoothAgentPos])

  return (
    <div className="dashboard-shell">
      <aside className="sidebar">
        <div className="card card-tight">
          <div className="card-title">
            Agent's view
            <span className="badge">{livePreview || latest ? 'streaming' : 'idle'}</span>
          </div>
          <AgentView
            livePreviewB64={livePreview?.b64 ?? null}
            liveIndex={livePreview?.index ?? null}
            liveLatLng={livePreview?.latLng ?? null}
            total={job.totalWaypoints}
            annotated={latest ? {
              url: latest.url,
              damages: latest.damages,
              description: latest.description,
            } : null}
          />
        </div>

        <div className="card">
          <div className="card-title">
            Plan a survey
            <span className="badge">{job.state === 'running' ? 'in progress' : 'click 2 points'}</span>
          </div>
          <PointPicker
            start={start}
            end={end}
            isRunning={job.state === 'running' || job.state === 'planning'}
            onClear={onClear}
            onJobQueued={onJobQueued}
            onLog={onLog}
          />
        </div>

        <div className="card">
          <div className="card-title">
            Agent status
            <span className="badge">
              <span className={`dot ${job.state}`} />
              {job.state}
            </span>
          </div>
          <div className="status-row">
            <span className="k">Waypoints</span>
            <span className="v">{job.currentIndex} / {job.totalWaypoints || '—'}</span>
          </div>
          <div className="status-row">
            <span className="k">Captured · Skipped</span>
            <span className="v">{job.captured} · {job.skipped}</span>
          </div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
          {job.state === 'done' && (
            <button
              className="job-panel-submit"
              style={{ marginTop: 10 }}
              onClick={onGenerateReport}
              disabled={reportBusy}
              type="button"
            >
              {reportBusy ? 'Generating report…' : 'Generate report'}
            </button>
          )}
        </div>

        <div className="card">
          <div className="card-title">
            Damages on screen
            <span className="badge">{items.length} img · {totalDamages}</span>
          </div>
          <div className="chip-row">
            {(Object.entries(stats) as [DamageClass, number][]).map(([k, v]) => (
              <span key={k} className={`chip ${v === 0 ? 'empty' : ''}`}>
                <span className="chip-swatch" style={{ background: `var(--c-${DAMAGE_KEYS[k]})` }} />
                {k} <strong>{v}</strong>
              </span>
            ))}
          </div>
          {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
        </div>

        <div className="card" style={{ flex: 1, minHeight: 160 }}>
          <div className="card-title">
            Activity feed
            {loading && <span className="badge">loading</span>}
          </div>
          <div className="feed">
            {feed.length === 0 && <div className="feed-line">Click two points on the map and press <em>Start survey</em>.</div>}
            {feed.map((entry) => (
              <div key={entry.id} className={`feed-line ${entry.cls}`}>{entry.text}</div>
            ))}
          </div>
        </div>
      </aside>

      <div className="map-wrap">
        <MapContainer
          center={FALLBACK_CENTER}
          zoom={FALLBACK_ZOOM}
          scrollWheelZoom
          style={{ height: '100%', width: '100%' }}
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          />
          <ViewportWatcher
            onBBox={onBBox}
            fitBounds={routeToFit}
            followPos={agentPos}
            onMapClick={onMapClick}
          />
          {start && <Marker position={start} icon={FLAG_START} />}
          {end && <Marker position={end} icon={FLAG_END} />}
          {plannedRoute.length > 1 && (
            <Polyline positions={plannedRoute} pathOptions={{ color: '#475569', weight: 3, dashArray: '4 6' }} />
          )}
          {livePolyline.length > 1 && (
            <Polyline positions={livePolyline} pathOptions={{ color: '#3b82f6', weight: 4 }} />
          )}
          {smoothAgentPos && <Marker position={smoothAgentPos} icon={LIVE_ICON} zIndexOffset={2000} />}
          {items.filter((it) => it.damages.length > 0).map((it) => (
            <Marker
              key={it.image_id}
              position={[it.latitude, it.longitude]}
              icon={damageIconFor(it)}
              eventHandlers={{ click: () => setSelected(it) }}
            >
              <Popup>
                <div className="popup">
                  <strong>{it.damages.length} damage{it.damages.length === 1 ? '' : 's'}</strong>
                  <div className="popup-meta">{new Date(it.captured_at).toLocaleString()}</div>
                  {it.vision_description && <div className="popup-vision">"{it.vision_description}"</div>}
                  <button className="popup-btn" onClick={() => setSelected(it)}>View details</button>
                </div>
              </Popup>
            </Marker>
          ))}
        </MapContainer>
      </div>

      {selected && <DamageDetailModal item={selected} onClose={() => setSelected(null)} />}
      {report && <ReportPanel report={report} onClose={() => setReport(null)} />}
    </div>
  )
}

function DamageDetailModal({
  item, onClose,
}: { item: DamageReportItem; onClose: () => void }) {
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
        {item.vision_description && (
          <div className="modal-vision">{item.vision_description}</div>
        )}
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
