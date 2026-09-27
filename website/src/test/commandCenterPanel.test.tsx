import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import * as transport from '../chat-core/transport/sendTurn'
import { createTestStore, renderWithProviders } from './helpers'
import CommandCenterPanel from '../pages/chat/command-center/CommandCenterPanel'
import CommandCenterDock from '../pages/chat/command-center/CommandCenterDock'
import { REQUEST_PUBLISHED_VIEW } from '../pages/chat/command-center/commandCenter.prompt'

function taskStore() {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'root', title: 'Conductor', messages: 0, running: true },
    { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: false },
  ] } })
}

describe('task dashboard host controls', () => {
  afterEach(() => vi.unstubAllGlobals())
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    // happy-dom has no layout; establish the panel width that selects tabs.
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(480)
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'sessionWorkProjection').mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'dispatched', status: 'blocked', summary: 'Needs evidence' },
    ] } })
    vi.spyOn(api, 'artifacts').mockResolvedValue({ artifacts: [] })
  })

  it('shows accepted progress and requests an authored dashboard only after a click', async () => {
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'queued', body: {} })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    expect(await screen.findByText('Accepted contract')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '1')
    expect(screen.getByRole('progressbar')).toHaveAttribute('max', '2')
    expect(send).not.toHaveBeenCalled()
    expect(screen.getByText('Permission mode: Normal')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create published view' }))
    expect(await screen.findByText('Published view requested — it will appear here when ready.')).toBeInTheDocument()
    expect(send).toHaveBeenCalledWith({ slot: 'root', message: REQUEST_PUBLISHED_VIEW, steer: 'auto' })
    expect(screen.getByRole('button', { name: 'Create published view' })).toBeDisabled()
  })

  it('keeps a refused design request retryable without claiming a dashboard exists', async () => {
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'refused', reason: 'Session is unavailable', body: {} })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(screen.getByRole('button', { name: 'Create published view' }))
    expect(await screen.findByText('Session is unavailable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create published view' })).toBeEnabled()
    expect(send).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Published view requested — it will appear here when ready.')).not.toBeInTheDocument()
  })

  it('shows failed runs as alerts without a draft-destroying agent hand-off', async () => {
    vi.mocked(api.workflowRuns).mockResolvedValue({ runs: [{ run_id: 'failed', name: 'Validation', session_key: 'dashboard:root', status: 'failed', error: 'Runner unavailable', last_log: 'Preparing checks' }] })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    expect(await screen.findByRole('alert')).toHaveTextContent('Runner unavailable')
    expect(screen.getByText('Preparing checks')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
  })

  it('keeps every section accessible with compact labels in a 320px panel', async () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(320)
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) { this.callback([{ target, contentRect: { width: 320 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    await screen.findByText('Accepted contract')
    const approvals = screen.getByRole('radio', { name: /Approvals/ })
    expect(approvals).not.toHaveTextContent('Approvals')
    fireEvent.click(approvals)
    expect(approvals).toHaveTextContent('Approvals')
    await waitFor(() => expect(screen.getByRole('radio', { name: /Overview/ })).not.toHaveTextContent('Overview'))
  })

  it('keeps a worker answer draft while switching between questions and approvals', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'dashboard:worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    await screen.findByText('Accepted contract')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    fireEvent.click(await screen.findByText('Stable API'))
    fireEvent.click(screen.getByRole('radio', { name: /Approvals/ }))
    expect(screen.getByRole('button', { name: 'Approve once' })).toBeVisible()
    expect(screen.getByText(/Approval required/)).toHaveTextContent('Normal')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    expect(screen.getByRole('button', { name: 'Submit' })).toBeEnabled()
    fireEvent.click(screen.getByRole('radio', { name: /Overview/ }))
    expect(screen.getByText('Accepted contract')).toBeVisible()
  })

  it.each(['disconnected', 'query failure'])('announces a stale dock through the shared error notice (%s)', async (failure) => {
    const store = taskStore()
    const state = store.getState()
    if (failure === 'query failure') vi.mocked(api.approvals).mockRejectedValue(new Error('Offline'))
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, {
      store: failure === 'disconnected' ? createTestStore({ ...state, dashboard: { ...state.dashboard, connected: false } }) : store,
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('Some sources are unavailable.')
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
  })

  it('keeps the dock entrance when collapsed and opens the existing panel', async () => {
    const open = vi.fn()
    renderWithProviders(<CommandCenterDock slot="root" onOpen={open} />, { store: taskStore() })
    await waitFor(() => expect(api.sessionWorkProjection).toHaveBeenCalledWith('root'))
    fireEvent.click(screen.getByRole('button', { name: 'Collapse summary' }))
    expect(screen.getByRole('button', { name: 'Expand summary' })).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'Dynamic Dashboard' }))
    expect(open).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Expand summary' }))
    expect(screen.getByRole('button', { name: 'Collapse summary' })).toHaveAttribute('aria-expanded', 'true')
  })
})
