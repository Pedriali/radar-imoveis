#!/bin/bash
# Disparo das 09h, 14h e 20h (chamado pelo com.pedriali.radar-imoveis.plist).
# 1. garante o runner do Mac rodando (o serviço pode ter caído depois de o Mac dormir);
# 2. espera ele aparecer online no GitHub (até ~90 s);
# 3. online: pede a varredura no Mac; fora do ar: pede na nuvem (Zap/OLX podem falhar, mas não fica sem varredura).
GH=/Users/otaviopedriali/.local/bin/gh
REPO=Pedriali/radar-imoveis
SERVICO=actions.runner.Pedriali-radar-imoveis.mac-radar

echo "== $(date '+%Y-%m-%d %H:%M:%S') disparo"
launchctl list "$SERVICO" >/dev/null 2>&1 || launchctl load "$HOME/Library/LaunchAgents/$SERVICO.plist"
if ! launchctl list "$SERVICO" 2>/dev/null | grep -q '"PID"'; then
  echo "runner parado: religando"
  launchctl kickstart "gui/$(id -u)/$SERVICO"
fi

status=offline
for _ in $(seq 1 18); do
  status=$("$GH" api "repos/$REPO/actions/runners" -q '.runners[] | select(.name=="mac-radar") | .status' 2>/dev/null)
  [ "$status" = online ] && break
  sleep 5
done

if [ "$status" = online ]; then
  echo "runner online: varredura no Mac"
  "$GH" workflow run radar.yml -R "$REPO" -f agendada=true
else
  echo "runner fora do ar ($status): varredura na nuvem"
  "$GH" workflow run radar.yml -R "$REPO" -f agendada=true -f nuvem=true
fi
