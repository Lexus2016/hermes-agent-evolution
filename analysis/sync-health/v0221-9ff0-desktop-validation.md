# Corrected Desktop combined validation

The final combined selection passed **381 tests in 19 files**, exit 0, at commit `9ff0fd41973bff45a288e56e1700f9b6433d7675` (tree `969c303093d70a9b4e2a2d8222f8fe83d0fb451f`). Git HEAD/tree and the clean worktree status were unchanged before and after execution. All 32 selected production/test file hashes were unchanged; the machine-readable receipt records each full hash.

The selection combines:

- Quick Entry relay, controller/reducer, mounted App, main bridge and IME regressions: 119 tests in 5 files.
- Reconciliation turn-owner regressions: 22 tests in 1 file.
- Draft owner, metadata/asset ownership and migration regressions: 68 tests in 3 files.
- Owned group-scroll viewport and renderer regressions: 4 tests in 2 files.
- Tool completion ownership and retained legacy/result contracts: 111 tests in 5 files.
- Boot recovery owner and reauth/cause regressions: 57 tests in 3 files.

Run from `apps/desktop`:

```sh
node ../../node_modules/vitest/vitest.mjs run --maxWorkers=2 --project electron electron/quick-entry.test.ts --project ui src/store/quick-entry.test.ts src/app/quick-entry/quick-entry-app.test.tsx src/app/contrib/hooks/use-quick-entry-bridge.test.ts src/lib/ime.test.ts src/lib/chat-messages/reconciliation.test.ts src/plugins/hermes-bots/create-agent-lazy-profile.test.tsx src/plugins/hermes-bots/data.bot-meta.test.ts src/plugins/hermes-bots/data.bot-meta-migration.test.ts src/plugins/hermes-bots/group-chat-scroll.test.tsx src/plugins/hermes-bots/group-chat-view.render.test.tsx src/lib/chat-messages/tool-part-identity.test.ts src/app/session/hooks/use-message-stream/late-tool-events.test.tsx src/lib/chat-messages.test.ts src/lib/chat-messages/tool-result-preservation.test.ts src/lib/chat-messages/competing-contracts.test.ts src/components/boot-failure-overlay.test.tsx src/components/boot-failure-reauth.test.ts src/components/boot-failure-cause.test.ts
```

Evidence: `/tmp/hermes-desktop-all-corrections-receipt.json` and `/tmp/hermes-desktop-all-corrections-tests.log`. The receipt includes the exact command, timestamps, terminal exit code, before/after HEAD/tree/status and all selected hashes.

## Limits

This is a focused regression selection using Electron-free relay helpers, jsdom and existing dependency harnesses. It does not establish native Electron/window/focus behavior, real OAuth/backend/network behavior, or a full desktop test-suite or semantic-typecheck pass. The earlier full desktop typecheck limitation remains separate. Hosted/native validation should still exercise document reload/recreated windows, reordered timeout/late replies, ownership-safe hide/focus, newer edits and unresolved status through remount, real owner-scoped draft assets/setup, and stale Boot recovery actions. Canonical repository lint and full health validation are separate receipts owned by the integration lead.
