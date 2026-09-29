/**
 * Unit tests for the reconnect scheduling and version resolution guards.
 *
 * Regression tests for the reconnect-wedge trap: startSocket() awaits
 * network I/O (fetchLatestBaileysVersion has no AbortSignal) before it
 * creates a socket, and the close handler used to re-enter it via a bare
 * `setTimeout(startSocket, ...)`. A rejection was unhandled and a stalled
 * fetch left the bridge permanently disconnected while its HTTP server
 * kept answering 503 — observed in the field as a bridge that logged
 * "Reconnecting in 3s..." once and then went silent for 27+ hours.
 *
 * These tests avoid importing bridge.js because that file starts an HTTP
 * server and Baileys socket at module load. Keep the helper module pure.
 */

import { strict as assert } from 'node:assert';

import {
  createExponentialBackoff,
  createReconnectScheduler,
  createVersionResolver,
} from './bridge_helpers.js';

const tick = () => new Promise(resolve => setImmediate(resolve));

// -- createExponentialBackoff ----------------------------------------------

// Delays double per consecutive attempt and are capped at maxDelayMs.
{
  const backoff = createExponentialBackoff({ baseDelayMs: 3000, factor: 2, maxDelayMs: 24000 });
  assert.deepEqual(
    [backoff.next(), backoff.next(), backoff.next(), backoff.next()],
    [3000, 6000, 12000, 24000],
  );
  // Capped: stays at max, never exceeds it.
  assert.equal(backoff.next(), 24000);
  assert.equal(backoff.next(), 24000);
  assert.equal(backoff.getAttempts(), 6);
}

// reset() returns the sequence to the base delay (stable connection).
{
  const backoff = createExponentialBackoff({ baseDelayMs: 1000, maxDelayMs: 8000 });
  backoff.next();
  backoff.next();
  assert.equal(backoff.getAttempts(), 2);
  backoff.reset();
  assert.equal(backoff.getAttempts(), 0);
  assert.equal(backoff.next(), 1000);
}

// Defaults: 3s base, x2, 5-minute cap.
{
  const backoff = createExponentialBackoff();
  assert.equal(backoff.next(), 3000);
  assert.equal(backoff.next(), 6000);
  assert.equal(backoff.next(), 12000);
  // Reaches and clamps at the 5-minute cap after enough consecutive failures.
  const sequence = [];
  for (let i = 0; i < 8; i += 1) sequence.push(backoff.next());
  assert.equal(sequence[sequence.length - 1], 300000);
  assert.equal(backoff.next(), 300000); // stays clamped
}

// -- createReconnectScheduler ---------------------------------------------

// A rejecting start function is caught and rescheduled at the retry delay;
// a subsequent success stops the retry chain.
{
  const timers = [];
  const logs = [];
  let attempts = 0;
  const startFn = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('boom');
  };

  const schedule = createReconnectScheduler(startFn, {
    retryDelayMs: 5000,
    log: line => logs.push(line),
    setTimeoutFn: (fn, ms) => timers.push({ fn, ms }),
  });

  schedule(3000);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 3000);

  timers[0].fn();
  await tick();
  await tick();

  assert.equal(attempts, 1);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /boom/);
  assert.equal(timers.length, 2, 'rejection must schedule a retry');
  assert.equal(timers[1].ms, 5000);

  timers[1].fn();
  await tick();
  await tick();

  assert.equal(attempts, 2);
  assert.equal(timers.length, 2, 'success must not schedule another attempt');
  assert.equal(logs.length, 1);
}

// A synchronous throw from the start function is contained the same way as
// an async rejection.
{
  const timers = [];
  const logs = [];
  const schedule = createReconnectScheduler(
    () => { throw new Error('sync boom'); },
    {
      retryDelayMs: 1000,
      log: line => logs.push(line),
      setTimeoutFn: (fn, ms) => timers.push({ fn, ms }),
    },
  );

  schedule(0);
  timers[0].fn();
  await tick();
  await tick();

  assert.equal(logs.length, 1);
  assert.match(logs[0], /sync boom/);
  assert.equal(timers.length, 2);
}

// -- createVersionResolver ------------------------------------------------

// A successful fetch returns and caches the version.
{
  const resolveVersion = createVersionResolver(
    async () => ({ version: [2, 3000, 99] }),
    { log: () => {} },
  );
  assert.deepEqual(await resolveVersion(), [2, 3000, 99]);
}

// A fetch that never settles resolves within the timeout bound instead of
// pending forever; before any success there is no cache, so the resolver
// yields null (callers fall back to the Baileys default).
{
  const logs = [];
  const resolveVersion = createVersionResolver(
    () => new Promise(() => {}),
    { timeoutMs: 20, log: line => logs.push(line) },
  );
  assert.equal(await resolveVersion(), null);
  assert.equal(logs.length, 1);
}

// After one success, later failures fall back to the cached version.
{
  const logs = [];
  let calls = 0;
  const resolveVersion = createVersionResolver(
    async () => {
      calls += 1;
      if (calls === 1) return { version: [2, 3000, 42] };
      throw new Error('network down');
    },
    { timeoutMs: 20, log: line => logs.push(line) },
  );
  assert.deepEqual(await resolveVersion(), [2, 3000, 42]);
  assert.deepEqual(await resolveVersion(), [2, 3000, 42]);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /network down/);
}

console.log('bridge.reconnect.test.mjs: all assertions passed');
