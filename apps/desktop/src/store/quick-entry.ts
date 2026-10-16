/**
 * Quick Entry (renderer side) — the mini composer's own state, and the
 * primary window's bridge back into the real prompt-submit path.
 *
 * The quick window carries NO gateway connection: it hands its text to the main
 * process, which forwards it to the primary renderer, which sends it through the
 * SAME `submitText` the normal composer uses (see
 * app/contrib/hooks/use-quick-entry-bridge). There is no second submit path and
 * no new gateway RPC.
 *
 * The device-local preference (enabled + shortcut) is authoritative in the MAIN
 * process — it owns the OS registration and must restore it on a cold launch
 * without the renderer ever visiting Settings. This module treats what the
 * bridge returns as the truth and caches it for the settings UI, same authority
 * split as keep-awake.
 */

import { atom } from 'nanostores'

export interface QuickEntryState {
  enabled: boolean
  /** null before the first read; the settings row shows a skeleton until then. */
  registered: boolean | null
  /** Why the OS shortcut isn't live: taken by another app, or unusable. */
  error: null | QuickEntryRegistrationError
  shortcut: string
}

export type QuickEntryRegistrationError = 'invalid' | 'taken'

export interface QuickEntryStatus {
  enabled: boolean
  error: null | QuickEntryRegistrationError
  registered: boolean
  shortcut: string
}

export const QUICK_ENTRY_DEFAULT_SHORTCUT = 'CommandOrControl+Shift+Space'

export const $quickEntry = atom<QuickEntryState>({
  enabled: true,
  error: null,
  registered: null,
  shortcut: QUICK_ENTRY_DEFAULT_SHORTCUT
})

function applyStatus(status: QuickEntryStatus | undefined): void {
  if (!status) {
    return
  }

  $quickEntry.set({
    enabled: status.enabled === true,
    error: status.error ?? null,
    registered: status.registered === true,
    shortcut: typeof status.shortcut === 'string' && status.shortcut ? status.shortcut : QUICK_ENTRY_DEFAULT_SHORTCUT
  })
}

/** True when the shell exposes the Quick Entry capability (desktop only). */
export function canUseQuickEntry(): boolean {
  return typeof window !== 'undefined' && typeof window.hermesDesktop?.quickEntry?.getSettings === 'function'
}

/** Read the live registration state into the store (Settings mount). */
export async function loadQuickEntrySettings(): Promise<void> {
  if (!canUseQuickEntry()) {
    return
  }

  try {
    applyStatus(await window.hermesDesktop.quickEntry.getSettings())
  } catch {
    // A failed read leaves the store as-is; the row keeps its last known copy.
  }
}

/**
 * Write a preference and adopt whatever the main process reports back — a
 * rejected shortcut or an already-taken chord comes back as an error state
 * instead of a silently-lost setting.
 */
export async function saveQuickEntrySettings(patch: { enabled?: boolean; shortcut?: string }): Promise<void> {
  if (!canUseQuickEntry()) {
    return
  }

  // Optimistic: paint the intent immediately, then let the authoritative reply
  // (which knows whether the OS accepted it) get the last word.
  const previous = $quickEntry.get()
  $quickEntry.set({ ...previous, ...patch, registered: previous.registered })

  try {
    applyStatus(await window.hermesDesktop.quickEntry.setSettings(patch))
  } catch {
    $quickEntry.set(previous)
  }
}

// ── Quick window submit state machine ───────────────────────────────────────

/** A recent session the quick window can target (pushed by the primary). */
export interface QuickEntrySessionOption {
  id: string
  title: string
}

/** Send into whatever chat the main window currently has in front. */
export const QUICK_TARGET_CURRENT = 'current'
/** Start a brand-new session for this prompt. */
export const QUICK_TARGET_NEW = 'new'

/**
 * The primary renderer's push into the quick window: is the gateway usable, and
 * which recent sessions can be targeted. The quick window has NO gateway of its
 * own, so this pushed copy is its only view of backend truth — it starts
 * disconnected (input disabled) until the first push proves otherwise.
 */
export interface QuickEntryStatePush {
  connected: boolean
  sessions: QuickEntrySessionOption[]
}

/** What a quick-window submit carries back to the primary renderer. */
export interface QuickEntrySubmitPayload {
  /** QUICK_TARGET_CURRENT, QUICK_TARGET_NEW, or a stored session id. */
  target: string
  text: string
}

export interface QuickEntryComposerSession {
  generation: string
  nextSubmitId: number
}

export interface QuickEntrySubmitRequest extends QuickEntrySubmitPayload {
  generation: string
  submitId: number
}

export interface QuickEntryCorrelatedResult extends QuickEntrySubmitResult {
  correlationId?: string
  generation: string
  submitId: number
}

export interface QuickEntrySubmitResult {
  ok: boolean
  code?: string
  message?: string
  retryable?: boolean
  sessionId?: string | null
  runtimeSessionId?: string | null
}

/**
 * The quick window's own composer state. Deliberately a tiny pure reducer: the
 * behavior that would actually break a user — an empty submit must not send but
 * must still not hide the window, a real submit clears the draft AND hides, a
 * double-fire while already submitting must not send twice, and a dead gateway
 * must disable sending entirely — is the part worth proving, and none of it
 * needs React or Electron.
 */
export interface QuickComposerState {
  /** Last pushed gateway truth. False (the initial value) disables submit. */
  connected: boolean
  draft: string
  /** Recent sessions the picker offers, pushed by the primary renderer. */
  sessions: QuickEntrySessionOption[]
  /** True between a send and its acknowledgement. Blocks a double-send. */
  submitting: boolean
  /** Inline delivery failure retained with the draft until retry. */
  error: null | string
  /** Local correlation for ignoring an acknowledgement from an older submit. */
  pendingSubmitId: number | null
  /** Text of the in-flight submit, kept so a late failure can restore it. */
  lastSubmitText: string
  /** Text owned by each unresolved submit. Terminal replies consume their own
   *  entry even when a newer submit already finished. */
  pendingSubmitTexts: Record<number, string>
  /** Superseded failures await a summon, then hand back their text once. Never
   *  evict an unseen failed draft to make room for a newer failure. */
  orphanedFailures: Array<{ message: string; submitId: number; text: string }>
  /** A submit whose outcome is UNKNOWN (relay timeout): the backend may still
   *  have accepted it. Kept so a late acknowledgement can reconcile, and never
   *  presented as retryable until non-acceptance is proven. */
  unknownSubmitId: number | null
  /** All timed-out requests, including ones superseded before their timeout. */
  unknownSubmitIds: number[]
  /** Where a submit lands: current / new / a stored session id. */
  target: string
  /** Whether the window should be visible. False asks the shell to hide. */
  visible: boolean
}

export type QuickComposerEvent =
  | { type: 'blur' }
  | { type: 'dismiss' }
  | { type: 'edit'; draft: string }
  | { type: 'shown' }
  | { type: 'state'; connected: boolean; sessions: QuickEntrySessionOption[] }
  | { type: 'submit'; submitId?: number }
  | { type: 'submit-error'; message: string; submitId: number }
  | { message: string; submitId: number; type: 'submit-unknown' }
  | { message: string; ok: boolean; submitId: number; type: 'late-result' }
  | { type: 'submit-ok'; submitId: number }
  | { type: 'target'; target: string }

/**
 * Map a relay result to the composer event that reconciles it. A timeout is an
 * UNKNOWN outcome — the prompt may already be accepted — so it must never be
 * mapped to a retryable failure.
 */
export function quickEntryResultEvent(result: QuickEntrySubmitResult, submitId: number): QuickComposerEvent {
  if (result.ok) {
    return { submitId, type: 'submit-ok' }
  }

  if (result.code === 'timeout') {
    return {
      message: result.message || 'Hermes has not confirmed the prompt yet — it may still be delivered.',
      submitId,
      type: 'submit-unknown'
    }
  }

  return {
    message: result.message || 'Quick Entry could not deliver the prompt.',
    submitId,
    type: 'submit-error'
  }
}

export interface QuickComposerTransition {
  /** Payload to send through the real prompt-submit path, or null for none. */
  send: null | QuickEntrySubmitPayload
  state: QuickComposerState
}

export const initialQuickComposerState: QuickComposerState = {
  // Disconnected until the primary renderer's first push proves otherwise — a
  // capture window that accepts text it can never deliver is a lie.
  connected: false,
  draft: '',
  error: null,
  lastSubmitText: '',
  orphanedFailures: [],
  pendingSubmitId: null,
  pendingSubmitTexts: {},
  sessions: [],
  submitting: false,
  target: QUICK_TARGET_CURRENT,
  unknownSubmitId: null,
  unknownSubmitIds: [],
  visible: true
}

const UNKNOWN_DELIVERY_MESSAGE = 'Hermes has not confirmed an earlier prompt yet — it may still be delivered.'

function consumeSubmitText(state: QuickComposerState, submitId: number): Record<number, string> {
  const pending = { ...state.pendingSubmitTexts }

  delete pending[submitId]

  return pending
}

function completeSubmit(state: QuickComposerState, submitId: number): QuickComposerState {
  // The acknowledgement owns the submitted text, not edits made since that
  // submit. A successful send must not clear or hide a newer unsent draft.
  const edited = state.draft.trim() !== state.lastSubmitText

  return {
    ...state,
    draft: edited ? state.draft : '',
    error: state.unknownSubmitIds.some(id => id !== submitId) ? UNKNOWN_DELIVERY_MESSAGE : null,
    lastSubmitText: '',
    pendingSubmitId: null,
    pendingSubmitTexts: consumeSubmitText(state, submitId),
    submitting: false,
    unknownSubmitId: null,
    unknownSubmitIds: state.unknownSubmitIds.filter(id => id !== submitId),
    visible: edited ? state.visible : false
  }
}

function dismissQuickComposer(state: QuickComposerState): QuickComposerTransition {
  // Escape / focus loss discards a surface with nothing unresolved. A submit
  // already handed to main keeps its correlation and draft: the promise
  // still resolves, and a late failure must be able to restore the text.
  const unresolved = state.submitting || Object.keys(state.pendingSubmitTexts).length > 0

  return {
    send: null,
    state: unresolved
      ? { ...state, error: null, visible: false }
      : {
          ...state,
          draft: '',
          error: null,
          lastSubmitText: '',
          pendingSubmitId: null,
          submitting: false,
          target: QUICK_TARGET_CURRENT,
          unknownSubmitId: null,
          visible: false
        }
  }
}

function showQuickComposer(state: QuickComposerState): QuickComposerTransition {
  // Re-summoned. With a submit unresolved the window reconnects to the SAME
  // generation and shows the text still being delivered. Otherwise the
  // surface is fresh — except that a failure whose generation lost the
  // window hands its text back rather than dropping it.
  if (state.submitting || Object.keys(state.pendingSubmitTexts).length > 0) {
    return {
      send: null,
      state: { ...state, error: state.unknownSubmitIds.length > 0 ? UNKNOWN_DELIVERY_MESSAGE : null, visible: true }
    }
  }

  return {
    send: null,
    state: {
      ...state,
      draft: state.orphanedFailures[0]?.text ?? '',
      error: state.orphanedFailures[0]?.message ?? null,
      lastSubmitText: '',
      orphanedFailures: state.orphanedFailures.slice(1),
      pendingSubmitId: null,
      submitting: false,
      target: QUICK_TARGET_CURRENT,
      unknownSubmitId: null,
      visible: true
    }
  }
}

function applyQuickComposerState(
  state: QuickComposerState,
  event: Extract<QuickComposerEvent, { type: 'state' }>
): QuickComposerTransition {
  // Adopt the pushed truth. A selected session that no longer exists in the
  // pushed list must not silently swallow the prompt — fall back to current.
  const targetStillValid =
    event.connected &&
    (state.target === QUICK_TARGET_CURRENT ||
      state.target === QUICK_TARGET_NEW ||
      event.sessions.some(session => session.id === state.target))

  return {
    send: null,
    state: {
      ...state,
      connected: event.connected,
      sessions: event.sessions,
      target: targetStillValid ? state.target : QUICK_TARGET_CURRENT
    }
  }
}

function submitQuickComposer(
  state: QuickComposerState,
  event: Extract<QuickComposerEvent, { type: 'submit' }>
): QuickComposerTransition {
  const text = state.draft.trim()

  if (state.unknownSubmitIds.length > 0) {
    return { send: null, state: { ...state, error: UNKNOWN_DELIVERY_MESSAGE } }
  }

  // Nothing to send — or nowhere to send it (gateway down): stay open and
  // keep the draft so a stray Enter can't make the text vanish. Re-entering
  // changed text supersedes the pending generation; an unknown outcome never
  // invites a second delivery attempt.
  if (!text || !state.connected || (state.submitting && text === state.lastSubmitText)) {
    return { send: null, state }
  }

  return {
    send: { target: state.target, text },
    // Wait for main's result before clearing or hiding the capture window (#85590).
    state: {
      ...state,
      error: null,
      lastSubmitText: text,
      pendingSubmitId: event.submitId ?? null,
      pendingSubmitTexts:
        event.submitId !== undefined && event.submitId > 0
          ? { ...state.pendingSubmitTexts, [event.submitId]: text }
          : state.pendingSubmitTexts,
      submitting: true
    }
  }
}

function acceptQuickComposerSubmit(
  state: QuickComposerState,
  event: Extract<QuickComposerEvent, { type: 'submit-ok' }>
): QuickComposerTransition {
  // The owning generation — or a submit whose outcome was unknown — clears
  // the surface. A late success for a superseded generation is still good
  // news, but it must not wipe the draft the window now owns.
  if (event.submitId <= 0 || !Object.hasOwn(state.pendingSubmitTexts, event.submitId)) {
    return { send: null, state }
  }

  const owner = state.pendingSubmitId === event.submitId || state.unknownSubmitId === event.submitId

  return {
    send: null,
    state: owner
      ? completeSubmit(state, event.submitId)
      : {
          ...state,
          pendingSubmitTexts: consumeSubmitText(state, event.submitId),
          unknownSubmitIds: state.unknownSubmitIds.filter(id => id !== event.submitId),
          error:
            state.error === UNKNOWN_DELIVERY_MESSAGE && !state.unknownSubmitIds.some(id => id !== event.submitId)
              ? null
              : state.error
        }
  }
}

function markQuickComposerSubmitUnknown(
  state: QuickComposerState,
  event: Extract<QuickComposerEvent, { type: 'submit-unknown' }>
): QuickComposerTransition {
  if (!Object.hasOwn(state.pendingSubmitTexts, event.submitId) || state.unknownSubmitIds.includes(event.submitId)) {
    return { send: null, state }
  }

  const owner = state.pendingSubmitId === event.submitId

  return {
    send: null,
    state: {
      ...state,
      error: owner ? event.message : state.error,
      pendingSubmitId: owner ? null : state.pendingSubmitId,
      submitting: owner ? false : state.submitting,
      unknownSubmitId: owner ? event.submitId : state.unknownSubmitId,
      unknownSubmitIds: [...state.unknownSubmitIds, event.submitId]
    }
  }
}

function applyQuickComposerLateResult(
  state: QuickComposerState,
  event: Extract<QuickComposerEvent, { type: 'late-result' }>
): QuickComposerTransition {
  // A late native event can arrive before the timed-out invoke microtask.
  // Its authenticated request ID still owns this text in either ordering.
  if (!Object.hasOwn(state.pendingSubmitTexts, event.submitId)) {
    return { send: null, state }
  }

  return event.ok
    ? acceptQuickComposerSubmit(state, { submitId: event.submitId, type: 'submit-ok' })
    : rejectQuickComposerSubmit(state, { message: event.message, submitId: event.submitId, type: 'submit-error' })
}

function rejectQuickComposerSubmit(
  state: QuickComposerState,
  event: Extract<QuickComposerEvent, { type: 'submit-error' }>
): QuickComposerTransition {
  if (event.submitId <= 0 || !Object.hasOwn(state.pendingSubmitTexts, event.submitId)) {
    return { send: null, state }
  }

  const pendingSubmitTexts = consumeSubmitText(state, event.submitId)
  const unknownSubmitIds = state.unknownSubmitIds.filter(id => id !== event.submitId)
  const failedText = state.pendingSubmitTexts[event.submitId]
  const owner = state.pendingSubmitId === event.submitId || state.unknownSubmitId === event.submitId

  // An owning request can also lose the editor to a newer unsent edit. Keep
  // its failed text for handback without replacing what the user is typing.
  const orphanedFailures =
    !owner || state.draft.trim() !== failedText
      ? [...state.orphanedFailures, { message: event.message, submitId: event.submitId, text: failedText }]
      : state.orphanedFailures

  if (event.submitId > 0 && state.pendingSubmitId === event.submitId) {
    return {
      send: null,
      state: {
        ...state,
        error: event.message,
        lastSubmitText: '',
        orphanedFailures,
        pendingSubmitId: null,
        pendingSubmitTexts,
        unknownSubmitIds,
        submitting: false,
        visible: true
      }
    }
  }

  if (event.submitId > 0 && state.unknownSubmitId === event.submitId) {
    // Non-acceptance is now proven: drop the unknown correlation so a
    // retry is legitimate, and keep the text.
    return {
      send: null,
      state: {
        ...state,
        error: event.message,
        lastSubmitText: '',
        orphanedFailures,
        pendingSubmitTexts,
        unknownSubmitIds,
        unknownSubmitId: null
      }
    }
  }

  // Late failure whose generation no longer owns the window: keep the text
  // for the next summon and surface the message now if nothing else has.
  return {
    send: null,
    state: {
      ...state,
      error: state.error === UNKNOWN_DELIVERY_MESSAGE ? event.message : (state.error ?? event.message),
      orphanedFailures,
      pendingSubmitTexts,
      unknownSubmitIds
    }
  }
}

export function quickComposerReducer(state: QuickComposerState, event: QuickComposerEvent): QuickComposerTransition {
  switch (event.type) {
    case 'blur':
    case 'dismiss': {
      return dismissQuickComposer(state)
    }

    case 'edit': {
      return { send: null, state: { ...state, draft: event.draft } }
    }

    case 'shown': {
      return showQuickComposer(state)
    }

    case 'state': {
      return applyQuickComposerState(state, event)
    }

    case 'submit': {
      return submitQuickComposer(state, event)
    }

    case 'submit-ok': {
      return acceptQuickComposerSubmit(state, event)
    }

    case 'submit-unknown': {
      return markQuickComposerSubmitUnknown(state, event)
    }

    case 'late-result': {
      return applyQuickComposerLateResult(state, event)
    }

    case 'submit-error': {
      return rejectQuickComposerSubmit(state, event)
    }

    case 'target': {
      return { send: null, state: { ...state, target: event.target } }
    }

    default: {
      return { send: null, state }
    }
  }
}

/** One controller belongs to one renderer document. View unmounts do not cancel
 * backend work or discard the text still owned by pending/unknown requests. */
export type QuickComposerApi = Pick<
  Window['hermesDesktop']['quickEntry'],
  'completeSubmit' | 'dismiss' | 'getComposerSession' | 'onLateResult' | 'onShown' | 'onState' | 'submit'
>

interface QuickComposerSnapshot {
  state: QuickComposerState
  ready: boolean
  sessionError: string | null
  focusVersion: number
}

export class QuickComposerController {
  private snapshot: QuickComposerSnapshot = {
    state: initialQuickComposerState,
    ready: false,
    sessionError: null,
    focusVersion: 0
  }
  private readonly listeners = new Set<() => void>()
  private readonly cleanups: Array<() => void> = []
  private session: QuickEntryComposerSession | null = null
  private handshake: Promise<void> | null = null
  private connected = false
  private disposed = false

  constructor(private readonly api: QuickComposerApi) {}

  getSnapshot = (): QuickComposerSnapshot => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)

    return () => this.listeners.delete(listener)
  }

  private publish(patch: Partial<QuickComposerSnapshot>): void {
    if (this.disposed) {
      return
    }

    this.snapshot = { ...this.snapshot, ...patch }

    for (const listener of this.listeners) {
      listener()
    }
  }

  connect = (): void => {
    if (this.connected || this.disposed) {
      return
    }

    this.connected = true
    this.cleanups.push(
      this.api.onShown(() => {
        this.dispatch({ type: 'shown' })
        this.publish({ focusVersion: this.snapshot.focusVersion + 1 })
      }),
      this.api.onState(payload =>
        this.dispatch({
          connected: payload?.connected === true,
          sessions: Array.isArray(payload?.sessions) ? payload.sessions : [],
          type: 'state'
        })
      ),
      this.api.onLateResult(result => this.receive(result, true))
    )
    void this.retrySession()
  }

  retrySession = (): Promise<void> => {
    if (this.disposed || this.session) {
      return Promise.resolve()
    }

    if (this.handshake) {
      return this.handshake
    }

    this.handshake = Promise.resolve()
      .then(() => this.api.getComposerSession())
      .then(session => {
        if (this.disposed) {
          return
        }

        if (
          !session ||
          !session.generation ||
          !Number.isSafeInteger(session.nextSubmitId) ||
          session.nextSubmitId <= 0
        ) {
          throw new Error('Quick Entry session unavailable')
        }

        this.session = { ...session }
        this.publish({ ready: true, sessionError: null })
      })
      .catch(() => {
        this.publish({ sessionError: 'Quick Entry could not connect. Press Enter to try again.' })
      })
      .finally(() => {
        this.handshake = null
      })

    return this.handshake
  }

  dispatch = (event: QuickComposerEvent): void => {
    if (this.disposed) {
      return
    }

    if (event.type === 'submit' && !this.session) {
      void this.retrySession()

      return
    }

    // Assign identity before dispatch, not from a mutable counter in a React
    // reducer. Rendering, StrictMode and effect replay cannot send a prompt.
    const dispatched = event.type === 'submit' ? { ...event, submitId: this.session!.nextSubmitId++ } : event
    const before = this.snapshot.state
    const transition = quickComposerReducer(before, dispatched)
    this.publish({ state: transition.state })

    if (transition.send && dispatched.type === 'submit') {
      void this.send(transition.send, dispatched.submitId!, this.session!.generation)
    } else if ((event.type === 'blur' || event.type === 'dismiss') && before.visible) {
      this.api.dismiss()
    }
  }

  private async send(payload: QuickEntrySubmitPayload, submitId: number, generation: string): Promise<void> {
    try {
      const result = await this.api.submit({ ...payload, generation, submitId })

      if (result.generation !== generation || result.submitId !== submitId) {
        return
      }

      this.receive(result, false)
    } catch {
      // An IPC rejection cannot establish whether the backend accepted text.
      // Keep it unknown rather than offering a potentially duplicate retry.
      if (!this.disposed && this.session?.generation === generation) {
        this.dispatch({
          message: 'Hermes has not confirmed delivery. Your prompt may still be delivered.',
          submitId,
          type: 'submit-unknown'
        })
      }
    }
  }

  private receive(result: QuickEntryCorrelatedResult, late: boolean): void {
    if (this.disposed || result.generation !== this.session?.generation) {
      return
    }

    const before = this.snapshot.state

    const event: QuickComposerEvent = late
      ? {
          message: result.message ?? 'Hermes could not deliver the prompt.',
          ok: result.ok,
          submitId: result.submitId,
          type: 'late-result'
        }
      : quickEntryResultEvent(result, result.submitId)

    const next = quickComposerReducer(before, event).state

    if (next === before) {
      return
    }

    this.publish({ state: next })

    if (result.ok && result.correlationId) {
      // Completion grants no backend powers: it only releases a one-use native
      // hide capability, with focus forbidden for late delivery confirmations.
      void this.api
        .completeSubmit({
          correlationId: result.correlationId,
          generation: result.generation,
          hide: before.visible && !next.visible
        })
        .catch(() => {
          // Delivery is already confirmed. A missed native hide is safe and its
          // capability expires; never retry the prompt or clear newer edits.
        })
    }
  }

  dispose = (): void => {
    if (this.disposed) {
      return
    }

    this.disposed = true

    for (const cleanup of this.cleanups) {
      cleanup()
    }

    this.cleanups.length = 0
    this.listeners.clear()
    this.session = null
    this.snapshot = { state: initialQuickComposerState, ready: false, sessionError: null, focusVersion: 0 }
  }
}

// ── Primary-renderer bridge ────────────────────────────────────────────────

let submitHandler: ((payload: QuickEntrySubmitPayload & { correlationId: string }) => void) | null = null
let unsubscribeSubmit: (() => void) | null = null

/**
 * Register the handler that turns a quick-window submit into a real send. The
 * primary window routes it by target: current chat → `submitText`, a stored
 * session id → resume + submit, new → fresh draft + submit.
 */
export function setQuickEntrySubmitHandler(
  fn: ((payload: QuickEntrySubmitPayload & { correlationId: string }) => void) | null
): void {
  submitHandler = fn
}

function normalizeSubmitPayload(raw: unknown): null | QuickEntrySubmitPayload {
  // Tolerate the v1 bare-string wire shape (an older quick window after a
  // partial update) by treating it as "send to the current chat".
  if (typeof raw === 'string') {
    return raw.trim() ? { target: QUICK_TARGET_CURRENT, text: raw } : null
  }

  if (!raw || typeof raw !== 'object') {
    return null
  }

  const record = raw as Record<string, unknown>
  const text = typeof record.text === 'string' ? record.text : ''

  if (!text.trim()) {
    return null
  }

  return {
    target: typeof record.target === 'string' && record.target ? record.target : QUICK_TARGET_CURRENT,
    text
  }
}

/**
 * Wire the quick-window → primary-renderer submit channel once. Returns a
 * disposer. Idempotent — a second call while wired is a no-op.
 */
export function initQuickEntryBridge(): () => void {
  const api = typeof window === 'undefined' ? undefined : window.hermesDesktop?.quickEntry

  if (!api?.onSubmit || unsubscribeSubmit) {
    return () => {}
  }

  unsubscribeSubmit = api.onSubmit(raw => {
    const payload = normalizeSubmitPayload(raw)

    if (payload && typeof raw === 'object' && raw !== null) {
      const correlationId = (raw as unknown as Record<string, unknown>).correlationId

      if (typeof correlationId === 'string') {
        submitHandler?.({ ...payload, correlationId })
      }
    }
  })

  return () => {
    unsubscribeSubmit?.()
    unsubscribeSubmit = null
  }
}
