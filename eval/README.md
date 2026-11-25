# 评测集说明（eval/）

这组文件用来判断一次质量改动让答案更准了还是更差了。
没有它，检索、抽取、可信度调整只能靠主观判断。

评测集是评测用数据，不随程序分发，不入 exe，也不入种子库。

---

## 1. 文件一览

| 文件 | 内容 |
|---|---|
| `eval_set.json` | 主评测集：60 题，人工审核过（`review.status` 为 ok / fixed / drop） |
| `eval_api.json` | 21 题，聚焦结构化接口来源（MediaWiki 模板 → 原子条目）的抽取质量 |
| `eval_tables.json` | 18 题，聚焦表格类数值（角色初始数值、弧盘效果等） |
| `accuracy_cases.json` | 6 道手工编写的准确性回归题，涵盖版本时效、来源冲突和部分可答。题目、参考答案和虚构来源都在同一快照里 |
| `grading_regressions.json` | 保存实际模型的标题与正文矛盾错答，以及区分两个生效日期的对照答法 |
| `real_snapshot_cases.json` | 12 道基于 2026-09-22 种子快照的真实资料题，带来源、原文锚点和判分边界。维护者已复核 3 道，另外 9 道标为 `pending` |
| `real_snapshot_grading_regressions.json` | 真实资料错答与对照答法，覆盖折后实际售价、逐项资格判断、撤回来源及缺少依据的归因 |
| `kb_report.json` | 2026-09-21 原始种子的历史体检报告。当前资料用下方的 `tools/kb_report.py` 重算。 |
| `report-*.json` | 运行产物，不提交（`.gitignore` 已忽略）。每次跑评测都会新生成一份，文件名含时间戳，`--tag` 可加自定义标记 |

历史备份 `eval_set.json.bak` 已删除，不随仓库提交，也不再需要。

---

## 2. 题目结构（`eval_set.json` → `items[]`）

```json
{
  "id": "q001",
  "question": "……",
  "expected_points": ["期望要点 1", "期望要点 2"],
  "expected_sources": ["https://…"],
  "category": "角色 / 剧情 / 玩法 / 系统 / 活动 / 术语 / 成就",
  "difficulty": "easy / medium / hard",
  "answerable": true,
  "review": { "status": "ok", "comment": "复核结论：…" }
}
```

字段语义：

- `expected_points`：必须来自知识库的要点列表，0–4 条。覆盖率就是按它逐条判分的。
- `expected_sources`：可接受的来源 URL。报告分别统计最终证据是否包含其中一页，以及答案是否实际引用其中一页。
- `required_evidence_sources`：准确性题中必须出现在证据里的所有页面，例如同一规则的新旧公告。
- `expected_unknowns`：「部分可答」题的未确认项。答案要明确指出哪些信息无法确认。
- `excluded_sources`：具体已撤回的来源和页面名。判分核对回答有无建议把这些页面用作证据。
- `answerable`：`false` 表示「任何可爬来源都查不到这个数据」。这类题考的是该拒答时能否拒答，
  而不是答对。
- `review.status`：
  | 值 | 含义 |
  |---|---|
  | `pending` | 待人工复核的候选，包括已对照快照初标的题目 |
  | `ok` | 人工确认题目与要点正确 |
  | `fixed` | 人工修正过（要点/来源/标签可能改过） |
  | `drop` | 剔除，不参与评测 |
- `review.comment`：该题的复核结论与理由（含日期）写在这里。`kb_gap` 题必须在此留下说明，
  否则 `tools/quality_check.py` 会判失败。不要往 JSON 里加自由字段，结构由 `tools/quality_check.py` 校验。

补充标签写在 `tags` 里，由人工标注：

`kb_gap` 是题目里的顶层布尔字段，也就是 `"kb_gap": true`，不是写在 `tags` 数组里。评测时
评测会把它复制进结果行的 `tags` 字典，`tools/run_eval.py` 中的 `split_scored()` 据此归类。

- `kb_gap`：知识库缺口题，不计入要点覆盖率，但仍计入编造统计。「库里没有」不等于「可以编」。
- `time_sensitive`：时效敏感题，答案会随版本更新变化。

> 当前状态：`eval/eval_set.json` 60 题里只有 `q057` 标了 `kb_gap: true`，而它的
> `review.status` 是 `drop`。也就是说现在没有在跑的缺口题，缺口规则本身仍有断言与
> `tools/kb_gap_report.py` 覆盖。真要补缺口题时，要同时把 `review.status` 设为 `ok`/`fixed`。

缺口题单独统计的原因：它们如果算进覆盖率分母，分数会随模型输出抖动
（实测 q035 在 2/2 与 1/2 之间跳），看起来像数据退化。这是 `tools/run_eval.py` 中 `split_scored()`
的职责，`quality_check.py` 对它有直接断言。

---

## 3. 怎么跑

离线来源回归使用全新临时库、仓库内的种子、FTS5，以及固定的 `top_k=8`、`min_relevance=0.05`。
CI 读取三份既有题集中已审核、可回答且有预期来源的 82 道题，并保留逐题报告。2026 年 11 月 19 日完成替代来源复核后，82 道均可评分。将来若题目只依赖暂停来源，报告会在 `deferred` 中保留原题等待补证。
准确性题使用自带的虚构来源，版本和数值仅在测试快照内成立。

真实资料题由 Codex 对照种子快照初标，`source_anchors` 记录在快照中可查到的原文片段。
待复核题通过 `--include-pending` 运行独立来源检查。

~~~powershell
# 离线验收有可用来源的基线题，并单列依赖暂停来源的历史题
.venv\Scripts\python.exe tools\run_eval.py --retrieval-only

# 检查 6 道准确性题的必需证据，无需模型或联网
.venv\Scripts\python.exe tools\run_eval.py --retrieval-only --set eval/accuracy_cases.json --seed eval/accuracy_cases.json

# 核对待复核的真实资料题
.venv\Scripts\python.exe tools\run_eval.py --retrieval-only --set eval/real_snapshot_cases.json --include-pending
~~~

模型评测也默认创建全新的临时库。请在 `.env` 配置模型，或者用 `--data-dir` 指向带 `config.json`
的评测目录。若要更换 `--seed`，请为它使用空库。

```powershell
# 全部（自动跳过 review.status == "drop" 的题；默认只用本地知识库）
.venv\Scripts\python.exe tools\run_eval.py

# 用同一快照跑六道准确性题
.venv\Scripts\python.exe tools\run_eval.py --set eval/accuracy_cases.json --seed eval/accuracy_cases.json --tag accuracy

# 用内置种子评测真实资料题（含待复核题）
.venv\Scripts\python.exe tools\run_eval.py --set eval/real_snapshot_cases.json --tag real-snapshot

# 只跑前 20 题
.venv\Scripts\python.exe tools\run_eval.py --limit 20

# 允许联网补齐（会真实搜索 + 抓取；默认关闭以保证可复现）
.venv\Scripts\python.exe tools\run_eval.py --web

# 给报告打标记，便于改动前后对比
.venv\Scripts\python.exe tools\run_eval.py --tag before-fix
```

答题和判分共用配置的模型。报告会记录种子 SHA-256、模型与检索设置、完整回答、带编号的引用，
以及答题时送进模型的证据。评测保存同一次检索的证据，供判分和人工复核。报告是本地产物，
分享前请核对其中的题目、回答和资料原文。

---

## 4. 指标与报告

`metrics` 字段（打印到控制台，也写进 `report-*.json`）：

| 指标 | 含义 |
|---|---|
| 要点覆盖率 | 计分题的平均覆盖比例（核心指标） |
| 完整覆盖率 / 完整覆盖题数 | 要点全中的题数比例 |
| 矛盾题数 | 回答在同一适用范围对同一必答要点给出互相矛盾结论的题数。该要点不给覆盖分 |
| 撤回来源误荐题数 | 回答建议玩家把被该题明确排除的页面用作核对来源的题数 |
| 证据来源命中率 | 最终证据里包含任一 `expected_sources` 的比例 |
| 引用命中率 | 答案引用了任一 `expected_sources` 的比例 |
| 引用支持率 | 判分认为引用内容支持对应事实结论的题目比例 |
| 应保留项覆盖率 | 部分可答题明确说明哪些信息无法确认的比例 |
| 拒答正确率 | 应拒答题里回答「资料未涵盖」的比例 |
| 疑似编造题数 | 出现具体但缺少适用证据支持的说法，例如用旧版数值回答现行规则 |
| 缺口题编造题数 | 知识库缺口题上的编造数（最危险的一类） |
| 答题失败题数 / 判分失败题数 | 两种失败分别计数。无效评分不进入覆盖率分母，有此类失败的运行以退出码 1 结束 |
| 平均延迟ms / 总耗时秒 | 性能参考 |

判分标准，也就是要点覆盖、是否拒答、是否编造，写在 `tools/run_eval.py` 的 `GRADE_SYSTEM` 里。
报告的 `diagnostics` 提示复核入口。出现 `expected_evidence_missing` 时看检索；出现
`citation_does_not_support_claim`、`required_point_missing` 或 `uncertainty_missing` 时，
对照 `evidence_context` 复核证据排序和回答。模型判分适合筛查，关键错题以人工逐条核对为准。
`contradicted_point` 表示至少一个要点有冲突，`contradicted` 数组标出具体要点。这类答案也会判为引用未完整支撑结论。
检查时同时看标题、摘要、表格和正文。准确标明不同日期、版本或出处的不同数值可以并列。
判分结果只对当前知识库快照成立：知识库更新后，`kb_gap` 与 `time_sensitive` 标签都需要重新确认。

---

## 5. 出题与体检

```powershell
# 生成候选题（必须先配好模型：.env 里 NTE_RAG_DEV_ENV=1 + NTE_RAG_DEV_LLM_*；
# 本工具没有 --with-llm 开关，它始终调用模型。生成结果需要逐题审核并写明 review.status）
.venv\Scripts\python.exe tools\build_eval_set.py --count 60

# 用本地数据库而不是种子文件出题
.venv\Scripts\python.exe tools\build_eval_set.py --count 60 --from-db

# 知识库体检报告（不调用模型）
.venv\Scripts\python.exe tools\kb_report.py
.venv\Scripts\python.exe tools\kb_gap_report.py

# 把人工写在 JSON 行尾的中文批注迁移进 review.comment（JSON 本身不支持注释）
.venv\Scripts\python.exe tools\repair_eval_notes.py            # 先看会改什么（dry-run）
.venv\Scripts\python.exe tools\repair_eval_notes.py --apply    # 实际写入
```

数据规则与来源取舍见 [docs/architecture.md](../docs/architecture.md) 与
[docs/data_sources_cn.md](../docs/data_sources_cn.md)；
跨源一致性与判例见 [docs/consistency_review.md](../docs/consistency_review.md)。
