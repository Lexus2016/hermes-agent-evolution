import { EventEmitter } from 'node:events'

import type { BrowserWindow, IpcMain, IpcMainInvokeEvent } from 'electron'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  bindQuickEntryComposerDocument,
  completeQuickEntryWindow,
  createQuickEntryShortcut,
  createQuickEntrySubmitRelay,
  DEFAULT_QUICK_ENTRY_SHORTCUT,
  type GlobalShortcutLike,
  parseQuickEntryShortcut,
  quickEntryWindowBounds,
  registerQuickEntrySubmitIpc,
  sanitizeQuickEntrySettings
} from './quick-entry'

function relayHarness() {
  const onComplete = vi.fn()
  const onLateResult = vi.fn()
  const relay = createQuickEntrySubmitRelay({ onComplete, onLateResult })
  const session = relay.openDocument(10)
  const owner = (submitId: number) => ({ generation: session.generation, submitId, windowId: 10 })

  const send = (submitId: number) => {
    let id = ''

    const promise = relay.begin(owner(submitId), correlationId => {
      id = correlationId
    })

    return { id, promise }
  }

  return { onComplete, onLateResult, owner, relay, send, session }
}

describe('quick-entry submit ack relay', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('ignores unknown replies and completes a successful request once, only on renderer approval', async () => {
    const h = relayHarness()
    const sent = h.send(1)
    h.relay.acknowledge('unknown', { ok: true })
    expect(h.relay.pendingCount()).toBe(1)
    h.relay.acknowledge(sent.id, { ok: true })
    await expect(sent.promise).resolves.toMatchObject({
      generation: h.session.generation,
      submitId: 1,
      correlationId: sent.id,
      ok: true
    })
    expect(h.onComplete).not.toHaveBeenCalled()
    expect(h.relay.complete(10, h.session.generation, sent.id, true)).toBe(true)
    expect(h.relay.complete(10, h.session.generation, sent.id, true)).toBe(false)
    expect(h.onComplete).toHaveBeenCalledExactlyOnceWith(h.owner(1), true)
  })

  it.each([0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])(
    'rejects invalid submit ID %s',
    async id => {
      const h = relayHarness()
      const forward = vi.fn()
      await expect(h.relay.begin(h.owner(id), forward)).resolves.toMatchObject({ code: 'invalid-submit' })
      expect(forward).not.toHaveBeenCalled()
      expect(h.relay.pendingCount()).toBe(0)
    }
  )

  it('rejects reused IDs and stale generations without advancing the current high-water', async () => {
    const h = relayHarness()
    const sent = h.send(5)
    await expect(h.send(5).promise).resolves.toMatchObject({ code: 'invalid-submit' })
    await expect(h.send(4).promise).resolves.toMatchObject({ code: 'invalid-submit' })
    expect(h.relay.getSession(10)?.nextSubmitId).toBe(6)
    h.relay.acknowledge(sent.id, { ok: false })
    await sent.promise
    await expect(h.relay.begin({ ...h.owner(6), generation: 'old' }, vi.fn())).resolves.toMatchObject({
      code: 'invalid-submit'
    })
    expect(h.relay.getSession(10)?.nextSubmitId).toBe(6)
  })

  it('only gates hide/focus on latest: older hide=false can consume once', async () => {
    const h = relayHarness()
    const first = h.send(1)
    const second = h.send(2)
    h.relay.acknowledge(first.id, { ok: true })
    h.relay.acknowledge(second.id, { ok: true })
    await Promise.all([first.promise, second.promise])
    expect(h.relay.complete(11, h.session.generation, second.id, true)).toBe(false)
    expect(h.relay.complete(10, 'wrong', second.id, true)).toBe(false)
    expect(h.relay.complete(10, h.session.generation, 'unknown', true)).toBe(false)
    expect(h.relay.complete(10, h.session.generation, first.id, true)).toBe(false)
    expect(h.relay.complete(10, h.session.generation, first.id, false)).toBe(true)
    expect(h.relay.complete(10, h.session.generation, first.id, false)).toBe(false)
    expect(h.onComplete).not.toHaveBeenCalled()
    expect(h.relay.complete(10, h.session.generation, second.id, true)).toBe(true)
    expect(h.onComplete).toHaveBeenCalledExactlyOnceWith(h.owner(2), true)
  })

  it.each([
    [1, 2],
    [2, 1]
  ])('correlates multiple timeouts and late results in order %s,%s', async (first, second) => {
    const h = relayHarness()
    const sent = [h.send(1), h.send(2)]
    await vi.advanceTimersByTimeAsync(15_000)

    for (const [index, request] of sent.entries()) {
      await expect(request.promise).resolves.toMatchObject({
        code: 'timeout',
        retryable: false,
        submitId: index + 1,
        correlationId: request.id,
        generation: h.session.generation
      })
    }

    expect(h.relay.reconcilableCount()).toBe(2)

    for (const id of [first, second]) {
      h.relay.acknowledge(sent[id - 1].id, { ok: id === 2 })
    }

    expect(h.onLateResult.mock.calls.map(([owner]) => owner.submitId)).toEqual([first, second])
    expect(h.relay.reconcilableCount()).toBe(0)
    h.relay.acknowledge(sent[1].id, { ok: true })
    expect(h.onLateResult).toHaveBeenCalledTimes(2)
    expect(h.relay.complete(10, h.session.generation, sent[1].id, true)).toBe(true)
    expect(h.onComplete).toHaveBeenCalledExactlyOnceWith(h.owner(2), false)
  })

  it('expires only accepted native capabilities and retains unresolved requests', async () => {
    const h = relayHarness()
    const accepted = h.send(1)
    const unknown = h.send(2)
    h.relay.acknowledge(accepted.id, { ok: true })
    await accepted.promise
    await vi.advanceTimersByTimeAsync(30_000)
    await unknown.promise
    expect(h.relay.acceptedCount()).toBe(0)
    expect(h.relay.complete(10, h.session.generation, accepted.id, true)).toBe(false)
    expect(h.relay.reconcilableCount()).toBe(1)
    h.relay.acknowledge(unknown.id, { ok: false })
    expect(h.onLateResult).toHaveBeenCalledTimes(1)
  })

  it('disposes pending, unknown and accepted ownership without routing callbacks to a new document', async () => {
    const h = relayHarness()
    const unknown = h.send(1)
    await vi.advanceTimersByTimeAsync(15_000)
    await unknown.promise
    const accepted = h.send(2)
    h.relay.acknowledge(accepted.id, { ok: true })
    await accepted.promise
    const pending = h.send(3)
    h.relay.disposeWindow(10)
    await expect(pending.promise).resolves.toMatchObject({ code: 'disposed' })
    const next = h.relay.openDocument(10)
    expect(next).toEqual({ generation: expect.not.stringMatching(h.session.generation), nextSubmitId: 1 })

    for (const id of [unknown.id, accepted.id, pending.id]) {
      h.relay.acknowledge(id, { ok: true })
    }

    expect(h.relay.complete(10, next.generation, accepted.id, true)).toBe(false)
    expect(h.onLateResult).not.toHaveBeenCalled()
    expect(h.onComplete).not.toHaveBeenCalled()
    expect([h.relay.pendingCount(), h.relay.reconcilableCount(), h.relay.acceptedCount()]).toEqual([0, 0, 0])
  })

  it('turns a synchronous forwarding failure into a correlated failure without leaving a timer', async () => {
    const h = relayHarness()
    await expect(
      h.relay.begin(h.owner(1), () => {
        throw new Error('closed')
      })
    ).resolves.toMatchObject({ code: 'forward-failed', submitId: 1 })
    expect(h.relay.pendingCount()).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
  })
})

function nativeWindow(id: number) {
  const contents = Object.assign(new EventEmitter(), { id, mainFrame: {}, send: vi.fn() })

  const win = Object.assign(new EventEmitter(), {
    webContents: contents,
    isDestroyed: vi.fn(() => false),
    isVisible: vi.fn(() => true),
    hide: vi.fn(),
    show: vi.fn(),
    focus: vi.fn()
  })

  return {
    win,
    native: win as unknown as BrowserWindow,
    event: { sender: contents, senderFrame: contents.mainFrame } as unknown as IpcMainInvokeEvent
  }
}

describe('Quick Entry registered native handlers', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('rotates only committed main documents and invalidates renderer loss/closed windows', async () => {
    const h = relayHarness()
    const window = nativeWindow(20)
    bindQuickEntryComposerDocument(window.native, h.relay)
    expect(h.relay.getSession(20)).toBeNull()
    window.win.webContents.emit('did-start-navigation', { isMainFrame: true })
    window.win.webContents.emit('did-fail-load')
    expect(h.relay.getSession(20)).toBeNull()
    window.win.webContents.emit('did-navigate')
    const original = h.relay.getSession(20)!
    const pending = h.relay.begin({ ...original, submitId: 4, windowId: 20 }, vi.fn())
    window.win.webContents.emit('did-start-navigation', { isMainFrame: true })
    window.win.webContents.emit('did-fail-load')
    window.win.webContents.emit('did-navigate-in-page')
    expect(h.relay.getSession(20)).toEqual({ ...original, nextSubmitId: 5 })
    window.win.webContents.emit('did-navigate')
    await expect(pending).resolves.toMatchObject({ code: 'disposed' })
    expect(h.relay.getSession(20)?.nextSubmitId).toBe(1)
    expect(h.relay.getSession(20)?.generation).not.toBe(original.generation)
    window.win.webContents.emit('render-process-gone')
    expect(h.relay.getSession(20)).toBeNull()
    window.win.webContents.emit('did-navigate')
    expect(h.relay.getSession(20)).not.toBeNull()
    window.win.emit('closed')
    expect(h.relay.getSession(20)).toBeNull()
  })

  it('authenticates real submit/session/ack/complete/dismiss handlers by current window and main frame', async () => {
    const h = relayHarness()
    const quick = nativeWindow(10)
    const primary = nativeWindow(1)
    const other = nativeWindow(99)
    const handlers = new Map<string, Parameters<IpcMain['handle']>[1]>()
    const events = new Map<string, (...args: unknown[]) => void>()

    const ipc = {
      handle: (name: string, handler: Parameters<IpcMain['handle']>[1]) => handlers.set(name, handler),
      on: (name: string, handler: (...args: unknown[]) => void) => events.set(name, handler)
    } as unknown as IpcMain

    registerQuickEntrySubmitIpc(ipc, {
      quickWindow: () => quick.native,
      primaryWindow: () => primary.native,
      relay: h.relay
    })

    const invoke = (name: string, event = quick.event, payload?: unknown) =>
      handlers.get(`hermes:quick-entry:${name}`)!(event, payload)

    expect(invoke('session')).toEqual(h.session)
    expect(invoke('session', other.event)).toBeNull()
    expect(invoke('session', { ...quick.event, senderFrame: other.event.senderFrame })).toBeNull()
    const payload = { ...h.session, submitId: 1, text: ' hello ', target: 'new' }
    expect(invoke('submit', other.event, payload)).toMatchObject({ code: 'forbidden' })
    const pending = invoke('submit', quick.event, payload)
    const forwarded = primary.win.webContents.send.mock.calls[0][1]
    expect(forwarded).toEqual({ correlationId: expect.any(String), target: 'new', text: 'hello' })
    events.get('hermes:quick-entry:ack')!(other.event, { correlationId: forwarded.correlationId, result: { ok: true } })
    expect(h.relay.pendingCount()).toBe(1)
    events.get('hermes:quick-entry:ack')!(primary.event, {
      correlationId: forwarded.correlationId,
      result: { ok: true }
    })
    await expect(pending).resolves.toMatchObject({
      generation: h.session.generation,
      submitId: 1,
      correlationId: forwarded.correlationId
    })
    const completion = { correlationId: forwarded.correlationId, generation: h.session.generation, hide: true }
    expect(invoke('complete', other.event, completion)).toBe(false)
    expect(invoke('complete', quick.event, { ...completion, hide: 'yes' })).toBe(false)
    expect(invoke('complete', quick.event, completion)).toBe(true)
    expect(invoke('complete', quick.event, completion)).toBe(false)
    events.get('hermes:quick-entry:dismiss')!(other.event)
    expect(quick.win.hide).not.toHaveBeenCalled()
    events.get('hermes:quick-entry:dismiss')!(quick.event)
    expect(quick.win.hide).toHaveBeenCalledOnce()
    expect(invoke('session')?.nextSubmitId).toBe(2)
  })

  it('carries identities through the real preload and releases its exact late-result listener', async () => {
    const renderer = Object.assign(new EventEmitter(), {
      invoke: vi.fn(async () => true),
      sendSync: vi.fn(),
      send: vi.fn()
    })

    let api!: {
      getComposerSession: () => Promise<unknown>
      submit: (payload: unknown) => Promise<unknown>
      completeSubmit: (payload: unknown) => Promise<unknown>
      onLateResult: (callback: (payload: unknown) => void) => () => void
    }

    vi.doMock('electron', () => ({
      contextBridge: {
        exposeInMainWorld: (_name: string, bridge: { quickEntry: typeof api }) => {
          api = bridge.quickEntry
        }
      },
      ipcRenderer: renderer,
      webFrame: {},
      webUtils: {}
    }))

    try {
      await import('./preload')
      const request = { generation: 'g', submitId: 3, text: 'preserve identity', target: 'current' }
      const completion = { generation: 'g', correlationId: 'native-correlation', hide: false }
      await api.getComposerSession()
      await api.submit(request)
      await api.completeSubmit(completion)
      expect(renderer.invoke.mock.calls).toEqual([
        ['hermes:quick-entry:session'],
        ['hermes:quick-entry:submit', request],
        ['hermes:quick-entry:complete', completion]
      ])
      const callback = vi.fn()
      const off = api.onLateResult(callback)
      const result = { generation: 'g', submitId: 3, correlationId: 'native-correlation', ok: false }
      renderer.emit('hermes:quick-entry:late-result', {}, result)
      expect(callback).toHaveBeenCalledExactlyOnceWith(result)
      off()
      renderer.emit('hermes:quick-entry:late-result', {}, result)
      expect(callback).toHaveBeenCalledOnce()
      expect(renderer.listenerCount('hermes:quick-entry:late-result')).toBe(0)
    } finally {
      vi.doUnmock('electron')
      vi.resetModules()
    }
  })

  it('never focuses for late results, hidden windows or a replacement window', () => {
    const quick = nativeWindow(10)
    const primary = nativeWindow(1)
    const dock = vi.fn()
    const owner = { windowId: 10, generation: 'g', submitId: 1 }
    completeQuickEntryWindow(owner, false, quick.native, primary.native, dock)
    expect(quick.win.hide).toHaveBeenCalledOnce()
    expect(primary.win.focus).not.toHaveBeenCalled()
    quick.win.isVisible.mockReturnValue(false)
    completeQuickEntryWindow(owner, true, quick.native, primary.native, dock)
    completeQuickEntryWindow(owner, true, nativeWindow(11).native, primary.native, dock)
    expect(primary.win.focus).not.toHaveBeenCalled()
    quick.win.isVisible.mockReturnValue(true)
    completeQuickEntryWindow(owner, true, quick.native, primary.native, dock)
    expect(primary.win.focus).toHaveBeenCalledOnce()
    expect(dock).toHaveBeenCalledOnce()
  })
})

function fakeGlobalShortcut(options: { register?: boolean; taken?: string[] } = {}) {
  const held = new Set(options.taken ?? [])

  const globalShortcut: GlobalShortcutLike = {
    isRegistered: vi.fn((accelerator: string) => held.has(accelerator)),
    register: vi.fn((accelerator: string) => {
      if (options.register === false) {
        return false
      }

      held.add(accelerator)

      return true
    }),
    unregister: vi.fn((accelerator: string) => void held.delete(accelerator))
  }

  return { globalShortcut, held }
}

describe('parseQuickEntryShortcut', () => {
  it('normalizes casing, aliases, and modifier order', () => {
    expect(parseQuickEntryShortcut('cmdorctrl+shift+space')).toEqual({
      accelerator: 'CommandOrControl+Shift+Space',
      ok: true
    })
    expect(parseQuickEntryShortcut('  Shift + CTRL + k ')).toEqual({ accelerator: 'Control+Shift+K', ok: true })
    expect(parseQuickEntryShortcut('Alt+f5')).toEqual({ accelerator: 'Alt+F5', ok: true })
    expect(parseQuickEntryShortcut('Meta+/')).toEqual({ accelerator: 'Super+/', ok: true })
  })

  it('collapses duplicate modifiers', () => {
    expect(parseQuickEntryShortcut('Ctrl+Control+Shift+J')).toEqual({ accelerator: 'Control+Shift+J', ok: true })
  })

  it('requires a modifier so a global bind cannot swallow a bare key', () => {
    expect(parseQuickEntryShortcut('K')).toEqual({ ok: false, reason: 'no-modifier' })
    expect(parseQuickEntryShortcut('Space')).toEqual({ ok: false, reason: 'no-modifier' })
  })

  it('requires exactly one non-modifier key', () => {
    expect(parseQuickEntryShortcut('Shift+Control')).toEqual({ ok: false, reason: 'no-key' })
    expect(parseQuickEntryShortcut('Shift+A+B')).toEqual({ ok: false, reason: 'invalid-key' })
    expect(parseQuickEntryShortcut('A+Shift')).toEqual({ ok: false, reason: 'invalid-modifier' })
  })

  it('rejects empty, junk, and the reserved Escape key', () => {
    expect(parseQuickEntryShortcut('')).toEqual({ ok: false, reason: 'empty' })
    expect(parseQuickEntryShortcut('   ')).toEqual({ ok: false, reason: 'empty' })
    expect(parseQuickEntryShortcut(null)).toEqual({ ok: false, reason: 'empty' })
    expect(parseQuickEntryShortcut('Ctrl+NotAKey')).toEqual({ ok: false, reason: 'invalid-key' })
    // Escape hides the window; binding it globally would make it un-toggleable.
    expect(parseQuickEntryShortcut('Ctrl+Escape')).toEqual({ ok: false, reason: 'reserved' })
  })

  it('accepts the shipped default unchanged', () => {
    expect(parseQuickEntryShortcut(DEFAULT_QUICK_ENTRY_SHORTCUT)).toEqual({
      accelerator: DEFAULT_QUICK_ENTRY_SHORTCUT,
      ok: true
    })
  })
})

describe('sanitizeQuickEntrySettings', () => {
  it('defaults to enabled with the default shortcut', () => {
    expect(sanitizeQuickEntrySettings(undefined)).toEqual({ enabled: true, shortcut: DEFAULT_QUICK_ENTRY_SHORTCUT })
    expect(sanitizeQuickEntrySettings('not an object')).toEqual({
      enabled: true,
      shortcut: DEFAULT_QUICK_ENTRY_SHORTCUT
    })
  })

  it('keeps an explicit disable and normalizes a stored shortcut', () => {
    expect(sanitizeQuickEntrySettings({ enabled: false, shortcut: 'alt+j' })).toEqual({
      enabled: false,
      shortcut: 'Alt+J'
    })
  })

  it('falls back to the default when the stored shortcut is unusable', () => {
    expect(sanitizeQuickEntrySettings({ enabled: true, shortcut: 'Q' })).toEqual({
      enabled: true,
      shortcut: DEFAULT_QUICK_ENTRY_SHORTCUT
    })
  })

  it('treats a non-boolean enabled as off (only `true` opts in once present)', () => {
    expect(sanitizeQuickEntrySettings({ enabled: 'yes' }).enabled).toBe(false)
  })
})

describe('createQuickEntryShortcut', () => {
  it('registers the normalized accelerator when enabled', () => {
    const { globalShortcut } = fakeGlobalShortcut()
    const onTrigger = vi.fn()
    const controller = createQuickEntryShortcut(globalShortcut, onTrigger)

    const state = controller.apply({ enabled: true, shortcut: 'cmdorctrl+shift+space' })

    expect(state).toEqual({ error: null, registered: true, shortcut: 'CommandOrControl+Shift+Space' })
    expect(globalShortcut.register).toHaveBeenCalledWith('CommandOrControl+Shift+Space', onTrigger)
    expect(controller.current()).toEqual(state)
  })

  it('never registers while the setting is disabled', () => {
    const { globalShortcut } = fakeGlobalShortcut()
    const controller = createQuickEntryShortcut(globalShortcut, vi.fn())

    const state = controller.apply({ enabled: false, shortcut: DEFAULT_QUICK_ENTRY_SHORTCUT })

    expect(globalShortcut.register).not.toHaveBeenCalled()
    expect(state).toEqual({ error: null, registered: false, shortcut: DEFAULT_QUICK_ENTRY_SHORTCUT })
  })

  it('releases the old accelerator before registering a new one', () => {
    const { globalShortcut, held } = fakeGlobalShortcut()
    const controller = createQuickEntryShortcut(globalShortcut, vi.fn())

    controller.apply({ enabled: true, shortcut: 'Alt+J' })
    controller.apply({ enabled: true, shortcut: 'Alt+K' })

    expect(globalShortcut.unregister).toHaveBeenCalledWith('Alt+J')
    expect(held.has('Alt+J')).toBe(false)
    expect(held.has('Alt+K')).toBe(true)
  })

  it('turning the feature off releases the live accelerator', () => {
    const { globalShortcut, held } = fakeGlobalShortcut()
    const controller = createQuickEntryShortcut(globalShortcut, vi.fn())

    controller.apply({ enabled: true, shortcut: 'Alt+J' })
    const off = controller.apply({ enabled: false, shortcut: 'Alt+J' })

    expect(globalShortcut.unregister).toHaveBeenCalledWith('Alt+J')
    expect(held.size).toBe(0)
    expect(off.registered).toBe(false)
    expect(off.error).toBeNull()
  })

  it("surfaces 'taken' when another app already owns the chord", () => {
    const { globalShortcut } = fakeGlobalShortcut({ taken: ['Alt+J'] })
    const controller = createQuickEntryShortcut(globalShortcut, vi.fn())

    const state = controller.apply({ enabled: true, shortcut: 'alt+j' })

    expect(globalShortcut.register).not.toHaveBeenCalled()
    expect(state).toEqual({ error: 'taken', registered: false, shortcut: 'Alt+J' })
  })

  it("surfaces 'taken' when the OS refuses the registration", () => {
    const { globalShortcut } = fakeGlobalShortcut({ register: false })
    const controller = createQuickEntryShortcut(globalShortcut, vi.fn())

    expect(controller.apply({ enabled: true, shortcut: 'Alt+J' })).toEqual({
      error: 'taken',
      registered: false,
      shortcut: 'Alt+J'
    })
  })

  it("surfaces 'invalid' for an unusable shortcut without asking the OS", () => {
    const { globalShortcut } = fakeGlobalShortcut()
    const controller = createQuickEntryShortcut(globalShortcut, vi.fn())

    expect(controller.apply({ enabled: true, shortcut: 'J' })).toEqual({
      error: 'invalid',
      registered: false,
      shortcut: 'J'
    })
    expect(globalShortcut.register).not.toHaveBeenCalled()
  })

  it('survives a throwing globalShortcut', () => {
    const globalShortcut: GlobalShortcutLike = {
      isRegistered: () => false,
      register: () => {
        throw new Error('x11 grab failed')
      },
      unregister: () => {}
    }

    const controller = createQuickEntryShortcut(globalShortcut, vi.fn())

    expect(controller.apply({ enabled: true, shortcut: 'Alt+J' }).error).toBe('taken')
  })

  it('dispose releases the accelerator and is idempotent', () => {
    const { globalShortcut, held } = fakeGlobalShortcut()
    const controller = createQuickEntryShortcut(globalShortcut, vi.fn())

    controller.apply({ enabled: true, shortcut: 'Alt+J' })
    controller.dispose()
    controller.dispose()

    expect(globalShortcut.unregister).toHaveBeenCalledTimes(1)
    expect(held.size).toBe(0)
    expect(controller.current().registered).toBe(false)
  })
})

describe('quickEntryWindowBounds', () => {
  it('centers horizontally and sits below the top edge of the work area', () => {
    const bounds = quickEntryWindowBounds({ height: 1000, width: 1600, x: 0, y: 0 })

    expect(bounds.width).toBe(640)
    expect(bounds.x).toBe((1600 - 640) / 2)
    expect(bounds.y).toBeGreaterThan(0)
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(1000)
  })

  it('respects a display origin offset (second monitor)', () => {
    const bounds = quickEntryWindowBounds({ height: 900, width: 1440, x: 1600, y: -200 })

    expect(bounds.x).toBe(1600 + (1440 - 640) / 2)
    expect(bounds.y).toBeGreaterThanOrEqual(-200)
  })

  it('stays inside a tiny work area', () => {
    const bounds = quickEntryWindowBounds({ height: 120, width: 320, x: 0, y: 0 })

    expect(bounds.width).toBeLessThanOrEqual(320)
    expect(bounds.height).toBeLessThanOrEqual(120)
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(120)
  })

  it('falls back to the origin without a work area', () => {
    expect(quickEntryWindowBounds()).toEqual({ height: 168, width: 640, x: 0, y: 0 })
  })
})
