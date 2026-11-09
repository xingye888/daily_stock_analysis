# -*- coding: utf-8 -*-
"""Offline tests for the A-share learning brief."""

import ast
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from src.learning_brief.brief import (
    accept_official_article,
    amount_from_unit_header,
    labeled_sheet_amount,
    limit_pool_date_ok,
    parse_sina_board,
    parse_sse_stock_trade_amt,
    assess_completeness,
    classify_headline,
    collect_market,
    concept_for_date,
    extract_unit_phrases,
    extract_zoom_text,
    market_clock,
    merge_index_quotes,
    parse_eastmoney_margin,
    parse_etf_page,
    parse_kuaixun,
    parse_limit_pool,
    parse_pbc_article,
    parse_pbc_links,
    parse_sina_quotes,
    parse_sse_margin,
    render_brief,
    summarize_fenbu,
    unit_phrase_for,
    write_brief,
)


BJ = timezone(timedelta(hours=8))


SINA_FIXTURE = "\n".join(
    [
        'var hq_str_sh000001="上证指数,3612.1470,3689.9109,3649.1774,3660.4861,3610.1978,0,0,477042561,772634566231,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-11-06,15:44:00";',
        'var hq_str_s_sh000001="上证指数,3649.1774,-40.7335,-1.10,4770425,77263457";',
        'var hq_str_sz399001="深证成指,11000.00,11471.093,11175.78,11200.00,11050.00,0,0,56934812200,885158720000,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-11-06,15:44:00";',
        'var hq_str_s_sz399001="深证成指,11175.78,-295.313,-2.57,569348122,88515872";',
    ]
)


class LearningBriefParseTests(unittest.TestCase):
    def test_sina_amount_cross_check_uses_observed_fields(self):
        merged = merge_index_quotes(parse_sina_quotes(SINA_FIXTURE))
        shanghai = next(row for row in merged if row["code"] == "sh000001")
        self.assertEqual(shanghai["quote_date"], "2026-11-06")
        self.assertEqual(shanghai["amount_field"], 772634566231)
        self.assertNotIn("amount_unit_checked", shanghai)
        self.assertAlmostEqual(shanghai["change"], -40.7335, places=3)

    def test_fenbu_counts_do_not_treat_buckets_as_limits(self):
        counts = summarize_fenbu([{"-1": 2, "0": 1, "1": 3}, {"-10": 4}])
        self.assertEqual(counts, {"up": 3, "down": 6, "flat": 1, "total": 10})

    def test_limit_pool_keeps_counts_and_drops_security_identity(self):
        parsed = parse_limit_pool(
            {
                "data": {
                    "tc": 2,
                    "qdate": 20261106,
                    "pool": [
                        {"c": "000001", "n": "样本证券", "hybk": "银行"},
                        {"c": "000002", "n": "另一样本", "hybk": "银行"},
                    ],
                }
            }
        )
        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["industries"], [{"name": "银行", "count": 2}])
        self.assertNotIn("000001", json.dumps(parsed, ensure_ascii=False))

    def test_sse_margin_keeps_raw_rzye(self):
        parsed = parse_sse_margin({"pageHelp": {"data": [{"opDate": "20261106", "rzye": "1168836509671", "rzmre": "10"}]}})
        self.assertEqual(parsed["rzye"], 1168836509671)

    def test_incomplete_etf_page_is_not_complete(self):
        parsed = parse_etf_page({"pageHelp": {"total": 3, "data": [{"STAT_DATE": "2026-11-06", "TOT_VOL": "1"}]}})
        self.assertFalse(parsed["complete"])
        self.assertEqual(parsed["sum_raw"], 1)

    def test_keyword_classification_and_kuaixun_parser(self):
        self.assertEqual(classify_headline("国常会要求完善资本市场稳市机制"), ["宏观", "政策"])
        self.assertEqual(classify_headline("埃及发生列车脱轨事故"), [])
        rows = parse_kuaixun('var ajaxResult={"LivesList":[{"title":"央行开展MLF操作","showtime":"2026-11-08 09:00:00","url_w":"https://example.com/a"}]};')
        self.assertEqual(rows[0]["source"], "东方财富快讯")

    def test_eastmoney_margin_does_not_invent_zero(self):
        self.assertIsNone(parse_eastmoney_margin({"result": {"data": []}}))


class LearningBriefRenderTests(unittest.TestCase):
    def test_sunday_lesson_and_missing_numbers_stay_unknown(self):
        payload = collect_market(
            http_get=lambda url, headers, timeout: b"{}",
            now=datetime(2026, 11, 8, 21, 0, tzinfo=BJ),
            env={},
        )
        text = render_brief(payload)
        self.assertIn("## 1. 事实", text)
        self.assertIn("## 2. 解释", text)
        self.assertIn("## 3. 未知", text)
        self.assertIn("事实、解释和未知", text)
        self.assertIn("上交所融资余额本次没有取到", text)
        self.assertNotIn("rzye=0", text)
        self.assertNotIn("荐股", text)
        self.assertNotIn("买入", text)
        self.assertNotIn("卖出", text)
        self.assertTrue(concept_for_date(datetime(2026, 11, 8, tzinfo=BJ))["title"])

    def test_szse_formula_unit_is_not_applied_to_the_cell(self):
        payload = {
            "generated_at": "2026-11-08 21:00:00",
            "session_date": "2026-11-06",
            "indices": [],
            "breadth": {"status": "missing"},
            "limit_up": {"status": "missing"},
            "limit_down": {"status": "missing"},
            "industries": {"status": "missing"},
            "concepts": {"status": "missing"},
            "persistence": {"status": "unknown"},
            "sse_margin": None,
            "szse_margin": {
                "date": "2026-11-04",
                "balance_raw": "10,867.48",
                "buy_raw": "777.16",
                "footer": "本日融资余额(元)=前日融资余额＋本日融资买入-本日融资偿还额",
                "attempted_dates": ["2026-11-06", "2026-11-05", "2026-11-04"],
            },
            "em_margin": None,
            "etf": {"status": "missing", "newer": None, "older": None, "errors": []},
            "headlines": {"fetched": 0, "matched": [], "matched_total": 0, "unmatched": 0, "errors": []},
            "lesson": concept_for_date(datetime(2026, 11, 8, tzinfo=BJ)),
            "errors": [],
            "key_presence": {},
        }
        text = render_brief(payload)
        self.assertIn("jrrzye=10,867.48", text)
        self.assertIn("不把该公式单位套到 jrrzye", text)
        self.assertNotIn("10,867.48 元", text)
        self.assertNotIn("10,867.48亿元", text)

    def test_renderer_puts_converted_turnover_in_explanation(self):
        payload = {
            "generated_at": "2026-11-08 21:00:00",
            "session_date": "2026-11-06",
            "indices": merge_index_quotes(parse_sina_quotes(SINA_FIXTURE)),
            "breadth": {"status": "missing"},
            "limit_up": {"status": "missing"},
            "limit_down": {"status": "missing"},
            "industries": {"status": "missing"},
            "concepts": {"status": "missing"},
            "persistence": {"status": "unknown"},
            "sse_margin": {"date": "20261106", "rzye": 1168836509671, "rzmre": None},
            "szse_margin": None,
            "em_margin": None,
            "etf": {"status": "missing", "newer": None, "older": None, "errors": []},
            "headlines": {"fetched": 0, "matched": [], "matched_total": 0, "unmatched": 0, "errors": []},
            "lesson": concept_for_date(datetime(2026, 11, 8, tzinfo=BJ)),
            "errors": [],
            "key_presence": {},
        }
        text = render_brief(payload)
        self.assertIn("772634566231", text)
        self.assertIn("1168836509671", text)
        self.assertNotIn("11688.37", text)
        self.assertNotIn("16577.93", text)
        raw_lines = [line for line in text.splitlines() if "rzye=" in line or "成交额字段原值" in line]
        self.assertTrue(raw_lines)
        self.assertFalse(any("亿元" in line for line in raw_lines))
        self.assertIn("单位未知", text)
        self.assertIn("不是完整简报", text)

    def test_module_does_not_import_notification_or_llm(self):
        path = os.path.join(os.path.dirname(__file__), "..", "src", "learning_brief", "brief.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertFalse(any("notification" in name or "litellm" in name or name.startswith("src.analyzer") for name in imported))

    def test_write_brief_uses_injected_http_and_saves_concept_state(self):
        def http_get(url, headers, timeout):
            if "hq.sinajs.cn" in url:
                return SINA_FIXTURE.encode("gb18030")
            if "getTopicZDFenBu" in url:
                return json.dumps({"data": {"qdate": 20261106, "fenbu": [{"1": 2, "-1": 3, "0": 1}]}}).encode()
            if "clist/get" in url and "t%3A3" in url or ("clist/get" in url and "t:3" in url):
                return json.dumps({"data": {"diff": [{"f14": "样本概念", "f3": 1.5}]}}).encode()
            if "clist/get" in url:
                return json.dumps({"data": {"diff": [{"f14": "样本行业", "f3": -0.5}]}}).encode()
            return b"{}"

        with tempfile.TemporaryDirectory() as directory:
            result = write_brief(directory, http_get=http_get, env={})
            self.assertEqual(result["status"], "written_partial")
            self.assertEqual(result["complete"], "false")
            self.assertTrue(os.path.exists(result["markdown"]))
            with open(os.path.join(directory, "hot_concepts_state.json"), encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["concept_date"], "2026-11-06")
            self.assertIn("样本概念", state["names"])

    def test_share_delta_is_not_described_as_money(self):
        payload = {
            "generated_at": "2026-11-08 21:00:00",
            "session_date": "2026-11-06",
            "indices": [],
            "breadth": {"status": "missing"},
            "limit_up": {"status": "missing"},
            "limit_down": {"status": "missing"},
            "industries": {"status": "missing"},
            "concepts": {"status": "missing"},
            "persistence": {"status": "unknown"},
            "sse_margin": None,
            "szse_margin": None,
            "em_margin": None,
            "etf": {
                "newer": {"stat_date": "2026-11-06", "sum_raw": 30, "rows": 2, "reported_total": 2, "complete": True},
                "older": {"stat_date": "2026-11-05", "sum_raw": 10, "rows": 2, "reported_total": 2, "complete": True},
                "errors": [],
            },
            "lesson": concept_for_date(datetime(2026, 11, 8, tzinfo=BJ)),
            "errors": [],
            "key_presence": {},
            "verified_docs": [],
        }
        text = render_brief(payload)
        delta_line = next(line for line in text.splitlines() if "差额" in line)
        self.assertIn("+20.00", delta_line)
        self.assertNotIn("净流入", delta_line)
        self.assertNotIn("申赎", delta_line)
        self.assertNotIn("亿元", delta_line)

    def test_keyword_titles_are_not_fundamental_facts(self):
        payload = collect_market(
            http_get=lambda url, headers, timeout: b"{}",
            now=datetime(2026, 11, 9, 8, 0, tzinfo=BJ),
            env={},
        )
        text = render_brief(payload)
        self.assertIn("基本面保持未知", text)
        self.assertNotIn("关键词", text)
        self.assertIn("开盘前", text)
        self.assertIn("不是完整简报", text)

    def test_verified_article_is_quoted_from_body(self):
        html = """
        <html><title>样本再贷款政策</title>
        <meta name="createDate" content="2026-11-04 09:04:10" />
        <div id="zoom"><p>为维护资本市场平稳运行，下调股票回购增持再贷款利率0.25个百分点至1%。支持对象为21家全国性银行，支持比例为100%，期限1年。</p>打印本页</div>
        </html>
        """
        article = parse_pbc_article(html, "http://www.pbc.gov.cn/sample/index.html")
        self.assertIn("0.25个百分点", article["text"])
        self.assertNotIn("打印本页", article["text"])
        links = parse_pbc_links(
            '<a href="/goutongjiaoliu/a/index.html">国务院关于金融工作情况的报告</a>'
            '<a href="/goutongjiaoliu/b/index.html">互换便利操作结果公告</a>'
            '<a href="/goutongjiaoliu/c/index.html">行长会见来宾</a>'
            '<a href="/goutongjiaoliu/d/index.html">中国京剧艺术普通纪念币公告</a>'
        )
        self.assertEqual([item["title"] for item in links], ["互换便利操作结果公告", "国务院关于金融工作情况的报告"])
        phrases = extract_unit_phrases("余券应划转量。融券单位：股（标的证券为股票）。")
        self.assertTrue(phrases and phrases[0].startswith("融券单位"))
        self.assertEqual(unit_phrase_for(phrases, "融资余额"), "")
        self.assertFalse(accept_official_article({"title": "中国人民银行公告第28号", "text": "定于发行普通纪念币一枚。"}))
        payload = {
            "generated_at": "2026-11-09 08:00:00",
            "session_date": "2026-11-06",
            "indices": [],
            "breadth": {"status": "missing"},
            "limit_up": {"status": "missing"},
            "limit_down": {"status": "missing"},
            "industries": {"status": "missing"},
            "concepts": {"status": "missing"},
            "persistence": {"status": "unknown"},
            "sse_margin": None,
            "szse_margin": None,
            "em_margin": None,
            "etf": {"status": "missing", "errors": []},
            "lesson": concept_for_date(datetime(2026, 11, 9, tzinfo=BJ)),
            "errors": [],
            "key_presence": {},
            "verified_docs": [article],
        }
        text = render_brief(payload)
        facts = text.split("### 1.1 基本面", 1)[1].split("### 1.2", 1)[0]
        self.assertIn("0.25个百分点", facts)
        self.assertIn("原始页面正文", facts)

    def test_unrelated_unit_phrase_does_not_convert_margin(self):
        self.assertEqual(unit_phrase_for(["融券单位：股（标的证券为股票）"], "融资余额"), "")
        self.assertEqual(extract_zoom_text('<div id="zoom">正文足够长。' + ("补充" * 30) + "打印本页<footer>页脚</footer></div>").find("页脚"), -1)

    def test_monday_morning_is_not_a_closing_brief(self):
        clock = market_clock(datetime(2026, 11, 9, 8, 20, tzinfo=BJ), "2026-11-06")
        verdict = assess_completeness({"clock": clock, "indices": [{"code": "sh000001"}, {"code": "sz399001"}], "breadth": {"status": "ok"}, "limit_up": {"status": "ok"}, "limit_down": {"status": "ok"}, "verified_docs": [{"title": "x"}], "cache_used": False})
        self.assertFalse(verdict["complete"])
        self.assertIn("行情日期不是生成日期", verdict["reasons"])
        self.assertIn("生成时不在收盘后", verdict["reasons"])

    def test_failed_run_does_not_keep_previous_brief_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.path.join(directory, "latest.md")
            with open(previous, "w", encoding="utf-8") as handle:
                handle.write("过期数字 SENTINEL-16577.93 亿元 完整简报")
            result = write_brief(directory, http_get=lambda url, headers, timeout: b"{}", env={})
            self.assertEqual(result["status"], "written_failed")
            with open(previous, encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("SENTINEL-16577.93", text)
            self.assertIn("不是完整简报", text)
            self.assertIn("不沿用上一份", text)

    def test_header_yuan_converts_and_unlabeled_field_does_not(self):
        parsed = amount_from_unit_header("成交金额(元)", "100,000,000.00", "成交金额")
        self.assertEqual(parsed["yi"], 1)
        self.assertIsNone(amount_from_unit_header("成交金额", "7732.82", "成交金额"))
        self.assertIsNone(amount_from_unit_header("融券单位：股", "1090179947092", "融资余额"))
        rows = [["证券类别", "成交金额(元)"], ["股票", "200,000,000"], ["ETF", "50,000,000"]]
        self.assertEqual(labeled_sheet_amount(rows, "股票", "成交金额")["yi"], 2)
        self.assertIsNone(parse_sse_stock_trade_amt({"result": [{"PRODUCT_CODE": "17", "TRADE_AMT": "7732.82", "TRADE_DATE": "20261106"}]}).get("yi"))
        board = parse_sina_board('var x = {"a":"hangye_ZA01,农业,15,8.7,0.05,0.67,1,2"};')
        self.assertEqual(board[0]["name"], "农业")
        text = render_brief(
            {
                "generated_at": "2026-11-09 16:10:00",
                "session_date": "2026-11-09",
                "indices": [],
                "breadth": {"status": "missing"},
                "limit_up": {"status": "missing"},
                "limit_down": {"status": "missing"},
                "industries": {"status": "missing"},
                "concepts": {"status": "missing"},
                "persistence": {"status": "unknown"},
                "sse_margin": None,
                "szse_margin": None,
                "em_margin": None,
                "etf": {"errors": []},
                "lesson": concept_for_date(datetime(2026, 11, 9, 16, 10, tzinfo=BJ)),
                "errors": [],
                "key_presence": {},
                "verified_docs": [],
                "szse_amount_date": "2026-11-09",
                "szse_stock_turnover": parsed,
            }
        )
        self.assertIn("表头原文“成交金额(元)”", text)
        self.assertIn("1.00 亿元", text)
        self.assertIn("不是完整简报", text)
        self.assertFalse(limit_pool_date_ok("20261109", "20261106"))
        self.assertTrue(limit_pool_date_ok("20261106", "20261106"))


if __name__ == "__main__":
    unittest.main()
