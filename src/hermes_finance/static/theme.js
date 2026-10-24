/* Hermes Finances: the theme, applied before the first paint (TASK-18, Settings > Appearance).
 *
 * The saved choice (dark / light / system) comes from the server (<html data-theme-pref>, the settings table), else
 * from this device's last one (localStorage, e.g. on the sign-in page), else dark. "system" follows the device and
 * changes with it. The page's other scripts read window.HFTheme and listen for the "hf-theme" event (charts redraw
 * their ink). Loaded synchronously in <head>, so there's no flash of the other theme.
 */
(() => {
  'use strict';
  const KEY = 'hf.theme';
  const root = document.documentElement;
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
  const ok = (v) => v === 'dark' || v === 'light' || v === 'system';
  let stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch (e) { /* storage off: the server's value or dark */ }
  let pref = ok(root.dataset.themePref) ? root.dataset.themePref : ok(stored) ? stored : 'dark';

  function resolved() {
    return pref === 'system' ? (media && media.matches ? 'light' : 'dark') : pref;
  }

  function apply() {
    const t = resolved();
    root.dataset.theme = t;
    root.style.colorScheme = t;
    const scheme = document.querySelector('meta[name=color-scheme]');
    if (scheme) scheme.content = t;
    const bar = document.querySelector('meta[name=theme-color]');
    if (bar) bar.content = t === 'light' ? '#f3f4f7' : '#0c0e13';
    try {
      localStorage.setItem(KEY, pref);
    } catch (e) { /* not remembered on this device */ }
    document.dispatchEvent(new CustomEvent('hf-theme', {detail: {pref, theme: t}}));
  }

  if (media && media.addEventListener) media.addEventListener('change', () => { if (pref === 'system') apply(); });
  window.HFTheme = {
    get pref() { return pref; },
    get theme() { return resolved(); },
    set(next) {
      if (!ok(next)) return;
      pref = next;
      root.dataset.themePref = next;
      apply();
    },
  };
  apply();
})();
