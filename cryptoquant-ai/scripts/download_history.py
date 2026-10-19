#!/usr/bin/env python
"""Download historical candles into the local SQLite store (public data, no credentials needed).

Examples:
    python scripts/download_history.py --days 30
    python scripts/download_history.py --symbols BTCUSD --timeframes 1h 4h --days 90 --db sqlite:///data/cq.db
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.data.delta_rest import DeltaPublicClient
from app.data.downloader import HistoryDownloader
from app.data.source import DataSourceError
from app.monitoring.logging_setup import configure_logging
from app.storage.candle_store import SqliteCandleStore


def main() -> int:
    settings = Settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=settings.symbols)
    ap.add_argument("--timeframes", nargs="+", default=settings.timeframes)
    ap.add_argument("--days", type=int, default=30, help="history length to ensure is stored (default 30)")
    ap.add_argument("--db", default=settings.database_url)
    args = ap.parse_args()

    configure_logging(settings.log_level, json_output=False)
    store = SqliteCandleStore.from_url(args.db)
    client = DeltaPublicClient(settings.exchange.rest_url, timeout_s=settings.exchange.request_timeout_s)
    downloader = HistoryDownloader(client, store)
    start = datetime.now(UTC) - timedelta(days=args.days)
    exit_code = 0
    try:
        for symbol in args.symbols:
            for tf in args.timeframes:
                rep = downloader.sync(symbol, tf, start)
                status = "contiguous" if rep.is_contiguous else f"{len(rep.remaining_gaps)} GAP(S)"
                print(f"{symbol:8} {tf:4} fetched={rep.fetched:6} stored={rep.stored_total:6}  {status}")
                for g in rep.remaining_gaps[:5]:
                    print(f"    gap: {g.first_missing.isoformat()} .. {g.last_missing.isoformat()} ({g.missing_bars} bars)")
                exit_code |= 0 if rep.is_contiguous else 1
    except DataSourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()
        store.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
