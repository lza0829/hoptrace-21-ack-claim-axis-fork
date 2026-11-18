async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || JSON.stringify(body)
    } catch (e) {
      const text = await res.text().catch(() => '')
      if (text) detail = text
    }
    const err = new Error(detail)
    err.status = res.status
    throw err
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id) => req(`/api/waybills/${encodeURIComponent(id)}/timeline`),
  advanceAck: (id, ackSeq) =>
    req(`/api/waybills/${encodeURIComponent(id)}/ack`, {
      method: 'POST',
      body: JSON.stringify({ ack_seq: ackSeq }),
    }),
  forkProofs: (waybillId) =>
    req(waybillId ? `/api/ack-fork-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/ack-fork-proofs'),
  pendingHops: (waybillId) =>
    req(waybillId ? `/api/pending-hops?waybill_id=${encodeURIComponent(waybillId)}` : '/api/pending-hops'),
  ackOrderingDemo: () => req('/api/ack-demo/ordering'),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
}
