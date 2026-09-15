from __future__ import annotations

import json
import secrets
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import CampaignConfig
from .paths import PathSafetyError
from .recovery import set_drain
from .status import campaign_status, task_detail

HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RASPA3 Campaign Manager</title>
<style>
:root{font-family:Inter,system-ui,sans-serif;color:#1f2937;background:#f5f7fa}body{margin:0}.top{background:#123b73;color:#fff;padding:14px 20px}.wrap{padding:18px;max-width:1500px;margin:auto}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.card,.panel{background:#fff;border:1px solid #d8dee8;border-radius:5px;padding:12px}.n{font-size:28px;font-weight:750;color:#123b73}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}input,select,button{font:inherit;padding:7px 9px;border:1px solid #b8c2d1;border-radius:4px}button{background:#123b73;color:#fff;border:0;cursor:pointer}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid #e5e7eb;text-align:left}th{position:sticky;top:0;background:#eef2f7}.complete{color:#087f5b;font-weight:700}.failed,.contract_failed,.timeout{color:#c92a2a;font-weight:700}.running{color:#e67700;font-weight:700}.scroll{max-height:60vh;overflow:auto}pre{white-space:pre-wrap;font-size:12px}.muted{color:#667085;font-size:12px}
</style></head><body><div class="top"><b>RASPA3 Campaign Manager</b> <span id="name"></span></div><div class="wrap">
<div class="cards" id="cards"></div><div class="toolbar"><select id="state"><option value="">all states</option></select><input id="gas" placeholder="gas"><input id="mof" placeholder="MOF contains"><button onclick="load()">Refresh</button></div>
<div class="panel"><div class="scroll"><table><thead><tr><th>MOF</th><th>Gas</th><th>T/K</th><th>P/bar</th><th>Seed</th><th>State</th><th>Attempt</th><th>Est. min</th></tr></thead><tbody id="rows"></tbody></table></div></div>
<div class="panel" style="margin-top:10px"><b>Task detail</b><pre id="detail" class="muted">Click a task row.</pre></div></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
async function load(){let q=new URLSearchParams();for(let id of ['state','gas','mof']){let v=document.getElementById(id).value;if(v)q.set(id,v)}let d=await fetch('/api/summary?'+q).then(r=>r.json());document.getElementById('name').textContent=' — '+d.campaign;let cards=[['Tasks',d.total],...Object.entries(d.counts)];document.getElementById('cards').innerHTML=cards.map(x=>`<div class=card><div class=n>${esc(x[1])}</div><div>${esc(x[0])}</div></div>`).join('');let states=Object.keys(d.counts);let sel=document.getElementById('state');let cur=sel.value;sel.innerHTML='<option value="">all states</option>'+states.map(s=>`<option ${s===cur?'selected':''}>${esc(s)}</option>`).join('');document.getElementById('rows').innerHTML=d.tasks.map(t=>`<tr onclick="detail('${esc(t.task_id)}')"><td>${esc(t.mof_id)}</td><td>${esc(t.gas)}</td><td>${esc(t.temperature_K)}</td><td>${esc(t.pressure_bar)}</td><td>${esc(t.seed)}</td><td class=${esc((t.status_state||'').toLowerCase())}>${esc(t.status_state)}</td><td>${esc(t.status_attempt_no)}</td><td>${((t.estimated_seconds||0)/60).toFixed(1)}</td></tr>`).join('')}
async function detail(id){let d=await fetch('/api/task/'+encodeURIComponent(id)).then(r=>r.json());document.getElementById('detail').textContent=JSON.stringify(d,null,2)}load();setInterval(load,15000);
</script></body></html>'''


def make_handler(config: CampaignConfig, *, allow_actions: bool = False, action_token: str | None = None):
    """Request-local validation; exposed for socket-free HTTP handler tests."""
    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: Any, status: int = 200):
            data=json.dumps(value,ensure_ascii=False,sort_keys=True).encode()
            self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            parsed=urllib.parse.urlparse(self.path)
            if parsed.path=="/":
                data=HTML.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
            try:
                if parsed.path=="/api/summary":
                    q=urllib.parse.parse_qs(parsed.query); states={q["state"][0]} if q.get("state") else None
                    self._json(campaign_status(config,states=states,gas=q.get("gas",[None])[0],mof_contains=q.get("mof",[None])[0])); return
                if parsed.path.startswith("/api/task/"):
                    task_id=urllib.parse.unquote(parsed.path.split("/api/task/",1)[1])
                    self._json(task_detail(config,task_id)); return
            except PathSafetyError:
                self._json({"error":"Invalid task path or task ID"},400); return
            except FileNotFoundError:
                self._json({"error":"Task/evidence not found"},404); return
            except (ValueError, TypeError, KeyError):
                self._json({"error":"Invalid task data; inspect local evidence"},500); return
            except OSError:
                self._json({"error":"Task read failed; inspect local permissions"},500); return
            self._json({"error":"not found"},404)
        def do_POST(self):
            if not allow_actions:
                self._json({"error":"dashboard is read-only"},403); return
            if self.headers.get("X-RASPA-Token")!=action_token:
                self._json({"error":"invalid action token"},403); return
            if self.path=="/api/drain": self._json(set_drain(config,True,"dashboard action")); return
            if self.path=="/api/resume": self._json(set_drain(config,False,"dashboard action")); return
            self._json({"error":"not found"},404)
        def log_message(self,fmt,*args): return
    return Handler


def serve_dashboard(config: CampaignConfig, host: str = "127.0.0.1", port: int = 8765, *, allow_actions: bool = False, token: str | None = None) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Public dashboard binding is not supported; use localhost with an SSH tunnel")
    if allow_actions:
        raise ValueError("P03 dashboard is read-only; action authorization is pending the execution/recovery repair")
    action_token = token or secrets.token_urlsafe(24)
    Handler = make_handler(config, allow_actions=False, action_token=action_token)
    server=ThreadingHTTPServer((host,port),Handler)
    print(f"Dashboard: http://{host}:{port}")
    if allow_actions: print(f"Action token: {action_token}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
