# 中国 A 股研究网站与本地训练交接文档

> 2026-11-17 最新补充：用户确认以整体指数、板块下一交易日收盘为主，并授权MAE未达0.1个百分点时部署开发验证中最好版本。已完成小批特征诊断和追加两轮优化；当前模型、实际误差、JSON推断对接和限制见 [DAILY_FORECAST_V4.md](DAILY_FORECAST_V4.md)。本文以下保留原交接快照；其“训练尚未开始”等状态不是最新进度。

更新日期：2026-11-16。面向接手本项目的本地开发助手及维护者。

本项目下一步是：在用户 Windows 电脑上完成全市场股票模型训练和调优，核验结果，将合格模型包上传到现有 GitHub 仓库，再确认网站实际启用。用户已经授权在本地训练、调优和上传；符合下述条件时可继续执行，无须重复询问泛化的上传许可。网页只做必要的小幅调整，GitHub Actions 负责采集、推断、结算和发布，不承担生产模型训练。

**截至本文核验时，本机训练尚未开始。** 当前会话运行在云端 Linux，无法访问用户的 `C:\Users\12291\Desktop\QuantModel` 或使用其 GPU。已有脚本和自动化测试通过，不等于已经在用户 Windows 电脑运行成功。本文记录的早期 CPU 实验也不是用户本机的训练成绩。

## 1 接手入口与当前状态

| 项目 | 地址或状态 |
| --- | --- |
| 代码仓库 | https://github.com/Shihaoling/a-share-radar |
| 正式网站 | https://shihaoling.github.io/a-share-radar/ |
| 唯一工作流 | `.github/workflows/update-and-publish.yml`，名称 `Update research and publish` |
| 工作流记录 | https://github.com/Shihaoling/a-share-radar/actions |
| 用户训练目录 | `C:\Users\12291\Desktop\QuantModel` |
| 预期本地仓库 | `C:\Users\12291\Desktop\QuantModel\a-share-radar` |
| 用户提供的硬件 | 内存 64GB，显存 8GB；具体 CPU、GPU、驱动必须在本机检测 |
| 本文对应功能代码 | `f739033472e306617f8e2fa3bc8b17e38d2c0f98` |
| 核验的数据提交 | `233af6d005245d34dd4f456d2e7f065510637d57` |
| 最近已核验发布 | Actions run `43880237832`，research 和 deploy 成功；当次测试 288 项通过 |
| 当前线上逐股模型 | `zero_return`，状态 `baseline`；尚无启用的本地神经部署包 |
| 本地训练技术说明 | [EQUITY_MODELS.md](EQUITY_MODELS.md) |

接手时先 `git fetch` 并核对新的 `main`；上表是交接快照，不应覆盖随后产生的新代码或行情。本文路径均相对于仓库根目录，除非明确写为 Windows 绝对路径。

2026-11-16 21:54 北京时间的数据快照：6 个市场指数、31 个申万一级行业；逐股日线归档 180 个交易日，范围 2026-02-13 至 2026-11-16；当前股票池 5,572 只，其中 5,565 只产生基线输出，已知流通市值权重覆盖约 99.9928%。剩余股票缺少可用输入，不补造预测。按历史档案训练时出现的股票数可以与当日上市股票池不同。行业分类完整，但成员完整标记为 30/31；交通运输有一条无效代码被排除，不能把分类完整等同于成员完整。个股已结算结果为 0，尚无真实逐日结算准确率。

行业基金有 116 个匹配候选，实际展示 61 张基金卡，覆盖 24/31 个行业。访客统计当前 `enabled=false`，未接入可用 GoatCounter 站点，不能声称已经统计了访客人数。

## 2 给本地助手的直接任务说明

可把以下文字连同本文件交给具备本机文件、终端和 GitHub 权限的助手：

> 请接手 Shihaoling/a-share-radar。先读 docs/HANDOFF.md、docs/EQUITY_MODELS.md 和本机已有 AGENTS.md，检查用户现有文件及运行中的任务，不覆盖用户改动。使用 C:\Users\12291\Desktop\QuantModel，实际检测 64GB 内存及 8GB 显存对应设备。在本机准备环境、核验历史数据、运行带进度页的训练和验证；已有合格运行结果时优先核验，不重复训练。用户已授权训练完成后直接上传。只有固定选择且通过双目标验证和确认门槛的完整 upload-ready 包，才上传到该仓库的 data/equity/models/。使用独立发布检出目录，保留旧基线和历史预测，不提交凭据、虚拟环境、私人日志或整套本地实验。保持 GitHub 生产训练关闭。上传后跟踪 research、deploy 和网站实际模型状态，给出运行目录、设备、指标、提交 SHA、Actions 链接及是否已启用。若模型未达标，保留当前生产模型并如实报告；不要改写报告或降低门槛来完成上传。遇到缺失数据、设备或登录条件，先完成其他可独立执行的工作，再说明具体阻塞。

完成标准是“本地结果可复现、部署包可校验、仓库提交成功、网站状态已核实”，不能把其中一个步骤替代全部步骤。训练程序不会自行执行 `git push`；本地助手需要执行第 6 节的发布操作。

## 3 本地准备与启动

### 3.1 保留现场并确认硬件

先查看 `QuantModel` 中已有的仓库、`models`、`logs` 和运行进程。若有正在训练的任务，查看其 `progress.json` 和日志，避免重复占用显存。不要删除已有实验，不要执行 `git reset --hard`、`git clean -fd` 或强制推送。

需要 Git、64 位 Windows Python 3.10–3.13，推荐 Python 3.12；GPU 训练还需要兼容的 NVIDIA CUDA 设备和驱动。8GB 显存这一信息本身不能证明支持 CUDA。脚本支持 Windows PowerShell 5.1；本交接时未在用户 Windows 真机执行，只完成静态检查及 Linux Python 测试。建议安装 Node.js 以执行前端契约检查；缺失时相关测试会跳过，需要在验证记录中说明。

```powershell
$QuantRoot = 'C:\Users\12291\Desktop\QuantModel'
$RepoPath = Join-Path $QuantRoot 'a-share-radar'
$env:PYTHONUTF8 = '1'
New-Item -ItemType Directory -Path $QuantRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $RepoPath)) {
    git clone https://github.com/Shihaoling/a-share-radar.git $RepoPath
    if ($LASTEXITCODE -ne 0) { throw 'Clone failed' }
}
Set-Location $RepoPath
git status --short
git remote -v
git log -1 --oneline
py -0p
Get-CimInstance Win32_Processor | Select-Object Name,NumberOfLogicalProcessors
Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi }
(Get-ChildItem data\equity\daily -Filter '*.json.gz' -File).Count
```

已有仓库仅在工作区清洁、分支和远端正确时执行 `git pull --ff-only origin main`。有本地修改则保留并检查，或另建检出目录。克隆或更新后应确认 180 个日线文件是否仍与交接快照一致；以后数量和日期会变化。`PYTHONUTF8=1` 仅设置当前终端及其子进程，避免部分未显式指定编码的读取在 Windows 默认编码下出错；新开终端执行训练或测试时需重新设置。

### 3.2 Windows 标准启动

从仓库根目录运行。已安装可用 PyTorch 时，使用自动设备检测：

```powershell
.\scripts\train-windows.ps1 `
  -Workspace 'C:\Users\12291\Desktop\QuantModel' `
  -Device auto -MaxStocks 0 -Hidden 128 -Epochs 20 `
  -BatchSize 0 -CpuThreads 0 -ExitAfterTraining
```

首次需要安装 CUDA 版 PyTorch，且本机已确认是支持 CUDA 的 NVIDIA 显卡时：

```powershell
.\scripts\train-windows.ps1 `
  -Workspace 'C:\Users\12291\Desktop\QuantModel' `
  -InstallCudaTorch -Device cuda -MaxStocks 0 `
  -Hidden 128 -Epochs 20 -BatchSize 0 -CpuThreads 0 `
  -ExitAfterTraining
```

`-InstallCudaTorch` 安装脚本固定的 PyTorch 2.8.0 / CUDA 12.6 官方构建，并做实际 CUDA 计算检查。不要因为命令包含 `cuda` 就报告 GPU 已工作；以检查结果和 `resources.json` 为准。`-Device cuda` 不可用时会失败；`auto` 可以回退 CPU，并明确记录实际设备。PyTorch 缺失且未选择安装时，脚本会停止并显示安装命令。

脚本创建独立环境 `<QuantRoot>\.venv-a-share-radar`，不会修改全局 Python，也不会修改全局 PowerShell 执行策略。若该目录已存在却缺少脚本所有权标记，脚本拒绝覆盖；查明来源后选择其他工作目录或明确复用可用环境。只有执行策略阻止已审阅脚本时，才可使用一次进程范围的 `powershell -NoProfile -ExecutionPolicy Bypass -File ...`，不要全局放宽策略。

**自动化助手建议保留 `-ExitAfterTraining`。** 默认不加它时，训练结束后进度服务器继续运行，终端不退出是正常行为。用户手动使用并希望训练后继续看页面，可以省略该参数；完成后 `Ctrl+C` 只关闭监听，结果保留。

Windows 启动器默认开启调优。直接调用 Python 入口时需要显式添加 `--tune`：

```powershell
$TrainPython = Join-Path $QuantRoot '.venv-a-share-radar\Scripts\python.exe'
$RunPath = Join-Path $QuantRoot ('models\manual-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& $TrainPython -u -m pipeline.local_training `
  --output-dir $RunPath --device auto --max-stocks 0 `
  --hidden 128 --epochs 20 --batch-size 0 --cpu-threads 0 `
  --tune --exit-after-training
if ($LASTEXITCODE -ne 0) { throw 'Local training failed; inspect saved progress and logs' }
```

### 3.3 资源策略与训练进度

CPU 自动线程数为 `min(16, max(1, 逻辑核数-2))`，可通过 `-CpuThreads` 调整。CUDA 会短测 batch 128、256、512、1024、2048，用 GRU 和 TCN 的实际前向、反向及优化器开销选择安全且吞吐较好的批量。显存预算取“当前空闲显存的 70%”与“总显存的 80%”中的较小值。探测使用随机张量，仅测硬件，不计入预测成绩。CPU 自动批量按可用资源选择 128 或 256。

目标是提高实际样本吞吐，同时给系统保留资源，不要求把内存或显存占用硬拉到 100%。现有训练没有自动混合精度、梯度累积、多 GPU 或分布式训练；如要增加，属于新实现，必须重新验证数值、保存加载和推断一致性。

训练自动打开 http://127.0.0.1:8768/ ，页面每 2 秒读取真实进度，显示配置试验、模型、epoch、当前批次、训练损失、验证 MAE、候选排名、样本吞吐、CPU 线程和显存。它不会编造全流程 ETA；60 秒无更新只是提示可能等待或暂停，数据预处理阶段也可能较久。

监控仅绑定本机回环地址。公开网站的 `training-monitor.html` 是界面文件，无法显示用户电脑的训练；不要把公开网址交付成“本地训练正在运行”的证据。8768 被占用时先确认占用者，或指定 `-Port 8769`，不要随意杀掉用户其他进程。

### 3.4 输出与中断处理

每次 Windows 启动器生成独立运行 ID。重要文件如下：

| 位置 | 内容与用途 |
| --- | --- |
| `<QuantRoot>\logs\<run-id>\training.log` | 本地安装、预检及训练日志 |
| `<QuantRoot>\models\<run-id>\resources.json` | 真实硬件、设备和批量探测结果 |
| `progress.json` | 阶段、轮次、指标、错误和更新时间 |
| `trial-01`、`trial-02`、`trial-03` | 各配置验证报告及候选权重 |
| `selection.json` | 确认测试之前固定的模型、配置及集成权重 |
| `final\training_report.json` | 最终固定模型的验证与确认报告；另有连字符兼容文件名 |
| `local-summary.json` | 是否完成、是否合格、实际执行环境、是否存在导出包 |
| `upload-ready` | 只有达标并导出成功时生成的完整部署包 |

训练目录中已有 `progress.json` 时拒绝覆盖。当前**不支持从中断的优化器状态或上一批次自动续训**；保留旧目录后启动新运行。行情下载支持按交易日断点续传，不要将它与神经训练续训混淆。

仅重新查看保存的进度，不训练：

```powershell
& $TrainPython -m pipeline.local_training --output-dir $RunPath --serve-only
```

## 4 训练数据和模型逻辑

### 4.1 数据准备与更长历史

`data/equity/daily/YYYYMMDD.json.gz` 每个文件保存一个交易日的全市场有效个股日线。现有 180 日足以打通首轮流程，但减去 60 日窗口后只有约 120 个样本日期，不足以证明跨牛熊、跨年度的稳定性。可在本地先完成小规模运行确认环境，再扩大到全部股票；正式研究宜补充更长历史并做滚动验证。

“180 日”是项目初始预算，不是接口历史上限。“本站历史不足”表示本地归档不足，不表示服务不提供历史。当前训练只读取已归档数据，不访问行情 API，也不需要 Token。

采集器可独立限量扩展历史。先安全配置本机 `STOCK_API_TOKEN` 环境变量，再使用：

```powershell
& $TrainPython -m pipeline.equity_data --days 750 --max-requests 90 --min-interval 6
```

每轮最多 90 次是示例预算，不是必须用满；已有有效日期自动跳过。当前日期枚举依赖已核验的交易日历或指数历史，若这些只覆盖近期，单改 `--days` 无法凭空得到三年日期，需要先补足日历或可信指数历史。不要按周一到周五猜交易日。

采集状态在 `data/equity/state.json`；日文件和状态按批次原子写入。明确权限或限频失败不重试；未知业务错误或连接失败每轮合计最多等待 30 秒后重试一次。尊重冷却，不删除状态文件来绕过限制，不用代理轮换或身份伪装规避限流。

重要数据约束：日线为未复权价格；`vol` 为手，`amount` 为千元；`daily_basic` 市值从万元乘 10,000 转为元。无成交的停牌行、重复、错误日期、截断批次需按采集器规则处理。`adj_factor` 当前未完整归档，不得宣称已拥有完整复权序列。历史成分归属和退市覆盖不完整，存在股票池及幸存偏差。

### 4.2 先逐股预测再聚合

所有股票共用一套模型参数，但每只股票输入自己的历史窗口并生成自己的输出。无需分别训练五千多个网络，也不能把指数预测复制给每只股票。之后按预测时已知的 `circ_mv` 加权汇总行业，并对全市场股票去重。

聚合收益为 `sum(可用股票的流通市值 × 个股预测收益) / sum(可用股票的流通市值)`。同时展示股票数量覆盖、权重覆盖和缺失权重；最低权重覆盖设置为 0.9。该结果是**成分股研究组合**，不等于官方上证、沪深 300 或申万指数的精确点位预测。

两个神经网络目标均以百分比为单位：

```text
next_open_return_pct  = 100 × (目标交易日 open  / 目标交易日 pre_close - 1)
next_close_return_pct = 100 × (目标交易日 close / 目标交易日 pre_close - 1)
```

供应商 `pre_close` 是目标日的除权参考价，可能不同于上一日原始收盘价。它在预测时可能尚未知，因此模型输出收益率，不将其简单乘昨日价格冒充准确的次日绝对开盘价。标签必须属于下一全市场交易日，不能跳过停牌多日当成“次日”。

### 4.3 模型与参数

| 模型 | 当前实现 | 作用 |
| --- | --- | --- |
| Zero | 两个目标均为 0 | 必须比较的零收益基准，无涨跌方向判断 |
| Ridge | 离线候选 alpha=10，窗口最后值、均值、标准差合计 24 维 | 便宜且可复现的基准 |
| GRU | 60 日 × 8 维量价特征，2 层，dropout 0.1，hidden 32/64/128 | 时序神经候选 |
| TCN | kernel=3，dilation=1/2/4/8，每块两次因果卷积，感受野 61 日 | 卷积时序候选 |
| Ensemble | `w × GRU + (1-w) × TCN`，w 从 0.25/0.5/0.75 选择 | 验证集选择的固定权重集成 |

默认 Windows 调优比较 hidden32/lr0.0003、hidden64/lr0.0003、hidden128/lr0.0001 三组。每组比较基准、GRU、TCN 与集成。AdamW、weight decay 0.0001、Huber loss、梯度裁剪 1，最多 20 轮，验证连续 4 轮无改善早停；种子固定为 42。直接使用 `pipeline.train_equity` 的默认参数不同，不能把其 hidden32/10 轮/200 只股票与 Windows 全市场默认混淆。

特征是标准化量价序列，不含可回填的历史新闻、财报或估值。窗口按需生成，不预展开全部“股票 × 日期 × 60 × 8”张量；仍需为全市场多年档案解析预留内存。独立生产基线 `shared_baseline.json` 使用另一套 16 维特征和 Ridge alpha=100，不能把它的报告与离线 24 维 Ridge 混在一张无说明的成绩表中。

### 4.4 时间切分和候选晋升

所有股票使用统一日期边界：至少 60 个训练日期，20 个验证日期，20 个确认测试日期，两段边界各隔 1 个交易日。缩放器仅在训练段拟合；验证集用于早停、模型和配置选择。调优阶段 `test_metrics_accessed=false`，不计算确认成绩。先保存 `selection.json`，再按相同配置、种子和固定集成权重执行最终确认。

报告含 MAE、RMSE、方向准确率、方向有效样本数、日期范围及设备。误差字段 `mae_pct` 使用百分点，例如 0.80 表示 0.80 个百分点误差；它不是“80%准确率”。预测或实际收益为零的记录不参与方向判断，零基线方向准确率为空。样本数是股票与日期的组合数，不是独立交易日数。

部署资格是四个条件同时成立：对下一开盘和下一收盘两个目标，分别在验证与确认集合上，候选 MAE 都严格小于 `min(Zero MAE, Ridge MAE) × 0.98`。即相对最佳基准改善超过 2%，而不是“命中率达到 98%”。最终赢家必须是 GRU、TCN 或 Ensemble；基准胜出时不生成神经部署包。

单次切分只构成当前软件的部署资格，不等于已证实交易收益。多时间段滚动检验、多种子、行业及市值分组稳定性、交易成本回测尚未完整实现。任何反复查看确认段后的调参，都应使用新的未参与选择的数据进行确认，不能继续把原测试段称为独立样本外结果。

早期云端 CPU 试验只用了 256 只按代码排序的股票、4 轮。其 TCN 验证选择后，确认开盘 MAE 约 0.8197，对比 Ridge 0.8056；确认收盘 MAE 约 2.1004，对比 Zero 2.0128，因此未晋升。它用于验证流程，不能充当用户本机全市场成绩。

## 5 判断训练结果是否可上传

必须同时检查文件内容与真实模型加载，不能只检查“程序退出码为 0”或存在 `.pt` 文件。

1. `local-summary.json` 为 `status=completed`、`execution=local`，`eligible_for_promotion=true`。
2. `final/training_report.json` 有固定选择、确认测试和两个基准；`confirmation_selection_fixed=true`、`test_metrics_accessed=true`，指标满足第 4.4 节。
3. `selection.json` 的模型、配置及集成权重与最终报告一致。
4. `upload-ready` 存在，使用正式加载器在 CPU 上成功加载并验证 SHA256、标签、维度、组件、缩放参数和报告一致性。
5. 本地运行期间未提前查看确认段后再针对同一确认段调参，未用当前新闻、财报或成分信息填造历史特征。

`local-summary.json` 中的 `uploaded=false`、`promoted=false` 是设计行为；导出的 `validation-report.json` 保留 `promoted=false`，没有 `uploaded` 字段。训练程序只产生候选，不执行 GitHub 上传或宣称生产生效。上传后另行记录提交及部署状态，不要手改报告里的标志来绕过导出检查。

先从启动器输出或日志确认本次实际运行目录，将 `$RunPath` 设为该目录的完整路径，例如 `Join-Path $QuantRoot 'models\实际运行ID'`；不要只取最近修改的文件夹，以免混入失败或其他实验。程序一般自动导出，也可显式导出到一个不存在的新目录：

```powershell
$QuantRoot = 'C:\Users\12291\Desktop\QuantModel'
$TrainPython = Join-Path $QuantRoot '.venv-a-share-radar\Scripts\python.exe'
$env:PYTHONUTF8 = '1'
$PackagePath = Join-Path $RunPath 'upload-ready'
if (-not (Test-Path -LiteralPath $PackagePath)) {
    & $TrainPython -m pipeline.deployed_equity --run-dir $RunPath --output-dir $PackagePath
    if ($LASTEXITCODE -ne 0) { throw 'No valid deployment package was produced' }
}
$env:RADAR_PACKAGE_TO_CHECK = $PackagePath
@'
import os
from pipeline.deployed_equity import load_deployed_model
b = load_deployed_model(os.environ['RADAR_PACKAGE_TO_CHECK'])
assert b is not None, 'Missing deployment manifest'
print('VALIDATED', b['kind'], b['model_id'], b['label_cutoff'])
'@ | & $TrainPython -
if ($LASTEXITCODE -ne 0) { throw 'Deployment package validation failed' }
Remove-Item Env:RADAR_PACKAGE_TO_CHECK
```

| 赢家 | 同一次提交必须包含的包文件 |
| --- | --- |
| GRU | `deployed-model.json`、`validation-report.json`、`gru.pt` |
| TCN | `deployed-model.json`、`validation-report.json`、`tcn.pt` |
| Ensemble | `deployed-model.json`、`validation-report.json`、`ensemble.json`、`gru.pt`、`tcn.pt` |

以实际导出包和清单列出的文件为准，不手工重命名权重、不拆成多个提交。每个模型资产限制 64MB、元数据限制 2MB；检查点仅保存状态和元数据，并用 `weights_only=True` 加载。只加载本项目可信流程生成的文件。

## 6 上传 GitHub 与核验网站

### 6.1 独立发布检出目录

训练目录可以保留实验与研究改动。发布使用一个新克隆，只提交完整部署包，避免把私有数据、临时文件或未验证代码一起推送。不要重新上传全部日线历史；如需单独扩充共享历史，应另做数据更新及体积、授权检查。

下面继续使用前文的 `$QuantRoot`、`$RunPath`、`$PackagePath`、`$TrainPython`。GitHub 登录采用本机已配置的 Git Credential Manager、GitHub CLI 或助手连接，不把令牌放入远端 URL、脚本或命令历史。

```powershell
$PublishPath = Join-Path $QuantRoot ('publish-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
git clone https://github.com/Shihaoling/a-share-radar.git $PublishPath
if ($LASTEXITCODE -ne 0) { throw 'Publish clone failed' }
Set-Location $PublishPath
$DeployPath = Join-Path $PublishPath 'data\equity\models'
$PackageFiles = @(Get-ChildItem -LiteralPath $PackagePath -File)
if ($PackageFiles.Count -eq 0) { throw 'Empty model package' }
$RelativeFiles = @()
foreach ($Asset in $PackageFiles) {
    Copy-Item -LiteralPath $Asset.FullName -Destination (Join-Path $DeployPath $Asset.Name)
    $RelativeFiles += ('data/equity/models/' + $Asset.Name)
}
& $TrainPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Repository tests failed' }
$env:RADAR_PACKAGE_TO_CHECK = $DeployPath
@'
import os
from pipeline.deployed_equity import load_deployed_model
b = load_deployed_model(os.environ['RADAR_PACKAGE_TO_CHECK'])
assert b is not None
print('READY', b['kind'], b['model_id'])
'@ | & $TrainPython -
if ($LASTEXITCODE -ne 0) { throw 'Copied model failed validation' }
Remove-Item Env:RADAR_PACKAGE_TO_CHECK
git add -- $RelativeFiles
if ($LASTEXITCODE -ne 0) { throw 'Staging failed' }
git diff --cached --stat
git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw 'Diff check failed' }
git status --short
```

逐项确认暂存区仅含上表部署包。旧 `shared_baseline.json` 和历史版本保留，供回退；不要删掉预测台账、历史行情或其他模型报告。代码或训练协议若在本机另有改动，必须先提交并测试相应实现，再导出与当前云端兼容的模型，不能只把新结构的权重塞入旧加载器。

确认包、测试和暂存范围正确后，本地助手可直接执行已获授权的提交与推送：

```powershell
git commit -m "model: publish validated local equity model"
if ($LASTEXITCODE -ne 0) { throw 'Model commit failed' }
git pull --rebase origin main
if ($LASTEXITCODE -ne 0) { throw 'Remote changed or conflict requires inspection; do not force push' }
& $TrainPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed after updating from main' }
$env:RADAR_PACKAGE_TO_CHECK = $DeployPath
@'
import os
from pipeline.deployed_equity import load_deployed_model
assert load_deployed_model(os.environ['RADAR_PACKAGE_TO_CHECK']) is not None
'@ | & $TrainPython -
if ($LASTEXITCODE -ne 0) { throw 'Model package is incompatible with updated main' }
Remove-Item Env:RADAR_PACKAGE_TO_CHECK
git push origin HEAD:main
if ($LASTEXITCODE -ne 0) { throw 'Push failed; inspect authentication or remote changes' }
git rev-parse HEAD
```

如远端在训练期间更新了模型加载器、特征或格式，rebase 后需要重跑相关测试和包加载检查再推送。若出现冲突，查明双方变化，不丢弃远端行情更新或强制覆盖。缺少写权限时说明需要完成的 GitHub 登录；这是认证阻塞，不是再次索要训练或上传许可。

### 6.2 发布后的四层验收

1. **仓库层**：远端 `main` 含完整包，记录提交 SHA、模型 ID 和包哈希；不要把本地 commit 成功当作 push 成功。
2. **Actions 层**：检查与提交对应的 `Update research and publish`，`research` 和 `deploy` 都成功。不能只看旧绿色运行，也不能凭一封失败邮件推断后续状态。
3. **数据层**：仓库文件为 `web/data/view.json`，对应[公开数据地址](https://shihaoling.github.io/a-share-radar/data/view.json)。确认 `equity_forecast.training.deployed_model.status == "active"`，并核对 kind、model_id、trained_at、预测目标日期、数量/市值覆盖及缺失原因。模型不能只是文件存在，实际股票输出还必须使用该 model_id、数值有限且沿用相同标签口径。
4. **网页层**：打开 `#forecast`、行业详情和个股列表，确认实际模型名称及数据日期；刷新静态 JSON 后仍使用旧模型时，应查原因，不直接标记“部署完成”。

主分支模型包提交会触发 Actions。它按清单安装 CPU PyTorch、校验资产、对缓存行情执行推断并发布，**不会重新拟合神经网络、重选参数或集成权重**。历史首次预测快照不可被新模型覆盖；新的合法目标生成新记录。

**一个需注意的时间限制**：当前训练报告未显式提供标签截止日时，导出器保守地以训练完成日期作为 `label_cutoff`。线上要求它不晚于预测参考交易日，并要求训练时刻不晚于当前时刻。因此在周末、节假日或参考行情尚未更新时，合法上传包也可能暂时无法启用。此时如实记录“已上传，尚未满足时点条件”；等待合法日期，或在有真实标签日期证据的前提下修正报告及导出逻辑并测试。不能手改为更早日期来绕过防泄漏检查。

损坏、不完整或不达标的包会报明错误并回退现有可用基线。回退不是新模型已正常上线。当前日线神经模型对 `today_close` 盘中任务仍会放弃输出，不能把此处空值当作加载失败。

### 6.3 回滚和交付记录

若新模型产生异常，在干净发布分支上对模型包提交执行 `git revert <model-commit-sha>`，检查后推送，等待新一轮发布。保留 Git 历史，不强推改写历史。回滚后同样核验数据和网页，不删除已经留存的历史预测。

本地助手最后应报告：真实设备；运行、日志、监控位置；日期范围与股票覆盖；验证和确认的双目标 MAE 与最佳基准；最终赢家、集成权重及是否达标；上传文件、提交 SHA、Actions 链接；线上模型 ID 和目标日期；未完成的具体原因。训练失败或未达标也是有效结果，应保留证据，不承诺一定能产生合格神经模型。

## 7 网站整体架构与文件地图

网站是静态 HTML/CSS/JavaScript 加 Python 批处理管线。GitHub Pages 只提供页面与已发布 JSON；浏览器不直接请求需要密钥的行情接口，没有常驻网站后端或交易下单服务。工作流运行 Python，写入观测和预测台账，生成轻量视图，再发布 `web` 目录。

| 文件或目录 | 职责 |
| --- | --- |
| `web/index.html`、`web/app.js`、`web/styles.css` | 页面、路由、图表、筛选、自选、解释弹窗、模型与数据状态 |
| `web/training-monitor.html` | 本机训练监控页面，与公开网站主界面分开 |
| `pipeline/run.py` | 全量、主源专用、分时专用、离线等入口和采集编排 |
| `pipeline/stock_api.py` | Tushare 兼容主源、SW 分类、成分、日线、估值、实时与新闻权限处理 |
| `pipeline/equity_data.py` | 按交易日采集全市场日线、质量校验、断点和冷却 |
| `pipeline/equity_model.py` | 逐股基线或部署模型推断、加权聚合、逐股预测快照与结算 |
| `pipeline/equity_update.py` | 股票历史、基金、缓存推断的有预算更新 |
| `pipeline/train_equity.py` | 离线神经训练、基准、验证、固定确认、模型安全加载 |
| `pipeline/local_training.py` | 本机硬件探测、调优编排、监控和合格导出 |
| `pipeline/training_resources.py`、`training_progress.py` | 真实资源探测、进度写入与回环 HTTP 服务 |
| `pipeline/deployed_equity.py` | 部署包导出、清单/哈希/报告检查、CPU 推断 |
| `pipeline/model.py` | 旧指数及行业直接预测的研究代码；云端禁训并仅复用合规缓存 |
| `pipeline/sources.py`、`fallback.py`、`intraday.py` | 新闻、备用行情、分时观测和故障降级 |
| `pipeline/sentiment.py` | 新闻归属、可见时间过滤、FinBERT 或规则情绪 |
| `pipeline/funds.py` | 券商关联管理人、行业基金匹配、价格/净值/规模 |
| `pipeline/calendar.py`、`validation.py` | 交易时段、目标日期、价格和来源一致性校验 |
| `pipeline/metrics.py`、`review.py`、`publish.py` | 估值与绩效口径、每日复盘、网页视图和健康摘要 |
| `pipeline/probe_stock_api.py` | 有预算的接口能力抽查，不代表全量接入 |
| `tests` | 日期、泄漏、行情、估值、模型、导出、监控、前端等回归测试 |

| 数据文件 | 作用与维护要求 |
| --- | --- |
| `web/data/dashboard.json` | 完整缓存、观测和台账；约 68MB 的当前快照，避免浏览器重复拉取 |
| `web/data/view.json` | 网页优先使用的精简视图；当前约 6.9MB，仍可进一步优化 |
| `web/data/health.json` | 来源、时间、接口权限、分类和覆盖质量摘要 |
| `web/data/reviews.json` | 按目标日期保存的复盘与晚到结算 |
| `web/data/tushare-capabilities.json` | 逐组、逐接口、带检查时间的能力证据 |
| `web/data/analytics.json` | 公开访客统计配置，不含私钥 |
| `data/equity/daily` | 个股训练日线档案 |
| `data/equity/forecasts` | 不可覆盖的每日首次逐股预测压缩快照 |
| `data/equity/models` | 旧可移植基线、历史实验报告和将来的完整部署包 |
| `data/equity/state.json` | 日线采集覆盖、质量和冷却状态 |
| `data/equity/bootstrap-request.json`、`bootstrap-complete.json` | 首轮补齐请求与完成标记；已有完成标记，不应反复重启初始补齐 |

不要手改已经结算的预测数值以提高历史准确率。模型版本变更、发布修复和数据晚到都应可追溯。

## 8 页面与业务口径

首页展示大盘指数、市场走势、来源时间、当前交易阶段及聚合预测。板块页使用 SW2021 一级行业，展示走势、主要股票、供应商估值、夏普和基金；股票可按行业筛选、搜索和分页。资讯页展示原文入口、来源分级、时间、关联产业和情绪；复盘面板汇总已结算预测误差、方向判断、情绪分布及数据覆盖。设置页展示来源状态、模型和接口能力。

### 8.1 行情和分时

优先显示有证据的当日收盘或有效实时行情，缺少当日数据时保留实际日期，不把昨日数据改成“今日”。来源报价时间与抓取时间分开；`retrieved_only` 只代表取回时间，不是已确认的实时行情。行情冲突需暂停受影响预测。

分时只记录功能运行后真实取得的点。点之间的连线不是分钟数据补全；不同来源分开，至少两个有效点才画趋势。市值曲线来自真实市值快照，不通过指数点位缩放伪造。ETF 代理价格不能替代申万行业指数或行业市值。

### 8.2 估值与基金

供应商 `sw_daily.pe` 单列为供应商 PE，不冒称归母 PE(TTM)。自算板块 PE(TTM) 应为可比成分市值之和除以归母 TTM 利润之和，PB 为市值之和除以归母净资产之和；不能平均个股倍数。保留亏损及负净资产贡献，分母非正或币种、日期、覆盖不足时给出不适用或样本组合说明。当前健康状态为 `vendor_ratios_only`，完整归母财务尚待补充。

夏普为 `sqrt(252) × mean(日收益 - 日无风险收益) / sampleSTD(日超额收益)`，标准差使用 n−1，最近最多 252、至少 60 个有效日样本；年无风险利率 2% 是配置假设。未复权价格算出的结果是价格收益指标，不是含分红总回报。点击页面指标旁问号查看公式、含义、日期及空值原因。

基金以 `etf_basic`、`fund_daily`、`etf_share_size` 为主要数据，管理人关系来自 `config/fund_managers.json` 中已记录的公开依据。华夏、广发、易方达、南方、华泰柏瑞等管理人分别标示券商控股或参股；不能统称为券商全资基金。

行业匹配目前以基金和跟踪指数名称为主，明确标记持仓待核验，最多三只并优先不同管理人。钢铁、纺织服饰、轻工制造、商贸零售、综合、煤炭、美容护理七个行业在交接快照没有合适卡片，不用无关宽基基金凑数。价格、NAV 和规模各带自己的日期；只有同日价格和 NAV 才计算折溢价。当前示例价格为 11 月 16 日，部分 NAV/规模为 11 月 13 日，不能混为当日数值。

### 8.3 情绪和每日准确性

中文金融新闻可用 `yiyanghkust/finbert-tone-chinese`，固定 revision `e91b1a3af10e1e8c9c03429d3cd7d5e9a1c8000d`，CPU 推断；英文或模型不可用时用明确标注的规则降级。没有适用证据是 unknown，不自动当作 neutral。传言默认排除，新闻首次采集时间和发表时间都参与可用性判断。

**有 FinBERT 代码不代表每次快照都用了 FinBERT。** 本文核验的最新摘要中 `model_used=false`、`model_inference_count=0`，实际采用规则/未知状态。核验每次执行应看 `sentiment_summary`、方法字段与日志，不以安装成功替代使用证据。

当前神经股票模型只有量价输入，新闻、财报、宏观接口成功不能等同于这些特征已经用于该模型。旧指数模型有时间点可得特征管线，但生产已禁训，尚无可对任意新输入拟合的在线直接模型。当前盘中“今日收盘”没有通过验证的神经模型，需要先积累带时间戳的分钟数据；日线模型在此目标主动放弃。

复盘只统计已到目标时点并拿到合规实际值的预测；晚到的结果回填原目标日期，未结算不能算错也不能算对。零收益无方向，不计方向准确率。情绪与后续价格的描述性一致率不是情绪分类准确率；没有人工标签时不应声称完成情绪准确性验证。

用户原始需求中的自动纠偏，目前拆为自动采集、自动结算和误差汇总，加本机重新训练后人工/助手发布。**生产不会每日自动重训。** 将来可在用户本机增加定期训练任务，仍需独立验证及版本回退；当前未配置这样的本地任务。

## 9 数据源能力与请求预算

主力服务 `https://t.xiaodefa.top/` 使用 Tushare 兼容 HTTP 协议。MCP、HTTP、Python SDK 是同一服务的三种访问方式，不是三个可交叉验证的独立源。它不是 Tushare 官方域名；15000 积分宣传不能证明每个接口均开放，也不能据此推断免费额度、独立上游或服务保证。以实际接口测试时间、参数和错误分类为准。

| 类别 | 已有证据及实际使用情况 |
| --- | --- |
| 行业和成分 | `index_classify(SW2021,L1)`、`index_member_all` 已接入，31 类完整 |
| 指数与股票行情 | `index_daily`、`sw_daily`、`daily`、`daily_basic` 已用于展示、归档或预测；交易日历使用 `trade_cal` |
| 基金 | `etf_basic`、`fund_daily`、`etf_share_size` 用于基金卡；`fund_basic`、`fund_nav` 有抽查成功证据 |
| 财报 | `income`、`income_vip`、`balancesheet`、`cashflow`、`fina_indicator`、`forecast` 抽查成功；`express` 当次查询为空，不等于权限被拒；尚未完成全市场历史财报特征管线 |
| 宏观与筹码 | `cn_cpi`、`cn_gdp`、`cyq_perf` 抽查成功；未纳入当前股票神经特征 |
| 实时与新闻 | 11 月 16 日的 `rt_idx_k`、`rt_sw_k`、`news` 及扩展 `major_news`、`cctv_news`、`anns_d` 查询被拒；按接口冷却，不能循环撞限 |
| 备用行情 | BaoStock、腾讯、新浪、东方财富；同标的、日期和复权口径才比较，存在未就绪和失效来源 |
| 资讯补充 | 官方/媒体 RSS、Federal Register、GDELT、国内财经入口；目录列出不等于已经成功采集或覆盖全量新闻 |

抽查成功只证明当时该参数可用，不等于任意日期和全量历史已验证。上述实时与新闻样本返回业务 403，程序按拒绝/冷却处理，具体服务方原因尚未完整识别，不能一律断言积分不足。交接时东方财富一个入口、GDELT、新浪分时存在错误或冷却，部分官方入口仍 pending；以 `health.json` 查看新状态，不能宣称所有备用来源始终可用。当前交叉核验摘要为 verified=0、single_source=6、not_comparable=78、conflict=0，接入多个备用渠道尚不等于已完成多源一致性确认。

| 采集路径 | 当前预算 |
| --- | --- |
| 主源常规更新 | 最多 12 请求/轮，最小间隔 3 秒 |
| 基金更新 | 最多 4 请求/轮，最小间隔 3 秒 |
| 个股日线常规更新 | 最多 2 请求/轮，最小间隔 6 秒 |
| 首轮日线补齐 | 有请求标记才执行，最多 180 请求并受时间预算约束；已经完成 |
| 分时专用更新 | 主源最多 2 次、备用最多 2 次，后者包含 robots 探测；有有效主源新报价时跳过重复备用 |
| 能力综合探测 | `audit_15000` 最多 20 请求；没有日常反复跑的必要 |

各子采集器预算独立，一次完整任务的总请求可能超过 12；不要把 12 解释成整个网站所有来源的统一上限。保留缓存、间隔、失败退避、429/Retry-After 和接口冷却。新闻没有“免费全量全球资讯”保证；不得通过伪装身份、绕过验证码或付费墙弥补权限缺失。

令牌仅放本机环境变量或 GitHub Actions Secret `STOCK_API_TOKEN`。本文不包含令牌。禁止把它写进公开配置、前端、日志、截图、工作流输入或 Git 历史。

## 10 配置 访客统计和运维

| 配置 | 当前关键设置 |
| --- | --- |
| `config/settings.json` | `training_enabled=false`；`equity.enable_training=false`；主源开启；新闻模型每轮最多 120 篇；传言排除；时区 Asia/Shanghai |
| 工作流业务步骤环境 | `RADAR_ALLOW_TRAINING=false`，阻止云端拟合；单元测试中的小型合成训练用于测试，不是生产重训 |
| `config/sectors.json` | 旧主题目录归档，当前行业以 SW2021 接口为准 |
| `config/sources.json` | 来源配置、频率、缓存及预算 |
| `config/proxies.json` | 备用来源、ETF 代理映射和核验轮转 |
| `config/fund_managers.json` | 券商与基金管理人的关系及证据 |
| `web/data/analytics.json` | 当前关闭，endpoint/dashboard_url 为空 |

浏览器设置只改变本机显示偏好，不会自动改仓库后端配置。原有 `retrain_days=7` 等字段仍保留，但在禁训开关下不会触发云端拟合。不要为“自动纠偏”擅自打开它们。

GoatCounter 接入需要用户已注册站点的公开计数 endpoint 和私有统计后台地址，之后修改 `analytics.json` 并发布；前端不得放管理 API Token。页面浏览与单独事件分开统计，所谓访客是供应商会话估计，不是精准自然人数。当前前端不会读取私有后台统计，因此启用埋点也不等于网站能自动显示所有统计数值。交接时统计尚未启用，无历史访客结果可核验。

当前仅一个工作流，包含 `research → deploy`。北京时间工作日 09:07–15:37 每半小时完整更新，额外行情任务合并成交易期间约 5 分钟最佳努力采样；工作日 18:17 补采行业收盘，每日 21:17 资讯更新。休市、午休及盘前盘后会跳过仅行情任务。GitHub 调度可能排队、延迟或跳过，不能承诺秒级实时。

页面每 60 秒读取已发布 JSON；点击刷新不直连金融网站。缓存未更新时价格不变是正常的。普通代码 push 主要校验并发布缓存；手动或定时完整任务才采集。数据机器人提交带 `[skip ci]`，避免数据更新循环触发。模型目录没有被忽略，因此完整模型包提交能触发推断和发布。

2026 年交易日历已按官方安排核验；2027 年之前需要维护新年度日历，未知年度及跨年目标必须暂停预测。不能简单把工作日当成开市日。

## 11 常见问题与处置

| 现象 | 排查与处理 |
| --- | --- |
| Python、CUDA 或 Torch 不可用 | 查看脚本预检和真实设备；只在兼容设备安装对应构建；不能把 CPU 回退写成 GPU 训练 |
| 显存不足 | 关闭不必要的本地 GPU 任务或降低 BatchSize/Hidden；保留失败目录，启动新运行；不要盲目增大模型 |
| 训练页面打不开 | 确认同一 Windows 电脑上的进程及 LOCAL_PROGRESS 地址；公开网站不是本地监听；检查端口占用 |
| 训练完成但终端不退出 | 默认监控服务常驻；完成后 Ctrl+C，或自动化使用 ExitAfterTraining |
| 缺少历史或样本 | 查归档质量、上市/停牌、60 日窗口、可用日历及切分边界；不是只看文件数量 |
| 请求拒绝或 429 | 查看能力报告和 state 冷却；不清除冷却、不提高频率、不把拒绝一律当 Token 失效 |
| 训练完没有 upload-ready | 查看赢家及四个 MAE 门槛；基准胜出或神经不达标时这是正常结果 |
| 包加载失败 | 对照清单、哈希、目标、特征维度、组件和报告；重新由原始运行导出，禁止手改标志 |
| push 成功但网站仍基线 | 查对应 Actions、deployed_model、训练时点/label_cutoff、盘中目标和行情输入；不可只看 commit |
| research 失败 deploy 跳过 | 定位本次首个失败步骤及具体日志，修复后核实新运行；不用旧失败邮件推断新状态 |
| 新模型没有改历史预测 | 首次预测冻结是设计要求；新模型不能重写旧预测提升历史成绩 |
| PE 或情绪为空 | 核对日期、财务归母口径和覆盖、新闻时效及模型实际使用状态；缺证据保留未知 |

离线查看网站可在仓库根目录执行 `python -m http.server 8743 --bind 127.0.0.1 --directory web`，打开 http://127.0.0.1:8743/ 。它只展示缓存，不会自动采集或训练；不用时 Ctrl+C，文件保留。请勿将带密钥或实验日志的整个项目根目录作为公开 HTTP 根目录。

## 12 后续工作优先级与证据

优先完成本机真实设备核验、全流程运行和结果交付。随后扩充历史日期，设计滚动时序及多种子验证；若验证不达标，继续保留零收益/线性基线。再逐项建立复权及公司行动、历史成员、退市覆盖、按实际公告可得时间归档的财报和新闻特征。只有在样本外验证有增益后，才加入逐股神经输入。

盘中收盘预测需独立分钟数据、目标定义及回测。行业基金可进一步用真实持仓核验映射。访客统计仍需有效站点配置。QMT 尚未接入，也没有东方财富当日全量资讯或社交平台传言全覆盖。网页可以补充本地模型版本、训练日期和覆盖提示，整体界面无需重做。现有约 6.9MB 轻量视图和较大的 Git 数据历史也可后续优化，但不要在本次模型上传中混入无关迁移。

本文件依据仓库代码、配置和以下已保存证据编写：

- `web/data/health.json`、`view.json`：实际数据、模型、情绪和来源状态。
- `web/data/tushare-capabilities.json`：2026-11-16 分组抽查结果。
- `data/equity/models/baseline_report.json`、`training_report.json`：早期基准和 CPU 试验。
- `.github/workflows/update-and-publish.yml`：唯一发布工作流及禁训设置。
- [已核验发布运行](https://github.com/Shihaoling/a-share-radar/actions/runs/43880237832)。

接手者交付时请追加自己的实际运行与部署记录，保留日期、提交、模型 ID 和限制，避免把本文历史快照当成持续更新的实时状态。
