# R3：评级到仓位和风险建议

负责人GitHub：bcj6。范围仅R3，不替代全组Report、视频、其他成员材料或组长审查。

## 这项功能做了什么

原系统只给Buy/Overweight/Hold/Underweight/Sell评级。R3在原评级后增加确定性仓位建议，例如本次真实模型给Overweight，假设当前15%，规则增加5个百分点，得到20%。这是2024-05-10的课程验证，不是当前投资建议或已执行交易。

评级由模型产生；仓位上限、历史波动、止损比例及缺数据处理由代码决定。模型不能批准越过风险上限。原`propagate`参数和`(state, rating_string)`返回保持不变，新结果放在`state['position_signal']`。默认关闭，提供明确context可启用；未知仓位不等于0。

## 政策与输入

采用的实验参数：Buy20%、增减5个百分点、固定上限20%、日波动阈值2%、正目标止损比例8%；confidence未提供。它们不是教师指定值或投资保证。详见[批准记录](governance/approved-policy-and-tests.md)。

`examples/position-context.json`的15%是假设仓位，必须与股票和分析日期匹配。Python可在config传`position_context`；CLI支持独立于`--portfolio`的`--position-context PATH`。完全关闭时清除context并将position_enabled设False。

## 复现

安装项目dev依赖后执行：

```text
python -m pytest -q
python -m ruff check .
python -m pytest -q tests/test_position.py tests/test_position_inputs.py tests/test_position_integration.py tests/test_position_reporting.py tests/test_deepseek_token_limit.py
```

Windows本机测试中有一个假回环认证服务，需要允许localhost监听；不需要真实账号或API密钥。默认排除的真实集成测试不能算作已经通过。

真实运行需要自行在私有环境中配置合法的模型连接。课程验证使用学校部署CS5351/DeepSeek-V4-Flash，经deepseek适配器连接学校/v1端点；凭据不属于本材料。设置provider、两个模型层、端点、语言和轮次后，可运行：

```text
python -m cli.main --ticker AAPL --date 2024-05-10 --analysts market --position-context docs/R3/examples/position-context.json --save --no-show --html
```

不要把该命令的默认设置当成本次受控脚本的全部控制。本次还限定了20请求、4096输出token、输入字符数、总已报告token及SDK超时/重试。学校别名价格未知，$0估算不是免费或可靠的USD硬预算；详见[evidence](verification.md)。

## 阅读顺序

1. [架构](architecture/architecture-blueprint.md)与[ADR](architecture/adr/0001-position-policy.md)。
2. [需求](req/requirements.yaml)、[不变量](architecture/invariants.md)、[追踪矩阵](architecture/trace-matrix.md)。
3. [验证](verification.md)、[治理与干预](governance/approval-and-interventions.md)、[审查备忘](review-memo.md)。

当前完整离线1774 passed、6 skipped、2 deselected；真实成功/故障注入/零预算/输出上限证据齐全。浏览器视觉截图未完成：本地file URL被安全策略拒绝，已停止该预览路径，不能把HTML结构测试称成视觉验收。
