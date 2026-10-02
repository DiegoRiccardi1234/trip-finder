"""Sei prove della conversazione attraverso un server realmente in ascolto.

Non avvia server, non legge chiavi e non usa TestClient. Distanza le richieste
per non bruciare la quota gratuita. Il report contiene solo richieste di prova.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

import _bootstrap  # noqa: F401
import httpx


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8099")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pause", type=float, default=15)
    args = parser.parse_args()
    target = date.today() + timedelta(days=14)
    target += timedelta(days=(4 - target.weekday()) % 7)
    written = target.strftime("%d/%m/%Y")
    cases = [
        ("completa", f"da Torino a Matera il {written}", [], "piano"),
        ("arrivo", f"devo essere a Matera il {written} entro sera, parto da Torino", [], "arrivo"),
        ("data_mancante", "da Torino a Matera", [], "data"),
        ("secondo_turno", f"il {written}", [
            {"role": "user", "content": "da Torino a Matera"},
            {"role": "assistant", "content": "Per quando?"},
        ], "piano"),
        ("origine_mancante", f"voglio andare a Matera il {written}", [], "origine"),
        ("tappe", f"da Torino a Roma il {written}, resto tre giorni, poi vado a Matera", [], "tappe"),
    ]
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "target_date": target.isoformat(), "cases": []}
    async with httpx.AsyncClient(timeout=300) as http:
        for index, (name, text, history, expected) in enumerate(cases):
            if index:
                await asyncio.sleep(max(0, args.pause))
            start = monotonic()
            try:
                response = await http.post(args.url.rstrip("/") + "/api/parse", json={"text": text, "messages": history})
                payload = response.json()
                stages = payload.get("stages") or []
                ok = response.status_code == 200
                if expected == "data":
                    ok = ok and payload.get("domanda") == "Per quando?"
                elif expected == "origine":
                    ok = ok and payload.get("domanda") == "Da dove parti e dove vai?"
                else:
                    ok = ok and bool(stages) and stages[0].get("origin", "").lower() == "torino" and stages[0].get("date") == target.isoformat()
                    if expected == "tappe":
                        ok = ok and len(stages) == 2 and stages[0].get("destination", "").lower() == "roma" and stages[0].get("stay_days") == 3 and stages[1].get("origin", "").lower() == "roma" and stages[1].get("destination", "").lower() == "matera" and stages[1].get("date") == (target + timedelta(days=3)).isoformat()
                    else:
                        ok = ok and stages[0].get("destination", "").lower() == "matera"
                    if expected == "arrivo":
                        ok = ok and stages[0].get("arrive_by") == "21:00:00"
                record = {"name": name, "text": text, "status": response.status_code, "ok": bool(ok), "response": payload}
            except (httpx.HTTPError, ValueError) as exc:
                record = {"name": name, "ok": False, "error": type(exc).__name__}
            record["elapsed_seconds"] = round(monotonic() - start, 2)
            report["cases"].append(record)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{name}: {'OK' if record['ok'] else 'FAIL'} ({record['elapsed_seconds']}s)", flush=True)
    return 0 if all(c["ok"] for c in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
