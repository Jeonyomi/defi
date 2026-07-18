"""LP 레짐 백테스트 — 상시 LP vs 변동성 게이트 LP vs 펀딩 캐리.

데이터 (전부 실측):
- ETH 가격: Binance USDT-M 1h (감마 손실 = 실현분산 경로 그대로)
- 펀딩: Hyperliquid mainnet ETH fundingHistory (시간별 실요율)

모델 (라이브 analytics와 동일 골격):
- 일 감마손실율 = m × RV_day / 8   (RV_day = 그날 1h 로그수익률 제곱합)
- 일 수수료율   = m × pool_yield / 365   (pool_yield는 라이브 실측 고정 — 한계 명시)
- 게이트: 직전 30일 실현변동성 < 손익분기 × ratio 일 때만 LP 가동

사용:
    python scripts/backtest_lp_regime.py --days 400 --fee-apr-m6 13.0 --m 6.0
"""

import argparse
import json
import math
import time
from datetime import datetime, timezone

import requests

DAY_MS = 86_400_000


def fetch_eth_1h(days: int) -> list[tuple[int, float]]:
    """Binance ETHUSDT 선물 1h 종가 (ts_ms, close)."""
    end = int(time.time() * 1000)
    cur = end - days * DAY_MS
    out: list[tuple[int, float]] = []
    while cur < end:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": "ETHUSDT", "interval": "1h",
                    "startTime": cur, "endTime": end, "limit": 1500},
            timeout=15,
        )
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for k in batch:
            t = int(k[0])
            if not out or t > out[-1][0]:
                out.append((t, float(k[4])))
        cur = int(batch[-1][0]) + 3_600_000
        time.sleep(0.25)
    return out[:-1]


def fetch_hl_funding(days: int) -> dict[int, float]:
    """HL mainnet ETH 시간별 펀딩율 {hour_ts_ms: rate}. 숏 수취 = +rate."""
    end = int(time.time() * 1000)
    cur = end - days * DAY_MS
    rates: dict[int, float] = {}
    while cur < end:
        r = requests.post(
            "https://api.hyperliquid.xyz/info", timeout=15,
            json={"type": "fundingHistory", "coin": "ETH",
                  "startTime": cur, "endTime": end},
        )
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for it in batch:
            rates[int(it["time"])] = float(it["fundingRate"])
        last = int(batch[-1]["time"])
        if last <= cur:
            break
        cur = last + 1
        time.sleep(0.2)
    return rates


def run(days: int, m: float, fee_apr_m6: float, gate_ratio: float,
        rerange_cost_bps: float) -> dict:
    candles = fetch_eth_1h(days)
    funding = fetch_hl_funding(days)
    if len(candles) < 24 * 40:
        raise SystemExit(f"캔들 부족: {len(candles)}")

    pool_yield = fee_apr_m6 / 100.0 / 6.0          # 풀레인지 환산 연수익률
    breakeven = math.sqrt(8 * pool_yield)          # 레인지 무관 손익분기 변동성
    gate = breakeven * gate_ratio
    fee_day = m * pool_yield / 365.0

    # 일 단위 집계
    by_day: dict[int, dict] = {}
    prev = None
    for ts, px in candles:
        d = ts // DAY_MS
        rec = by_day.setdefault(d, {"rv": 0.0, "fund": 0.0})
        if prev is not None:
            r = math.log(px / prev)
            rec["rv"] += r * r
        prev = px
    for ts, rate in funding.items():
        d = ts // DAY_MS
        if d in by_day:
            by_day[d]["fund"] += rate

    days_sorted = sorted(by_day)[1:]               # 첫 부분일 제외
    # 전략별 누적 (단위자본 1.0, 단리 합산 — 소액이라 복리 차이 무시)
    strat = {k: 0.0 for k in ("lp_always", "lp_gated", "carry", "combo_now", "combo_gated")}
    active_days = 0
    rv_window: list[float] = []
    gate_state = True
    switches = 0
    daily_rows = []

    for d in days_sorted:
        rec = by_day[d]
        gamma = m * rec["rv"] / 8.0
        lp_net = fee_day - gamma
        fund = rec["fund"]                          # 숏 1.0 노셔널 일일 수취율

        # 게이트 판정은 전일까지의 30d 변동성 (look-ahead 없음)
        if len(rv_window) >= 30:
            sigma30 = math.sqrt(sum(rv_window[-30:]) / 30 * 365)
            new_state = sigma30 < gate
        else:
            new_state = False                       # 워밍업 구간은 보수적으로 비활성
        if new_state != gate_state:
            switches += 1
            strat["lp_gated"] -= rerange_cost_bps / 10_000.0
            strat["combo_gated"] -= rerange_cost_bps / 10_000.0
        gate_state = new_state

        strat["lp_always"] += lp_net
        strat["carry"] += fund
        strat["combo_now"] += lp_net + 0.45 * fund  # 현행: LP 1.0 + 헤지숏 0.45 노셔널
        if gate_state:
            strat["lp_gated"] += lp_net
            strat["combo_gated"] += lp_net + 0.45 * fund
            active_days += 1
        else:
            strat["combo_gated"] += fund            # LP 접은 구간은 풀 펀딩 캐리
        rv_window.append(rec["rv"])
        daily_rows.append((d, lp_net, fund, gate_state))

    n = len(days_sorted)
    apr = {k: v / n * 365 * 100 for k, v in strat.items()}

    # 최근 30일 레짐
    last30 = daily_rows[-30:]
    lp30 = sum(r[1] for r in last30) / 30 * 365 * 100
    fund30 = sum(r[2] for r in last30) / 30 * 365 * 100

    # 창 전체 평균 실현변동성과, 상시 LP가 본전이 되려면 필요한 수수료 APR(m=6 기준)
    avg_var = sum(by_day[d]["rv"] for d in days_sorted) / n * 365
    avg_vol = math.sqrt(avg_var)
    required_fee_apr_m6 = avg_var / 8 * 6 * 100

    return {
        "window_days": n, "m": m, "fee_apr_m6": fee_apr_m6,
        "breakeven_vol_pct": breakeven * 100, "gate_vol_pct": gate * 100,
        "apr_pct": {k: round(v, 2) for k, v in apr.items()},
        "gated_active_pct": round(active_days / n * 100, 1),
        "gate_switches": switches,
        "last30d_apr": {"lp_net": round(lp30, 2), "funding": round(fund30, 2)},
        "avg_realized_vol_pct": round(avg_vol * 100, 1),
        "required_fee_apr_m6_pct": round(required_fee_apr_m6, 1),
        "funding_coverage_days": round(len(funding) / 24, 1),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--m", type=float, default=6.0)
    ap.add_argument("--fee-apr-m6", type=float, default=13.0,
                    help="현재 풀에서 m=6일 때 실측 수수료 APR%%")
    ap.add_argument("--gate-ratio", type=float, default=0.9,
                    help="게이트 = 손익분기 변동성 × ratio")
    ap.add_argument("--rerange-cost-bps", type=float, default=15.0,
                    help="게이트 전환(진입/철수) 1회당 비용 — 가스+슬리피지 추정")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    res = run(args.days, args.m, args.fee_apr_m6, args.gate_ratio,
              args.rerange_cost_bps)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
