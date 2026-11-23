"""Shared market data acquisition for historical replay and scheduled A/H tracking."""
import argparse
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
from time import sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FINANCE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PRICES = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"
HK_PRICES = "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"
HK_ADJUSTED = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
MINUTES = "https://web.ifzq.gtimg.cn/appstock/app/day/query"
FX = "https://api.hkma.gov.hk/public/market-data-and-statistics/monthly-statistical-bulletin/er-ir/er-eeri-daily"
BEIJING = timezone(timedelta(hours=8))
SESSION_SLOTS = {"morning": {"A": "0930", "HK": "0930"}, "afternoon": {"A": "1430", "HK": "1530"}}
ELAPSED = {"A": 210, "HK": 300}
DATA_ERRORS = (OSError, TypeError, KeyError, ValueError)


def get_json(endpoint, params):
    url = endpoint + "?" + urlencode(params)
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as response:
                return json.load(response)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            sleep(0.5 * (2 ** attempt))


def daily_endpoint(code, adjustment):
    if code.startswith("hk"):
        return HK_PRICES if adjustment == "raw" else HK_ADJUSTED
    return PRICES


def daily_rows(code, year, end, adjustment="raw", live=False):
    last = min(end, f"{year}-12-31")
    first = f"{year}-01-01"
    # Tencent treats the requested end date as exclusive in parts of the feed.
    exclusive = last if year == int(end[:4]) else (date.fromisoformat(last) + timedelta(days=1)).isoformat()
    if live:
        # The dated feed omits today's unfinished adjusted row. Its open anchors the scale only.
        exclusive = ""
    suffix = "" if adjustment == "raw" else adjustment
    data = get_json(daily_endpoint(code, adjustment), {"param": f"{code},day,{first},{exclusive},320,{suffix}"})
    if data["code"] != 0 or code not in data["data"]:
        raise ValueError(f"{code} {year} 行情响应格式异常")
    series = data["data"][code]
    rows = series.get(suffix + "day", [])
    return {day: {"date": day, "open": float(o), "close": float(c), "high": float(h), "low": float(lo),
                  "volume": number(extra[0]) if extra else None}
            for day, o, c, h, lo, *extra in rows if first <= day <= last}


def get_bars(code, year, end, adjusted):
    rows = daily_rows(code, year, end, "hfq" if adjusted else "raw")
    return {day: [bar[k] for k in ("open", "close", "high", "low")] for day, bar in rows.items()}


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def get_fx(end, start="2018-12-01"):
    rates = {}
    for offset in range(0, 20000, 100):
        response = get_json(FX, {"offset": offset, "fields": "end_of_day,cny"})
        if not response["header"]["success"]:
            raise ValueError("香港金管局汇率响应异常")
        rows = response["result"]["records"]
        rates.update((r["end_of_day"], r["cny"]) for r in rows if r["cny"] and r["end_of_day"] <= end)
        if not rows or rows[-1]["end_of_day"] < start:
            break
    return rates


def previous_fx(rates, dates, day):
    i = bisect_right(dates, (date.fromisoformat(day) - timedelta(days=1)).isoformat()) - 1
    if i >= 0 and (date.fromisoformat(day) - date.fromisoformat(dates[i])).days <= 8:
        return dates[i], rates[dates[i]]
    return None, None


def get_financials(code, annual=True):
    params = dict(reportName="RPT_LICO_FN_CPD", columns="ALL", pageSize=100,
                  sortColumns="REPORTDATE", sortTypes="-1", filter=f'(SECURITY_CODE="{code[2:]}")')
    result = get_json(FINANCE, {**params, "pageNumber": 1})["result"]
    data = list(result["data"])
    for page in range(2, result["pages"] + 1):
        data.extend(get_json(FINANCE, {**params, "pageNumber": page})["result"]["data"])
    reports = {}
    for row in data:
        period = row["REPORTDATE"][:10]
        if (annual and not period.endswith("-12-31")) or not row["NOTICE_DATE"] or not row["UPDATE_DATE"]:
            continue
        notice, update = row["NOTICE_DATE"][:10], row["UPDATE_DATE"][:10]
        if not period < min(notice, update):
            continue
        fields = ("BASIC_EPS", "PARENT_NETPROFIT", "BPS", "WEIGHTAVG_ROE", "TOTAL_OPERATE_INCOME")
        report = dict(zip(("eps", "profit", "book", "roe", "revenue"), (row[k] for k in fields)))
        report.update(period=period, notice=notice, updated=update)
        if period in reports and report != reports[period]:
            raise ValueError(f"{code} {period} 同期财报有多个版本，需要明确版本口径")
        reports[period] = report
    return [reports[key] for key in sorted(reports)]


def intraday_rows(code, end):
    payload = get_json(MINUTES, {"code": code})
    days = {}
    for row in payload["data"][code]["data"]:
        day = date.fromisoformat(f'{row["date"][:4]}-{row["date"][4:6]}-{row["date"][6:]}').isoformat()
        if day <= end:
            days[day] = {}
            for line in row["data"]:
                time, price, *extra = line.split()
                timestamp = datetime.strptime(time, "%H%M").strftime("%H%M")
                price = number(price)
                if price:
                    days[day][timestamp] = {"price": price, "volume": number(extra[0]) if extra else None,
                                            "amount": number(extra[1]) if len(extra) > 1 else None}
    return days


def get_minutes(code, end):
    return {day: {time: row["price"] for time, row in quotes.items()}
            for day, quotes in intraday_rows(code, end).items()}


def available(reports, day, notice_only=False):
    # At least one calendar day of lag, since the feed supplies dates only.
    versions = {}
    for row in reports:
        stamp = row["notice"] if notice_only else max(row["notice"], row["updated"])
        if stamp < day and (row["period"] not in versions or stamp > versions[row["period"]][0]):
            versions[row["period"]] = stamp, row
    return [versions[period][1] for period in sorted(versions)]


def read_filings(path):
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    filings = {}
    for code, rows in document.items():
        securities_code(code)
        filings[code] = []
        for row in rows:
            if isinstance(row, list):
                year, stamp, eps, profit, source = row
                row = dict(period=f"{year}-12-31", notice=stamp, updated=stamp,
                           eps=eps, profit=profit, source=source)
            period, notice, updated = (date.fromisoformat(row[key]) for key in ("period", "notice", "updated"))
            if (period >= min(notice, updated) or row.get("currency", "CNY") != "CNY"
                    or not str(row.get("source", "")).startswith(("https://", "http://"))):
                raise ValueError(f"{code} {period} 缺合格披露日期或来源")
            filings[code].append(row)
    return filings


def recent_daily(code, day, adjustment, limit=120, live=False):
    last = date.fromisoformat(day)
    rows = daily_rows(code, last.year, day, adjustment, live=live)
    if len(rows) <= limit:
        rows = {**daily_rows(code, last.year - 1, day, adjustment), **rows}
    return dict(sorted(rows.items())[-(limit + 1):])


def session_quote(code, day, cutoff, rows, previous):
    current = rows.get(cutoff)
    if not current:
        return None
    earlier = [row["price"] for t, row in rows.items() if "0930" <= t <= cutoff]
    hour, minute = cutoff[:2], cutoff[2:]
    return dict(code=code, time=f"{day}T{hour}:{minute}:00+08:00",
                currency="HKD" if code.startswith("hk") else "CNY", source=MINUTES,
                price=current["price"], open=rows.get("0930", {}).get("price"),
                prev_close=previous, prev_close_source=daily_endpoint(code, "raw") if previous is not None else None,
                high=max(earlier), low=min(earlier),
                volume=current["volume"], amount=current["amount"], auction_volume=None,
                volume_unit="股" if code.startswith("hk") else "手",
                ohlc_basis="截至指定分钟的供应商分时报价采样")


def track(code, day, session, as_of):
    market = "HK" if code.startswith("hk") else "A"
    cutoff = SESSION_SLOTS[session][market]
    result = dict(market=market, date=day, cutoff=f"{cutoff[:2]}:{cutoff[2:]}", quote=None,
                  volprice=None, gaps=[])
    slot = datetime.fromisoformat(f"{day}T{result['cutoff']}+08:00")
    if slot > as_of:
        result["gaps"].append("尚未到任务截点")
        return result
    try:
        minute_rows = intraday_rows(code, day).get(day, {})
    except DATA_ERRORS as error:
        result["gaps"].append(f"分时接口不可用：{error}")
        return result
    if cutoff not in minute_rows:
        result["gaps"].append("缺任务截点分时记录")
        return result
    try:
        raw = recent_daily(code, day, "raw")
    except DATA_ERRORS as error:
        raw = {}
        result["gaps"].append(f"日线接口不可用：{error}")
    history = {d: bar for d, bar in raw.items() if d < day}
    previous = history[max(history)]["close"] if history else None
    quote = session_quote(code, day, cutoff, minute_rows, previous)
    result["quote"] = quote
    if previous is None:
        result["gaps"].append("未取到上一交易日日线收盘价")
    if session == "morning":
        result["gaps"].append("未取到独立竞价匹配量")
        return result
    if not history or quote["open"] is None or quote["volume"] is None:
        result["gaps"].append("缺历史日线、开盘或截点累计量，无法构造量价输入")
        return result
    try:
        adjusted = recent_daily(code, day, "qfq", live=slot.date() == as_of.date())
    except DATA_ERRORS as error:
        adjusted = {}
        result["gaps"].append(f"前复权接口不可用：{error}")
    adjusted_open = adjusted.get(day, {}).get("open")
    use_adjusted = (adjusted_open is not None and math.isclose(adjusted_open, quote["open"], abs_tol=1e-6)
                    and history.keys() <= adjusted.keys())
    adjustment = "qfq" if use_adjusted else "raw"
    if not use_adjusted:
        result["gaps"].append("前复权日线缺失或与任务日原价无法校验同一尺度，量价初判使用统一原价")
    past = adjusted if use_adjusted else history
    bars = [past[d] for d in sorted(history)[-120:] if past[d]["volume"] is not None]
    current_bar = {key: quote[key] for key in ("open", "high", "low", "volume")}
    bars.append(dict(date=day, close=quote["price"], **current_bar))
    result["volprice"] = dict(market=market, elapsed_minutes=ELAPSED[market], bars=bars, adjustment=adjustment,
                              source=MINUTES, historical_source=daily_endpoint(code, adjustment))
    return result


def securities_code(code):
    if not re.fullmatch(r"(?:(?:sh|sz|bj)\d{6}|hk\d{5})", code):
        raise argparse.ArgumentTypeError("请使用交易所开头的代码，如 sh600036 或 hk00700")
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tracking = commands.add_parser("session", help="A/H 股任务截点行情与量价输入")
    tracking.add_argument("--session", choices=SESSION_SLOTS, required=True)
    tracking.add_argument("--date", default=datetime.now(BEIJING).date().isoformat())
    tracking.add_argument("--codes", nargs="+", type=securities_code, required=True)
    tracking.add_argument("--output", type=Path, help="另存为 JSON，供 volprice 读取")
    daily = commands.add_parser("daily", help="指定年份的原价／前复权／后复权日线")
    daily.add_argument("--code", type=securities_code, required=True)
    daily.add_argument("--year", type=int, required=True)
    daily.add_argument("--end", required=True)
    daily.add_argument("--adjust", choices=("raw", "qfq", "hfq"), default="raw")
    finance = commands.add_parser("finance", help="返回截至指定日期可用的财务")
    finance.add_argument("--code", type=securities_code, required=True)
    finance.add_argument("--as-of", required=True)
    finance.add_argument("--period", choices=("annual", "all"), default="annual")
    finance.add_argument("--filings", type=Path, help="已核实的发行人财报 JSON")
    fx = commands.add_parser("fx", help="指定日前最近的 HKMA 人民币兑港币观察值")
    fx.add_argument("--date", required=True)
    args = parser.parse_args()
    try:
        if args.command == "session":
            day = date.fromisoformat(args.date).isoformat()
            as_of = datetime.now(BEIJING)
            codes = list(dict.fromkeys(args.codes))
            with ThreadPoolExecutor(max_workers=min(len(codes), 8)) as pool:
                results = dict(zip(codes, pool.map(lambda code: track(code, day, args.session, as_of), codes)))
            ok = all(r["quote"] and r["quote"]["prev_close"] is not None for r in results.values())
            payload = dict(ok=ok, session=args.session, date=day, symbols=results)
        elif args.command == "daily":
            end = date.fromisoformat(args.end).isoformat()
            bars = daily_rows(args.code, args.year, end, args.adjust)
            payload = dict(ok=bool(bars), code=args.code, adjustment=args.adjust, end=end,
                           source=daily_endpoint(args.code, args.adjust), bars=list(bars.values()),
                           gap=None if bars else "未取到指定日期及复权口径的日线")
        elif args.command == "finance":
            as_of = date.fromisoformat(args.as_of).isoformat()
            entered = read_filings(args.filings).get(args.code, []) if args.filings else []
            annual = args.period == "annual"
            reports = [] if args.code.startswith("hk") else get_financials(args.code, annual)
            if annual:
                entered = [row for row in entered if row["period"].endswith("-12-31")]
            data = available(reports + entered, as_of)
            source = None if args.code.startswith("hk") else FINANCE
            payload = dict(ok=bool(data), code=args.code, as_of=as_of, currency="CNY", source=source,
                           reports=data, gap=None if data else "未取到符合时点要求的财务披露")
        else:
            day = date.fromisoformat(args.date)
            rates = get_fx(day.isoformat(), (day - timedelta(days=8)).isoformat())
            observed, rate = previous_fx(rates, sorted(rates), day.isoformat())
            payload = dict(ok=rate is not None, as_of=day.isoformat(), date=observed, hkd_per_cny=rate,
                           source=FX, gap=None if rate is not None else "指定日前 8 天内未取到汇率")
    except (*DATA_ERRORS, argparse.ArgumentTypeError) as error:
        payload = dict(ok=False, error=str(error))
    output = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    if getattr(args, "output", None):
        args.output.write_text(output + "\n", encoding="utf-8")
        output = json.dumps({k: payload[k] for k in ("ok", "date", "session", "error") if k in payload}
                            | {"output": str(args.output)}, ensure_ascii=False, indent=2)
    print(output)
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
