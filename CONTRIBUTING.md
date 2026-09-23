# 参与贡献（CONTRIBUTING）

感谢愿意搭把手。本项目是 Windows 专用的单机工具，贡献方式以「改代码 / 补数据源 / 报缺陷」为主。
下面写清楚怎么把环境跑起来，以及什么样的改动会被接受，尽量不浪费你的时间。

提交之前请先读一遍 [`README.md`](README.md)，了解功能与已知限制；再读一遍
[`docs/architecture.md`](docs/architecture.md)，改代码前先看这一页。

---

## 1. 开发环境（Windows）

### 1.1 环境要求

| 项 | 要求 |
|---|---|
| 平台 | Windows 10/11 x64（没有 macOS / Linux 支持，见 §3.1） |
| Python | 3.12（本仓库在 3.12.10 上验证） |
| Node.js | 不需要：前端是原生 HTML/CSS/JS，没有构建链 |
| Git | 任意近期版本；仓库内文本统一 LF（由 `.gitattributes` 钉住） |

### 1.2 创建虚拟环境

```powershell
python -m venv .venv
```

### 1.3 安装依赖

```powershell
# 普通机器：直接用 venv 自己的解释器装（关键是用 venv 里的那个，别用系统的 python）
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

> 受限环境里的备用路径，比如沙箱、企业策略、受限账户，典型症状是 pip 解包 wheel 时报
> `[Errno 13] Permission denied`。这类环境里 `tempfile.mkdtemp()` 建的临时目录权限是 `0o700`，
> 后续写入会被拒绝。这时换一个带 pip 的解释器，运行仓库自带的安装器，把包装进 venv 的
> `site-packages`：
>
> ```powershell
> python tools\pip_runner.py install --target .venv\Lib\site-packages -r requirements-dev.txt
> ```
>
> `tools\pip_runner.py` 是很薄的一层包装，只做一件事：把 `tempfile.mkdtemp()` 换成默认权限实现。
> 直接用 `python -m pip install --target .venv\Lib\site-packages -r requirements-dev.txt` 效果一样，
> 前提是那个解释器自己带 pip。普通机器上两条路产物没有区别。

> 装依赖用的解释器带不带 pip 都行，但 `run.py`、`tools\*.py` 这些项目脚本一律显式
> 用虚拟环境解释器调用，写成 `.venv\Scripts\python.exe run.py`，不要依赖 PATH 里的 `python`。
> 路径写全才不会串环境，Windows 上翻日志也更容易看清用的是哪一个。

如果安装 `pywebview` 时卡在 `proxy_tools` 这个旧式 `setup.py` 包上，那是沙箱禁止管道通信导致的。
按 [`docs/development.md`](docs/development.md)「安装依赖」小节的 `tools\fetch_pure_sdist.py` 兜底步骤处理即可。

> 精确版本清单见 [`requirements.lock.txt`](requirements.lock.txt)，那是维护者机器 2026-09-23 的快照，
> 仅作参考。事实来源是 `requirements.txt` 与 `requirements-dev.txt`。

### 1.4 跑起来

```powershell
# 打开原生窗口（默认）
.venv\Scripts\python.exe run.py

# 只起本地服务、不打开界面（自动化/调试用）
.venv\Scripts\python.exe run.py --headless

# 启动自检：起服务 → 验鉴权/接口/知识库 → 打印并写盘一份 JSON 报告
.venv\Scripts\python.exe run.py --selftest

# 打印版本号
.venv\Scripts\python.exe run.py --version
```

想让它把数据写到别处，不污染你自己的数据目录，设环境变量即可：

```powershell
$env:NTE_RAG_DATA_DIR = "$PWD\.scratch_data"
```

其它进程级开关的名字与读取规则集中在 `app/core/env.py`，包括 `NTE_RAG_PORTABLE` 和
`NTE_RAG_DISABLE_AUTH`。开发期的密钥放 `.env`，从 `.env.example` 复制。它要有两道闸门才生效：
非打包状态，也就是 `apply_dev_env()` 里的 `is_frozen()` 判断；以及显式写上 `NTE_RAG_DEV_ENV=1`。
少了这一行，`.env` 会被整体忽略。它只填空着的密钥字段，不会覆盖界面里已保存的 Key。`.env` 已被
`.gitignore`、`.spec`、`tools/secret_scan.py` 三重排除。

---

## 2. 一条硬规则：自检必须全绿

改完代码，开 PR 之前跑一次：

```powershell
.venv\Scripts\python.exe tools\quality_check.py
```

成功的标志是最后两行逐字为：

```
共 698 项：通过 698，失败 0
全部通过
```

`698` 是开发机上的数字，它包含 `eval/report-*.json` 与 `data/cache/wiki_api/` 相关的比对。
这两样都是刻意不入库的本地产物，所以干净 clone 与 CI 里没有。那部分比对会打印成
`⏭ 跳过：…`，并单独汇总为「另有 N 处未参与比对」，总数因此更少。唯一必须成立的是
「失败 0」，别拿总数去卡 CI。

几点补充：

- 不需要网络，也不需要任何模型 Key。它只 import `app` 的模块和标准库，自己创建临时数据目录
  `.quality_check_*`，结束时清理掉，所以在任何机器上都能离线复跑。
- 它管的是机制，不管答案质量：清洗规则、来源黑名单、官方优先裁决、表格抽取、
  抓取失败可见性、种子库升级、可信度派生、结构化接口、鉴权与接口清单、模板默认值、重复合并、
  版本与时效、评测基线数字对账、发布件回归……改到哪块，断言通常就在哪块。
- 失败的项不要靠改断言来「修」。如果你确实改了行为，请一并改断言，并在
  `CHANGELOG.md` 里写清楚为什么这个新行为是期望的。
- 断言数会随功能变化，只增不减是常态。如果你有意增删断言，请一并更新
  `README.md`、`README.en.md`、`CONTRIBUTING.md`、`CHANGELOG.md`、`docs/architecture.md`、
  `docs/development.md` 里的数字。CI 刻意不写死总数，干净检出天然比开发机少几十条。它只要求
  退出码 0、`失败 0`、参与比对的断言不少于 500。最后那条下界是为了挡住
  「一项都没跑」却显示成功的情况。
- 只想看失败项：加 `--quiet`。

答案质量另有一套离线评测集，需要模型 Key，不参与 CI，见 [`eval/README.md`](eval/README.md)。

---

## 3. 代码约定

下面这些规则都能在仓库现状里验证，不是理想化的建议。

### 3.1 Windows 专用，别引入跨平台代码路径

运行与构建都只支持 Windows：DPAPI（`CryptProtectData`）加密、pywebview/WebView2 原生窗口、
PowerShell 脚本构建。所以：

- 不要写 `os.fork`、`signal.SIGUSR1`、`pwd`/`grp`、`fcntl`、`termios` 这类 POSIX-only 代码；
- 遇到平台不同、行为也不同的地方，缺的那条路必须失败关闭并说明原因，
  不要静默降级成「看起来能用」；
- 不要为了让某个 Linux 检查通过而引入 `platform.system()` 分支。

### 3.2 中文优先

代码注释、日志、文档、界面文案一律用简体中文。技术专有名词保留英文原文，比如 FastAPI、FTS5、bigram。
对外文案要具体、克制：写清楚限制与代价，不要用「革命性」「完美」这类形容词。

### 3.3 不要加重依赖

`requirements.txt` 里的每一条都会进 exe，直接影响分发包体积。下面这些明确禁止加入：

- `torch` / `sentence-transformers`：体积会从 ~60 MB 涨到 1.5 GB+；
- `jieba`：只有源码包，而且中文检索用 `app/core/chunk.py` 的自研 bigram 分词已经够用。

真要加新依赖，PR 描述里必须写清为什么现有的东西做不到，并给出体积影响。
可选的增强要写成软依赖，比如运行环境里恰好有 `jieba` 就用，没有照样跑。

### 3.4 文档要能落到代码上

写文档、注释、PR 说明时，提到实现就给出具体位置，形如：

```
app/core/trust.py:139         ← 可信度公式
app/core/ingest.py:1132-1214  ← load_seed 的指纹逻辑
```

不要写「在检索模块里」「相关代码里」这类没法 grep 的说法。行号会随重构移动，
所以除了行号，再给出函数名，比如 `load_seed`，这样更耐用。

### 3.5 失败路径必须可见、并且失败关闭

- 抓取、解析、入库的失败不能静默吞掉，要进更新报告、日志或返回结构里，
  比如 `ingest_sources()` 返回的 `dropped` / `errors`；
- 拿不准的策略取保守一侧，并在注释里写明为什么保守。参考 `app/core/fetch.py` 的 robots 策略：
  读不到 `robots.txt` 时不抓该站，网络错误、403、超时都算读不到；只有明确 404 才视为「未设限制」。
  旁边写着理由：漏抓优于越界；
- 评测或自检里出现的「降级」要有断言兜住，不然下一个人会以为它没发生过。

### 3.6 密钥只走一条路

任何涉及 API Key 或 Token 的读写，都必须经过 `app/core/secrets.py`：

- 存储走 DPAPI 加密（`CryptProtectData`）；
- 回显只返回前 4 位 + 后 4 位的掩码；
- 日志、异常、堆栈先过 `secrets.scrub()`。

不要在源码、`.spec`、测试夹具或文档里写死任何真实密钥或个人路径。
`tools/secret_scan.py` 会在打包前后各扫一遍，命中就中止构建。
自检脚本如果必须用看起来像真密钥的合成串验证脱敏，请用运行时拼装，比如 `"sk-" + "a"*27`；
确实要写成字面量时，在同一行行尾加注释 `# secret-scan: allow`。

### 3.7 抓取要守规矩

新增或修改数据源时，遵守目标站点的 `robots.txt` 与 `Crawl-delay`，同域请求保持最小间隔，
被限流或 WAF 拦下就按 `Retry-After` 退避。加数据源之前先跑一次可达性验证，参考
[`docs/data_sources_cn.md`](docs/data_sources_cn.md) 与 `tools/crawl_probe.py`；
然后到 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 登记来源与权利归属。

---

## 4. PR 检查清单

提交前逐条过一遍，都很短，但都真的会被看：

- [ ] `.venv\Scripts\python.exe tools\quality_check.py` 输出 `共 698 项：通过 698，失败 0`（干净检出会少几十条，见第 2 节）；
- [ ] 没有新增依赖，或者已在描述里写明理由与体积影响；
- [ ] diff 里没有密钥、Token、个人路径（`C:\Users\<你的用户名>\…`）、临时数据目录产物；
- [ ] `CHANGELOG.md` 已按「新增 / 变更 / 修复」记录用户能感知的改动；
- [ ] 改动是否影响打包（`tools\build_exe.ps1`）或自检？影响的话在描述里说明，
      并确认 `.ps1` 脚本仍是 UTF-8 带 BOM + LF（自检【22】会核对）；
- [ ] 描述里写清楚改了什么、为什么这么改、怎么验证的，别只贴一句「修复 bug」。

PR 描述模板见 [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)。

---

## 5. 报告缺陷与提需求

- 缺陷：用 [`.github/ISSUE_TEMPLATE/bug_report.md`](.github/ISSUE_TEMPLATE/bug_report.md)，
  它会把需要的信息列全。最关键的一项是日志尾巴：`%APPDATA%\NTE-RAG\logs\app.log`，
  便携模式下在 `data\logs\app.log`。
  日志里的密钥会被 `app/core/secrets.py` 自动脱敏，可以放心贴。
  但截图和正文里请不要出现 Key，那不在脱敏范围内。
- 安全问题：不要开公开 issue，见 [`SECURITY.md`](SECURITY.md) 的私下上报方式。
- 数据问题，比如某条知识写错了、来源不可信、条目该删：同样走 issue，
  但请给出具体条目、来源 URL 和你的依据。本项目的立场是冲突不覆盖、人工裁决留档，
  有依据的纠正一定会被采纳，判例写法见 [`docs/consistency_review.md`](docs/consistency_review.md)。
- 版权/内容下架：见 [`.github/ISSUE_TEMPLATE/takedown.md`](.github/ISSUE_TEMPLATE/takedown.md)
  与 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

## 6. 项目背景与维护方式

- 主要方向是「自带大模型 API」。本项目的核心是把你自己的大模型 API 接进来做《异环》问答，
  预置 17 家服务商，也可以指向本机 Ollama / LM Studio / vLLM。内置中文资料库是知识底座，
  不配模型时的「本地资料直出」只是备用路径。涉及问答流程的改动，请围绕这条主线考虑。
- 这是单人维护的项目，不是团队协作：issue 与 PR 都会看，但不承诺响应时间。
- Windows 专用、中文资料优先，改动请顺着这两条既有约束走，见 §3.1、§3.2。
- 内置资料来自公开站点，授权规则与移除流程见 [`DATA_LICENSE.md`](DATA_LICENSE.md)。
  请不要提交新的全量抓取结果或第三方素材，立绘、音频、视频一律不收。
- 开发过程用 AI 编程助手协助（DeepSeek Harness 配合 DeepSeek v4.1-Flash），
  数据标准与发布决定由作者拍板。提交前请自己跑绿自检（§2），AI 生成但没跑过的代码不要提。
- 想改行为之前先开 issue 说清动机。单人维护下，先对齐再写代码对双方都省事。

---

参与本项目即表示你同意遵守 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
