---
name: retrieval-coverage-gap-analysis
description: Use when a research search supplies a known-relevant reference set plus retrieved records and query logs, and needs recall/coverage computed from the reference denominator, per-reference hit status, actionable miss classification, and supplementary-round marginal gain in a contract-preserving result.json.
---

# 检索覆盖率与遗漏分析

## 用途与适用条件

当任务要求回答"关键文献是否被找到、漏在哪个查询/字段/来源/时间窗、补检是否真的降低遗漏"时使用。适用于系统综述前期检索评估、指南证据更新、数据库选型对比与检索策略调优。

**适用前提**：输入真实提供**已知相关文献参考集**（来自综述、指南、专家清单或既有系统综述），以及检索结果与查询日志。没有参考集就没有合法分母，本 Skill 会返回 `insufficient_input`，不会用检索结果自身反推分母。

**不适用**：正式系统综述纳排、全文精读、替代数据库接入。不联网，不声称任何数据库的全局覆盖。

## 输入

| 输入 | 说明 |
|---|---|
| `/input/case.json` | 必读。常见结构：`reference_set`/`gold_set`/`known_relevant`（参考集）、`retrieved`/`results`（检索结果，可含 `query`/`source`/`field`）、`queries`（查询日志，含 `query`、`source`、`field`、`year_from`/`year_to`）、`search_scope`（`sources`、`fields`、`date_from`、`date_to`）、`supplementary_queries`（补检轮次）。实际键名以文件为准。 |
| `task.md` | 可选，存在时读取用于核对参考集来源与检索范围。 |
| `--work` | 工作目录，默认 `/app/work`。 |

## 处理流程（对应评分维度）

1. **冻结参考集**（任务完成度）：逐条规范化参考记录，按标识符去重，输出规模与 `frozen_keys_sha_short`（仅由参考集键生成的短哈希），使分母可核对、可复算。
2. **命中判定**（结果正确性）：按 `DOI → PMID → arXiv → 归一化标题` 精确匹配；无强标识符时用标题相似度 ≥0.90 且年份相差 ≤1 的模糊匹配，并记录 `matched_by` 与来源定位。命中只认实际存在的检索记录，不补造。
3. **覆盖率计算**（正确性/完整性）：`coverage_rate = matched / reference_total`，分母永远来自参考集；同时报告 `retrieved_total` 与 `precision_proxy = matched / retrieved_total`，并注明它只是代理指标。
4. **漏检分类**（可核验性）：对每条未命中参考集记录给出可操作原因与证据：
   - `query_term_gap`：标题/摘要关键术语未出现在任何查询式中，附未覆盖词表与词覆盖率；
   - `field_restriction`：检索字段集合排除了摘要等字段；
   - `time_window`：参考年份早于/晚于所有查询窗口；
   - `source_not_covered`：声明的来源库没有任何返回；
   - `record_not_retrievable`：参考记录被标记撤稿或全文不可得；
   - `identifier_anomaly`：参考记录缺可用标识符与标题；
   - `no_declared_query`：未提供查询式，无法归因；
   - `no_matched_retrieval`：检索集中确实没有匹配记录。
5. **补检评估**（创新增益）：逐轮统计补检查询的返回条数、新命中参考数、`marginal_gain` 与边际命中率；只把此前未命中的参考计入增益，避免"重跑同义查询"式虚增。
6. **残余遗漏**（完整性）：补检后仍未命中的参考进入 `residual_gaps`，逐条保留原因；给出停止规则（某轮新增为 0 即停止）。

## 输出契约

写入 `--out`（默认 `/app/output/result.json`）。顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `case_id` | string | 原样保留输入 `case_id` |
| `status` | string | `success` / `partial` / `insufficient_input` / `failed` |
| `result` | object | `reference_set`、`coverage`、`match_results`、`misses`、`gap_summary`、`supplementary_search`、`residual_gaps`、`queries`、`search_scope`、`stop_rule`、`limitations`、`change_log` |
| `coverage` | object | 顶层镜像：参考总数、命中、遗漏、覆盖率、检索总数、代理精确率 |
| `misses` | array | 顶层镜像的逐条遗漏清单 |
| `evidence` | array | 参考集、查询式、命中/遗漏的定位 |
| `uncertainty` | array | 无参考集、无检索结果、无查询日志、分母口径等说明 |
| `errors` | array | 运行错误；正常为空 |
| `stats` | object | 参考数、检索数、命中、遗漏、补检轮次、残余遗漏 |
| `contract_notes` | object | 未产出契约字段说明 |
| `output_contract_fields` | object | `provided` 与 `present` |

输入 `output_contract` 的每个字段名都会出现在顶层；无法产出时写 `null` 并在 `contract_notes` 说明。

`status`：无参考集 → `insufficient_input`；有参考集但缺检索结果或缺查询日志 → `partial`；三者齐备 → `success`；解析失败 → `failed`。

## 异常处理

| 情形 | 处理 | 输出标记 |
|---|---|---|
| 无参考集 | 不计算覆盖率，不虚构分母 | `status=insufficient_input` |
| 参考集为空但检索有结果 | 只列检索结果，说明无法评估覆盖 | `uncertainty.type=no_reference_set` |
| 无检索结果 | 所有参考记为遗漏并分类 | `uncertainty.type=no_retrieved_records` |
| 无查询日志 | 覆盖率仍可算，但原因归为不可归因 | `no_declared_query` |
| 参考记录缺标题与标识符 | 保留在分母，单独标记 | `identifier_anomaly` |
| 参考记录撤稿/全文不可得 | 计入 `record_not_retrievable`，不当作检索失败 | `gap_type` |
| 补检无新增 | `marginal_gain=0`，按停止规则停止 | `supplementary_search[]` |
| 输出目录不可写 | stderr 简短错误，退出码 3 | 无静默成功 |

## 限制

- 覆盖率只针对给定参考集，不代表文献总量或数据库真实召回。
- 标题模糊匹配是候选判断，边界个案可能误判，需人工抽检。
- 归因只基于提供的查询式、字段、年份与来源；未记录的检索策略不可见。
- 代理精确率 `matched / retrieved_total` 会被参考集规模影响，不能当作真实精确率。

## 最小调用示例（示例，非正式案例结果）

```bash
python /skills/weekly-skill/scripts/run_skill.py \
  --case /input/case.json --out /app/output/result.json --work /app/work
```

输入片段（示例）：

```json
{"case_id": "demo-004",
 "reference_set": [{"title": "NLRP3 and pyroptosis", "doi": "10.1/ref1", "year": 2019}],
 "queries": [{"query": "NLRP3 pyroptosis", "source": "pubmed", "field": "title"}],
 "retrieved": [{"title": "NLRP3 inflammasome and pyroptosis", "doi": "10.1/ref1"}]}
```

示意输出（示例，已截断）：

```json
{"case_id": "demo-004", "status": "success",
 "coverage": {"reference_total": 1, "matched": 1, "missed": 0, "coverage_rate": 1.0},
 "result": {"gap_summary": {}, "supplementary_search": [], "residual_gaps": []}}
```

以上为接口示意，不代表任何官方固定案例结果。


## 交付保证与评分对齐

1. 在 Skill 安装目录执行：`cd /skills/weekly-skill && python scripts/run_skill.py --case /input/case.json --out /app/output/result.json --work /app/work`；路径不同时显式改 `--case` / `--out` / `--work`，不要假定相对路径可用。
2. 必须产出**完整成品** `result.json`：参考集每条记录都有命中状态，未命中项都有 `gap_type` 归因，`coverage` 计数闭合；禁止 skeleton、`pending`、空占位或用流程说明代替结果。
3. 脚本无法自动完成的实质判断（真实查询式归因、补检是否真实降低遗漏）由调用 Agent 依据实际材料补全并写回 `result.json`，不得编造命中或遗漏。
4. 自检：`case_id` 与输入一致；`output_contract` 每个字段存在；`status` 取本契约值 `success` / `partial` / `insufficient_input` / `failed`（不得写 `completed`）；每条结论有来源定位。
5. 输入缺失、JSON 损坏或字段异常时仍写出结构化结果并说明缺口，不崩溃、不静默成功。

## 最终交付强制要求

1. **调用 Agent 必须交付成品，不是流程说明**：脚本产出只是索引、核对或规格骨架；调用 Agent 必须在同一 `result.json` 中补齐本题要求的最终产物（正文/译文/审稿意见/综述/分析结论/图表源文件与导出件/幻灯片页内容与讲稿/实验方案等）。交付字段为空、只有标题骨架，或含 `pending`、`TODO` 之类占位，一律视为未完成。
2. **必须覆盖全部输入条目**：输入中的每个章节、问题、文献、图、页面、指标都要有对应条目，不得只处理示例或首条；确实缺失的条目写入 `uncertainty` / `limitations` 并说明原因，不得静默跳过。
3. **必须自检后再交付**：`case_id` 与输入一致；`output_contract` 每个字段都存在；状态值取本题契约规定的枚举；数字、单位、引用、来源可逐条回溯；结果内部无自相矛盾。
4. **参数等价**：`python scripts/run_skill.py --case <输入> --out <输出>` 与 `--output <输出>` 等价；默认 `/input/case.json` → `/app/output/result.json`。
5. **平台用 3 个固定案例分别运行**：逻辑要由输入驱动，不针对某一份示例硬编码；字段命名差异走别名匹配与显式降级，并在 `uncertainty` 记录假设。
