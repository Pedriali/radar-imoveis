"""Cancela pedidos de varredura presos esperando o Mac (runner radar-mac desligado ou dormindo).

Um pedido do botão VARRER AGORA com o Mac fora do ar fica na fila esperando o runner e segura o grupo
de concorrência "varredura": a reserva da nuvem ficaria parada atrás dele. Roda no job "trava" da reserva
(cron), antes de ela varrer. Usa o GITHUB_TOKEN do workflow (permissão actions: write).
"""
import datetime as dt
import json
import os
import urllib.request

REPO = os.environ["REPO"]
TOKEN = os.environ["GH_TOKEN"]
ESPERA_MAX = dt.timedelta(minutes=20)


def api(path, method="GET"):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/{path}", method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else {}


agora = dt.datetime.now(dt.timezone.utc)
runs = api("actions/workflows/radar.yml/runs?status=queued&per_page=30").get("workflow_runs", [])
for run in runs:
    criado = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    if agora - criado < ESPERA_MAX:
        continue
    jobs = api(f"actions/runs/{run['id']}/jobs").get("jobs", [])
    preso = any(j["name"] == "varredura" and j["status"] == "queued" and not j.get("runner_name")
                and "radar-mac" in (j.get("labels") or []) for j in jobs)
    if preso:
        print(f"cancelando run {run['id']} (esperando o Mac desde {run['created_at']})")
        api(f"actions/runs/{run['id']}/cancel", method="POST")
