import csv
from decimal import Decimal
from html import escape
from pathlib import Path
from string import Template

from config_test import BACKTEST_DIR, RESULTS_DIR, SYMBOL

RESULT_COLUMNS = (
    "buy_price", "sell_price", "completed_cycles", "ending_equity_usdt",
    "net_profit_usdt", "net_profit_pct",
)


def format_price(value: Decimal) -> str:
    whole, _, fraction = format(value, "f").partition(".")
    return f"{whole}.{fraction.rstrip('0').ljust(4, '0')}"


def format_percent(value: Decimal) -> str:
    return f"{value:+,.2f}%"


def net_profit(row: dict) -> str:
    return f"{row['net_profit_usdt']:+,.2f} USDT ({format_percent(row['net_profit_pct'])})"


def metric(label: str, value: str) -> str:
    return f'<div class="metric"><dt>{label}</dt><dd>{escape(value)}</dd></div>'


def heatmap(results: list[dict], buys: list[Decimal], sells: list[Decimal]) -> str:
    by_pair = {(row["buy_price"], row["sell_price"]): row for row in results}
    best = results[0]
    highest = max(0, best["net_profit_pct"])
    lowest = min(0, results[-1]["net_profit_pct"])
    columns = "".join(f'<th scope="col">{format_price(sell)}</th>' for sell in sells)
    rows = []
    for buy in buys:
        cells = [f'<th scope="row">{format_price(buy)}</th>']
        for sell in sells:
            row = by_pair.get((buy, sell))
            if row is None:
                cells.append('<td class="unavailable" aria-label="Unavailable"></td>')
                continue
            percent = row["net_profit_pct"]
            if percent > 0:
                hue, strength = 151, percent / highest
            elif percent < 0:
                hue, strength = 8, percent / lowest
            else:
                hue, strength = 210, 0
            lightness = 97 - 28 * strength
            tooltip = (f"BUY {format_price(buy)}\nSELL {format_price(sell)}\n"
                       f"Net profit {net_profit(row)}\nCompleted cycles {row['completed_cycles']:,}")
            title = escape(tooltip).replace("\n", "&#10;")
            highlight = ' class="best"' if row is best else ""
            cells.append(f'<td{highlight} style="background:hsl({hue} 65% {lightness:.2f}%)" title="{title}">'
                         f'{format_percent(percent)}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")

    return f"""<section class="panel">
      <h2 id="heatmap-heading">Profitability heatmap</h2>
      <div class="scroll" role="region" aria-labelledby="heatmap-heading" tabindex="0">
        <table class="heatmap" aria-labelledby="heatmap-heading">
          <thead>
            <tr><td></td><th colspan="{len(sells)}" scope="colgroup">SELL price</th></tr>
            <tr><th scope="col">BUY price</th>{columns}</tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>"""


def top_combinations(results: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{format_price(row['buy_price'])}</td><td>{format_price(row['sell_price'])}</td>"
        f"<td>{format_percent(row['net_profit_pct'])}</td><td>{row['net_profit_usdt']:+,.2f}</td>"
        f"<td>{row['completed_cycles']:,}</td></tr>"
        for row in results[:5]
    )
    return f"""<section class="panel">
      <h2 id="top-heading">Top 5 combinations</h2>
      <div class="scroll" role="region" aria-labelledby="top-heading" tabindex="0">
        <table aria-labelledby="top-heading">
          <thead><tr><th scope="col">BUY</th><th scope="col">SELL</th><th scope="col">Net profit %</th>
            <th scope="col">Net profit USDT</th><th scope="col">Completed cycles</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>"""


def save_results(results: list[dict], buys: list[Decimal], sells: list[Decimal],
                 directory: Path = RESULTS_DIR) -> tuple[Path, Path]:
    best = results[0]
    start, end = best["start_utc"], best["end_utc"]
    prefix = f"{SYMBOL}_{start:%Y%m%dT%H%MZ}_{end:%Y%m%dT%H%MZ}"
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{prefix}_combinations.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    multiple = len(results) > 1
    label = "Best " if multiple else ""
    metrics = [
        metric(label + "BUY price", format_price(best["buy_price"])),
        metric(label + "SELL price", format_price(best["sell_price"])),
    ]
    if not multiple:
        metrics.extend([
            metric("Initial balance", f"{best['initial_usdt']:,.2f} USDT"),
            metric("Final equity", f"{best['ending_equity_usdt']:,.2f} USDT"),
        ])
    metrics.extend([
        metric("Best net profit" if multiple else "Net profit", net_profit(best)),
        metric("Completed cycles", f"{best['completed_cycles']:,}"),
    ])

    template = Template((BACKTEST_DIR / "report_template.html").read_text(encoding="utf-8"))
    report_path = directory / f"{prefix}_report.html"
    report_path.write_text(template.substitute(
        symbol=escape(SYMBOL), metrics="".join(metrics),
        details=heatmap(results, buys, sells) + top_combinations(results) if multiple else "",
    ), encoding="utf-8")
    return report_path, csv_path
