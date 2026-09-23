# 第三方内容与数据来源说明（THIRD_PARTY_NOTICES）

本文件说明 `nte-rag` 仓库里哪些内容不是本项目原创，以及它们的权利归属与使用方式。

- 本仓库的**程序代码**采用 MIT 协议，见 `LICENSE`。
- 本文件涉及的第三方内容不在 MIT 授权范围内，其著作权归各自原始站点与作者所有。
- 内置种子库收录的是**摘要式事实**（每条 9–401 字符）与**原文摘录**（每段 120–700 字符），
  不存整页 HTML 存档、不包含任何美术素材（界面图标全部由代码绘制），
  并在每条结论后标注来源与 URL。
- 收录清单、实测数字与移除流程见 `DATA_LICENSE.md`。
- 如果任何权利方认为收录内容不妥，请在本仓库提出 Issue（模板
  `.github/ISSUE_TEMPLATE/takedown.md`），相应条目会被**立即移除**，无需任何法律文书。

---

## 1. 内置种子库 `seed/seed_kb.json`

这是随程序分发的离线知识库（70 篇文档 / 152 段原文摘录 / 685 条结构化事实，
快照时间 `2026-09-22T22:49:06`），内容摘录自下列站点。
种子库中的每条事实都带有 `source_url`，可逐条回溯到原文。

| 站点 | 域名 | 收录内容 | 本项目标注的来源类型 |
|---|---|---|---|
| 《异环》官方网站 | `yh.wanmei.com` | 世界观与角色设定（叙事文本）、新闻与游戏公告 | `official`（官方） |
| BWIKI（bilibili 游戏 wiki） | `wiki.biligame.com/yh` | 弧盘 / 角色 / 卡带 / 道具图鉴与 MediaWiki 模板字段 | `wiki` |
| 萌娘百科 | `zh.moegirl.org.cn` | 《异环》条目与名词词典 | `wiki` |
| 玩一玩游戏网 | `m.wywyx.com` | 角色图鉴（作为 BWIKI 之外的**第二来源**，用于跨源一致性比对） | `wiki` |
| 游民星空 | `www.gamersky.com` | 攻略手册 | `community`（社区） |
| 3DM | `shouyou.3dmgame.com` | 攻略合集 | `community` |
| 17173 | `news.17173.com` | 单条**人工录入**事实的来源页（经 `tools/manual_facts.py` 录入，非自动抓取） | `manual`（人工录入） |
| kamigame（日文攻略站） | `kamigame.jp` | 单条**人工录入**事实的来源页（作为对照来源，非自动抓取） | `manual`（人工录入） |

补充几点：

- **游侠网（`ali213.net`，含 `gl.ali213.net`）** 只用于**人工抽查**，不作为自动来源；
  `薄荷` 生日一案的第三来源证据出自 `gl.ali213.net` 的角色介绍页，
  记录在 `docs/consistency_review.md`。
- **新浪（`sina.cn`）** 只作为官方微博内容的**镜像记录**出现在
  `docs/data_sources_cn.md` 的来源梳理里，不是种子库的自动来源。
- 官方域名有两种写法都对：国服官网 `yh.wanmei.com`（`app/core/sources.py` 里的自动来源），
  以及海外中文版 `nte.perfectworld.com`（仅见于早期开发记录，**不是**自动来源）。
- 已被**移除**的来源与原因（如 `9game.cn` 疑为 AI 生成内容、混淆了《异环》与其它游戏的角色）
  记录在 `app/core/sources.py` 的注释与 `docs/` 的数据来源文档中。
- 来源可信度不是"自报"的：`app/core/trust.py` 用
  `来源等级 0.45 + 提取方式 0.25 + 跨源一致性 0.20 + 时效 0.10` 派生可信度，
  每条事实的标签里都能看到这个算式。

## 2. 评测集与审计记录

- `eval/` 下的题目集（`eval_set.json` / `eval_api.json` / `eval_tables.json`）与评分脚本
  由本项目编写，**题目与脚本**版权归本项目（MIT）。其中的参考答案引用了上述站点的内容摘要。
- `eval/report-*.json` 是运行产物，**不入库**（已在 `.gitignore` 中排除）。
- `docs/` 下的审计与验证报告是项目自己的工作记录，其中引用的第三方页面片段同样归原站点所有。

## 3. 上游开源依赖与其许可证

运行时依赖（`requirements.txt`，版本以 `requirements.lock.txt` 为准）：

| 依赖 | 许可证 | 说明 |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | 本机 HTTP 服务 |
| [uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI 服务器 |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | HTTP 客户端 |
| [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) | MIT | HTML 解析（依赖 soupsieve，MIT） |
| [lxml](https://lxml.de/) | BSD-3-Clause | 解析后端 |
| [trafilatura](https://github.com/adbar/trafilatura) | Apache-2.0 | 正文抽取（随包 `justext` BSD-2-Clause、`courlan` Apache-2.0、`htmldate` Apache-2.0、`dateparser` BSD-3-Clause） |
| [ddgs](https://github.com/deedy5/ddgs) | MIT | 联网检索 |
| [pydantic](https://docs.pydantic.dev/) | MIT | 接口请求体校验（`app/server/api.py` 直接 import） |
| [starlette](https://www.starlette.io/) | BSD-3-Clause | FastAPI 的底层 ASGI 框架（`app/server/api.py` 直接 import） |
| [pywebview](https://pywebview.flowrl.com/) | BSD-3-Clause | 原生窗口（Windows 上依赖 `pythonnet`/`clr_loader`，均 MIT，以及 `proxy_tools`，见下） |

开发/打包期依赖（`requirements-dev.txt`，**不进入 exe 运行时**）：

| 依赖 | 许可证 | 说明 |
|---|---|---|
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0-or-later，**附打包例外条款** | 仅用于生成 exe；其例外条款允许以任何许可证分发打包产物，故 MIT 应用可正常分发 |
| [Pillow](https://python-pillow.org/) | MIT-CMU | 仅 `tools/make_icon.py` / 截图脚本使用 |
| [pythonnet](https://github.com/pythonnet/pythonnet) + [clr_loader](https://github.com/pythonnet/clr-loader) | MIT | pywebview 的 Windows 后端 |

其它组件：

- **`proxy_tools`**（`0.1.0`，MIT）是 pywebview 的传递依赖，随 exe 一起打包。
  它没有发布 `.dist-info` 元数据，因此版本号取自 PyPI 上的对应发布（详见
  `requirements.lock.txt` 文末说明）。
- **Microsoft Edge WebView2 Runtime** 是 pywebview 在 Windows 上渲染窗口所依赖的
  **系统组件**，由微软按 [WebView2 再分发条款](https://developer.microsoft.com/microsoft-edge/webview2/)
  提供。本仓库**不分发**该运行时；程序启动时只做一次能力探测，
  探测失败会明确提示用户在系统里安装，而不是静默失败
  （见 `app/main.py` 的 `probe_webview_supported()`）。
- 以上依赖**均未做修改**，各自遵循其自身许可证。若你需要严格的自证清单与哈希，
  `requirements.lock.txt` 记录了本项目实测通过的精确版本，但**不含**各包的许可正文。
  需要许可全文时请从上游发行版获取。
- 传递依赖中有两个许可证与上表不同族：`certifi`（**MPL-2.0**，httpx 的 CA 证书包）
  与 `tld`（**MPL-1.1 / GPL-2.0-only / LGPL-2.1-or-later 三选一**，courlan 的依赖）。
  二者都以未修改的二进制形式随包分发，符合其许可条款；
  如果你的分发场景对 MPL/LGPL 有额外要求，请自行评估。

## 4. 商标与同人声明

《异环》（Neverness to Everness / NTE）的名称、角色与相关素材的权利归其开发商与发行商所有。
本项目是**玩家自制的非官方工具**，与官方团队没有隶属或合作关系，也不包含任何官方美术资源。
程序中展示的结论均标注来源，不代表官方说法。
