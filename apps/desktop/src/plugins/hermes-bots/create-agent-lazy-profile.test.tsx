/**
 * New Bot creates its profile LAZILY. Two contracts hang off that:
 *
 *  - MCP OAuth / API-key credentials must be configurable DURING creation
 *    rather than forcing create-then-edit, so the setup button materializes
 *    the profile through `ensureAgentCreated()` on its first action. The
 *    creation is single-flighted and idempotent: whichever door fires first
 *    (a setup click, the Capabilities tab, or Create Bot itself) is the only
 *    `profiles.create` the dialog ever sends.
 *  - `reset()` has to restore the SAME clone source the dialog mounted with.
 *    It restored '__none__' instead: the first open cloned the main profile,
 *    every later open after a create or cancel silently started fresh.
 */

import type * as HermesSdk from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as DataModule from './data'
import { translateBots } from './i18n-test-helper'
import type { RosterRow } from './types'

interface CapabilitiesViewProps {
  fixedConnection?: string
  fixedProfile?: string
}

const mocks = vi.hoisted(() => ({
  active: { connectionId: null as string | null, profile: 'default' },
  connections: vi.fn(async (): Promise<Awaited<ReturnType<typeof HermesSdk.host.connections>>> => []),
  completeMcpOAuth: vi.fn(
    async (
      _options: Parameters<typeof HermesSdk.host.completeMcpOAuth>[0]
    ): Promise<Awaited<ReturnType<typeof HermesSdk.host.completeMcpOAuth>>> => ({
      flow_id: 'synthetic-flow',
      server_name: 'linear',
      status: 'approved',
      authorization_url: null,
      error: null
    })
  ),
  createCanonicalChat: vi.fn(async () => 'session-1'),
  deleteProfile: vi.fn(async (): Promise<void> => undefined),
  draftImage: { value: null as string | null },
  gateway: { current: null as ReturnType<typeof HermesSdk.host.getGateway> },
  host: null as null | typeof HermesSdk.host,
  /** Flipped off to model a desktop build that predates the live surface. */
  hasCapabilitiesView: { value: true },
  notify: vi.fn(),
  notifyError: vi.fn(),
  request: vi.fn(),
  requestProfile: vi.fn(async (_route: unknown, _method: string, _params?: unknown): Promise<unknown> => ({})),
  saveBotMeta: vi.fn(),
  skillsView: [] as CapabilitiesViewProps[]
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const original = await importOriginal<typeof HermesSdk>()
  const { atom } = await import('nanostores')

  const CapabilitiesViewStub = (props: CapabilitiesViewProps) => {
    mocks.skillsView.push(props)

    return null
  }

  // Builds that route `fixedConnection` get the live Capabilities tab for
  // remote targets too, pinned to the target machine's backend.
  CapabilitiesViewStub.supportsFixedConnection = true

  const host: typeof HermesSdk.host = {
    ...original.host,
    connections: mocks.connections,
    completeMcpOAuth: mocks.completeMcpOAuth,
    deleteProfile: mocks.deleteProfile,
    getGateway: () => mocks.gateway.current,
    notify: mocks.notify,
    notifyError: mocks.notifyError,
    request: mocks.request,
    requestProfile: mocks.requestProfile as typeof HermesSdk.host.requestProfile,
    state: {
      ...original.host.state,
      connectionId: { ...atom<string | null>(null), get: () => mocks.active.connectionId },
      profile: { ...atom('default'), get: () => mocks.active.profile }
    }
  }

  mocks.host = host

  const mocked: Record<string, unknown> = {
    ...original,
    host,
    // The plugin bundle normally lands via `ctx.i18n.register` at load.
    usePluginI18n: () => translateBots
  }

  Object.defineProperty(mocked, 'CapabilitiesView', {
    configurable: true,
    enumerable: true,
    get: () => (mocks.hasCapabilitiesView.value ? CapabilitiesViewStub : undefined)
  })

  return mocked
})

vi.mock('./canonical-chat', () => ({ createCanonicalChat: mocks.createCanonicalChat }))
vi.mock('./data', async importOriginal => {
  const original = await importOriginal<typeof DataModule>()

  return {
    ...original,
    captureDraftBotMeta: (...args: Parameters<typeof original.captureDraftBotMeta>) => {
      const captured = original.captureDraftBotMeta(...args)

      return {
        ...captured,
        save: (patch: Parameters<typeof captured.save>[0]) => {
          mocks.saveBotMeta(typeof args[0] === 'string' ? args[0] : args[0].name, patch)

          return captured.save(mocks.draftImage.value ? { ...patch, image: mocks.draftImage.value } : patch)
        }
      }
    }
  }
})

// Each build re-imports the dialog's whole module graph.
vi.setConfig({ testTimeout: 30_000 })

const roster: RosterRow[] = [{ connectionId: 'local', name: 'default' }]

const registryConnections: Awaited<ReturnType<typeof HermesSdk.host.connections>> = [
  { id: 'local', kind: 'local', label: 'This Mac', primary: true, tokenPreview: null, tokenSet: false },
  { id: 'studio', kind: 'remote', label: 'Studio', primary: false, tokenPreview: null, tokenSet: false }
]

/** A catalog entry that needs an API key — the only row that renders setup. */
const catalog = { auth: null as string | null, installed: false, name: 'linear', requires: ['LINEAR_API_KEY'] }

function withQueryClient(children: ReactNode) {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  )
}

async function renderDialog(
  hasCapabilitiesView: boolean,
  onConfigureModel?: (bot: RosterRow) => void,
  onClose = () => undefined
) {
  mocks.hasCapabilitiesView.value = hasCapabilitiesView
  vi.resetModules()

  const { CreateAgentDialog } = await import('./create-dialog')

  const view = render(
    withQueryClient(<CreateAgentDialog onClose={onClose} onConfigureModel={onConfigureModel} open roster={roster} />)
  )

  fireEvent.change(screen.getByPlaceholderText('inbox-triage'), { target: { value: 'inbox-triage' } })
  fireEvent.click(screen.getByRole('button', { name: /Advanced/ }))

  return view
}

function createCalls() {
  return mocks.request.mock.calls.filter(([method]) => method === 'profiles.create')
}

/** The control under a `labeled(...)` caption — the label is presentational,
 *  so it carries no `for`/`id` pair to query by. */
function controlUnder(caption: string) {
  return screen.getByText(caption).parentElement!.querySelector('[role="combobox"]') as HTMLElement
}

beforeAll(async () => {
  // Radix Select/Dialog reach for APIs jsdom does not implement.
  Element.prototype.scrollIntoView = () => undefined
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.releasePointerCapture = () => undefined
  Element.prototype.setPointerCapture = () => undefined
  // Warm the transform cache so the first test isn't charged for it.
  await import('./create-dialog')
}, 120_000)

beforeEach(() => {
  vi.clearAllMocks()
  catalog.auth = null
  mocks.completeMcpOAuth.mockResolvedValue({
    flow_id: 'synthetic-flow',
    server_name: 'linear',
    status: 'approved',
    authorization_url: null,
    error: null
  })
  mocks.draftImage.value = null
  mocks.active.connectionId = null
  mocks.active.profile = 'default'
  mocks.gateway.current = { request: mocks.request } as unknown as ReturnType<typeof HermesSdk.host.getGateway>

  if (mocks.host) {
    mocks.host.requestProfile = mocks.requestProfile as typeof HermesSdk.host.requestProfile
    mocks.host.deleteProfile = mocks.deleteProfile
    mocks.host.completeMcpOAuth = mocks.completeMcpOAuth as typeof HermesSdk.host.completeMcpOAuth
  }

  mocks.deleteProfile.mockResolvedValue(undefined)
  mocks.skillsView.length = 0
  mocks.connections.mockResolvedValue([])
  mocks.requestProfile.mockResolvedValue({})
  mocks.request.mockImplementation(async (method: string) => {
    switch (method) {
      case 'mcp.catalog':
        return { servers: [catalog] }

      case 'mcp.servers.test':
        return { ok: true }

      case 'profiles.describe':
        return { mcp_servers: [], skills: [], toolsets: [] }

      case 'cli.exec':
        return { code: 0 }

      default:
        return {}
    }
  })
})

afterEach(() => {
  cleanup()
})

describe('materializing the draft profile', () => {
  it.each([
    ['小助手', 'u5c0f-u52a9-u624b'],
    ['test机器人', 'test-u673a-u5668-u4eba']
  ])('creates %s with an ASCII profile id and retains the display name', async (enteredName, profileName) => {
    await renderDialog(true)

    fireEvent.change(screen.getByPlaceholderText('inbox-triage'), {
      target: { value: enteredName }
    })

    const create = screen.getByRole('button', { name: 'Create Bot' })

    expect((create as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(create)

    await waitFor(() => expect(createCalls()).toHaveLength(1))
    expect(createCalls()[0][1]).toMatchObject({
      description: enteredName,
      name: profileName
    })
    expect(mocks.saveBotMeta).toHaveBeenCalledWith(profileName, expect.objectContaining({ title: enteredName }))
    await waitFor(() => expect(mocks.createCanonicalChat).toHaveBeenCalledWith(profileName, { kickoff: true }))
  })

  it('creates it once when the Capabilities tab opens, pinned to the new slug', async () => {
    await renderDialog(true)

    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))

    await waitFor(() => expect(createCalls()).toHaveLength(1))
    expect(createCalls()[0][1]).toMatchObject({ clone_from: 'default', name: 'inbox-triage' })
    // The live surface needs a REAL backend row to write to.
    await waitFor(() => expect(mocks.skillsView.at(-1)).toMatchObject({ fixedProfile: 'inbox-triage' }))

    // Create Bot goes through the same helper — no duplicate profiles.create.
    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))

    await waitFor(() => expect(mocks.createCanonicalChat).toHaveBeenCalledWith('inbox-triage', { kickoff: true }))
    expect(createCalls()).toHaveLength(1)
  })

  it('pins a remote-target draft to the TARGET machine, not the active gateway', async () => {
    mocks.connections.mockResolvedValue(registryConnections)

    await renderDialog(true)

    // "Create on" only exists on a multi-connection desktop.
    await screen.findByText('Create on')
    fireEvent.click(controlUnder('Create on'))
    fireEvent.click(await screen.findByRole('option', { name: 'Studio' }))
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))

    // The create lands on THAT machine's backend — no gateway switch, and the
    // clone source is the remote's own default (a local profile name the
    // remote box does not have would fail there).
    await waitFor(() =>
      expect(mocks.requestProfile).toHaveBeenCalledWith(
        expect.objectContaining({ connectionId: 'studio' }),
        'profiles.create',
        expect.objectContaining({ clone_from: 'default', name: 'inbox-triage' })
      )
    )
    expect(createCalls()).toHaveLength(0)
    await waitFor(() =>
      expect(mocks.skillsView.at(-1)).toMatchObject({ fixedConnection: 'studio', fixedProfile: 'inbox-triage' })
    )
  })

  it('lets a remote-target Bot start fresh instead of forcing a clone of the target default', async () => {
    mocks.connections.mockResolvedValue(registryConnections)

    await renderDialog(true)

    await screen.findByText('Create on')
    fireEvent.click(controlUnder('Create on'))
    fireEvent.click(await screen.findByRole('option', { name: 'Studio' }))

    // The picker stays live for a remote target and offers only what exists
    // on THAT machine: fresh, or its own default — never a local roster name.
    const cloneFrom = controlUnder('Clone from profile (on Studio)')

    expect(cloneFrom.getAttribute('data-disabled')).toBeNull()
    fireEvent.click(cloneFrom)
    expect(screen.getAllByRole('option').map(option => option.textContent)).toEqual([
      'Fresh profile (bundled skills)',
      'default'
    ])
    fireEvent.click(screen.getByRole('option', { name: /Fresh profile/ }))
    // Shared keys live on the TARGET machine, and the label says which.
    screen.getByText('Share keys & accounts with the default profile on Studio')

    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))

    await waitFor(() =>
      expect(mocks.requestProfile).toHaveBeenCalledWith(
        expect.objectContaining({ connectionId: 'studio' }),
        'profiles.create',
        expect.objectContaining({ clone_from: null, name: 'inbox-triage' })
      )
    )
  })

  it('creates it on the first MCP setup click, then adds the server to it', async () => {
    // Older builds keep the staged MCP tab, which is where the setup button
    // lives. There is no "save the agent first" gate: the button is live.
    await renderDialog(false)

    fireEvent.click(screen.getByRole('button', { name: 'MCP' }))
    fireEvent.click(await screen.findByRole('button', { name: /Set up/ }))

    await waitFor(() => expect(createCalls()).toHaveLength(1))
    expect(createCalls()[0][1]).toMatchObject({ name: 'inbox-triage' })
    // The add lands on the profile the click just minted, not on `null`.
    await waitFor(() =>
      expect(mocks.request).toHaveBeenCalledWith('mcp.servers.add', {
        name: 'linear',
        preset: 'linear',
        profile: 'inbox-triage'
      })
    )
  })
})

describe('provider readiness before the intro turn', () => {
  const notReady = { error: 'No usable credentials found for openrouter.', ok: false }

  it('keeps the bot, skips the intro, and offers model setup when its provider is not ready', async () => {
    const base = mocks.request.getMockImplementation()!
    mocks.request.mockImplementation(async (method: string, params?: unknown) =>
      method === 'setup.runtime_check' ? notReady : base(method, params)
    )
    const onConfigureModel = vi.fn()

    await renderDialog(true, onConfigureModel)
    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))

    await waitFor(() => expect(mocks.createCanonicalChat).toHaveBeenCalledWith('inbox-triage', { kickoff: false }))
    expect(mocks.request).toHaveBeenCalledWith('setup.runtime_check', { profile: 'inbox-triage' })
    expect(createCalls()).toHaveLength(1)

    const toast = mocks.notify.mock.calls.at(-1)![0]

    expect(toast).toMatchObject({
      detail: notReady.error,
      kind: 'warning',
      title: 'Bot "Inbox Triage" created'
    })
    toast.action.onClick()
    expect(onConfigureModel).toHaveBeenCalledWith(expect.objectContaining({ name: 'inbox-triage' }))
  })

  it('runs the intro when the provider check passes', async () => {
    const base = mocks.request.getMockImplementation()!
    mocks.request.mockImplementation(async (method: string, params?: unknown) =>
      method === 'setup.runtime_check' ? { ok: true } : base(method, params)
    )

    await renderDialog(true)
    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))

    await waitFor(() => expect(mocks.createCanonicalChat).toHaveBeenCalledWith('inbox-triage', { kickoff: true }))
    expect(mocks.notify).toHaveBeenLastCalledWith({ kind: 'success', message: 'Bot "Inbox Triage" created' })
  })

  it('checks a remote bot on its own machine and warns without an editor shortcut', async () => {
    mocks.connections.mockResolvedValue(registryConnections)
    mocks.requestProfile.mockImplementation(async () => notReady)

    await renderDialog(true, vi.fn())
    await screen.findByText('Create on')
    fireEvent.click(controlUnder('Create on'))
    fireEvent.click(await screen.findByRole('option', { name: 'Studio' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))

    await waitFor(() =>
      expect(mocks.notify).toHaveBeenLastCalledWith(
        expect.objectContaining({ kind: 'warning', title: 'Bot "Inbox Triage" created on Studio' })
      )
    )
    expect(mocks.requestProfile).toHaveBeenCalledWith(
      expect.objectContaining({ connectionId: 'studio' }),
      'setup.runtime_check',
      { profile: 'inbox-triage' }
    )
    expect(mocks.notify.mock.calls.at(-1)![0].action).toBeUndefined()
    expect(mocks.request).not.toHaveBeenCalledWith('setup.runtime_check', expect.anything())
  })
})

describe('the clone-from default', () => {
  it('regression: reset restores the mounted default, not "fresh profile"', async () => {
    await renderDialog(true)

    // `labeled()` renders a bare <label>, so the trigger is found by position.
    const cloneFrom = () =>
      screen.getByText('Clone from profile').parentElement!.querySelector('[role="combobox"]') as HTMLElement

    expect(cloneFrom().textContent).toBe('default')

    fireEvent.click(cloneFrom())
    fireEvent.click(await screen.findByRole('option', { name: /Fresh profile/ }))
    expect(cloneFrom().textContent).toMatch(/Fresh profile/)

    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))
    await waitFor(() => expect(mocks.createCanonicalChat).toHaveBeenCalled())

    // Second open: the picker must read `default` again, or every agent after
    // the first silently starts from a bare profile.
    fireEvent.click(screen.getByRole('button', { name: /Advanced/ }))
    expect(cloneFrom().textContent).toBe('default')
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void

  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })

  return { promise, resolve, reject }
}

describe('the draft retains its original owner', () => {
  function modernOwner() {
    mocks.active.connectionId = 'local'
    mocks.active.profile = 'work'
    mocks.connections.mockResolvedValue(registryConnections)
  }

  it('locks name and target at materialization and keeps submit on the captured SDK route', async () => {
    modernOwner()
    await renderDialog(true)
    await screen.findByText('Create on')
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
    await waitFor(() => expect(mocks.skillsView.at(-1)).toMatchObject({ fixedConnection: 'local' }))
    expect((screen.getByPlaceholderText('inbox-triage') as HTMLInputElement).disabled).toBe(true)
    expect(controlUnder('Create on').getAttribute('data-disabled')).not.toBeNull()
    fireEvent.change(screen.getByPlaceholderText('inbox-triage'), { target: { value: 'other-name' } })
    expect((screen.getByPlaceholderText('inbox-triage') as HTMLInputElement).value).toBe('inbox-triage')

    const replacement = vi.fn()
    mocks.active.connectionId = 'studio'
    mocks.active.profile = 'elsewhere'
    mocks.host!.requestProfile = replacement
    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))

    await waitFor(() =>
      expect(mocks.requestProfile).toHaveBeenCalledWith(
        { connectionId: 'local', mode: 'local', profile: 'work', targetProfile: 'work' },
        'setup.runtime_check',
        { profile: 'inbox-triage' }
      )
    )
    expect(mocks.requestProfile.mock.calls.filter(([, method]) => method === 'profiles.create')).toHaveLength(1)
    expect(replacement).not.toHaveBeenCalled()
    expect(mocks.createCanonicalChat).not.toHaveBeenCalled()
  })

  it('retains the draft after rejected cleanup and retries only its original delete handle', async () => {
    modernOwner()
    const onClose = vi.fn()
    await renderDialog(true, undefined, onClose)
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
    await waitFor(() => expect(mocks.skillsView.at(-1)).toMatchObject({ fixedProfile: 'inbox-triage' }))
    const replacement = vi.fn()
    mocks.host!.deleteProfile = replacement
    mocks.active.connectionId = 'studio'
    mocks.active.profile = 'inbox-triage'
    mocks.deleteProfile.mockRejectedValueOnce(new Error('cleanup refused'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await screen.findByText('cleanup refused')
    expect(onClose).not.toHaveBeenCalled()
    expect((screen.getByPlaceholderText('inbox-triage') as HTMLInputElement).value).toBe('inbox-triage')
    expect((screen.getByPlaceholderText('inbox-triage') as HTMLInputElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(mocks.deleteProfile).toHaveBeenCalledTimes(2)
    expect(mocks.deleteProfile).toHaveBeenLastCalledWith({
      connectionId: 'local',
      mode: 'local',
      profile: 'inbox-triage',
      targetProfile: 'inbox-triage'
    })
    expect(replacement).not.toHaveBeenCalled()
    expect(mocks.requestProfile.mock.calls.filter(([, method]) => method === 'profiles.create')).toHaveLength(1)
  })

  it('awaits pending creation and deletion, coalescing repeated Cancel and target changes', async () => {
    modernOwner()
    const create = deferred<unknown>()
    const remove = deferred<void>()
    mocks.requestProfile.mockImplementation(async (_route, method) =>
      method === 'profiles.create' ? create.promise : {}
    )
    mocks.deleteProfile.mockImplementation(() => remove.promise)
    const onClose = vi.fn()
    await renderDialog(true, undefined, onClose)
    await screen.findByText('Create on')
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
    await waitFor(() =>
      expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'profiles.create')).toBe(true)
    )
    expect(controlUnder('Create on').getAttribute('data-disabled')).not.toBeNull()
    fireEvent.click(controlUnder('Create on'))
    expect(screen.queryByRole('option', { name: 'Studio' })).toBeNull()
    mocks.active.connectionId = 'studio'
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(mocks.deleteProfile).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    await act(async () => create.resolve({}))
    await waitFor(() => expect(mocks.deleteProfile).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).not.toHaveBeenCalled()
    await act(async () => remove.resolve())
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(mocks.deleteProfile).toHaveBeenCalledTimes(1)
    expect(mocks.requestProfile.mock.calls.filter(([, method]) => method === 'profiles.create')).toHaveLength(1)
  })

  it('uses the captured legacy gateway for submit after the foreground socket changes', async () => {
    await renderDialog(true)
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
    await waitFor(() => expect(mocks.skillsView.at(-1)).toMatchObject({ fixedProfile: 'inbox-triage' }))
    const replacement = vi.fn()
    mocks.gateway.current = { request: replacement } as unknown as ReturnType<typeof HermesSdk.host.getGateway>
    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))
    await waitFor(() => expect(mocks.request).toHaveBeenCalledWith('setup.runtime_check', { profile: 'inbox-triage' }))
    expect(replacement).not.toHaveBeenCalled()
    expect(mocks.createCanonicalChat).not.toHaveBeenCalled()
  })

  it('checks legacy CLI deletion results and retains the draft until retry succeeds', async () => {
    const onClose = vi.fn()
    await renderDialog(true, undefined, onClose)
    mocks.host!.deleteProfile = undefined as unknown as typeof HermesSdk.host.deleteProfile
    const base = mocks.request.getMockImplementation()!
    let blocked = true
    mocks.request.mockImplementation(async (method: string, params?: unknown) =>
      method === 'cli.exec' ? (blocked ? { blocked: true, hint: 'delete blocked' } : { code: 0 }) : base(method, params)
    )
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
    await waitFor(() => expect(mocks.skillsView.at(-1)).toMatchObject({ fixedProfile: 'inbox-triage' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await screen.findByText('delete blocked')
    expect(onClose).not.toHaveBeenCalled()
    blocked = false
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(mocks.request).toHaveBeenCalledWith('cli.exec', { argv: ['profile', 'delete', 'inbox-triage', '--yes'] })
  })

  it('fails closed when no stable legacy gateway can be captured', async () => {
    mocks.gateway.current = null
    await renderDialog(true)
    fireEvent.click(screen.getByRole('button', { name: 'Create Bot' }))
    await screen.findByText('Cannot capture the draft gateway. Reconnect before creating a bot.')
    expect(createCalls()).toHaveLength(0)
    expect(mocks.createCanonicalChat).not.toHaveBeenCalled()
  })
})

describe('draft cancellation failure boundaries', () => {
  it('can close after a pending creation fails without attempting deletion', async () => {
    const creation = deferred<unknown>()
    const base = mocks.request.getMockImplementation()!
    mocks.request.mockImplementation(async (method: string, params?: unknown) =>
      method === 'profiles.create' ? creation.promise : base(method, params)
    )
    const onClose = vi.fn()
    await renderDialog(true, undefined, onClose)
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
    await waitFor(() => expect(createCalls()).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await act(async () => creation.reject(new Error('creation refused')))
    await screen.findByText('creation refused')
    expect(mocks.deleteProfile).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(mocks.deleteProfile).not.toHaveBeenCalled()
  })

  it('keeps a legacy draft retryable until the original gateway returns for SDK deletion', async () => {
    const gateway = mocks.gateway.current
    const onClose = vi.fn()
    await renderDialog(true, undefined, onClose)
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
    await waitFor(() => expect(mocks.skillsView.at(-1)).toMatchObject({ fixedProfile: 'inbox-triage' }))
    mocks.gateway.current = { request: vi.fn() } as unknown as ReturnType<typeof HermesSdk.host.getGateway>
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await screen.findByText('Return to the draft gateway before discarding this bot.')
    expect(onClose).not.toHaveBeenCalled()
    expect(mocks.deleteProfile).not.toHaveBeenCalled()
    mocks.gateway.current = gateway
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(mocks.deleteProfile).toHaveBeenCalledWith('inbox-triage')
  })

  it('does not bypass owner-qualified SDK teardown on a registry connection', async () => {
    mocks.active.connectionId = 'studio'
    await renderDialog(true)
    mocks.host!.deleteProfile = undefined as unknown as typeof HermesSdk.host.deleteProfile
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
    await waitFor(() => expect(mocks.skillsView.at(-1)).toMatchObject({ fixedProfile: 'inbox-triage' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await screen.findByText('Source-scoped draft deletion requires host.deleteProfile.')
    expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'cli.exec')).toBe(false)
  })
})

describe('pending draft configuration retains its owner', () => {
  it.each(['success', 'rejection'] as const)(
    'waits for an owned avatar write %s before deleting the draft',
    async outcome => {
      mocks.active.connectionId = 'local'
      mocks.active.profile = 'work'
      mocks.draftImage.value = 'data:image/png;base64,cHJvYmU='
      const asset = deferred<unknown>()
      mocks.requestProfile.mockImplementation(async (_route, method) => {
        if (method === 'profiles.list') {
          return { profiles: [{ name: 'inbox-triage' }] }
        }

        if (method === 'profiles.set_asset') {
          return asset.promise
        }

        return {}
      })
      const onClose = vi.fn()
      await renderDialog(true, undefined, onClose)
      fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
      await waitFor(() =>
        expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'profiles.set_asset')).toBe(true)
      )
      mocks.active.connectionId = 'studio'
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(mocks.deleteProfile).not.toHaveBeenCalled()
      expect(onClose).not.toHaveBeenCalled()
      await act(async () => (outcome === 'success' ? asset.resolve({}) : asset.reject(new Error('asset refused'))))
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
      expect(mocks.deleteProfile).toHaveBeenCalledWith({
        connectionId: 'local',
        mode: 'local',
        profile: 'inbox-triage',
        targetProfile: 'inbox-triage'
      })
    }
  )

  it('captures MCP add on the draft owner while creation waits across a route switch', async () => {
    mocks.active.connectionId = 'local'
    mocks.active.profile = 'work'
    const creation = deferred<unknown>()
    const ambientAdds: Array<string | null> = []
    const base = mocks.request.getMockImplementation()!
    mocks.request.mockImplementation(async (method: string, params?: unknown) => {
      if (method === 'mcp.servers.add') {
        ambientAdds.push(mocks.active.connectionId)
      }

      return base(method, params)
    })
    mocks.requestProfile.mockImplementation(async (_route, method) =>
      method === 'profiles.create' ? creation.promise : {}
    )
    await renderDialog(false)
    fireEvent.click(screen.getByRole('button', { name: 'MCP' }))
    fireEvent.click(await screen.findByRole('button', { name: /Set up/ }))
    await waitFor(() =>
      expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'profiles.create')).toBe(true)
    )
    mocks.active.connectionId = 'studio'
    await act(async () => creation.resolve({}))
    await screen.findByPlaceholderText('LINEAR_API_KEY')
    expect(ambientAdds).toEqual([])
    await waitFor(() =>
      expect(mocks.requestProfile).toHaveBeenCalledWith(
        { connectionId: 'local', mode: 'local', profile: 'work', targetProfile: 'work' },
        'mcp.servers.add',
        { profile: 'inbox-triage', name: 'linear', preset: 'linear' }
      )
    )
    expect(ambientAdds).toEqual([])
    mocks.active.connectionId = 'third-owner'
    fireEvent.change(screen.getByPlaceholderText('LINEAR_API_KEY'), { target: { value: 'synthetic-only-key' } })
    fireEvent.click(screen.getByRole('button', { name: /Save.*test/i }))
    await waitFor(() =>
      expect(mocks.requestProfile).toHaveBeenCalledWith(
        expect.objectContaining({ connectionId: 'local', profile: 'work' }),
        'mcp.servers.set_api_key',
        expect.objectContaining({ profile: 'inbox-triage', value: 'synthetic-only-key' })
      )
    )
    await waitFor(() =>
      expect(mocks.requestProfile).toHaveBeenCalledWith(
        expect.objectContaining({ connectionId: 'local', profile: 'work' }),
        'mcp.servers.test',
        { profile: 'inbox-triage', name: 'linear' }
      )
    )
    expect(mocks.request.mock.calls.some(([method]) => method === 'mcp.servers.set_api_key')).toBe(false)
  })
})

describe('draft MCP cancellation and capability boundaries', () => {
  async function beginSetup(onClose = () => undefined) {
    await renderDialog(false, undefined, onClose)
    fireEvent.click(screen.getByRole('button', { name: 'MCP' }))
    fireEvent.click(await screen.findByRole('button', { name: /Set up|Sign in/ }))
  }

  it('drains a pending add before deletion and refuses a key write while Cancel is pending', async () => {
    mocks.active.connectionId = 'local'
    const add = deferred<unknown>()
    const deletion = deferred<void>()
    mocks.requestProfile.mockImplementation(async (_route, method) => (method === 'mcp.servers.add' ? add.promise : {}))
    mocks.deleteProfile.mockImplementation(() => deletion.promise)
    const onClose = vi.fn()
    await beginSetup(onClose)
    await waitFor(() =>
      expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'mcp.servers.add')).toBe(true)
    )
    mocks.active.connectionId = 'studio'
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(mocks.deleteProfile).not.toHaveBeenCalled()
    await act(async () => add.resolve({}))
    await waitFor(() => expect(mocks.deleteProfile).toHaveBeenCalledTimes(1))
    fireEvent.change(await screen.findByPlaceholderText('LINEAR_API_KEY'), { target: { value: 'synthetic-only-key' } })
    fireEvent.click(screen.getByRole('button', { name: /Save.*test/i }))
    await screen.findByText(/This draft is being discarded/)
    expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'mcp.servers.set_api_key')).toBe(false)
    await act(async () => deletion.resolve())
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('does not start setup after a pending creation is cancelled', async () => {
    mocks.active.connectionId = 'local'
    const creation = deferred<unknown>()
    mocks.requestProfile.mockImplementation(async (_route, method) =>
      method === 'profiles.create' ? creation.promise : {}
    )
    const onClose = vi.fn()
    await beginSetup(onClose)
    await waitFor(() =>
      expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'profiles.create')).toBe(true)
    )
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await act(async () => creation.resolve({}))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(mocks.requestProfile.mock.calls.some(([, method]) => method.startsWith('mcp.servers.'))).toBe(false)
  })

  it.each([true, false])('probes the captured owner instead of ambient availability: supported=%s', async supported => {
    mocks.active.connectionId = 'local'
    const base = mocks.request.getMockImplementation()!
    mocks.request.mockImplementation(async (method: string, params?: unknown) => {
      if (method === 'mcp.servers.list' && supported) {
        throw new Error('unknown method')
      }

      return base(method, params)
    })
    mocks.requestProfile.mockImplementation(async (_route, method) => {
      if (method === 'mcp.servers.list' && !supported) {
        throw new Error('unknown method')
      }

      return {}
    })
    await beginSetup()
    await waitFor(() =>
      expect(mocks.requestProfile).toHaveBeenCalledWith(
        expect.objectContaining({ connectionId: 'local' }),
        'mcp.servers.list',
        {}
      )
    )
    expect(mocks.request.mock.calls.some(([method]) => method === 'mcp.servers.list')).toBe(false)

    if (supported) {
      await screen.findByPlaceholderText('LINEAR_API_KEY')
    } else {
      await waitFor(() => expect(screen.queryByRole('button', { name: /^Set up$/ })).toBeNull())
      expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'mcp.servers.add')).toBe(false)
    }
  })

  it('pins OAuth to its captured owner and passes cancellation before deleting', async () => {
    mocks.active.connectionId = 'local'
    mocks.active.profile = 'work'
    catalog.auth = 'oauth'
    const creation = deferred<unknown>()
    const oauth = deferred<Awaited<ReturnType<typeof HermesSdk.host.completeMcpOAuth>>>()
    mocks.requestProfile.mockImplementation(async (_route, method) =>
      method === 'profiles.create' ? creation.promise : {}
    )
    mocks.completeMcpOAuth.mockImplementation(() => oauth.promise)
    const onClose = vi.fn()
    await beginSetup(onClose)
    await waitFor(() =>
      expect(mocks.requestProfile.mock.calls.some(([, method]) => method === 'profiles.create')).toBe(true)
    )
    mocks.active.connectionId = 'studio'
    const replacement = vi.fn()
    mocks.host!.completeMcpOAuth = replacement
    await act(async () => creation.resolve({}))
    await waitFor(() => expect(mocks.completeMcpOAuth).toHaveBeenCalledTimes(1))
    const options = mocks.completeMcpOAuth.mock.calls[0][0]
    expect(options.profile).toEqual({ connectionId: 'local', profile: 'inbox-triage' })
    expect(options.cancelled?.()).toBe(false)
    expect(replacement).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(options.cancelled?.()).toBe(true)
    expect(mocks.deleteProfile).not.toHaveBeenCalled()
    await act(async () => oauth.reject(new Error('synthetic cancellation')))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(mocks.deleteProfile).toHaveBeenCalledWith(
      expect.objectContaining({ connectionId: 'local', profile: 'inbox-triage' })
    )
  })

  it('fails closed for legacy OAuth without an explicit connection owner', async () => {
    catalog.auth = 'oauth'
    await beginSetup()
    await screen.findByText(/A registered draft gateway is required/)
    expect(mocks.completeMcpOAuth).not.toHaveBeenCalled()
  })
})

it('preserves standalone name-scoped MCP setup without a draft target', async () => {
  vi.resetModules()
  const { McpSetupButton } = await import('./mcp-setup')
  render(withQueryClient(<McpSetupButton entry={{ ...catalog, fromCatalog: true }} profile="existing-bot" />))
  fireEvent.click(await screen.findByRole('button', { name: /Set up/ }))
  await screen.findByPlaceholderText('LINEAR_API_KEY')
  expect(mocks.request).toHaveBeenCalledWith('mcp.servers.add', {
    profile: 'existing-bot',
    name: 'linear',
    preset: 'linear'
  })
  expect(mocks.requestProfile).not.toHaveBeenCalled()
})

it('does not borrow another legacy owners same-name image while creation is pending', async () => {
  const creation = deferred<unknown>()
  const image = 'data:image/png;base64,cHJvYmU='
  mocks.draftImage.value = image
  const base = mocks.request.getMockImplementation()!
  mocks.request.mockImplementation(async (method: string, params?: unknown) => {
    if (method === 'profiles.create') {
      return creation.promise
    }

    if (method === 'profiles.list') {
      return { profiles: [{ name: 'inbox-triage' }] }
    }

    return base(method, params)
  })
  await renderDialog(true)
  fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }))
  await waitFor(() => expect(createCalls()).toHaveLength(1))
  const { $botMeta } = await import('./data')
  const otherLook = { 'inbox-triage': { title: 'Other owner', image } }
  $botMeta.set(otherLook)
  const replacement = vi.fn()
  mocks.gateway.current = { request: replacement } as unknown as ReturnType<typeof HermesSdk.host.getGateway>
  await act(async () => creation.resolve({}))
  await waitFor(() =>
    expect(mocks.request).toHaveBeenCalledWith('profiles.set_asset', {
      name: 'inbox-triage',
      asset: 'avatar',
      data: image
    })
  )
  expect($botMeta.get()).toEqual(otherLook)
  expect(replacement).not.toHaveBeenCalled()
})
