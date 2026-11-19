# 观澜腾讯云运行与数据交接

截至 2026-11-19，网页通过本机 SSH 隧道访问。Nginx 在服务器上监听 `127.0.0.1:8769`，静态目录为 `/srv/ashare-radar-preview/public`。最后一份 GitHub Pages 数据作为展示基线。行情、资讯、模型、研究和数据集进度分别用独立 JSON 更新。

## 定时任务

所有时间均为北京时间。云端模型使用固定训练产物，`RADAR_ALLOW_TRAINING=false`。训练与调参在本地完成。

| 工作 | systemd 单元 | 时刻 |
| --- | --- | --- |
| SXData 行情与当日分时 | `ashare-preview-relay.service` | 交易时段每 5 秒最多发起一批，滚动 60 秒最多 12 批。通常 10 批指数与行业、2 批个股，个股约每 30 秒一批 |
| 即时资讯及 MiniMax | `ashare-preview-news@collect.timer`、`ashare-preview-news.path` | 09:00–16:00 每 2 分钟采集，其他时间每 20 分钟采集。收到新条目或补齐正文后唤醒分析，连续分批处理可分析的积压。受限条目留在队列，冷却期结束后的下一次新增可触发补做。 |
| 日线、成分和已训练模型 | `ashare-preview-daily.timer`、`ashare-preview-reference.timer`、`ashare-preview-model.timer` | 盘后分批采集收盘数据，交易日 07:41 和 08:41 再补上一交易日缺项。日线模型可在开盘前用新到的收盘数据重新推断。盘中每五分钟保存对应分钟的输入，滚动预测今日收盘尚待独立模型 |
| 盘后研究和复盘 | `ashare-cloud-research.timer` | 工作日 17:26、19:38、21:22，程序再次检查是否为交易日 |
| 历史快讯采集 | `ashare-training-collect.timer` | 09:00–16:00 每 10 分钟处理一个窗口，其余每分钟一个窗口 |
| 历史快讯标注 | `ashare-training-label.timer` | 每批结束后等待 1 分钟再处理下一批，每批最多 32 篇 |
| 私有数据集导出 | `ashare-training-export.timer` | 每小时第 47 分钟 |

从旧版迁移时，用 `systemctl disable --now ashare-preview-news@analyze.timer` 停用即时分析时钟，再用 `systemctl mask ashare-preview-news@analyze.timer` 屏蔽该实例。安装新版程序和路径单元后，执行 `systemctl daemon-reload` 和 `systemctl enable --now ashare-preview-news.path`。已经启用的路径单元用 `systemctl restart ashare-preview-news.path` 重新监听。正在处理的分析批次可以自然结束。

行情调度使用 `relay-active.json`，离线回放也读取此文件。共享额度、慢请求或限流会降低实际采样频率。长请求结束后，错过的个股批次优先使用下一次机会，采集器按正常间隔继续工作。午休和盘后暂停实时行情请求。浏览器在交易时段每 5 秒检查已发布的指数与行业行情，确认午休或收盘后三类行情快照分别每 5 分钟检查一次。资讯使用独立的发布周期。

### 采集限额与缺口维护

本站所有腾讯云 SXData 任务共用 `relay/rate-gate.json` 和文件锁。每次发送前在锁内核对最近 60 秒的请求，合计最多 100 次，重试也占用额度。行情正常时相邻批次至少间隔 5 秒，涉及其他业务时至少间隔 6 秒。10 分钟内连续累计 2 次明确实时 429 后，行情间隔增至 10 秒，3 次及以上增至 15 秒。旧限流记录退出窗口后逐级恢复。供应商指定的更长冷却仍然有效。

独立定时器 `ashare-minute-recovery.timer` 查找交易所指数在已结束交易分钟中的缺口，每 60 秒最多向 `idx_mins` 请求一个标的的真实 1 分钟行情。任务按日、标的持久化，午休可补上午数据，盘中空返回会退避，跨日继续处理。补点在同一私有 SQLite 库的 `minute_recovery` 表中分别保存源时点和实际取得时间。网站用 `/data/minute-recovery.json` 填补当日分时，相同分钟优先显示已采集的实时报价。原始实盘观测和当时已保存的模型输入可独立复核。

资讯每轮仍只请求一次。常规窗口回看水位前 3 小时，每 2 小时借用一轮检查过去 24 小时。在窗口积压时兼顾最新快讯，持久化的拆分队列继续处理触顶和异常窗口。同日标题相同且正文有变化的快讯分别保留。

在服务器上运行以下只读命令核对共享计量及工作进程日志。

```sh
cd /opt/ashare-radar-preview
PYTHONPATH=runtime /var/lib/ashare-radar-preview/venv/bin/python deploy/tencent-preview/collector_health.py --since today
journalctl -u ashare-minute-recovery.service --since today --no-pager
```

共享计量中的 `starts_last_60s` 是当前滚动窗口，`peak_60s` 是计量开始以来的最高值。`apis` 按接口记录累计发送、完成结果、本地延后和传输异常，`recent` 保留有限的逐次耗时样本。日志中一次资讯调度遇到冷却不一定向上游发出了请求。引用 429 或失败率时应区分有供应商返回码的请求、HTTP 错误、传输异常和本地延后，并注明计量时段。新计量启用前的历史日志没有 DNS、连接或读取阶段的逐次归因。

盘后研究复用现有行情校验、估值风险指标、个股模型和持仓聚合、ETF 数据及预测台账模块。成分按日期记录并轮换刷新。资金流以 SXData `moneyflow` 的实际盘后回包为准。研究投影记录发布时刻、市场覆盖和各子任务状态。订阅接口或数据迟到会体现为待更新，日后按同一时间口径继续处理。

## 历史新闻范围与导出

SXData 限流时，历史采集暂停到冷却结束。`collection_priority_days` 中指定的日期先补采，再处理此前尝试过的日期及其拆分窗口。补采队列全部处理后才查询尚未开始的日期。连续收到限流时，按 1、2、4、8、15 分钟退避，并遵循数据源返回的更长等待时间。共享冷却拦截的调度不计入日期请求次数。看板逐秒显示冷却剩余时间。

历史标注每次发起一个 MiniMax 请求，优先处理较早日期的首标及重试条目。批量模式使用 `MiniMax-M3.1-Flash` 的 `low` 思考档位，每篇按独立 ID 返回结构化 JSON。解析时将证据编号还原为本篇原文并校验，无法归类的影响期限记录为 `uncertain`。批量结果的 `label.annotation_prompt` 标记为 `fulltext-sw31-impact-v2-refs+structured-batch-v4`。整批格式错误等待 2 分钟重试，两次未通过的条目逐条重试。传输超时后至少等待 5 分钟，再将重试批量减半，最少 1 篇。成功后恢复正常批量。看板显示近 10 分钟实际入库速率，点击日期可查看待首次标注、待重试和待复核条数。

范围为 **2026-05-22 00:00（含）至 2026-11-18 00:00（不含）**，180 个北京时间自然日。来源为 SXData 的 `news`、`src=cls`（财联社快讯），保存接口返回的快讯正文。当返回达到 1,500 条或超过响应大小时拆分窗口。同一日期的规范化标题与正文均相同才合并正文，同标题的不同正文各自保留。原始来源记录列入 `source_occurrences`，按各自发布时间和抓取时间留存。主记录 `published_at` 取该内容最早的已观察来源时间。模型标签逐条校验证据来自本篇正文，原模型实际接受的发布时间留在 `label.input_published_at`。

| 路径 | 内容 |
| --- | --- |
| `/var/lib/ashare-radar-preview/training-news/dataset.sqlite3` | 私有原始数据、窗口队列、标注、错误和 token 计量 |
| `/var/lib/ashare-radar-preview/training-news/export/news.jsonl.gz` | 可转移至另一台机器的压缩 JSON Lines 数据 |
| `/var/lib/ashare-radar-preview/training-news/export/manifest.json` | 导出范围、条数、完成状态及压缩文件的 SHA-256 |
| `/var/lib/ashare-radar-preview/training-news/final/2026-05-22_to_2026-11-17/` | 180 天复核与全部标注完成后，定时任务自动发布通过 SHA-256 和记录数校验的正式数据与清单 |
| `/training-progress/` | 采集与标注各有月历，点击日期查看两个阶段的状态、条数和待处理明细 |

每条导出记录包括稳定 ID、来源、原始发布时间 `published_at`、实际采集时间 `received_at`、实际标注时间 `labelled_at`、标题、正文、正文散列和标注状态。标签包含文字摘要、语气、事实状态、市场及行业影响、传导方向、置信度、观察期、证据和不确定性。模型版本和提示版本随记录保留。

`backfilled=true` 的记录是在历史日期之后采集和标注。合并行情时应保留实际可得时点。做回测复现时按可得时点过滤；按发布日期做离线训练时应明确属于事后标注，并按时间划分验证集与测试集。未标注的记录保留 `label=null`。看板上的「无返回」表示源接口该窗口回空；完整性以当前数据源和已查窗口为范围。

可通过 SSH 登录后运行下列命令查看任务状态或立即生成一次导出。

```sh
systemctl list-timers 'ashare-training-*' 'ashare-cloud-research*'
journalctl -u ashare-training-news@collect.service -u ashare-training-news@label.service -n 50 --no-pager
sudo systemctl start ashare-training-news@export.service
cd /var/lib/ashare-radar-preview/training-news/export
sha256sum news.jsonl.gz
```

看板显示「正式归档已校验」后，在训练电脑上使用已有授权的 SSH 凭据下载正式清单和压缩文件。下载后比对 `manifest.json` 的 `sha256`。阶段导出仍可从 `export/` 查看。

```sh
scp ubuntu@${RADAR_HOST}:/var/lib/ashare-radar-preview/training-news/final/2026-05-22_to_2026-11-17/news.jsonl.gz .
scp ubuntu@${RADAR_HOST}:/var/lib/ashare-radar-preview/training-news/final/2026-05-22_to_2026-11-17/manifest.json .
```

## 域名

腾讯云控制台显示 `mystockmarket.online` 未备案。上海实例在备案通过后配置主域名及需要的 `www` 记录、HTTPS、80/443 防火墙规则，再从公网验证域名、网页和数据接口。训练原文和导出文件保留在私有目录，通过 SSH 转移。
