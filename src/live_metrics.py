"""실시간 거래 성과 메트릭 — Sharpe / win rate / profit factor.

DB 기반 (백테스트 metrics.py 와 분리):
  - daily_snapshots (총평가 시계열) → daily returns → Sharpe / 변동성
  - trades (cycle 별 실현) → win rate / profit factor

cycle = 매수 → 매도 net qty 0 도달까지의 1 단위 거래 묶음.
미완 cycle 은 win/loss 통계에서 제외 (보수적).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import config
from . import db

TRADING_DAYS_PER_YEAR = 252


def daily_returns(days: int = 30) -> list[float]:
    """daily_snapshots 의 total_value 시계열 → 일별 returns.

    return[i] = (total[i] - total[i-1]) / total[i-1]
    """
    cutoff = (datetime.now() - timedelta(days=days + 1)).strftime("%Y-%m-%d")
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT date, total_value FROM daily_snapshots
            WHERE env = ? AND date >= ?
            ORDER BY date ASC
            """,
            (config.KIS_ENV, cutoff),
        ).fetchall()

    if len(rows) < 2:
        return []

    returns = []
    prev = None
    for r in rows:
        cur = float(r["total_value"] or 0)
        if cur <= 0:
            prev = cur
            continue
        if prev is not None and prev > 0:
            returns.append((cur - prev) / prev)
        prev = cur
    return returns


def sharpe_ratio(returns: list[float], risk_free_daily: float = 0.0) -> float | None:
    """Annualized Sharpe. 표본 < 2 또는 std=0 → None."""
    if len(returns) < 2:
        return None
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean_r - risk_free_daily) / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def annualized_volatility(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)


def closed_cycles(strategy: str | None = None, days: int | None = None) -> list[dict]:
    """trades → 종목별 cycle 분해 → closed cycle 의 실현 손익.

    반환: [{strategy, symbol, realized, buy_value, sell_value}, ...]

    cycle = 1번째 매수 → net qty 0 도달까지. 미완 cycle 제외.
    """
    where = "WHERE env = ?"
    params: list = [config.KIS_ENV]
    if strategy:
        where += " AND strategy = ?"
        params.append(strategy)
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        where += " AND executed_at >= ?"
        params.append(cutoff)

    with db.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id, strategy, symbol, side, quantity, price, executed_at
            FROM trades {where}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()

    cycles: list[dict] = []
    by_pair: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["strategy"], r["symbol"])
        if key not in by_pair:
            by_pair[key] = {
                "buy_qty": 0, "buy_value": 0.0,
                "sell_qty": 0, "sell_value": 0.0,
            }
        p = by_pair[key]
        qty = int(r["quantity"])
        price = float(r["price"])
        if r["side"].lower() == "buy":
            p["buy_qty"] += qty
            p["buy_value"] += qty * price
        else:
            p["sell_qty"] += qty
            p["sell_value"] += qty * price

        if p["buy_qty"] > 0 and p["sell_qty"] >= p["buy_qty"]:
            avg_buy = p["buy_value"] / p["buy_qty"]
            realized = p["sell_value"] - (p["sell_qty"] * avg_buy)
            cycles.append({
                "strategy": key[0],
                "symbol": key[1],
                "realized": realized,
                "buy_value": p["buy_value"],
                "sell_value": p["sell_value"],
            })
            by_pair[key] = {
                "buy_qty": 0, "buy_value": 0.0,
                "sell_qty": 0, "sell_value": 0.0,
            }

    return cycles


def win_rate_stats(strategy: str | None = None, days: int | None = None) -> dict:
    """closed cycle 기반 통계.

    반환:
      n_cycles, n_wins, n_losses,
      win_rate (%), avg_win, avg_loss,
      profit_factor (sum_wins / |sum_losses|),
      total_realized
    """
    cycles = closed_cycles(strategy, days)
    if not cycles:
        return {
            "n_cycles": 0, "n_wins": 0, "n_losses": 0,
            "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "profit_factor": 0.0, "total_realized": 0.0,
        }

    wins = [c["realized"] for c in cycles if c["realized"] > 0]
    losses = [c["realized"] for c in cycles if c["realized"] < 0]
    sum_wins = sum(wins)
    sum_losses = abs(sum(losses))

    return {
        "n_cycles": len(cycles),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": (len(wins) / len(cycles) * 100) if cycles else 0.0,
        "avg_win": (sum_wins / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": (sum_wins / sum_losses) if sum_losses > 0 else (
            float("inf") if sum_wins > 0 else 0.0
        ),
        "total_realized": sum(c["realized"] for c in cycles),
    }
