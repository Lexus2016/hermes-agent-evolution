# Quick Entry correlation/window ownership validation

Checkpoint: `6e2ea5b886a48dcda07ca76b48b7244055976d74` (tree `481c7f6b64dc0c30abb5eb66db56c3d742ac70a5`). The nine transport paths are frozen. This is separate from reducer preservation checkpoint `4f44a018fda95fc8536e89f69e76b26133add856`.

## Passing focused checks

From `apps/desktop`:

```sh
npx vitest run --project electron electron/quick-entry.test.ts --project ui src/store/quick-entry.test.ts src/app/quick-entry/quick-entry-app.test.tsx src/app/contrib/hooks/use-quick-entry-bridge.test.ts src/lib/ime.test.ts
```

<!-- no-tmp: ok — historical Linux evidence location, not a runtime path or command recommendation. -->
Result: **119 tests passed in 5 files**. Receipt: `/tmp/hermes-quick-entry-transport-tests.log`.

<!-- no-tmp: ok — historical Linux evidence location, not a runtime path or command recommendation. -->
The tests execute the actual relay, registered IPC handlers, imported preload, pure reducer and real React/jsdom App. Native windows/IPC transport are mocked. Coverage includes positive safe-integer IDs, duplicate/unknown/wrong-window/wrong-generation rejection, one-use completion, older authenticated hide=false cleanup, 30-second capability-only expiry, multiple reordered timeouts/results, failed-draft FIFO, current edits, unknown-status explanation, IME/target behavior, StrictMode exactly-one send/listener, delayed/failed handshake, real view unmount/remount gaps and explicit document teardown. Existing shortcut/settings/window-bounds tests remain byte-identical; existing 38 reducer cases and native option contrast regression remain exercised. Detailed mapping: `/tmp/hermes-quick-entry-transport-retention.json`.

<!-- no-tmp: ok — historical Linux evidence location, not a runtime path or command recommendation. -->
From the repository root, ESLint on the exact nine paths exits 0; `git diff --check` is clean. Log: `/tmp/hermes-quick-entry-transport-eslint.log`. The bounded TypeScript structure parser reports **zero changed units above CC 20 / function lines 300 / nesting 6**. Existing main.ts over-cap units are unchanged and the file shrinks by 35 lines. This is a scoped body-identity comparison, not a full health rerun. Paths, hashes and metrics: `/tmp/hermes-quick-entry-transport-paths.json`, `/tmp/hermes-quick-entry-transport-receipt.json`, `/tmp/hermes-quick-entry-transport-ratchet.json`.

## Typecheck boundaries

These focused checks exit 0:

```sh
# no-tmp: ok — archived command records the Linux validation receipt; not a portable rerun instruction.
npx tsc --noEmit -p /tmp/hermes-quick-entry-transport-tsconfig.json
npx tsc --noEmit --skipLibCheck --target es2022 --module esnext --moduleResolution bundler --types node apps/desktop/electron/quick-entry.ts
```

<!-- no-tmp: ok — historical Linux evidence location, not a runtime path or command recommendation. -->
The scratch UI config extends the desktop config and includes the actual global/Vite declarations, store, App and their direct tests. Logs: `/tmp/hermes-quick-entry-transport-ui-types.log`, `/tmp/hermes-quick-entry-transport-relay-types.log`.

<!-- no-tmp: ok — historical Linux evidence location, not a runtime path or command recommendation. -->
An expanded Electron test/preload import check exits 2 with 371 diagnostics in preload.ts, connection-config.ts, api-transport.ts and profile-delete-routing.ts; it reports none in quick-entry.ts or quick-entry.test.ts. Log: `/tmp/hermes-quick-entry-transport-electron-types.log`. This does not certify the whole Electron graph. The earlier full desktop typecheck exit 137 remains unresolved. No full desktop/typecheck/health success is claimed.

## Native verification still required

No native Electron/OS execution was performed. This workspace has no Electron dist binary/path.txt, DISPLAY is unset and Xvfb is not installed. The mock tests establish logic, not OS focus/window ordering.

On a hosted native desktop runner, verify:

1. First load, aborted navigation, in-place navigation, committed reload, renderer crash and closed/recreated window. Session identity must appear only for a committed document; same-document remount keeps the counter; committed reload permits a fresh counter and rejects old replies/capabilities.
2. Submit A then B, force both timeouts, return success/failure in both orders. Each result must reconcile only its owner; no automatic resend. Older failed text must remain recoverable exactly once.
3. Edit after submit, including during timeout, and allow old/new success to arrive. New unsent text must stay visible; stale completion must not hide or move focus.
4. Timely latest untouched success may hide and focus the primary window. Late success may hide only its untouched latest editor and must not focus the primary. Hidden/canceled/replaced windows must not be raised.
5. Close/Cancel/blur and re-show while requests are pending/unknown. Preserve actionable text, explain every unknown-outcome send block, and clear that uncertainty when its terminal reply arrives.
6. StrictMode and actual view remount, including a terminal reply during the unmounted gap and a delayed/failed handshake. Keep one native listener set and one send per user submission; dispose listeners/callback capabilities only with the document.

The 30-second expiry revokes only accepted-result native hide/focus capability. Pending/unknown request text is never evicted by that timer; backend cancellation is not claimed.
