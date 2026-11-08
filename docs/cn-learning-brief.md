# A股全市场学习简报

本命令整理 A 股全市场学习材料。它记录行情、融资、ETF 份额和新闻标题，并把事实、解释、未知分开。它不写个股买卖建议，不调用 LLM，不发送通知，不连接券商。

英文索引只保留入口，正文以本中文说明为准。

## 运行

```bash
python -m src.learning_brief
```

默认输出到运行目录下的 `reports/cn_learning_brief/`。可用环境变量或参数改目录：

```bash
CN_LEARNING_BRIEF_OUTPUT_DIR=reports/cn_learning_brief python -m src.learning_brief
python -m src.learning_brief --output-dir reports/cn_learning_brief
```

运行器只使用 Python 标准库。它不读取 `.env` 里的密钥；即使环境里已有 LLM 或通知变量，命令也不会使用变量值。

产物：

| 文件 | 内容 |
| --- | --- |
| `YYYY-MM-DD.md` | 按生成日期保存的简报 |
| `latest.md` | 同一份简报的稳定路径 |
| `latest.coverage.json` | 覆盖矩阵 |
| `hot_concepts_state.json` | 本次概念涨幅前 5 名，供下一次比较 |

`reports/` 已在 `.gitignore` 中，样报不会进入 Git。

退出码：`0` 表示写到了带指数行情的简报；`2` 表示文件已写但指数行情缺失；`1` 表示目录无法写入。退出码 `0` 只表示文件写完，不表示三面数据齐全，也不表示模型综述已经完成。

## 与上游大盘复盘的边界

`python main.py --market-review` 会走 `MarketAnalyzer.generate_market_review()`，需要可用的 LLM 后端，并在未加 `--no-notify` 时进入通知发送。学习简报不调用这条链路。

本次没有配置可用的 LLM API 密钥，也没有把 Google AI Pro 订阅当成 Gemini API 密钥。因此没有模型生成的市场综述。简报里的解释只说明口径和换算，不用模板句代替端到端分析成功。

## 数据口径

| 材料 | 来源 | 写入位置 |
| --- | --- | --- |
| 指数点位、行情时间、成交额原值 | 新浪财经 `hq.sinajs.cn` 完整行情与简要行情 | 事实保留原值；完整字段与简要字段比值约等于 10000 时，解释里才把完整字段读成元 |
| 涨跌家数 | 东方财富 `getTopicZDFenBu` | 事实为分布桶相加；解释说明它不是涨跌停家数 |
| 涨停、跌停家数 | 东方财富涨停池、跌停池字段 `tc` | 事实只写家数和行业出现次数，不写证券代码 |
| 行业、概念涨跌幅 | 东方财富板块列表 | 事实为价格涨跌幅；解释说明它不是业绩或政策变化 |
| 热点持续性 | 本目录上一份 `hot_concepts_state.json` | 没有上一交易日名单时保持未知 |
| 上交所融资余额 | `queryMargin.do` 字段 `rzye` | 事实写原值；JSON 无单位，按公开汇总页惯例折算的亿元放在解释 |
| 深交所融资余额 | `CATALOGID=1837_xxpl` 字段 `jrrzye` | 事实写原值、尝试过的日期和页脚。页脚公式中的“元”不套用到汇总单元格，单元格单位保持未知 |
| 两市融资对照 | 东方财富 `RPTA_WEB_MARGIN_DAILYTRADE` 字段 `FIN_BALANCE` | 与交易所数字并列，日期不同则不相加 |
| ETF 份额 | 上交所 `COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L` 字段 `TOT_VOL` | 事实写两日原值合计和差额；万份换算只作为 AkShare 处理方式的解释 |
| 宏观、政策、行业、业绩线索 | 东方财富快讯和栏目 353 的标题 | 事实为标题、时间、来源和链接；关键词归类不是正文核对 |

主力资金净流入不在资金口径内。深市 ETF 份额、申购赎回金额、交易所官方全市场股票成交额，本次取不到就留在未知。

## 已有 16:30 任务如何读取

工作日 16:30（北京时间）的发送任务已经由 Grok Bot 安排在当前对话中。本仓库不新增 `schedule`、GitHub Actions 定时任务或通知渠道。

持续运行时，生成程序应在 16:30 之前把简报写到稳定目录。Bot 只读取该目录的 `latest.md`：

1. 打开 `latest.md`，核对其中的生成时间是否属于当次预期运行。
2. 转述文件中的事实、解释和未知，保留日期、来源和单位。
3. 文件不存在、生成时间不是当次运行，或文件写明某项未知时，消息直接说明缺口，不另算数字，也不改用模型补写。

临时开发环境里的文件会随环境消失，不能当作持续运行产物。

## 最简持久运行

持久运行需要另选一台由使用者控制的机器，并另行确认后才启动。下面的安排不在本次执行。

1. 使用本 Fork 的学习简报分支，只在该机器上创建虚拟环境并安装 Python 3.10 以上。学习简报命令本身不再安装 akshare、LiteLLM 或通知依赖。
2. 选择一个私有输出目录，设置 `CN_LEARNING_BRIEF_OUTPUT_DIR`。不要把目录提交到公开 Fork，也不要把 `.env` 放进去。
3. 每个 A 股交易日 16:00（北京时间）运行一次 `python -m src.learning_brief`。16:00 早于已有的 16:30 读取；融资余额和 ETF 份额仍可能晚于收盘，简报会按来源日期标明。
4. 让已有 Grok Bot 在 16:30 读取这个目录的 `latest.md`。
5. 不启用 GitHub Actions，不配置 Webhook，不把服务暴露到公网，不接入券商。

费用：这条命令访问的是新浪、东方财富、上交所和深交所的公开页面，不需要 API 密钥，也没有本程序发起的模型或通知费用。机器、网络和 Grok Bot 本身是否另有费用，以使用者自己的账户为准，这里不估算月费。若以后要补上模型综述，需要单独的 LLM API 密钥和对应服务商的计费权限；Google AI Pro 订阅不能直接代替该密钥。

## 回滚

删除 `src/learning_brief/`、`tests/test_cn_learning_brief.py` 和本说明，并去掉 `docs/CHANGELOG.md`、`docs/INDEX.md`、`docs/INDEX_EN.md`、`docs/grok-bot-integration.md`、`.env.example` 中对应的入口即可。已写出的 `reports/cn_learning_brief/` 只是本地产物。
