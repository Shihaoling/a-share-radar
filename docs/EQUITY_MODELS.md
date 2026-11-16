# 股票级共享模型与本地训练

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

训练默认 batch128、AdamW 学习率3e-4、weight decay1e-4、Huber loss、梯度裁剪1、最多10轮、验证集4轮不改善即停止。可测试 hidden64、batch256，但必须测量实际显存，不能保证所有8GB显卡的批量上限相同。

64GB内存/8GB显存是上述小模型的设计目标，不表示已经连接或使用用户本机GPU。本命令在调用它的运行环境执行；报告记录实际 `device` 与CUDA峰值分配显存，CPU运行时该显存项为空。`--device auto` 优先CUDA，否则CPU；不会自动把任务传送到另一个电脑。

## 可直接运行

先查看网页“个股模型 · 训练与核验”的已归档交易日数。接口拒绝、限频或连接失败后，`data/equity/state.json` 保留冷却时间；冷却未到不重复请求，也不清除冷却来绕过来源限制。GitHub Actions会在后续任务断点续传。首轮目标180日，训练样本未足够时股票预测保持待生成。

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

`eligible_for_promotion` 要求神经赢家在两个目标上，验证与确认MAE都比零收益和Ridge中的最佳基准至少低2%。这仍只是单次切分候选资格，**不会自动切换线上模型**；`promoted` 始终为false，生产模型保持不变。下一阶段应完成多段滚动检验、多随机种子稳定性检验、按行业/股票规模的误差检查，再显式接入每日推断。不能宣称未达标模型已经晋升。

结果写入 `data/equity/models/training_report.json` 和兼容名称 `training-report.json`；每次神经训练另存带时间戳的 `.pt` 候选检查点。检查点仅含模型状态、结构、训练段缩放参数、标签口径与验证元数据。调用接口：

```python
from pipeline.train_equity import load_neural_bundle, predict_neural_samples
bundle = load_neural_bundle("data/equity/models/gru_RUN_TIMESTAMP.pt", device="cpu")
predicted_returns_pct = predict_neural_samples(bundle, sequences)  # shape: N × 60 × 8
# 返回 N × 2，依次为下一开盘收益%、下一收盘收益%。
```

只加载自己生成和信任的检查点。常规线上逐股推断不依赖PyTorch；首次Actions的CPU试训及手动训练步骤会安装PyTorch。训练命令只保存候选与报告，不改变正式预测记录。

## 研究限制

- 当前股票名单、当前行业归属不能证明历史股票池无幸存偏差；正式回测应包含退市股票与历史生效成员信息。报告保留这一限制。
- 归档缺口、复权缺失和低历史覆盖应产生缺失/待训练状态，不得填造价格或复制其他股票输出。
- 新闻、财报、估值需保存实际可得时间后才能成为历史特征，不能按报告期提前使用公告内容。
- 预测误差不等于可交易利润。策略回测必须另计交易成本、滑点、换手、停牌、涨跌停与实际交易规则。
- 聚合必须使用预测时点已知权重；没有官方指数权重时，名称为成分股研究组合，并展示股票/市值覆盖率。
- 测试段不得反复用来选参数。自动更新应保留旧模型、冻结预测快照并待结果结算后评分，不能改写历史预测。

## 参考来源

- [Microsoft Qlib A股研究基准](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md?plain=1)
- [Qlib Alpha360的60日量价设计](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py)
- [Qlib GRU官方实现](https://github.com/microsoft/qlib/blob/main/qlib/contrib/model/pytorch_gru_ts.py)
- [TCN论文](https://arxiv.org/abs/1803.01271)及[原作者代码](https://github.com/locuslab/TCN)
- [DLinear论文](https://arxiv.org/abs/2205.13504)及[官方代码](https://github.com/cure-lab/LTSF-Linear)
- [PatchTST论文](https://arxiv.org/abs/2211.14730)及[官方代码](https://github.com/yuqinie98/PatchTST)
- [scikit-learn时间切分](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)与[数据泄漏防范](https://scikit-learn.org/stable/common_pitfalls.html)

DLinear/PatchTST可以作为后续对照。通用时间序列论文和公开Qlib历史基准均不是本网站的收益承诺，也不能替代使用本站数据进行独立检验。
