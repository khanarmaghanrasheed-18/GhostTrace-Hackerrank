/* App-wide state: the server status, background jobs, and page clean-up. */

import {api, toast} from "./core.js";

export const state = {status: null};
const listeners = new Set();
let leaving = [];
let lastReading = null;

export async function refreshStatus() {
  const status = await api("/api/status");
  const wasReading = lastReading;
  lastReading = status.reading ? status.reading.id : null;
  state.status = status;
  for (const fn of [...listeners]) {
    try { fn(status); } catch (err) { console.error(err); }
  }
  if (wasReading && !lastReading) toast(`Files are ready: ${status.sources.toLocaleString()} files, ${status.records.toLocaleString()} records.`);
  return status;
}

/** Call fn whenever the status changes, until the page is left. */
export function onStatus(fn) {
  listeners.add(fn);
  onLeave(() => listeners.delete(fn));
}

/** Call fn whenever the status changes, for as long as the app is open (the shell uses this). */
export function onStatusAlways(fn) { listeners.add(fn); }

/** Run fn when the current page is replaced. */
export function onLeave(fn) { leaving.push(fn); }

export function leavePage() {
  const fns = leaving;
  leaving = [];
  for (const fn of fns) {
    try { fn(); } catch (err) { console.error(err); }
  }
}

// The status is fetched often while files are being read, and now and then otherwise.
let timer = null;
export function startPolling() {
  const tick = async () => {
    try { await refreshStatus(); } catch (err) { /* the server may be restarting */ }
    timer = setTimeout(tick, state.status && state.status.reading ? 1200 : 8000);
  };
  if (!timer) tick();
}
export function pollSoon() {
  clearTimeout(timer);
  timer = null;
  setTimeout(startPolling, 300);
}
