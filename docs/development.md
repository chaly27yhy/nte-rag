# 开发、打包与踩坑记录

README 只保留**用户使用**部分（快速开始、使用说明、常见问题、数据来源与授权）。
这一页收录与使用者无关的专业内容：评测集怎么跑、环境搭建与依赖、运行与调试、开发期 `.env`、
自带诊断工具、重建种子知识库、打包与发布、发布前验证、平台适配、项目结构与入口文件说明，
以及开发过程中踩过的坑（无控制台陷阱、GBK 控制台、MediaWiki JSON 被当 HTML 抽取、PowerShell 编码等）。

返回入口：[`README.md`](../README.md) ｜ 架构与取舍：[`architecture.md`](architecture.md) ｜
评测集说明：[`../eval/README.md`](../eval/README.md)

> 这一页各部分的写作时间不同，正文里出现的数字（断言数、体积、哈希）都是**各自那一步的快照**：
> 断言总数与产物哈希的最新值以 [`README.md`](../README.md) 和 `tools/quality_check.py` 的实际输出为准。

---

## 评测

「这次改动到底让答案更准了还是更差了」不能凭感觉判断，因此仓库里带了一套可复跑的评测集，
放在 [`eval/`](../eval/README.md)（评测数据**不随程序分发**，不进 exe、不进种子库）：

| 文件 | 内容 |
|---|---|
| [`eval/eval_set.json`](../eval/eval_set.json) | 主评测集 **60 题**，人工逐题审核过（`review.status` = ok / fixed / drop） |
| [`eval/eval_api.json`](../eval/eval_api.json) | **21 题**，聚焦结构化接口（MediaWiki 模板 → 原子条目）的抽取质量 |
| [`eval/eval_tables.json`](../eval/eval_tables.json) | **18 题**，聚焦表格类数值（角色初始数值、弧盘效果等） |
| [`eval/kb_report.json`](../eval/kb_report.json) | 知识库快照体检报告（`tools/kb_report.py` 输出） |

怎么跑、每题的结构与判分规则（要点覆盖率 / 来源命中率 / 该拒答时能否拒答）、
以及 A/B 对比的做法，都写在 [`eval/README.md`](../eval/README.md) 里。要点：

```powershell
# 跑主评测集（需要先配好模型；--limit 只跑前 N 题，--web 允许联网补充，--tag 给报告打标记）
.venv\Scripts\python.exe tools\run_eval.py --limit 10
```

- **评分需要配好可用的模型**，会产生真实 API 费用；不配模型时只有 `tools/kb_gap_report.py`
  这类**离线**检查可用（它只核对「这道题的期望要点在本地库里有没有依据」，不花钱、不联网）。
- `eval/report-*.json` 是运行产物，不入库。

---
## 环境搭建、运行与打包

### 环境要求

- Windows 10/11 x64
- Python 3.12（本仓库用 3.12.10 验证）
- 无需 Node.js（前端是原生 HTML/CSS/JS，没有构建链）

### 安装依赖

普通机器（推荐，一条命令）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

> 关键是**用 venv 自己的解释器**装进 venv。不要用系统 `python` 跑 `pip install`，
> 否则依赖会落到全局 site-packages，而 `run.py` 用的是 `.venv` 里的解释器。

受限环境（沙箱、企业策略、受限账户）里的备用方式：

```powershell
python -m venv .venv
# 这类环境下 venv 里可能连 pip 都没有（ensurepip 被拦），所以用带 pip 的 Python
# 把包装进 venv 的 site-packages
python tools\pip_runner.py install --target .venv\Lib\site-packages -r requirements-dev.txt
```

> 受限环境里 `tempfile.mkdtemp()` 创建的目录（内部以 `0o700` 创建）会拒绝后续写入，
> pip 解包 wheel 会报 `[Errno 13] Permission denied`，所以要 `tools\pip_runner.py`：
> 它只是把 `mkdtemp` 换成默认权限实现，不影响产物。
> 旧式 `setup.py` 包（如 pywebview 的唯一依赖 `proxy_tools`）在同一环境下也会失败，
> 可用 `tools\fetch_pure_sdist.py` 直接解包：
> ```powershell
> python tools\fetch_pure_sdist.py proxy_tools --target .venv\Lib\site-packages
> pip install --target .venv\Lib\site-packages --no-deps pywebview bottle
> ```

### 运行与调试

```powershell
# 开发态启动（打开桌面窗口；--no-window 用浏览器，--headless 只起服务）
.venv\Scripts\python.exe run.py

# 只起服务，不打开界面
.venv\Scripts\python.exe run.py --headless --port 8765

# 自检：启动服务 → 验鉴权/接口/知识库 → 输出 JSON 报告并写盘
.venv\Scripts\python.exe run.py --selftest

# 窗口冒烟测试：开窗数秒后自动关闭（退出码 2 = 本机 WebView2 不可用，属预期）
.venv\Scripts\python.exe run.py --window-test
```

> 也可以直接用 `.venv\Scripts\python.exe -m app.main <参数>`，效果相同。

### ⚠️ 窗口程序的「无控制台」陷阱

打包成 `console=False` 的窗口程序后，**双击启动时 `sys.stdout` 与 `sys.stderr` 都是 `None`**。
这会引发一类只在双击时出现、用命令行参数无法复现的崩溃：

- **uvicorn 默认日志格式器**（`uvicorn/logging.py`）在 `__init__` 里对 `sys.stdout.isatty()` 求值，
  `None.isatty()` 直接抛 `AttributeError`，被 `logging.config.dictConfig` 包成
  `Unable to configure formatter 'default'`，表现为**双击后弹「本地服务启动失败」**。
  本项目通过给 uvicorn 传 `log_config=None` 绕开了它（窗口程序本来也不需要它的彩色控制台格式）。
- **任何裸 `print()`** 都会抛 `AttributeError`；本项目统一走 `app/main.py` 的 `_say()`
  （stdout 为 None 时写空设备或落日志，绝不抛异常）。
- `logging.StreamHandler(sys.stderr)` 不能在 `sys.stderr is None` 时创建，`setup_logging()` 已做判断。

另有一个诊断开关 `--no-console`，它把 `sys.stdout/stderr` 置空，能精确复现双击时的状态。例如：

```powershell
# 复现「双击条件」下的完整自检（报告仍会写盘）
.venv\Scripts\python.exe run.py --selftest --no-console
```

`tools\verify_exe.ps1` 里的服务实跑与窗口冒烟测试**都带上了 `--no-console`**，
以覆盖这个盲区。

### 开发期配置 `.env`

复制 `.env.example` 为 `.env` 并填入自己的测试 Key：

```ini
NTE_RAG_DEV_ENV=1
NTE_RAG_DEV_LLM_PROVIDER=openai
NTE_RAG_DEV_LLM_BASE_URL=https://api.deepseek.com/v1
NTE_RAG_DEV_LLM_API_KEY=你的测试Key
NTE_RAG_DEV_LLM_MODEL=deepseek-chat
NTE_RAG_DEV_SEARCH_PROVIDER=bocha
NTE_RAG_DEV_SEARCH_API_KEY=你的博查Key
```

`.env` 有**两道闸门**，两道都满足才生效：只在非打包状态下起作用（`apply_dev_env()` 里的 `is_frozen()` 判断），
**并且必须显式写上 `NTE_RAG_DEV_ENV=1`**。没有这一行，整个 `.env` 会被忽略：写入的只是**空着的**密钥字段，
界面里已经保存好的 Key 不会被覆盖。`.env` 已被 `.gitignore`、`.spec`、`secret_scan.py` 三重排除。

> 进程级开关也走同一套前缀：`NTE_RAG_DATA_DIR` 指定数据目录、`NTE_RAG_PORTABLE=1`
> 强制便携模式（`=0` 强制 `%APPDATA%`）、`NTE_RAG_DISABLE_AUTH=1` 关闭接口鉴权
> （只给自动化测试用）。名字与读取规则集中定义在 `app/core/env.py`。

### 自带诊断工具

```powershell
.venv\Scripts\python.exe tools\crawl_probe.py --list  # 列出内置数据源
.venv\Scripts\python.exe tools\crawl_probe.py --source official_lore  # 试抓某个数据源，看每页抽到多少字
.venv\Scripts\python.exe tools\crawl_probe.py --url "https://example.com/a" --compare  # 对比多种正文抽取策略（站点改版时定位问题）
.venv\Scripts\python.exe tools\kb_probe.py "薄荷" --data-dir .seed_check  # 检索诊断：这个问题到底命中了什么
# 数据质量机制自测：清洗规则 / 来源黑名单 / 官方优先裁决 / 表格抽取 / 抓取失败可见性 / 种子库升级 /
# 界面与合规 / 可信度派生 / 结构化接口 / JSON 响应不被 HTML 抽取吃掉 / 模板默认值与废话字段 /
# 重复合并 / 知识库缺口口径 / 证据选择 / 版本与时效 / 评测基线数字与报告 JSON 逐列对账 /
# 第二来源适配 / 冲突行不许被种子导出丢弃（薄荷生日那次的真 bug）/ 改名回归 / 发布件回归 /
# 三份审计的修复回归（长问题 500 / 版本号 / superseded 投票 / 接地检验 / 种子指纹）/
# 第二轮健壮性回归（请求级总超时 / robots 读不到就不抓 / 重试退避与 Retry-After / 连接池复用 /
# 冷却跨实例共享 / 内容类型分流 / 壁纸边收边判 / 前端竞态与流式节流）/
# 第三批修复回归（自检壁纸往返 / 畸形 URL / 表格串表 / 授权口径）/ 首启向导的 inert 作用域 /
# 关于页与 README 的关键信息同步 /
# 第四批修复回归（robots 语义 / 字词站与视频页的预置黑名单 / 主题相关性闸门）/
# 第五批修复回归（关于页能脱离 README 单独读 / 模型 API 标成可选）/
# 第六批修复回归（更新报告的计数口径：取到正文 / 新页面 / 内容未变 / 按规则跳过 / 抓取失败）/
# 第七批修复回归（已证伪来源的页面级撤回：写入侧、种子导入侧、老库启动迁移）/
# 第八批修复回归（设置页拉取模型的探针地址采用规则：两种请求体形态、换主机要重填 Key）/
# 第九批修复回归（CI 工作流能被 GitHub 接受：工作流级只写字面量、数据目录在步骤里设置）/
# 698 项断言，
# 不需要模型也不需要网络；--quiet 只打印失败项
.venv\Scripts\python.exe tools\quality_check.py --quiet
.venv\Scripts\python.exe tools\wiki_api_check.py --list  # 结构化接口（BWIKI 模板字段）端到端：--offline 只用缓存
.venv\Scripts\python.exe tools\wiki_api_check.py --source bwiki_api_arc --limit 46  # 批量取原文（一次 20 页），46 条弧盘约 3 次请求
.venv\Scripts\python.exe tools\wiki_api_check.py --category 角色 --also 角色图鉴,自机角色  # 分类名被站点改掉时的探针（每个名字 1 次请求）
.venv\Scripts\python.exe tools\field_report.py  # 字段体检：同一模板/分类里哪些字段「所有页面同值」（= 模板默认值，不能当实体属性入库）
.venv\Scripts\python.exe tools\field_report.py --caliber  # 口径对照：BWIKI「生命/攻击」与玩一玩「初始生命/初始攻击」能否当同一指标（结论：不能）
.venv\Scripts\python.exe tools\build_api_eval.py --limit-chars 6 --limit-arcs 6  # 结构化字段专项题集：从缓存出题，自带「旧种子已能答」的区分度守卫
.venv\Scripts\python.exe tools\run_eval.py --set eval\eval_api.json --tag api-new
# 换种子做 A/B：--seed 指定任意种子文件，必须配一个全新的空 --data-dir
.venv\Scripts\python.exe tools\run_eval.py --set eval\eval_api.json `
    --seed <旧的种子文件>.json --data-dir .eval_api_old --tag api-old
.venv\Scripts\python.exe tools\dedupe_report.py  # 近似重复/冲突体检：扫种子文件或本地库，--merge 才真的合并
.venv\Scripts\python.exe tools\dedupe_report.py --data-dir .eval_data --merge
.venv\Scripts\python.exe tools\consistency_report.py --file seed\seed_kb.json --show 40  # 跨来源一致性报告：同一 (实体, 字段) 被几个独立域名写过、值是否一致（默认只预演不落盘）
.venv\Scripts\python.exe tools\consistency_report.py --data-dir .eval_data --verdict conflict --apply
.venv\Scripts\python.exe tools\build_eval_set.py --count 60  # 评测集：生成候选题 → 人工审核 eval/eval_set.json → 跑分
.venv\Scripts\python.exe tools\run_eval.py --tag before
# 离线核对「这道题的期望要点在本地库里到底有没有依据」（不联网、不花钱、不用模型）：数字缺失=强信号，
# 措辞缺失=提示；结论由人工写回评测集（kb_gap + review.comment）
.venv\Scripts\python.exe tools\kb_gap_report.py
.venv\Scripts\python.exe tools\kb_gap_report.py --include-drop --json-out eval\kb_gap.json
.venv\Scripts\python.exe tools\run_eval.py --set eval\eval_tables.json --tag tables  # 表格/数值专项题集（18 题，用来单独衡量表格抽取的效果）
.venv\Scripts\python.exe tools\crawl_probe.py --from-seed --tables  # 离线核对表格解析结果（不联网，直接读种子库里的切片）
.venv\Scripts\python.exe tools\repair_eval_notes.py --apply --in-place  # 中文批注误写到 JSON 结构外（会让文件无法解析）时迁移回 review.comment
.venv\Scripts\python.exe tools\secret_scan.py  # 密钥扫描（发布门禁）
.venv\Scripts\python.exe tools\make_version_info.py  # 生成 exe 用的 Windows 版本资源（--check 只校验是否与 __version__ 一致）
# 生成 README 里的界面截图：先起一个 --headless 服务，再让无头 Edge 逐张切页/切主题截图
# （空数据目录时加 --fresh，会多截一张首次使用向导）
.venv\Scripts\python.exe -m app.main --headless
.venv\Scripts\python.exe tools\make_readme_shots.py --port <上面的端口> --out docs\screenshots
```

> `tools/make_readme_shots.py` 直接用 Edge 的 DevTools Protocol（不需要 Playwright / Selenium），
> 只用标准库加 `websocket-client`；它每次截图后都会**回读一次 DOM** 并把页面文本打进日志，
> 所以截图内容是可核对的，不是黑盒。

> 工具脚本都 import 了 `tools/_console.py`：Windows 控制台默认 GBK，
> `✅`/`✓` 这类符号编码不了会让 `print` 抛异常，这个坑发生过两次。
> `secret_scan.py` 在**成功**那一行崩掉，构建脚本因此误报「发现密钥泄漏」；
> `seed_builder.py` 则在抓完页面准备输出汇总时崩掉，白抓一轮。

> 另一个坑出在结构化接口上。抓取层原本对所有响应都跑 HTML 正文抽取
> （BeautifulSoup + trafilatura）。MediaWiki 的 JSON 响应里带原文，`<br>`、`<span>`
> 会被当成 HTML 标签吃掉，JSON 尾部整段消失 → 「不是合法 JSON」→ 一次 20 页的批量请求
> **整批判为失败**（弧盘 44 页里有 20 页因此丢了很久）。现在 `app/core/fetch.py` 会先看
> `Content-Type`：JSON 响应直接原样解码，不进 HTML 抽取。
> 断言里用本地 `http.server` 起了个返回 JSON 的假接口守住这条（`quality_check.py` 【6】）。

> **行内放行标记（合成样本专用）**：自检脚本 `tools/quality_check.py` 必须拿看起来像真密钥的合成串来验证脱敏
> 逻辑（`sk-…`、`X-API-KEY: …` 之类），这些合成串会被同一套门禁判成泄漏，
> 把 `tools/build_exe.ps1` 卡在第 3 步（2026-09-23 卡住过一次）。现在两种做法配合使用：
> 一是**运行时拼装**（`"sk-" + "a"*27`），源码里不存在可直接复制的完整串，这是首选做法；
> 二是确实要写成字面量时，在**同一行行尾**加注释 `# secret-scan: allow`。
> 标记只对该行生效、且必须写在 `#` 或 `//` 注释里；`.env` 里真实密钥的精确指纹
> **不受标记影响**，永远拦截。判据由 `quality_check.py` 【23】守住。

> **robots 语义与「抓回无关页面」（2026-09-24 发现）。** 在打包版里点
> 「立即更新」，报告里出现 5 条 `robots.txt` 相关错误，官网 `yh.wanmei.com` 每轮都被跳过。
> 查下来是 `allowed()` 只认 `status == "ok"`：官网的 `robots.txt` 是 **HTTP 200 但空文件**，
> 被判成 `missing`，而 `note()` 却写着「robots.txt 不存在，允许抓取」——行为与文案互相矛盾，
> 这句还作为「错误」写进了更新报告。现在按 RFC 9309 处理：404 或 200 空文件都算
> 「站点没有声明任何规则」→ 允许抓取；只有读不到（网络错误、403、超时、5xx）才保守不抓。
> 回归断言【31】。修复后实测官网可以抓取（正文 181 字符——官网是 JS 壳，只够当叙事来源）。
>
> 同一轮还有一批「跟游戏无关」的页面进了库：`hanyuguoxue.com`、`hgcha.com` 的汉字字典页
> （搜索把「异环 地图 区域 探索」拆出了「异」这个字，返回字典条目），以及 B 站视频页
> （抓到的正文只有标题与推荐列表）。`quality.validate_page()` 只看长度、信息密度、
> 跨作品污染，字典页又长又密，全部放行——**搜索层的分词质量不该由清洗层兜底**，
> 现在在入库层加了两道默认闸门：字词/字典站与视频页进预置黑名单（发请求之前就跳过），
> 社区来源的页面要求标题或正文里出现主题词（默认取各更新主题的共有词「异环」）。
> 百度百科整站保留（那里有正经《异环》词条），只封 `hanyu.baidu.com` 子域；
> 官方站与 wiki 站不受主题闸门约束（BWIKI 模板页标题本来就不带游戏名）。
> 已入库的 6 篇垃圾页连同 16 个切片一并删掉。
>
> **「关于」页要能脱离 README 单独读（2026-09-24）。** 那一页原本只有免责声明、项目背景与运行信息，
> 不读 README 就看不出这程序能干哪些事、资料从哪来、代码与资料各是什么许可。现在补了
> 「这个程序能做什么」（五个页签各自的作用 + 内置资料规模）与「授权与许可」（代码 MIT、
> 内置资料走 `DATA_LICENSE.md`、依赖与署名见 `THIRD_PARTY_NOTICES.md`、不含官方美术素材），
> 抓取那条也补上预置黑名单与主题词闸门。顺带修掉一处不准确：设置页写的是「模型 API（必填）」，
> 但没配模型时程序会退化为「本地资料直出」，并非不可用——现在改叫「可选」，`README.md` 同步。
> 界面改了就要重出截图（`06-settings.png`、`07-about.png`），重出用
> `tools/make_readme_shots.py`；该脚本的 CDP 通道**偶发卡死**（第一次跑就卡在连接阶段，
> 清掉自己留下的 `msedge.exe` 后重跑即成功），稳妥做法是后台跑 + 超时就杀进程重来。
> 新截图还要用 Pillow 无损重压（`optimize=True, compress_level=9`，只在像素字节完全一致且更小时替换），
> 06 与 07 分别从 372 KB / 410 KB 压到 343 KB / 399 KB。

> **更新报告的计数口径（2026-09-24 发现）。** 这轮日志写着「抓取 17 页，
> 过滤低质量页面 1 页，8 条错误」，可同一批主题里实际只碰到 **15 个不同页面**。查出来是三个原因：
> ①按 robots 规则主动跳过的页面（官网 3 次、百度百科「异」2 次）被当成失败写进了「错误」；
> ②被清洗层拒收的那 1 页同时进了「过滤」和「错误」；③内容没变的重抓在判断 `changed` 之前
> 就把 `pages` 加了 1，所以重复抓到的页面也算「新页面」。现在报告拆成六个计数：
> 「取到正文 / 新页面（+ 内容未变）/ 按 robots、黑名单规则跳过 / 过滤 / 抓取失败 / 错误」，
> 且 `取到正文 = 新页面 + 过滤 + 内容未变`；错误明细不再每个主题只留前 3 条。
> 更新页的状态卡同步改成同一套说法（以前只有一个「抓取 N 页」，看到「抓取 17 页却新增 0 条」
> 会以为更新没起作用）。**`ingest_sources()` 那条内置数据源路径有同样的「被拒页面同时进过滤与错误」
> 写法，这一处刻意未改**——它的错误列表被 `tools/seed_builder.py` 与 `tools/wiki_api_check.py` 消费，
> 改动影响面更大。回归断言【32】。
>
> **「同一个问题出现两个矛盾说法」（2026-09-24 发现），根因在一张已证伪的页面上。**
> 以截图 02/03 里那句「薄荷的生日是哪天？」为例：生日早就人工核对过是 6月1日，
> 为什么助手还是答「一条写 6月1日，另一条结构化字段写 8月20日」。查下来，条目层只剩人工裁定过的
> `薄荷·生日 = 6月1日`，8月20日 是 **BWIKI 薄荷页的正文切片**——那张页面早前被查实技能抄自另一款游戏的
> 角色、人物故事抄自同站早雾页、生日与 CV 与同站娜娜莉页相同，当时只删了它派生的 13 条条目、
> 给 6 条打了「存疑」，**页面本身仍以 `active` 留在库里**，检索层继续把它当证据送进提示词。
> 只删字段不够，因为这张页面没有任何一个字段能证明可信。
> 现在新增 `app/core/curation.py`：一份「已证伪页面」名单 + `KnowledgeBase.revoke_source()`，
> 把命中页面派生的 `documents`/`chunks`/`facts` 全部置为 `revoked`（软撤回，记录留库供复核，
> 检索只读 `active`/`conflict`），写入侧、种子导入侧、老库启动迁移三处一起收。
> **种子文件本身不改**：改了它会变 `seed_fingerprint`、让所有老用户重导一遍，
> 所以过滤放在 `load_seed()` 里，名单放在代码里。修完重跑同一个问题，回答只剩「薄荷的生日是 6月1日」；
> 开发库 84 篇/747 条 → 83/744，发行库 82/685 → 81/682。回归断言【33】。
>
> **「设置页拉取模型失败、向导却能拉」的那个探针（2026-09-24 发现）。**
> 现象是设置页里自己填 Base URL 和 Key、点「拉取可用模型」失败，而首启向导里同样配置能拉到。
> 查出来两层：①界面发的请求体是**扁平**的 section 字段（`llmPayload()` 直接返回 `llm` 的字段），
> 探针却按 `payload.get(section)` 读，等于整份请求体都没被读到，探针一直在拿旧配置探测；
> ②即使读到了，探针也会把请求体里的 Base URL 一律丢掉，只认服务端预设表或上一次保存的地址。
> 向导每一步（选服务商、填 Key、选模型）都先保存再走下一步，它的请求正好打在自己刚存下的地址上，
> 所以看不出问题。现在探针两种请求体形态都认，地址的采用规则是：带新密钥就采用它带来的地址
> （密钥是请求方自己给的，没有外泄路径）；不带新密钥却要换主机（包括只换服务商）时明确报
> 「更换模型服务地址后需要重新填写 API Key」，而不是悄悄用旧地址，也不把已存密钥送到新主机；
> 没有旧密钥可泄、或主机没变时照用请求里的地址。掩码串不算新密钥，判据
> `secrets.is_mask_value()` 与 `Config.set_secret` 共用，免得出现「提示重填 Key、填了同样的 Key 仍被拒」。
> 探针改成模块级函数，只动 `config.detached_copy()` 的一次性副本，活配置的内存与磁盘都不变。
> 用无头 Edge 真点设置页时还看到两处界面问题，一并修了：失败提示原先不带程序实际请求的地址，
> 而且程序自己发的那句「更换模型服务地址后需要重新填写 API Key」会被前端错误归类改写成
> 「模型服务拒绝了这次请求（密钥无效或无权限）」——会被误读成 Key 打错了；现在这句有专门的
> 归类与提示，判定顺序也被断言钉住。回归断言【34】。

> **CI 工作流被 GitHub 静默拒收（【35】）。** 推送之后，Actions 里出现两条
> `completed / failure` 但**一个 job 都没有**的记录，页面上写着 `Invalid workflow file:
> Unrecognized named-value: 'runner'`。根因是 `.github/workflows/ci.yml` 的工作流级 `env` 里写了
> `NTE_RAG_DATA_DIR: ${{ runner.temp }}\nte-rag-ci`：`runner` 上下文在工作流级的 `env` 里不可用，
> GitHub 在解析阶段就整份拒收，本地把同样的命令跑一遍完全看不出问题（本地验证过的是「命令在干净
> clone 里能过」，没验证「工作流文件能被接受」）。改法是把数据目录挪进自检步骤
> （`$env:NTE_RAG_DATA_DIR = $dataDir`，`$dataDir` 仍取自 `$env:RUNNER_TEMP`），工作流级只留字面量。
> 自检【35】钉住这条：工作流级不允许出现 `${{ }}`（注释行不计），数据目录必须在步骤里设置，
> 退出码 / 失败 0 / 全部通过 / 总数下界 / 密钥门禁 / 失败留档六项也一并钉住。回归断言【35】。

### 重建种子知识库

```powershell
.venv\Scripts\python.exe tools\seed_builder.py --out seed\seed_kb.json `
    --sources official_lore,official_news,moegirl_yihuan,bwiki_characters `
    --per-source-limit 8 --with-llm
```

`--with-llm` 需要 `.env` 里配好模型，会额外抽取结构化知识条目（种子库质量更高）。

**防退化**：如果本批条目数少于已有种子（多半是数据源被限流或封禁），
新结果会另存为 `seed_kb.partial.json`，**不会覆盖**原种子。这不是理论问题。
BWIKI 被 WAF 拦过一次，223 条的种子被覆盖成 110 条，表格条目从 79 掉到 0。

### 打包

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
```

脚本会依次执行：依赖检查 → 生成图标 → **构建前密钥扫描** → 源码自检 →
PyInstaller 打包（**单文件 + 便携目录两种形态**）→ **构建后扫描两份 exe 的二进制** →
打包 zip → 输出体积与 SHA256。

> 构建脚本自身还有两个坑，都不影响产物，但容易误判成「构建失败」：
> 1. **`tools/build_exe.ps1` 与 `tools/verify_exe.ps1` 必须保持 UTF-8 带 BOM**：无 BOM 时 PowerShell 5.1
>    按 ANSI 解码、中文注释会把字符串引号吃掉，报 `The string is missing the terminator`。
>    用编辑器改写这两个文件时容易丢掉 BOM，而一旦丢掉，本机自检会因此少掉 8 条断言
>    （`quality_check.py`【22】核对这两个文件的前三字节必须是 `EF BB BF`，正是按这个下界守住）。
> 2. **自检的 stderr 会污染构建脚本的退出码。** `app.main --selftest` 会把 INFO 日志写到 stderr，
>    PowerShell 5.1 又把原生程序的 stderr 包成 `NativeCommandError`；实测「管道」与 `2> 文件` 两种写法
>    都会让**整个构建成功却以退出码 1 结束**；脚本现在用 cmd 把 fd2 接到
>    `.build_selftest\selftest_stderr.log`，PowerShell 看不到 stderr，完整日志仍留在磁盘上。

产物：

```
dist\NTE-RAG.exe                     # 单文件版
dist\NTE-RAG\                        # 便携目录版（exe + _internal）
dist\NTE-RAG-onedir.zip              # 便携目录版的压缩包（解压后得到 NTE-RAG\ 文件夹）
```

> 压缩包里带顶层文件夹是刻意的：早先用 `Compress-Archive -Path 目录\*` 打出来的包里
> 没有这一层，解压会把 2600 多个文件倒进当前目录。现在传的是目录本身。
>
> `tools/make_version_info.py` 会在打包前生成 `assets/version_info.txt`（Windows 版本资源），
> 所以两个 exe 的属性面板里能看到产品名与版本号。版本号唯一来源是 `app/__init__.py` 的
> `__version__`；该文件是构建产物、**不提交**（`.gitignore` 已忽略）。

### 发布前验证

```powershell
powershell -ExecutionPolicy Bypass -File tools\verify_exe.ps1 -Exe 'dist\NTE-RAG\NTE-RAG.exe'
```

该脚本会在**全新的空目录**里模拟用户首次拿到产物的场景，逐项验证：

1. 产物信息（体积、SHA256、形态自动识别）；
2. `--selftest` 独立自检（11 项）：鉴权是否生效、静态资源是否齐全、知识库可读写、种子库是否导入、
   **界面结构完整（`ui_markup`）**、**壁纸接口读写后会原样还原用户既有壁纸（`wallpaper_roundtrip`）**；
3. 窗口冒烟测试（`--window-test`，开窗数秒后自动关闭，不留残留）；
4. `--headless` 实跑：轮询 HTTP 健康检查，验证无令牌请求被拒绝（403）、带令牌可正常访问；
5. 干净目录中不含任何密钥配置；
6. 对 exe 二进制做密钥扫描；
7. 分发目录清洁度检查（不含 `.env` 与私钥文件）。

脚本同时支持两种形态：检测到 `_internal` 目录就整体复制，否则按单文件处理。

### 平台要求与验证结论

平台相关的要求与限制（与具体开发机器无关）：

| 平台要求 | 说明 |
|---|---|
| **仅 Windows** | 运行与构建都只支持 Windows 10/11 x64（构建脚本用 PowerShell 5.1 + PyInstaller） |
| **原生窗口依赖 WebView2 / .NET（pythonnet）** | 在**子进程**中探测并缓存结果，不可用时自动改用默认浏览器，功能一致、不会因此崩溃 |
| **密钥绑定 Windows 账户** | API Key 用 Windows DPAPI 加密，只有当前 Windows 账户能解密；换机器或换账户则自动失效并要求重填 |
| **构建脚本必须带 UTF-8 BOM** | `tools\build_exe.ps1`、`tools\verify_exe.ps1` 在无 BOM 时中文注释会导致 PowerShell 5.1 语法解析失败 |

在受限验证环境下实测到的结论（供在自己的机器上对照）：

- 单文件版与便携目录版**均全部验证通过**（自检 11/11、服务实跑、鉴权 403/200、种子库导入、
  两次二进制密钥扫描、`--no-console` 双击条件复现）；单文件版首次启动需自解包，数秒延迟属正常。
- 真实抓取（官网设定页与新闻列表、BWIKI 含 API 枚举、萌娘百科）无错误；真实模型问答正常
  （流式 2.7s / 731 字 / 8 条引用）；免 Key 的 DuckDuckGo 兜底返回 5 条结果；
  中文检索相关查询相关性 0.766、无关查询 0.000（正确触发联网）。
- 界面结构、图标与元素 id 一致性、壁纸接口往返与两层适配、向导回退结构均已自动化验证
  （见 `tools/quality_check.py` 全量 **698** 项断言与自检的 `ui_markup` / `wallpaper_roundtrip` / `starter_asks`）。
- 复现方式：把开发用 Key 填入 `.env` 后执行 `.venv\Scripts\python.exe tools\live_check.py`。

### 发布（GitHub Releases）

仓库里只放源码，不放构建产物。`dist/` 已被 `.gitignore` 忽略，任何 exe / zip 都不进版本库
（二进制会迅速把仓库体积撑大，而且 Git 无法给出有意义的 diff，也容易被误以为源码可直接运行）。
发布流程：

```powershell
# 1. 提交源码（构建产物不会出现在待提交列表里，可用 git status 复核）
git add -A
git commit -m "NTE-RAG 1.0.0"

# 2. 打标签并推送
git tag v1.0.0
git push -u origin main
git push origin v1.0.0

# 3. 记录产物哈希（贴进 Release 说明）
Get-FileHash dist\NTE-RAG.exe -Algorithm SHA256
Get-FileHash dist\NTE-RAG-onedir.zip -Algorithm SHA256
```

然后到 GitHub 的 **Releases → Draft a new release**，选 `v1.0.0`，**把下面这些作为附件上传**：

| Release 附件 | 来源 | 说明 |
|---|---|---|
| `NTE-RAG-onedir.zip`（约 40.8 MB） | `dist\NTE-RAG-onedir.zip` | **推荐下载**。解压得到一个 `NTE-RAG\` 文件夹，双击里面的 `NTE-RAG.exe` |
| `NTE-RAG.exe`（约 40.5 MB） | `dist\NTE-RAG.exe` | 单文件版，免解压；首次启动会自解包到临时目录，稍慢，且可能被杀软/企业策略拦截 |
| `SHA256SUMS.txt`（可选） | 把上面两条 `Get-FileHash` 的输出存成文本 | 让用户能核对下载到的文件没被篡改 |

发布注意事项：

- GitHub 会自动为标签附带 *Source code (zip/tar.gz)*，那就是源码，不用自己再压一份；
- 不要提交 `dist/`（已忽略）、`assets/version_info.txt`（构建产物，已忽略）与 `data/`（运行时数据）；
- Release 说明里建议写明「中文优先、非官方、素材版权归原站」，与 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) 说法一致；
- 1.0.0 的实测哈希（2026-09-24 最后一次重建，含首启向导 `inert` 修复、「关于」页扩写、抓取闸门修正、更新报告的计数口径修正、已证伪来源的页面级撤回、设置页拉取模型的探针地址修正，以及仓库行尾统一）：
  `NTE-RAG.exe` =
  `B390EE2B3D1A37E579BD794B79BDB4157DB5EFC0DE8757F65E5E71A1C8AE12F5`（42,513,558 B），
  `NTE-RAG-onedir.zip` = `934C2698C536D34BE4AD6278CA8510081F8CD6ACA9A2503A037F223EFD940EEC`（42,773,055 B、
  2610 条目、顶层只有 `NTE-RAG\`）。
  重打包后哈希必然变化，请以当次 `build_exe.ps1` 的输出为准。

---
## 项目结构

```
nte-rag/
├─ run.py                  # 运行/打包入口（先建立包上下文，再调用 app.main）
├─ app/
│  ├─ main.py              # 起本地服务 + 窗口/浏览器；含 --selftest/--headless/--window-test
│  ├─ config.py            # 配置读写、默认值合并、密钥字段的加解密出口
│  ├─ core/
│  │  ├─ paths.py          # 只读资源 vs 可写数据；便携模式判定
│  │  ├─ secrets.py        # DPAPI 加解密、掩码、日志脱敏、密钥特征识别
│  │  ├─ llm.py            # 三协议模型客户端（OpenAI 兼容 / Anthropic / Gemini）
│  │  ├─ search.py         # 搜索提供商（博查/Tavily/Serper/DDG/必应）+ 降级链
│  │  ├─ fetch.py          # 抓取 + robots 策略 + 正文抽取 + 退避重试
│  │  ├─ chunk.py          # 中文 bigram 分词、切块、SimHash
│  │  ├─ store.py          # SQLite + FTS5：documents / chunks / facts / 日志 / 主题队列
│  │  ├─ ingest.py         # 入库流水线：抽条目、判定重复/更新/冲突、按主题更新
│  │  ├─ sources.py        # 内置数据源目录 + 三种连接器（page/list/mw_allpages）
│  │  ├─ autoupdate.py     # 更新调度器：定时、启动、手动、进度上报
│  │  ├─ starter.py        # 开场推荐问题：从知识库随机抽 + 套问句
│  │  └─ rag.py            # 问答链路：检索 → 联网补齐 → 带引用作答
│  ├─ server/api.py        # FastAPI 接口（含一次性令牌鉴权）
│  └─ web/                 # 原生前端（index.html / style.css / app.js）
├─ seed/seed_kb.json       # 随程序分发的种子知识库
├─ assets/icon.ico         # 应用图标（由 tools/make_icon.py 生成）
├─ tools/                  # 开发与构建工具（不进入 exe）
├─ docs/                   # 架构速查、开发日志、数据源调研与判例档案
│  └─ screenshots/         # README 用界面截图（由 tools/make_readme_shots.py 实跑生成）
├─ eval/                   # 评测集与体检报告（见 eval/README.md）
├─ NTE-RAG.spec            # PyInstaller 配置（单文件版）
├─ NTE-RAG-onedir.spec     # PyInstaller 配置（便携目录版）
├─ LICENSE                 # MIT（代码）；数据出处见 THIRD_PARTY_NOTICES.md
├─ THIRD_PARTY_NOTICES.md  # 逐站点数据署名与免责
├─ requirements.txt        # 运行时依赖
└─ requirements-dev.txt    # 打包期依赖（PyInstaller、Pillow）
```

---
## 开发环境适配说明

`tools/` 下有三个**为受限环境（沙箱、企业策略、受限账户）准备**的运行器，普通机器上不需要它们：
`pip_runner.py`（`tempfile.mkdtemp()` 以 `0o700` 创建的目录会拒绝后续写入，导致 pip 解包 wheel 报
`[Errno 13]`）、`fetch_pure_sdist.py`（旧式 `setup.py` 包需要 pip 起子进程，受限环境禁止管道通信）、
`pyinstaller_runner.py`（PyInstaller 的 `isolated` 机制同样依赖子进程管道，该运行器改为就地执行）。

`tools/build_exe.ps1` 与 `tools/verify_exe.ps1` 必须以 **UTF-8 BOM** 保存：Windows PowerShell 5.1
在无 BOM 时会按 ANSI 解码，中文会导致语法解析失败。编辑器改写这两个文件后要确认 BOM 仍在。

## 入口文件说明

打包入口用的是项目根目录的 `run.py`，不是 `app/main.py`。
PyInstaller 会把入口脚本当作 `__main__` 直接执行，此时没有包上下文，
`app/main.py` 里的相对导入会抛 `ImportError: attempted relative import with no known parent package`。
`run.py` 先建立包上下文再调用 `app.main.main()`，`.venv\Scripts\python.exe run.py` 与打包运行都能正常工作。
