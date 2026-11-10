"""Imprime pular=true se o horário de varredura mais recente (09h, 14h ou 20h BRT) já foi varrido."""
import datetime as dt
import json
import sys

agora = dt.datetime.now(dt.timezone.utc)
slots = [agora.replace(hour=h, minute=0, second=0, microsecond=0) - dt.timedelta(days=d)
         for d in (0, 1) for h in (12, 17, 23)]
slot = max(s for s in slots if s <= agora + dt.timedelta(minutes=10))
try:
    runs = json.load(open(sys.argv[1])).get("runs", [])
except (OSError, ValueError):
    runs = []
ultima = dt.datetime.fromisoformat(runs[-1]["em"].replace("Z", "+00:00")) if runs else None
feita = ultima is not None and ultima >= slot - dt.timedelta(minutes=15)
print(f"pular={'true' if feita else 'false'}")
