# 观澜腾讯云运行与数据交接

截至 2026-11-18，网页通过本机 SSH 隧道访问。Nginx 在服务器上监听 `127.0.0.1:8769`，静态目录为 `/srv/ashare-radar-preview/public`。最后一份 GitHub Pages 数据作为展示基线。行情、资讯、模型、研究和数据集进度分别用独立 JSON 更新。

## 定时任务

所有时间均为北京时间。云端模型使用固定训练产物，`RADAR_ALLOW_TRAINING=false`。训练与调参在本地完成。

| 工作 | systemd 单元 | 时刻 |
| --- | --- | --- |
| SXData 行情与当日分时 | `ashare-preview-relay.service` | 交易时段按 6 秒窗口串行轮询，失败进入冷却队列 |
| 即时资讯及 MiniMax | `ashare-preview-news@collect.timer`、`ashare-preview-news.path`、`ashare-preview-news@analyze.timer` | 09:00–16:00 每 2 分钟采集，其他时间每 20 分钟采集。新资讯入库触发分析，分析定时器续处理 |
| 日线、成分和已训练模型 | `ashare-preview-daily.timer`、`ashare-preview-reference.timer`、`ashare-preview-model.timer` | 按已发布数据分批更新，盘中保存对应分钟的观测，已有模型仅推断 |
| 盘后研究和复盘 | `ashare-cloud-research.timer` | 工作日 17:26、19:38、21:22，程序再次检查是否为交易日 |
| 历史快讯采集 | `ashare-training-collect.timer` | 09:00–16:00 每 10 分钟处理一个窗口，其余每分钟一个窗口 |
| 历史快讯标注 | `ashare-training-label.timer` | 每批结束后等待 1 分钟再处理下一批，每批最多 32 篇 |
| 私有数据集导出 | `ashare-training-export.timer` | 每小时第 47 分钟 |

盘后研究复用现有行情校验、估值风险指标、个股模型和持仓聚合、ETF 数据及预测台账模块。成分按日期记录并轮换刷新。资金流以 SXData `moneyflow` 的实际盘后回包为准。研究投影记录发布时刻、市场覆盖和各子任务状态。订阅接口或数据迟到会体现为待更新，日后按同一时间口径继续处理。

## 历史新闻范围与导出

SXData 限流时，历史采集暂停到冷却结束。`collection_priority_days` 中指定的日期先补采，再处理此前尝试过的日期及其拆分窗口。补采队列全部处理后才查询尚未开始的日期。连续收到限流时，按 1、2、4、8、15 分钟退避，并遵循数据源返回的更长等待时间。共享冷却拦截的调度不计入日期请求次数。看板逐秒显示冷却剩余时间，待采集用浅紫色，正在等待源回包用深紫色。

历史标注每次发起一个 MiniMax 请求。批量模式使用 `MiniMax-M3.1-Flash` 的 `low` 思考档位，每篇按独立 ID 返回结构化 JSON。解析时将证据编号还原为本篇原文并校验。批量结果的 `label.annotation_prompt` 标记为 `fulltext-sw31-impact-v2-refs+structured-batch-v3`。格式失败的条目可合批重试一次，仍未通过校验则逐条重试。传输超时后至少等待 5 分钟，再将重试批量减半，最少 1 篇。成功后恢复正常批量。看板显示近 10 分钟实际成功入库的标注速率。

范围为 **2026-05-22 00:00（含）至 2026-11-18 00:00（不含）**，180 个北京时间自然日。来源为 SXData 的 `news`、`src=cls`（财联社快讯），保存接口返回的快讯正文。当返回达到 1,500 条或超过响应大小时拆分窗口。同一日期的规范化标题与正文均相同才合并正文，同标题的不同正文各自保留。原始来源记录列入 `source_occurrences`，按各自发布时间和抓取时间留存。主记录 `published_at` 取该内容最早的已观察来源时间。模型标签逐条校验证据来自本篇正文，原模型实际接受的发布时间留在 `label.input_published_at`。

| 路径 | 内容 |
| --- | --- |
| `/var/lib/ashare-radar-preview/training-news/dataset.sqlite3` | 私有原始数据、窗口队列、标注、错误和 token 计量 |
| `/var/lib/ashare-radar-preview/training-news/export/news.jsonl.gz` | 可转移至另一台机器的压缩 JSON Lines 数据 |
| `/var/lib/ashare-radar-preview/training-news/export/manifest.json` | 导出范围、条数、完成状态及压缩文件的 SHA-256 |
| `/var/lib/ashare-radar-preview/training-news/final/2026-05-22_to_2026-11-17/` | 180 天复核与全部标注完成后，定时任务自动发布通过 SHA-256 和记录数校验的正式数据与清单 |
| `/training-progress/` | 网站进度看板，显示每日覆盖和聚合计数 |

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
scp ubuntu@TENCENT_HOST:/var/lib/ashare-radar-preview/training-news/final/2026-05-22_to_2026-11-17/news.jsonl.gz .
scp ubuntu@TENCENT_HOST:/var/lib/ashare-radar-preview/training-news/final/2026-05-22_to_2026-11-17/manifest.json .
```

## 域名

腾讯云控制台显示 `mystockmarket.online` 未备案。上海实例在备案通过后配置主域名及需要的 `www` 记录、HTTPS、80/443 防火墙规则，再从公网验证域名、网页和数据接口。训练原文和导出文件保留在私有目录，通过 SSH 转移。

## 从公开备份恢复中转服务

在服务器上创建权限为 0600 的 `/etc/ashare-radar-preview/relay-identity.env`，写入 `RADAR_EXPECTED_STOCK_KEY_SHA256=<SHA-256>`，值为去除首尾空白后的 SXData 凭据的 SHA-256。
