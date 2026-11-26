/* Load the original script files in their original order. One request at a time also works
 * with the small listen backlog of unmodified python -m http.server. No bundle or dependency.
 * The stylesheets are plain <link>s in <head> (the first paint waits for them), and while the scripts load the empty
 * page shows the App's own skeleton cards, which app.js replaces when it draws the first page. */
(() => {
  'use strict';
  const config = document.currentScript.dataset;
  async function asset(tag, attr, url) {
    await new Promise((resolve, reject) => {
      const el = document.createElement(tag);
      if (tag === 'link') el.rel = 'stylesheet';
      el[attr] = url;
      el.onload = resolve;
      el.onerror = () => reject(new Error(`Demo asset failed: ${url}`));
      document.head.append(el);
    });
  }
  (async () => {
    const page = document.getElementById('page');
    if (page && !page.children.length) {
      for (let i = 0; i < 2; i++) { const c = document.createElement('span'); c.className = 'skel sk-card'; page.append(c); }
    }
    for (const url of JSON.parse(config.scripts)) await asset('script', 'src', url);
  })().catch((e) => { console.error(e); });
})();
