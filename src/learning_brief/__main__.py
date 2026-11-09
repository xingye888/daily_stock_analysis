# -*- coding: utf-8 -*-
"""Write one A-share learning brief. Does not schedule, notify, or call an LLM."""

from __future__ import annotations

import argparse
import os
import sys

from src.learning_brief.brief import write_brief


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "采集免密钥公开行情并写出 A 股全市场学习简报。"
            "不调用 LLM，不发送通知，不连接券商。"
        )
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("CN_LEARNING_BRIEF_OUTPUT_DIR", "reports/cn_learning_brief"),
        help="输出目录，可用环境变量 CN_LEARNING_BRIEF_OUTPUT_DIR 覆盖",
    )
    args = parser.parse_args(argv)
    try:
        result = write_brief(args.output_dir)
    except OSError as exc:
        print(f"status=write_failed error={exc}", file=sys.stderr)
        return 1
    print(
        "status={status} complete={complete} llm=blocked notifications_sent=false "
        "markdown={markdown} coverage={coverage}".format(**result)
    )
    return 0 if result["status"] != "written_failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
