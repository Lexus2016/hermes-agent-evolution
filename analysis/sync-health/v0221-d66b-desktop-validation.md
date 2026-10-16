# Group workspace presentation extraction

The reviewed scroll correction exposed a parent function already beyond ordinary new-scope limits: LegacyGroupChatWorkspace measured CC36 and357 owned lines. Root authorized cohesive presentation extraction, not a three-line trim or metric waiver.

## Frozen source

- group-chat-view.tsx: d09c2730385c74f0ffd9610d4bee9c09b309a372
- group-chat-workspace-chrome.tsx: a85b6dc5dcb23499e85fc8be0cd57d9f56ef88c3
- group-chat-workspace-chrome.test.tsx: 1d8593b70027b0cd9dddf181ca5646a73b7878e7

The new sibling contains the room header and activity summary/presentation. Parent activity state/subscription, hook order, dialogs, composer state, current-thread stop callback and live member lookup remain parent-owned. Children introduce no DOM wrapper or keyed remount. The scroll callback, submit/reply callbacks, memberDescriptors, stopRoomRun and renderEntry retain their exact function bodies.

## Scoped structure

- LegacyGroupChatWorkspace: CC36/357 lines/nesting2 → CC16/245 lines/nesting1.
- LegacyGroupChatHeader: CC7/81 lines/nesting0.
- LegacyGroupActivityPanel: CC16/69 lines/nesting2.
- Every changed/new unit meets ordinary20/300/6 limits.116 existing parent units retain their body identities; separate unchanged over-cap functions, including renderEntry, receive no automatic allowance.

## Validation

Seven directly affected existing suites passed30 tests both before and after extraction. Three added mounted-workspace tests cover header availability/image/Back/settings/member/disband-cancel actions, activity disclosure across parent rerenders, current-thread stop with roster changed after paint, and unresolved-failure summary retirement. The final expanded corrected-desktop selection passed410 tests in25 suites, exit0. All39 selected source/test hashes were unchanged before/after; unrelated gateway/tools work was concurrent, so this receipt does not claim whole-tree immutability.

Run from apps/desktop:

```sh
node ../../node_modules/vitest/vitest.mjs run --maxWorkers=2 --project electron electron/quick-entry.test.ts --project ui src/store/quick-entry.test.ts src/app/quick-entry/quick-entry-app.test.tsx src/app/contrib/hooks/use-quick-entry-bridge.test.ts src/lib/ime.test.ts src/lib/chat-messages/reconciliation.test.ts src/plugins/hermes-bots/create-agent-lazy-profile.test.tsx src/plugins/hermes-bots/data.bot-meta.test.ts src/plugins/hermes-bots/data.bot-meta-migration.test.ts src/plugins/hermes-bots/group-chat-scroll.test.tsx src/plugins/hermes-bots/group-chat-view.render.test.tsx src/lib/chat-messages/tool-part-identity.test.ts src/app/session/hooks/use-message-stream/late-tool-events.test.tsx src/lib/chat-messages.test.ts src/lib/chat-messages/tool-result-preservation.test.ts src/lib/chat-messages/competing-contracts.test.ts src/components/boot-failure-overlay.test.tsx src/components/boot-failure-reauth.test.ts src/components/boot-failure-cause.test.ts src/plugins/hermes-bots/group-chat-workspace-chrome.test.tsx src/plugins/hermes-bots/group-chat-timeline.test.tsx src/plugins/hermes-bots/group-chat-view.inline-code.test.tsx src/plugins/hermes-bots/group-chat-view.wrap.test.tsx src/plugins/hermes-bots/group-chat-view.test.ts src/plugins/hermes-bots/canonical-group-capability-gates.test.tsx
```

ESLint and git diff --check passed. All three files parse without syntax diagnostics. No semantic typecheck was run for this extraction; full desktop typecheck and native Electron/OS validation remain separate holds. The tests use existing SDK/jsdom harnesses, not live external backends or native windows.

Evidence: consolidated-receipt.json binds the exact command and 39 before/after hashes; consolidated-tests.log has the terminal 410-pass result; ratchet.json records unit comparisons and preserved parent helpers; syntax.json and eslint.log record scoped checks.
