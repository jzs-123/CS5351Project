# R3 规则、接口与测试审阅稿

日期：2026-11-17。状态：用户已明确采用本文件政策和 T01—T18 测试计划，允许据此进入 OpenCode 编码。继承 11/11 可行方案；采用计划不代表功能已经实现或验收。

## 1. 供采用的政策 v1

比例统一使用 0—1 小数。Buy 固定目标 0.20；Overweight 当前权重 +0.05；Hold 当前权重；Underweight max(0, 当前权重−0.05)；Sell 0。REVIEW/未知评级返回 needs_review，无目标数字。

有效性检查 → 评级目标 → 风险上限。配置最大单票 C=0.20；最近 21 个真实已结束交易日的收盘价产生 20 个简单日收益；样本标准差 ddof=1，日度、不年化。阈值 T=0.02；vol=0 时 s=1，否则 s=min(1,T/vol)；有效上限 C*s；最终权重 min(原目标,有效上限)。关闭波动缩放时仍执行固定上限和数值校验。

Hold 当前仓位已超限时由风险上限覆盖，必须解释 cap_override。Underweight 不能反向加仓；所有确定权重在 [0,C] 内。

非 Sell 正常结果要求显式 current_weight∈[0,1]；未知返回 needs_context，明确 0 是已知空仓。Sell 可在无当前仓位/行情时建议 0；有效输入已算出零目标时波动不适用。正目标所需价格不足/异常返回 needs_data，不伪造低波动。

正目标 stop_loss_pct=0.08（比例策略，非绝对价或执行保证）；零目标/无确定目标为 null。confidence=null，confidence_source='not_provided'。这些为实验参数，非教师规定及真实投资参数。

## 2. 输入/输出与兼容

- Python：config 中显式 position_context；CLI：拟新增 --position-context JSON；不替代现有 --portfolio。
- JSON 字段：ticker、as_of、current_weight、weight_source；使用项目符号规范化；日期须与分析日期一致；不读取或推算真实账户事实。
- 历史价格优先复用 load_ohlcv(..., fill_gaps=False)，只保留截至分析日的真实观测；检查重复、乱序、非正/非有限价格、样本不足和数据过旧。11/17 核对发现现有适配器已定义 MAX_OHLCV_STALE_DAYS=10（日历日），实现复用既有阈值及检查，并对快照路径验证同一口径，不另造交易日历。
- 输出：rating/status/weight/stop_loss_pct/confidence/current_weight/raw_target_weight/effective_cap/daily_volatility/reason/policy_version/input_as_of/input_digest/data_source，以及计算参数、最后实际价格日期/样本数。
- 不改变 propagate 参数和二元返回，signal 保持旧字符串；R3 结果加入 final_state，显式进入日志及共用报告。
- 默认关闭；非法 JSON/配置/显式非法数值在昂贵分析前报错；运行时数据暂不可用保留原分析并记录 R3 失败状态。
- 不跨股票/日期复用可变状态；输入内容与政策有摘要；旧 checkpoint/旧报告无新字段时兼容。

## 3. 测试预批准矩阵

本节列的是待实现的测试计划，没有把它们标为已经运行。pytest 使用浮点容差；网络和模型调用与默认离线测试分开。

| ID | 输入或场景 | 必须观察的结果 |
|---|---|---|
| T01 | Buy，当前 5%，vol 1% | 原目标/最终目标 20%，止损比例 8% |
| T02 | 同 T01，vol 4% | 有效上限和最终目标 10%，原因说明波动限制 |
| T03 | Overweight 5%、Hold 8%、Underweight 15%，低波动 | 分别 10%、8%、10% |
| T04 | Underweight 当前 3%；Sell 缺仓位/行情 | 均得到目标 0，止损不适用；Sell 不联网 |
| T05 | Hold 当前 25%，cap 20%，低波动 | 目标 20%，明确 cap_override |
| T06 | REVIEW/未知评级；非 Sell 当前仓位未知 | 分别 needs_review/needs_context，weight null；不把未知填 0 |
| T07 | bool、NaN、Inf、负数/越界，坏 JSON、配置非法 | 尽早明确拒绝，昂贵调用未发生 |
| T08 | 零波动、关闭缩放、替换 cap/映射参数 | 仍遵守上限；政策来自配置，不硬写进 prompt |
| T09 | 固定 21 收盘价，手工可核算标准差 | 与 ddof=1 日波动一致；日期/计算参数有记录 |
| T10 | 未来日期、重复/乱序日期、非正/缺失价格、样本不足/过旧 | 不产生伪造正常建议；分别记录数据原因 |
| T11 | 真实 OHLCV 适配一次成功 | 来源/截至日期/最后价格日期/快照可复查；回放不得称新取数 |
| T12 | 相同评级/仓位，逐渐增加波动；Underweight 网格 | 目标不因波动增加而增加；Underweight 不反向增仓 |
| T13 | Python 正常/调试路径与 CLI 流式路径 | 同输入得到同建议，原评级和接口不变，各一次接入 |
| T14 | JSON、Markdown、HTML 报告 | 数值/原因一致；JSON 确实保存新增字段 |
| T15 | 成功→关闭、成功→缺数据、旧结果/旧 checkpoint | 没有旧成功建议残留，旧版结果仍能显示 |
| T16 | 连续不同股票/日期；相同路径更换输入；重复保存 | 不串数据；摘要体现内容变化；计算可复现 |
| T17 | 与 R4/R5 同时启用；R5预算中止/模型异常 | 不吞预算异常、不发额外 LLM、不伪报分析成功；R4/R5 既有测试不回归 |
| T18 | 所有受影响测试、完整默认离线测试、lint；受控真实冒烟 | 留真实结果、版本与已知失败，不把 CI 历史通过当本地新测试 |

## 4. 人工审阅记录

- 当前用户已授权：整理计划、维护交接、逐步开始；指定参考已有可行方案。
- 2026-11-17 用户对本文件政策和 T01—T18 的明确回复：“采用草案和测试计划，继续实施”。该决定持续有效，不重复请求。
- 2026-11-17 用户进一步明确允许：沿用已有课程配置，将公开 CS5351Project 源码、已批准的 R3 规则及必要开发指令发送到 llm.cs.cityu.edu.hk 的 cityu-litellm/CS5351/DeepSeek-V4-Flash，用于本次 OpenCode 开发；不发送密钥、连接配置文件或其他课程作业。无需就同一范围再次请求。
- 课程历史整理 D9 原文摘录：“生成代码前停止，等待人工批准”。因此先完成环境和可审阅材料；计划采用后再进入功能编码。
- 正式 OpenCode 会话必须从实际启动时保存；Codex 准备工作单独记录，不补造既往会话。
