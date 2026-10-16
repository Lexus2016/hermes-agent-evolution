import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { $desktopBoot } from '@/store/boot'
import { $notifications } from '@/store/notifications'
import { $desktopOnboarding } from '@/store/onboarding'
import { $connection } from '@/store/session'

import { BootFailureOverlay } from './boot-failure-overlay'

// Remote-backend users hit a hard boot failure that isn't OAuth reauth (token
// auth, wrong URL, unreachable host). The recovery screen must let them fix the
// remote connection in place — the "Connection settings" action swaps the card
// to an in-line connect form — instead of stranding them (the old bug forced a
// hand-edit of connection.json).

function failBoot() {
  $desktopBoot.set({
    error: 'Could not connect to Hermes gateway',
    fakeMode: false,
    message: 'boot failed',
    phase: 'renderer.error',
    progress: 40,
    running: false,
    timestamp: Date.now(),
    visible: true
  })
}

function stubDesktop(config: Record<string, unknown>, overrides: Record<string, unknown> = {}) {
  const original = window.hermesDesktop
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      getRecentLogs: async () => ({ lines: [] }),
      getConnectionConfig: async () => config,
      getBootstrapState: async () => ({
        active: false,
        manifest: null,
        stages: {},
        error: null,
        log: [],
        startedAt: null,
        completedAt: null,
        setupChoice: null,
        unsupportedPlatform: null,
        bundled: false
      }),
      ...overrides
    }
  })

  return () => Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
}

const remoteToken = {
  envOverride: false,
  mode: 'remote',
  profile: null,
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: true,
  remoteUrl: 'http://100.116.104.53:9191',
  cloudOrg: ''
}

beforeEach(() => {
  $desktopOnboarding.set({
    configured: true,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false,
    freeTierReady: false
  })
  failBoot()
})

afterEach(cleanup)

describe('BootFailureOverlay', () => {
  it('keeps keyboard focus inside the recovery surface', () => {
    render(
      <>
        <button type="button">Background action</button>
        <BootFailureOverlay />
      </>
    )

    const recoverySurface = screen.getByRole('dialog', { name: /Hermes couldn't start/i })
    const retry = screen.getByRole('button', { name: /retry/i })
    const backgroundAction = screen.getByText(/background action/i)

    retry.focus()
    backgroundAction.focus()

    expect(recoverySurface.getAttribute('aria-modal')).toBe('true')
    expect(recoverySurface.contains(globalThis.document.activeElement)).toBe(true)
  })

  it('swaps to the in-place gateway settings view (no route nav) and back', async () => {
    render(<BootFailureOverlay />)

    fireEvent.click(screen.getByRole('button', { name: /gateway settings/i }))
    // Recovery actions give way to the embedded panel (behind a Back control).
    expect(await screen.findByRole('button', { name: /back/i })).toBeTruthy()
    expect(screen.getByRole('dialog', { name: /gateway settings/i }).getAttribute('aria-modal')).toBe('true')
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /back/i }))
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /back/i })).toBeNull()
  })

  it('hides the modal on dismiss without clearing the boot error', () => {
    const { rerender } = render(<BootFailureOverlay />)
    const error = $desktopBoot.get().error

    fireEvent.click(screen.getByRole('button', { name: /^close$/i }))

    expect(screen.queryByRole('dialog')).toBeNull()
    expect($desktopBoot.get().error).toBe(error)
    expect($desktopBoot.get().error).toBeTruthy()

    $desktopBoot.set({ ...$desktopBoot.get(), error: 'A different startup failure' })
    rerender(<BootFailureOverlay />)
    expect(screen.getByRole('dialog', { name: /Hermes couldn't start/i })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /^close$/i }))
    expect(screen.queryByRole('dialog')).toBeNull()

    $desktopBoot.set({ ...$desktopBoot.get(), error: null, running: false })
    rerender(<BootFailureOverlay />)
    $desktopBoot.set({ ...$desktopBoot.get(), error, running: false })
    rerender(<BootFailureOverlay />)

    expect(screen.getByRole('dialog', { name: /Hermes couldn't start/i })).toBeTruthy()
  })

  it('dismisses on Escape and keeps the boot error latched', () => {
    render(<BootFailureOverlay />)
    const error = $desktopBoot.get().error

    fireEvent.keyDown(screen.getByRole('dialog', { name: /Hermes couldn't start/i }), { key: 'Escape' })

    expect(screen.queryByRole('dialog')).toBeNull()
    expect($desktopBoot.get().error).toBe(error)
  })

  it('dismisses from the embedded gateway settings view', async () => {
    render(<BootFailureOverlay />)

    fireEvent.click(screen.getByRole('button', { name: /gateway settings/i }))
    expect(await screen.findByRole('dialog', { name: /gateway settings/i })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /^close$/i }))

    expect(screen.queryByRole('dialog')).toBeNull()
    expect($desktopBoot.get().error).toBeTruthy()
  })

  it('re-shows the same error after a retry starts and fails again', () => {
    render(<BootFailureOverlay />)
    const error = $desktopBoot.get().error

    fireEvent.click(screen.getByRole('button', { name: /^close$/i }))
    expect(screen.queryByRole('dialog')).toBeNull()

    act(() => $desktopBoot.set({ ...$desktopBoot.get(), running: true }))
    act(() => $desktopBoot.set({ ...$desktopBoot.get(), error, running: false }))

    expect(screen.getByRole('dialog', { name: /Hermes couldn't start/i })).toBeTruthy()
  })

  it('drops local-only Repair and Use-local-gateway on a local failure', () => {
    render(<BootFailureOverlay />)
    // No connection config stub → treated as a local failure.
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /repair/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /use local gateway/i })).toBeNull()
  })

  it('leads with Gateway settings and drops Repair for a remote (token) failure', async () => {
    const restore = stubDesktop(remoteToken)

    try {
      render(<BootFailureOverlay />)
      await waitFor(() => expect(screen.queryByRole('button', { name: /repair/i })).toBeNull())
      expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /use local gateway/i })).toBeTruthy()
    } finally {
      restore()
    }
  })

  it('opens gateway settings with a partial persisted remote config', async () => {
    const restore = stubDesktop({ mode: 'remote', remoteAuthMode: undefined, remoteUrl: undefined })

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(screen.getByRole('button', { name: /gateway settings/i }))

      expect(await screen.findByRole('button', { name: /back/i })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    } finally {
      restore()
    }
  })

  it('clears and signs in only the failed gateway once', async () => {
    const gatewayUrl = 'http://100.116.104.53:9191'
    const logout = vi.fn().mockResolvedValue({ ok: true, connected: false })
    const login = vi.fn().mockResolvedValue({ ok: true, connected: false })

    const restore = stubDesktop(
      {
        ...remoteToken,
        remoteAuthMode: 'oauth',
        remoteOauthConnected: false,
        remoteTokenSet: false,
        remoteUrl: gatewayUrl
      },
      {
        oauthLoginConnectionConfig: login,
        oauthLogoutConnectionConfig: logout,
        probeConnectionConfig: vi.fn().mockResolvedValue({ providers: [{ id: 'basic', type: 'password' }] })
      }
    )

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /sign out & sign in/i }))

      await waitFor(() => expect(login).toHaveBeenCalledWith(gatewayUrl))
      expect(logout).toHaveBeenCalledTimes(1)
      expect(logout).toHaveBeenCalledWith(gatewayUrl)
      expect(login).toHaveBeenCalledTimes(1)
    } finally {
      restore()
    }
  })

  it('recovers a cloud connection through the portal cascade instead of native OAuth', async () => {
    const gatewayUrl = 'https://agent-1.agents.nousresearch.com'
    const logout = vi.fn().mockResolvedValue({ ok: true, connected: false })
    const nativeLogin = vi.fn().mockResolvedValue({ ok: true, connected: false })
    const cloudStatus = vi.fn().mockResolvedValue({ portalBaseUrl: 'https://portal.nousresearch.com', signedIn: false })

    const cloudLogin = vi.fn().mockResolvedValue({
      ok: true,
      portalBaseUrl: 'https://portal.nousresearch.com',
      signedIn: true
    })

    const cloudAgentSignIn = vi.fn().mockResolvedValue({ baseUrl: gatewayUrl, connected: false })

    const restore = stubDesktop(
      {
        ...remoteToken,
        mode: 'cloud',
        remoteAuthMode: 'oauth',
        remoteOauthConnected: false,
        remoteTokenSet: false,
        remoteUrl: gatewayUrl
      },
      {
        cloud: { status: cloudStatus, login: cloudLogin, agentSignIn: cloudAgentSignIn },
        oauthLoginConnectionConfig: nativeLogin,
        oauthLogoutConnectionConfig: logout,
        probeConnectionConfig: vi.fn().mockResolvedValue({ providers: [{ id: 'nous', type: 'oauth' }] })
      }
    )

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /sign in/i }))

      await waitFor(() => expect(cloudAgentSignIn).toHaveBeenCalledWith(gatewayUrl))
      // The ladder owns the logout: exactly one drop of this gateway's cookies.
      expect(logout).toHaveBeenCalledExactlyOnceWith(gatewayUrl)
      expect(cloudStatus).toHaveBeenCalledTimes(1)
      expect(cloudLogin).toHaveBeenCalledTimes(1)
      expect(nativeLogin).not.toHaveBeenCalled()
    } finally {
      restore()
    }
  })

  it('shows the Nous Cloud down recovery when the backend flags isCloudBackendDown', async () => {
    const restore = stubDesktop(remoteToken)
    $desktopBoot.set({
      error: 'Nous Cloud agent ares-3009.agents.nousresearch.com is down (HTTP 503: server-side fault).',
      fakeMode: false,
      isCloudBackendDown: true,
      message: 'boot failed',
      phase: 'renderer.error',
      progress: 40,
      running: false,
      statusCode: 503,
      timestamp: Date.now(),
      visible: true
    })

    try {
      render(<BootFailureOverlay />)
      // Cloud-specific title + actionable recovery instead of the generic
      // remote-failure copy.
      expect(await screen.findByText(/Nous Cloud agent is down/i)).toBeTruthy()
      // Portal and Discord are dedicated action buttons (localized labels
      // can't drift the URLs, which live in code).
      expect(screen.getByRole('button', { name: /check portal status/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /get help on discord/i })).toBeTruthy()
      // Cloud-down is a remote failure: local-only Repair is dropped; the
      // actionable paths are Gateway settings + Use local gateway.
      expect(screen.queryByRole('button', { name: /repair/i })).toBeNull()
      expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /use local gateway/i })).toBeTruthy()
      // The electron-built error message (portal / local mode / Discord) is
      // still surfaced in the error box.
      expect(screen.getByText(/ares-3009\.agents\.nousresearch\.com/i)).toBeTruthy()
    } finally {
      restore()
    }
  })

  const bundledState = {
    active: false,
    manifest: null,
    stages: {},
    error: null,
    log: [],
    startedAt: null,
    completedAt: null,
    setupChoice: null,
    unsupportedPlatform: null,
    bundled: true
  }

  it('offers "Reinstall the app" on a bundled install only when the payload itself is damaged', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)
    const restore = stubDesktop({ mode: 'local' }, { getBootstrapState: async () => bundledState, openExternal })
    $desktopBoot.set({
      ...$desktopBoot.get(),
      error:
        'This app bundles its own Hermes runtime, but the runtime files are missing or damaged. Reinstall Hermes Desktop to restore it.'
    })

    try {
      render(<BootFailureOverlay />)

      // The bundled artifact has no installer to repair with — the action is
      // a docs link, and the hint says reinstall instead of re-run installer.
      expect(await screen.findByRole('button', { name: /reinstall the app/i })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /repair install/i })).toBeNull()
      expect(screen.getByText(/reinstall the app to restore/i)).toBeTruthy()

      fireEvent.click(screen.getByRole('button', { name: /reinstall the app/i }))
      await waitFor(() =>
        expect(openExternal).toHaveBeenCalledWith('https://hermes-agent.nousresearch.com/docs/user-guide/desktop')
      )
    } finally {
      restore()
    }
  })

  it('a bundled install with an unrelated failure gets neither Repair nor Reinstall', async () => {
    const restore = stubDesktop({ mode: 'local' }, { getBootstrapState: async () => bundledState })
    $desktopBoot.set({ ...$desktopBoot.get(), error: 'listen EADDRINUSE: address already in use 127.0.0.1:8642' })

    try {
      render(<BootFailureOverlay />)

      expect(await screen.findByRole('button', { name: /retry/i })).toBeTruthy()
      // Wait for the bundled snapshot to land before asserting the negatives.
      await waitFor(() => expect(screen.queryByRole('button', { name: /repair install/i })).toBeNull())
      expect(screen.queryByRole('button', { name: /reinstall the app/i })).toBeNull()
      expect(screen.queryByText(/reinstall the app to restore/i)).toBeNull()
    } finally {
      restore()
    }
  })

  it.each(['refused', 'thrown', 'unavailable'])('preserves a %s repair failure without reloading', async failure => {
    const reload = vi.fn()
    const originalLocation = Object.getOwnPropertyDescriptor(window, 'location')!
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload }
    })

    const repair =
      failure === 'unavailable'
        ? undefined
        : vi.fn(async () => {
            if (failure === 'thrown') {
              throw new Error('installer permission denied')
            }

            return { ok: false, error: 'bundled-immutable' }
          })

    const restore = stubDesktop({ mode: 'local' }, { repairBootstrap: repair })

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /repair install/i }))

      const message =
        failure === 'thrown'
          ? 'installer permission denied'
          : failure === 'refused'
            ? en.boot.failure.bundledReinstallHint
            : en.boot.errors.ipcBridgeUnavailable

      await waitFor(() =>
        expect($notifications.get()).toEqual(
          expect.arrayContaining([expect.objectContaining({ kind: 'error', message })])
        )
      )
      expect(reload).not.toHaveBeenCalled()
      expect(screen.getByRole('button', { name: /repair install/i }).hasAttribute('disabled')).toBe(false)
    } finally {
      restore()
      Object.defineProperty(window, 'location', originalLocation)
      $notifications.set([])
    }
  })

  it('reloads after an accepted repair', async () => {
    const reload = vi.fn()
    const originalLocation = Object.getOwnPropertyDescriptor(window, 'location')!
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload }
    })

    const restore = stubDesktop({ mode: 'local' }, { repairBootstrap: vi.fn().mockResolvedValue({ ok: true }) })

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /repair install/i }))

      await waitFor(() => expect(reload).toHaveBeenCalled())
      expect($notifications.get().some(n => n.kind === 'error')).toBe(false)
    } finally {
      restore()
      Object.defineProperty(window, 'location', originalLocation)
      $notifications.set([])
    }
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

function recoveryHarness() {
  let config = {
    ...remoteToken,
    remoteAuthMode: 'oauth',
    remoteTokenSet: false,
    remoteUrl: 'https://gateway-a.invalid'
  }

  const getConfig = vi.fn(async () => config)
  const probe = vi.fn(async () => ({ providers: [{ id: 'basic', type: 'password' }] }))
  const logout = vi.fn(async () => ({ ok: true, connected: false }))
  const login = vi.fn(async () => ({ ok: true, connected: false }))
  const reset = vi.fn(async () => undefined)
  const reload = vi.fn()
  const originalLocation = Object.getOwnPropertyDescriptor(window, 'location')!
  Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, reload } })

  const restore = stubDesktop(config, {
    getConnectionConfig: getConfig,
    probeConnectionConfig: probe,
    oauthLogoutConnectionConfig: logout,
    oauthLoginConnectionConfig: login,
    resetBootstrap: reset
  })

  return {
    getConfig,
    probe,
    logout,
    login,
    reset,
    reload,
    switchTo(patch: Partial<typeof config>, error = 'A different boot failure') {
      config = { ...config, ...patch }
      $desktopBoot.set({ ...$desktopBoot.get(), error })
    },
    restore() {
      cleanup()
      restore()
      Object.defineProperty(window, 'location', originalLocation)
      $connection.set(null)
      $notifications.set([])
    }
  }
}

describe('boot recovery ownership', () => {
  it('removes A sign-in when visible B is a non-reauth failure', async () => {
    const h = recoveryHarness()

    try {
      render(<BootFailureOverlay />)
      await screen.findByRole('button', { name: /sign out & sign in/i })
      act(() => h.switchTo({ mode: 'cloud', remoteAuthMode: 'token', remoteUrl: 'https://gateway-b.invalid' }))
      await waitFor(() => expect(h.getConfig).toHaveBeenCalledTimes(2))
      expect(screen.queryByRole('button', { name: /sign out & sign in/i })).toBeNull()
      expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
      expect(h.logout).not.toHaveBeenCalled()
      expect(h.login).not.toHaveBeenCalled()
    } finally {
      h.restore()
    }
  })

  it.each(['missing', 'rejected'])('clears A recovery when B config lookup is %s', async failure => {
    const h = recoveryHarness()

    try {
      render(<BootFailureOverlay />)
      await screen.findByRole('button', { name: /sign out & sign in/i })

      if (failure === 'missing') {
        Object.defineProperty(window.hermesDesktop, 'getConnectionConfig', { value: undefined, configurable: true })
      } else {
        h.getConfig.mockRejectedValueOnce(new Error('synthetic lookup failure'))
      }

      act(() => h.switchTo({ remoteUrl: 'https://gateway-b.invalid' }))
      await act(async () => undefined)
      expect(screen.queryByRole('button', { name: /sign out & sign in/i })).toBeNull()
      expect(h.logout).not.toHaveBeenCalled()
    } finally {
      h.restore()
    }
  })

  it('ignores reversed old probes and repeated switches while the current owner remains actionable', async () => {
    const h = recoveryHarness()
    const oldProbe = deferred<Awaited<ReturnType<typeof h.probe>>>()
    const middleProbe = deferred<Awaited<ReturnType<typeof h.probe>>>()
    h.probe.mockReturnValueOnce(oldProbe.promise).mockReturnValueOnce(middleProbe.promise)

    try {
      render(<BootFailureOverlay />)
      await waitFor(() => expect(h.probe).toHaveBeenCalledTimes(1))
      act(() => h.switchTo({ remoteUrl: 'https://gateway-b.invalid' }, 'B failure'))
      await waitFor(() => expect(h.probe).toHaveBeenCalledTimes(2))
      act(() => h.switchTo({ remoteUrl: 'https://gateway-c.invalid' }, 'C failure'))
      await screen.findByRole('button', { name: /sign out & sign in/i })
      await act(async () => {
        middleProbe.resolve({ providers: [] })
        oldProbe.resolve({ providers: [] })
      })
      fireEvent.click(screen.getByRole('button', { name: /sign out & sign in/i }))
      await waitFor(() => expect(h.login).toHaveBeenCalledExactlyOnceWith('https://gateway-c.invalid'))
      expect(h.logout).toHaveBeenCalledExactlyOnceWith('https://gateway-c.invalid')
    } finally {
      h.restore()
    }
  })

  it('invalidates a same-error connection switch before React cleanup can run', async () => {
    const h = recoveryHarness()
    const configRead = deferred<Awaited<ReturnType<typeof h.getConfig>>>()

    try {
      render(<BootFailureOverlay />)
      const signIn = await screen.findByRole('button', { name: /sign out & sign in/i })
      h.getConfig.mockReturnValueOnce(configRead.promise)
      fireEvent.click(signIn)
      await act(async () => {
        $connection.set({
          baseUrl: 'https://gateway-b.invalid',
          connectionId: 'b',
          isFullscreen: false,
          nativeOverlayWidth: 0,
          token: '',
          wsUrl: '',
          logs: [],
          windowButtonPosition: null
        })
        configRead.resolve({
          ...remoteToken,
          remoteAuthMode: 'oauth',
          remoteTokenSet: false,
          remoteUrl: 'https://gateway-a.invalid'
        })
      })
      expect(h.logout).not.toHaveBeenCalled()
      expect(h.login).not.toHaveBeenCalled()
      expect(h.reset).not.toHaveBeenCalled()
      expect(h.reload).not.toHaveBeenCalled()
    } finally {
      h.restore()
    }
  })

  it.each(['changed', 'rejected', 'missing'])(
    'revalidates the config owner before sign-in when lookup is %s',
    async failure => {
      const h = recoveryHarness()

      try {
        render(<BootFailureOverlay />)
        const signIn = await screen.findByRole('button', { name: /sign out & sign in/i })

        if (failure === 'changed') {
          h.getConfig.mockResolvedValueOnce({ ...remoteToken, mode: 'cloud', remoteUrl: 'https://gateway-b.invalid' })
        } else if (failure === 'rejected') {
          h.getConfig.mockRejectedValueOnce(new Error('synthetic lookup failure'))
        } else {
          Object.defineProperty(window.hermesDesktop, 'getConnectionConfig', { value: undefined, configurable: true })
        }

        fireEvent.click(signIn)
        await waitFor(() => expect(screen.queryByRole('button', { name: /sign out & sign in/i })).toBeNull())
        expect(h.logout).not.toHaveBeenCalled()
        expect(h.login).not.toHaveBeenCalled()
      } finally {
        h.restore()
      }
    }
  )

  it('does not let pending A sign-in reload B or clear B sign-in busy state', async () => {
    const h = recoveryHarness()
    const firstLogin = deferred<Awaited<ReturnType<typeof h.login>>>()
    const nextLogin = deferred<Awaited<ReturnType<typeof h.login>>>()
    h.login.mockReturnValueOnce(firstLogin.promise).mockReturnValueOnce(nextLogin.promise)

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /sign out & sign in/i }))
      await waitFor(() => expect(h.login).toHaveBeenCalledTimes(1))
      act(() => h.switchTo({ remoteUrl: 'https://gateway-b.invalid' }))
      const nextSignIn = await screen.findByRole('button', { name: /sign out & sign in/i })
      await waitFor(() => expect(nextSignIn.hasAttribute('disabled')).toBe(false))
      fireEvent.click(nextSignIn)
      await waitFor(() => expect(h.login).toHaveBeenCalledTimes(2))
      await act(async () => firstLogin.resolve({ ok: true, connected: true }))
      expect(h.reset).not.toHaveBeenCalled()
      expect(h.reload).not.toHaveBeenCalled()
      expect(nextSignIn.hasAttribute('disabled')).toBe(true)
      await act(async () => nextLogin.resolve({ ok: true, connected: false }))
      expect(nextSignIn.hasAttribute('disabled')).toBe(false)
    } finally {
      h.restore()
    }
  })

  it('does not reset or reload B after an already-started cloud A cascade completes', async () => {
    const h = recoveryHarness()
    const cascade = deferred<{ baseUrl: string; connected: boolean }>()
    const agentSignIn = vi.fn(() => cascade.promise)
    Object.defineProperty(window.hermesDesktop, 'cloud', {
      configurable: true,
      value: {
        status: vi.fn(async () => ({ signedIn: true })),
        agentSignIn
      }
    })
    h.switchTo({ mode: 'cloud' }, 'Cloud A failure')

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /sign out & sign in/i }))
      await waitFor(() => expect(agentSignIn).toHaveBeenCalledWith('https://gateway-a.invalid'))
      act(() => h.switchTo({ mode: 'remote', remoteAuthMode: 'token', remoteUrl: 'https://gateway-b.invalid' }))
      await act(async () => cascade.resolve({ baseUrl: 'https://gateway-a.invalid', connected: true }))
      expect(h.reset).not.toHaveBeenCalled()
      expect(h.reload).not.toHaveBeenCalled()
    } finally {
      h.restore()
    }
  })

  it.each(['remote', 'cloud'])('completes one current %s recovery and reloads', async mode => {
    const h = recoveryHarness()
    h.login.mockResolvedValue({ ok: true, connected: true })
    const agentSignIn = vi.fn(async () => ({ baseUrl: 'https://gateway-a.invalid', connected: true }))
    Object.defineProperty(window.hermesDesktop, 'cloud', {
      configurable: true,
      value: {
        status: vi.fn(async () => ({ signedIn: true })),
        agentSignIn
      }
    })
    h.switchTo({ mode }, 'Current owner failure')

    try {
      render(<BootFailureOverlay />)
      const button = await screen.findByRole('button', { name: /sign out & sign in/i })
      fireEvent.click(button)
      fireEvent.click(button)
      await waitFor(() => expect(h.reload).toHaveBeenCalledTimes(1))
      expect(h.logout).toHaveBeenCalledExactlyOnceWith('https://gateway-a.invalid')
      expect(h.reset).toHaveBeenCalledTimes(mode === 'cloud' ? 1 : 0)
      expect(h.login).toHaveBeenCalledTimes(mode === 'remote' ? 1 : 0)
      expect(agentSignIn).toHaveBeenCalledTimes(mode === 'cloud' ? 1 : 0)
    } finally {
      h.restore()
    }
  })

  it('does not reload B when A reset was already started before the switch', async () => {
    const h = recoveryHarness()
    const reset = deferred<void>()
    h.reset.mockReturnValueOnce(reset.promise)
    Object.defineProperty(window.hermesDesktop, 'cloud', {
      configurable: true,
      value: {
        status: vi.fn(async () => ({ signedIn: true })),
        agentSignIn: vi.fn(async () => ({ baseUrl: 'https://gateway-a.invalid', connected: true }))
      }
    })
    h.switchTo({ mode: 'cloud' }, 'Cloud A failure')

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /sign out & sign in/i }))
      await waitFor(() => expect(h.reset).toHaveBeenCalledTimes(1))
      act(() => h.switchTo({ remoteAuthMode: 'token', remoteUrl: 'https://gateway-b.invalid' }))
      await act(async () => reset.resolve())
      expect(h.reload).not.toHaveBeenCalled()
    } finally {
      h.restore()
    }
  })

  it.each(['probe', 'signin'])('does not publish or reload after unmount during %s', async stage => {
    const h = recoveryHarness()
    const probe = deferred<Awaited<ReturnType<typeof h.probe>>>()
    const login = deferred<Awaited<ReturnType<typeof h.login>>>()

    if (stage === 'probe') {
      h.probe.mockReturnValueOnce(probe.promise)
    } else {
      h.login.mockReturnValueOnce(login.promise)
    }

    try {
      const { unmount } = render(<BootFailureOverlay />)

      if (stage === 'probe') {
        await waitFor(() => expect(h.probe).toHaveBeenCalledTimes(1))
      } else {
        fireEvent.click(await screen.findByRole('button', { name: /sign out & sign in/i }))
        await waitFor(() => expect(h.login).toHaveBeenCalledTimes(1))
      }

      unmount()
      await act(async () => {
        probe.resolve({ providers: [] })
        login.resolve({ ok: true, connected: true })
      })
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(h.reset).not.toHaveBeenCalled()
      expect(h.reload).not.toHaveBeenCalled()
      expect($notifications.get().some(item => item.kind === 'success')).toBe(false)
    } finally {
      h.restore()
    }
  })
})
