"""Ask follows saved Settings without reloading; only synthetic data and stubbed chat replies."""
import json

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import db, prefs

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")


@pytest.mark.parametrize("width", [1440, 390])
def test_saved_ai_modes_activate_one_ask_panel_without_reload(tmp_path, data_dir, width):
    path = tmp_path / "finance.db"
    gate_dbs.build("full", path)
    with db.connect(path) as conn:
        prefs.save(conn, {"ai.mode": "off", "ai.model": "test/first"})
    site = Server(path)
    try:
        with Chrome() as b:
            b.size(width, 900)
            b.goto(site.url + "/#/settings")
            assert b.settled(cap_ms=5000) == ''
            doc = b.js("window.__pend.doc")
            assert b.js("document.querySelectorAll('#askb, #chat').length") == 0
            # Leave Settings saves real. Stub only chat: no model or bridge can be called.
            b.js("""(() => {
              window.chatCalls = []; const original = window.fetch;
              window.holdPrefs = true;
              window.prefsHeld = new Promise(resolve => { window.onPrefsHeld = resolve; });
              window.fetch = (url, opts) => {
                if (String(url) === '/api/prefs' && window.holdPrefs) {
                  window.holdPrefs = false;
                  return original(url, opts).then(response => new Promise(resolve => {
                    window.releasePrefs = () => resolve(response);
                    window.onPrefsHeld();
                  }));
                }
                if (String(url).startsWith('/api/chat')) {
                  chatCalls.push([String(url), opts && opts.method || 'GET']);
                  if (opts && opts.method === 'POST') return Promise.resolve(new Response(JSON.stringify({
                    reply: 'Synthetic reply', cards: [{id: 7, status: 'approved', title: 'Synthetic change', summary: 'Test'}]
                  }), {status: 200}));
                  if (String(url).startsWith('/api/chat/cards')) return Promise.resolve(new Response(JSON.stringify({
                    cards: [{id: 7, status: 'approved', title: 'Synthetic change', summary: 'Test'}]
                  }), {status: 200}));
                }
                return original(url, opts);
              }; return true;
            })()""")

            def start_save(action):
                # Register before the action: successful save events are authoritative; clicks are not.
                b.js("""window.savedPrefs = new Promise((resolve, reject) => {
                  const timer = setTimeout(() => {
                    document.removeEventListener('hf-prefs', done);
                    reject(new Error('Settings save did not succeed'));
                  }, 5000);
                  function done(e) {
                    clearTimeout(timer);
                    resolve({prefs: e.detail, ask: document.body.dataset.ask || 'off',
                      buttons: document.querySelectorAll('#askb').length,
                      panels: document.querySelectorAll('#chat').length});
                  }
                  document.addEventListener('hf-prefs', done, {once: true});
                });""" + action)

            def saved(values):
                result = b.js('window.savedPrefs')
                with db.connect_readonly(path) as conn:
                    for key, value in values.items():
                        assert prefs.get(conn, key) == value
                        group, field = key.split('.')
                        assert result['prefs'][group][field] == value
                # Ask activation/removal happens synchronously at save success, before metadata completes.
                current = result['prefs']['ai']['mode']
                assert result['ask'] == current
                assert result['buttons'] == result['panels'] == (0 if current == 'off' else 1)
                assert b.settled(cap_ms=5000) == ''

            def mode(label, held=False):
                start_save("[...document.querySelectorAll('[aria-label=\"AI provider\"] button')].find(x => x.textContent === "
                           + json.dumps(label) + ").click()")
                if held:
                    # Hold a real successful POST response, without a timing-dependent delay. The DB has
                    # changed, but neither the segment nor Ask may claim success before the response arrives.
                    b.js('Promise.race([window.prefsHeld, window.savedPrefs])')
                    b.settle()  # the old helper returns even though save() has not received its response
                    with db.connect_readonly(path) as conn:
                        assert prefs.get(conn, 'ai.mode') == 'api'
                    assert b.js("document.querySelectorAll('#askb, #chat').length") == 0
                    assert b.js("document.querySelector('[aria-label=\"AI provider\"] [aria-pressed=true]').textContent") == 'Off'
                    b.js('window.releasePrefs()')
                saved({'ai.mode': {'API key': 'api', 'Hermes': 'hermes', 'Off': 'off'}[label]})
                assert b.js("document.querySelector('[aria-label=\"AI provider\"] [aria-pressed=true]').textContent") == label
                assert b.js("document.querySelectorAll('#askb').length <= 1 && document.querySelectorAll('#chat').length <= 1")

            def opened(brain):
                assert b.js("document.querySelectorAll('#askb').length") == 1
                assert b.js("document.querySelectorAll('#chat').length") == 1
                b.js("document.querySelector('#askb').click()")
                assert b.settled(cap_ms=5000) == ''
                assert b.js("!document.querySelector('#chat').hidden && document.body.classList.contains('chaton')")
                assert b.js("document.querySelector('#chat .brain').textContent") == brain

            for i, (label, brain) in enumerate([("API key", "via OpenRouter · first"), ("Hermes", "via Hermes"),
                                                ("API key", "via OpenRouter · first")]):
                mode(label, held=i == 0)
                opened(brain)
                if label == "API key":
                    b.js("document.querySelector('#chat textarea').value = 'Synthetic question'; document.querySelector('#chsend').click()")
                    b.js("__appear(() => !document.querySelector('#chsend').disabled, 5000, 'chat reply')")
                    assert b.js("chatCalls.filter(x => x[1] === 'POST').length") == 1
                mode("Off")
                assert b.js("[...document.querySelectorAll('#askb, #chat')].every(x => x.hidden)")
                assert not b.js("document.body.classList.contains('chaton')")
                count = b.js("chatCalls.length")
                b.js("new Promise(r => setTimeout(r, 1200))")
                assert b.js("chatCalls.length") == count  # approved-card polling stopped
                b.js("chatCalls.length = 0")
            mode("API key")
            opened("via OpenRouter · first")
            b.js("document.querySelector('#chat [aria-label=Close]').click()")
            start_save("document.querySelector('[aria-label=Model]').value = 'test/second'; "
                       "document.querySelector('[aria-label=Model]').dispatchEvent(new Event('change'))")
            saved({'ai.model': 'test/second'})
            start_save("document.querySelector('[aria-label=\"Provider preset\"]').value = 'openai'; "
                       "document.querySelector('[aria-label=\"Provider preset\"]').dispatchEvent(new Event('change'))")
            saved({'ai.preset': 'openai'})
            opened("via OpenAI · second")
            assert b.js("window.__pend.doc") == doc
            assert b.problems() == []
    finally:
        site.close()
