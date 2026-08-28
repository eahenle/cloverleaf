import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from '../api'
import type { ServiceStatus, SystemStatus as SystemStatusResponse } from '../types'

const frontendService: ServiceStatus = {
  id: 'frontend',
  name: 'Frontend',
  condition: 'healthy',
  detail: 'The Cloverleaf interface is running',
}

export function SystemStatus() {
  const [status, setStatus] = useState<SystemStatusResponse | null>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      setStatus(await api.status())
      setError('')
    } catch (reason) {
      setStatus(null)
      setError(reason instanceof Error ? reason.message : 'Backend status is unavailable.')
    } finally {
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 5_000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const services = status
    ? [frontendService, ...status.services]
    : [
        frontendService,
        {
          id: 'backend',
          name: 'Backend API',
          condition: 'unavailable' as const,
          detail: error || 'Waiting for the backend status endpoint…',
        },
      ]
  const degradedCount = services.filter((service) => service.condition !== 'healthy').length

  return (
    <div className="panel-body system-status" role="tabpanel" aria-label="System status">
      <div className="status-toolbar">
        <div>
          <strong>{degradedCount === 0 ? 'All systems operational' : `${degradedCount} service${degradedCount === 1 ? '' : 's'} need attention`}</strong>
          <span>{status ? `Checked ${new Date(status.checked_at).toLocaleTimeString()}` : 'Checking services…'}</span>
        </div>
        <button type="button" onClick={() => void refresh()} disabled={refreshing} aria-label="Refresh system status">
          <RefreshCw size={12} className={refreshing ? 'spin' : ''} /> Refresh
        </button>
      </div>
      <div className="service-list" aria-live="polite">
        {services.map((service) => (
          <article className={`service-status ${service.condition}`} key={service.id}>
            <span className={`status-dot ${service.condition === 'healthy' ? 'success' : service.condition === 'degraded' ? 'compiling' : 'error'}`} />
            <div>
              <strong>{service.name}</strong>
              <span>{service.detail}</span>
            </div>
            <small>{service.condition}</small>
          </article>
        ))}
      </div>
    </div>
  )
}
