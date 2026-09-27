import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

/** Bottom fade for a scroller whose last visible row would otherwise be cut
 *  mid-line by whatever sits under it -- the model picker's list ends at the
 *  effort block's top border, and a half row against that border read as
 *  overlap. Apply while `useMoreBelow` is true and nowhere else: a fade on a
 *  list that is fully visible dims a real last row for nothing. */
export const MORE_BELOW_MASK =
  '[mask-image:linear-gradient(to_bottom,black_calc(100%-20px),transparent)] [-webkit-mask-image:linear-gradient(to_bottom,black_calc(100%-20px),transparent)]'

/** Keeps one set of listeners on whatever element `ref` holds. Re-run on every
 *  render of the caller: the scroller may mount later than the hook -- ChatPane
 *  calls these at its top level while the list lives in a portal that opens on
 *  click -- so binding once on mount to a ref that was still null would leave
 *  the list with no listeners for as long as it is open. `attach` returns the
 *  matching detach; `onGone` runs when the element has gone away. */
function useFollowElement(
  ref: RefObject<HTMLElement | null>,
  target: (el: HTMLElement) => HTMLElement | null,
  attach: (el: HTMLElement) => () => void,
  onGone: () => void,
) {
  const attached = useRef<HTMLElement | null>(null)
  const detach = useRef<(() => void) | null>(null)
  const sync = useCallback(() => {
    const el = ref.current ? target(ref.current) : null
    if (el === attached.current) return
    detach.current?.()
    detach.current = null
    attached.current = el
    if (!el) {
      onGone()
      return
    }
    detach.current = attach(el)
  }, [ref, target, attach, onGone])
  useEffect(sync)
  useEffect(() => () => detach.current?.(), [])
}

const self = (el: HTMLElement) => el
const parentOf = (el: HTMLElement) => el.parentElement

/** True while `ref`'s scroller has content below its viewport. Re-measured on
 *  its own scroll, on a size change of the scroller, and on every render of
 *  the caller (a filter narrowing the list changes the content height without
 *  either of those events). */
export function useMoreBelow(ref: RefObject<HTMLElement | null>): boolean {
  const [more, setMore] = useState(false)
  const measure = useCallback(() => {
    const el = ref.current
    if (el) setMore(el.scrollTop + el.clientHeight < el.scrollHeight - 1)
  }, [ref])
  useEffect(measure)
  const attach = useCallback((el: HTMLElement) => {
    el.addEventListener('scroll', measure, { passive: true })
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(el)
    return () => {
      el.removeEventListener('scroll', measure)
      observer?.disconnect()
    }
  }, [measure])
  // The scroller went away (the portal closed): a fade has nothing to fade.
  const gone = useCallback(() => setMore(false), [])
  useFollowElement(ref, self, attach, gone)
  return more
}

/** Height, in px, that trims `ref`'s scroller to whole `[role="option"]` rows
 *  when it has to scroll, or undefined when every row already fits. Under the
 *  fade a row cut mid-line read as a broken row rather than as "more below"
 *  (a short split pane shows one on every open), so the list gives up the
 *  partial row and ends on a whole one; the fade then only softens the edge.
 *  Measured with the trim lifted, so a pane that grows back gets its rows
 *  back, and re-measured on every render of the caller plus any size change
 *  of the scroller's parent, which is what decides how tall it may be. */
export function useWholeRows(ref: RefObject<HTMLElement | null>): number | undefined {
  const [trim, setTrim] = useState<number | undefined>(undefined)
  const measure = useCallback(() => {
    const el = ref.current
    if (!el) return
    const pinned = el.style.maxHeight
    el.style.maxHeight = ''
    const available = el.clientHeight
    const overflows = el.scrollHeight > available + 1
    el.style.maxHeight = pinned
    if (!overflows || available <= 0) {
      setTrim(undefined)
      return
    }
    const top = el.getBoundingClientRect().top - el.scrollTop
    let fit = 0
    for (const row of el.querySelectorAll<HTMLElement>('[role="option"]')) {
      const bottom = row.getBoundingClientRect().bottom - top
      if (bottom > available) break
      fit = bottom
    }
    // Under one whole row there is nothing sensible to trim to.
    setTrim(fit > 0 && fit < available ? Math.floor(fit) : undefined)
  }, [ref])
  useEffect(measure)
  const attach = useCallback((parent: HTMLElement) => {
    if (typeof ResizeObserver === 'undefined') return () => {}
    const observer = new ResizeObserver(measure)
    observer.observe(parent)
    return () => observer.disconnect()
  }, [measure])
  const gone = useCallback(() => setTrim(undefined), [])
  useFollowElement(ref, parentOf, attach, gone)
  return trim
}
