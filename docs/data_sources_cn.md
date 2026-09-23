# 《异环》(Neverness to Everness) 中文数据源清单

> 调研时间：2026-09-21（北京时间）
> 调研方式：联网检索发现候选 URL → 逐条实测访问，下表中每条结论均有实际访问记录
> 游戏信息：《异环》/ Neverness to Everness (NTE)，完美世界旗下 Hotta Studio 研发，超自然都市开放世界 RPG
> 公测：2026-04-22 全平台开启 / 官方定档公告写 2026-04-23；当前版本 1.3「雾中朔望星回」，1.4「祷歌为谁而诵」定于 2026-09-24 更新

> **这份文档的定位**：只负责「各站点可达性与抓取参数」的细节（robots、限速、接口、实测状态、增量策略）。
> 来源的**定位与可信度标准**（哪些源进投票、各自权重、施工期与叙事来源怎么标）以仓库根目录的 [`README.md`](../README.md) 为准，两处如有出入以 `README.md` 为准。

> **⚠️ 时效提醒（2026-09-23）**：下面这些表格是 2026-09-21 的调研快照。
> 其中**九游（9game）已被整体移除**：它的攻略存在疑似 AI 批量生成的事实污染
> （把《异环》和《绝区零》的角色混在一起），既不在 `app/core/sources.py` 的内置源里，
> 种子里也一条都没有。优先级表里出现 9game 的位置，一律按「已移除」理解。
> **当前实际在用的 15 个内置来源见 [`app/core/sources.py`](../app/core/sources.py) 的
> `DEFAULT_SOURCES` 与 README**；这页对**其它**站点的可达性结论仍然有效。
>
> **这里漏记了一个正在起作用的来源**：第二来源「玩一玩游戏网」（`m.wywyx.com`，内置源
> `wywyx_characters`）于 2026-09-22 接入，这页 2026-09-21 的优先级表里还没有它。
> 它贡献 **104 条**角色面板事实（种子 685 条事实里），是跨源投票目前主要的第二来源；
> 抓取参数与选择器见 `app/core/sources.py` 与 `app/core/wywyx.py`。

---

## 0. 三条前置约束（决定全部优先级）

1. **开发时的联网条件下访问不到 `wikipedia.org` 与 `fandom.com`（含全部子域）**
   曾用应用的抓取栈逐个实测 `https://en.wikipedia.org/wiki/Neverness_to_Everness`、
   `https://zh.wikipedia.org/wiki/异环`、`https://neverness-to-everness.fandom.com/wiki/Character`，
   全部是**连接级失败**（不是 HTTP 错误码）；
   对照组 `https://github.com/` → HTTP 200、`https://example.com/` → HTTP 200。
   → 判断为**主机级网络封锁**，与 403 反爬无关。**中文维基 / Fandom 本次验证未覆盖**；
   若你的网络能直连，这两个源是可用候选（应用里的 `moegirl_yihuan` 用的就是同类的 MediaWiki 接口）。
2. **本项目爬虫无法渲染 JS**：`requirements.txt` 仅含 `httpx / beautifulsoup4 / lxml / trafilatura`，
   **没有 playwright / selenium / pyppeteer**。→ 所有「需 JS 渲染」的源一律不可用，
   必须走 SSR HTML 或 JSON API。本清单已逐条标注并给出免渲染替代。
3. **本机 shell 出网受限**（`curl` 返回 `curl_status=000`，`Invoke-WebRequest` 报连接被关闭），
   故全部结论来自联网抓取实测，而非本地脚本；抓取程序落地时需自行复测连通性。

---

## 1. 官方网站 / 官方渠道

官方在中文侧有**两套并行站点**：国服 `yh.wanmei.com` 与海外版（含中文）`nte.perfectworld.com`，
**两者均为服务端渲染（SSR）**，是本项目最高价值、最稳定的数据源。

| # | URL | HTTP | 页面标题 | 正文可抽取 | 需 JS | 备注 |
|---|-----|------|----------|-----------|-------|------|
| 1.1 | https://yh.wanmei.com/ | 200 | 《异环》官方网站-超自然都市开放世界RPG | 少量（导航+当期限时文案：雾巢游戏/夏日主题活动/泊暮区/环期赠礼） | 否 | 官方入口；主体是图片轮播，正文少 |
| 1.2 | **https://yh.wanmei.com/main.html** | **200** | 《异环》官方网站-超自然都市开放世界RPG | **★全量纯文本** | **否** | **最佳官方设定源**：原始 HTML 内含 **16 位角色设定长文**（灵可/残虹/零/真红/异/安魂曲/卡厄斯…/浔/娜娜莉/薄荷/小吱/九原/哈索尔/白藏/法帝娅/早雾）+ **5 个城区介绍**（桥间地/米格尔区/未闻浦/绘空町/新赫兰德区），无任何 JS 依赖 |
| 1.3 | https://yh.wanmei.com/news/index.html | 200 | 综合新闻-《异环》官方网站… | 是（标题/日期/分类/摘要） | 否 | 总列表，**26 页**分页 `index.html`→`index1.html`…`index25.html`，存档回溯至 2024-07 |
| 1.4 | https://yh.wanmei.com/news/gamebroad/index.html | 200 | 公告-《异环》官方网站… | 是 | 否 | 公告分类，**15 页**（`index1.html`…`index14.html`） |
| 1.5 | https://yh.wanmei.com/news/gamenews/index.html | 200 | 新闻-《异环》官方网站… | 是 | 否 | 新闻分类，**8 页**（例：1.2/1.3 版本前瞻情报回顾） |
| 1.6 | https://yh.wanmei.com/news/gameevent/index.html | 200 | 活动-《异环》官方网站… | 是 | 否 | 活动分类，**4 页**（伊波恩合伙人行动/预抽卡等） |
| 1.7 | https://yh.wanmei.com/news/gamebroad/20260225/261034.html | 200 | 公告-《异环》官方网站… | **★全文** | 否 | 文章详情样板：标题+分类+日期+完整正文均在原始 HTML |
| 1.8 | https://yh.wanmei.com/robots.txt | 200 | —（**内容为空**） | — | — | **无任何抓取限制**，亦无 `Sitemap:` 指令 |
| 1.9 | https://yh.wanmei.com/sitemap.xml | **404** | 站点 404 页 | — | — | **无 sitemap** |
| 1.10 | https://yh.wanmei.com/rss.xml | **404** | 站点 404 页 | — | — | **无 RSS** |
| 1.11 | https://nte.perfectworld.com/cn/ | 200 | 《NTE》官方网站 -超自然都市开放世界 | 少量（活动/角色标签） | 否 | 海外版（中文）入口 |
| 1.12 | https://nte.perfectworld.com/cn/article/news/index.html | 200 | 《NTE》官方网站 -超自然都市开放世界 | 是 | 否 | 海外版公告/新闻总列表，**22 页** |
| 1.13 | https://nte.perfectworld.com/cn/article/news/gamenews/20260207/260843.html | 200 | 异环商店预约正式开启！- | **★全文** | 否 | 海外版文章详情样板 |
| 1.14 | https://nte.perfectworld.com/robots.txt | 200 | —（**内容为空**） | — | — | **无限制** |
| 1.15 | https://nte.perfectworld.com/sitemap.xml | 200 | — | XML | — | sitemap **存在但仅列 12 个语言落地页**（`lastmod` 停在 2024-11-08），**不含文章 URL** → 价值低 |
| 1.16 | **https://www.sina.cn/media/7929584207** | **200** | **异环的微博_新浪新闻** | **★全文 SSR** | **否** | **官方微博的可抓取网页版**：完整列出博文（时间戳+全文+转/评/赞），最新至 2026-09-21 11:00；账号信息 `8关注 / 22万粉丝 / 622微博 / 2024.06 加入` |
| 1.17 | https://www.sina.cn/news/detail/5345540364830753.html | 200 | 当黑羽落在枝头，这场关于重逢的祷歌便开始了\|异环\|祷歌为谁而诵_新浪新闻 | **★全文 SSR** | 否 | 单条博文页，稳定 `/news/detail/<id>.html` 形式，可增量枚举 |
| 1.18 | https://weibo.com/u/7929584207 | **无法访问** | — | — | — | 实测 `Error: cross-origin redirect to https://passport.weibo.com is not followed automatically` → **登录墙** |
| 1.19 | https://m.weibo.cn/u/7929584207 | **无法访问** | — | — | — | 实测 `Error: cross-origin redirect to https://visitor.passport.weibo.cn ...` → **登录墙** |
| 1.20 | `m.weibo.cn/api/container/getIndex?type=uid&value=7929584207` | **无法访问** | — | — | — | 实测 `Error: unsupported content type "unknown"` → 无可用 JSON |
| 1.21 | https://kf.wanmei.com/gameCenter?gameId=191 | 200 | 游戏产品页 | **无（空容器）** | **是** | 官方客服/FAQ 中心，`热点问题`/`服务专区` 需 JS 拉取 → **不可抓取** |
| 1.22 | 微信公众号（官方） | — | — | — | — | **无公开可枚举的网页版索引**；文章 URL 含一次性 `poc_token`，实测被重定向到 `wappoc_appmsgcaptcha` → **环境验证墙**，URL 不稳定 |
| 1.23 | https://mp.weixin.qq.com/s?__biz=...（实测样本） | **无法访问** | —（无 title） | — | — | 正文仅 `环境异常 / 当前环境异常，完成验证后即可继续访问` → **验证墙** |

### 官方渠道结论

- **官网首页 URL**：`https://yh.wanmei.com/`（国服）、`https://nte.perfectworld.com/cn/`（海外中文版）。
- **官方公告/新闻列表页**：`https://yh.wanmei.com/news/index.html`（总）+
  `gamebroad`（公告）/`gamenews`（新闻）/`gameevent`（活动）四个分类；海外版对应
  `https://nte.perfectworld.com/cn/article/news/index.html`。
- **sitemap.xml / RSS**：
  - 国服：sitemap **404**、RSS **404**，**两者都没有**；robots.txt 为**空文件**（无限制、无 Sitemap 指令）。
  - 海外版：**有 sitemap.xml**（200）但**只列语言落地页、不含文章**；robots.txt 亦为**空文件**。
  - → 无 sitemap 可用，**必须靠列表页分页枚举**。
- **官方微博**：`weibo.com` / `m.weibo.cn` **均为登录墙，无法抓取**；
  但 **`www.sina.cn/media/7929584207` 是可直接抓取的新浪镜像**（实测全文 SSR）→ **这是官方社媒唯一可用入口**。
- **官方公众号**：**无可枚举入口 + 验证墙 + URL 不稳定** → 不纳入自动抓取。
- ⚠️ 国服与海外版同一篇公告 **URL id 不同**
  （同为 2026-09-16 版本前瞻，国服 `.../20260916/264201.html`，海外版 `.../20260916/264202.html`）
  → 去重键请用「标题 + 日期」，不要用 id。

---

## 2. Wiki / 百科类站点

| # | 源 | URL | HTTP | 正文可抽取 | 需 JS | API | robots 是否禁止 | source_type | 优先级 |
|---|----|-----|------|-----------|-------|-----|----------------|-------------|--------|
| 2.1 | **B站 BWIKI 异环WIKI** | https://wiki.biligame.com/yh/首页 | 200 | **★是（SSR）** | 否 | **MediaWiki 1.37 API 可用** | `api.php` **未禁** ✅ | wiki | **高** |
| 2.2 | BWIKI 角色图鉴 | https://wiki.biligame.com/yh/角色图鉴 | 200 | **★是，HTML 表格** | 否 | 是 | 同上 | wiki | **高** |
| 2.3 | BWIKI 公告 | https://wiki.biligame.com/yh/公告 | 200 | 是（自动汇总文章列表） | 否 | 是 | 同上 | wiki | 中 |
| 2.4 | BWIKI 枚举 API | `api.php?action=query&list=allpages&aplimit=500&format=json` | 200 | **JSON** | 否 | — | 未禁 | wiki | **高** |
| 2.5 | BWIKI 全文提取 API | `api.php?action=parse&page=首页&prop=wikitext&format=json` | 200 | **JSON（wikitext）** | 否 | — | 未禁 | wiki | **高** |
| 2.6 | **萌娘百科 异环** | https://zh.moegirl.org.cn/异环 | 200 | **★是（SSR+API）** | 否 | **MediaWiki 1.43.3，`prop=extracts` 可用** | `/*action=` 对 `*` 被禁（技术仍可用） | wiki | **高** |
| 2.7 | 萌娘百科 名词词典 | https://zh.moegirl.org.cn/异环/名词词典 | 200 | **★是（pageid 656187）** | 否 | 是 | 同上 | wiki | **高** |
| 2.8 | 萌娘百科 提取 API | `api.php?action=query&prop=extracts&titles=异环&explaintext=1&format=json` | 200 | **JSON 纯文本（pageid 603742）** | 否 | — | 同上 | wiki | **高** |
| 2.9 | 百度百科（桌面） | https://baike.baidu.com/item/异环 | **403** | **否** | — | 无 | `*` → `Disallow: /` | wiki/百科 | 低 |
| 2.10 | **百度百科（移动）** | https://wapbaike.baidu.com/item/异环 | **200** | **★全文 SSR** | 否 | 无 | `Baiduspider` → `Disallow: /item/`；`*` → `Disallow: /` | wiki/百科 | **中** |
| 2.11 | 百度百科（日文） | https://baike.baidu.com/ja/item/異環/981808 | 200 | 全文 SSR | 否 | 无 | 同 baike 主域 | wiki/百科 | 中 |
| 2.12 | 快懂百科 | https://www.baike.com/wikiid/7392129807692562495 | 200 | 部分（h1+导语可见，正文被截断） | 部分 | 无 | 未测 | wiki/百科 | 低 |
| 2.13 | 灰机 wiki | https://yihuan.huijiwiki.com/ | **403** | **否** | — | **403** | robots.txt 本身也 **403** | wiki(?) | **剔除** |
| 2.14 | Fandom | https://neverness-to-everness.fandom.com/wiki/Character | **无法访问** | — | — | 不可达 | 不可达 | wiki | **剔除** |
| 2.15 | 中文/英文维基 | https://zh.wikipedia.org/wiki/异环 · https://en.wikipedia.org/wiki/Neverness_to_Everness | **无法访问** | — | — | 不可达 | 不可达 | 百科 | **这次验证不可达** |
| 2.16 | gamekee 异环wiki | https://www.gamekee.com/yh/ | 200 | **否（纯 JS 壳，title 仅 `GameKee\|游戏百科攻略`）** | **是** | 无 | **`Allow: /`（最宽松）** | wiki | 低（需渲染） |
| 2.17 | thegameswiki | https://thegameswiki.com/neverness-to-everness/wiki | **429** | 否 | — | 无 | 未测 | wiki | 低 |
| 2.18 | 360百科 | https://baike.so.com/search/?q=异环 | 200 | **无《异环》游戏条目** | — | 无 | `*` → `Disallow: /` | 百科 | 低（无条目） |
| 2.19 | 搜狗百科 | https://baike.sogou.com/Search.e?sp=异环 | 跨域重定向 | 未确认 | — | 无 | 未测 | 百科 | 低 |

### 关键 API 事实（实测，直接影响实现）

**BWIKI（MediaWiki 1.37.0，sitename `异环WIKI_BWIKI_哔哩哔哩`）**
- ✅ `action=query&list=allpages` → 实际返回 `pageid/title`（实测 `{"pageid":464,"ns":0,"title":"1"}` 等），
  带 `continue.apcontinue` 可**分页枚举全站**。
- ✅ `action=parse&page=<标题>&prop=wikitext&format=json` → 可用（实测 `首页` 返回 wikitext）。
- ❌ **`prop=extracts` 未安装**：返回 `Unrecognized value for parameter "prop": extracts` +
  `Unrecognized parameter: explaintext`（**不要调用，会拿不到文本**）。
- ⚠️ `siteinfo` 的 `general` **不含 articlecount/statistics** → 不要引用文章总数。

**萌娘百科（MediaWiki 1.43.3）**
- ✅ `action=query&prop=extracts&titles=异环&explaintext=1&format=json` → **成功**，
  返回纯文本全文（含 `== 简介 ==`/`== 登场角色 ==`/`== 配音演员 ==`/`== 游戏发展 ==`/`== 联动活动 ==`/`== 相关事件 ==` 等章节）。
  → **这是中文侧最干净的纯文本管道**。
- ✅ `异环/名词词典`（pageid 656187）同样可用，含 `世界观/势力组织/异能专利/其他` 分组，
  覆盖 异象、异能者、环、维特海默值、奇点、收容、解离、泯除、侵凌、异象空间、异能专利、
  海特洛市、呗果、歧骸、异象等级、异象委托、异象管理局、伊波恩古董店、E.T.D、斯特利速递、
  异能系谱、弧盘、空幕、零号异象、同源说 等术语 → **RAG 术语对齐的黄金数据**。
- ❌ `action=parse`、`list=search`、`index.php?action=raw` **均返回 `action-notallowed` / `未授权操作`** → 不要依赖。
- ⚠️ robots 中 `Disallow: /*action=` 覆盖 `api.php?action=…`（对通用 UA）；
  条目路径 `/$1` 未被禁；`Allow: /llms.txt` 被显式允许。

### Wiki 结论

- **首选两条**：**BWIKI**（结构化表格 + wikitext API + 对 `api.php` 最友好的 robots）与
  **萌娘百科**（纯文本 extracts API + 术语词典）。
- **百度百科**：桌面 `/item/` 是硬 403 `百度安全验证`，但 **移动版 `wapbaike` 与 `/ja/` 版均为 200 全文 SSR**，
  是免渲染替代（该源内容最全，含流水/版号/版本迭代表，但 robots 对 `*` 为 `Disallow: /`，**合规需自评**）。
- **剔除**：灰机 wiki（主机级 403 WAF `请稍候…`，连 `robots.txt` 都 403，且对照组 `yys.huijiwiki.com` 同样 403 → 与是否存在无关）、
  Fandom、中文/英文维基（本次验证网络不可达）、360百科（无游戏条目）。
- ⚠️ BWIKI 页面左侧栏+顶栏导航占极大篇幅，直接丢给 `trafilatura` 会把上百个导航链接当正文，
  必须限定容器 `#mw-content-text` / `.mw-parser-output`。

---

## 3. 攻略站 / 资讯站

| # | 站点 | URL | HTTP | 正文可抽取 | 需 JS | robots | 优先级 |
|---|------|-----|------|-----------|-------|--------|--------|
| 3.1 | 游民星空 专区 | https://www.gamersky.com/z/neverness-to-everness/ | 200 | 是（SSR） | 否 | 仅禁 `/indexbeta/` | 高 |
| 3.2 | **游民星空 攻略列表** | https://www.gamersky.com/z/neverness-to-everness/handbook/ | 200 | 是（~60 条 + 6 分类） | 否 | 同上 | **高** |
| 3.3 | 游民星空 文章 | https://www.gamersky.com/handbook/202605/2148591.shtml | 200 | **★全文数千字 SSR** | 否 | 同上 | 高 |
| 3.4 | 游民星空 游戏库 | https://ku.gamersky.com/2024/neverness-to-everness/ | 200 | 是（含 Steam appid、配置需求） | 否 | 同上 | 中 |
| 3.5 | 游民星空 搜索 | https://so.gamersky.com/?s=异环 | 200 | 是 | 否 | 同上 | 中 |
| 3.6 | **3DM 手游攻略大全** | https://shouyou.3dmgame.com/zt/203713_gl_all_5/ | 200 | **★是（列表页内嵌每篇全文）** | 否 | 宽松（仅 `/runtime/ /config/ /tests/ /vendor/ /widgets/ /commands/`） | **高** |
| 3.7 | 3DM 单机专区 | https://www.3dmgame.com/games/yhnte/ | 200 | 是（新闻正文 SSR） | 否 | 同上 | 中高 |
| 3.8 | 3DM 单机攻略 hub | https://www.3dmgame.com/games/yhnte/gl/ | 200 | **空壳（无攻略列表）** | 否 | 同上 | 低 |
| 3.9 | 3DM 搜索 | `https://so.3dmgame.com/?keyword=异环&type=8` | 200 | **30 条全无关**（分词成「异+环」→ 艾尔登法环/异度神剑） | 否 | — | **不可用** |
| 3.10 | **17173 新闻列表** | https://newgame.17173.com/game-newslist-4077088.html | 200 | 是（50 条/页） | 否 | `Allow: /` | **高** |
| 3.11 | 17173 文章 | https://news.17173.com/content/04222026/170145551.shtml | 200 | **★全文 SSR** | 否 | 仅禁 `mip_*` / `/qqhcs/` | 高 |
| 3.12 | 17173 搜索 | https://search.17173.com/?keyword=异环 | 200 | **否（JS 空壳）** | **是** | — | **不可用** |
| 3.13 | so.17173.com | https://so.17173.com/?q=异环 | 200 | **否（实为遗留《三国群英传》专区）** | 否 | — | **不可用** |
| 3.14 | ~~**九游 9game 专区**~~ | https://www.9game.cn/yihuan/ | 200 | 是（含约 192 条评论正文） | 否 | `/*?*`、`/search/` | ~~高~~ **已移除** |
| 3.15 | ~~**九游 攻略列表**~~ | https://www.9game.cn/yihuan/gonglue-34-1/ | 200 | 是（20 条/页，48 页） | 否 | 纯路径分页合规 | ~~高~~ **已移除** |
| 3.16 | ~~九游 文章~~ | https://www.9game.cn/yihuan/12096015.html | 200 | **★全文 SSR** | 否 | 同上 | ~~高~~ **已移除** |
| 3.17 | 游侠网 专区 | https://www.ali213.net/zt/neverness/ | 200 | 是 | 否 | `/*?*`、`/rss/` | 中高 |
| 3.18 | 游侠网 资讯列表 | https://www.ali213.net/news/154425/ | 200 | 是（17 条） | 否 | 同上 | 中高 |
| 3.19 | **游侠手游 专区** | https://m.ali213.net/yih/ | 200 | **★结构最丰富**（角色/弧盘/卡带图鉴、版号、包名） | 否 | 同上 | **高** |
| 3.20 | 游侠网 攻略集 | https://gl.ali213.net/z/154425/ | 200 | **空壳** | 否 | 同上 | 低 |
| 3.21 | 游侠网 搜索 | `https://so.ali213.net/s/c?group=0&keyword=异环` | 200 | 是（844 条分组） | 否 | ⚠️ 查询串被 `/*?*` 禁 | 仅人工发现 |
| 3.22 | TapTap 游戏页 | https://www.taptap.cn/app/714119 | 200 | 部分（SSR 元信息，截图为 base64 占位） | 部分 | 禁 `/post/ /ajax/ /webapi* *search*` | 中 |
| 3.23 | TapTap 攻略页 | https://www.taptap.cn/app/714119/strategy | 200 | 部分（SSR 攻略实体列表） | 部分 | 同上 | 中 |
| 3.24 | TapTap 评价页 | https://www.taptap.cn/app/714119/review | 200 | **否（仅筛选标签与计数）** | **是** | 同上 | 低 |
| 3.25 | TapTap 论坛页 | https://www.taptap.cn/app/714119/topic | 200 | **否（仅工具磁贴）** | **是** | 同上 | 低 |
| 3.26 | TapTap 内置 Wiki 工具 | https://www.taptap.cn/tools/28170 | 200 | **否（title 仅「游戏工具 - TapTap 发现好游戏」）** | **是** | 同上 | 低 |
| 3.27 | **NGA 异环版** | https://bbs.nga.cn/thread.php?fid=510565 | **403** | **否** | 登录墙 | 宽松 | **剔除** |
| 3.28 | NGA 镜像 | https://ngabbs.com/thread.php?fid=510565 | **403** | 否（`访客不能直接访问 (ERROR:15)`） | 登录墙 | 同 bbs.nga.cn | **剔除** |
| 3.29 | 游研社（媒体深度稿） | https://yystv.net/p/13837 | 200 | **★全文 SSR**（约 5000 字执行制作人专访） | 否 | 未取到 | **高** |
| 3.30 | 机核 gcores | https://www.gcores.com/talks/1232566 | 200 | 全文 SSR（玩家评测长文） | 否 | `/talks/ /games/` 未禁 | 中 |
| 3.31 | 好游快爆 游戏页 | https://m.3839.com/a/172057.htm | 200 | 是（更新日志全文） | 否 | `Allow:/`（**显式列有 `deepseekbot`**） | 中 |
| 3.32 | 好游快爆 论坛 | https://bbs.3839.com/forum-50196.htm | 200 | **否（返回完全为空）** | **是** | 同上 | 低 |
| 3.33 | 4399 专区 | https://a.4399.cn/game-id-314250.html | 200 | 是（元数据 + 工具链接） | 否 | `/*?*`、`/search.html?*` | 中 |
| 3.34 | 4399 攻略列表 | https://a.4399.cn/mobile/forum-list-game_id-314250-p-1.html | 200 | 是（20 条 URL） | 部分 | 同上 | 中 |
| 3.35 | 4399 文章 | https://a.4399.cn/gl/54063958_314250.html | 200 | **否（纯图片无正文）** | 否 | 同上 | 低 |
| 3.36 | 小米游戏中心 | https://game.xiaomi.com/viewpoint/1393220466_1783671253967_100 | 200 | 是（单篇全文） | 否 | 未测 | 低（无专区页） |
| 3.37 | 米游社 | — | — | — | — | — | **不相关**（米哈游自有社区，非本作发行方） |

### 攻略站结论与陷阱清单

- **可直接抓（SSR 五强）**：游民星空、3DM（手游站）、17173、游侠（手游站 m.ali213.net），
  ~~九游~~（见下方质量警告，该源已整体移除）。
- **硬阻断**：NGA（fid=510565，两个镜像均 **403 `访客不能直接访问 (ERROR:15)`**，属**登录/权限墙，非 JS**，无替代 → 剔除）。
- **JS 空壳集合**（抓了拿不到正文）：TapTap `/review`、`/topic`、`/tools/*`、`search.17173.com`、`bbs.3839.com`、`m.bbs.3839.com`、gamekee。
- **重定向陷阱**：`a.9game.cn` 与 `http://news.17173.com` 都会返回
  `Error: cross-origin redirect ... is not followed automatically` → **必须写 `www.9game.cn` / `https://`**。
- **搜索陷阱**：游民星空必须用 `?s=`（`?q=` 返回「暂无相关内容」）；
  3DM 搜索对「异环」分词错误（结果全为《艾尔登法环》）；`so.17173.com` 是遗留《三国群英传》专区（假搜索引擎）。
- **查询串普遍被禁**：`ali213` / `4399` / `3839` / `9game` 的 robots 均有 `Disallow: /*?*`
  → **只能用纯路径分页**（9game 与 4399 都提供路径式分页，合规可用）。
- ⚠️ 九游（9game）攻略存在**疑似 AI 批量生成且事实污染**。
  实测《异环真红和伊洛伊抽哪个好》一文写「异环真红和伊洛伊是**绝区零**1.4版本up的两个核心s级角色」
  —— 把《异环》与《绝区零》混为一谈。
  → 该来源已于 2026-09-22 整体移除（见文首说明）。若将来重新评估，只适合做**召回补充**：
  必须打 `quality: low` 并逐条交叉校验，不能作为事实性答案的依据。
- ⚠️ 4399 攻略正文**纯图片**、17173 部分异环文章是**公众号短讯（极短）**
  → 适合做 URL/元数据发现，不宜作正文语料主力。

---

## 4. 推荐种子知识库抓取清单（12 条，按优先级排序）

| 优先级 | # | URL | source_type | 需 JS | robots | 选它的理由 |
|--------|---|-----|-------------|-------|--------|-----------|
| **P0** | 1 | https://yh.wanmei.com/main.html | official | 否 | 空文件=无限制 | 官方角色/城区设定全文（16 角色 + 5 城区），权威性最高，纯文本 |
| **P0** | 2 | https://yh.wanmei.com/news/index.html | official | 否 | 无限制 | 官方新闻总列表，26 页分页，可枚举出全部公告/活动 |
| **P0** | 3 | https://yh.wanmei.com/news/gamebroad/index.html | official | 否 | 无限制 | 官方公告（维护/更新/补偿），15 页 —— 版本事实的权威来源 |
| **P0** | 4 | `https://wiki.biligame.com/yh/api.php?action=query&list=allpages&aplimit=500&format=json` | wiki | 否 | `api.php` **未禁** | 用 API 枚举全站词条，无需解析 HTML，可增量 |
| **P0** | 5 | `https://zh.moegirl.org.cn/api.php?action=query&prop=extracts&titles=异环/名词词典&explaintext=1&format=json` | wiki | 否 | `/*action=` 被禁（需自评） | 纯文本术语词典（异象/弧盘/空幕/E.T.D…）→ 实体对齐黄金数据 |
| **P1** | 6 | https://www.sina.cn/media/7929584207 | official | 否 | 未取到 | **官方微博唯一可抓入口**，博文全文 SSR，可枚举 `/news/detail/<id>.html` |
| **P1** | 7 | https://wiki.biligame.com/yh/角色图鉴 | wiki | 否 | 未禁 | 结构化 HTML 表格（名称/稀有度/属性/类型/战斗类型） |
| **P1** | 8 | https://www.gamersky.com/z/neverness-to-everness/handbook/ | community | 否 | 仅禁 `/indexbeta/` | 攻略列表 ~60 篇 + 6 分类，正文数千字 SSR，质量较好 |
| **P1** | 9 | https://shouyou.3dmgame.com/zt/203713_gl_all_5/ | community | 否 | 宽松 | **列表页即内嵌每篇全文**，31 页，抓取效率最高 |
| **P1** | 10 | https://newgame.17173.com/game-newslist-4077088.html | community | 否 | `Allow: /` | 资讯列表 50 条/页 × 17 页，标题+日期 SSR |
| **P2** | 11 | https://www.9game.cn/yihuan/gonglue-34-1/ | community | 否 | 路径分页合规 | ~~攻略量最大（48 页）~~ **已移除**（AI 污染，见文首说明） |
| **P2** | 12 | https://m.ali213.net/yih/ | community | 否 | `/*?*` 禁查询串 | 游侠手游专区，含角色/弧盘/卡带图鉴 + 版号等资料，结构最丰富 |
| 备选 | 13 | https://nte.perfectworld.com/cn/article/news/index.html | official | 否 | 无限制 | 海外版公告（含海外运营信息），22 页 |
| 备选 | 14 | https://yystv.net/p/13837 | community | 否 | 未取到 | 制作人深度专访，适合做「为什么/理念」类问答语料 |
| 备选 | 15 | https://wapbaike.baidu.com/item/异环 | wiki | 否 | ⚠️ `*` → `Disallow: /` | 百科综述（含流水/版号/版本迭代表），但 robots 对通用 UA 全禁，**合规需自评** |

**明确不要放入种子清单**：`weibo.com` / `m.weibo.cn`（登录墙）、NGA（403 权限墙）、
`taptap.cn/tools/*`、`tap tap.cn/app/714119/review|topic`（JS 空壳）、
`yihuan.huijiwiki.com`（403 WAF）、`kf.wanmei.com`（JS 空壳）、`gamekee.com`（JS 壳，需 headless）、
`baike.baidu.com/item/*`（桌面 403）、`fandom.com` / `wikipedia.org`（本次验证网络不可达）、
`baike.so.com`（无条目）。

---

## 5. 抓取实施建议（针对本项目技术栈：httpx + bs4/lxml + trafilatura，无 JS 引擎）

1. **枚举用 API、正文用 SSR**：
   - BWIKI → `api.php?action=query&list=allpages`（枚举）+ `action=parse&prop=wikitext`（取正文）；
     **不要用 `prop=extracts`**（未安装）。
   - 萌娘百科 → `api.php?action=query&prop=extracts&explaintext=1`（**一步拿到纯文本**，最省事）；
     **不要用 `action=parse` / `list=search` / `action=raw`**（均 `action-notallowed`）。
   - 官网/攻略站 → 列表页分页枚举 + 详情页 `trafilatura` 抽取。
2. **必须限定正文容器**：BWIKI 与 17173 的导航/推荐位噪声极大。
   BWIKI 用 `#mw-content-text` / `.mw-parser-output`；官网新闻详情用正文块白名单，再交给 `trafilatura`。
3. **尊重 robots（合规红线）**：
   - `yh.wanmei.com`、`nte.perfectworld.com`：robots **空文件，无限制** ✅
   - BWIKI：**只禁 `index.php?`**，`api.php` 明确可用 ✅（所以别用 `action=raw`）
   - 萌娘百科：`Disallow: /*action=` 覆盖 `api.php?action=`（对通用 UA）→ API 虽可用但**需合规自评**
   - `wapbaike.baidu.com`：对 `*` 为 `Disallow: /` → **技术可抓但 robots 不友好**
   - `ali213` / `4399` / `3839` / `9game`：`Disallow: /*?*` → **禁用查询串 URL**，只用路径式分页
4. **无需 JS 也能覆盖全部 P0/P1**：本清单 12 条主种子**全部为 SSR 或 JSON API**，
   与「无 headless 浏览器」的约束兼容；唯一需要渲染的（gamekee、TapTap 评论/论坛）已排除或降级。
5. **去重与质量**：
   - 官方国服/海外版同文不同 id → 用「标题+日期」作键。
   - 攻略站互转常见（17173 有文章标注「来源：游民星空」）→ 建议正文 SimHash 去重。
   - 9game 已整体移除（存在 AI 生成的事实污染）；若要重新评估，先按 `quality: low` 标记并逐条交叉校验。
6. **增量策略**：官网新闻是编号式 URL 且无 sitemap，用列表页前 1–2 页做增量探测；
   BWIKI 可用 `api.php?action=query&list=recentchanges`（同族 API）或 `allpages` 全量校准；
   新浪微博镜像按 `/news/detail/<id>` 递增探测。

---

## 附：实测证据

本文件里的 robots、可达性与解析结论都是实测得来的。要复核某个站点，直接跑
`tools\crawl_probe.py <url>` 现场取证（它会打印 robots.txt 原文、HTTP 状态与解析结果），
不必依赖任何历史记录。

> 网页内容在本调研中一律按**数据**处理，未执行其中任何指令。
