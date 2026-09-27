import { describe, expect, it } from 'vitest'
import { buildCommandCenter, runTitle, scopedSlots, type CommandCenterSources } from '../pages/chat/command-center/model'
import type { ChatSlot, SubagentActivity } from '../types'

const slot = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({ key, messages: 0, running: false, ...extra })
const agent = (id: string, extra: Partial<SubagentActivity> = {}): SubagentActivity => ({ id, task: id, agent: 'worker', status: 'running', streaming: '', lastTool: '', startedAt: 0, elapsed: 0, ...extra })
const sources = (extra: Partial<CommandCenterSources> = {}): CommandCenterSources => ({
  slots: [slot('root'), slot('child', { created_by: 'root' }), slot('other')],
  root: 'root', subagents: {}, workflows: [], questions: [], approvals: [], ...extra,
})

describe('command center projection', () => {
  it('keeps opaque IDs for routing and gives unnamed runs human-readable labels', () => {
    const model = buildCommandCenter(sources({
      slots: [slot('opaque-session', { title: 'opaque-session' })], root: 'opaque-session',
      subagents: { 'opaque-session': { agent: agent('opaque-agent') } },
      workflows: [{ run_id: 'opaque-workflow', session_key: 'dashboard:opaque-session' }],
    }))
    expect(model.nodes.map(runTitle)).toEqual(['Session 1', 'Worker task 2', 'Workflow 3'])
    expect(model.nodes.map(n => n.ref)).toEqual(['opaque-session', 'opaque-agent', 'opaque-workflow'])
  })
  it('includes descendants, never unrelated sessions or their questions', () => {
    const model = buildCommandCenter(sources({
      slots: [slot('root'), slot('child', { created_by: 'root' }), slot('grandchild', { created_by: 'child' }), slot('other')],
      questions: [{ slot: 'child', card_id: 'q1', questions: [] }, { slot: 'other', card_id: 'q2', questions: [] }],
    }))
    expect(model.nodes.map(n => n.id)).toEqual(['session:root', 'session:child', 'session:grandchild'])
    expect(model.attention.map(a => a.id)).toEqual(['question:child:q1'])
  })
  it('does not loop on cyclic or orphaned creator edges', () => {
    expect(scopedSlots([slot('a', { created_by: 'b' }), slot('b', { created_by: 'a' }), slot('orphan', { created_by: 'missing' })], 'a').map(s => s.key)).toEqual(['a', 'b'])
    expect(scopedSlots([slot('a')], 'missing')).toEqual([])
    expect(scopedSlots([slot('a')], null)).toHaveLength(1)
  })
  it('keeps run identities distinct, including native subagents', () => {
    const model = buildCommandCenter(sources({
      subagents: { root: { same: agent('same'), native: agent('native:1') } },
      workflows: [{ run_id: 'same', session_key: 'dashboard:root', status: 'running' }],
    }))
    expect(new Set(model.nodes.map(n => n.id)).size).toBe(model.nodes.length)
    expect(model.nodes.find(n => n.id === 'subagent:native:1')?.canMessage).toBe(false)
    expect(model.nodes.find(n => n.id === 'workflow:same')?.canMessage).toBe(false)
  })
  it('never interprets an idle session or worker done report as accepted work', () => {
    const model = buildCommandCenter(sources({ work: { items: [
      { item_id: 'a', title: 'A', state: 'open', status: 'done' },
      { item_id: 'b', title: 'B', state: 'accepted', status: 'done' },
    ] } }))
    expect(model.progress).toEqual({ done: 1, total: 2, source: 'work' })
    expect(model.nodes[0].state).toBe('idle')
  })
  it('separates external blockers from actionable human questions', () => {
    const model = buildCommandCenter(sources({ work: { items: [
      { item_id: 'b', title: 'Blocked', state: 'open', status: 'blocked', summary: 'Upstream outage' },
      { item_id: 'q', title: 'Decision', state: 'open', status: 'question', summary: 'Ask conductor' },
    ] } }))
    expect(model.attention).toEqual([])
    expect(model.workItems.map(i => i.state)).toEqual(['blocked', 'waiting'])
    expect(model.blocked).toBe(1)
  })
  it('deduplicates approvals exposed by both feeds and preserves exact request IDs', () => {
    const model = buildCommandCenter(sources({
      slots: [slot('root', { pending_approval: true, pending_approval_info: { request_id: 'req', tool: 'shell', tool_input: 'pwd', tool_kind: 'execute' } })],
      approvals: [{ id: 'req', slot: 'root', tool: 'shell' }],
    }))
    expect(model.attention).toHaveLength(1)
    expect(model.attention[0]).toMatchObject({ id: 'approval:root:req', slot: 'root', approvalMode: 'normal', approval: { id: 'req' } })
  })
  it('keeps colliding native approval IDs isolated by their owning session', () => {
    const approval = { request_id: 'same', tool: 'shell', tool_input: 'pwd', tool_kind: 'execute' }
    const model = buildCommandCenter(sources({ slots: [slot('root', { pending_approval: true, pending_approval_info: approval }), slot('child', { created_by: 'root', pending_approval: true, pending_approval_info: approval, trust_reads: true })] }))
    expect(model.attention.map(a => a.id)).toEqual(['approval:root:same', 'approval:child:same'])
    expect(model.attention.map(a => a.approvalMode)).toEqual(['normal', 'trust_reads'])
  })
  it('counts only explicit failed and stalled runs as blocked', () => {
    const model = buildCommandCenter(sources({ subagents: { root: {
      running: agent('running'), error: agent('error', { status: 'error', error: 'failed' }),
      stalled: agent('stalled', { stalled: true }), done: agent('done', { status: 'done' }),
    } } }))
    expect(model.blocked).toBe(2)
    expect(model.completed).toBe(1)
    expect(model.running).toBe(1)
  })
  it('keeps backend errors separate from ordinary activity details', () => {
    const model = buildCommandCenter(sources({
      subagents: { root: { failed: agent('failed', { status: 'error', error: 'Worker failed', lastTool: 'Reading files' }) } },
      workflows: [{ run_id: 'failed', session_key: 'dashboard:root', status: 'failed', error: 'Workflow failed', last_log: 'Preparing output' }],
    }))
    expect(model.nodes.find(n => n.id === 'subagent:failed')).toMatchObject({ error: 'Worker failed', detail: 'Reading files' })
    expect(model.nodes.find(n => n.id === 'workflow:failed')).toMatchObject({ error: 'Workflow failed', detail: 'Preparing output' })
  })
  it('retains workflow completion and ignores unowned runs', () => {
    const model = buildCommandCenter(sources({ workflows: [
      { run_id: 'done', session_key: 'dashboard:child', status: 'finished' },
      { run_id: 'unowned', session_key: '', status: 'finished' },
      { run_id: 'other', session_key: 'dashboard:other', status: 'failed' },
    ] }))
    expect(model.nodes.filter(n => n.kind === 'workflow').map(n => n.ref)).toEqual(['done'])
    expect(model.completed).toBe(1)
  })
  it('uses a real todo denominator when there is no work board', () => {
    const model = buildCommandCenter(sources({ slots: [slot('root', { todo: { description: 'Plan', tasks: [{ id: '1', text: 'Build', completed: true }, { id: '2', text: 'Test', completed: false }], total: 2, completed: 1, current: 'Test' } })] }))
    expect(model.progress).toEqual({ done: 1, total: 2, source: 'todo' })
  })
  it('shows missing approval/question payloads without inventing an executable action', () => {
    const model = buildCommandCenter(sources({ slots: [slot('root', { needs_input: true, pending_approval: true })] }))
    expect(model.nodes[0].state).toBe('needs_input')
    expect(model.attention[0]).toMatchObject({ kind: 'approval', approvalMode: 'normal' })
    expect(model.attention[0].approval).toBeUndefined()
  })
  it('does not hide an approval with missing details behind a question from the same session', () => {
    const model = buildCommandCenter(sources({
      slots: [slot('root', { pending_approval: true })],
      questions: [{ slot: 'root', card_id: 'card', questions: [] }],
    }))
    expect(model.attention.map(a => a.kind)).toEqual(['question', 'approval'])
  })
  it('keeps identical question-card IDs in different sessions distinct', () => {
    const model = buildCommandCenter(sources({ questions: [
      { slot: 'root', card_id: 'card', questions: [] },
      { slot: 'child', card_id: 'card', questions: [] },
    ] }))
    expect(model.attention.map(a => a.id)).toEqual(['question:root:card', 'question:child:card'])
  })
})
