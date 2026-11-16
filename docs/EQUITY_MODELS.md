# 股票级共享模型与本地训练

本地助手从环境检查到上传、上线验收的完整操作，以及网站架构、当前状态与已知限制，见[完整交接文档](HANDOFF.md)。

网站先为每只股票计算预测收益，再按预测时点已知权重汇总板块与全市场。共享模型表示所有股票共用模型参数，每只股票输入自己的历史窗口并获得自己的输出，不是把指数预测复制给成分股，也不需要分别训练数千个网络。

## 输入和目标

历史文件位于 `data/equity/daily/YYYYMMDD.json.gz`，每个文件的 `rows` 保存个股日线。训练入口只使用已归档量价数据；当前新闻、当前估值和当前行业名单不会回填成历史特征。公开输出单位为百分比：

```
下一开盘收益 = 100 × (下一交易日 open / 下一交易日 pre_close − 1)
下一收盘收益 = 100 × (下一交易日 close / 下一交易日 pre_close − 1)
```

标签使用目标交易日供应商给出的 `pre_close`，避免直接用昨日原始收盘价造成除权伪收益。该参考价格可能在预测时尚未知，因此模型只承诺收益率口径，不能将收益率直接乘昨日价格冒充可比绝对价格。停牌跨日样本不视为次日样本。

日线模型不能代替“盘中实时价格至今日收盘”的模型；后者需要独立记录并验证带时间戳的盘中历史。当前序列特征与数据质量约束以 `pipeline/equity_model.py` 为准。

## 首轮模型

| 模型 | 默认设置 | 用途 |
| --- | --- | --- |
| 零收益 | 两个目标均为0 | 最低基准 |
| Ridge | 试训alpha=10、24维摘要；线上基线alpha=100、16维量价特征 | CPU 共享基准 |
| GRU | 60日×8特征，hidden32，2层，dropout0.1 | 小型循环序列候选 |
| TCN | channels32，kernel3，dilation1/2/4/8，每块2次因果卷积 | 感受野61日的卷积候选 |

训练默认 batch128、AdamW 学习率3e-4、weight decay1e-4、Huber loss、梯度裁剪1、最多10轮、验证集4轮不改善即停止。可测试 hidden64/128及更大batch，但必须测量实际显存，不能保证所有8GB显卡的批量上限相同。

64GB内存/8GB显存是上述小模型的设计目标，不表示已经连接或使用用户本机GPU。本命令在调用它的运行环境执行；报告记录实际 `device` 与CUDA峰值分配显存，CPU运行时该显存项为空。`--device auto` 优先CUDA，否则CPU；不会自动把任务传送到另一个电脑。

## 可直接运行

先查看网页“个股模型 · 训练与核验”的已归档交易日数。**“本站历史不足”表示已经下载并通过校验的数据不足，不表示Tushare没有历史。** 官方 `daily` 文档说明提供全部历史，支持按交易日获取全市场；本站首轮180个交易日是采集和试训的预算目标，不是接口历史上限。停牌日不保证有记录，新股也不具有上市之前的历史。

接口拒绝、限频或连接失败后，`data/equity/state.json` 保留冷却时间；冷却未到不重复请求，也不清除冷却来绕过来源限制。未知业务错误与明确权限/限频错误分别记录。每轮对连接或未知业务错误合计最多重试一次、等待30秒；明确权限或限频错误不重试。GitHub Actions会在后续任务断点续传。训练样本未足够时股票预测保持待生成。

2026-11-16的首次补齐停在14日，第15批（2026-10-27）返回业务错误。旧程序没有保留服务方错误码和分类，不能确定原始原因；它设置的一小时冷却也不代表服务方限制。当天21:05（北京时间）按同一全市场参数复核该日期成功，收到5,571行，其中1条停牌无成交记录排除，保留5,570行。补齐从已验证成功的日期继续，并改为至少6秒间隔。官方服务与当前兼容服务的历史覆盖及权限仍应分别核验，单次成功不能证明兼容服务所有接口都具有官方的完整权限。

本机配置 `STOCK_API_TOKEN` 环境变量后，可分批补充更长历史（命令不回显令牌，已有完整日期自动跳过）：

```bash
python -m pipeline.equity_data --days 750 --max-requests 90 --min-interval 6
```

这条命令需要已有市场日历覆盖目标日期；不足时只采集已核实日期，不构造交易日。不要把令牌写进网页或提交到仓库。

先在项目目录安装基础依赖，完成历史采集后验证便宜基线：

```bash
python -m pip install -r requirements.txt
python -m pipeline.train_equity --models ridge --max-stocks 200 --device cpu
```

安装可选PyTorch依赖后，执行有限规模的CPU试验：

```bash
python -m pip install -r requirements-equity.txt
python -m pipeline.train_equity --models gru,tcn --max-stocks 100 --epochs 3 --device cpu --cpu-threads 4
```

在用户有可用CUDA的本机运行：

```bash
python -m pipeline.train_equity --models gru,tcn --max-stocks 500 --epochs 20 --hidden 64 --batch-size 128 --device cuda
```

`--max-stocks 0` 才选择全部归档股票；默认200只是按股票代码排序截取的可控试验，不代表全市场覆盖或无偏抽样。正式研究建议至少3年历史，先300–500只股票打通流程，再扩展全市场。180日首次归档减去60日窗口后只有约120个样本日期，只足以运行有限的训练/验证/测试试验，不能证明跨市场环境稳健性。

窗口按需读取，内存中只保存样本索引，不预展开全部 `股票×日期×60日×特征` 张量。Ridge只保存24维窗口摘要，神经训练批次才产生窗口。全市场多年的缓存、文件解析与特征生成仍会占用内存和运行时间。

## 验证与晋升

所有股票按同一日期边界切分；训练、验证、测试之间各隔1个交易日。默认最少60个训练日期，验证20日，测试20日。缩放器仅拟合训练段的历史观察值。验证集选择候选并早停；确定赢家后才评估保留的确认测试段。报告包含MAE、RMSE、实际和预测收益均非零时的方向准确率、实际纳入方向评估的样本数、日期范围、参数、时间和设备。零收益基线没有上涨/下跌判断，其方向准确率显示为空。

`eligible_for_promotion` 要求神经赢家在两个目标上，验证与确认MAE都比零收益和Ridge中的最佳基准严格改善超过2%。这仍只是单次切分候选资格，**训练程序不会自动切换线上模型**；报告的 `promoted` 始终为false。现已接入合格模型包的云端加载与每日推断，上传和网站实际启用须另行核验。多段滚动检验、多随机种子稳定性检验、按行业/股票规模的误差检查仍是后续研究工作。不能宣称未达标模型已经晋升。

结果写入 `data/equity/models/training_report.json` 和兼容名称 `training-report.json`；每次神经训练另存带时间戳的 `.pt` 候选检查点。检查点仅含模型状态、结构、训练段缩放参数、标签口径与验证元数据。调用接口：

```python
from pipeline.train_equity import load_neural_bundle, predict_neural_samples
bundle = load_neural_bundle("data/equity/models/gru_RUN_TIMESTAMP.pt", device="cpu")
predicted_returns_pct = predict_neural_samples(bundle, sequences)  # shape: N × 60 × 8
# 返回 N × 2，依次为下一开盘收益%、下一收盘收益%。
```

只加载自己生成和信任的检查点。现有可移植基线推断不依赖PyTorch；上传神经部署包后，工作流会安装CPU版PyTorch执行推断。2026-11-16的首次云端CPU试训已结束；按后续要求，工作流不再自动训练GRU/TCN，也不再提供云端神经训练按钮。后续神经模型在本机训练、调优、验证后上传合格包。训练命令只保存候选与报告，不改变正式预测记录。

## Windows本地训练与进度页面

目标目录为 `C:\Users\12291\Desktop\QuantModel`。启动脚本默认使用当前账户的 `%USERPROFILE%\Desktop\QuantModel`，也可显式传入路径。**云端会话看不到该目录时，本机训练不会自动开始**；必须在能访问该目录的Windows电脑上执行。脚本、网页和测试就绪不等于本机GPU已运行。

将本仓库克隆或更新到该目录下的 `a-share-radar` 子目录，先确认 `data/equity/daily` 已有行情归档，再从仓库根目录启动：

```powershell
.\scripts\train-windows.ps1 -Workspace 'C:\Users\12291\Desktop\QuantModel' -Device auto
```

脚本创建独立虚拟环境；PyTorch未安装时会停止并给出安装方式。NVIDIA CUDA设备可用显式 `-InstallCudaTorch -Device cuda` 安装已核实的PyTorch 2.8.0/cu126构建并要求GPU运行；需要兼容的驱动。安装方式来自[PyTorch官方历史版本](https://pytorch.org/get-started/previous-versions/#v280)。不确认显卡类型时先使用自动检测；CUDA不可用会明确显示CPU，显式 `-Device cuda` 则直接报错。

默认设置：全部归档股票（`MaxStocks=0`）、最多20轮、早停4轮、GRU/TCN共享模型；默认Hidden128时比较32/64/128三个容量配置，并加入GRU/TCN集成候选。自动检测逻辑核数、可用RAM和CUDA；CPU线程默认保留两个逻辑核、最多16线程，可显式指定。CUDA以随机张量短测128、256、512、1024、2048的批量，使用GRU和TCN的实际前后向与优化器开销，在显存预算内选择实测吞吐较好的批量。预算不超过当前空闲显存的70%或总显存的80%。随机张量仅测试硬件，不计入金融预测成绩。8GB显卡并不保证任意批量都能运行。

训练自动打开 `http://127.0.0.1:8768/`，显示阶段、试验数、当前轮批次、真实训练损失/验证MAE曲线、候选排名、训练速度和显存。页面每2秒读取本机进度文件；没有数据就保留空值，60秒没有更新仅提示可能等待或暂停。服务只允许本机访问，不提供上传接口。

每次训练使用独立目录，保留 `progress.json`、`resources.json`、`selection.json`、`local-summary.json`、各试验报告和候选检查点。各配置只计算验证指标；集成的GRU权重仅从0.25/0.5/0.75中按验证MAE选择。先把赢家写入 `selection.json`，再固定配置、集成权重与种子执行最终确认。一次时间切分仍不代表跨市场稳健性；不能根据最终测试成绩回头调参。

训练完成后进度页保持可查看；按 `Ctrl+C` 关闭监听，结果文件保留。重新查看已完成记录而不训练，可在对应虚拟环境运行：

```powershell
python -m pipeline.local_training --output-dir '某次运行目录' --serve-only
```

达到双目标、验证及确认门槛后，脚本生成本次运行下的 `upload-ready` 目录，内含已固定的模型、验证报告、部署清单和SHA256；不达门槛时仅保留报告，不生成可部署包。也可手动运行 `python -m pipeline.deployed_equity --run-dir 某次运行目录 --output-dir 一个新的导出目录`。

本机训练完成后，将 `upload-ready` 内全部文件作为同一提交上传至仓库 `data/equity/models/`；保留旧模型版本用于回退。部署清单的变更会触发Actions，安装CPU推断依赖、校验模型与报告、用已缓存行情推断并发布，**不再训练或重新选择集成权重**。不完整、校验失败或不达门槛的上传不会启用，继续使用既有可用基线。脚本本身不自动上传或推送仓库，也不会把本机路径写入导出报告。

全局 `training_enabled=false`、股票模型 `enable_training=false` 以及业务步骤的 `RADAR_ALLOW_TRAINING=false` 共同关闭云端拟合。缺少可用模型时保留空值，不能临时训练；旧指数直接模型只有完全相同输入缓存时才复用。扩大历史可使用上文限频采集命令，训练入口自身不访问行情API。

## 研究限制

- 当前股票名单、当前行业归属不能证明历史股票池无幸存偏差；正式回测应包含退市股票与历史生效成员信息。报告保留这一限制。
- 归档缺口、复权缺失和低历史覆盖应产生缺失/待训练状态，不得填造价格或复制其他股票输出。
- 新闻、财报、估值需保存实际可得时间后才能成为历史特征，不能按报告期提前使用公告内容。
- 预测误差不等于可交易利润。策略回测必须另计交易成本、滑点、换手、停牌、涨跌停与实际交易规则。
- 聚合必须使用预测时点已知权重；没有官方指数权重时，名称为成分股研究组合，并展示股票/市值覆盖率。
- 测试段不得反复用来选参数。自动更新应保留旧模型、冻结预测快照并待结果结算后评分，不能改写历史预测。

## 参考来源

- [Tushare官方历史范围与权限](https://tushare.pro/document/1?doc_id=108)
- [Tushare daily参数、字段与批量获取方式](https://tushare.pro/document/2?doc_id=27)
- [Microsoft Qlib A股研究基准](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md?plain=1)
- [Qlib Alpha360的60日量价设计](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py)
- [Qlib GRU官方实现](https://github.com/microsoft/qlib/blob/main/qlib/contrib/model/pytorch_gru_ts.py)
- [TCN论文](https://arxiv.org/abs/1803.01271)及[原作者代码](https://github.com/locuslab/TCN)
- [DLinear论文](https://arxiv.org/abs/2205.13504)及[官方代码](https://github.com/cure-lab/LTSF-Linear)
- [PatchTST论文](https://arxiv.org/abs/2211.14730)及[官方代码](https://github.com/yuqinie98/PatchTST)
- [scikit-learn时间切分](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)与[数据泄漏防范](https://scikit-learn.org/stable/common_pitfalls.html)

DLinear/PatchTST可以作为后续对照。通用时间序列论文和公开Qlib历史基准均不是本网站的收益承诺，也不能替代使用本站数据进行独立检验。
