import type { BrowserWindow, IpcMain, IpcMainEvent, IpcMainInvokeEvent } from 'electron'

/**
 * Quick Entry — the global-hotkey mini composer.
 *
 * A small frameless always-on-top window that a global shortcut summons from
 * anywhere so the user can fire a prompt at Hermes without raising the whole
 * app. The window carries NO gateway connection of its own: it forwards the
 * text to the primary renderer, which sends it through the SAME prompt-submit
 * path the normal composer uses (see app/contrib/hooks/use-quick-entry-bridge).
 *
 * Everything Electron-free lives here so the parts that actually break a user —
 * accelerator validation, "disabled means never register", and surfacing a
 * shortcut another app already owns — are unit-testable without booting
 * Electron. main.ts owns the BrowserWindow, the file I/O, and the real
 * `globalShortcut`.
 */

// Default matches the muscle memory of the apps this ports from (Claude
// Desktop's quick entry / ChatGPT's Quick Chat sit on a Cmd+Shift chord).
const DEFAULT_QUICK_ENTRY_SHORTCUT = 'CommandOrControl+Shift+Space'

// Compact capture surface: wide enough for a sentence, short enough to read as
// a HUD rather than a second app window. Height covers the composer row plus
// the session-target picker row; the renderer never grows the OS window in v1.
const QUICK_ENTRY_WINDOW_WIDTH = 640
const QUICK_ENTRY_WINDOW_HEIGHT = 168

// Spotlight-ish placement: horizontally centered on the active display, a
// comfortable fraction down from the top rather than dead center.
const QUICK_ENTRY_TOP_FRACTION = 0.22

export interface QuickEntrySubmitRelayResult {
  ok: boolean
  [key: string]: unknown
}

export interface QuickEntryComposerSession {
  generation: string
  nextSubmitId: number
}

export interface QuickEntrySubmitOwner {
  windowId: number
  generation: string
  submitId: number
}

export interface QuickEntryRelayReply extends QuickEntrySubmitRelayResult {
  correlationId?: string
  generation: string
  submitId: number
}

interface RelayRequest {
  owner: QuickEntrySubmitOwner
  resolve: (result: QuickEntryRelayReply) => void
  timer: ReturnType<typeof setTimeout>
}

interface AcceptedSubmit {
  owner: QuickEntrySubmitOwner
  late: boolean
  timer: ReturnType<typeof setTimeout>
}

interface QuickEntryRelayOptions {
  onLateResult: (owner: QuickEntrySubmitOwner, result: QuickEntryRelayReply) => void
  onComplete: (owner: QuickEntrySubmitOwner, focusPrimary: boolean) => void
  timeoutMs?: number
  completionTimeoutMs?: number
}

/** Main owns document generations. Reading a session never resets its counter
 * or invalidates unresolved requests; only verified native lifecycle does. */
class QuickEntrySubmitRelay {
  private readonly sessions = new Map<number, { generation: string; highWater: number }>()
  private readonly pending = new Map<string, RelayRequest>()
  private readonly reconcilable = new Map<string, QuickEntrySubmitOwner>()
  private readonly accepted = new Map<string, AcceptedSubmit>()
  private sequence = 0
  private generationSequence = 0

  constructor(private readonly options: QuickEntryRelayOptions) {}

  openDocument(windowId: number): QuickEntryComposerSession {
    this.disposeWindow(windowId)
    this.sessions.set(windowId, { generation: `qe-document-${++this.generationSequence}`, highWater: 0 })

    return this.getSession(windowId)!
  }

  getSession(windowId: number): QuickEntryComposerSession | null {
    const session = this.sessions.get(windowId)

    return session ? { generation: session.generation, nextSubmitId: session.highWater + 1 } : null
  }

  private ownsDocument(owner: QuickEntrySubmitOwner): boolean {
    return this.sessions.get(owner.windowId)?.generation === owner.generation
  }

  private reply(
    owner: QuickEntrySubmitOwner,
    result: QuickEntrySubmitRelayResult,
    correlationId?: string
  ): QuickEntryRelayReply {
    return { ...result, correlationId, generation: owner.generation, submitId: owner.submitId }
  }

  begin(owner: QuickEntrySubmitOwner, forward: (correlationId: string) => void): Promise<QuickEntryRelayReply> {
    const session = this.sessions.get(owner.windowId)

    if (
      !this.ownsDocument(owner) ||
      !Number.isSafeInteger(owner.submitId) ||
      owner.submitId <= (session?.highWater ?? 0)
    ) {
      return Promise.resolve(
        this.reply(owner, {
          code: 'invalid-submit',
          message: 'Quick Entry submission identity is stale or invalid.',
          ok: false,
          retryable: false
        })
      )
    }

    session!.highWater = owner.submitId
    const correlationId = ['qe', Date.now(), ++this.sequence].join('-')

    return new Promise(resolve => {
      const timer = setTimeout(() => this.timeout(correlationId), this.options.timeoutMs ?? 15_000)
      this.pending.set(correlationId, { owner, resolve, timer })

      try {
        forward(correlationId)
      } catch {
        // A synchronous send failure did not reach the primary renderer.
        this.acknowledge(correlationId, {
          code: 'forward-failed',
          message: 'Quick Entry could not reach Hermes.',
          ok: false,
          retryable: true
        })
      }
    })
  }

  private timeout(correlationId: string): void {
    const request = this.pending.get(correlationId)

    if (!request) {
      return
    }

    this.pending.delete(correlationId)
    this.reconcilable.set(correlationId, request.owner)
    request.resolve(
      this.reply(
        request.owner,
        {
          code: 'timeout',
          message: 'Hermes has not confirmed the prompt yet — it may still be delivered.',
          ok: false,
          retryable: false
        },
        correlationId
      )
    )
  }

  acknowledge(correlationId: string, result: QuickEntrySubmitRelayResult): void {
    if (!result || typeof result.ok !== 'boolean') {
      return
    }

    const request = this.pending.get(correlationId)
    const owner = request?.owner ?? this.reconcilable.get(correlationId)

    if (!owner || !this.ownsDocument(owner)) {
      return
    }

    this.pending.delete(correlationId)
    this.reconcilable.delete(correlationId)

    if (request) {
      clearTimeout(request.timer)
    }

    if (result.ok) {
      // This timer revokes only the ability to hide/focus. It never discards
      // unresolved text, cancels a backend submit, or invites a retry.
      const timer = setTimeout(() => this.accepted.delete(correlationId), this.options.completionTimeoutMs ?? 30_000)
      this.accepted.set(correlationId, { late: !request, owner, timer })
    }

    const reply = this.reply(owner, result, correlationId)

    if (request) {
      request.resolve(reply)
    } else {
      this.options.onLateResult(owner, reply)
    }
  }

  complete(windowId: number, generation: string, correlationId: string, hide: boolean): boolean {
    const accepted = this.accepted.get(correlationId)

    if (
      !accepted ||
      accepted.owner.windowId !== windowId ||
      accepted.owner.generation !== generation ||
      !this.ownsDocument(accepted.owner)
    ) {
      return false
    }

    // Older accepted results may release their capability with hide=false.
    // Only the newest request can hide the window or move native focus.
    if (hide && this.sessions.get(windowId)?.highWater !== accepted.owner.submitId) {
      return false
    }

    clearTimeout(accepted.timer)
    this.accepted.delete(correlationId)

    if (hide) {
      this.options.onComplete(accepted.owner, !accepted.late)
    }

    return true
  }

  disposeWindow(windowId: number): void {
    this.sessions.delete(windowId)

    for (const [id, request] of this.pending) {
      if (request.owner.windowId !== windowId) {
        continue
      }

      clearTimeout(request.timer)
      this.pending.delete(id)
      request.resolve(this.reply(request.owner, { code: 'disposed', ok: false, retryable: false }, id))
    }

    for (const [id, owner] of this.reconcilable) {
      if (owner.windowId === windowId) {
        this.reconcilable.delete(id)
      }
    }

    for (const [id, accepted] of this.accepted) {
      if (accepted.owner.windowId !== windowId) {
        continue
      }

      clearTimeout(accepted.timer)
      this.accepted.delete(id)
    }
  }

  pendingCount(): number {
    return this.pending.size
  }
  reconcilableCount(): number {
    return this.reconcilable.size
  }
  acceptedCount(): number {
    return this.accepted.size
  }
}

export function createQuickEntrySubmitRelay(options: QuickEntryRelayOptions): QuickEntrySubmitRelay {
  return new QuickEntrySubmitRelay(options)
}

/** Commit is the document boundary: a started/aborted navigation still leaves
 * the old editor alive. No session is exposed for the initial about:blank. */
export function bindQuickEntryComposerDocument(win: BrowserWindow, relay: QuickEntrySubmitRelay): void {
  const windowId = win.webContents.id
  win.webContents.on('did-navigate', () => relay.openDocument(windowId))
  win.webContents.on('render-process-gone', () => relay.disposeWindow(windowId))
  win.on('closed', () => relay.disposeWindow(windowId))
}

interface QuickEntryIpcOptions {
  quickWindow: () => BrowserWindow | null
  primaryWindow: () => BrowserWindow | null
  relay: QuickEntrySubmitRelay
}

function isQuickEntrySender(event: IpcMainEvent | IpcMainInvokeEvent, win: BrowserWindow | null): boolean {
  return !!win && !win.isDestroyed() && event.sender === win.webContents && event.senderFrame === event.sender.mainFrame
}

/** These are the real registered IPC handlers, kept Electron-runtime-free so
 * origin, generation and completion checks are tested at the actual seam. */
export function registerQuickEntrySubmitIpc(ipc: Pick<IpcMain, 'handle' | 'on'>, options: QuickEntryIpcOptions): void {
  const { relay } = options
  ipc.handle('hermes:quick-entry:session', event => {
    return isQuickEntrySender(event, options.quickWindow()) ? relay.getSession(event.sender.id) : null
  })
  ipc.handle('hermes:quick-entry:complete', (event, payload) => {
    if (!isQuickEntrySender(event, options.quickWindow()) || typeof payload?.hide !== 'boolean') {
      return false
    }

    return relay.complete(event.sender.id, payload.generation, payload.correlationId, payload.hide)
  })
  ipc.handle('hermes:quick-entry:submit', (event, payload) => {
    const identity = { generation: payload?.generation, submitId: payload?.submitId }

    if (!isQuickEntrySender(event, options.quickWindow())) {
      return {
        ...identity,
        code: 'forbidden',
        message: 'Quick Entry sender is not authorized.',
        ok: false,
        retryable: false
      }
    }

    const text = typeof payload?.text === 'string' ? payload.text.trim() : ''

    if (!text) {
      return { ...identity, code: 'empty', message: 'Enter a prompt before submitting.', ok: false, retryable: false }
    }

    const primary = options.primaryWindow()

    if (!primary || primary.isDestroyed()) {
      return {
        ...identity,
        code: 'no-primary',
        message: 'The primary Hermes window is unavailable.',
        ok: false,
        retryable: true
      }
    }

    const target = typeof payload?.target === 'string' && payload.target ? payload.target : 'current'

    return relay.begin({ ...identity, windowId: event.sender.id }, correlationId => {
      primary.webContents.send('hermes:quick-entry:submit', { correlationId, target, text })
    })
  })
  ipc.on('hermes:quick-entry:ack', (event, payload) => {
    if (!isQuickEntrySender(event, options.primaryWindow())) {
      return
    }

    relay.acknowledge(payload?.correlationId, payload?.result)
  })
  ipc.on('hermes:quick-entry:dismiss', event => {
    const win = options.quickWindow()

    if (isQuickEntrySender(event, win)) {
      win!.hide()
    }
  })
}

export function completeQuickEntryWindow(
  owner: QuickEntrySubmitOwner,
  focusPrimary: boolean,
  quickWindow: BrowserWindow | null,
  primaryWindow: BrowserWindow | null,
  showDock: () => void
): void {
  if (
    !quickWindow ||
    quickWindow.isDestroyed() ||
    quickWindow.webContents.id !== owner.windowId ||
    !quickWindow.isVisible()
  ) {
    return
  }

  quickWindow.hide()

  if (!focusPrimary) {
    return
  }

  showDock()

  if (primaryWindow && !primaryWindow.isDestroyed()) {
    primaryWindow.show()
    primaryWindow.focus()
  }
}

// Electron accelerator vocabulary (electronjs.org/docs/latest/api/accelerator).
// Kept as data so validation and the settings UI agree on one list.
const ACCELERATOR_MODIFIERS = new Set([
  'alt',
  'altgr',
  'cmd',
  'cmdorctrl',
  'command',
  'commandorcontrol',
  'control',
  'ctrl',
  'meta',
  'option',
  'shift',
  'super'
])

const ACCELERATOR_KEYS = new Set([
  'backspace',
  'delete',
  'down',
  'end',
  'enter',
  'escape',
  'home',
  'insert',
  'left',
  'medianexttrack',
  'mediaplaypause',
  'mediaprevioustrack',
  'mediastop',
  'pagedown',
  'pageup',
  'plus',
  'printscreen',
  'return',
  'right',
  'space',
  'tab',
  'up',
  'volumedown',
  'volumemute',
  'volumeup'
])

// Single printable characters Electron accepts verbatim, plus 0-9 / A-Z below.
const ACCELERATOR_PUNCTUATION = new Set([
  '!',
  '"',
  '#',
  '$',
  '%',
  '&',
  "'",
  '(',
  ')',
  '*',
  '+',
  ',',
  '-',
  '.',
  '/',
  ':',
  ';',
  '<',
  '=',
  '>',
  '?',
  '@',
  '[',
  '\\',
  ']',
  '^',
  '_',
  '`',
  '{',
  '|',
  '}',
  '~'
])

/** Why a shortcut string was rejected. The renderer maps these to copy. */
export type QuickEntryShortcutError =
  'empty' | 'invalid-key' | 'invalid-modifier' | 'no-key' | 'no-modifier' | 'reserved'

export type QuickEntryShortcutParse = { ok: false; reason: QuickEntryShortcutError } | { accelerator: string; ok: true }

function isAcceleratorKey(token: string): boolean {
  if (ACCELERATOR_KEYS.has(token)) {
    return true
  }

  if (/^f([1-9]|1[0-9]|2[0-4])$/.test(token)) {
    return true
  }

  if (/^num(?:[0-9]|lock|dec|add|sub|mult|div)$/.test(token)) {
    return true
  }

  return token.length === 1 && (/^[a-z0-9]$/.test(token) || ACCELERATOR_PUNCTUATION.has(token))
}

/**
 * Validate + normalize a user-typed accelerator.
 *
 * Rules beyond Electron's own grammar, both deliberate:
 * - At least one modifier. A bare global key steals that key from EVERY app.
 * - `Escape` can't be the key: inside the window Escape means "hide", so
 *   binding it globally would make the shortcut un-toggleable.
 */
export function parseQuickEntryShortcut(raw: unknown): QuickEntryShortcutParse {
  if (typeof raw !== 'string' || !raw.trim()) {
    return { ok: false, reason: 'empty' }
  }

  const parts = raw
    .split('+')
    .map(part => part.trim())
    .filter(Boolean)

  if (parts.length === 0) {
    return { ok: false, reason: 'empty' }
  }

  const modifiers: string[] = []
  let key: null | string = null

  for (const part of parts) {
    const lower = part.toLowerCase()

    if (ACCELERATOR_MODIFIERS.has(lower)) {
      if (key) {
        // A modifier after the key ("A+Shift") is not a valid accelerator.
        return { ok: false, reason: 'invalid-modifier' }
      }

      modifiers.push(lower)

      continue
    }

    if (key) {
      // Two non-modifier keys ("Shift+A+B").
      return { ok: false, reason: 'invalid-key' }
    }

    if (!isAcceleratorKey(lower)) {
      return { ok: false, reason: 'invalid-key' }
    }

    key = lower
  }

  if (!key) {
    return { ok: false, reason: 'no-key' }
  }

  if (modifiers.length === 0) {
    return { ok: false, reason: 'no-modifier' }
  }

  if (key === 'escape') {
    return { ok: false, reason: 'reserved' }
  }

  // Canonical casing so a saved shortcut round-trips identically no matter how
  // the user typed it, and duplicate modifiers collapse.
  const seen = new Set<string>()

  const normalizedModifiers = modifiers
    .map(modifier => CANONICAL_MODIFIER[modifier] ?? modifier)
    .filter(modifier => (seen.has(modifier) ? false : (seen.add(modifier), true)))
    // Stable display order (Electron itself is order-insensitive).
    .sort((left, right) => MODIFIER_ORDER.indexOf(left) - MODIFIER_ORDER.indexOf(right))

  return { accelerator: [...normalizedModifiers, canonicalKey(key)].join('+'), ok: true }
}

const CANONICAL_MODIFIER: Record<string, string> = {
  alt: 'Alt',
  altgr: 'AltGr',
  cmd: 'Command',
  cmdorctrl: 'CommandOrControl',
  command: 'Command',
  commandorcontrol: 'CommandOrControl',
  control: 'Control',
  ctrl: 'Control',
  meta: 'Super',
  option: 'Option',
  shift: 'Shift',
  super: 'Super'
}

const MODIFIER_ORDER = ['CommandOrControl', 'Command', 'Control', 'Super', 'Alt', 'Option', 'AltGr', 'Shift']

const CANONICAL_KEY: Record<string, string> = {
  backspace: 'Backspace',
  delete: 'Delete',
  down: 'Down',
  end: 'End',
  enter: 'Enter',
  escape: 'Escape',
  home: 'Home',
  insert: 'Insert',
  medianexttrack: 'MediaNextTrack',
  mediaplaypause: 'MediaPlayPause',
  mediaprevioustrack: 'MediaPreviousTrack',
  mediastop: 'MediaStop',
  pagedown: 'PageDown',
  pageup: 'PageUp',
  plus: 'Plus',
  printscreen: 'PrintScreen',
  return: 'Return',
  right: 'Right',
  space: 'Space',
  tab: 'Tab',
  up: 'Up',
  volumedown: 'VolumeDown',
  volumemute: 'VolumeMute',
  volumeup: 'VolumeUp',
  left: 'Left'
}

function canonicalKey(key: string): string {
  if (CANONICAL_KEY[key]) {
    return CANONICAL_KEY[key]
  }

  if (/^f([1-9]|1[0-9]|2[0-4])$/.test(key)) {
    return key.toUpperCase()
  }

  if (key.length === 1 && /^[a-z]$/.test(key)) {
    return key.toUpperCase()
  }

  return key
}

/** The persisted shape of `quick-entry.json` (main-process owned). */
export interface QuickEntrySettings {
  enabled: boolean
  shortcut: string
}

/**
 * Raw persisted JSON → usable settings. A malformed/absent file, or a shortcut
 * that no longer validates (hand-edited, or from a future build), falls back to
 * the default shortcut rather than leaving the feature un-summonable.
 */
export function sanitizeQuickEntrySettings(raw: unknown): QuickEntrySettings {
  const record = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const parsed = parseQuickEntryShortcut(record.shortcut)

  return {
    // Default ON: the feature is inert until the shortcut is pressed.
    enabled: record.enabled === undefined ? true : record.enabled === true,
    shortcut: parsed.ok ? parsed.accelerator : DEFAULT_QUICK_ENTRY_SHORTCUT
  }
}

/** The slice of Electron's `globalShortcut` we use (injected for testing). */
export interface GlobalShortcutLike {
  isRegistered(accelerator: string): boolean
  register(accelerator: string, callback: () => void): boolean
  unregister(accelerator: string): void
}

/**
 * What Settings shows. `registered` is the ground truth (we asked the OS);
 * `error` distinguishes "you turned it off" from "another app owns that chord",
 * which is the failure this feature must never swallow.
 */
export interface QuickEntryRegistration {
  error: null | QuickEntryRegistrationError
  registered: boolean
  shortcut: string
}

export type QuickEntryRegistrationError = 'invalid' | 'taken'

export interface QuickEntryShortcutController {
  /** Registration state as of the last apply. */
  current(): QuickEntryRegistration
  /** Release the shortcut (quit / feature off). Idempotent. */
  dispose(): void
  /** Re-register to match `settings`. Returns the resulting state. */
  apply(settings: QuickEntrySettings): QuickEntryRegistration
}

/**
 * Owns the one live global accelerator. Single resolver so every caller — boot,
 * the settings write, quit — gets the same answer and we can never leak two
 * registrations for one feature.
 *
 * Disabled settings never touch `register()` at all: a user who turned Quick
 * Entry off must not have their chord silently held hostage.
 */
export function createQuickEntryShortcut(
  globalShortcut: GlobalShortcutLike,
  onTrigger: () => void
): QuickEntryShortcutController {
  let active: null | string = null
  let state: QuickEntryRegistration = { error: null, registered: false, shortcut: DEFAULT_QUICK_ENTRY_SHORTCUT }

  const release = () => {
    if (active) {
      try {
        globalShortcut.unregister(active)
      } catch {
        // Best effort — a dead accelerator must not block a re-register.
      }

      active = null
    }
  }

  return {
    apply(settings) {
      const parsed = parseQuickEntryShortcut(settings.shortcut)
      const shortcut = parsed.ok ? parsed.accelerator : settings.shortcut

      release()

      if (!settings.enabled) {
        state = { error: null, registered: false, shortcut }

        return state
      }

      if (!parsed.ok) {
        state = { error: 'invalid', registered: false, shortcut }

        return state
      }

      // `isRegistered` catches the common conflict before we ask, and
      // `register()` returning false catches the rest (another process owns it
      // OS-wide). Both land in the same surfaced 'taken' state.
      let ok = false

      try {
        ok = globalShortcut.isRegistered(parsed.accelerator)
          ? false
          : globalShortcut.register(parsed.accelerator, onTrigger)
      } catch {
        ok = false
      }

      active = ok ? parsed.accelerator : null
      state = { error: ok ? null : 'taken', registered: ok, shortcut: parsed.accelerator }

      return state
    },
    current() {
      return state
    },
    dispose() {
      release()
      state = { ...state, error: null, registered: false }
    }
  }
}

/**
 * Where the quick window opens on a given display work area. Centered
 * horizontally, a fraction down from the top, and clamped so it stays fully
 * inside the work area on small/odd displays.
 */
export function quickEntryWindowBounds(workArea?: { height: number; width: number; x: number; y: number }): {
  height: number
  width: number
  x: number
  y: number
} {
  const width = Math.min(QUICK_ENTRY_WINDOW_WIDTH, workArea?.width ?? QUICK_ENTRY_WINDOW_WIDTH)
  const height = Math.min(QUICK_ENTRY_WINDOW_HEIGHT, workArea?.height ?? QUICK_ENTRY_WINDOW_HEIGHT)

  if (!workArea) {
    return { height, width, x: 0, y: 0 }
  }

  const x = Math.round(workArea.x + (workArea.width - width) / 2)
  const maxY = workArea.y + workArea.height - height
  const y = Math.round(Math.min(Math.max(workArea.y, workArea.y + workArea.height * QUICK_ENTRY_TOP_FRACTION), maxY))

  return { height, width, x, y }
}

export { DEFAULT_QUICK_ENTRY_SHORTCUT, QUICK_ENTRY_TOP_FRACTION, QUICK_ENTRY_WINDOW_HEIGHT, QUICK_ENTRY_WINDOW_WIDTH }
