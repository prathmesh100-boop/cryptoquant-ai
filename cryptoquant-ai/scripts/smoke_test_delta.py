#!/usr/bin/env python
"""Verify the UNVERIFIED Delta assumptions against the real exchange. Run this locally (needs internet).

    python scripts/smoke_test_delta.py            # REST checks
    python scripts/smoke_test_delta.py --ws 90    # also listen to the candle WebSocket for 90 seconds

Writes nothing to disk, sends no credentials, places no orders. Paste the output back so the README can be updated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx

from app.config import DELTA_INDIA_PUBLIC_WS_URL, DELTA_INDIA_REST_URL
from app.data.frames import floor_time
from app.data.live import MalformedMessageError, build_subscribe_message, parse_candle_message

HEADERS = {"User-Agent": "cryptoquant-ai-smoke/0.1", "Accept": "application/json"}


def check(label: str, ok: bool | None, detail: str = "") -> None:
    mark = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    print(f"[{mark}] {label}" + (f": {detail}" if detail else ""))


def rest_checks(base: str) -> None:
    now = datetime.now(UTC)
    with httpx.Client(base_url=base, headers=HEADERS, timeout=15) as c:
        r = c.get("/v2/products/BTCUSD")
        check("BTCUSD product exists", r.status_code == 200 and r.json().get("success") is True, f"HTTP {r.status_code}")
        if r.status_code == 200:
            p = r.json()["result"]
            check("product facts (needed in Phase 5/7)", None,
                  f"id={p.get('id')} type={p.get('contract_type')} tick={p.get('tick_size')} "
                  f"contract_value={p.get('contract_value')} {p.get('contract_unit_currency')} "
                  f"settling={p.get('settling_asset', {}).get('symbol')} state={p.get('state')}")
        for tf, hrs in (("1h", 6), ("4h", 48), ("1d", 24 * 5)):
            end = int(floor_time(now, tf).timestamp())
            start = end - hrs * 3600
            r = c.get("/v2/history/candles", params={"resolution": tf, "symbol": "BTCUSD", "start": start, "end": end})
            body = r.json() if r.status_code == 200 else {}
            rows = body.get("result") or []
            check(f"{tf}: candles returned", bool(rows), f"HTTP {r.status_code}, {len(rows)} rows, "
                  f"X-RATE-LIMIT-* headers={[k for k in r.headers if k.lower().startswith('x-rate')]}")
            if not rows:
                continue
            times = sorted(int(x["time"]) for x in rows)
            step = {"1h": 3600, "4h": 14400, "1d": 86400}[tf]
            check(f"{tf}: all times on UTC epoch grid", all(t % step == 0 for t in times),
                  f"first={datetime.fromtimestamp(times[0], UTC)} last={datetime.fromtimestamp(times[-1], UTC)}")
            check(f"{tf}: request 'end' is INCLUSIVE (a bar opening exactly at end is returned)", None,
                  f"end={end} returned_max={times[-1]} -> {'inclusive' if times[-1] == end else 'exclusive'}")
            check(f"{tf}: no gaps in range", len({b - a for a, b in zip(times, times[1:])} - {step}) == 0)
        # open-time vs close-time convention: ask for a window containing the CURRENT hour
        cur = int(floor_time(now, "1h").timestamp())
        r = c.get("/v2/history/candles", params={"resolution": "1h", "symbol": "BTCUSD", "start": cur - 7200, "end": int(now.timestamp())})
        ts = sorted(int(x["time"]) for x in (r.json().get("result") or [])) if r.status_code == 200 else []
        if ts:
            check("'time' semantics", None,
                  f"newest bar time={datetime.fromtimestamp(ts[-1], UTC)}, now={now:%H:%M}: "
                  f"{'OPEN time of in-progress bar (expected)' if ts[-1] == cur else 'NOT the open time of the in-progress bar - REVIEW'}")


async def ws_check(url: str, seconds: int, rest_base: str | None) -> None:
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    from app.data.delta_rest import DeltaPublicClient
    from app.data.live import LiveCandleAssembler
    from app.data.quality import diff_candles

    types, accepted, rejected = Counter(), 0, Counter()
    samples: list[str] = []
    finalized = []
    closed_early: str | None = None
    assemblers: dict[tuple[str, str], LiveCandleAssembler] = {}
    async with connect(url, open_timeout=15) as ws:
        await ws.send(build_subscribe_message(["BTCUSD"], ["1m"]))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            except ConnectionClosed as exc:
                closed_early = f"server closed the connection early: {exc}"
                break
            text = raw if isinstance(raw, str) else raw.decode(errors="replace")
            try:
                types[json.loads(text).get("type", "?")] += 1
            except (ValueError, AttributeError):
                types["<non-json>"] += 1
            if len(samples) < 3:
                samples.append(text)
            try:
                c = parse_candle_message(text)
            except MalformedMessageError as exc:
                rejected[str(exc)[:120]] += 1
                continue
            if c is not None:
                accepted += 1
                a = assemblers.setdefault((c.symbol, c.timeframe), LiveCandleAssembler(c.symbol, c.timeframe))
                finalized.extend(a.on_update(c))
    check("WebSocket connected & subscribed", True, url)
    if closed_early:
        check("connection stayed open for the whole window", False, closed_early)
    check("message types seen", None, dict(types))
    check("candle messages accepted by our parser", accepted > 0 and not rejected, f"accepted={accepted} rejected={sum(rejected.values())}")
    for reason, n in rejected.most_common(3):
        print(f"   rejected x{n}: {reason}")
    for smp in samples:
        print("   sample:", smp[:300])
    check("bars finalized by assembler", bool(finalized) or None, f"{len(finalized)} (needs a window longer than 60-120 s)")
    if finalized and rest_base:
        rest = DeltaPublicClient(rest_base)
        try:
            for ws_c in finalized[:3]:
                ref = rest.fetch_candles(ws_c.symbol, ws_c.timeframe, ws_c.open_time, ws_c.open_time + timedelta(minutes=1))
                if not ref:
                    check(f"REST has WS bar {ws_c.open_time:%H:%M}", False, "REST returned nothing for that bar")
                    continue
                d = diff_candles(ws_c, ref[0])
                check(f"WS bar {ws_c.open_time:%H:%M} equals REST", not d,
                      "identical" if not d else f"DIFFERS (ws, rest): {d}")
        finally:
            rest.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DELTA_INDIA_REST_URL)
    ap.add_argument("--ws", type=int, metavar="SECONDS", help="also listen to the candle WebSocket")
    ap.add_argument("--ws-url", default=DELTA_INDIA_PUBLIC_WS_URL)
    ap.add_argument("--skip-rest", action="store_true", help="skip REST checks (used for offline testing)")
    a = ap.parse_args()
    try:
        if not a.skip_rest:
            rest_checks(a.base_url)
        if a.ws:
            asyncio.run(ws_check(a.ws_url, a.ws, None if a.skip_rest else a.base_url))
    except (httpx.HTTPError, OSError) as exc:
        print(f"NETWORK ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
