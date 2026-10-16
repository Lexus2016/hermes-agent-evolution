import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  type QuickComposerApi,
  QuickComposerController,
  type QuickEntryComposerSession,
  type QuickEntryCorrelatedResult
} from '@/store/quick-entry'

import { disposeQuickEntryController, QuickEntryApp } from './quick-entry-app'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void

  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })

  return { promise, reject, resolve }
}

function bridge() {
  const shown = new Set<() => void>()
  const state = new Set<Parameters<QuickComposerApi['onState']>[0]>()
  const late = new Set<Parameters<QuickComposerApi['onLateResult']>[0]>()
  const requests: Array<ReturnType<typeof deferred<QuickEntryCorrelatedResult>>> = []

  const api: QuickComposerApi = {
    completeSubmit: vi.fn(async () => true),
    dismiss: vi.fn(),
    getComposerSession: vi.fn(async () => ({ generation: 'document-a', nextSubmitId: 1 })),
    onShown: vi.fn(callback => {
      shown.add(callback)

      return () => shown.delete(callback)
    }),
    onState: vi.fn(callback => {
      state.add(callback)

      return () => state.delete(callback)
    }),
    onLateResult: vi.fn(callback => {
      late.add(callback)

      return () => late.delete(callback)
    }),
    submit: vi.fn(() => {
      const request = deferred<QuickEntryCorrelatedResult>()
      requests.push(request)

      return request.promise
    })
  }

  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { quickEntry: api } })

  return {
    api,
    late,
    requests,
    shown,
    state,
    connected: () => {
      for (const callback of state) {
        callback({ connected: true, sessions: [] })
      }
    },
    result: (submitId: number, ok = true): QuickEntryCorrelatedResult => ({
      correlationId: `correlation-${submitId}`,
      generation: 'document-a',
      ok,
      submitId
    })
  }
}

function edit(text: string) {
  fireEvent.change(screen.getByLabelText('Quick Entry'), { target: { value: text } })
}

function submit() {
  fireEvent.keyDown(screen.getByLabelText('Quick Entry'), { code: 'Enter', key: 'Enter' })
}

async function mount(h: ReturnType<typeof bridge>) {
  const view = render(
    <StrictMode>
      <QuickEntryApp />
    </StrictMode>
  )

  await act(async () => {
    h.connected()
  })

  return view
}

beforeEach(() => {
  disposeQuickEntryController()
})
afterEach(() => {
  cleanup()
  disposeQuickEntryController()
  vi.restoreAllMocks()
})

describe('Quick Entry document controller and real React view', () => {
  // Preserve #77429: native picker options need both theme contrast tokens.
  it('paints every native target option with matching theme foreground and background tokens', async () => {
    const h = bridge()
    await mount(h)
    act(() => {
      for (const callback of h.state) {
        callback({ connected: true, sessions: [{ id: 'session-1', title: 'A recent session' }] })
      }
    })
    const options = screen.getAllByRole('option') as HTMLOptionElement[]
    expect(options).toHaveLength(3)

    for (const option of options) {
      expect(option.style.color).toBe('var(--ui-text-primary, var(--foreground))')
      expect(option.style.backgroundColor).toBe('var(--ui-bg-elevated, var(--background))')
    }
  })

  it('sends once under StrictMode render/effect replay using each dispatched identity', async () => {
    const h = bridge()
    await mount(h)
    edit('first')
    submit()
    submit()
    edit('second')
    submit()
    expect(h.api.submit).toHaveBeenCalledTimes(2)
    expect(vi.mocked(h.api.submit).mock.calls.map(([payload]) => [payload.submitId, payload.text])).toEqual([
      [1, 'first'],
      [3, 'second']
    ])
    expect(h.api.getComposerSession).toHaveBeenCalledOnce()
    expect(h.api.onShown).toHaveBeenCalledOnce()
    expect(h.api.onState).toHaveBeenCalledOnce()
    expect(h.api.onLateResult).toHaveBeenCalledOnce()
    await act(async () => {
      h.requests[0].resolve(h.result(1))
    })
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('second')
    expect(h.api.completeSubmit).toHaveBeenLastCalledWith({
      correlationId: 'correlation-1',
      generation: 'document-a',
      hide: false
    })
    await act(async () => {
      h.requests[1].resolve(h.result(3))
    })
    expect(h.api.completeSubmit).toHaveBeenLastCalledWith({
      correlationId: 'correlation-3',
      generation: 'document-a',
      hide: true
    })
    expect(h.api.dismiss).not.toHaveBeenCalled()
  })

  it('keeps picked targets and ignores IME/Shift Enter without treating picker focus as dismissal', async () => {
    const h = bridge()
    await mount(h)
    edit('route this')
    const input = screen.getByLabelText('Quick Entry')
    const picker = screen.getByRole('combobox')
    fireEvent.blur(input, { relatedTarget: picker })
    expect(h.api.dismiss).not.toHaveBeenCalled()
    fireEvent.change(picker, { target: { value: 'new' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(h.api.submit).not.toHaveBeenCalled()
    submit()
    expect(h.api.submit).toHaveBeenCalledExactlyOnceWith({
      generation: 'document-a',
      submitId: 1,
      text: 'route this',
      target: 'new'
    })
  })

  it('preserves pending drafts and receives terminal replies during an actual unmount/remount gap', async () => {
    const h = bridge()
    const view = await mount(h)
    edit('submitted first')
    submit()
    edit('new unsent edit')
    view.unmount()
    expect(h.late.size).toBe(1)
    await act(async () => {
      h.requests[0].resolve({ ...h.result(1, false), message: 'not delivered' })
    })
    await mount(h)
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('new unsent edit')
    expect(h.api.getComposerSession).toHaveBeenCalledOnce()
    fireEvent.keyDown(screen.getByLabelText('Quick Entry'), { key: 'Escape' })
    act(() => {
      for (const callback of h.shown) {
        callback()
      }
    })
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('submitted first')
    submit()
    expect(vi.mocked(h.api.submit).mock.calls[1][0].submitId).toBe(2)
  })

  it('keeps a delayed handshake through remount and uses the native next counter', async () => {
    const h = bridge()
    const session = deferred<QuickEntryComposerSession>()
    vi.mocked(h.api.getComposerSession).mockReturnValue(session.promise)
    const view = await mount(h)
    edit('waiting draft')
    submit()
    expect(h.api.submit).not.toHaveBeenCalled()
    view.unmount()
    await mount(h)
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('waiting draft')
    await act(async () => {
      session.resolve({ generation: 'resumed-document', nextSubmitId: 12 })
    })
    submit()
    expect(h.api.submit).toHaveBeenCalledExactlyOnceWith({
      generation: 'resumed-document',
      submitId: 12,
      target: 'current',
      text: 'waiting draft'
    })
    expect(h.api.getComposerSession).toHaveBeenCalledOnce()
  })

  it('preserves drafts after handshake failure and safely retries without submitting implicitly', async () => {
    const h = bridge()
    vi.mocked(h.api.getComposerSession).mockRejectedValueOnce(new Error('IPC unavailable'))
    await mount(h)
    edit('keep until connected')
    expect(screen.getByRole('alert').textContent).toContain('Press Enter to try again')
    await act(async () => {
      submit()
    })
    expect(h.api.getComposerSession).toHaveBeenCalledTimes(2)
    expect(h.api.submit).not.toHaveBeenCalled()
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('keep until connected')
    submit()
    expect(h.api.submit).toHaveBeenCalledOnce()
  })

  it('rejects stale-generation callbacks and retains uncertain text on an invoke rejection', async () => {
    const h = bridge()
    await mount(h)
    edit('may have reached backend')
    submit()
    await act(async () => {
      h.requests[0].reject(new Error('invoke failed'))
    })
    edit('new edit')
    submit()
    expect(h.api.submit).toHaveBeenCalledOnce()
    act(() => {
      for (const callback of h.late) {
        callback({ ...h.result(1), generation: 'old-document' })
      }
    })
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('new edit')
    expect(h.api.completeSubmit).not.toHaveBeenCalled()
    act(() => {
      for (const callback of h.late) {
        callback(h.result(1))
      }
    })
    expect(h.api.completeSubmit).toHaveBeenCalledExactlyOnceWith({
      correlationId: 'correlation-1',
      generation: 'document-a',
      hide: false
    })
    act(() => {
      for (const callback of h.late) {
        callback(h.result(1))
      }
    })
    expect(h.api.completeSubmit).toHaveBeenCalledOnce()
  })

  it.each([
    [1, 2],
    [2, 1]
  ])('reconciles late results in order %s,%s without stealing current edits or native focus', async (first, second) => {
    const h = bridge()
    await mount(h)
    edit('first')
    submit()
    edit('second')
    submit()
    await act(async () => {
      h.requests[0].resolve({ ...h.result(1, false), code: 'timeout' })
      h.requests[1].resolve({ ...h.result(2, false), code: 'timeout' })
    })
    edit('new unsent edit')

    for (const id of [first, second]) {
      act(() => {
        for (const callback of h.late) {
          callback({ ...h.result(id, id === 2), message: 'failed first' })
        }
      })
      expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('new unsent edit')
    }

    expect(h.api.completeSubmit).toHaveBeenCalledExactlyOnceWith({
      correlationId: 'correlation-2',
      generation: 'document-a',
      hide: false
    })
    fireEvent.keyDown(screen.getByLabelText('Quick Entry'), { key: 'Escape' })
    act(() => {
      for (const callback of h.shown) {
        callback()
      }
    })
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('first')
  })

  it('explains an older unknown outcome after newer success/re-show and resumes sending only after reconciliation', async () => {
    const h = bridge()
    await mount(h)
    edit('older A')
    submit()
    edit('newer B')
    submit()
    await act(async () => {
      h.requests[0].resolve({ ...h.result(1, false), code: 'timeout' })
    })
    await act(async () => {
      h.requests[1].resolve(h.result(2))
    })
    act(() => {
      for (const callback of h.shown) {
        callback()
      }
    })
    expect(screen.getByRole('alert').textContent).toContain('earlier prompt')
    edit('draft C')
    submit()
    expect(h.api.submit).toHaveBeenCalledTimes(2)
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('draft C')
    expect(screen.getByRole('alert').textContent).toContain('may still be delivered')
    act(() => {
      for (const callback of h.late) {
        callback({ ...h.result(1, false), message: 'A was not delivered' })
      }
    })
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('draft C')
    expect(screen.getByRole('alert').textContent).toBe('A was not delivered')
    submit()
    expect(h.api.submit).toHaveBeenCalledTimes(3)
    expect(vi.mocked(h.api.submit).mock.calls[2][0].text).toBe('draft C')
    await act(async () => {
      h.requests[2].resolve(h.result(4))
    })
    act(() => {
      for (const callback of h.shown) {
        callback()
      }
    })
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('older A')
  })

  it('consumes success after cancel without requesting hide/focus and keeps a newer re-show edit', async () => {
    const h = bridge()
    await mount(h)
    edit('first')
    submit()
    fireEvent.keyDown(screen.getByLabelText('Quick Entry'), { key: 'Escape' })
    expect(h.api.dismiss).toHaveBeenCalledOnce()
    await act(async () => {
      h.requests[0].resolve(h.result(1))
    })
    expect(h.api.completeSubmit).toHaveBeenLastCalledWith({
      correlationId: 'correlation-1',
      generation: 'document-a',
      hide: false
    })
    act(() => {
      for (const callback of h.shown) {
        callback()
      }
    })
    edit('second')
    submit()
    fireEvent.blur(screen.getByLabelText('Quick Entry'))
    act(() => {
      for (const callback of h.shown) {
        callback()
      }
    })
    edit('edit after return')
    await act(async () => {
      h.requests[1].resolve(h.result(2))
    })
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('edit after return')
    expect(h.api.completeSubmit).toHaveBeenLastCalledWith({
      correlationId: 'correlation-2',
      generation: 'document-a',
      hide: false
    })
  })

  it('handles a late native event before the timeout promise callback exactly once', async () => {
    const h = bridge()
    await mount(h)
    edit('first')
    submit()
    await act(async () => {
      h.requests[0].resolve({ ...h.result(1, false), code: 'timeout' })

      for (const callback of h.late) {
        callback(h.result(1))
      }
    })
    expect((screen.getByLabelText('Quick Entry') as HTMLInputElement).value).toBe('')
    expect(h.api.completeSubmit).toHaveBeenCalledOnce()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('disposes listeners and guards delayed handshakes and result callbacks after document teardown', async () => {
    const h = bridge()
    const delayed = deferred<QuickEntryComposerSession>()
    vi.mocked(h.api.getComposerSession).mockReturnValue(delayed.promise)
    const first = new QuickComposerController(h.api)
    first.connect()
    const handshake = first.retrySession()
    first.dispose()
    delayed.resolve({ generation: 'disposed-document', nextSubmitId: 1 })
    await handshake
    expect(first.getSnapshot().ready).toBe(false)
    expect([h.late.size, h.shown.size, h.state.size]).toEqual([0, 0, 0])

    vi.mocked(h.api.getComposerSession).mockResolvedValue({ generation: 'document-a', nextSubmitId: 4 })
    const current = new QuickComposerController(h.api)
    current.connect()
    await current.retrySession()
    h.connected()
    current.dispatch({ type: 'edit', draft: 'pending' })
    current.dispatch({ type: 'submit' })
    const staleLate = [...h.late][0]
    current.dispose()
    staleLate(h.result(4))
    h.requests[0].resolve(h.result(4))
    await Promise.resolve()
    expect(h.api.completeSubmit).not.toHaveBeenCalled()
    expect(current.getSnapshot().state.pendingSubmitTexts).toEqual({})
    expect([h.late.size, h.shown.size, h.state.size]).toEqual([0, 0, 0])
  })
})
