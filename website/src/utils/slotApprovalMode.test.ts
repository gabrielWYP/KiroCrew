import { describe, it, expect } from 'vitest'
import { slotApprovalMode } from './slotApprovalMode'

describe('slotApprovalMode', () => {
  it('shows yolo over any slot trust', () => {
    expect(slotApprovalMode('yolo', { trust: true, trust_scope: 'crew:x' })).toBe('yolo')
  })

  it('shows trust for the session flag', () => {
    expect(slotApprovalMode('normal', { trust: true })).toBe('trust')
  })

  it('shows trust for a live scoped grant', () => {
    expect(slotApprovalMode('normal', { trust: false, trust_scope: 'crew:slack-radar:autoapprove' })).toBe('trust')
  })

  it('shows normal once the scoped grant lapses', () => {
    expect(slotApprovalMode('normal', { trust: false, trust_scope: '' })).toBe('normal')
  })

  it('shows trust_reads below trust', () => {
    expect(slotApprovalMode(undefined, { trust_reads: true })).toBe('trust_reads')
  })

  it('shows normal with no slot', () => {
    expect(slotApprovalMode(undefined, undefined)).toBe('normal')
  })
})
