/* Load the original asset files in their original order. One request at a time also works
 * with the small listen backlog of unmodified python -m http.server. No bundle or dependency. */
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
    for (const url of JSON.parse(config.styles)) await asset('link', 'href', url);
    for (const url of JSON.parse(config.scripts)) await asset('script', 'src', url);
  })().catch((e) => { console.error(e); });
})();
