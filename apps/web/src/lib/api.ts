import { getToken } from './erpApi'
import type { BBox, DamageReportItem, Frame, UploadItem } from './types'

export class ApiConfigError extends Error {}
export class ApiHttpError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function getApiBase(): string {
  const base = import.meta.env.VITE_API_BASE
  if (!base) {
    throw new ApiConfigError(
      'VITE_API_BASE is not set. Copy .env.example to .env and configure the backend URL.',
    )
  }
  return base.replace(/\/$/, '')
}

function filenameFor(frame: Frame): string {
  const idx = String(frame.index).padStart(6, '0')
  return `img_${idx}_${frame.capturedAt}.jpg`
}

function frameToUploadItem(frame: Frame, filename: string): UploadItem {
  return {
    filename,
    latitude: frame.fix?.lat ?? null,
    longitude: frame.fix?.lng ?? null,
    captured_at: new Date(frame.capturedAt).toISOString(),
    heading: frame.fix?.heading ?? null,
    altitude: frame.fix?.altitude ?? null,
    gps_accuracy: frame.fix?.accuracy ?? null,
  }
}

/** POST /api/v1/images/batch — multipart/form-data with `files` (repeated) + `items_json`. */
export async function uploadBatch(frames: Frame[], signal?: AbortSignal): Promise<void> {
  const url = `${getApiBase()}/api/v1/images/batch`
  const form = new FormData()
  const items: UploadItem[] = []
  for (const f of frames) {
    const name = filenameFor(f)
    form.append('files', f.blob, name)
    items.push(frameToUploadItem(f, name))
  }
  form.append('items_json', JSON.stringify(items))

  const res = await fetch(url, { method: 'POST', body: form, signal })
  if (!res.ok) {
    throw new ApiHttpError(res.status, `Upload failed: HTTP ${res.status}`)
  }
}

/** GET /api/v1/images/damage-report?lon_min&lat_min&lon_max&lat_max[&source][&inspection_id] */
export async function fetchDamageReport(
  bbox: BBox,
  signal?: AbortSignal,
  opts?: { source?: string; inspectionId?: string; dateFrom?: string; dateTo?: string },
): Promise<DamageReportItem[]> {
  const url = new URL(`${getApiBase()}/api/v1/images/damage-report`)
  url.searchParams.set('lon_min', String(bbox.lonMin))
  url.searchParams.set('lat_min', String(bbox.latMin))
  url.searchParams.set('lon_max', String(bbox.lonMax))
  url.searchParams.set('lat_max', String(bbox.latMax))
  if (opts?.source) url.searchParams.set('source', opts.source)
  if (opts?.inspectionId) url.searchParams.set('inspection_id', opts.inspectionId)
  if (opts?.dateFrom) url.searchParams.set('date_from', opts.dateFrom)
  if (opts?.dateTo) url.searchParams.set('date_to', opts.dateTo)
  const headers = new Headers()
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const res = await fetch(url.toString(), { signal, headers })
  if (!res.ok) {
    throw new ApiHttpError(res.status, `Damage report failed: HTTP ${res.status}`)
  }
  return (await res.json()) as DamageReportItem[]
}
