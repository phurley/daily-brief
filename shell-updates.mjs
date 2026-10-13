// A shell update reloads each open report only after its files are cached.
// The worker also reloads legacy pages that do not implement this handshake.
export function connectShellUpdates(beforeReload = () => {}) {
  if (!('serviceWorker' in navigator)) return { check: async () => {} };
  navigator.serviceWorker.addEventListener('message', event => {
    if (event.data?.type !== 'BRIEF_SHELL_RELOAD') return;
    try { beforeReload(); } finally { event.ports[0]?.postMessage('ready'); }
  });
  let registration;
  function register() {
    if (!registration) registration = navigator.serviceWorker.register('./sw.js', { updateViaCache: 'none' })
      .catch(() => { registration = null; return null; });
    return registration;
  }
  void register();
  let checking;
  return {
    check() {
      if (!checking) checking = register().then(async worker => {
        if (worker) await worker.update();
      }).catch(() => {}).finally(() => { checking = null; });
      return checking;
    },
  };
}

const RELOAD_STATE = 'daily-brief:shell-reload:v1';
export function saveShellReloadState() {
  const panel = document.querySelector('#recommendation-controls');
  const details = [...document.querySelectorAll('details[id]')].map(element => [element.id, element.open]);
  const fields = [...(panel?.querySelectorAll('input[name]') || [])].map(element =>
    [element.name, element.type === 'checkbox' ? element.checked : element.value]);
  try {
    sessionStorage.setItem(RELOAD_STATE, JSON.stringify({
      url: location.pathname + location.search, details, fields,
      preferencesOpen: Boolean(panel?.querySelector('details')?.open), scrollY: window.scrollY,
    }));
  } catch { /* Saved preferences and weights remain in their existing stores. */ }
}

export function readShellReloadState() {
  try {
    const state = JSON.parse(sessionStorage.getItem(RELOAD_STATE));
    sessionStorage.removeItem(RELOAD_STATE);
    return state?.url === location.pathname + location.search ? state : null;
  } catch { return null; }
}

export function restoreShellReloadState(state) {
  if (!state) return;
  for (const [id, open] of state.details || []) {
    const element = document.getElementById(id);
    if (element?.tagName === 'DETAILS') element.open = open;
  }
  const panel = document.querySelector('#recommendation-controls');
  const settings = panel?.querySelector('details');
  if (settings) settings.open = state.preferencesOpen;
  for (const [name, value] of state.fields || []) {
    const element = [...(panel?.querySelectorAll('input[name]') || [])].find(input => input.name === name);
    if (!element) continue;
    if (element.type === 'checkbox') element.checked = value;
    else element.value = value;
  }
  requestAnimationFrame(() => window.scrollTo({ top: state.scrollY, behavior: 'instant' }));
}
