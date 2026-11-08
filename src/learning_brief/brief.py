# -*- coding: utf-8 -*-
"""Collect a keyless A-share learning brief and render facts, notes, and gaps.

This module does not import the notification stack, does not call an LLM, and
does not log in to a broker. Converted units stay in the explanation section
when the source payload itself does not declare a unit.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

BJ_TZ = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

INDEX_CODES = (
    "sh000001",
    "sz399001",
    "sz399006",
    "sh000300",
    "sh000688",
    "sh000016",
)
TURNOVER_CODES = ("sh000001", "sz399001")

def _sina_url() -> str:
    symbols = []
    for code in INDEX_CODES:
        symbols.extend((code, "s_" + code))
    return "https://hq.sinajs.cn/list=" + ",".join(symbols)


SINA_URL = _sina_url()
EASTMONEY_HOSTS = (
    "https://17.push2.eastmoney.com",
    "https://79.push2.eastmoney.com",
    "https://push2.eastmoney.com",
)
FENBU_URL = (
    "https://push2ex.eastmoney.com/getTopicZDFenBu"
    "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
)
ZT_URL = (
    "https://push2ex.eastmoney.com/getTopicZTPool"
    "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
    "&Pageindex=0&pagesize=200&sort=fbt:asc"
)
DT_URL = (
    "https://push2ex.eastmoney.com/getTopicDTPool"
    "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
    "&Pageindex=0&pagesize=20&sort=fund:asc"
)
SSE_MARGIN_URL = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
SZSE_MARGIN_URL = "https://www.szse.cn/api/report/ShowReport/data"
EM_MARGIN_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SSE_ETF_URL = "https://query.sse.com.cn/commonQuery.do"
KUAIXUN_URL = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
EM_COLUMN_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"

HEADLINE_RULES = (
    ("宏观", ("国务院", "国常会", "央行", "人民银行", "统计局", "GDP", "CPI", "PMI", "降准", "降息", "财政", "MLF", "LPR")),
    ("政策", ("证监会", "交易所", "监管", "稳市", "印花税", "国务院", "国常会")),
    ("行业", ("行业", "板块", "工信部", "发改委", "新能源", "半导体", "银行", "地产", "消费")),
    ("业绩", ("业绩", "净利", "净利润", "财报", "预告", "快报", "营收")),
)

KEY_NAMES = (
    "GEMINI_API_KEY",
    "GEMINI_API_KEYS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "LITELLM_API_KEY",
    "TUSHARE_TOKEN",
)
NOTIFY_NAMES = (
    "WECHAT_WEBHOOK_URL",
    "FEISHU_WEBHOOK_URL",
    "FEISHU_APP_SECRET",
    "DINGTALK_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "CUSTOM_WEBHOOK_URLS",
    "PUSHPLUS_TOKEN",
    "EMAIL_PASSWORD",
    "SMTP_PASSWORD",
)

HttpGet = Callable[[str, Mapping[str, str], float], bytes]


def beijing_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(BJ_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=BJ_TZ)
    return current.astimezone(BJ_TZ)


def format_stamp(current: datetime) -> str:
    return beijing_now(current).strftime("%Y-%m-%d %H:%M:%S")


def _decimal(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def _signed(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f}"


def default_http_get(url: str, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _get_json(http_get: HttpGet, url: str, headers: Mapping[str, str], timeout: float = 20) -> Any:
    raw = http_get(url, headers, timeout)
    return json.loads(raw.decode("utf-8", "replace"))


def _error_text(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text.replace("\n", " ")[:240]


def _headers(referer: str) -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Referer": referer}


def parse_sina_quotes(text: str) -> list[dict[str, Any]]:
    """Parse Sina full and summary index quotes from one response body."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if "hq_str_" not in line or '"' not in line:
            continue
        code = line.split("hq_str_", 1)[1].split("=", 1)[0].strip()
        body = line.split('"', 2)[1]
        parts = body.split(",")
        if code.startswith("s_"):
            if len(parts) < 6:
                continue
            rows.append(
                {
                    "kind": "summary",
                    "code": code[2:],
                    "name": parts[0],
                    "current": float(parts[1]),
                    "change": float(parts[2]),
                    "change_pct": float(parts[3]),
                    "amount_summary": float(parts[5]),
                }
            )
            continue
        if len(parts) < 32:
            continue
        current = float(parts[3])
        prev_close = float(parts[2])
        change = current - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0.0
        rows.append(
            {
                "kind": "full",
                "code": code,
                "name": parts[0],
                "current": current,
                "prev_close": prev_close,
                "change": change,
                "change_pct": change_pct,
                "amount_yuan": float(parts[9]),
                "quote_date": parts[30],
                "quote_time": parts[31],
            }
        )
    return rows


def merge_index_quotes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    full = {row["code"]: row for row in rows if row["kind"] == "full"}
    summary = {row["code"]: row for row in rows if row["kind"] == "summary"}
    merged: list[dict[str, Any]] = []
    for code in INDEX_CODES:
        item = full.get(code)
        if item is None:
            continue
        brief = summary.get(code)
        ratio = None
        amount_summary = None
        if brief and brief.get("amount_summary"):
            amount_summary = brief["amount_summary"]
            ratio = item["amount_yuan"] / amount_summary
        merged.append(
            {
                **item,
                "amount_summary": amount_summary,
                "amount_ratio": ratio,
                "amount_unit_checked": ratio is not None and 9000 <= ratio <= 11000,
            }
        )
    return merged


def summarize_fenbu(items: list[dict[str, Any]]) -> dict[str, int]:
    up = down = flat = 0
    for item in items:
        for key, value in item.items():
            bucket = int(key)
            count = int(value)
            if bucket > 0:
                up += count
            elif bucket < 0:
                down += count
            else:
                flat += count
    return {"up": up, "down": down, "flat": flat, "total": up + down + flat}


def _industry_counts(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in pool:
        name = str(row.get("hybk") or "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"name": name, "count": count} for name, count in ordered[:5]]


def parse_limit_pool(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    pool = data.get("pool") or []
    reported = data.get("tc")
    count = int(reported) if reported is not None else len(pool)
    return {
        "qdate": str(data.get("qdate") or ""),
        "count": count,
        "returned": len(pool),
        "complete": reported is None or len(pool) >= int(reported),
        "industries": _industry_counts(pool),
    }


def parse_sse_margin(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = ((payload.get("pageHelp") or {}).get("data")) or payload.get("result") or []
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or row.get("rzye") in (None, ""):
            continue
        parsed.append(
            {
                "date": str(row.get("opDate") or ""),
                "rzye": int(float(row["rzye"])),
                "rzmre": int(float(row["rzmre"])) if row.get("rzmre") not in (None, "") else None,
            }
        )
    if not parsed:
        return None
    parsed.sort(key=lambda item: item["date"])
    return parsed[-1]


def parse_szse_margin(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, list) or not payload:
        return None
    block = payload[0] if isinstance(payload[0], dict) else {}
    rows = block.get("data") or []
    if not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if row.get("jrrzye") in (None, ""):
        return None
    footer = str((block.get("metadata") or {}).get("footer") or "")
    return {
        "balance_raw": str(row.get("jrrzye")),
        "buy_raw": str(row.get("jrrzmr") or ""),
        "footer": _plain_footer(footer),
    }


def _plain_footer(footer: str) -> str:
    text = re.sub(r"<[^>]+>", " ", footer)
    return " ".join(text.split())[:240]


def parse_eastmoney_margin(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    result = payload.get("result") or {}
    rows = result.get("data") or []
    if not rows:
        return None
    row = rows[0]
    if row.get("FIN_BALANCE") in (None, ""):
        return None
    return {
        "date": str(row.get("STATISTICS_DATE") or "")[:10],
        "fin_balance": float(row["FIN_BALANCE"]),
        "fin_buy": float(row["FIN_BUY_AMT"]) if row.get("FIN_BUY_AMT") not in (None, "") else None,
    }


def parse_etf_page(payload: Mapping[str, Any]) -> dict[str, Any]:
    page = payload.get("pageHelp") or {}
    rows = page.get("data") or []
    volumes = []
    dates = set()
    for row in rows:
        if row.get("TOT_VOL") in (None, ""):
            continue
        volumes.append(float(row["TOT_VOL"]))
        if row.get("STAT_DATE"):
            dates.add(str(row["STAT_DATE"]))
    reported = page.get("total")
    reported_count = int(reported) if reported not in (None, "") else None
    return {
        "rows": len(volumes),
        "reported_total": reported_count,
        "complete": reported_count is not None and reported_count == len(volumes) and len(dates) <= 1,
        "stat_date": next(iter(dates), ""),
        "sum_raw": sum(volumes),
    }


def parse_board_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    rows = []
    for row in data.get("diff") or []:
        if row.get("f14") in (None, "") or row.get("f3") in (None, "-"):
            continue
        rows.append({"name": str(row["f14"]), "change_pct": float(row["f3"])})
    return rows


def parse_kuaixun(text: str) -> list[dict[str, str]]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    payload = json.loads(text[start : end + 1])
    items = []
    for row in payload.get("LivesList") or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "title": title,
                "time": str(row.get("showtime") or ""),
                "url": str(row.get("url_w") or ""),
                "source": "东方财富快讯",
            }
        )
    return items


def parse_em_column(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    items = []
    for row in ((payload.get("data") or {}).get("list")) or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "title": title,
                "time": str(row.get("showTime") or ""),
                "url": str(row.get("url") or ""),
                "source": str(row.get("mediaName") or "东方财富栏目353"),
            }
        )
    return items


def classify_headline(title: str) -> list[str]:
    labels = [label for label, words in HEADLINE_RULES if any(word in title for word in words)]
    return labels


def concept_for_date(current: datetime) -> dict[str, str]:
    lessons = {
        1: (
            "成交额",
            "成交额记录一段时间内成交的金额。指数行情里的成交额只覆盖该指数统计到的成交，"
            "把上证指数和深证成指的成交额相加，仍然要单独核对它是否等于交易所公布的全市场股票成交额。",
        ),
        2: (
            "融资余额",
            "融资余额是投资者向券商借入资金买入证券后尚未偿还的金额。"
            "它由券商报送后再汇总，通常晚于当天收盘行情，不能把它看成同一分钟的资金流入。",
        ),
        3: (
            "涨跌家数和涨跌停",
            "涨跌家数统计价格高于或低于前收盘价的证券数量。涨停和跌停还要看证券所属板块的涨跌幅限制。"
            "某个涨跌幅分布桶里的数量，不等于精确的涨停或跌停家数。",
        ),
        4: (
            "热点持续性",
            "一天的概念涨幅榜只说明这次取数时哪些名称涨得多。"
            "要讨论持续性，至少需要同一来源、同一口径在多个交易日的名单，并记下停更和改名。",
        ),
        5: (
            "ETF份额和申赎",
            "ETF份额变化可以提供申购和赎回方向的线索。份额增加不等于已经知道申购金额，"
            "因为还缺净值和深市等其他市场的份额。场内买卖成交额也是另一套数字。",
        ),
        6: (
            "行情日期和发布日期",
            "周末或休市时打开行情，接口仍可能返回最近一个交易日的收盘数据。"
            "简报里的生成时间、行情日期和融资或份额的统计日期要分开写。",
        ),
        7: (
            "事实、解释和未知",
            "事实保留接口里的原值、日期和来源。解释说明字段含义、单位换算和不能推出的范围。"
            "取不到、口径没有写明，或只有标题没有正文核对的内容，写入未知。",
        ),
    }
    title, body = lessons[beijing_now(current).isoweekday()]
    return {"title": title, "body": body}


def concept_persistence(
    previous: Mapping[str, Any] | None,
    names: list[str],
    session_date: str,
) -> dict[str, Any]:
    if not names:
        return {"status": "missing", "overlap": [], "previous_date": ""}
    if not previous or not previous.get("names"):
        return {"status": "unknown", "overlap": [], "previous_date": ""}
    previous_date = str(previous.get("concept_date") or "")
    overlap = [name for name in names if name in set(previous.get("names") or [])]
    if previous_date == session_date:
        return {"status": "same_session", "overlap": overlap, "previous_date": previous_date}
    return {"status": "compared", "overlap": overlap, "previous_date": previous_date}


def _safe_call(label: str, func: Callable[[], Any], errors: list[dict[str, str]]) -> Any:
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - each public source fails independently
        errors.append({"source": label, "error": _error_text(exc)})
        return None


def _fetch_indices(http_get: HttpGet) -> list[dict[str, Any]]:
    raw = http_get(SINA_URL, _headers("https://finance.sina.com.cn"), 20)
    return merge_index_quotes(parse_sina_quotes(raw.decode("gb18030", "replace")))


def _board_url(host: str, board_type: str, descending: bool) -> str:
    params = {
        "pn": "1",
        "pz": "5",
        "po": "1" if descending else "0",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": f"m:90 t:{board_type} f:!50",
        "fields": "f3,f12,f14",
    }
    return host + "/api/qt/clist/get?" + urllib.parse.urlencode(params)


def _fetch_board(http_get: HttpGet, board_type: str) -> dict[str, Any]:
    last_error = "no host attempted"
    for host in EASTMONEY_HOSTS:
        try:
            top = parse_board_rows(
                _get_json(http_get, _board_url(host, board_type, True), _headers("https://quote.eastmoney.com/"), 12)
            )
            bottom = parse_board_rows(
                _get_json(http_get, _board_url(host, board_type, False), _headers("https://quote.eastmoney.com/"), 12)
            )
            if top or bottom:
                return {"status": "ok", "host": host, "top": top, "bottom": bottom}
            last_error = f"{host} returned no rows"
        except Exception as exc:  # noqa: BLE001
            last_error = _error_text(exc)
    return {"status": "missing", "error": last_error, "top": [], "bottom": []}


def _fetch_pool(http_get: HttpGet, url: str, session_date: str) -> dict[str, Any]:
    dated = url + (("&date=" + session_date.replace("-", "")) if session_date else "")
    payload = _get_json(http_get, dated or url, _headers("https://quote.eastmoney.com/ztb/"))
    parsed = parse_limit_pool(payload)
    if parsed["count"] == 0 and session_date:
        payload = _get_json(http_get, url, _headers("https://quote.eastmoney.com/ztb/"))
        parsed = parse_limit_pool(payload)
    parsed["status"] = "ok" if parsed["qdate"] or parsed["count"] else "missing"
    return parsed


def _fetch_sse_margin(http_get: HttpGet) -> dict[str, Any] | None:
    today = beijing_now().strftime("%Y%m%d")
    start = (beijing_now() - timedelta(days=20)).strftime("%Y%m%d")
    params = {
        "isPagination": "true",
        "beginDate": start,
        "endDate": today,
        "tabType": "",
        "stockCode": "",
        "pageHelp.pageSize": "30",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "5",
    }
    url = SSE_MARGIN_URL + "?" + urllib.parse.urlencode(params)
    return parse_sse_margin(_get_json(http_get, url, _headers("https://www.sse.com.cn/")))


def _fetch_szse_margin(http_get: HttpGet, session_date: str) -> dict[str, Any] | None:
    start = datetime.strptime(session_date, "%Y-%m-%d") if session_date else beijing_now()
    last_error = ""
    attempted = []
    for offset in range(0, 8):
        day = (start - timedelta(days=offset)).strftime("%Y-%m-%d")
        attempted.append(day)
        params = {
            "SHOWTYPE": "JSON",
            "CATALOGID": "1837_xxpl",
            "txtDate": day,
            "tab1PAGENO": "1",
            "random": "0.2",
        }
        url = SZSE_MARGIN_URL + "?" + urllib.parse.urlencode(params)
        try:
            parsed = parse_szse_margin(
                _get_json(
                    http_get,
                    url,
                    _headers("https://www.szse.cn/disclosure/margin/margin/index.html"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            last_error = _error_text(exc)
            continue
        if parsed:
            parsed["date"] = day
            parsed["attempted_dates"] = attempted
            return parsed
    if last_error:
        raise RuntimeError(last_error + "；已尝试 " + ",".join(attempted))
    return None


def _fetch_em_margin(http_get: HttpGet) -> dict[str, Any] | None:
    params = {
        "reportName": "RPTA_WEB_MARGIN_DAILYTRADE",
        "columns": "STATISTICS_DATE,FIN_BALANCE,FIN_BUY_AMT",
        "pageNumber": "1",
        "pageSize": "1",
        "sortColumns": "STATISTICS_DATE",
        "sortTypes": "-1",
        "p": "1",
        "pageNo": "1",
        "pageNum": "1",
    }
    url = EM_MARGIN_URL + "?" + urllib.parse.urlencode(params)
    return parse_eastmoney_margin(_get_json(http_get, url, _headers("https://data.eastmoney.com/")))


def _fetch_etf_pair(http_get: HttpGet, session_date: str) -> dict[str, Any]:
    start = datetime.strptime(session_date, "%Y-%m-%d") if session_date else beijing_now()
    found: list[dict[str, Any]] = []
    errors: list[str] = []
    for offset in range(0, 10):
        if len(found) >= 2:
            break
        day = (start - timedelta(days=offset)).strftime("%Y-%m-%d")
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": "10000",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": day,
        }
        url = SSE_ETF_URL + "?" + urllib.parse.urlencode(params)
        try:
            parsed = parse_etf_page(_get_json(http_get, url, _headers("https://www.sse.com.cn/"), 30))
        except Exception as exc:  # noqa: BLE001
            errors.append(_error_text(exc))
            continue
        if parsed["rows"] and parsed["stat_date"] and parsed["stat_date"] not in {item["stat_date"] for item in found}:
            found.append(parsed)
    result: dict[str, Any] = {"status": "missing", "newer": None, "older": None, "errors": errors}
    if found:
        result["status"] = "ok"
        result["newer"] = found[0]
        result["older"] = found[1] if len(found) > 1 else None
    return result


def _fetch_headlines(http_get: HttpGet) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    rows: list[dict[str, str]] = []
    kuaixun = _safe_call(
        "东方财富快讯",
        lambda: parse_kuaixun(
            http_get(KUAIXUN_URL, _headers("https://kuaixun.eastmoney.com/"), 20).decode("utf-8", "replace")
        ),
        errors,
    )
    if kuaixun:
        rows.extend(kuaixun)
    params = {
        "client": "web",
        "biz": "web_news_col",
        "column": "353",
        "order": "1",
        "needInteractData": "0",
        "page_index": "1",
        "page_size": "30",
        "req_trace": "learning-brief",
        "fields": "showTime,title,mediaName,url",
        "types": "1,20",
    }
    column = _safe_call(
        "东方财富栏目353",
        lambda: parse_em_column(
            _get_json(
                http_get,
                EM_COLUMN_URL + "?" + urllib.parse.urlencode(params),
                _headers("https://kuaixun.eastmoney.com/"),
            )
        ),
        errors,
    )
    if column:
        rows.extend(column)
    seen = set()
    matched = []
    for row in rows:
        if row["title"] in seen:
            continue
        seen.add(row["title"])
        labels = classify_headline(row["title"])
        if not labels:
            continue
        matched.append({**row, "labels": labels})
    matched.sort(key=lambda item: item["time"], reverse=True)
    return {
        "fetched": len(seen),
        "matched": matched[:8],
        "matched_total": len(matched),
        "unmatched": max(len(seen) - len(matched), 0),
        "errors": errors,
    }


def _key_presence(env: Mapping[str, str]) -> dict[str, bool]:
    return {name: bool(str(env.get(name) or "").strip()) for name in (*KEY_NAMES, *NOTIFY_NAMES)}


def collect_market(
    http_get: HttpGet | None = None,
    now: datetime | None = None,
    previous_state: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    getter = http_get or default_http_get
    current = beijing_now(now)
    errors: list[dict[str, str]] = []
    indices = _safe_call("新浪指数行情", lambda: _fetch_indices(getter), errors) or []
    session_dates = sorted({row["quote_date"] for row in indices if row.get("quote_date")})
    session_date = session_dates[-1] if session_dates else ""

    breadth = _safe_call(
        "东方财富涨跌分布",
        lambda: _get_json(getter, FENBU_URL, _headers("https://quote.eastmoney.com/")),
        errors,
    )
    breadth_parsed = None
    if isinstance(breadth, dict):
        data = breadth.get("data") or {}
        if data.get("fenbu"):
            breadth_parsed = {
                "status": "ok",
                "qdate": str(data.get("qdate") or ""),
                **summarize_fenbu(data.get("fenbu") or []),
            }
    limit_up = _safe_call("东方财富涨停池", lambda: _fetch_pool(getter, ZT_URL, session_date), errors)
    limit_down = _safe_call("东方财富跌停池", lambda: _fetch_pool(getter, DT_URL, session_date), errors)
    industries = _safe_call("东方财富行业板块", lambda: _fetch_board(getter, "2"), errors) or {
        "status": "missing",
        "top": [],
        "bottom": [],
    }
    concepts = _safe_call("东方财富概念板块", lambda: _fetch_board(getter, "3"), errors) or {
        "status": "missing",
        "top": [],
        "bottom": [],
    }
    sse_margin = _safe_call("上交所融资融券汇总", lambda: _fetch_sse_margin(getter), errors)
    szse_margin = _safe_call("深交所融资融券汇总", lambda: _fetch_szse_margin(getter, session_date), errors)
    em_margin = _safe_call("东方财富融资融券汇总", lambda: _fetch_em_margin(getter), errors)
    etf = _safe_call("上交所ETF份额", lambda: _fetch_etf_pair(getter, session_date), errors) or {
        "status": "missing",
        "newer": None,
        "older": None,
        "errors": [],
    }
    headlines = _safe_call("新闻标题", lambda: _fetch_headlines(getter), errors) or {
        "fetched": 0,
        "matched": [],
        "matched_total": 0,
        "unmatched": 0,
        "errors": [],
    }
    concept_names = [row["name"] for row in concepts.get("top") or []]
    for label, board in (("东方财富行业板块", industries), ("东方财富概念板块", concepts)):
        if board.get("status") != "ok" and board.get("error"):
            errors.append({"source": label, "error": board["error"]})
    return {
        "generated_at": format_stamp(current),
        "session_date": session_date,
        "llm_called": False,
        "notifications_sent": False,
        "broker_connected": False,
        "key_presence": _key_presence(env if env is not None else os.environ),
        "indices": indices,
        "breadth": breadth_parsed or {"status": "missing"},
        "limit_up": limit_up or {"status": "missing", "count": None, "industries": []},
        "limit_down": limit_down or {"status": "missing", "count": None},
        "industries": industries,
        "concepts": concepts,
        "persistence": concept_persistence(previous_state, concept_names, session_date),
        "sse_margin": sse_margin,
        "szse_margin": szse_margin,
        "em_margin": em_margin,
        "etf": etf,
        "headlines": headlines,
        "lesson": concept_for_date(current),
        "errors": errors,
    }


def _coverage_rows(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    indices = payload.get("indices") or []
    turnover_ready = [row for row in indices if row["code"] in TURNOVER_CODES and row.get("amount_unit_checked")]
    sse = payload.get("sse_margin")
    szse = payload.get("szse_margin")
    em_margin = payload.get("em_margin")
    etf = payload.get("etf") or {}
    headlines = payload.get("headlines") or {}
    breadth = payload.get("breadth") or {}
    limit_up = payload.get("limit_up") or {}
    limit_down = payload.get("limit_down") or {}
    concepts = payload.get("concepts") or {}
    industries = payload.get("industries") or {}
    persistence = payload.get("persistence") or {}
    rows = [
        _coverage(
            "基本面",
            "宏观、政策、行业、业绩新变化",
            "部分" if headlines.get("matched") else "缺失",
            payload.get("generated_at") or "",
            "东方财富快讯；东方财富栏目353",
            "只有标题和关键词归类，没有全文核对，也不能确认这些标题就是新的变化",
        ),
        _coverage(
            "资金面",
            "上证指数与深证成指成交额",
            "已取得" if len(turnover_ready) == 2 else "缺失",
            str(payload.get("session_date") or ""),
            "新浪财经指数行情",
            "两只指数成交额相加仍可能不同于交易所公布的全市场股票成交额",
        ),
        _coverage(
            "资金面",
            "上交所融资余额原值",
            "已取得" if sse else "缺失",
            str((sse or {}).get("date") or ""),
            "query.sse.com.cn queryMargin.do 字段 rzye",
            "JSON 没有单位字段，折算成亿元放在解释里",
        ),
        _coverage(
            "资金面",
            "深交所融资余额原值",
            "已取得" if szse else "缺失",
            str((szse or {}).get("date") or ""),
            "www.szse.cn CATALOGID=1837_xxpl 字段 jrrzye",
            "页脚公式中的元不套用到汇总单元格；单元格自身没有单位字段，本次不换算",
        ),
        _coverage(
            "资金面",
            "两市融资余额对照",
            "已取得" if em_margin else "缺失",
            str((em_margin or {}).get("date") or ""),
            "东方财富 RPTA_WEB_MARGIN_DAILYTRADE 字段 FIN_BALANCE",
            "日期常晚于行情日期；页面惯例单位写入解释，不与不同日期的交易所数字相加",
        ),
        _coverage(
            "资金面",
            "ETF申赎",
            "部分" if (etf.get("newer") or {}).get("complete") else "缺失",
            str((etf.get("newer") or {}).get("stat_date") or ""),
            "上交所 ETF 份额 JSON 字段 TOT_VOL",
            "只有上交所份额原值及两日差额，没有申赎金额，也没有深市份额",
        ),
        _coverage(
            "情绪面",
            "涨跌家数",
            "已取得" if breadth.get("status") == "ok" else "缺失",
            str(breadth.get("qdate") or ""),
            "东方财富 getTopicZDFenBu",
            "按涨跌幅分布桶相加，不是交易所官方家数，也不是精确涨跌停家数",
        ),
        _coverage(
            "情绪面",
            "涨停与跌停家数",
            "已取得"
            if limit_up.get("status") == "ok" and limit_down.get("status") == "ok"
            else "部分"
            if limit_up.get("status") == "ok" or limit_down.get("status") == "ok"
            else "缺失",
            str(limit_up.get("qdate") or limit_down.get("qdate") or ""),
            "东方财富涨停池、跌停池字段 tc",
            "专题池口径可能与按价格自行计算的涨跌停不同；简报不列证券代码",
        ),
        _coverage(
            "情绪面",
            "行业与概念涨跌榜",
            "已取得" if industries.get("status") == "ok" and concepts.get("status") == "ok" else "部分" if industries.get("status") == "ok" or concepts.get("status") == "ok" else "缺失",
            str(payload.get("session_date") or ""),
            "东方财富板块列表",
            "这是价格涨跌幅，不是行业基本面变化",
        ),
        _coverage(
            "情绪面",
            "热点持续性",
            "已比较" if persistence.get("status") == "compared" else "缺失",
            str(payload.get("session_date") or ""),
            "本目录上一份 hot_concepts_state.json",
            "没有上一交易日的同口径名单时，持续性保持未知；一次名称重合也不能证明热点持续",
        ),
        _coverage(
            "解释",
            "LLM 市场综述",
            "阻塞",
            payload.get("generated_at") or "",
            "未调用模型",
            "没有可用且已获准使用的 LLM API 密钥；订阅本身不提供本次调用",
        ),
    ]
    return rows


def _coverage(dimension: str, metric: str, status: str, as_of: str, source: str, gap: str) -> dict[str, str]:
    return {
        "dimension": dimension,
        "metric": metric,
        "status": status,
        "as_of": as_of or "无日期",
        "source": source,
        "gap": gap,
    }


def render_brief(payload: Mapping[str, Any]) -> str:
    lines: list[str] = [
        "# A股全市场学习简报",
        "",
        f"- 生成时间：{payload.get('generated_at')}（北京时间，UTC+8）",
        f"- 指数行情日期：{payload.get('session_date') or '未知'}",
        "- 范围：全市场学习材料。证券涨跌榜只作为市场现象的计数和名称，不写买卖建议。",
        "- 模型：本次 llm_called=false。上游大盘复盘的模型综述没有运行。",
        "- 通知：本次 notifications_sent=false。钉钉、企业微信、飞书、Telegram、邮件、Webhook 都没有发送。",
        "- 交易：本次 broker_connected=false。没有券商登录、下单或持仓修改。",
        "",
        "## 1. 事实",
        "",
        "### 1.1 基本面线索",
        "",
        "下面是标题命中宏观、政策、行业或业绩关键词的公开条目。命中只表示标题里有这些词。",
        "",
    ]
    headlines = payload.get("headlines") or {}
    matched = headlines.get("matched") or []
    if matched:
        lines.append(
            f"本次读到 {headlines.get('fetched', 0)} 条去重标题，"
            f"其中 {headlines.get('matched_total', len(matched))} 条命中关键词，这里列出时间最新的 {len(matched)} 条。"
        )
        lines.append("")
        for index, item in enumerate(matched, start=1):
            labels = "、".join(item.get("labels") or [])
            lines.append(
                f"{index}. [{labels}] {item.get('time') or '时间未知'} {item.get('source')}：{item.get('title')}"
            )
            if item.get("url"):
                lines.append(f"   链接：{item['url']}")
    else:
        lines.append("本次没有取得可归类的标题。")
    lines.extend(["", "### 1.2 资金面", ""])
    lines.extend(_render_indices(payload.get("indices") or []))
    lines.extend(_render_margin(payload))
    lines.extend(_render_etf(payload.get("etf") or {}))
    lines.extend(["", "### 1.3 情绪面", ""])
    lines.extend(_render_breadth(payload.get("breadth") or {}))
    lines.extend(_render_limits(payload.get("limit_up") or {}, payload.get("limit_down") or {}))
    lines.extend(_render_boards("行业涨跌幅前 5 名和后 5 名", payload.get("industries") or {}))
    lines.extend(_render_boards("概念涨跌幅前 5 名和后 5 名", payload.get("concepts") or {}))
    lines.extend(_render_persistence(payload.get("persistence") or {}))
    lines.extend(["", "## 2. 解释", ""])
    lines.extend(_render_explanations(payload))
    lines.extend(["", "## 3. 未知", ""])
    lines.extend(_render_unknowns(payload))
    lesson = payload.get("lesson") or {}
    lines.extend(
        [
            "",
            "## 4. 今日入门概念",
            "",
            f"概念：{lesson.get('title') or '未知'}",
            "",
            "这一节是固定教材说明，用来解释字段，不新增行情数字。",
            "",
            lesson.get("body") or "",
            "",
            "## 5. 覆盖矩阵",
            "",
            "| 维度 | 指标 | 状态 | 日期 | 来源 | 缺口 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in _coverage_rows(payload):
        lines.append(
            "| {dimension} | {metric} | {status} | {as_of} | {source} | {gap} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## 6. 已有 16:30 任务如何读取",
            "",
            "工作日 16:30（北京时间）的简报消息已经由现有 Grok Bot 任务发送。本程序不创建第二个定时任务，也不调用通知接口。",
            "该任务应读取持续运行目录中的 `latest.md`，只转述文件里的事实、解释和未知。文件不存在或日期不是当次运行时，消息应写明没有新简报，并由任务停止补写数字。",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_indices(indices: list[dict[str, Any]]) -> list[str]:
    if not indices:
        return ["指数行情本次没有取到。", ""]
    lines = [
        "新浪财经同一次响应里的完整指数行情。成交额使用响应中的第 10 个字段（从 1 开始计数，程序下标 9）。",
        "",
        "| 指数 | 代码 | 行情日期 | 行情时间 | 点位 | 涨跌点 | 涨跌幅 | 成交额原值 | 简要行情成交额原值 | 二者比值 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in indices:
        ratio = "无" if row.get("amount_ratio") is None else _decimal(row["amount_ratio"], 2)
        summary = "无" if row.get("amount_summary") is None else str(int(round(row["amount_summary"])))
        lines.append(
            "| {name} | {code} | {quote_date} | {quote_time} | {current} | {change} | {pct}% | {amount} | {summary} | {ratio} |".format(
                name=row["name"],
                code=row["code"],
                quote_date=row.get("quote_date") or "",
                quote_time=row.get("quote_time") or "",
                current=_decimal(row["current"], 2),
                change=_signed(row["change"], 2),
                pct=_signed(row["change_pct"], 2),
                amount=str(int(round(row["amount_yuan"]))),
                summary=summary,
                ratio=ratio,
            )
        )
    checked = [row for row in indices if row["code"] in TURNOVER_CODES and row.get("amount_unit_checked")]
    lines.append("")
    if len(checked) == 2:
        total = sum(row["amount_yuan"] for row in checked)
        lines.append(
            f"上证指数与深证成指成交额原值合计：{int(round(total))}。"
            "创业板指、沪深300、科创50、上证50没有加入合计，避免把包含关系里的成交额重复相加。"
        )
    else:
        lines.append("上证指数与深证成指的成交额原值没有同时通过简要行情比值核对，本次不写合计。")
    unchecked = [row["name"] for row in indices if not row.get("amount_unit_checked")]
    if unchecked:
        lines.append(
            "比值不在 9000 到 11000 的指数，本次不判断成交额单位："
            + "、".join(unchecked)
            + "。"
        )
    return lines


def _render_margin(payload: Mapping[str, Any]) -> list[str]:
    lines = ["", "融资余额按来源分开列出，不同日期的数字不相加。"]
    sse = payload.get("sse_margin")
    if sse:
        lines.append(
            f"上交所汇总：信用交易日期 {sse['date']}，字段 rzye={sse['rzye']}"
            + (f"，字段 rzmre={sse['rzmre']}" if sse.get("rzmre") is not None else "")
            + "。来源：https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
        )
    else:
        lines.append("上交所融资余额本次没有取到。")
    szse = payload.get("szse_margin")
    if szse:
        attempted = "、".join(szse.get("attempted_dates") or [])
        lines.append(
            f"深交所汇总：本次取到数据的查询日期 {szse['date']}，字段 jrrzye={szse['balance_raw']}，字段 jrrzmr={szse['buy_raw']}。"
            + (f"从行情日向前尝试的日期：{attempted}。" if attempted else "")
            + "来源：https://www.szse.cn/api/report/ShowReport/data?CATALOGID=1837_xxpl"
        )
        if szse.get("footer"):
            lines.append(f"深交所本次响应页脚：{szse['footer']}")
        lines.append("汇总单元格没有单独的单位字段。页脚里的“元”写在勾稽公式中，本次不把该公式单位套到 jrrzye 的数字上，也不做亿元换算。")
    else:
        lines.append("深交所融资余额本次没有取到。")
    em_margin = payload.get("em_margin")
    if em_margin:
        buy = f"，字段 FIN_BUY_AMT={_decimal(em_margin['fin_buy'], 2)}" if em_margin.get("fin_buy") is not None else ""
        lines.append(
            f"东方财富两融日报：统计日期 {em_margin['date']}，字段 FIN_BALANCE={_decimal(em_margin['fin_balance'], 2)}{buy}。"
            "来源：https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_MARGIN_DAILYTRADE"
        )
    else:
        lines.append("东方财富融资余额对照本次没有取到。")
    return lines


def _render_etf(etf: Mapping[str, Any]) -> list[str]:
    lines = ["", "ETF 只记录上交所份额字段原值，不记录单只基金名称。"]
    newer = etf.get("newer")
    older = etf.get("older")
    if not newer:
        lines.append("上交所 ETF 份额本次没有取到。")
        return lines
    lines.append(
        f"较新统计日 {newer.get('stat_date')}：TOT_VOL 原值合计 {_decimal(newer.get('sum_raw') or 0, 2)}，"
        f"返回 {newer.get('rows')} 行，接口 total={newer.get('reported_total')}，"
        f"行数与 total {'一致' if newer.get('complete') else '不一致'}。"
    )
    if older:
        lines.append(
            f"较早统计日 {older.get('stat_date')}：TOT_VOL 原值合计 {_decimal(older.get('sum_raw') or 0, 2)}，"
            f"返回 {older.get('rows')} 行，接口 total={older.get('reported_total')}，"
            f"行数与 total {'一致' if older.get('complete') else '不一致'}。"
        )
        if newer.get("complete") and older.get("complete"):
            delta = float(newer["sum_raw"]) - float(older["sum_raw"])
            lines.append(f"两日 TOT_VOL 原值合计差额：{_signed(delta, 2)}。")
    else:
        lines.append("没有取到第二个统计日，本次不计算份额差额。")
    lines.append("来源：https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L")
    return lines


def _render_breadth(breadth: Mapping[str, Any]) -> list[str]:
    if breadth.get("status") != "ok":
        return ["涨跌家数本次没有取到。"]
    return [
        (
            f"东方财富涨跌幅分布：分布日期 {breadth.get('qdate')}，"
            f"上涨 {breadth.get('up')} 家，下跌 {breadth.get('down')} 家，平盘 {breadth.get('flat')} 家，"
            f"分布合计 {breadth.get('total')} 家。"
        ),
        "来源：https://push2ex.eastmoney.com/getTopicZDFenBu",
    ]


def _render_limits(limit_up: Mapping[str, Any], limit_down: Mapping[str, Any]) -> list[str]:
    lines = [""]
    if limit_up.get("status") == "ok":
        lines.append(
            f"东方财富涨停池：日期 {limit_up.get('qdate')}，字段 tc={limit_up.get('count')}，"
            f"本次返回 {limit_up.get('returned')} 条。"
        )
        industries = limit_up.get("industries") or []
        if industries and limit_up.get("complete"):
            text = "、".join(f"{item['name']} {item['count']} 只" for item in industries)
            lines.append(f"涨停池返回记录中的行业出现次数前 5 名：{text}。")
        elif industries:
            lines.append("涨停池只返回了部分记录，本次不统计行业出现次数。")
    else:
        lines.append("涨停家数本次没有取到。")
    if limit_down.get("status") == "ok":
        lines.append(
            f"东方财富跌停池：日期 {limit_down.get('qdate')}，字段 tc={limit_down.get('count')}。"
        )
    else:
        lines.append("跌停家数本次没有取到。")
    lines.append("涨停池和跌停池的来源页面属于东方财富专题，简报不写其中的证券代码。")
    return lines


def _render_boards(title: str, board: Mapping[str, Any]) -> list[str]:
    lines = ["", title + "："]
    if board.get("status") != "ok":
        lines.append("本次没有取到。")
        return lines
    lines.append("涨幅前列：" + _board_text(board.get("top") or []))
    lines.append("跌幅前列：" + _board_text(board.get("bottom") or []))
    lines.append(f"来源主机：{board.get('host') or '东方财富板块列表'}")
    return lines


def _board_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "无"
    return "、".join(f"{row['name']} {_signed(row['change_pct'], 2)}%" for row in rows)


def _render_persistence(persistence: Mapping[str, Any]) -> list[str]:
    status = persistence.get("status")
    lines = ["", "热点持续性："]
    if status == "compared":
        overlap = persistence.get("overlap") or []
        lines.append(
            f"上一份概念涨幅前 5 名的行情日期是 {persistence.get('previous_date')}，"
            f"与本次前 5 名重合 {len(overlap)} 个"
            + (f"：{'、'.join(overlap)}。" if overlap else "。")
        )
    elif status == "same_session":
        lines.append("上一份状态与本次指数行情日期相同，本次不把重复运行写成新的持续性证据。")
    else:
        lines.append("没有可比较的上一交易日概念榜，本次不判断热点是否持续。")
    return lines


def _render_explanations(payload: Mapping[str, Any]) -> list[str]:
    lines = [
        "成交额字段：完整行情第 10 个字段与简要行情成交额字段的比值约等于 10000 时，"
        "前者按元、后者按万元记录。亿元换算是原值除以 100000000，只在比值核对通过后作为阅读单位。",
        "涨跌家数把分布接口中的正桶、负桶和 0 桶分别相加。接近 10% 或 20% 的桶仍是分布计数，不替代涨停池字段 tc。",
        "融资余额的交易所日期可以晚于指数行情日期。日期不同的上交所、深交所和东方财富数字保持并列。",
        "上交所 rzye 的 JSON 没有单位。公开汇总页长期把同类合计显示为元；按这个惯例，亿元等于 rzye 除以 100000000。这个换算写在解释中。",
        "深交所页脚用“元”描述余额勾稽公式。汇总行 jrrzye 仍是页面上的另一个数字，JSON 没有给出它自己的单位，所以保持原文字，不换成元或亿元。",
        "东方财富 FIN_BALANCE 沿用该数据中心页面的亿元展示惯例。它是对照值，不覆盖交易所原值。",
        "ETF 的 TOT_VOL 差额使用两日原值合计相减。AkShare 的 fund_etf_scale_sse 会把该字段乘以 10000 后称为份；"
        "本次 JSON 没有单位字段，所以“万份”只作为这条第三方处理的解释。差额的正负只表示份额合计升降。",
        "主力资金净流入不进入本简报的资金口径。板块涨跌幅也不解释成行业业绩或政策变化。",
        "标题关键词由固定词表归类，用来找线索。它不判断消息真假，也不判断影响大小。",
    ]
    checked = [row for row in payload.get("indices") or [] if row["code"] in TURNOVER_CODES and row.get("amount_unit_checked")]
    if len(checked) == 2:
        total_yi = sum(row["amount_yuan"] for row in checked) / 100000000
        lines.insert(1, f"按上述核对，本次上证指数与深证成指成交额合计可阅读为 {_decimal(total_yi, 2)} 亿元。")
    sse = payload.get("sse_margin")
    if sse:
        lines.insert(
            4,
            f"按元惯例，上交所 {sse['date']} 的 rzye 可阅读为 {_decimal(sse['rzye'] / 100000000, 2)} 亿元。",
        )
    return lines


def _render_unknowns(payload: Mapping[str, Any]) -> list[str]:
    lines = []
    for item in payload.get("errors") or []:
        lines.append(f"- {item['source']}：{item['error']}")
    headlines = payload.get("headlines") or {}
    for item in headlines.get("errors") or []:
        lines.append(f"- {item['source']}：{item['error']}")
    etf_errors = (payload.get("etf") or {}).get("errors") or []
    for item in etf_errors:
        lines.append(f"- 上交所ETF份额尝试：{item}")
    lines.extend(
        [
            "- 交易所官方全市场股票成交额：本次没有单独取得，指数成交额合计不能自动替代它。",
            "- 深市 ETF 份额和全市场申购赎回金额：本次没有取得。",
            "- 主力资金完整资金流：本次没有采集，也不用估算值填空。",
            "- 宏观、政策和业绩的正文、发布时间和原始文件页码：本次只有新闻标题。",
            "- 热点是否持续：没有同口径多日样本时保持未知；有一次名称重合也只是名单比较。",
            "- 模型对三面材料的综述：没有调用 LLM API。Google AI Pro 订阅没有被当作 API 密钥。",
        ]
    )
    presence = payload.get("key_presence") or {}
    configured = [name for name, present in presence.items() if present]
    if configured:
        lines.append("- 环境里存在非空凭证变量，本程序没有读取变量值，也没有用它们发起调用：" + "、".join(configured) + "。")
    else:
        lines.append("- 检查的 LLM、行情令牌和通知凭证变量均为空。")
    return lines


def load_state(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_brief(output_dir: str, http_get: HttpGet | None = None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    state_path = os.path.join(output_dir, "hot_concepts_state.json")
    previous = load_state(state_path)
    payload = collect_market(http_get=http_get, previous_state=previous, env=env)
    markdown = render_brief(payload)
    generated_day = str(payload["generated_at"])[:10]
    dated_path = os.path.join(output_dir, generated_day + ".md")
    latest_path = os.path.join(output_dir, "latest.md")
    coverage_path = os.path.join(output_dir, "latest.coverage.json")
    for path in (dated_path, latest_path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(markdown)
    coverage = _coverage_rows(payload)
    with open(coverage_path, "w", encoding="utf-8") as handle:
        json.dump({"generated_at": payload["generated_at"], "rows": coverage}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    names = [row["name"] for row in (payload.get("concepts") or {}).get("top") or []]
    if names and payload.get("session_date"):
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "concept_date": payload["session_date"],
                    "names": names,
                    "saved_at": payload["generated_at"],
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
    status = "written" if payload.get("indices") else "written_without_quotes"
    return {
        "status": status,
        "markdown": latest_path,
        "coverage": coverage_path,
    }
