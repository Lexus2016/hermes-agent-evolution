import { describe, expect, it } from 'vitest'

import { toolCallOwnerMessageId, upsertToolPart } from './tool-parts'
import type { GatewayEventPayload } from './types'

describe('identified tool completion ownership', () => {
  it.each([
    ['different commands', { args: { command: 'echo A' } }, { args: { command: 'echo B' } }],
    ['identical commands', { args: { command: 'echo A' } }, { args: { command: 'echo A' } }],
    ['sparse completion', { args: { command: 'echo A' } }, {}],
    [
      'conflicting commands with a shared preview',
      { args: { command: 'echo A' }, preview: 'Running command' },
      { args: { command: 'echo B' }, preview: 'Running command' }
    ],
    ['preview-only legacy row', { preview: 'echo B' }, { args: { command: 'echo B' } }],
    ['context without incoming identifying args', { context: 'echo A' }, { context: 'echo A' }],
    [
      'unlisted full args',
      { args: { url: 'https://example.test' }, context: 'echo B' },
      { args: { command: 'echo B' } }
    ],
    [
      'non-string full args',
      { args: { command: { operation: 'A' } }, context: 'echo B' },
      { args: { command: 'echo B' } }
    ],
    [
      'identifying args despite a matching context',
      { args: { command: 'echo A' }, context: 'echo B' },
      { args: { command: 'echo B' } }
    ]
  ])('keeps distinct provider IDs separate: %s', (_label, start, completion) => {
    const before = upsertToolPart([], { name: 'terminal', tool_id: 'provider-A', ...start }, 'running', 1)
    const payload = { name: 'terminal', tool_id: 'provider-B', result: 'B result', ...completion }

    expect(toolCallOwnerMessageId([{ id: 'live', role: 'assistant', parts: before }], payload)).toBeNull()
    const completed = upsertToolPart(before, payload, 'complete', 2)
    const replayed = upsertToolPart(completed, { name: 'terminal', tool_id: 'provider-B' }, 'complete', 3)

    expect(replayed).toHaveLength(2)
    expect(replayed[0]).toEqual(before[0])
    expect(replayed[1]).toMatchObject({ toolCallId: 'provider-B', result: 'B result', timestamp: 2 })
    expect(replayed[1]).toHaveProperty('args', {
      ...('args' in completion ? completion.args : {}),
      ...('context' in completion ? { context: completion.context } : {}),
      ...('preview' in completion ? { preview: completion.preview } : {})
    })
  })

  it.each([
    ['terminal', 'command', 'echo legacy'],
    ['web_search', 'query', 'legacy weather']
  ])('preserves context-only legacy %s starts with different event IDs', (name, field, value) => {
    const before = upsertToolPart([], { name, tool_id: 'legacy-start', context: value }, 'running', 1)

    const after = upsertToolPart(
      before,
      { name, tool_id: 'legacy-complete', args: { [field]: value }, result: 'done' },
      'complete',
      2
    )

    expect(after).toHaveLength(1)
    expect(after[0]).toMatchObject({ toolCallId: 'legacy-complete', result: 'done', timestamp: 1 })
  })

  it('keeps exact owners, synthetic adoption and id-less oldest-first completion', () => {
    let parts = upsertToolPart([], { name: 'terminal', tool_id: 'A', args: { command: 'echo A' } }, 'running', 1)
    parts = upsertToolPart(parts, { name: 'terminal', tool_id: 'B', args: { command: 'echo B' } }, 'running', 2)
    parts = upsertToolPart(parts, { name: 'terminal', tool_id: 'B', result: 'B' }, 'complete', 3)
    parts = upsertToolPart(parts, { name: 'terminal', result: 'A' }, 'complete', 4)
    expect(parts).toMatchObject([
      { toolCallId: 'A', result: 'A' },
      { toolCallId: 'B', result: 'B' }
    ])

    let synthetic = upsertToolPart([], { name: 'terminal', context: 'first' }, 'running', 1)
    synthetic = upsertToolPart(synthetic, { name: 'terminal', context: 'second' }, 'running', 2)
    synthetic = upsertToolPart(synthetic, { name: 'terminal', result: 'first result' }, 'complete', 3)
    synthetic = upsertToolPart(
      synthetic,
      { name: 'terminal', tool_id: 'new-id', result: 'second result' },
      'complete',
      4
    )
    expect(synthetic).toMatchObject([
      { args: { context: 'first' }, result: 'first result' },
      { toolCallId: 'new-id', args: { context: 'second' }, result: 'second result' }
    ])
  })

  it.each(['clarify', 'setup_mcp'])('preserves request-backed %s argument correlation', name => {
    const payload: GatewayEventPayload = { name, args: { question: 'Which account?' } }
    const before = upsertToolPart([], { ...payload, tool_id: 'request-id' }, 'running', 1)
    const after = upsertToolPart(before, { ...payload, tool_id: 'provider-id', result: 'chosen' }, 'complete', 2)

    expect(after).toHaveLength(1)
    expect(after[0]).toMatchObject({ toolCallId: 'provider-id', result: 'chosen' })
  })
})
