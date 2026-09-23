# 文档索引（docs/）

这里的文档写的是当前实际行为，可以当参考手册看。

判断现状以代码和 `tools/quality_check.py` 的断言为准。审计过程、优化方案、开发流水这些
「当时怎么修的」记录不随仓库分发（只对写作者有用，改动后就会过期），需要追溯时查看提交信息。

> 入口：先读仓库根的 [`README.md`](../README.md)（功能、安装、使用、已知限制），
> 要改代码或自己打包再看 [`development.md`](development.md) 与 [`architecture.md`](architecture.md)。

---

## 现状参考

| 文档 | 是什么 | 谁该读 |
|---|---|---|
| [`architecture.md`](architecture.md) | 架构现状说明：模块划分、入口点索引、检索与可信度派生链路、常见改动的落点 | **所有人**，尤其是第一次改代码的人 |
| [`development.md`](development.md) | 开发与打包手册：评测集怎么跑、环境搭建与依赖、运行调试、开发期 `.env`、诊断工具、重建种子库、打包与发布、平台适配、项目结构与入口文件说明、踩坑记录（README 里面向用户的部分不重复这些内容） | 要改代码、重新打包或排查构建问题的人 |
| [`data_sources_cn.md`](data_sources_cn.md) | 中文数据源清单：官方站、wiki、社区站的定位、可信度等级与可用性备注 | 增删数据源、调可信度权重的人 |
| [`consistency_review.md`](consistency_review.md) | 跨来源冲突的判例档案：每条裁决的依据与结论（种子里 685 条事实 / 74 槽 / 多来源 14 / 冲突 0，撤回 1 篇后程序内为 682 条） | 质疑某条知识、要提纠正的人；新裁决请按同样格式追加 |
| [`eval/README.md`](../eval/README.md) | 离线评测集的构成（`eval_set.json` / `eval_api.json` / `eval_tables.json`）、怎么跑、结果怎么读 | 要评估回答质量的人（需要模型 Key，不参与 CI） |

---

## 维护约定

- 新增文档放进本目录，并在上表里加一行：写清**是什么**、**谁该读**。
- 文档里提到实现时，要给出**具体位置**（`app/core/trust.py:139` 或函数名），
  不要写「相关模块里」这类没法 grep 的说法，见 [`../CONTRIBUTING.md`](../CONTRIBUTING.md) §3.4。
- 结论性的数字（断言数、条目数、评测基线）一变，就同步仓库根的 `README.md`、
  `CONTRIBUTING.md` 与这里引用的说法，避免再出现「文档说 A、代码是 B」。
