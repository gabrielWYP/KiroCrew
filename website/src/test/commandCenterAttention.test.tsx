import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { createTestStore, renderWithProviders } from './helpers'
import AttentionCard from '../pages/chat/command-center/AttentionCard'
import { api, ApiError } from '../api/client'
import * as transport from '../chat-core/transport/sendTurn'
import type { AttentionItem } from '../pages/chat/command-center/model'

const approval: AttentionItem = { id: 'approval:child:r1', kind: 'approval', slot: 'child', native: true, approvalMode: 'normal', approval: { id: 'r1', tool: 'shell', tool_input: 'git status' } }
const question: AttentionItem = { id: 'question:q1', kind: 'question', slot: 'child', question: { slot: 'child', ask_id: 'q1', questions: [{ question: 'Which scope?', options: [{ label: 'Backend' }, { label: 'Frontend' }] }] } }

describe('task dashboard input routing', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('does not auto-approve in Normal mode and routes one explicit click to the exact slot/request', async () => {
    const approve = vi.spyOn(api, 'approveChatSlot').mockResolvedValue({ ok: true })
    const resolve = vi.spyOn(api, 'resolveApproval').mockResolvedValue({ ok: true })
    renderWithProviders(<AttentionCard item={approval} title="Backend worker" />)
    expect(approve).not.toHaveBeenCalled()
    expect(screen.getByText(/Approval required/)).toHaveTextContent('Normal')
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('child', 'approved', { request_id: 'r1' }))
    expect(resolve).not.toHaveBeenCalled()
    expect(await screen.findByText('Your response was recorded.')).toBeInTheDocument()
  })

  it.each([true, false])('rejects only this request without changing permission mode (native=%s)', async (native) => {
    const resolve = vi.spyOn(api, 'resolveApproval').mockResolvedValue({ ok: true })
    const approve = vi.spyOn(api, 'approveChatSlot').mockResolvedValue({ ok: true })
    renderWithProviders(<AttentionCard item={{ ...approval, native }} title="Worker" />)
    fireEvent.click(screen.getByRole('button', { name: 'Reject once' }))
    if (native) {
      await waitFor(() => expect(approve).toHaveBeenCalledWith('child', 'rejected_once', { request_id: 'r1' }))
      expect(resolve).not.toHaveBeenCalled()
    } else {
      await waitFor(() => expect(resolve).toHaveBeenCalledWith('r1', 'reject_once'))
      expect(approve).not.toHaveBeenCalled()
    }
  })

  it('keeps the complete approval command verbatim in a focusable scrolling preview', () => {
    const command = JSON.stringify({ command: `git show --format=%H ${'very-long-ref-'.repeat(30)}\\n` })
    const { container } = renderWithProviders(<AttentionCard item={{ ...approval, approval: { ...approval.approval!, tool_input: command } }} title="Worker" />)
    const preview = container.querySelector('pre')!
    expect(preview.textContent).toBe(command)
    expect(preview).toHaveAttribute('tabindex', '0')
    expect(preview).toHaveClass('overflow-auto', 'whitespace-pre', 'break-normal')
    expect(screen.getByText(/Approval required/)).toHaveTextContent('Permission mode: Normal')
  })

  it('locks a double click and keeps a failed approval retryable', async () => {
    let reject!: (error: Error) => void
    const approve = vi.spyOn(api, 'approveChatSlot').mockReturnValue(new Promise((_resolve, no) => { reject = no }))
    renderWithProviders(<AttentionCard item={approval} title="Worker" />)
    const button = screen.getByRole('button', { name: 'Approve once' })
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(approve).toHaveBeenCalledTimes(1))
    await act(async () => reject(new Error('Offline')))
    expect(await screen.findByText('Offline')).toBeInTheDocument()
    await waitFor(() => expect(button).toBeEnabled())
    expect(screen.queryByText('Your response was recorded.')).not.toBeInTheDocument()
  })

  it('answers a blocking question by ID, never by sending to the active chat', async () => {
    const answer = vi.spyOn(api, 'answerQuestion').mockResolvedValue({ ok: true })
    const send = vi.spyOn(transport, 'sendTurn')
    renderWithProviders(<AttentionCard item={question} title="Worker" />)
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await waitFor(() => expect(answer).toHaveBeenCalledWith('q1', { 'Which scope?': 'Backend' }))
    expect(send).not.toHaveBeenCalled()
  })

  it('retires an expired approval without claiming it was approved or offering another submission', async () => {
    vi.spyOn(api, 'approveChatSlot').mockRejectedValue(new ApiError(404, 'expired'))
    renderWithProviders(<AttentionCard item={approval} title="Worker" />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument())
    expect(screen.queryByText('Your response was recorded.')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open session' })).toHaveAttribute('href', '/chat?sid=child')
  })

  it('preserves the selected answer after an uncertain direct delivery', async () => {
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'unknown', body: {} })
    const dismiss = vi.spyOn(api, 'dismissQuestionCard')
    renderWithProviders(<AttentionCard item={{ ...question, question: { ...question.question!, ask_id: undefined, card_id: 'card', native: true } }} title="Worker" />)
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await screen.findByText(/Delivery is uncertain/)
    expect(send).toHaveBeenCalledWith({ slot: 'child', message: 'Which scope?: Backend' })
    expect(dismiss).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Submit' })).toBeEnabled()
    expect(send).toHaveBeenCalledTimes(1)
  })

  it.each([true, false])('steers only a native answer while its own slot is busy (native=%s)', async (native) => {
    const initial = createTestStore().getState()
    const store = createTestStore({ ...initial, chat: { ...initial.chat, activeSlot: 'child', slotRunning: true } })
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'queued', body: {} })
    vi.spyOn(api, 'dismissQuestionCard').mockResolvedValue({ ok: true })
    renderWithProviders(<AttentionCard item={{ ...question, question: { ...question.question!, ask_id: undefined, card_id: 'card', native } }} title="Worker" />, { store })
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await screen.findByText('Your response was recorded.')
    expect(send).toHaveBeenCalledWith({ slot: 'child', message: 'Which scope?: Backend', ...(native ? { steer: true } : {}) })
  })

  it('never sends the answer twice when retiring its already-delivered card fails', async () => {
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'dispatched', body: { ok: true } })
    vi.spyOn(api, 'dismissQuestionCard').mockRejectedValue(new Error('Retirement failed'))
    renderWithProviders(<AttentionCard item={{ ...question, question: { ...question.question!, ask_id: undefined, card_id: 'card' } }} title="Worker" />)
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await screen.findByText('Retirement failed')
    expect(screen.queryByRole('button', { name: 'Submit' })).not.toBeInTheDocument()
    expect(send).toHaveBeenCalledTimes(1)
  })
})
