import { t, tPlural } from '@/i18n'
import '@/i18n/en/mcp'

export const ROBINHOOD_MCP_URL = 'https://agent.robinhood.com/mcp/trading'
export const ROBINHOOD_HELP_URL =
  'https://robinhood.com/us/en/support/articles/agentic-trading-overview/#ConnectyourAIagent'
export const BASE_MCP_URL = 'https://mcp.base.org'
export const BASE_HELP_URL = 'https://docs.base.org/agents'

export type McpTransport = 'streamable_http' | 'sse' | 'stdio'

export interface McpServerConfig {
  name: string
  transport: McpTransport
  url: string | null
  command: string | null
  args: string[]
  env: Record<string, string>
  headers: Record<string, string>
  oauth: boolean
  tool_timeout_seconds: number
}

export interface McpServerStatus {
  name: string
  transport?: McpTransport
  url?: string | null
  oauth?: boolean
  authenticated?: boolean
  connected?: boolean
  tools?: string[]
}

export interface McpConfigResponse {
  mcp?: {
    enabled?: boolean
    servers?: McpServerConfig[]
  }
}

export interface McpStatusResponse {
  enabled?: boolean
  servers?: McpServerStatus[]
}

export interface McpWorkspace {
  enabled: boolean
  servers: McpServerConfig[]
  statusByName: Record<string, McpServerStatus>
}

export interface McpServerDraft {
  originalName: string | null
  name: string
  transport: McpTransport
  url: string
  command: string
  args: string
  env: Record<string, string>
  headers: string
  oauth: boolean
  timeout: string
}

export interface McpDraftErrors {
  name?: string
  url?: string
  command?: string
  headers?: string
  timeout?: string
}

export type McpServerTone = 'connected' | 'authorization' | 'paused' | 'offline' | 'unavailable'

export interface McpServerPresentation {
  tone: McpServerTone
  label: string
  detail: string
  toolCount: number
}

export interface PartnerPresentation {
  tone: 'connected' | 'authorization' | 'paused' | 'ready' | 'unavailable'
  label: string
  detail: string
  tools: string
  action: string
}

export type PartnerKey = 'rh' | 'base'

export function normalizeWorkspace(
  config: McpConfigResponse | null | undefined,
  status: McpStatusResponse | null | undefined,
): McpWorkspace {
  const servers = Array.isArray(config?.mcp?.servers) ? config.mcp.servers : []
  const statusByName = Object.fromEntries(
    (Array.isArray(status?.servers) ? status.servers : []).map((entry) => [entry.name, entry]),
  )
  return {
    enabled: Boolean(config?.mcp?.enabled),
    servers,
    statusByName,
  }
}

export function createServerDraft(
  server?: Partial<McpServerConfig> & { originalName?: string | null },
): McpServerDraft {
  return {
    originalName: server?.originalName ?? null,
    name: server?.name ?? '',
    transport: server?.transport ?? 'streamable_http',
    url: server?.url ?? '',
    command: server?.command ?? '',
    args: server?.args?.join(' ') ?? '',
    env: server?.env ?? {},
    headers: JSON.stringify(server?.headers ?? {}, null, 2),
    oauth: Boolean(server?.oauth),
    timeout: String(server?.tool_timeout_seconds ?? 30),
  }
}

export function validateServerDraft(
  draft: McpServerDraft,
  servers: McpServerConfig[],
): McpDraftErrors {
  const errors: McpDraftErrors = {}
  const name = draft.name.trim()
  if (!name) errors.name = t('mcp.errorName')
  else if (!/^[a-zA-Z0-9._-]+$/.test(name)) {
    errors.name = t('mcp.errorNameCharset')
  } else if (servers.some((server) => server.name === name && server.name !== draft.originalName)) {
    errors.name = t('mcp.errorNameTaken')
  }

  if (draft.transport === 'stdio') {
    if (!draft.command.trim()) errors.command = t('mcp.errorCommand')
  } else {
    try {
      const url = new URL(draft.url.trim())
      if (!['http:', 'https:'].includes(url.protocol)) {
        errors.url = t('mcp.errorUrlScheme')
      }
    } catch {
      errors.url = t('mcp.errorUrl')
    }
  }

  try {
    const headers = JSON.parse(draft.headers || '{}') as unknown
    if (!headers || Array.isArray(headers) || typeof headers !== 'object') {
      errors.headers = t('mcp.errorHeadersObject')
    } else if (Object.values(headers).some((value) => typeof value !== 'string')) {
      errors.headers = t('mcp.errorHeaderValues')
    }
  } catch {
    errors.headers = t('mcp.errorHeadersJson')
  }

  const timeout = Number(draft.timeout)
  if (!Number.isFinite(timeout) || timeout < 1 || timeout > 600) {
    errors.timeout = t('mcp.errorTimeout')
  }
  return errors
}

export function serverFromDraft(draft: McpServerDraft): McpServerConfig {
  const stdio = draft.transport === 'stdio'
  return {
    name: draft.name.trim(),
    transport: draft.transport,
    command: stdio ? draft.command.trim() : null,
    args: stdio ? draft.args.trim().split(/\s+/).filter(Boolean) : [],
    url: stdio ? null : draft.url.trim(),
    env: draft.env,
    headers: stdio ? {} : (JSON.parse(draft.headers || '{}') as Record<string, string>),
    oauth: draft.transport === 'streamable_http' && draft.oauth,
    tool_timeout_seconds: Number(draft.timeout) || 30,
  }
}

export function transportLabel(transport: McpTransport): string {
  if (transport === 'streamable_http') return t('mcp.transportStreamableHttp')
  if (transport === 'sse') return t('mcp.transportSse')
  return t('mcp.transportStdio')
}

export function serverDetail(server: McpServerConfig): string {
  if (server.transport !== 'stdio') return server.url || t('mcp.detailIncomplete')
  return [server.command, ...server.args].filter(Boolean).join(' ') || t('mcp.detailIncomplete')
}

export function serverPresentation(
  server: McpServerConfig,
  status: McpServerStatus | undefined,
  enabled: boolean,
  statusAvailable = true,
): McpServerPresentation {
  const toolCount = status?.tools?.length ?? 0
  if (!enabled)
    return {
      tone: 'paused',
      label: t('mcp.statePaused'),
      detail: t('mcp.statePausedDetail'),
      toolCount,
    }
  if (!statusAvailable) {
    return {
      tone: 'unavailable',
      label: t('mcp.stateUnavailable'),
      detail: t('mcp.stateUnavailableDetail'),
      toolCount,
    }
  }
  if (status?.connected) {
    return {
      tone: 'connected',
      label: t('mcp.stateConnected'),
      detail: tPlural('mcp.stateConnectedDetail', toolCount),
      toolCount,
    }
  }
  if (server.oauth && !status?.authenticated) {
    return {
      tone: 'authorization',
      label: t('mcp.stateAuthorization'),
      detail: t('mcp.stateAuthorizationDetail'),
      toolCount,
    }
  }
  return {
    tone: 'offline',
    label: t('mcp.stateOffline'),
    detail: t('mcp.stateOfflineDetail'),
    toolCount,
  }
}

const PARTNER_URLS: Record<PartnerKey, string> = {
  rh: ROBINHOOD_MCP_URL,
  base: BASE_MCP_URL,
}

const PARTNER_I18N_KEYS: Record<PartnerKey, string> = {
  rh: 'rh',
  base: 'base',
}

/**
 * Generalized partner presentation replacing the Robinhood-only version.
 * Takes a *key* (``'rh'`` or ``'base'``) and looks up the matching i18n
 * prefix (``mcp.rh*`` or ``mcp.base*``) and server URL.
 */
export function partnerPresentation(
  key: PartnerKey,
  servers: McpServerConfig[],
  statusByName: Record<string, McpServerStatus>,
  enabled: boolean,
  statusAvailable = true,
): PartnerPresentation {
  const partnerUrl = PARTNER_URLS[key]
  const k = PARTNER_I18N_KEYS[key]
  const server = servers.find((entry) => entry.url === partnerUrl)
  const readyLabel = t(`mcp.${k}Ready`)
  const readyDetail = t(`mcp.${k}ReadyDetail`)
  const readyTools = t(`mcp.${k}ReadyTools`)
  const readyAction = t(`mcp.${k}ReadyAction`)
  const reviewAction = t(`mcp.${k}ReviewAction`)

  if (!server) {
    return { tone: 'ready', label: readyLabel, detail: readyDetail, tools: readyTools, action: readyAction }
  }
  const status = statusByName[server.name]
  const toolCount = status?.tools?.length ?? 0
  if (!enabled) {
    return { tone: 'paused', label: t(`mcp.${k}Paused`), detail: t(`mcp.${k}PausedDetail`), tools: t(`mcp.${k}PausedTools`), action: reviewAction }
  }
  if (!statusAvailable) {
    return { tone: 'unavailable', label: t(`mcp.${k}Unavailable`), detail: t(`mcp.${k}UnavailableDetail`), tools: t(`mcp.${k}UnavailableTools`), action: reviewAction }
  }
  if (status?.connected) {
    return { tone: 'connected', label: t(`mcp.${k}Connected`), detail: tPlural(`mcp.${k}ConnectedDetail`, toolCount), tools: t(`mcp.${k}ConnectedTools`, { count: toolCount }), action: t(`mcp.${k}ManageAction`) }
  }
  if (server.oauth && !status?.authenticated) {
    return { tone: 'authorization', label: t(`mcp.${k}OauthRequired`), detail: t(`mcp.${k}OauthDetail`), tools: t(`mcp.${k}OauthTools`), action: t(`mcp.${k}AuthorizeAction`) }
  }
  return { tone: 'ready', label: readyLabel, detail: t(`mcp.${k}SavedDetail`), tools: readyTools, action: reviewAction }
}

/** Convenience wrapper: robinhoodPresentation calls partnerPresentation with key 'rh'. */
export function robinhoodPresentation(
  servers: McpServerConfig[],
  statusByName: Record<string, McpServerStatus>,
  enabled: boolean,
  statusAvailable = true,
): PartnerPresentation {
  return partnerPresentation('rh', servers, statusByName, enabled, statusAvailable)
}

/** Convenience wrapper for the Base MCP partner card. */
export function basePresentation(
  servers: McpServerConfig[],
  statusByName: Record<string, McpServerStatus>,
  enabled: boolean,
  statusAvailable = true,
): PartnerPresentation {
  return partnerPresentation('base', servers, statusByName, enabled, statusAvailable)
}
