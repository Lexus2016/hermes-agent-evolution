import { useStore } from '@nanostores/react'
import { Dialog as DialogPrimitive } from 'radix-ui'
import { type ComponentProps, lazy, type ReactNode, Suspense, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { DialogCloseButton } from '@/components/ui/dialog'
import { DialogPortalContainerContext } from '@/components/ui/dialog-portal-context'
import { ErrorIcon } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { LogView } from '@/components/ui/log-view'
import type { DesktopConnectionConfig, DesktopOauthLoginResult } from '@/global'
import { useI18n } from '@/i18n'
import { reestablishCloudAgentSession } from '@/lib/cloud-agent-session'
import { DESKTOP_DOCS_URL } from '@/lib/docs'
import { openExternalLink } from '@/lib/external-link'
import { ChevronLeft, ExternalLink, FileText, Loader2, LogIn, RefreshCw, SlidersHorizontal, Wrench } from '@/lib/icons'
import { $desktopBoot, type DesktopBootState } from '@/store/boot'
import { notify, notifyError } from '@/store/notifications'
import { $desktopOnboarding } from '@/store/onboarding'
import { $connection } from '@/store/session'

import { classifyLocalBootFailure, type LocalBootFailureCopy, localBootFailureCopy } from './boot-failure-cause'
import type { RemoteReauth } from './boot-failure-reauth'
import {
  deriveProviderShape,
  isRemoteConfig,
  isRemoteReauthFailure,
  signInLabel,
  sshFailureMessage
} from './boot-failure-reauth'

// The recovery "Gateway settings" view embeds the real Settings → Gateway panel
// (identical URL/auth/test/save controls — no parallel form to drift). Lazy so
// it stays out of the always-mounted overlay's bundle until opened.
const GatewaySettings = lazy(() =>
  import('@/app/settings/gateway-settings').then(module => ({ default: module.GatewaySettings }))
)

type BusyAction = 'local' | 'repair' | 'retry' | 'signin' | null
type RecoveryView = 'connect' | 'recovery'

interface BootRecovery {
  config: DesktopConnectionConfig
  reauth: RemoteReauth | null
  isCurrent: () => boolean
  invalidate: () => void
}

function sameRecoveryOwner(left: DesktopConnectionConfig, right: DesktopConnectionConfig) {
  return (
    left.mode === right.mode &&
    left.profile === right.profile &&
    left.remoteUrl === right.remoteUrl &&
    left.remoteAuthMode === right.remoteAuthMode &&
    left.cloudOrg === right.cloudOrg
  )
}

/** Classification and its sign-in URL are one failure-owned snapshot. */
function useBootRecovery(boot: DesktopBootState, visible: boolean) {
  const connection = useStore($connection)
  const [recovery, setRecovery] = useState<BootRecovery | null>(null)

  useEffect(() => {
    setRecovery(null)

    if (!visible) {
      return
    }

    let cancelled = false
    // Check the live stores too: a switch can happen before React runs cleanup.
    // The boot snapshot distinguishes repeated A → B → A failures.
    const isCurrent = () => !cancelled && $connection.get() === connection && $desktopBoot.get() === boot

    const invalidate = () => {
      if (isCurrent()) {
        cancelled = true
        setRecovery(null)
      }
    }
    void (async () => {
      const desktop = window.hermesDesktop

      if (!desktop?.getConnectionConfig) {
        return
      }

      let config: DesktopConnectionConfig

      try {
        config = { ...(await desktop.getConnectionConfig()) }
      } catch {
        return
      }

      if (!isCurrent()) {
        return
      }

      setRecovery({ config, reauth: null, isCurrent, invalidate })

      if (!isRemoteReauthFailure(config, boot.error)) {
        return
      }

      let shape = deriveProviderShape(null)

      try {
        const probe = await desktop.probeConnectionConfig(config.remoteUrl)
        shape = deriveProviderShape(probe?.providers)
      } catch {
        // The same owner remains actionable with generic provider copy.
      }

      if (isCurrent()) {
        setRecovery({ config, reauth: { url: config.remoteUrl, ...shape }, isCurrent, invalidate })
      }
    })()

    return () => {
      cancelled = true
    }
  }, [boot, visible, connection])

  return recovery?.isCurrent() ? recovery : null
}

// A remote gateway whose access cookie has lapsed (e.g. the dashboard
// restarted on the remote box) boots into this overlay with a reauth-shaped
// error. The local-recovery buttons (Retry resets the local bootstrap latch;
// Repair re-runs the installer) are no-ops for that case — the only fix is to
// re-establish the remote session. The detection + copy helpers live in
// ./boot-failure-reauth so they're unit-testable without a React render.

// Recovery surface for a hard boot failure (gateway never came up, backend
// exited during startup, bootstrap latched, …). Without this the app shell
// renders dead — "gateway offline", no composer, only a toast — with no way
// to retry, repair the install, switch the gateway, or find the logs.
function BootFailureModal({
  children,
  onDismiss,
  title
}: {
  children: ReactNode
  onDismiss: () => void
  title?: string
}) {
  const [contentNode, setContentNode] = useState<HTMLDivElement | null>(null)

  return (
    <DialogPrimitive.Root
      onOpenChange={open => {
        if (!open) {
          onDismiss()
        }
      }}
      open
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Content aria-describedby={undefined} aria-modal="true" asChild ref={setContentNode}>
          <div
            className="fixed inset-0 z-(--z-setup) flex items-center justify-center bg-(--ui-chat-surface-background) p-6"
            // Masks the whole app on boot failure — must stay filled under window
            // glass. Contract: `[data-glass-opaque]` in styles.css.
            data-glass-opaque=""
          >
            <DialogPortalContainerContext.Provider value={contentNode}>
              {title ? <DialogPrimitive.Title className="sr-only">{title}</DialogPrimitive.Title> : null}
              {children}
            </DialogPortalContainerContext.Provider>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

interface RecoveryPresentationInput {
  copy: ReturnType<typeof useI18n>['t']['boot']['failure']
  error: string | null
  remoteReauth: boolean
  remoteFailure: boolean
  cloudDown: boolean
  bundled: boolean
  label: string
  onSettings: () => void
  onRetry: () => void
  onLocal: () => void
  onSignIn: () => void
  onRepair: () => void
}

type RecoveryVariant = ComponentProps<typeof Button>['variant']
interface RecoveryAction {
  key: string
  label: string
  onClick: () => void
  icon?: ReactNode
  variant?: RecoveryVariant
  busy?: Exclude<BusyAction, null>
}

// Keep presentation policy separate from failure-owned asynchronous recovery.
function recoveryPresentation({
  copy,
  error,
  remoteReauth,
  remoteFailure,
  cloudDown,
  bundled,
  label,
  onSettings,
  onRetry,
  onLocal,
  onSignIn,
  onRepair
}: RecoveryPresentationInput) {
  const settingsAction: RecoveryAction = {
    key: 'settings',
    label: copy.gatewaySettings,
    onClick: onSettings,
    icon: <SlidersHorizontal />
  }

  const retryAction: RecoveryAction = {
    key: 'retry',
    label: copy.retry,
    onClick: onRetry,
    icon: <RefreshCw />,
    busy: 'retry'
  }

  const localAction: RecoveryAction = {
    key: 'local',
    label: copy.useLocalGateway,
    onClick: onLocal,
    variant: 'secondary',
    busy: 'local'
  }

  let actions: RecoveryAction[]
  let hint: string
  // The electron boot path flags a Nous Cloud backend-down (502/503/504) with
  // the structured isCloudBackendDown/statusCode it carries through boot
  // progress. When set, the recovery screen leads with the cloud-specific
  // guidance instead of the generic remote-failure copy (#85335).

  if (remoteReauth) {
    actions = [
      {
        key: 'signin',
        label: copy.signOutAndSignIn,
        onClick: onSignIn,
        icon: <LogIn />,
        busy: 'signin'
      },
      { ...settingsAction, variant: 'secondary' },
      localAction
    ]
    hint = copy.remoteSignInHint(label)
  } else if (cloudDown) {
    // A Nous Cloud agent is down — the user cannot restart the managed
    // instance and Repair is local-only. Lead with the paths that actually
    // resolve it: check the portal (status/instance controls), switch to the
    // local gateway, retry, or get support on Discord. Portal/Discord are
    // buttons (not URLs buried in the hint prose) so localized hints can't
    // drift the links.
    actions = [
      {
        key: 'portal',
        label: copy.cloudDownCheckPortal,
        onClick: () => openExternalLink('https://portal.nousresearch.com'),
        icon: <ExternalLink />
      },
      localAction,
      { ...retryAction, variant: 'secondary' },
      {
        key: 'discord',
        label: copy.cloudDownDiscord,
        onClick: () => openExternalLink('https://discord.gg/NousResearch'),
        variant: 'ghost'
      },
      { ...settingsAction, variant: 'ghost' }
    ]
    hint = copy.cloudDownHint
  } else if (remoteFailure) {
    actions = [settingsAction, { ...retryAction, variant: 'secondary' }, localAction]
    hint = copy.remoteFailureHint
  } else {
    // Local failure: Use-local is redundant with Retry (both re-target local), so
    // it's dropped here; keep it for remote failures where it's the fall-back.
    // A bundled install's payload is immutable, so there is no installer to
    // re-run: Repair is dropped, and "Reinstall the app" is offered only when
    // the payload itself is what's broken — a port clash or timeout on a
    // bundled install is not fixed by reinstalling.
    const damagedPayload: boolean = bundled && classifyLocalBootFailure(error) === 'installMissing'

    const fixAction: RecoveryAction | null = damagedPayload
      ? {
          key: 'reinstall',
          label: copy.reinstallApp,
          onClick: () => openExternalLink(DESKTOP_DOCS_URL),
          icon: <ExternalLink />,
          variant: 'secondary'
        }
      : bundled
        ? null
        : {
            key: 'repair',
            label: copy.repairInstall,
            onClick: onRepair,
            icon: <Wrench />,
            variant: 'secondary',
            busy: 'repair'
          }

    actions = [retryAction, ...(fixAction ? [fixAction] : []), { ...settingsAction, variant: 'ghost' }]
    hint = damagedPayload ? copy.bundledReinstallHint : bundled ? '' : copy.repairHint
  }

  const title = remoteReauth ? copy.remoteTitle : cloudDown ? copy.cloudDownTitle : copy.title
  const description = remoteReauth ? copy.remoteDescription : cloudDown ? copy.cloudDownDescription : copy.description

  return { actions, hint, title, description }
}

export function BootFailureOverlay() {
  const boot = useStore($desktopBoot)
  const onboarding = useStore($desktopOnboarding)
  const { t } = useI18n()
  const [busy, setBusy] = useState<BusyAction>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [showLogs, setShowLogs] = useState(false)
  const signInOwner = useRef<BootRecovery | null>(null)
  // A bundled install (payload ships in-app) has no installer to repair with.
  // Read from the bootstrap state snapshot so Repair is never offered there;
  // "Reinstall the app" replaces it only when the payload itself is damaged.
  const [bundled, setBundled] = useState(false)
  // Swap the card body to the embedded Gateway settings panel in place of routing
  // to the full Settings page (keeps the user on the recovery surface, no z-index
  // juggling, no second connection form to maintain).
  const [view, setView] = useState<RecoveryView>('recovery')
  // Dismissal hides the modal only. The latched boot error stays so recovery
  // can return, and a later clear or a different error shows the overlay again.
  const [dismissedError, setDismissedError] = useState<string | null>(null)

  const visible = Boolean(boot.error) && boot.error !== dismissedError && !boot.running
  // While first-run onboarding owns the picker/flow we let it surface its own
  // progress; the recovery overlay is for hard failures, which it covers via a
  // higher z-index regardless of onboarding state.
  const suppressed = onboarding.flow.status !== 'idle' && onboarding.flow.status !== 'error'
  const recovery = useBootRecovery(boot, visible)
  const remoteReauth = recovery?.reauth ?? null
  const connectionConfig = recovery?.config ?? null
  const remoteFailure = isRemoteConfig(connectionConfig)

  useEffect(() => {
    if (signInOwner.current && !signInOwner.current.isCurrent()) {
      setBusy(current => (current === 'signin' ? null : current))
    }
  }, [recovery])

  useEffect(() => {
    setDismissedError(current => (boot.running || current !== boot.error ? null : current))
  }, [boot.error, boot.running])

  useEffect(() => {
    if (!visible) {
      return
    }

    void window.hermesDesktop
      ?.getRecentLogs()
      .then(res => setLogs(res.lines ?? []))
      .catch(() => undefined)
  }, [boot.error, visible])

  // Bundled installs carry their runtime as an immutable payload — repair
  // would re-run an installer that must never fire for them. Resolve the
  // artifact kind from the bootstrap snapshot, including failures before setup.
  useEffect(() => {
    if (!visible) {
      return
    }

    let cancelled = false

    void window.hermesDesktop
      ?.getBootstrapState()
      .then(snapshot => {
        if (!cancelled && snapshot) {
          setBundled(snapshot.bundled)
        }
      })
      .catch(() => undefined)

    return () => {
      cancelled = true
    }
  }, [boot.error, visible])

  useEffect(() => {
    if (!visible) {
      setView('recovery')
    }
  }, [visible])

  if (!visible || suppressed) {
    return null
  }

  const retry = async () => {
    setBusy('retry')
    await window.hermesDesktop?.resetBootstrap().catch(() => undefined)
    window.location.reload()
  }

  const repair = async (): Promise<void> => {
    setBusy('repair')

    try {
      if (!window.hermesDesktop?.repairBootstrap) {
        throw new Error(t.boot.errors.ipcBridgeUnavailable)
      }

      const result = await window.hermesDesktop.repairBootstrap()

      // Main refuses repair on a bundled install (its stamp is authoritative;
      // our snapshot may be stale) — say what to do instead of the raw code.
      if (result?.error === 'bundled-immutable') {
        throw new Error(t.boot.failure.bundledReinstallHint)
      }

      if (!result?.ok) {
        throw new Error(result?.error || t.boot.errors.desktopBootFailed)
      }

      window.location.reload()
    } catch (error) {
      notifyError(error, t.boot.failure.repairInstall)
    } finally {
      setBusy(null)
    }
  }

  const switchToLocalGateway = async () => {
    setBusy('local')
    // Soft apply: tears down the primary and re-dials in place (shell stays).
    await window.hermesDesktop?.applyConnectionConfig({ mode: 'local' }).catch(() => undefined)
    setBusy(null)
  }

  // Clear this gateway's stale auth first, then re-establish it through the
  // connection's owning login flow. Hermes Cloud must reuse its portal session
  // and per-agent cascade; generic remote gateways use native/embedded OAuth.
  // Reload after success so boot mints a fresh ticket against the new session.
  // The cloud ladder is shared with Settings (reestablishCloudAgentSession) so
  // the boot recovery and the in-Settings recovery cannot drift apart.
  const signInRemote = async () => {
    const owner = recovery

    if (!owner?.reauth || signInOwner.current?.isCurrent()) {
      return
    }

    signInOwner.current = owner
    const isCurrent = () => signInOwner.current === owner && owner.isCurrent()
    const { config, reauth } = owner
    setBusy('signin')

    try {
      const desktop = window.hermesDesktop
      const currentConfig = await desktop?.getConnectionConfig?.().catch(() => null)

      if (!isCurrent()) {
        return
      }

      if (
        !currentConfig ||
        !sameRecoveryOwner(config, currentConfig) ||
        !isRemoteReauthFailure(currentConfig, boot.error)
      ) {
        owner.invalidate()

        return
      }

      let connected: boolean
      let error: string | undefined

      if (config.mode === 'cloud' && desktop.cloud) {
        const outcome = await reestablishCloudAgentSession(desktop, reauth.url)

        if (!isCurrent()) {
          return
        }

        if (outcome === 'portal-incomplete') {
          notify({
            kind: 'warning',
            title: t.boot.failure.signInIncompleteTitle,
            message: t.boot.failure.signInIncompleteMessage
          })

          return
        }

        connected = true
      } else {
        await desktop.oauthLogoutConnectionConfig?.(reauth.url)

        if (!isCurrent()) {
          return
        }

        const result: DesktopOauthLoginResult | undefined = await desktop.oauthLoginConnectionConfig(reauth.url)

        if (!isCurrent()) {
          return
        }

        connected = result?.connected === true
        error = result?.error
      }

      if (connected) {
        if (config.mode === 'cloud') {
          await desktop.resetBootstrap().catch(() => undefined)
        }

        if (!isCurrent()) {
          return
        }

        notify({ kind: 'success', title: t.boot.failure.signedInTitle, message: t.boot.failure.signedInMessage })
        window.location.reload()

        return
      }

      notify({
        kind: 'warning',
        title: t.boot.failure.signInIncompleteTitle,
        message: error ? `${t.boot.failure.signInIncompleteMessage}: ${error}` : t.boot.failure.signInIncompleteMessage
      })
    } catch (err) {
      if (isCurrent()) {
        notifyError(err, t.boot.failure.signInFailed)
      }
    } finally {
      if (isCurrent()) {
        signInOwner.current = null
        setBusy(null)
      }
    }
  }

  const openLogs = () => void window.hermesDesktop?.revealLogs().catch(() => undefined)

  const dismiss = () => setDismissedError(boot.error)

  const copy = t.boot.failure

  // SSH failures keep their own gloss; every other local failure is classified
  // into one plain sentence, raw output collapsed underneath (desktop-05).
  const failureCopy: LocalBootFailureCopy =
    connectionConfig?.mode === 'ssh'
      ? { headline: sshFailureMessage(connectionConfig, boot.error, t.settings.gateway), rawDetail: null }
      : localBootFailureCopy(boot.error, t.boot.causes)

  const label = signInLabel(remoteReauth, {
    identityProvider: copy.identityProvider,
    remoteGateway: copy.signInToRemoteGateway,
    withProvider: copy.signInWithProvider
  })

  const { actions, hint, title, description } = recoveryPresentation({
    copy,
    error: boot.error,
    remoteReauth: Boolean(remoteReauth),
    remoteFailure,
    cloudDown: Boolean(boot.isCloudBackendDown),
    bundled,
    label,
    onSettings: () => setView('connect'),
    onRetry: () => void retry(),
    onLocal: () => void switchToLocalGateway(),
    onSignIn: () => void signInRemote(),
    onRepair: () => void repair()
  })

  if (view === 'connect') {
    return (
      <BootFailureModal onDismiss={dismiss} title={copy.gatewaySettings}>
        <div className="relative flex max-h-[86vh] w-full max-w-[46rem] flex-col overflow-hidden rounded-xl border border-(--stroke-nous) bg-(--ui-chat-bubble-background) shadow-nous">
          <DialogCloseButton />
          {/* Subtle back affordance (projects/overlay idiom): muted → foreground
              on hover, no divider. */}
          <button
            className="flex w-full items-center gap-1.5 px-4 pt-4 text-left text-xs text-muted-foreground transition-colors hover:text-foreground"
            onClick={() => setView('recovery')}
            type="button"
          >
            <ChevronLeft className="size-3.5" />
            {copy.back}
          </button>
          <div className="min-h-0 flex-1 pt-4">
            <Suspense fallback={<Loader className="mx-auto my-16 size-6 text-(--ui-text-tertiary)" />}>
              <GatewaySettings embedded />
            </Suspense>
          </div>
        </div>
      </BootFailureModal>
    )
  }

  return (
    <BootFailureModal onDismiss={dismiss}>
      <div className="relative w-full max-w-[40rem] overflow-hidden rounded-xl border border-(--stroke-nous) bg-(--ui-chat-bubble-background) shadow-nous">
        <DialogCloseButton />
        <div className="flex items-start gap-3 px-5 py-4 pr-12">
          <ErrorIcon className="mt-0.5" size="1.25rem" />
          <div>
            <DialogPrimitive.Title asChild>
              <h2 className="text-[0.9375rem] font-semibold tracking-tight">{title}</h2>
            </DialogPrimitive.Title>
            <p className="mt-1 text-[0.8125rem] leading-5 text-(--ui-text-tertiary)">{description}</p>
          </div>
        </div>

        <div className="grid gap-4 p-5 pt-0">
          <div className="rounded-2xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-xs text-destructive">
            {failureCopy.headline}
            {failureCopy.rawDetail ? (
              <details className="mt-2 text-muted-foreground">
                <summary className="cursor-pointer select-none font-medium">{copy.details}</summary>
                <pre
                  className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap wrap-break-word font-mono text-[0.6875rem] leading-relaxed"
                  data-selectable-text="true"
                >
                  {failureCopy.rawDetail}
                </pre>
              </details>
            ) : null}
          </div>

          <div className="grid gap-2">
            <div className="flex flex-wrap gap-2">
              {actions.map(action => (
                <Button disabled={Boolean(busy)} key={action.key} onClick={action.onClick} variant={action.variant}>
                  {action.busy && busy === action.busy ? <Loader2 className="animate-spin" /> : action.icon}
                  {action.label}
                </Button>
              ))}
              <Button onClick={openLogs} variant="ghost">
                <FileText />
                {copy.openLogs}
              </Button>
            </div>
            {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
          </div>

          {logs.length > 0 ? (
            <div className="grid gap-2">
              <Button
                className="-ml-2 self-start font-medium"
                onClick={() => setShowLogs(v => !v)}
                size="xs"
                type="button"
                variant="text"
              >
                {showLogs ? copy.hideRecentLogs : copy.showRecentLogs}
              </Button>
              {showLogs ? <LogView className="max-h-48">{logs.slice(-40).join('')}</LogView> : null}
            </div>
          ) : null}
        </div>
      </div>
    </BootFailureModal>
  )
}
