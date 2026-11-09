# -*- coding: utf-8 -*-
"""Collect a keyless A-share learning brief and render facts, notes, and gaps.

This module does not import the notification stack, does not call an LLM, and
does not log in to a broker. A number is converted only when the same source
page states that number's unit. Field ratios are not used as unit evidence.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Mapping
from xml.etree import ElementTree as ET

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
    "https://82.push2.eastmoney.com",
    "https://33.push2.eastmoney.com",
    "https://79.push2.eastmoney.com",
    "https://17.push2.eastmoney.com",
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
)
SINA_BOARD_URLS = {
    "2": "http://money.finance.sina.com.cn/q/view/newFLJK.php?param=industry",
    "3": "http://money.finance.sina.com.cn/q/view/newFLJK.php?param=class",
}
SZSE_XLSX_URL = "https://www.szse.cn/api/report/ShowReport"
SSE_DEAL_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_MARGIN_SOA_URL = "https://query.sse.com.cn/commonSoaQuery.do"
SSE_OVERVIEW_JS = "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_stockData_2021.js"
SSE_MARGIN_JS = "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_otherData_2021.js"
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


def _maybe_decompress(raw: bytes, content_encoding: str = "") -> bytes:
    if "gzip" in content_encoding.lower() or raw.startswith(b"\x1f\x8b"):
        return gzip.decompress(raw)
    return raw


def default_http_get(url: str, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _maybe_decompress(response.read(), response.headers.get("Content-Encoding") or "")


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
                "amount_field": float(parts[9]),
                "quote_date": parts[30],
                "quote_time": parts[31],
            }
        )
    return rows


def merge_index_quotes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep both Sina fields. Their ratio is not evidence of a currency unit."""
    full = {row["code"]: row for row in rows if row["kind"] == "full"}
    summary = {row["code"]: row for row in rows if row["kind"] == "summary"}
    merged: list[dict[str, Any]] = []
    for code in INDEX_CODES:
        item = full.get(code)
        if item is None:
            continue
        brief = summary.get(code)
        merged.append({**item, "amount_summary": None if brief is None else brief.get("amount_summary")})
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


PBC_INDEX_URL = "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
PBC_ORIGIN = "http://www.pbc.gov.cn"
SSE_MARGIN_PAGE = "https://www.sse.com.cn/market/othersdata/margin/sum/"
EM_MARGIN_PAGE = "https://data.eastmoney.com/rzrq/zhtjday.html"
_PBC_KEEP = ("公告", "政策", "贷款", "报告")
_PBC_SKIP = ("会见", "出席", "英文", "English", "纪念币")


class _ZoomText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._active = 0
        self._depth = 0
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        if dict(attrs).get("id") == "zoom":
            self._active = 1
            self._depth = 1
            return
        if self._active and tag == "div":
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if self._active and tag == "div":
            self._depth -= 1
            if self._depth <= 0:
                self._active = 0

    def handle_data(self, data: str) -> None:
        if self._active and not self._skip:
            self.parts.append(data)


def extract_zoom_text(html: str) -> str:
    parser = _ZoomText()
    parser.feed(html)
    text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    for marker in ("打印本页", "法律声明", "关闭窗口"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text


def parse_pbc_links(html: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, title in re.findall(r'href="([^"]+)"[^>]*>([^<]{8,160})</a>', html):
        name = re.sub(r"\s+", " ", title).strip()
        if any(word in name for word in _PBC_SKIP):
            continue
        if not any(word in name for word in _PBC_KEEP):
            continue
        if href.startswith("/"):
            url = PBC_ORIGIN + href
        elif href.startswith("http://www.pbc.gov.cn/") or href.startswith("https://www.pbc.gov.cn/"):
            url = href
        else:
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append({"title": name, "url": url, "source": "中国人民银行"})

    def _rank(item: dict[str, str]) -> int:
        title = item["title"]
        if "公告" in title:
            return 0
        if "贷款" in title or "政策" in title:
            return 1
        if "报告" in title:
            return 2
        return 3

    found.sort(key=_rank)
    return found


def parse_pbc_article(html: str, url: str) -> dict[str, str] | None:
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
    date_match = re.search(r'name="createDate"\s+content="([^"]+)"', html, flags=re.I)
    body = extract_zoom_text(html)
    if title_match is None or len(body) < 60:
        return None
    return {
        "source": "中国人民银行",
        "kind": "原始页面",
        "title": re.sub(r"\s+", " ", title_match.group(1)).strip(),
        "published": date_match.group(1).strip() if date_match else "",
        "url": url,
        "text": body[:500],
    }


def extract_unit_phrases(html: str) -> list[str]:
    cleaned = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    cleaned = re.sub(r"<style[\s\S]*?</style>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    phrases: list[str] = []
    for match in re.finditer(r"[\u4e00-\u9fff]{0,8}单位[:：][^。；]{0,36}", cleaned):
        phrase = match.group(0).strip()
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return phrases[:4]


def unit_phrase_for(phrases: list[str], metric: str) -> str:
    for phrase in phrases:
        if metric in phrase:
            return phrase
    return ""


def amount_from_unit_header(header: str, raw_text: str, metric: str) -> dict[str, Any] | None:
    """Convert only when the same header names both the metric and the yuan unit."""
    if metric not in header or "(元)" not in header:
        return None
    try:
        yuan = float(str(raw_text).replace(",", "").strip())
    except ValueError:
        return None
    return {"header": header, "raw": str(raw_text).strip(), "yuan": yuan, "yi": yuan / 100000000}


def parse_xlsx_table(raw: bytes) -> list[list[str]]:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(raw)) as workbook:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in root.findall(ns + "si"):
                shared.append("".join(node.text or "" for node in item.iter(ns + "t")))
        sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in sheet.iter(ns + "row"):
        values: list[str] = []
        for cell in row.findall(ns + "c"):
            kind = cell.attrib.get("t")
            inline = cell.find(ns + "is")
            value = cell.find(ns + "v")
            if inline is not None:
                values.append("".join(node.text or "" for node in inline.iter(ns + "t")))
            elif kind == "s" and value is not None and value.text and value.text.isdigit():
                index = int(value.text)
                values.append(shared[index] if index < len(shared) else "")
            elif value is not None and value.text is not None:
                values.append(value.text)
            else:
                values.append("")
        if any(values):
            rows.append(values)
    return rows


def labeled_sheet_amount(rows: list[list[str]], row_name: str, metric: str) -> dict[str, Any] | None:
    if not rows:
        return None
    header = rows[0]
    column = next((index for index, name in enumerate(header) if metric in name and "(元)" in name), None)
    if column is None:
        return None
    for row in rows[1:]:
        if not row or row[0].strip() != row_name:
            continue
        if column >= len(row):
            return None
        parsed = amount_from_unit_header(header[column], row[column], metric)
        if parsed is None:
            return None
        parsed["row"] = row_name
        return parsed
    return None


def parse_sina_board(text: str) -> list[dict[str, Any]]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    payload = json.loads(text[start : end + 1])
    rows: list[dict[str, Any]] = []
    for value in payload.values():
        parts = str(value).split(",")
        if len(parts) < 6 or not parts[1].strip():
            continue
        try:
            change = float(parts[5])
        except ValueError:
            continue
        rows.append({"name": parts[1].strip(), "change_pct": change})
    return rows


def parse_sse_stock_trade_amt(payload: Mapping[str, Any]) -> dict[str, str] | None:
    for row in payload.get("result") or []:
        if str(row.get("PRODUCT_CODE") or "") == "17" and row.get("TRADE_AMT") not in (None, ""):
            return {"date": str(row.get("TRADE_DATE") or ""), "trade_amt": str(row.get("TRADE_AMT"))}
    return None


def sse_turnover_unit_from_script(script: str) -> str:
    """Official daily-overview script labels product 17 TRADE_AMT as 亿元."""
    if "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C" not in script:
        return ""
    if 'indexOf("17")' not in script:
        return ""
    if "v.TRADE_AMT" not in script or "成交金额(亿元)" not in script:
        return ""
    return "成交金额(亿元)"


def sse_margin_unit_from_script(script: str) -> str:
    """Official margin summary script prints rzye under 融资余额(元)."""
    if "RZRQ_HZ_INFO" not in script:
        return ""
    if "v.rzye" not in script or "融资余额(元)" not in script:
        return ""
    return "融资余额(元)"


def market_clock(generated: datetime, quote_date: str) -> dict[str, Any]:
    current = beijing_now(generated)
    minutes = current.hour * 60 + current.minute
    if current.isoweekday() >= 6:
        phase = "周末休市"
    elif minutes < 9 * 60 + 30:
        phase = "开盘前"
    elif minutes < 15 * 60:
        phase = "交易时段"
    else:
        phase = "收盘后"
    quote = quote_date or ""
    return {
        "generated_date": current.strftime("%Y-%m-%d"),
        "quote_date": quote,
        "phase": phase,
        "quote_is_generation_date": bool(quote) and quote == current.strftime("%Y-%m-%d"),
    }


def assess_completeness(payload: Mapping[str, Any]) -> dict[str, Any]:
    clock = payload.get("clock") or {}
    codes = {row.get("code") for row in payload.get("indices") or []}
    reasons: list[str] = []
    if payload.get("cache_used"):
        reasons.append("使用了过期缓存")
    if not {"sh000001", "sz399001"} <= codes:
        reasons.append("缺少上证或深证行情")
    if not clock.get("quote_is_generation_date"):
        reasons.append("行情日期不是生成日期")
    if clock.get("phase") != "收盘后":
        reasons.append("生成时不在收盘后")
    breadth_ok = (payload.get("breadth") or {}).get("status") == "ok"
    limits_ok = (payload.get("limit_up") or {}).get("status") == "ok" and (payload.get("limit_down") or {}).get("status") == "ok"
    if not breadth_ok or not limits_ok:
        reasons.append("涨跌家数或涨跌停不齐")
    if not payload.get("verified_docs"):
        reasons.append("没有核对到原始公告正文")
    if not (payload.get("szse_stock_turnover") or {}).get("header"):
        reasons.append("没有带表头单位的成交额")
    industries = payload.get("industries") or {}
    concepts = payload.get("concepts") or {}
    if industries.get("status") != "ok" or concepts.get("status") != "ok":
        reasons.append("行业或概念涨跌榜缺失")
    if (payload.get("persistence") or {}).get("status") != "compared":
        reasons.append("没有上一交易日同口径热点名单")
    return {"complete": not reasons, "reasons": reasons, "cache_used": bool(payload.get("cache_used"))}


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
            "ETF份额字段",
            "份额字段记录份额规模。两个日期的原值相减，只说明份额合计变了。"
            "原值没有货币单位时，不能把差额写成金额。",
        ),
        6: (
            "行情日期和发布日期",
            "周末或休市时打开行情，接口仍可能返回最近一个交易日的收盘数据。"
            "简报里的生成时间、行情日期和融资或份额的统计日期要分开写。",
        ),
        7: (
            "事实、解释和未知",
            "事实保留接口里的原值、日期和来源。单位以同一来源写明的文字为准。"
            "取不到、口径没有写明，或只有标题没有正文的内容，写入未知。",
        ),
    }
    title, body = lessons[beijing_now(current).isoweekday()]
    return {"title": title, "body": body}


def concept_persistence(
    previous: Mapping[str, Any] | None,
    names: list[str],
    session_date: str,
    source: str = "",
) -> dict[str, Any]:
    if not names:
        return {"status": "missing", "overlap": [], "previous_date": "", "source": source}
    if not previous or not previous.get("names"):
        return {"status": "unknown", "overlap": [], "previous_date": "", "source": source}
    previous_source = str(previous.get("source") or "")
    if not previous_source or previous_source != source:
        return {
            "status": "different_source",
            "overlap": [],
            "previous_date": str(previous.get("concept_date") or ""),
            "source": source,
        }
    previous_date = str(previous.get("concept_date") or "")
    overlap = [name for name in names if name in set(previous.get("names") or [])]
    if previous_date == session_date:
        return {"status": "same_session", "overlap": overlap, "previous_date": previous_date, "source": source}
    return {"status": "compared", "overlap": overlap, "previous_date": previous_date, "source": source}


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


def _rank_board(rows: list[dict[str, Any]], limit: int = 5) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda item: item["change_pct"])
    return list(reversed(ordered[-limit:])), ordered[:limit]


def _fetch_sina_board(http_get: HttpGet, board_type: str) -> dict[str, Any]:
    url = SINA_BOARD_URLS[board_type]
    text = http_get(url, _headers("http://finance.sina.com.cn/stock/sl/"), 20).decode("gb18030", "replace")
    rows = parse_sina_board(text)
    if not rows or not _board_has_movement(rows):
        return {"status": "missing", "error": "新浪板块涨跌幅为空或全为 0，不作为收盘榜", "top": [], "bottom": []}
    top, bottom = _rank_board(rows)
    return {
        "status": "ok",
        "host": url,
        "source": "新浪板块",
        "top": top,
        "bottom": bottom,
        "note": "数值是新浪板块字符串第6段。板块页列名有“涨跌幅”，数据响应本身没有表头。",
    }


def limit_pool_date_ok(returned: str, requested: str) -> bool:
    if requested and returned and returned != requested:
        return False
    return True


def _board_has_movement(rows: list[dict[str, Any]]) -> bool:
    return any(abs(float(row.get("change_pct") or 0)) > 0 for row in rows)


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
            if (top or bottom) and _board_has_movement([*top, *bottom]):
                return {"status": "ok", "host": host, "source": "东方财富板块", "top": top, "bottom": bottom}
            if top or bottom:
                last_error = f"{host} 涨跌幅全部为 0，不作为收盘榜"
            else:
                last_error = f"{host} returned no rows"
        except Exception as exc:  # noqa: BLE001
            last_error = _error_text(exc)
    try:
        fallback = _fetch_sina_board(http_get, board_type)
    except Exception as exc:  # noqa: BLE001
        fallback = {"status": "missing", "error": last_error + "；新浪：" + _error_text(exc), "top": [], "bottom": []}
    if fallback.get("status") != "ok":
        fallback["error"] = last_error if not fallback.get("error") else str(fallback["error"])
    return fallback


def _szse_xlsx(http_get: HttpGet, catalog: str, date_key: str, day: str) -> list[list[str]]:
    params = {"SHOWTYPE": "xlsx", "CATALOGID": catalog, "TABKEY": "tab1", date_key: day, "random": "0.2"}
    url = SZSE_XLSX_URL + "?" + urllib.parse.urlencode(params)
    return parse_xlsx_table(http_get(url, _headers("https://www.szse.cn/"), 25))


def _fetch_szse_labeled_amounts(http_get: HttpGet, session_date: str) -> dict[str, Any]:
    start = datetime.strptime(session_date, "%Y-%m-%d") if session_date else beijing_now()
    last_error = ""
    attempted: list[str] = []
    for offset in range(0, 8):
        day = (start - timedelta(days=offset)).strftime("%Y-%m-%d")
        attempted.append(day)
        rows = None
        for _retry in range(2):
            try:
                rows = _szse_xlsx(http_get, "1803_sczm", "txtQueryDate", day)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = _error_text(exc)
                rows = None
        if rows is None:
            continue
        stock = labeled_sheet_amount(rows, "股票", "成交金额")
        etf = labeled_sheet_amount(rows, "ETF", "成交金额")
        if stock or etf:
            return {"date": day, "stock": stock, "etf": etf, "attempted_dates": attempted, "error": ""}
    return {"date": "", "stock": None, "etf": None, "attempted_dates": attempted, "error": last_error}


def _fetch_szse_margin_xlsx(http_get: HttpGet, session_date: str) -> dict[str, Any] | None:
    start = datetime.strptime(session_date, "%Y-%m-%d") if session_date else beijing_now()
    last_error = ""
    attempted = []
    for offset in range(0, 8):
        day = (start - timedelta(days=offset)).strftime("%Y-%m-%d")
        attempted.append(day)
        try:
            rows = _szse_xlsx(http_get, "1837_xxpl", "txtDate", day)
        except Exception as exc:  # noqa: BLE001
            last_error = _error_text(exc)
            continue
        if len(rows) < 2:
            continue
        parsed = None
        for index, name in enumerate(rows[0]):
            if index < len(rows[1]):
                parsed = amount_from_unit_header(name, rows[1][index], "融资余额")
            if parsed is not None:
                break
        if parsed is None:
            continue
        parsed["date"] = day
        parsed["attempted_dates"] = attempted
        parsed["source"] = "深交所xlsx"
        return parsed
    if last_error:
        raise RuntimeError(last_error)
    return None


def _fetch_sse_trade_amt(http_get: HttpGet, session_date: str) -> dict[str, str] | None:
    start = datetime.strptime(session_date, "%Y-%m-%d") if session_date else beijing_now()
    for offset in range(0, 8):
        day = (start - timedelta(days=offset)).strftime("%Y-%m-%d")
        params = {
            "sqlId": "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C",
            "PRODUCT_CODE": "01,02,03,11,17",
            "type": "inParams",
            "SEARCH_DATE": day,
        }
        url = SSE_DEAL_URL + "?" + urllib.parse.urlencode(params)
        parsed = parse_sse_stock_trade_amt(_get_json(http_get, url, _headers("https://www.sse.com.cn/")))
        if parsed and parsed.get("trade_amt") not in (None, "", "0", "0.0"):
            parsed["searched_date"] = day
            return parsed
    return None


def _fetch_pool(http_get: HttpGet, url: str, session_date: str) -> dict[str, Any]:
    dated = url + (("&date=" + session_date.replace("-", "")) if session_date else "")
    payload = _get_json(http_get, dated or url, _headers("https://quote.eastmoney.com/ztb/"))
    parsed = parse_limit_pool(payload)
    if parsed["count"] == 0 and session_date:
        payload = _get_json(http_get, url, _headers("https://quote.eastmoney.com/ztb/"))
        parsed = parse_limit_pool(payload)
    requested = session_date.replace("-", "") if session_date else ""
    returned = str(parsed.get("qdate") or "")
    if not limit_pool_date_ok(returned, requested):
        return {
            "status": "missing",
            "count": None,
            "qdate": returned,
            "industries": [],
            "error": f"返回日期 {returned} 与行情日 {requested} 不一致",
        }
    parsed["status"] = "ok" if parsed["qdate"] or parsed["count"] else "missing"
    return parsed


def _fetch_sse_margin(http_get: HttpGet) -> dict[str, Any] | None:
    today = beijing_now().strftime("%Y%m%d")
    start = (beijing_now() - timedelta(days=20)).strftime("%Y%m%d")
    params = {
        "isPagination": "true",
        "pageHelp.pageSize": "25",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        "stockCode": "",
        "beginDate": start,
        "endDate": today,
        "sqlId": "RZRQ_HZ_INFO",
    }
    url = SSE_MARGIN_SOA_URL + "?" + urllib.parse.urlencode(params)
    parsed = parse_sse_margin(_get_json(http_get, url, _headers("https://www.sse.com.cn/market/othersdata/margin/sum/")))
    return parsed


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


def _decode_html(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def accept_official_article(article: Mapping[str, str]) -> bool:
    blob = str(article.get("title") or "") + str(article.get("text") or "")[:160]
    return "纪念币" not in blob


def _fetch_verified_docs(http_get: HttpGet) -> list[dict[str, str]]:
    index_raw = http_get(PBC_INDEX_URL, _headers(PBC_ORIGIN + "/"), 20)
    links = parse_pbc_links(_decode_html(index_raw))
    docs: list[dict[str, str]] = []
    for link in links[:6]:
        if len(docs) >= 2:
            break
        article = parse_pbc_article(_decode_html(http_get(link["url"], _headers(PBC_INDEX_URL), 20)), link["url"])
        if article and accept_official_article(article):
            docs.append(article)
    if not docs:
        raise RuntimeError("没有得到可核对的原始页面正文")
    return docs


def _fetch_unit_phrases(http_get: HttpGet, url: str, referer: str) -> list[str]:
    return extract_unit_phrases(_decode_html(http_get(url, _headers(referer), 20)))


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
    clock = market_clock(current, session_date)
    after_close = clock.get("phase") == "收盘后" and clock.get("quote_is_generation_date")

    breadth = None
    limit_up = None
    limit_down = None
    deferred = {
        "status": "deferred",
        "reason": "未到收盘，或行情日不是生成日。涨跌家数和涨跌停留到收盘后再运行，不采用盘中或开盘前的零值。",
    }
    breadth_parsed = None
    if after_close:
        breadth = _safe_call(
            "东方财富涨跌分布",
            lambda: _get_json(getter, FENBU_URL, _headers("https://quote.eastmoney.com/")),
            errors,
        )
        if isinstance(breadth, dict):
            data = breadth.get("data") or {}
            if data.get("fenbu"):
                counted = summarize_fenbu(data.get("fenbu") or [])
                qdate = str(data.get("qdate") or "")
                if counted["total"] > 0 and limit_pool_date_ok(qdate, session_date.replace("-", "")):
                    breadth_parsed = {"status": "ok", "qdate": qdate, **counted}
                elif counted["total"] == 0:
                    errors.append({"source": "东方财富涨跌分布", "error": "分布合计为 0，不作为有效涨跌家数"})
                else:
                    errors.append({"source": "东方财富涨跌分布", "error": f"返回日期 {qdate} 与行情日不一致"})
        limit_up = _safe_call("东方财富涨停池", lambda: _fetch_pool(getter, ZT_URL, session_date), errors)
        limit_down = _safe_call("东方财富跌停池", lambda: _fetch_pool(getter, DT_URL, session_date), errors)
    else:
        breadth_parsed = deferred
        limit_up = deferred
        limit_down = deferred
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
    szse_margin = _safe_call("深交所融资余额表", lambda: _fetch_szse_margin_xlsx(getter, session_date), errors)
    if not szse_margin:
        szse_margin = _safe_call("深交所融资融券汇总", lambda: _fetch_szse_margin(getter, session_date), errors)
    szse_amounts = _safe_call("深交所证券类别统计", lambda: _fetch_szse_labeled_amounts(getter, session_date), errors) or {}
    sse_trade_amt = _safe_call("上交所股票成交概况", lambda: _fetch_sse_trade_amt(getter, session_date), errors)
    overview_script = _safe_call(
        "上交所每日概况脚本",
        lambda: getter(SSE_OVERVIEW_JS, _headers("https://www.sse.com.cn/market/stockdata/overview/day/"), 20).decode("utf-8", "replace"),
        errors,
    ) or ""
    margin_script = _safe_call(
        "上交所融资融券脚本",
        lambda: getter(SSE_MARGIN_JS, _headers("https://www.sse.com.cn/market/othersdata/margin/sum/"), 20).decode("utf-8", "replace"),
        errors,
    ) or ""
    if sse_trade_amt:
        sse_trade_amt = {**sse_trade_amt, "unit_label": sse_turnover_unit_from_script(overview_script)}
    if sse_margin:
        sse_margin = {**sse_margin, "unit_label": sse_margin_unit_from_script(margin_script)}
    em_margin = _safe_call("东方财富融资融券汇总", lambda: _fetch_em_margin(getter), errors)
    etf = _safe_call("上交所ETF份额", lambda: _fetch_etf_pair(getter, session_date), errors) or {
        "status": "missing",
        "newer": None,
        "older": None,
        "errors": [],
    }
    verified_docs = _safe_call("中国人民银行原始页面", lambda: _fetch_verified_docs(getter), errors) or []
    sse_unit_phrases = _safe_call(
        "上交所融资页面单位语句",
        lambda: _fetch_unit_phrases(getter, SSE_MARGIN_PAGE, "https://www.sse.com.cn/"),
        errors,
    ) or []
    em_unit_phrases = _safe_call(
        "东方财富融资页面单位语句",
        lambda: _fetch_unit_phrases(getter, EM_MARGIN_PAGE, "https://data.eastmoney.com/"),
        errors,
    ) or []
    if sse_margin:
        sse_margin = {**sse_margin, "unit_phrases": sse_unit_phrases}
    if em_margin:
        em_margin = {**em_margin, "unit_phrases": em_unit_phrases}
    concept_names = [row["name"] for row in concepts.get("top") or []]
    for label, board in (("东方财富行业板块", industries), ("东方财富概念板块", concepts)):
        if board.get("status") != "ok" and board.get("error"):
            errors.append({"source": label, "error": board["error"]})
    for label, pool in (("涨停池", limit_up), ("跌停池", limit_down)):
        if isinstance(pool, dict) and pool.get("status") != "ok" and pool.get("error"):
            errors.append({"source": label, "error": pool["error"]})
    payload = {
        "generated_at": format_stamp(current),
        "session_date": session_date,
        "clock": clock,
        "cache_used": False,
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
        "persistence": concept_persistence(
            previous_state,
            concept_names,
            session_date,
            str((concepts or {}).get("source") or ""),
        ),
        "sse_margin": sse_margin,
        "szse_margin": szse_margin,
        "em_margin": em_margin,
        "szse_stock_turnover": szse_amounts.get("stock"),
        "szse_etf_turnover": szse_amounts.get("etf"),
        "szse_amount_date": szse_amounts.get("date") or "",
        "szse_amount_attempted": szse_amounts.get("attempted_dates") or [],
        "sse_trade_amt": sse_trade_amt,
        "sse_unit_phrases": sse_unit_phrases,
        "em_unit_phrases": em_unit_phrases,
        "etf": etf,
        "verified_docs": verified_docs,
        "lesson": concept_for_date(current),
        "errors": errors,
    }
    payload["completeness"] = assess_completeness(payload)
    return payload



def _view(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    if not data.get("clock"):
        generated = datetime.strptime(str(data.get("generated_at") or "2026-11-08 21:00:00"), "%Y-%m-%d %H:%M:%S")
        data["clock"] = market_clock(generated.replace(tzinfo=BJ_TZ), str(data.get("session_date") or ""))
    data.setdefault("verified_docs", [])
    data.setdefault("cache_used", False)
    data.setdefault("sse_unit_phrases", (data.get("sse_margin") or {}).get("unit_phrases") or [])
    data.setdefault("em_unit_phrases", (data.get("em_margin") or {}).get("unit_phrases") or [])
    data["completeness"] = assess_completeness(data)
    return data


def _coverage_rows(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    data = _view(payload)
    indices = data.get("indices") or []
    core = [row for row in indices if row.get("code") in TURNOVER_CODES]
    sse = data.get("sse_margin")
    szse = data.get("szse_margin")
    em_margin = data.get("em_margin")
    etf = data.get("etf") or {}
    docs = data.get("verified_docs") or []
    breadth = data.get("breadth") or {}
    limit_up = data.get("limit_up") or {}
    limit_down = data.get("limit_down") or {}
    industries = data.get("industries") or {}
    concepts = data.get("concepts") or {}
    persistence = data.get("persistence") or {}
    clock = data.get("clock") or {}
    completeness = data.get("completeness") or {}
    return [
        _coverage("基本面", "原始公告正文", "已核对" if docs else "缺失", "、".join(item.get("published") or "无日期" for item in docs) or "无日期", "中国人民银行页面正文", "未打开正文的标题不进入基本面"),
        _coverage("资金面", "上证与深证成交额原值", "已取得" if len(core) == 2 else "缺失", str(data.get("session_date") or "无日期"), "新浪财经行情字段", "响应没有单位说明，不换算，也不把两个字段的比值当作单位"),
        _coverage("资金面", "上交所融资余额原值", "已取得" if sse else "缺失", str((sse or {}).get("date") or "无日期"), "queryMargin.do 字段 rzye", "只有页面语句同时写明融资余额和单位时才采用该单位"),
        _coverage("资金面", "深交所融资余额原值", "已取得" if szse else "缺失", str((szse or {}).get("date") or "无日期"), "深交所汇总字段 jrrzye", "页脚公式中的单位不套到汇总单元格"),
        _coverage("资金面", "东方财富融资对照原值", "已取得" if em_margin else "缺失", str((em_margin or {}).get("date") or "无日期"), "RPTA_WEB_MARGIN_DAILYTRADE 字段 FIN_BALANCE", "页面没有同时写明该字段和单位时不换算"),
        _coverage("资金面", "上交所ETF份额原值差额", "部分" if (etf.get("newer") or {}).get("complete") else "缺失", str((etf.get("newer") or {}).get("stat_date") or "无日期"), "上交所 TOT_VOL", "差额不是金额；没有深市份额"),
        _coverage("情绪面", "涨跌家数", "已取得" if breadth.get("status") == "ok" else "缺失", str(breadth.get("qdate") or "无日期"), "东方财富涨跌幅分布", "分布桶相加，不是涨跌停家数"),
        _coverage("情绪面", "涨停与跌停家数", "已取得" if limit_up.get("status") == "ok" and limit_down.get("status") == "ok" else "部分" if limit_up.get("status") == "ok" or limit_down.get("status") == "ok" else "缺失", str(limit_up.get("qdate") or limit_down.get("qdate") or "无日期"), "东方财富涨停池和跌停池字段 tc", "不列证券代码"),
        _coverage("情绪面", "行业与概念涨跌榜", "已取得" if industries.get("status") == "ok" and concepts.get("status") == "ok" else "部分" if industries.get("status") == "ok" or concepts.get("status") == "ok" else "缺失", str(data.get("session_date") or "无日期"), "东方财富板块列表", "价格涨跌幅，不是业绩或政策"),
        _coverage("情绪面", "热点持续性", "已比较" if persistence.get("status") == "compared" else "缺失", str(data.get("session_date") or "无日期"), "上一份概念名称", "没有上一交易日名单时保持未知"),
        _coverage("日期", "生成日与行情日", "一致" if clock.get("quote_is_generation_date") else "不一致", f"{clock.get('generated_date') or '无日期'} / {clock.get('quote_date') or '无日期'}", "本机北京时间与行情字段", clock.get("phase") or "无时段"),
        _coverage("完整性", "当日完整简报", "通过" if completeness.get("complete") else "未通过", str(data.get("generated_at") or "无日期"), "本次现场请求", "；".join(completeness.get("reasons") or []) or "无"),
        _coverage("解释", "模型综述", "未知", str(data.get("generated_at") or "无日期"), "未调用模型", "没有使用 LLM API 密钥"),
    ]


def _coverage(dimension: str, metric: str, status: str, as_of: str, source: str, gap: str) -> dict[str, str]:
    return {"dimension": dimension, "metric": metric, "status": status, "as_of": as_of or "无日期", "source": source, "gap": gap}


def _prepare(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _view(payload)


def render_brief(payload: Mapping[str, Any]) -> str:
    data = _prepare(payload)
    clock = data["clock"]
    completeness = data["completeness"]
    lines = [
        "# A股学习简报",
        "",
        f"- 生成时间：{data.get('generated_at')}（北京时间）",
        f"- 指数行情日期：{data.get('session_date') or '未知'}",
        f"- 时段：{clock.get('phase')}",
        "- 状态：完整简报" if completeness.get("complete") else "- 状态：不是完整简报。原因：" + "；".join(completeness.get("reasons") or ["资料不足"]),
        "- 来源：本次现场请求。没有把上一份简报中的数字写进来。",
        "- 模型综述：未知。本次没有调用 LLM。",
        "",
        "## 1. 事实",
        "",
        "### 1.1 基本面",
        "",
    ]
    docs = data.get("verified_docs") or []
    if docs:
        lines.append(f"下面 {len(docs)} 份材料来自已打开的原始页面正文，不是按标题关键词挑选的新闻列表。")
        lines.append("")
        for index, item in enumerate(docs, start=1):
            lines.append(f"{index}. {item.get('source')}，发布时间 {item.get('published') or '未知'}：{item.get('title')}")
            lines.append(f"   链接：{item.get('url')}")
            body = str(item.get("text") or "")
            excerpt = body[:180] + ("……（其余正文见 latest.evidence.md）" if len(body) > 180 else "")
            lines.append(f"   正文摘录：{excerpt}")
            lines.append("")
    else:
        lines.append("本次没有打开可核对的原始公告正文。基本面保持未知。")
        lines.append("")
    lines.extend(_render_price_facts(data))
    lines.extend(_render_margin_facts(data))
    lines.extend(_render_etf_facts(data))
    lines.extend(_render_sentiment_facts(data))
    lines.extend(["", "## 2. 解释", ""])
    lines.extend(
        [
            "表头写“(元)”时，原值除以 100000000 读成亿元。表头写“(亿元)”时，数字本身就是亿元，不再除以 1 亿。没有表头的字段保持原值。",
            "融资余额按各自日期并列。日期不同的数字不相加。",
            "涨跌家数是分布桶里的计数相加。涨停和跌停以专题池的 tc 为准。",
            "行业和概念涨跌幅是价格变动。它们不说明业绩或政策。",
            "热点名称重合只比较榜单名称。",
            "",
            "## 3. 未知",
            "",
        ]
    )
    for item in data.get("errors") or []:
        lines.append(f"- {item['source']}：{item['error']}")
    for item in (data.get("etf") or {}).get("errors") or []:
        lines.append(f"- 上交所ETF份额字段：{item}")
    lines.extend(
        [
            "- 没有表头原文的字段不换算。新浪指数成交额字段属于这类。",
            "- 深市 ETF 份额，以及申购或赎回的货币金额，本次没有取得。深交所 ETF 行只表示成交金额。",
            "- 主力资金流没有采集。",
            "- 热点是否持续：没有同口径多日样本时保持未知。",
            "- 模型综述保持未知。",
        ]
    )
    presence = data.get("key_presence") or {}
    configured = [name for name, present in presence.items() if present]
    if configured:
        lines.append("- 环境中有非空凭证变量。本程序没有读取变量值，也没有用它们发起调用：" + "、".join(configured) + "。")
    else:
        lines.append("- 检查过的 LLM、行情令牌和通知凭证变量都是空的。")
    lesson = data.get("lesson") or {}
    lines.extend(
        [
            "",
            "## 4. 今日入门概念",
            "",
            f"概念：{lesson.get('title') or '未知'}",
            "",
            "这一节只解释字段，不新增行情数字。",
            "",
            lesson.get("body") or "",
            "",
            "## 5. 已有 16:30 任务如何读取",
            "",
            "工作日 16:30（北京时间）的任务保持不变。它读取同一次运行写出的 `latest.md`。",
            "文件写明「不是完整简报」或「本次没有形成简报」时，消息只转述这个状态，不用上一份文件补数字。",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_price_facts(data: Mapping[str, Any]) -> list[str]:
    lines = ["### 1.2 资金面", ""]
    stock = data.get("szse_stock_turnover") or {}
    if stock.get("header"):
        lines.append(
            f"深交所股票成交金额：日期 {data.get('szse_amount_date')}，表头原文“{stock['header']}”，"
            f"数值 {stock['raw']} 元。按该表头换算为 {_decimal(stock['yi'], 2)} 亿元。"
            + (
                f"尝试日期：{'、'.join(data.get('szse_amount_attempted') or [])}。"
                if data.get("szse_amount_attempted")
                else ""
            )
        )
    else:
        lines.append("深交所带表头的股票成交金额本次没有取到。")
    sse_amt = data.get("sse_trade_amt") or {}
    if sse_amt.get("trade_amt") and sse_amt.get("unit_label") == "成交金额(亿元)":
        lines.append(
            f"上交所股票成交金额：日期 {sse_amt.get('date')}，产品代码 17，字段 TRADE_AMT={sse_amt['trade_amt']}。"
            "每日概况脚本把该字段写入表头“成交金额(亿元)”。按该表头，这个数字的单位是亿元。"
        )
    elif sse_amt.get("trade_amt"):
        lines.append(
            f"上交所股票成交概况：日期 {sse_amt.get('date')}，产品代码 17，字段 TRADE_AMT={sse_amt['trade_amt']}。"
            "没有取到配套的表头原文，不换算。"
        )
    indices = [row for row in data.get("indices") or [] if row.get("code") in TURNOVER_CODES]
    if not indices:
        lines.append("上证指数和深证成指本次没有取到。")
        lines.append("")
        return lines
    pre_close = (data.get("clock") or {}).get("phase") != "收盘后"
    if pre_close:
        lines.append("当前不是收盘后。下面的指数快照不是收盘定稿；点位或成交额为 0 时，不计算涨跌幅。请于收盘后再运行。")
    for row in indices:
        if pre_close and (float(row.get("current") or 0) == 0 or float(row.get("amount_field") or 0) == 0):
            lines.append(
                f"{row['name']} 行情时间 {row.get('quote_date')} {row.get('quote_time')}，"
                f"点位原值 {row.get('current')}，成交额字段原值 {int(round(float(row.get('amount_field') or 0)))}。"
                "这是盘前或盘中空值，不换算，也不当成跌停。"
            )
            continue
        lines.append(
            f"{row['name']} { _decimal(row['current'], 2) } 点，涨跌 {_signed(row['change'], 2)} 点，"
            f"涨跌幅 {_signed(row['change_pct'], 2)}%。行情时间 {row.get('quote_date')} {row.get('quote_time')}。"
        )
        lines.append(
            f"成交额字段原值 {int(round(row['amount_field']))}。来源没有写出这个字段的单位，本次不换算。"
        )
    lines.append("上证与深证的成交额原值不相加，也不换算。")
    lines.append("")
    return lines


def _render_margin_facts(data: Mapping[str, Any]) -> list[str]:
    lines = []
    sse = data.get("sse_margin")
    if sse and sse.get("unit_label") == "融资余额(元)":
        lines.append(
            f"上交所融资余额：日期 {sse['date']}，字段 rzye={sse['rzye']}。"
            "融资融券汇总脚本把 rzye 放在表头“融资余额(元)”下。"
            f"按该表头换算为 {_decimal(sse['rzye'] / 100000000, 2)} 亿元。"
        )
    elif sse:
        lines.append(f"上交所融资余额：日期 {sse['date']}，字段 rzye={sse['rzye']}。")
        lines.append("没有取到“融资余额(元)”表头原文。rzye 的单位未知，不换算。")
    else:
        lines.append("上交所融资余额本次没有取到。")
    szse = data.get("szse_margin")
    if szse and szse.get("header"):
        attempted = "、".join(szse.get("attempted_dates") or [])
        lines.append(
            f"深交所融资余额：日期 {szse.get('date')}，表头原文“{szse['header']}”，数值 {szse['raw']} 元。"
            f"按该表头换算为 {_decimal(szse['yi'], 2)} 亿元。"
            + (f"尝试日期：{attempted}。" if attempted else "")
        )
    elif szse:
        attempted = "、".join(szse.get("attempted_dates") or [])
        lines.append(
            f"深交所融资余额：查询日期 {szse['date']}，字段 jrrzye={szse['balance_raw']}。"
            + (f"尝试日期：{attempted}。" if attempted else "")
        )
        lines.append("不把该公式单位套到 jrrzye。汇总单元格的单位未知，不换算。")
    else:
        lines.append("深交所融资余额本次没有取到。")
    em_margin = data.get("em_margin")
    if em_margin:
        phrase = unit_phrase_for(data.get("em_unit_phrases") or [], "融资余额")
        lines.append(f"东方财富融资对照：日期 {em_margin['date']}，字段 FIN_BALANCE={_decimal(em_margin['fin_balance'], 2)}。")
        if phrase:
            lines.append(f"页面单位原文：{phrase}")
        else:
            lines.append("页面没有同时写明 FIN_BALANCE 的单位。这个原值不换算。")
    else:
        lines.append("东方财富融资对照本次没有取到。")
    lines.append("")
    return lines


def _render_etf_facts(data: Mapping[str, Any]) -> list[str]:
    etf_turnover = data.get("szse_etf_turnover") or {}
    lines: list[str] = []
    if etf_turnover.get("header"):
        lines.append(
            f"深交所 ETF 成交金额：日期 {data.get('szse_amount_date')}，表头原文“{etf_turnover['header']}”，"
            f"数值 {etf_turnover['raw']} 元。按该表头换算为 {_decimal(etf_turnover['yi'], 2)} 亿元。"
            "这是成交金额，不是份额变化。"
        )
    else:
        lines.append("深交所 ETF 成交金额本次没有取到。深市份额变化保持未知。")
    etf = data.get("etf") or {}
    newer = etf.get("newer")
    older = etf.get("older")
    if not newer:
        lines.extend(["上交所 ETF 份额原值本次没有取到。", ""])
        return lines
    lines.append(
        f"上交所 TOT_VOL 原值合计：{newer.get('stat_date')} 为 {_decimal(float(newer.get('sum_raw') or 0), 2)}，"
        f"共 {newer.get('rows')} 行，接口 total={newer.get('reported_total')}。"
    )
    if older and newer.get("complete") and older.get("complete"):
        delta = float(newer["sum_raw"]) - float(older["sum_raw"])
        lines.append(
            f"{older.get('stat_date')} 的原值合计为 {_decimal(float(older.get('sum_raw') or 0), 2)}。"
            f"两日原值差额 {_signed(delta, 2)}。JSON 没有单位。这个差额不能换成金额。"
        )
    else:
        lines.append("没有两个完整统计日，本次不计算原值差额。")
    lines.append("")
    return lines


def _render_sentiment_facts(data: Mapping[str, Any]) -> list[str]:
    lines = ["### 1.3 情绪面", ""]
    breadth = data.get("breadth") or {}
    limit_up = data.get("limit_up") or {}
    deferred = breadth.get("status") == "deferred" or limit_up.get("status") == "deferred"
    if deferred:
        lines.append(
            "涨跌家数、涨停和跌停需要收盘后、且返回日期等于行情日。"
            "当前不是这个条件，本次不采集，也不把盘中或开盘前的零值写成完整结果。请于收盘后再运行。"
        )
    elif breadth.get("status") == "ok":
        lines.append(
            f"涨跌分布日期 {breadth.get('qdate')}：上涨 {breadth.get('up')} 家，下跌 {breadth.get('down')} 家，"
            f"平盘 {breadth.get('flat')} 家，合计 {breadth.get('total')} 家。"
        )
    elif not deferred:
        lines.append("涨跌家数本次没有取到。")
    limit_down = data.get("limit_down") or {}
    if deferred:
        pass
    elif limit_up.get("status") == "ok":
        lines.append(f"涨停池日期 {limit_up.get('qdate')}，字段 tc={limit_up.get('count')}。")
        if limit_up.get("complete") and limit_up.get("industries"):
            text = "、".join(f"{item['name']} {item['count']} 只" for item in limit_up["industries"][:3])
            lines.append(f"涨停池行业出现次数前 3 名：{text}。")
    elif not deferred:
        lines.append("涨停家数本次没有取到。")
    if deferred:
        pass
    elif limit_down.get("status") == "ok":
        lines.append(f"跌停池日期 {limit_down.get('qdate')}，字段 tc={limit_down.get('count')}。")
    else:
        lines.append("跌停家数本次没有取到。")
    if not deferred:
        lines.append("涨停池和跌停池不列证券代码。")
    for title, board in (("行业", data.get("industries") or {}), ("概念", data.get("concepts") or {})):
        if board.get("status") == "ok":
            source = board.get("source") or "板块列表"
            lines.append(f"{title}涨幅前 3 名（{source}）：" + _board_text((board.get("top") or [])[:3]))
            lines.append(f"{title}跌幅前 3 名：" + _board_text((board.get("bottom") or [])[:3]))
            if board.get("note"):
                lines.append(board["note"])
        else:
            lines.append(f"{title}涨跌榜本次没有取到。")
    persistence = data.get("persistence") or {}
    if persistence.get("status") == "compared":
        overlap = persistence.get("overlap") or []
        lines.append(f"与上一份概念名称相比，重合 {len(overlap)} 个。这只是名称比较。")
    elif persistence.get("status") == "same_session":
        lines.append("上一份概念名称与本次行情日期相同，不作为新的持续性证据。")
    elif persistence.get("status") == "different_source":
        lines.append("上一份概念名单来源不同，不和本次名单比较。")
    elif (data.get("concepts") or {}).get("status") == "ok":
        lines.append("没有可比较的上一交易日同口径概念榜。本次名单会按来源和行情日保存，供下一次比较。")
    else:
        lines.append("本次没有可用的概念涨跌榜，所以没有写入收盘快照。收盘后名单出现涨跌时，会按来源和行情日保存。")
    return lines


def _board_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "无"
    return "、".join(f"{row['name']} {_signed(row['change_pct'], 2)}%" for row in rows)


def render_evidence(payload: Mapping[str, Any]) -> str:
    data = _prepare(payload)
    lines = ["# 证据", "", "本文件保留原值和链接。阅读用简报见 latest.md。", ""]
    for row in data.get("indices") or []:
        lines.append(
            f"- {row.get('name')} {row.get('code')} {row.get('quote_date')} {row.get('quote_time')} "
            f"点位 {row.get('current')} 成交额字段 {int(round(row.get('amount_field') or 0))} "
            f"另一字段 {row.get('amount_summary')}"
        )
    sse_phrases = data.get("sse_unit_phrases") or []
    em_phrases = data.get("em_unit_phrases") or []
    lines.append("- 上交所页面单位语句：" + ("；".join(sse_phrases) if sse_phrases else "没有检索到"))
    lines.append("- 东方财富页面单位语句：" + ("；".join(em_phrases) if em_phrases else "没有检索到"))
    szse = data.get("szse_margin") or {}
    if szse.get("footer"):
        lines.append("- 深交所页脚：" + szse["footer"])
    for item in data.get("verified_docs") or []:
        lines.append(f"- 公告 {item.get('published')} {item.get('title')} {item.get('url')}")
    return "\n".join(lines).rstrip() + "\n"


def load_state(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write(path: str, content: str) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temporary, path)


def write_brief(output_dir: str, http_get: HttpGet | None = None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    state_path = os.path.join(output_dir, "hot_concepts_state.json")
    previous = load_state(state_path)
    payload = collect_market(http_get=http_get, previous_state=previous, env=env)
    markdown = render_brief(payload)
    if not payload.get("indices"):
        markdown = "# A股学习简报\n\n本次没有形成简报。指数行情没有取到，文件中不沿用上一份简报的数字。\n\n" + markdown
    generated_day = str(payload["generated_at"])[:10]
    latest_path = os.path.join(output_dir, "latest.md")
    _atomic_write(os.path.join(output_dir, generated_day + ".md"), markdown)
    _atomic_write(latest_path, markdown)
    _atomic_write(os.path.join(output_dir, "latest.evidence.md"), render_evidence(payload))
    coverage_path = os.path.join(output_dir, "latest.coverage.json")
    _atomic_write(coverage_path, json.dumps({"generated_at": payload["generated_at"], "complete": payload["completeness"]["complete"], "rows": _coverage_rows(payload)}, ensure_ascii=False, indent=2) + "\n")
    concepts = payload.get("concepts") or {}
    names = [row["name"] for row in concepts.get("top") or []]
    source = str(concepts.get("source") or "")
    moved = _board_has_movement([*(concepts.get("top") or []), *(concepts.get("bottom") or [])])
    if names and payload.get("session_date") and payload.get("indices") and source and moved:
        _atomic_write(
            state_path,
            json.dumps(
                {
                    "source": source,
                    "endpoint": str(concepts.get("host") or ""),
                    "concept_date": payload["session_date"],
                    "names": names,
                    "saved_at": payload["generated_at"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    status = "written_failed" if not payload.get("indices") else "written_complete" if payload["completeness"]["complete"] else "written_partial"
    return {
        "status": status,
        "complete": "true" if payload["completeness"]["complete"] else "false",
        "markdown": latest_path,
        "coverage": coverage_path,
    }
