import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { translateBots } from './i18n-test-helper'

vi.mock('@hermes/plugin-sdk', async () => {
  const { useStore } = await import('@nanostores/react')
  const { createGroupGateway, pluginSdkMock } = await import('./group-test-utils')
  const base = await pluginSdkMock(createGroupGateway().host)

  const Button = ({ children, onClick }: { children?: ReactNode; onClick?: () => void }) => (
    <button onClick={onClick}>{children}</button>
  )

  return {
    ...base,
    Button,
    RowButton: Button,
    cn: (...values: unknown[]) => values.filter(Boolean).join(' '),
    Codicon: () => null,
    CopyButton: () => null,
    ConfirmDialog: () => null,
    Dialog: () => null,
    DialogContent: () => null,
    DialogDescription: () => null,
    DialogFooter: () => null,
    DialogHeader: () => null,
    DialogTitle: () => null,
    Input: () => null,
    MessageTextContent: ({ text }: { text: string }) => <span>{text}</span>,
    ToggleRow: () => null,
    Tip: ({ children }: { children: ReactNode }) => children,
    relativeTime: () => 'now',
    useI18n: () => ({ t: { common: { cancel: 'Cancel', save: 'Save' } } }),
    usePluginI18n: () => translateBots,
    useValue: useStore
  }
})
vi.mock('./avatar', () => ({ avatarColor: () => '#888', botAppearance: () => ({}), BotFace: () => null }))
vi.mock('./group-chat-parts', () => ({
  GroupClarifyCard: () => null,
  GroupImageControls: () => null,
  GroupMentionInput: () => null
}))

const scrollIntoView = vi.fn()
const originalScrollIntoView = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
const members = [{ name: 'builder' }]

const message = (id: number) => ({
  id: `message-${id}`,
  thread: 'thread',
  from: { kind: 'member' as const, name: 'builder' },
  text: `Reply ${id}`,
  at: id
})

beforeEach(async () => {
  // jsdom does not lay out transcripts; give the viewport explicit metrics.
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView })
  scrollIntoView.mockClear()
  vi.stubGlobal('BroadcastChannel', undefined)
  const { $groupChats } = await import('./group-chat')
  $groupChats.set({ Room: { log: [message(1)], running: false, sessions: {}, watermarks: {} } })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()

  if (originalScrollIntoView) {
    Object.defineProperty(Element.prototype, 'scrollIntoView', originalScrollIntoView)
  } else {
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  }
})

function setScrollPosition(viewport: Element, top: number) {
  Object.defineProperties(viewport, {
    scrollHeight: { configurable: true, value: 2000 },
    clientHeight: { configurable: true, value: 400 },
    scrollTop: { configurable: true, value: top, writable: true }
  })
  fireEvent.scroll(viewport)
}

async function appendReply() {
  const { $groupChats } = await import('./group-chat')
  act(() => {
    const room = $groupChats.get().Room
    $groupChats.set({ Room: { ...room, log: [...room.log, message(room.log.length + 1)] } })
  })
}

async function logViewport(container: HTMLElement) {
  return waitFor(() => {
    const viewport = container.querySelector<HTMLDivElement>('.overflow-y-auto')
    expect(viewport).not.toBeNull()

    return viewport!
  })
}

it('opens at the bottom, respects reading history, and follows again near the bottom or on explicit reopen', async () => {
  const { GroupChatWorkspace } = await import('./group-chat-view')
  const view = render(<GroupChatWorkspace group="Room" members={members} />)
  const viewport = await logViewport(view.container)
  expect(scrollIntoView).toHaveBeenCalledWith({ block: 'end' })

  setScrollPosition(viewport, 100)
  scrollIntoView.mockClear()
  await appendReply()
  expect(scrollIntoView).not.toHaveBeenCalled()

  // The existing follow threshold is 80px, including a reader just above bottom.
  setScrollPosition(viewport, 1550)
  await appendReply()
  expect(scrollIntoView).toHaveBeenCalledOnce()

  setScrollPosition(viewport, 100)
  scrollIntoView.mockClear()
  view.rerender(<GroupChatWorkspace group="Room" members={members} visible={false} />)
  await appendReply()
  expect(scrollIntoView).not.toHaveBeenCalled()
  view.rerender(<GroupChatWorkspace group="Room" members={members} visible />)
  expect(scrollIntoView).toHaveBeenCalledOnce()
  scrollIntoView.mockClear()
  await appendReply()
  expect(scrollIntoView).toHaveBeenCalledOnce()
})

it('owns its scroll listener even inside another viewport and cleans up replaced and unmounted nodes', async () => {
  const added = vi.spyOn(Element.prototype, 'addEventListener')
  const removed = vi.spyOn(Element.prototype, 'removeEventListener')
  const { GroupChatWorkspace } = await import('./group-chat-view')

  const workspace = (key: string) => (
    <div data-slot="scroll-area-viewport">
      <GroupChatWorkspace group="Room" key={key} members={members} />
    </div>
  )

  const view = render(workspace('first'))
  const first = await logViewport(view.container)

  const listenersFor = (node: Element) =>
    added.mock.calls.filter(([type], index) => type === 'scroll' && added.mock.contexts[index] === node)

  const removalsFor = (node: Element) =>
    removed.mock.calls.filter(([type], index) => type === 'scroll' && removed.mock.contexts[index] === node)

  expect(listenersFor(first)).toHaveLength(1)
  const firstListener = listenersFor(first)[0][1]
  expect(listenersFor(first)[0][2]).toEqual({ passive: true })
  const ancestor = first.closest('[data-slot="scroll-area-viewport"]')!
  expect(listenersFor(ancestor)).toHaveLength(0)
  setScrollPosition(first, 100)
  setScrollPosition(ancestor, 1600)
  scrollIntoView.mockClear()
  await appendReply()
  expect(scrollIntoView).not.toHaveBeenCalled()
  expect(listenersFor(first)).toHaveLength(1)

  view.rerender(workspace('replacement'))
  const replacement = await logViewport(view.container)
  expect(replacement).not.toBe(first)
  expect(removalsFor(first)).toEqual([['scroll', firstListener]])
  expect(listenersFor(replacement)).toHaveLength(1)
  setScrollPosition(replacement, 100)
  scrollIntoView.mockClear()
  await appendReply()
  expect(scrollIntoView).not.toHaveBeenCalled()

  const replacementListener = listenersFor(replacement)[0][1]
  view.unmount()
  expect(removalsFor(replacement)).toEqual([['scroll', replacementListener]])
  expect(removalsFor(first)).toHaveLength(1)
})
