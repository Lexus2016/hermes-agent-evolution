import { Button, cn, Codicon, relativeTime, RowButton, Tip } from '@hermes/plugin-sdk'

import { botSourceStatus } from './data'
import { GROUP_ACTIVITY_GLYPHS, groupActivityLabel, groupActivityTone } from './group-activity'
import type { GroupActivityEntry } from './group-activity'
import { $groupChatWorkspace } from './group-chat'
import { useBots } from './i18n'
import { displayName } from './labels'
import { botRosterMeta } from './routing'
import type { BotMeta, GroupMember } from './types'

interface LegacyGroupChatHeaderProps {
  allMeta: Record<string, BotMeta>
  group: string
  image?: null | string
  members: GroupMember[]
  onBack?: () => void
  onDisband: () => void
  onManageMembers: () => void
  onSettings: () => void
}

export function LegacyGroupChatHeader({
  allMeta,
  group,
  image,
  members,
  onBack,
  onDisband,
  onManageMembers,
  onSettings
}: LegacyGroupChatHeaderProps) {
  const b = useBots()
  const availableMembers = members.filter(member => botSourceStatus(member).available).length
  const availabilityLabel = `${availableMembers} of ${members.length} available`

  const memberNames =
    members.map(b => displayName(b, botRosterMeta(b, allMeta))).join(', ') || 'No bots in this group chat'

  return (
    <div className="flex items-center gap-2 px-2.5 pt-2.5 pb-2">
      <Button onClick={() => (onBack ? onBack() : $groupChatWorkspace.set(null))} size="sm" variant="ghost">
        Back
      </Button>
      {/* Room picture (set via Group settings) leads the title when present. */}
      {image ? (
        <img
          alt=""
          className="size-6 shrink-0 rounded-md object-cover ring-1 ring-(--ui-stroke-secondary)"
          src={image}
        />
      ) : (
        <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-(--chrome-action-hover) text-(--ui-text-tertiary)">
          <Codicon name="organization" />
        </span>
      )}
      <div className="min-w-0 flex-1 truncate text-sm font-semibold">{group}</div>
      <Tip label={memberNames}>
        <span
          aria-label={availabilityLabel}
          className={cn(
            'shrink-0 text-[0.65rem] text-(--ui-text-quaternary)',
            members.length > 0 && availableMembers < members.length && 'text-amber-600 dark:text-amber-300'
          )}
        >
          {members.length > 0 && availableMembers < members.length
            ? availabilityLabel
            : b.group.memberCount(members.length)}
        </span>
      </Tip>
      <Tip label={b.group.settingsHint(group)}>
        <Button
          aria-label={b.group.settingsLabel(group)}
          className="shrink-0 text-(--ui-text-tertiary) hover:text-foreground"
          onClick={onSettings}
          size="sm"
          variant="ghost"
        >
          <Codicon name="gear" />
        </Button>
      </Tip>
      <Tip label="Manage members">
        <Button
          aria-label="Manage group members"
          className="shrink-0 text-(--ui-text-tertiary) hover:text-foreground"
          onClick={onManageMembers}
          size="sm"
          variant="ghost"
        >
          <Codicon name="organization" />
        </Button>
      </Tip>
      <Tip label={b.group.disbandHint(group)}>
        <Button
          aria-label={b.group.disbandLabel(group)}
          className="shrink-0 text-(--ui-text-tertiary) hover:text-destructive"
          onClick={onDisband}
          size="sm"
          variant="ghost"
        >
          <Codicon name="trash" />
        </Button>
      </Tip>
    </div>
  )
}

interface LegacyGroupActivityPanelProps {
  activityEvents: GroupActivityEntry[]
  activityOpen: boolean
  group: string
  onToggle: () => void
  running?: boolean
  stopRoomRun: () => Promise<void>
}

export function LegacyGroupActivityPanel({
  activityEvents,
  activityOpen,
  group,
  onToggle,
  running,
  stopRoomRun
}: LegacyGroupActivityPanelProps) {
  const b = useBots()
  const latestActivity = activityEvents.length ? activityEvents[activityEvents.length - 1] : null
  // A later "settled" event must not hide a member that failed to answer.
  // Successful completion for that member clears its unresolved warning.
  const unresolvedFailures = new Map<string, GroupActivityEntry>()

  for (const event of activityEvents) {
    const key = event.member || ''

    if (event.kind === 'failed' || event.kind === 'timed-out') {
      unresolvedFailures.delete(key)
      unresolvedFailures.set(key, event)
    } else if (event.kind === 'replied' || event.kind === 'passed' || event.kind === 'delivered') {
      unresolvedFailures.delete(key)
    }
  }

  const summaryActivity =
    !running && unresolvedFailures.size ? [...unresolvedFailures.values()].at(-1)! : latestActivity

  return (
    <div className="border-b border-(--ui-stroke-secondary)">
      <div className="flex items-center gap-1">
        <RowButton
          aria-controls={`group-activity:${group}`}
          aria-expanded={activityOpen}
          className="flex min-w-0 flex-1 items-center gap-1.5 px-2.5 py-1 text-left text-[0.7rem] text-(--ui-text-quaternary) transition-colors hover:text-foreground"
          onClick={onToggle}
        >
          <Codicon className="shrink-0 text-[0.65rem]" name={activityOpen ? 'chevron-down' : 'chevron-right'} />
          <span className="shrink-0 font-medium">{b.group.activity}</span>
          {summaryActivity ? (
            <span
              className={cn('min-w-0 flex-1 truncate', groupActivityTone(summaryActivity.kind))}
            >{`${groupActivityLabel(summaryActivity, group)} · ${relativeTime(summaryActivity.at)}`}</span>
          ) : null}
        </RowButton>
        {running ? (
          <Tip label={b.group.stopHint}>
            <Button
              className="shrink-0 text-(--ui-accent)"
              onClick={() => void stopRoomRun()}
              size="xs"
              variant="ghost"
            >
              <Codicon name="debug-stop" />
              {b.group.stop}
            </Button>
          </Tip>
        ) : null}
      </div>
      {activityOpen ? (
        <div className="grid gap-0.5 px-2.5 pb-1.5" id={`group-activity:${group}`}>
          {activityEvents.length ? (
            [...activityEvents].reverse().map((event, i) => (
              <div className="flex items-center gap-1.5 text-[0.7rem]" key={`${event.at}:${i}`}>
                <Codicon
                  className={cn('shrink-0 text-[0.65rem]', groupActivityTone(event.kind))}
                  name={GROUP_ACTIVITY_GLYPHS[event.kind] || 'circle-outline'}
                />
                <span className={cn('min-w-0 flex-1 truncate', groupActivityTone(event.kind))}>
                  {groupActivityLabel(event, group)}
                </span>
                <span className="shrink-0 text-[0.625rem] text-(--ui-text-quaternary)">{relativeTime(event.at)}</span>
                {running && event.kind === 'working' ? (
                  <Tip label={b.group.stopHint}>
                    <Button
                      className="shrink-0 text-(--ui-accent)"
                      onClick={() => void stopRoomRun()}
                      size="micro"
                      variant="ghost"
                    >
                      <Codicon name="debug-stop" />
                      {b.group.stop}
                    </Button>
                  </Tip>
                ) : null}
              </div>
            ))
          ) : (
            <div className="px-0.5 pb-0.5 text-[0.625rem] text-(--ui-text-quaternary)">{b.group.noActivityYet}</div>
          )}
        </div>
      ) : null}
    </div>
  )
}
