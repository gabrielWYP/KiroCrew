import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Activity, ArrowUpRight, LayoutDashboard, MessageSquare, ShieldCheck } from 'lucide-react'
import { PanelSectionHeader, Btn } from '../../../components/ui'
import SegmentedControl from '../../../components/SegmentedControl'
import SimpleSelect from '../../../components/SimpleSelect'
import ErrorNotice from '../../../components/ErrorNotice'
import { fmtDateTime, fmtNumber } from '../../../i18n/format'
import { useCommandCenter } from './useCommandCenter'
import TaskDashboardFrame from './TaskDashboardFrame'
import AttentionCard from './AttentionCard'
import { APPROVAL_MODE_KEYS, runTitle, type RunState } from './model'
import { sendTurn } from '../../../chat-core/transport/sendTurn'
import { useContainerWidth } from '../../../hooks/useContainerWidth'
import { REQUEST_PUBLISHED_VIEW } from './commandCenter.prompt'

const STATE_KEYS: Record<RunState, string> = {
  running: 'commandCenter.running', idle: 'commandCenter.idle', done: 'commandCenter.done',
  blocked: 'commandCenter.blocked', waiting: 'commandCenter.waiting', needs_input: 'commandCenter.needs_input', stopped: 'commandCenter.stopped',
}

export default function CommandCenterPanel({ slot, active }: { slot: string | null; active: boolean }) {
  const { t } = useTranslation()
  const data = useCommandCenter(slot, active)
  const [panelRef, panelWidth] = useContainerWidth<HTMLDivElement>()
  const [selected, setSelected] = useState<string | null>(null)
  const artifact = data.dashboards.find(a => a.slug === selected) || data.dashboards[0]
  const [section, setSection] = useState<'dashboard' | 'attention' | 'approvals'>('dashboard')
  const requestDashboard = useMutation({
    retry: false,
    mutationFn: async () => {
      if (!slot) return
      const receipt = await sendTurn({ slot, message: REQUEST_PUBLISHED_VIEW, steer: 'auto' })
      if (receipt.status !== 'dispatched' && receipt.status !== 'queued') {
        throw new Error(receipt.status === 'refused' ? receipt.reason || t('commandCenter.send_refused') : t('commandCenter.send_unknown'))
      }
    },
  })
  return <div ref={panelRef} className="h-full flex flex-col min-w-0 bg-bg text-text" data-testid="command-center-panel">
    <header className="shrink-0 p-3 border-b border-border space-y-3">
      <div className="flex gap-2 items-center flex-wrap"><LayoutDashboard size={17} className="text-accent" /><h2 className="font-semibold text-sm">{t('commandCenter.title')}</h2>
        <span className="ml-auto text-[11px] text-muted inline-flex items-center gap-1"><ShieldCheck size={12} />{t('commandCenter.permission_mode', { mode: t(APPROVAL_MODE_KEYS[data.approvalMode]) })}</span>
      </div>
      <p className="text-[12px] text-muted">{t('commandCenter.description')}</p>
      <div className="grid grid-cols-3 gap-2" aria-live="polite">
        {([['commandCenter.running', data.running], ['commandCenter.blocked', data.blocked], ['commandCenter.approvals', data.approvalCount]] as const).map(([key, count]) => <div key={key} className="rounded-lg border border-border bg-card p-2">
          <div className="font-mono text-lg font-semibold">{fmtNumber(count)}</div><div className="text-[11px] text-muted">{t(key)}</div>
        </div>)}
      </div>
      {data.progress && <div className="space-y-1">
        <p className="text-[12px] text-muted">{t('commandCenter.progress', { done: fmtNumber(data.progress.done), total: fmtNumber(data.progress.total) })}</p>
        <progress className="w-full h-1.5 accent-accent" value={data.progress.done} max={data.progress.total} aria-label={t('commandCenter.progress_label')} />
      </div>}
      <SegmentedControl value={section} onChange={setSection} compact={panelWidth !== null && panelWidth < 380} layoutId={`task-dashboard-section-${slot}`} segments={[
        { key: 'dashboard', label: t('commandCenter.dashboard'), icon: <LayoutDashboard size={14} /> },
        { key: 'attention', label: t('commandCenter.needs_input'), icon: <MessageSquare size={14} />, count: data.attention.length - data.approvalCount },
        { key: 'approvals', label: t('commandCenter.approvals'), icon: <ShieldCheck size={14} />, count: data.approvalCount },
      ]} />
      {/* No hand-off: pending QuestionCard answer drafts remain mounted below. */}
      {data.stale && <ErrorNotice message={t('commandCenter.stale')} />}
    </header>
    <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-4" hidden={section !== 'dashboard'}>
      {data.dashboards.length > 1 && <label className="flex flex-col gap-1 text-[12px] text-muted">{t('commandCenter.dashboard')}
        <SimpleSelect options={data.dashboards.map(a => a.slug)} optionLabels={data.dashboards.map(a => a.name)} value={artifact?.slug || ''} onChange={setSelected} />
      </label>}
      {artifact ? <TaskDashboardFrame key={artifact.slug} artifact={artifact} active={active && section === 'dashboard'} />
        : <div className="rounded-lg border border-border bg-card p-4 space-y-2">
          <LayoutDashboard size={24} className="text-accent" />
          <h3 className="text-sm font-semibold">{t('commandCenter.adaptive_title')}</h3>
          <p className="text-sm text-muted leading-relaxed">{t('commandCenter.adaptive_description')}</p>
          <Btn disabled={!slot || requestDashboard.isPending || requestDashboard.isSuccess} onClick={() => requestDashboard.mutate()}>{t('commandCenter.request_design')}</Btn>
          {requestDashboard.isSuccess && <p role="status" className="text-sm text-muted">{t('commandCenter.design_requested')}</p>}
          {/* No hand-off: pending QuestionCard answer drafts remain mounted below. */}
          <ErrorNotice message={requestDashboard.error?.message} />
        </div>}
      <PanelSectionHeader label={t('commandCenter.live_activity')} />
      {data.loading && <p role="status" className="text-sm text-muted">{t('commandCenter.loading')}</p>}
      {data.nodes.map(node => <div key={node.id} className="flex items-start gap-2 py-2 border-b border-border last:border-0">
        <Activity size={14} className={node.state === 'blocked' ? 'text-warn mt-1 shrink-0' : 'text-muted mt-1 shrink-0'} />
        <div className="flex-1 min-w-0"><p className="text-[13px] font-medium break-words">{runTitle(node)}</p>
          {node.detail && <p className="text-[12px] text-muted break-words line-clamp-2">{node.detail}</p>}
          {/* No hand-off: pending QuestionCard answer drafts remain mounted in this panel. */}
          <ErrorNotice message={node.error} />
          <p className="text-[11px] text-muted mt-1">{t(STATE_KEYS[node.state])}</p>
        </div>
        <Link to={`/chat?sid=${encodeURIComponent(node.slot)}`} aria-label={t('commandCenter.open_session')} className="text-accent p-1"><ArrowUpRight size={14} /></Link>
      </div>)}
      {data.workItems.map(item => <div key={item.item_id} className="text-[13px] border-l-2 border-border pl-3">
        <p>{item.title}</p><p className="text-muted text-[12px]">{t(STATE_KEYS[item.state])}{item.summary ? ` · ${item.summary}` : ''}</p>
      </div>)}
      <p className="flex gap-1.5 items-start text-[11px] text-muted"><ShieldCheck size={13} className="shrink-0" />{t('commandCenter.contained')}</p>
    </div>
    <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-3" hidden={section === 'dashboard'}>
      {!data.stale && !data.attention.some(a => section === 'approvals' ? a.kind === 'approval' : a.kind !== 'approval') && <p className="text-sm text-muted p-3">{t('commandCenter.no_input')}</p>}
      {data.attention.map(item => <div key={item.id} hidden={section === 'approvals' ? item.kind !== 'approval' : item.kind === 'approval'}>
        <AttentionCard item={item} title={runTitle(data.nodes.find(n => n.id === `session:${item.slot}`)!)} />
      </div>)}
    </div>
    <footer className="shrink-0 border-t border-border px-3 py-2 text-[11px] text-muted">
      {data.updatedAt > 0 ? t('commandCenter.updated', { time: fmtDateTime(data.updatedAt) }) : t('commandCenter.loading')}
    </footer>
  </div>
}
