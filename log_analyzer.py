"""주식·코인 관찰 CSV/로그 로컬 분석기.

예:
  python3 log_analyzer.py --stock-dir /home/trade/upbit_bot --coin-dir /home/trade/upbit6974
  python3 log_analyzer.py --coin-dir /home/trade/upbit6974 --hours 24
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def num(value: str) -> float:
    try:
        return float(str(value).replace(",", "").replace("%", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def analyze_csv(path: Path, since: datetime | None) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(row.get("timestamp", "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                if since and ts.replace(tzinfo=None) < since:
                    continue
                key = row.get("market") or row.get("code") or row.get("name") or "unknown"
                row["_ts"] = ts.isoformat()
                grouped[key].append(row)
    except (OSError, UnicodeDecodeError):
        return []
    result = []
    for key, rows in grouped.items():
        rows.sort(key=lambda x: x["_ts"])
        first, last = rows[0], rows[-1]
        start, end = num(first.get("price")), num(last.get("price"))
        changes = [num(r.get("change_24h_pct")) for r in rows if r.get("change_24h_pct")]
        values = [num(r.get("value_24h_krw") or r.get("value_krw")) for r in rows]
        result.append({"market_or_code": key, "name": last.get("name", key), "samples": len(rows),
                       "first_timestamp": first["_ts"], "last_timestamp": last["_ts"],
                       "first_price": start, "last_price": end,
                       "period_change_pct": round((end / start - 1) * 100, 4) if start else 0,
                       "min_price": min(num(r.get("price")) for r in rows),
                       "max_price": max(num(r.get("price")) for r in rows),
                       "range_pct": round((max(num(r.get("price")) for r in rows) / min(num(r.get("price")) for r in rows) - 1) * 100, 4) if min(num(r.get("price")) for r in rows) else 0,
                       "last_24h_change_pct": changes[-1] if changes else 0,
                       "avg_24h_value_krw": round(sum(values) / len(values), 2) if values else 0,
                       "trend": "상승" if end > start else "하락" if end < start else "보합"})
    return result


def find_csvs(root: Path, kind: str) -> list[Path]:
    if kind == "coin":
        return sorted((root / "data" / "upbit_observer").glob("snapshots_*.csv"))
    return sorted((root / "data" / "watchlist_observer").glob("snapshots_*.csv"))


def count_errors(root: Path) -> int:
    logs = list((root / "logs").glob("*.log"))
    return sum(len(re.findall(r"ERROR|오류|Traceback|HTTP [45]\d\d", p.read_text(encoding="utf-8", errors="ignore"), re.I)) for p in logs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock-dir", type=Path)
    ap.add_argument("--coin-dir", type=Path)
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--output", type=Path, default=Path("log_analysis"))
    args = ap.parse_args()
    since = datetime.now() - timedelta(hours=args.hours)
    all_rows = []
    for kind, root in (("stock", args.stock_dir), ("coin", args.coin_dir)):
        if not root:
            continue
        rows = []
        for path in find_csvs(root, kind):
            rows.extend(analyze_csv(path, since))
        # 같은 종목이 월별 파일에 중복되면 최근 레코드 기준으로 합치지 않고 그대로 표시한다.
        for row in rows:
            row["bot"] = kind
        all_rows.extend(rows)
        print(f"[{kind}] samples={sum(r['samples'] for r in rows)} errors={count_errors(root)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = list(all_rows[0]) if all_rows else ["bot"]
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(all_rows)
    args.output.with_suffix(".json").write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in all_rows:
        print(f"{r['bot']:5} {r['name']:<18} {r['trend']} {r['period_change_pct']:+.2f}% samples={r['samples']}")
    print(f"저장: {args.output.with_suffix('.csv')} / {args.output.with_suffix('.json')}")


if __name__ == "__main__":
    main()
