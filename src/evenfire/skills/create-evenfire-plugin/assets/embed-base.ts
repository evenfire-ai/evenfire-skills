// Inside the Evenfire Desktop app the plugin UI is served under
//   /api/v1/sandbox-ui/<namespace>/<recipe>/view/
// and the page can be opened at a nested route (…/view/items/42). Import this
// module first in your entry file (never as an inline <script>: the embed's
// CSP only runs scripts from your own origin).

const EMBED_VIEW_RE = /^(.*\/sandbox-ui\/[^/]+\/[^/]+\/view)(?:\/|$)/

/** The "…/view" prefix when embedded; undefined in local development. */
export const embedBasename: string | undefined = EMBED_VIEW_RE.exec(window.location.pathname)?.[1]

// Pin the document base to the embed root so relative URLs (API calls, event
// streams, lazy-loaded chunks) resolve the same at any route depth.
if (embedBasename) {
  let base = document.querySelector('base')
  if (!base) {
    base = document.createElement('base')
    document.head.prepend(base)
  }
  base.setAttribute('href', `${window.location.origin}${embedBasename}/`)
}

/**
 * Build a URL for your own backend. Pass a path WITHOUT a leading slash
 * ("api/items"): a leading slash would escape the embed prefix.
 */
export function apiUrl(path: string): string {
  return new URL(path.replace(/^\/+/, ''), document.baseURI).href
}

/**
 * fetch() for your backend: asks for JSON so platform status answers come back
 * as JSON instead of an HTML page, and recovers once from an expired embed
 * session (401 sandbox_ui_session_invalid / sandbox_ui_session_required).
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  if (!headers.has('accept')) headers.set('accept', 'application/json')
  const send = () => fetch(apiUrl(path), { ...init, headers, credentials: 'same-origin' })
  const res = await send()
  if (res.status !== 401) return res
  const body = await res.clone().json().catch(() => null)
  const code = (body as { error?: string } | null)?.error
  if (code !== 'sandbox_ui_session_invalid' && code !== 'sandbox_ui_session_required') return res
  const clerum = (window as unknown as { clerum?: { requestSessionRefresh?: () => Promise<void> } }).clerum
  if (typeof clerum?.requestSessionRefresh !== 'function') return res
  try {
    await clerum.requestSessionRefresh() // at most once per 30 s per view
  } catch {
    return res
  }
  return send()
}
