/**
 * The model picker's list fades its bottom edge only while rows sit below the
 * viewport, and trims itself to whole rows when it has to scroll. jsdom lays
 * nothing out, so these tests hand the scroller its geometry by hand and check
 * what the two hooks conclude from it -- including the case that bit ChatPane,
 * where the scroller mounts in a portal AFTER the hook first ran.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { useRef } from 'react'
import { MORE_BELOW_MASK, useMoreBelow, useWholeRows } from '../hooks/useMoreBelow'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/** Give a jsdom element the geometry a laid-out scroller would report. */
function shape(el: HTMLElement, geometry: { clientHeight: number; scrollHeight: number; top?: number }) {
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => geometry.clientHeight })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => geometry.scrollHeight })
  el.getBoundingClientRect = () => ({ top: geometry.top ?? 0, bottom: (geometry.top ?? 0) + geometry.clientHeight } as DOMRect)
}

function rowAt(el: HTMLElement, top: number, height: number) {
  el.getBoundingClientRect = () => ({ top, bottom: top + height } as DOMRect)
}

function Scroller({ open, rows = 0 }: { open: boolean; rows?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const more = useMoreBelow(ref)
  const trim = useWholeRows(ref)
  return (
    <div data-testid="host">
      {open && (
        <div ref={ref} data-testid="list" role="listbox" className={more ? MORE_BELOW_MASK : ''} style={{ maxHeight: trim }}>
          {Array.from({ length: rows }, (_, i) => <button key={i} role="option" aria-selected={false}>{`row ${i}`}</button>)}
        </div>
      )}
    </div>
  )
}

describe('useMoreBelow', () => {
  it('fades only while content sits below the viewport, and follows the scroll', () => {
    const view = render(<Scroller open />)
    const list = view.getByTestId('list')
    const geometry = { clientHeight: 100, scrollHeight: 250 }
    shape(list, geometry)
    // A re-render re-measures (the filter case): the fade appears.
    view.rerender(<Scroller open rows={1} />)
    expect(list.className).toBe(MORE_BELOW_MASK)
    // Scrolled to the bottom, nothing is below: the fade lifts on the scroll event.
    list.scrollTop = 150
    fireEvent.scroll(list)
    expect(list.className).toBe('')
  })

  it('attaches to a scroller that mounts after the hook first ran', () => {
    // ChatPane's case: the hook runs at the top level, the list is a portal
    // that opens later. The listeners must land on the element that exists
    // when it exists, not on the null the ref held at mount.
    const view = render(<Scroller open={false} />)
    view.rerender(<Scroller open />)
    const list = view.getByTestId('list')
    shape(list, { clientHeight: 100, scrollHeight: 250 })
    fireEvent.scroll(list)
    expect(list.className).toBe(MORE_BELOW_MASK)
    list.scrollTop = 150
    fireEvent.scroll(list)
    expect(list.className).toBe('')
    // Closing the portal drops the fade with it.
    view.rerender(<Scroller open={false} />)
    view.rerender(<Scroller open />)
    expect(view.getByTestId('list').className).toBe('')
  })
})

describe('useWholeRows', () => {
  it('trims a scrolling list to the last row that fits, and leaves a fitting list alone', () => {
    const view = render(<Scroller open rows={4} />)
    const list = view.getByTestId('list')
    // Four 40px rows in a 100px viewport: two fit whole, the third is cut.
    const geometry = { clientHeight: 100, scrollHeight: 160, top: 0 }
    shape(list, geometry)
    list.querySelectorAll<HTMLElement>('[role="option"]').forEach((row, i) => rowAt(row, i * 40, 40))
    view.rerender(<Scroller open rows={4} />)
    expect(list.style.maxHeight).toBe('80px')
    // Everything fits: no trim.
    geometry.clientHeight = 200
    geometry.scrollHeight = 160
    view.rerender(<Scroller open rows={4} />)
    expect(list.style.maxHeight).toBe('')
  })

  it('does not trim when not even one whole row fits', () => {
    const view = render(<Scroller open rows={3} />)
    const list = view.getByTestId('list')
    shape(list, { clientHeight: 30, scrollHeight: 120, top: 0 })
    list.querySelectorAll<HTMLElement>('[role="option"]').forEach((row, i) => rowAt(row, i * 40, 40))
    view.rerender(<Scroller open rows={3} />)
    expect(list.style.maxHeight).toBe('')
  })

  it('re-measures when the scroller\'s parent changes size', () => {
    let resize: (() => void) | undefined
    vi.stubGlobal('ResizeObserver', class {
      constructor(cb: () => void) { resize = cb }
      observe() {}
      disconnect() {}
    })
    const view = render(<Scroller open rows={4} />)
    const list = view.getByTestId('list')
    const geometry = { clientHeight: 100, scrollHeight: 160, top: 0 }
    shape(list, geometry)
    list.querySelectorAll<HTMLElement>('[role="option"]').forEach((row, i) => rowAt(row, i * 40, 40))
    act(() => resize?.())
    expect(list.style.maxHeight).toBe('80px')
    // The pane grows: the third row comes back.
    geometry.clientHeight = 130
    act(() => resize?.())
    expect(list.style.maxHeight).toBe('120px')
  })
})
