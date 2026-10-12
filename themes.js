// Runs before styles are painted so returning readers see their saved theme immediately.
(() => {
  const key = 'daily-brief:theme:v1';
  const themes = {
    original: { color: '#233f4a' },
    newspaper: { color: '#f5f0e5', kicker: 'The Daily Brief', caption: 'Local life. Carefully reported.' },
    infographic: { color: '#f4f6fb', kicker: 'The day, decoded.', caption: 'Your daily field of view' },
    'star-trek': { color: '#080b14', kicker: 'Daily Brief / Command', caption: 'Sector 042 · Southeast Michigan' },
    redwood: { color: '#163f32', kicker: 'A little perspective.', caption: 'The Redwood edition / Daily Brief' },
    michigan: { color: '#00274c', kicker: 'Made for Michigan.', caption: 'The Great Lakes edition / Daily Brief' },
  };
  const normalize = value => Object.hasOwn(themes, value) ? value : 'original';
  let current = 'original';
  try { current = normalize(localStorage.getItem(key)); } catch { /* Session-only mode. */ }

  function apply(value) {
    current = normalize(value);
    document.documentElement.dataset.theme = current;
    document.querySelector('meta[name="theme-color"]').content = themes[current].color;
    const select = document.querySelector('#theme-select');
    if (!select) return;
    select.value = current;
    document.querySelector('.theme-signature').hidden = current === 'original';
    document.querySelector('#theme-kicker').textContent = themes[current].kicker || '';
    document.querySelector('#theme-caption').textContent = themes[current].caption || '';
  }
  apply(current);
  document.addEventListener('DOMContentLoaded', () => {
    apply(current);
    const status = document.querySelector('#theme-status');
    document.querySelector('#theme-select').addEventListener('change', event => {
      apply(event.target.value);
      let saved = true;
      try { localStorage.setItem(key, current); } catch { saved = false; }
      const label = event.target.selectedOptions[0].textContent;
      status.textContent = saved ? `${label} theme saved in this browser.` : `${label} theme applied for this visit. Browser storage is unavailable.`;
    });
    window.addEventListener('storage', event => {
      if (event.key === key || event.key === null) {
        apply(event.newValue);
        status.textContent = 'Theme updated from another tab.';
      }
    });
  });
})();
