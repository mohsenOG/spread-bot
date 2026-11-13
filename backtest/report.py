from decimal import Decimal
from html import escape
from pathlib import Path
from string import Template

import pandas as pd

from backtest import MINUTE, BacktestResult
from config_test import BACKTEST_DIR, RESULTS_DIR


def format_holding(seconds: float | None) -> str:
    if seconds is None:
        return "N/A"
    minutes, seconds = divmod(round(seconds), 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    duration = f"{hours}h {minutes}m" + (f" {seconds}s" if seconds else "")
    return f"{days}d {duration}" if days else duration


def format_percent(value: Decimal) -> str:
    return f"{value:+.2E}%" if abs(value) >= 1_000_000 else f"{value:+,.2f}%"


def money(value: Decimal, *, signed: bool = False) -> str:
    return f"{value:+,.2f}" if signed else f"{value:,.2f}"


def color(value: Decimal) -> str:
    return "gain" if value > 0 else "loss" if value < 0 else ""


def metric(label: str, value: str, tone: str = "") -> str:
    return (f'<div class="metric"><dt>{escape(label)}</dt>'
            f'<dd class="{tone}">{escape(value)}</dd></div>')


def table_row(cells: tuple) -> str:
    return "<tr>" + "".join(
        f'<td data-sort="{escape(str(key), quote=True)}" class="{tone}">{escape(str(value))}</td>'
        for value, key, tone in cells
    ) + "</tr>"


def cycle_rows(cycles: pd.DataFrame) -> str:
    rows = []
    for row in cycles.itertuples(index=False):
        rows.append(table_row((
            (row.cycle, row.cycle, ""),
            (f"{row.sell_time_utc:%Y-%m-%d %H:%M}", row.sell_time_utc.timestamp(), ""),
            (f"{row.sell_price:.4f}", row.sell_price, ""),
            (f"{row.buy_time_utc:%Y-%m-%d %H:%M}", row.buy_time_utc.timestamp(), ""),
            (f"{row.buy_price:.4f}", row.buy_price, ""),
            (f"{row.sell_fee_usdc + row.buy_fee_usdc:,.4f}", row.sell_fee_usdc + row.buy_fee_usdc, ""),
            (money(row.profit_usdt, signed=True), row.profit_usdt, color(row.profit_usdt)),
            (format_percent(row.return_pct), row.return_pct, color(row.return_pct)),
            (format_holding(row.holding_seconds), row.holding_seconds, ""),
        )))
    return "".join(rows) or '<tr><td colspan="9" class="empty">No completed cycles.</td></tr>'


def month_rows(months: list[dict]) -> str:
    rows = []
    for row in months:
        profit, percent = row["profit_usdt"], row["return_pct"]
        rows.append(table_row((
            (row["month"], row["month"], ""),
            (row["completed_cycles"], row["completed_cycles"], ""),
            (money(row["opening_equity_usdt"]), row["opening_equity_usdt"], ""),
            (money(row["closing_equity_usdt"]), row["closing_equity_usdt"], ""),
            (money(profit, signed=True), profit, color(profit)),
            (format_percent(percent), percent, color(percent)),
        )))
    return "".join(rows)


def equity_chart(result: BacktestResult) -> str:
    start, end = (result.summary[key] for key in ("start_utc", "end_utc"))
    points = [(start, result.summary["initial_usdt"])]
    points.extend((time + MINUTE, value) for time, value in result.equity[
        ["candle_time_utc", "equity_usdt"]
    ].itertuples(index=False, name=None))
    left, right, top, bottom = 84, 954, 20, 240
    low, high = min(value for _, value in points), max(value for _, value in points)
    padding = (high - low) / 12 or high / 100
    low, high = low - padding, high + padding
    duration = (end - start).total_seconds()

    # Keep both extremes in each bucket so long histories still show brief moves.
    step = (len(points) + 699) // 700
    sampled = [points[0]]
    for index in range(0, len(points), step):
        bucket = points[index:index + step]
        extremes = (min(bucket, key=lambda point: point[1]), max(bucket, key=lambda point: point[1]))
        sampled.extend(sorted(set(extremes)))
    sampled.append(points[-1])
    coordinates = []
    for time, value in sampled:
        x = left + (right - left) * (time - start).total_seconds() / duration
        y = bottom - (bottom - top) * float((value - low) / (high - low))
        coordinates.append(f"{x:.2f},{y:.2f}")

    parts = ['<svg viewBox="0 0 980 285" role="img" aria-label="USDT equity over time">']
    for tick in range(5):
        y = bottom - (bottom - top) * tick / 4
        value = low + (high - low) * tick / 4
        parts.append(f'<line x1="{left}" x2="{right}" y1="{y}" y2="{y}"/>'
                     f'<text x="{left - 9}" y="{y + 4}" text-anchor="end">{value:,.2f}</text>')
    date_format = "%Y-%m-%d %H:%M" if duration < 3 * 86400 else "%Y-%m-%d"
    intervals = min(4, int(duration // 60))
    for tick in range(intervals + 1):
        x = left + (right - left) * tick / intervals
        date = start + (end - start) * tick / intervals
        anchor = "start" if tick == 0 else "end" if tick == intervals else "middle"
        parts.append(f'<text x="{x}" y="265" text-anchor="{anchor}">{date:{date_format}}</text>')
    line = " ".join(coordinates)
    parts.append(f'<polygon points="{left},{bottom} {line} {right},{bottom}"/>'
                 f'<polyline points="{line}"/></svg>')
    return "".join(parts)


def save_results(result: BacktestResult, directory: Path = RESULTS_DIR) -> Path:
    summary = result.summary
    directory.mkdir(parents=True, exist_ok=True)
    start, end = summary["start_utc"], summary["end_utc"]
    prefix = f"{summary['symbol']}_{start:%Y%m%dT%H%MZ}_{end:%Y%m%dT%H%MZ}"
    result.cycles.to_csv(directory / f"{prefix}_cycles.csv", index=False)
    result.equity.to_csv(directory / f"{prefix}_equity.csv", index=False)
    pd.DataFrame([summary]).to_csv(directory / f"{prefix}_summary.csv", index=False)

    net, realized, unrealized = (summary[key] for key in
                                 ("net_profit_usdt", "realized_profit_usdt", "unrealized_profit_usdt"))
    total, annualized = (summary[key] for key in ("total_return_pct", "annualized_return_pct"))
    opened = summary["open_sell_time_utc"]
    metrics = [
        metric("Starting capital", money(summary["initial_usdt"]) + " USDT"),
        metric("Ending equity", money(summary["ending_equity_usdt"]) + " USDT"),
        metric("Net profit", money(net, signed=True) + " USDT", color(net)),
        metric("Total return", format_percent(total), color(total)),
        metric("Annualized return", format_percent(annualized), color(annualized)),
        metric("Realized profit", money(realized, signed=True) + " USDT", color(realized)),
        metric("Unrealized P/L", money(unrealized, signed=True) + " USDT", color(unrealized)),
        metric("Completed cycles", f"{summary['completed_cycles']:,}"),
        metric("Average holding", format_holding(summary["average_holding_seconds"])),
        metric("Maximum holding", format_holding(summary["max_holding_seconds"])),
        metric("Open position", "USDC / waiting to buy" if opened is not None else "None"),
    ]
    if opened is None:
        position = '<p class="empty">No open position. The strategy ended in WAIT_SELL.</p>'
    else:
        position = '<dl class="metrics">' + "".join((
            metric("SELL time (UTC)", f"{opened:%Y-%m-%d %H:%M}"),
            metric("Available USDC", f"{summary['ending_usdc']:,.4f}"),
            metric("SELL fee", f"{summary['open_sell_fee_usdc']:,.4f} USDC"),
            metric("Final USDT close", f"{summary['final_close_usdc_per_usdt']:.4f} USDC"),
            metric("Mark-to-market value", money(summary["open_position_value_usdt"]) + " USDT"),
            metric("Unrealized P/L", money(unrealized, signed=True) + " USDT", color(unrealized)),
            metric("Time waiting for BUY", format_holding(summary["open_holding_seconds"])),
        )) + "</dl>"

    template = Template((BACKTEST_DIR / "report_template.html").read_text(encoding="utf-8"))
    path = directory / f"{prefix}_report.html"
    path.write_text(template.substitute(
        symbol=escape(summary["symbol"]), start=f"{start:%Y-%m-%d %H:%M}", end=f"{end:%Y-%m-%d %H:%M}",
        buy=f"{summary['buy_price']:.4f}", sell=f"{summary['sell_price']:.4f}",
        fee=f"{summary['maker_fee_pct']:.2f}%", candles=f"{summary['candles']:,}",
        gaps=f"{summary['missing_minutes']:,}", metrics="".join(metrics),
        chart=equity_chart(result), cycles=cycle_rows(result.cycles),
        months=month_rows(result.months), position=position,
    ), encoding="utf-8")
    return path
