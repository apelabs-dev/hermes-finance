#!/usr/bin/env python3
"""One-off (t_d31fda2a): pull ONE real valuation for each demo car and landmark and freeze it in demo_assets.json.

    uv run python scripts/demo_valuations_pull.py [--env ~/.hermes/.env] [--dry-run]

The demo build never calls an API: it reads the frozen values from scripts/demo_assets.json. This script is the only
thing that ever asked, once, by hand.

- Cars: MarketCheck's active used listings for the year / make / model / trim near the car's ZIP (`stats=price`), the
  median asking price. The demo cars have no VIN, and MarketCheck's price-predict call needs one.
- Landmarks: RentCast's AVM (`/v1/avm/value?address=`). If RentCast has no estimate for a landmark, the next
  `spare_properties` entry takes its place (a car without listings likewise takes the next `spare_vehicles` entry).
- Keys: MARKETCHECK_API_KEY / RENTCAST_API_KEY are read from the env file inside this process only. Nothing prints,
  logs or saves a key: output is item + value + source + date, and an error shows only its HTTP status / kind.
- Quota: at most 10 MarketCheck and 12 RentCast calls in all, counted in the JSON before each call is sent. An item
  that already has a valuation is never asked again, and each success is saved at once, so a re-run never re-calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ASSETS = ROOT / "scripts" / "demo_assets.json"
CAPS = {"marketcheck": 10, "rentcast": 12}
KEYS = {"marketcheck": "MARKETCHECK_API_KEY", "rentcast": "RENTCAST_API_KEY"}
MARKETCHECK_SEARCH = "https://api.marketcheck.com/v2/search/car/active"
RENTCAST_AVM = "https://api.rentcast.io/v1/avm/value"
RADIUS_MILES = 50


class Stop(Exception):
    pass


def load_keys(env_file: Path) -> dict[str, str]:
    """Only the two keys this script needs, from KEY=VALUE lines (quotes and `export ` allowed). Never printed."""
    keys = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if sep and name.strip() in KEYS.values():
            keys[name.strip()] = value.strip().strip("'\"")
    return keys


def save(data: dict) -> None:
    ASSETS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def call(data: dict, provider: str, url: str, headers: dict):
    """One counted GET. Returns (status, parsed JSON or None); the count is saved before the request goes out."""
    from hermes_finance import valuation
    if data["calls"][provider] >= CAPS[provider]:
        raise Stop(f"{provider}: the run's cap of {CAPS[provider]} calls is used up")
    data["calls"][provider] += 1
    save(data)
    try:
        status, body = valuation.urllib_transport(url, headers, 20.0)
    except OSError as e:   # TimeoutError included; the message is only a kind, never the URL
        return f"network {type(e).__name__}", None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def car_value(data: dict, car: dict, key: str):
    q = {"api_key": key, "year": car["year"], "make": car["make"], "model": car["model"], "trim": car["trim"],
         "zip": car["zip"], "radius": RADIUS_MILES, "car_type": "used", "stats": "price", "rows": 0}
    status, js = call(data, "marketcheck", f"{MARKETCHECK_SEARCH}?{urlencode(q)}", {})
    if isinstance(status, str):
        return None, status
    if status != 200 or not isinstance(js, dict):
        return None, f"HTTP {status}"
    price = ((js.get("stats") or {}).get("price") or {})
    n = js.get("num_found") or price.get("count") or 0
    median = price.get("median")
    if not median or not n:
        return None, "no listings"
    return {"value": round(float(median)), "source": "MarketCheck active used listings (median asking price)",
            "listings": int(n), "radius_miles": RADIUS_MILES, "zip": car["zip"],
            "mean": round(float(price["mean"])) if price.get("mean") else None,
            "date": date.today().isoformat()}, None


def home_value(data: dict, place: dict, key: str):
    status, js = call(data, "rentcast", f"{RENTCAST_AVM}?{urlencode({'address': place['address']})}",
                      {"X-Api-Key": key})
    if isinstance(status, str):
        return None, status
    if status == 404:   # RentCast's answer when it has no data for an address
        return None, "no estimate (HTTP 404)"
    if status != 200 or not isinstance(js, dict):
        return None, f"HTTP {status}"
    if not js.get("price"):
        return None, "no estimate"
    return {"value": round(float(js["price"])), "source": "RentCast AVM (/v1/avm/value)",
            "low": js.get("priceRangeLow"), "high": js.get("priceRangeHigh"),
            "date": date.today().isoformat()}, None


def label(kind: str, x: dict) -> str:
    return f"{x['year']} {x['make']} {x['model']} {x['trim']}" if kind == "vehicles" else x["name"]


def pull(data: dict, kind: str, spares: str, fetch, key: str) -> None:
    for i, item in enumerate(data[kind]):
        while item.get("valuation") is None:
            got, why = fetch(data, item, key)
            if got:
                item["valuation"] = got
                save(data)
                print(f"{label(kind, item)}: ${got['value']:,} ({got['source']}, {got['date']})")
                break
            data.setdefault("tried", []).append({"item": label(kind, item), "why": why, "date": date.today().isoformat()})
            print(f"{label(kind, item)}: no value ({why})")
            if not why.startswith("no "):   # only "no listings" / "no estimate" moves on to a spare
                save(data)
                raise Stop(f"{kind}: {why}; stopping (no numbers are invented)")
            if not data.get(spares):
                save(data)
                raise Stop(f"{kind}: no spare left after {label(kind, item)}")
            nxt = data[spares].pop(0)
            item = data[kind][i] = {**nxt, "valuation": None}
            save(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", type=Path, default=Path.home() / ".hermes" / ".env")
    ap.add_argument("--dry-run", action="store_true", help="say what would be asked, ask nothing")
    a = ap.parse_args(argv)
    data = json.loads(ASSETS.read_text())
    todo = [label(k, x) for k in ("vehicles", "properties") for x in data[k] if x.get("valuation") is None]
    if a.dry_run or not todo:
        print("to pull:", todo or "nothing", "| calls so far:", data["calls"])
        return 0
    keys = load_keys(a.env)
    try:
        for kind, spares, fetch, provider in (("vehicles", "spare_vehicles", car_value, "marketcheck"),
                                              ("properties", "spare_properties", home_value, "rentcast")):
            if any(x.get("valuation") is None for x in data[kind]):
                if not keys.get(KEYS[provider]):
                    raise Stop(f"{KEYS[provider]} is not in the env file")
                pull(data, kind, spares, fetch, keys[KEYS[provider]])
    except Stop as e:
        print(f"STOPPED: {e} | calls: {data['calls']}")
        return 1
    print(f"done | calls: {data['calls']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
