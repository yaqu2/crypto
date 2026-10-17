# FlowDesk v2.7 · 新闻事件数据接入与时间因果审计

**说明：这是可实际请求指定供应商 API 的代码，不包含新闻账号、API Key、付费授权，也没有在当前离线环境成功请求真实供应商。** 任何 API 请求失败均为 `NO_GO`；股票新闻不足以证明价格变动因果或盈利优势。FlowDesk 继续禁止自动下单。

## 已支持的三种新闻数据源

| 模式 | 命令 | API | 重要事实 |
|---|---|---|---|
| 实时授权候选（建议最终使用） | `bash StartNews.command --provider benzinga` | Massive Benzinga News v2 `GET /benzinga/v2/news` | 需独立 Benzinga News 数据授权；REST 轮询并非零延迟；即使请求成功也不代表新闻最初可见时刻已核实 |
| 一般股票新闻（开发与诊断） | `bash StartNews.command --provider massive --interval 3600` | Massive `GET /v2/reference/news` | 官方个人股票方案 **updated hourly**；绝不能当作秒级事件预测源 |
| 备用新闻 | `bash StartNews.command --provider finnhub` | Finnhub `GET /api/v1/news?category=general` | 计划准入与新鲜度需查你的账号；官方不保证任意用户可获得低延迟新闻 |

- Massive Benzinga v2 官方说明：https://massive.com/docs/rest/partners/benzinga/news
- Massive News 官方说明：https://massive.com/docs/rest/stocks/news
- Finnhub OpenAPI: https://github.com/Finnhub-Stock-API/finnhub-go/blob/master/api/openapi.yaml

## 在 Mac 上接入

1. 确保先安装 v2.6；关闭 FlowDesk，下载 **v2.7 安全增量补丁**，然后执行其中的 `安装v27.command`。v2.7补丁只支持完整未修改的 v2.6；已经冻结未来实验时禁止原地安装。
2. **自己去新闻提供商网站开通所需新闻方案，获取密钥。** 不要将密钥发给聊天机器人。执行 `bash ConfigureNews.command`，根据提示输入并保存在本机 `.private/news.env`（Unix 600 文件权限）。该路径不随升级包打包、不提交到报告。
3. 测试一次：`bash PollNews.command --provider benzinga`；如果只持有普通 Massive 股票 News 权限，运行 `bash PollNews.command --provider massive`。首次遇到 HTTP 403 通常是授权不支持相应接口，不代表你输错代码。
4. 在**单独的 Terminal** 运行 `bash StartNews.command --provider benzinga --interval 60`，与原 `bash Start.command` 并行。终端关闭后新闻轮询停止。每分钟轮询是可配置示例，不构成严格秒级 SLA。
5. 查看时间证据：`bash StudyNews.command`；在 FlowDesk 网页找到“新闻事件与产业链观察”；或通过 `news-status`、`news-latest`、`news-research` 本机接口查看。不应将新闻来源解释成已经找到买入卖出条件。
6. 如需把新闻与已完成的分钟微观结构对齐：先运行 `bash StudyMultiScale.command --events-file /path/to/recording/events.jsonl`；再运行 `bash StudyNews.command --microstructure-report output/research/multiscale_v26.json`。新闻以该分钟**结束**时的本地可用状态入模，不允许未来新闻回流。
7. 严格指定历史时点（**应是实际决策时间**）：`bash StudyNews.command --asof 2026-10-15T14:30:00Z`。
8. 对接 v2.6 微观结构：`bash StudyMultiScale.command --context-file research/news/news_events.jsonl --asof 2026-10-15T14:30:00Z`。若配合原始盘口事件，可加 `--events-file /path/to/events.jsonl`。

## 保存什么信息

`research/news/news_events.jsonl` 是追加式哈希链审计日志。每条新闻含 `type='news'`、`key`、`title/summary/link/tickers/topics`、`published_at`、`first_seen_at`、`received_at`、`revision`、`previousHash`、`eventHash`。

- `published_at` 是供应商返回的**发表时间声明**，不是这套程序自己观察到新闻的时间。
- `first_seen_at` 与 `received_at` **都取本机收到 API 结果时的 UTC 时刻**。本产品并不知道新闻供应商原始入库时钟，所以以本机观察时刻作为保守入模下限；不把发表时间直接变成回测中已经知道的特征。
- 修订后标题变化会以新 `revision` 追加，过去版本保留；重复新闻去重；日志中间丢失或修改后禁止继续追加、禁止研究（本地 SHA256 哈希防意外篡改，不构成外部第三方独立时间戳证明）。
- **历史回填不等于历史事前可用。** 如果你今天拉到上周报道，该报道从今天第一次采集开始才对 FlowDesk 可用。供应商历史公告先后顺序不可倒填。
- 标题中的 AI、利率、光模块、存储等词仅产生**话题标签**。不等于新闻情绪、事件冲击或行情因果已验证。`bySectorMentions` 是文章和股票提及计数，不是板块资金流。
- 文章内容仅用于本地研究显示；新闻原文版权和数据再分发按供应商授权条款执行。本实现不重新发布全文。

## 门禁与已知局限

1. 数据权限未获授权、不存在 API Key、API 429/403、网络异常、分页达到上限、日志校验失败都明确报告 `NO_GO`；历史合法结果保留，但不冒充服务在线。
2. 只读新闻采集、事件主题分类和按当时可用信息的研究入口，**不生成量化方向预测、更不下单**。
3. 供应商每月调用次数、费用与套餐按其最新合同确定；本代码没有购买任何数据服务；首次运行需要 Mac 本机外网。
4. 真实延迟必须计算 `received_at - published_at` 的分布，并对不同数据渠道和市场时段分层对照；**请求成功不能证明数据真的足够实时**。
5. `finhub` 模式的 `general` 类型没有完整的 AAOI/LITE 个股覆盖保证；需要个股专属新闻可扩展独立 `/company-news` 请求并遵循配额。
6. 强烈建议将 `research/news/news_events.jsonl` 连同市场逐笔/分钟数据单独备份；后续正式研究冻结时还要登记规则版本、数据开始日期和新闻提供商授权属性。

## 工程验收

`python3 -m unittest tests.test_v27_news -v`：本轮专项模拟接口与完整性测试，无真实供应商请求。旧版研究测试是否通过请以随包 `工程验收_v27.md` 为准。旧封存策略核心代码与订单权限未修改。
