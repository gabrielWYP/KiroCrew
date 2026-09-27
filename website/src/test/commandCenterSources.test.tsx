// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { createTestStore, renderHookWithProviders, renderWithProviders } from './helpers'
import { api } from '../api/client'
import { useCommandCenter } from '../pages/chat/command-center/useCommandCenter'
import TaskDashboardFrame, { TASK_DASHBOARD_SANDBOX } from '../pages/chat/command-center/TaskDashboardFrame'
import type { Artifact } from '../types'

const artifact = (slug: string, session: string, content = '<h1>Task-specific map</h1>'): Artifact => ({
  slug, session_key: session, name: slug, kind: 'html', source: 'chat', description: '', tags: ['task-dashboard'],
  version: 1, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', content,
})
function store() {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'root', title: 'Conductor', messages: 0, running: true },
    { key: 'builder', created_by: 'root', messages: 0, running: false },
    { key: 'unrelated', messages: 0, running: true },
  ] } })
}

describe('task dashboard sources and containment', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'sessionWorkProjection').mockResolvedValue({ value: { items: [] } })
    vi.spyOn(api, 'artifacts').mockResolvedValue({ artifacts: [artifact('own', 'dashboard:root'), artifact('child', 'builder'), artifact('foreign', 'unrelated'), artifact('unbound', '')] })
  })

  it('admits arbitrary authored layouts from the owning team, not similarly tagged unrelated sessions', async () => {
    const { result } = renderHookWithProviders(() => useCommandCenter('root'), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.dashboards.map(a => a.slug)).toEqual(['own', 'child'])
    expect(result.current.nodes.map(n => n.slot)).toEqual(['root', 'builder'])
    expect(result.current.relevant).toBe(true)
    expect(result.current.stale).toBe(false)
  })

  it('never falls back to the whole fleet while the owning slot is unresolved', () => {
    const { result } = renderHookWithProviders(() => useCommandCenter(null), { store: store() })
    expect(result.current.nodes).toEqual([])
    expect(result.current.attention).toEqual([])
    expect(result.current.dashboards).toEqual([])
    expect(api.pendingQuestions).not.toHaveBeenCalled()
  })

  it('reads the fleet only when explicitly requested, without per-session work queries', async () => {
    const { result } = renderHookWithProviders(() => useCommandCenter(null, true, 'fleet'), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.nodes.map(n => n.slot)).toEqual(['root', 'builder', 'unrelated'])
    expect(result.current.dashboards.map(a => a.slug)).toEqual(['own', 'child', 'foreign'])
    expect(api.sessionWorkProjection).not.toHaveBeenCalled()
    expect(result.current.stale).toBe(false)
    expect(result.current.updatedAt).toBeGreaterThan(0)
  })

  it('reports unavailable sources instead of claiming no questions are pending', async () => {
    vi.mocked(api.pendingQuestions).mockRejectedValue(new Error('offline'))
    const { result } = renderHookWithProviders(() => useCommandCenter('root'), { store: store() })
    await waitFor(() => expect(result.current.stale).toBe(true))
    expect(result.current.updatedAt).toBe(0)
  })

  it('renders model HTML through the sandbox document service without a privileged bridge', async () => {
    const modelHtml = '<article><h1>Dependency map</h1><script>window.taskSpecific=true</script></article>'
    vi.spyOn(api, 'artifact').mockResolvedValue(artifact('own', 'root', modelHtml))
    const mint = vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/token' })
    const { container } = renderWithProviders(<TaskDashboardFrame artifact={artifact('own', 'root')} active />)
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe(TASK_DASHBOARD_SANDBOX)
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(mint).toHaveBeenCalledWith(expect.stringContaining('Dependency map'))
    expect(mint.mock.calls[0][0]).toContain("connect-src 'none'")
    expect(mint.mock.calls[0][0]).toContain("script-src 'none'")
    expect(mint.mock.calls[0][0]).not.toContain('window.taskSpecific')
    expect(container.querySelector('script')).toBeNull()
  })
})
