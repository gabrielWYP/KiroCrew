import DOMPurify from 'dompurify'

/** Task text may be private. An opaque origin stops host reads, not outbound
 * navigation or script-created DNS lookups. Keep model layout, never model code.
 * Native HTML (details, anchors, CSS and SVG) remains freely authorable; new
 * evidence arrives as a new artifact revision, not an in-frame network fetch. */
export function dashboardDocument(html: string, themeVars: Record<string, string>, mode: 'dark' | 'light'): string {
  const sanitized = DOMPurify.sanitize(html, {
    WHOLE_DOCUMENT: true,
    ADD_TAGS: ['style'],
    FORBID_TAGS: ['script', 'link', 'meta', 'base', 'iframe', 'frame', 'object', 'embed', 'applet', 'template', 'noscript', 'form', 'input', 'button', 'textarea', 'select', 'animate', 'set'],
  })
  const doc = new DOMParser().parseFromString(sanitized, 'text/html')
  // CSP doesn't govern navigation. Retain only in-document links and remove
  // resource-hint markup before any model bytes reach a live browsing context.
  for (const element of doc.querySelectorAll('[href], [xlink\\:href]')) {
    for (const attr of ['href', 'xlink:href']) {
      const value = element.getAttribute(attr)
      if (value && !value.startsWith('#')) element.removeAttribute(attr)
    }
  }
  const csp = doc.createElement('meta')
  csp.httpEquiv = 'Content-Security-Policy'
  csp.content = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none';"
  doc.head.prepend(csp)
  const style = doc.createElement('style')
  // readThemeVars already sanitizes the host's computed CSS values.
  style.textContent = `:root{${Object.entries(themeVars).map(([key, value]) => `${key}:${value}`).join(';')};color-scheme:${mode}}body{margin:0;padding:16px;background:var(--bg);color:var(--text);font-family:system-ui,sans-serif}`
  csp.after(style)
  return '<!doctype html>' + doc.documentElement.outerHTML
}
