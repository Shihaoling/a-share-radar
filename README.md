# 观澜 · 中国 A 股研究工作台

[网站](https://shihaoling.github.io/a-share-radar/) · [自动更新记录](https://github.com/Shihaoling/a-share-radar/actions)

研究主体是中国 A 股：沪深大盘、行业与热门主题、A 股成分股。国际信息只作为中国板块的政策、汇率、能源和供应链影响因素。项目不连接交易账户，不自动下单。

## 已实现

- 6 个市场指数；31 行业研究种子、20 热门主题，以及东方财富行业/概念目录动态发现。采用供应商分类，不冒称申万行业。
- 备用行情渠道：BaoStock、腾讯、新浪与东方财富；同日期、同标的、同复权口径的价格交叉核验，价格冲突暂停预测。27 类板块配有独立参考 ETF，清楚标为代理，不替代板块点位或市值。
- 板块筛选、自选、指数趋势、成分股与采集后累计的总市值快照。主题重叠，市值不能加总为全市场市值。
- 官方 RSS、全球媒体 RSS、Federal Register、GDELT 和国内财经入口；原文链接、首次采集时间、来源分级、关键词产业关联及标题情绪。
- 下一次开盘的日频模型：零收益基线、Ridge、可选 MLP(32,16)。时间顺序选择/确认窗口，达不到基线则保留基线。每次新数据到达都会重新比较，保留模型版本。
- 盘中目标为今日收盘，午休不跳到次日。缺乏有效行情时间戳或分钟样本时暂停预测；可用条件下仅显示明确标记的价格延续基线。
- 不可覆盖的预测字段、延迟结算、每日复盘、误差和方向指标、基线对比、按板块/任务分组、CSV 导出；晚到的结果回填原目标日期复盘。
- 情绪分布、盘前语气与 A 股日涨跌的描述性一致率；无人工真值时不报告“情绪分类准确率”。
- 可选 GoatCounter 访客统计；后台保持私有，无前端 API 密钥。普通访问与单独 pageview 事件分列，访客是约八小时会话估计，并非真实人数。

## 必须理解的当前边界

这不是已证明有效的交易模型。新闻情绪目前为透明词典/关联规则，**尚未进入价格预测模型**；系统保存首次采集时间，供日后建立无泄漏新闻训练集。传言默认排除。没有把“新闻数量/情绪分数”包装为上涨概率，也没有声称覆盖所有新闻。

免费接口不保证实时和稳定。东方财富快照若只含抓取时间，明确标为 `retrieved_only`，不能作为经确认的盘中报价。市值历史从实际采集日起积累，不能通过指数曲线伪造历史市值。历史及成分分批轮转，板块目录覆盖不代表所有板块都已有同等历史深度。

2026 交易日历按上交所公告核实；未核实的年度或跨年下一交易日会暂停预测。需要在 2027 年之前补充新的官方日历。

页面默认真实模式。独立的“演示模式”只演示交互，明显标为合成数据，永不提交到训练、台账或生产采集。

## GitHub 部署

- GitHub Pages 来源设置为 **GitHub Actions**。
- `.github/workflows/update-and-publish.yml` 在主分支更新或手动触发时执行测试、采集、预测、保存观测和发布。
- 工作日北京时间 09:07—15:37 每半小时，以及每日 21:17 执行。GitHub 调度可能排队、延迟或跳过，不适合低延迟交易。
- 自动采集使用限流、缓存、robots、失败退避、HTTP 429 熔断；不绕过登录、验证码或付费墙。
- `web/data/dashboard.json` 保存观测与预测台账；`web/data/reviews.json` 保存每日复盘。源代码和数据变更由 Git 历史保留。
- 这是公开仓库和公开网站。请勿上传持仓、账户、密钥、私人研究笔记或未获授权的数据。免费接口的代码许可不等于上游数据再分发许可；增加数据源时自行核对适用条款。

## 本机运行与小模型

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m pipeline.run
python -m http.server 8743 --directory web
```

浏览器打开 http://localhost:8743 。没有网络时可执行 `python -m pipeline.run --offline`：仅保留缓存或空状态，不生成假数据。

启用小网络：`python -m pipeline.run --enable-mlp`，或修改 `config/settings.json` 的 `enable_mlp`。它只是候选模型，仍需通过与基线的时间样本外比较。默认训练要求为 120 个训练样本 + 20 个选择窗口 + 20 个确认窗口。无需 GPU。

设置文件：

| 文件 | 用途 |
| --- | --- |
| `config/settings.json` | 模型开关、样本阈值、预测时间槽 |
| `config/sectors.json` | 中国行业/主题目录、关键词、上下游关系 |
| `config/sources.json` | 来源、抓取频率、缓存、超时和预算 |
| `config/proxies.json` | 备用来源、ETF映射和每日双源核验轮转 |
| `web/data/analytics.json` | 公开统计 endpoint 与后台链接；不含任何 token |

浏览器“数据设置”仅改变本机显示偏好；下载配置后还需提交仓库，才改变云端执行。网站的刷新按钮只重新读取已发布快照，不会在访客浏览器中绕过限流采集金融网站。

## 最新模型的扩展路线

可把 Chronos-2 等公开时序基础模型作为额外挑战模型，本项目尚未下载或启用其权重。通用时序基准成绩不能替代 A 股样本外验证。大语言模型适合辅助事件抽取，不应直接生成看似精确的涨跌概率。先积累带首次可见时间的新闻样本、真实预测与标签，再评估情绪/事件特征的增益。

## 来源与参考

- [AKShare 数据接口](https://akshare.akfamily.xyz/)：依赖接口可能变动，以状态面板和执行日志为准。
- [上交所年度休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/)
- [美联储 RSS](https://www.federalreserve.gov/feeds/feeds.htm)
- [EIA 开放数据](https://www.eia.gov/opendata/)
- [Federal Register API](https://www.federalregister.gov/reader-aids/developer-resources/rest-api)
- [GDELT DOC API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [Chronos-2 模型卡](https://huggingface.co/amazon/chronos-2)
- [GoatCounter 访客定义](https://www.goatcounter.com/help/sessions)
- [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)

更新日期：2026-11-16。
