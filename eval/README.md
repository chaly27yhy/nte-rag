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
| `kb_report.json` | 知识库快照体检报告（`tools/kb_report.py` 输出）：文档/条目数、来源分层、域名分布、重复标题、冲突、时效敏感条目等 |
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
- `expected_sources`：期望的来源 URL，用来检验检索环节是否命中，也就是引用命中率。
- `answerable`：`false` 表示「任何可爬来源都查不到这个数据」。这类题考的是该拒答时能否拒答，
  而不是答对。
- `review.status`：
  | 值 | 含义 |
  |---|---|
  | `pending` | 自动生成的候选，尚未审核 |
  | `ok` | 人工确认题目与要点正确 |
  | `fixed` | 人工修正过（要点/来源/标签可能改过） |
  | `drop` | 剔除，不参与评测 |
- `review.comment`：该题的复核结论与理由（含日期）写在这里。`kb_gap` 题必须在此留下说明，
  否则 `tools/quality_check.py` 会判失败。不要往 JSON 里加自由字段，结构由 `tools/quality_check.py` 校验。

补充标签写在 `tags` 里，由人工标注：

`kb_gap` 是题目里的顶层布尔字段，也就是 `"kb_gap": true`，不是写在 `tags` 数组里。评测时
`tools/run_eval.py:278-280` 会把它复制进结果行的 `tags` 字典，`split_scored()`
（`tools/run_eval.py:41-53`）从那里读出来分流。

- `kb_gap`：知识库缺口题，不计入要点覆盖率，但仍计入编造统计。「库里没有」不等于「可以编」。
- `time_sensitive`：时效敏感题，答案会随版本更新变化。

> 当前状态：`eval/eval_set.json` 60 题里只有 `q057` 标了 `kb_gap: true`，而它的
> `review.status` 是 `drop`。也就是说现在没有在跑的缺口题，缺口规则本身仍有断言与
> `tools/kb_gap_report.py` 覆盖。真要补缺口题时，要同时把 `review.status` 设为 `ok`/`fixed`。

缺口题单独统计的原因：它们如果算进覆盖率分母，分数会随模型输出抖动
（实测 q035 在 2/2 与 1/2 之间跳），看起来像数据退化。这是 `tools/run_eval.py:40 split_scored()`
的职责，`quality_check.py` 对它有直接断言。

---

## 3. 怎么跑

```powershell
# 全部（自动跳过 review.status == "drop" 的题；默认只用本地知识库）
.venv\Scripts\python.exe tools\run_eval.py

# 只跑前 20 题
.venv\Scripts\python.exe tools\run_eval.py --limit 20

# 允许联网补齐（会真实搜索 + 抓取；默认关闭以保证可复现）
.venv\Scripts\python.exe tools\run_eval.py --web

# 给报告打标记，便于改动前后对比
.venv\Scripts\python.exe tools\run_eval.py --tag before-fix
```

需要模型配置：判分由一个「严格评测员」提示词驱动，见 `tools/run_eval.py` 的 `GRADE_SYSTEM`。
在界面「设置」页或 `.env` 里配好 LLM 即可。`--no-seed` 可以跳过种子知识库自动导入。

---

## 4. 指标与报告

`metrics` 字段（打印到控制台，也写进 `report-*.json`）：

| 指标 | 含义 |
|---|---|
| 要点覆盖率 | 计分题的平均覆盖比例（核心指标） |
| 完整覆盖率 / 完整覆盖题数 | 要点全中的题数比例 |
| 引用命中率 | 答案引用里包含 `expected_sources` 的比例（检验检索环节） |
| 拒答正确率 | 应拒答题里回答「资料未涵盖」的比例 |
| 疑似编造题数 | 出现了与期望要点矛盾、且参考资料里无依据的具体事实 |
| 缺口题编造题数 | 知识库缺口题上的编造数（最危险的一类） |
| 平均延迟ms / 总耗时秒 | 性能参考 |

判分标准，也就是要点覆盖、是否拒答、是否编造，写在 `tools/run_eval.py` 的 `GRADE_SYSTEM` 里。
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
