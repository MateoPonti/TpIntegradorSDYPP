"""Validación opcional en Chromium: python pilar2/tests/browser_notifier.py.

Requiere playwright y su Chromium (python -m playwright install chromium).
Usa APIs y WebSocket simulados, pero ejecuta las páginas completas y sus temporizadores en el navegador.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "services/coordinator/static"
MOCK_WS = """
window.sockets = []; window.online = true;
window.WebSocket = class {
  constructor() {
    this.sent = []; this.subs = new Map(); sockets.push(this);
    setTimeout(() => online ? this.onopen?.() : this.onclose?.(), 0);
  }
  send(raw) {
    const m = JSON.parse(raw); this.sent.push(m);
    if (m.jsonrpc !== '2.0') throw new Error('protocolo viejo');
    if (m.method !== 'sdmer_subscribe') return;
    const sid = '0x' + m.id.toString(16).padStart(16, '0');
    this.subs.set(sid, m.params);
    queueMicrotask(() => {
      this.onmessage({data: JSON.stringify({jsonrpc:'2.0', id:m.id, result:sid})});
      const snap = m.params[0] === 'newBlocks' ? {height:1, tip:'0'} :
        {height:1, account:m.params[1], balance_cents:100, pending_out_cents:0, available_cents:100};
      this.notice(sid, {type:'snapshot', ...snap});
    });
  }
  notice(sid, result) {
    this.onmessage({data:JSON.stringify({jsonrpc:'2.0', method:'sdmer_subscription',
      params:{subscription:sid, result}})});
  }
  block() {
    for (const [sid, params] of this.subs) if (params[0] === 'newBlocks')
      this.notice(sid, {type:'event', event:'block.solved', index:1});
  }
  close() { this.onclose?.(); }
};
"""


def main():
    with sync_playwright() as pw:
        executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        browser = pw.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
        for name in ("index.html", "cuenta.html"):
            page = browser.new_page()
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(MOCK_WS)
            page.clock.install()

            def serve(route):
                path = urlparse(route.request.url).path
                local = STATIC / (name if path == "/" else path.removeprefix("/"))
                if local.is_file():
                    route.fulfill(path=str(local))
                    return
                requests.append(path)
                data = {"/status": {"chain_height": 1, "pending_transactions": 0,
                                     "miners": {"cpu": 0, "gpu": 0}},
                        "/blocks": {"blocks": []}, "/miners": {"miners": []},
                        "/accounts": {"accounts": []}, "/crowdfunds": {"crowdfunds": []},
                        "/multisig": {"accounts": [], "invites": []},
                        "/unirse/config": {"notifier": "ws://test/ws"}, "/chain-id": {"chain_id": 2026},
                        "/balances/ana": {"balance_cents": 100, "pending_out_cents": 0, "available_cents": 100}}
                route.fulfill(json=data.get(path, {}))

            page.route("**/*", serve)
            page.goto("http://test/")
            page.clock.run_for(500)
            page.wait_for_function("wsOpen")
            if name == "cuenta.html":
                page.evaluate("abrir('ana', null)")
            page.wait_for_timeout(300)
            page.wait_for_load_state("networkidle")
            before = len(requests)
            page.clock.run_for(60000)
            page.wait_for_timeout(300)
            page.wait_for_load_state("networkidle")
            assert len(requests) == before, (name, "polling con WS conectado", requests[before:])
            with page.expect_response(lambda response: urlparse(response.url).path in ("/status", "/crowdfunds")):
                page.evaluate("sockets.at(-1).block()")
            page.wait_for_timeout(300)
            page.wait_for_load_state("networkidle")
            assert len(requests) > before, (name, "no se actualizó con newBlocks")
            page.evaluate("online = false; sockets.at(-1).close()")
            before = len(requests)
            with page.expect_response(lambda response: urlparse(response.url).path in ("/status", "/crowdfunds")):
                page.clock.run_for(16000)
            page.wait_for_timeout(300)
            page.wait_for_load_state("networkidle")
            assert len(requests) > before, (name, "fallback no actualizó")
            page.evaluate("online = true")
            page.clock.run_for(32000)
            page.wait_for_function("wsOpen")
            page.wait_for_timeout(300)
            page.wait_for_load_state("networkidle")
            before = len(requests)
            page.clock.run_for(60000)
            page.wait_for_timeout(300)
            page.wait_for_load_state("networkidle")
            assert len(requests) == before, (name, "fallback quedó activo tras reconectar", requests[before:])
            assert not errors, (name, errors)
            print(json.dumps({"page": name, "connected_60s": "sin polling", "newBlocks": "actualiza",
                              "fallback_15s": "actualiza", "reconnected_60s": "sin polling"}))
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
