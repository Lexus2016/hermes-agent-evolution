import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { translateBots } from './i18n-test-helper'

vi.mock('@hermes/plugin-sdk', async () => {
  const { useStore } = await import('@nanostores/react')
  const { createGroupGateway, pluginSdkMock } = await import('./group-test-utils')
  const base = await pluginSdkMock(createGroupGateway().host)

  const Button = ({
    children,
    onClick,
    title,
    disabled,
    'aria-label': label,
    'aria-expanded': expanded
  }: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button aria-expanded={expanded} aria-label={label} disabled={disabled} onClick={onClick} title={title}>
      {children}
    </button>
  )

  return {
    ...base,
    Button,
    RowButton: Button,
    cn: (...values: unknown[]) => values.filter(Boolean).join(' '),
    Codicon: () => null,
    CopyButton: () => null,
    ConfirmDialog: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
      open ? (
        <div role="alertdialog">
          <button onClick={onClose}>Cancel disband</button>
        </div>
      ) : null,
    Dialog: ({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) =>
      open ? (
        <div aria-label="Group settings" role="dialog">
          <button onClick={() => onOpenChange(false)}>Close settings</button>
        </div>
      ) : null,
    DialogContent: () => null,
    DialogDescription: () => null,
    DialogFooter: () => null,
    DialogHeader: () => null,
    DialogTitle: () => null,
    Input: () => null,
    ToggleRow: () => null,
    MessageTextContent: ({ text }: { text: string }) => <span>{text}</span>,
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
vi.mock('./group-chat-view-members', () => ({
  GroupMemberPicker: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? (
      <div aria-label="Group members" role="dialog">
        <button onClick={onClose}>Close members</button>
      </div>
    ) : null
}))

const originalScrollIntoView = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')

beforeEach(async () => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  vi.stubGlobal('BroadcastChannel', undefined)

  const [{ $groupChats }, { $groupActivity }, { $botMeta, $lastRoster }] = await Promise.all([
    import('./group-chat'),
    import('./group-activity'),
    import('./data')
  ])

  $groupChats.set({ Room: { epoch: 1, log: [], running: false, sessions: {}, watermarks: {} } })
  $groupActivity.set({})
  $botMeta.set({})
  $lastRoster.set([])
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

it('keeps header availability, image, Back and parent-owned dialog actions', async () => {
  const { $groupChats, $groupChatWorkspace } = await import('./group-chat')
  const { GroupChatWorkspace } = await import('./group-chat-view')
  $groupChats.set({ Room: { ...$groupChats.get().Room, image: 'data:image/png;base64,room' } })
  $groupChatWorkspace.set('Room')
  const onBack = vi.fn()
  const members = [{ name: 'builder' }, { name: 'offline', remoteSource: true, sourceReachable: false }]
  const view = render(<GroupChatWorkspace group="Room" members={members} onBack={onBack} />)
  await screen.findByRole('button', { name: 'Back' })
  expect(screen.getByLabelText('1 of 2 available').textContent).toBe('1 of 2 available')
  expect(view.container.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,room')
  fireEvent.click(screen.getByRole('button', { name: 'Group settings for Room' }))
  expect(screen.getByRole('dialog', { name: 'Group settings' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Close settings' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Manage group members' }))
  expect(screen.getByRole('dialog', { name: 'Group members' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Close members' }))
  fireEvent.click(screen.getByRole('button', { name: 'Disband Room' }))
  expect(screen.getByRole('alertdialog')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel disband' }))
  expect(screen.queryByRole('alertdialog')).toBeNull()
  expect($groupChats.get().Room).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Back' }))
  expect(onBack).toHaveBeenCalledOnce()
  expect($groupChatWorkspace.get()).toBe('Room')
  view.rerender(<GroupChatWorkspace group="Room" members={members} />)
  fireEvent.click(screen.getByRole('button', { name: 'Back' }))
  expect($groupChatWorkspace.get()).toBeNull()
})

it('retains disclosure across rerenders and stops the current thread with live members', async () => {
  const [{ $groupChats }, activity, data, rounds, { GroupChatWorkspace }] = await Promise.all([
    import('./group-chat'),
    import('./group-activity'),
    import('./data'),
    import('./group-rounds'),
    import('./group-chat-view')
  ])

  const stop = vi.spyOn(rounds, 'stopGroupThread').mockResolvedValue(undefined)
  data.$botMeta.set({ builder: { groups: ['Room'] }, reviewer: { groups: ['Room'] } })
  data.$lastRoster.set([{ name: 'builder' }])
  $groupChats.set({ Room: { ...$groupChats.get().Room, running: true } })
  activity.recordGroupActivity('Room', { kind: 'working', member: 'builder', thread: 'old-thread' })
  const members = [{ name: 'builder' }]
  const view = render(<GroupChatWorkspace group="Room" members={members} />)
  const toggle = await screen.findByRole('button', { name: /^Activity/ })
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(toggle)
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  view.rerender(<GroupChatWorkspace group="Room" members={members} visible={false} />)
  view.rerender(<GroupChatWorkspace group="Room" members={[...members]} visible />)
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  act(() => {
    activity.recordGroupActivity('Room', { kind: 'working', member: 'reviewer', thread: 'current-thread' })
  })
  // Change the roster after the last paint: stop must read it at click time.
  data.$lastRoster.set([{ name: 'reviewer' }])
  fireEvent.click(screen.getAllByRole('button', { name: 'Stop' }).at(-1)!)
  await waitFor(() => expect(stop).toHaveBeenCalledOnce())
  expect(stop).toHaveBeenCalledWith('Room', 'current-thread', [{ name: 'reviewer', title: '' }])
  act(() => {
    $groupChats.set({ Room: { ...$groupChats.get().Room, running: false } })
  })
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
})

it('keeps unresolved failure summaries after settle until that member completes', async () => {
  const [activity, { GroupChatWorkspace }] = await Promise.all([
    import('./group-activity'),
    import('./group-chat-view')
  ])

  activity.recordGroupActivity('Room', { kind: 'failed', member: 'builder', reason: 'offline' })
  activity.recordGroupActivity('Room', { kind: 'settled', member: null })
  render(<GroupChatWorkspace group="Room" members={[{ name: 'builder' }]} />)
  const toggle = await screen.findByRole('button', { name: /^Activity/ })
  expect(toggle.textContent).toContain('builder hit an error — offline')
  act(() => {
    activity.recordGroupActivity('Room', { kind: 'delivered', member: 'builder' })
  })
  await waitFor(() =>
    expect(screen.getByRole('button', { name: /^Activity/ }).textContent).toContain('builder delivered a late reply')
  )
  expect(screen.getByRole('button', { name: /^Activity/ }).textContent).not.toContain('hit an error')
})
