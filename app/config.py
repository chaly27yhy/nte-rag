"""配置管理：读写 data/config.json，密钥字段一律以 DPAPI 密文落盘。

约定
- 字段名以 `_enc` 结尾的，存的是 secrets.protect() 的密文，绝不存明文。
- 对外只暴露 get_secret()/set_secret()/masked_summary()，界面回显永远脱敏。
- 配置结构做「默认值 + 深度合并」，升级版本新增字段不会让旧配置失效。
- 开发期 .env 只在「未打包」且**显式启用**时读取（见 apply_dev_env），永远不参与打包。
- 读配置容忍 UTF-8 BOM（`utf-8-sig`）：外部编辑器与 PowerShell 的
  `Set-Content -Encoding UTF8` 都会写 BOM，而 `json.loads` 见到 BOM 会直接失败——
  那曾让「整份配置被当成损坏文件改名、用户全部设置与 5 个密钥静默回默认」。
- 解析失败时优先用 `config.json.bak`（上一次落盘前的完好副本）恢复，其次才回落默认值。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import env as env_mod
from .core import paths, secrets

_LOGGER = logging.getLogger(env_mod.LOGGER_NAME)

# 保留给未来的配置迁移：当前**只写不读**，全仓没有任何代码按它分支。
# 真要引入迁移时，在这里加版本判定，并保持旧配置文件能被 load() 读进来。
CONFIG_VERSION = 1

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    # ---- 对话模型 ----
    "llm": {
        "preset": "deepseek",          # 服务商预设 id，见 app/core/providers.py
        "provider": "openai",          # openai | anthropic | gemini（由预设决定协议）
        "base_url": "",                # 留空则用预设的官方地址（不会再回落到 OpenAI）
        "model": "",
        "key_enc": "",
        "temperature": 0.3,
        "max_tokens": 3000,
        "timeout": 120,
        "extra_headers": {},
    },
    # ---- 向量模型（可选，仅用于提升检索召回）----
    # 未接线：这一段与下面 kb.use_embeddings 是同一个已实现但**检索路径未接入**的可选能力。
    # 服务端有 llm.EmbeddingClient 与 POST /api/config/test-embedding 可用（能测通 Key），
    # 片段向量列与 store.semantic_chunks 也都在，但 rag.retrieve 从不调用向量召回。
    # 详见 app/core/store.py 的 semantic_chunks 文档字符串。
    "embedding": {
        "enabled": False,
        "provider": "openai",
        "base_url": "",
        "model": "",
        "key_enc": "",
        "batch_size": 16,
    },
    # ---- 联网搜索 ----
    "search": {
        "provider": "bocha",           # bocha | tavily | serper | duckduckgo | bing | auto
        "bocha_key_enc": "",
        "tavily_key_enc": "",
        "serper_key_enc": "",
        # 未实现：SearXNG 自建实例地址。app/core/search.py 会把它读进
        # SearchClient，但当前没有任何请求路径会用到它（保留是为将来接自建实例）。
        "searxng_url": "",
        "free_fallback": True,         # 主源不可用时自动降级到免 Key 源
        "max_results": 8,
        "timeout": 20,
    },
    # ---- 抓取节奏 ----
    # 实测 BWIKI 连续抓 190 页后会开始返回 HTTP 567（WAF 拦截）。
    # 与其被拦下后还硬撞几十次，不如放慢节奏 + 命中拦截就整个域冷却一轮。
    "fetch": {
        "cooldown_seconds": 600,     # 被 403/429/567 拦下后，该域名冷却多久（秒）
        "wiki_min_interval": 5,      # wiki 类站点的抓取间隔（秒），普通站点仍为 2 秒
    },
    # ---- 知识库 ----
    "kb": {
        "chunk_size": 700,
        "chunk_overlap": 100,
        "top_k": 8,
        # 低于该相关性的候选不进证据（见 app/core/rag.py 的 retrieve）。
        # 默认 0.05 极松，几乎不过滤；调高会让「本地没资料」更快触发联网补齐。
        "min_relevance": 0.05,
        # 未接线：见上面 embedding 段的说明——置 True 目前不会改变检索行为。
        "use_embeddings": False,
        "seed_loaded": False,
    },
    # ---- 自动联网更新 ----
    "auto_update": {
        "enabled": True,
        "on_startup": True,
        "interval_hours": 24,
        "max_pages_per_run": 25,
        "max_topics_per_run": 6,
        "topics": [],
        # 来源黑名单：命中的域名不会被搜索、抓取或入库（用户可自行增删）
        "blocked_domains": [],
        # 这里曾有 custom_sources（用户自填抓取地址）。抓取链路从未消费它，
        # 属于「能添加、能看到、永远不生效」的半成品，已于 2026-09-23 移除。
        # 老 config.json 里可能还留着这个键，_deep_merge 会原样保留，不影响运行。
    },
    # ---- 数据质量 ----
    "quality": {
        "filter_boilerplate": True,      # 剔除「扫码关注」这类模板噪声
        "reject_foreign_games": True,    # 拒绝疑似跨作品污染（如攻略里混入其它游戏角色）
        "min_page_chars": 150,           # 清理后正文短于该值不入库
        "min_info_density": 0.35,        # 信息密度下限
        "official_priority": True,       # 官方来源与社区来源冲突时，官方自动优先
    },
    # ---- 回答策略 ----
    "answer": {
        "auto_web": True,              # 本地证据不足时自动联网
        "web_trigger_score": 0.35,     # 低于该相关性则触发联网
        "max_web_pages": 4,
        "min_web_pages": 1,
        # 关掉后：提示词不再要求标注来源编号，返回的引用清单也为空
        # （见 app/core/rag.py 的 system_prompt / build_messages）。
        "cite_sources": True,
    },
    # ---- 界面 ----
    "ui": {
        "window": True,                # True=pywebview 原生窗口
        "port": 0,                     # 0=随机空闲端口
        "open_browser_fallback": True,
        "theme": "dark",               # dark | light | system（跟随系统）
        # 壁纸：默认关闭，且**只使用用户自备的本地图片**。
        # 分发包内不含任何官方美术素材（官方《派生作品指引》禁止直接复制使用官方素材），
        # 所以这里存的是用户自己导入的文件名。
        "wallpaper_enabled": False,
        "wallpaper_file": "",          # 数据目录下的文件名，例如 wallpaper.jpg
        "wallpaper_dim": 0.6,          # 壁纸遮罩强度 0~0.9（越大越暗，保证正文可读）
        # 背景适配：contain=整张可见（比例不匹配处用同图模糊铺底补满）
        #           cover  =铺满窗口，超出部分被裁掉（竖图/超宽图会只剩中间一条）
        "wallpaper_fit": "contain",
        "wizard_done": False,          # 首启向导是否已关闭（关掉后不再自动弹出）
    },
    "privacy": {
        "log_llm_requests": False,     # 关：不记录请求正文，避免证据内容外泄到日志
        "redact_logs": True,
    },
}

# 开箱默认抓取主题（用户可在界面增删）
DEFAULT_TOPICS: List[str] = [
    "异环 角色 图鉴 技能",
    "异环 主线 剧情 章节",
    "异环 地图 区域 探索",
    "异环 战斗 玩法 系统",
    "异环 活动 公告 更新",
]


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 合并进 base 的副本；dict 递归合并，其它类型直接覆盖。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_dotenv(path: Path) -> Dict[str, str]:
    """极简 .env 解析（仅开发态使用，不引入第三方依赖）。"""
    data: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return data


class Config:
    """线程安全的配置对象。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else paths.config_path()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        # 读取失败时留下的人话说明；随 masked_summary() 一起给界面，
        # 免得「配置变成默认值」这件事只发生在日志里、用户毫不知情。
        self.load_warning = ""
        self.load()

    # ---------- 读写 ----------

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, Any]]:
        """读一份配置 JSON；任何失败都返回 None（绝不抛异常）。

        - `encoding="utf-8-sig"`：带不带 BOM 都能读。BOM 是真实发生过的数据丢失
          来源（外部工具用 `Set-Content -Encoding UTF8` 写一次 config.json，就会
          让 `json.loads` 抛异常，进而把用户全部设置与密钥重置成默认值）。
        - 顶层不是对象（例如被写成了 list）也算失败：后面的取值全是按 dict 假设的。
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def load(self) -> None:
        with self._lock:
            raw: Dict[str, Any] = {}
            self.load_warning = ""
            if self._path.exists():
                loaded = self._read_json(self._path)
                if loaded is not None:
                    raw = loaded
                else:
                    # 解析失败：先试着用上一次落盘前的完好副本恢复。
                    backup = self._path.with_suffix(".json.bak")
                    recovered = self._read_json(backup) if backup.exists() else None
                    if recovered is not None:
                        raw = recovered
                        self.load_warning = "配置文件损坏，已用备份 config.json.bak 恢复，请检查设置是否完整。"
                        _LOGGER.warning("[config] %s 无法解析，已用 %s 恢复", self._path.name, backup.name)
                    else:
                        # 没有可用备份时保留现场（改名而不是删除，方便事后查看），
                        # 再回落到默认配置，避免直接崩溃。
                        self.load_warning = "配置文件损坏且无可用备份，已重置为默认设置，请重新填写服务商与密钥。"
                        _LOGGER.error("[config] %s 无法解析且没有备份，已改名保留现场并重置为默认配置", self._path.name)
                        try:
                            self._path.replace(self._path.with_suffix(".json.broken"))
                        except Exception:
                            pass
            merged = _deep_merge(DEFAULT_CONFIG, raw)
            if not merged.get("auto_update", {}).get("topics"):
                merged["auto_update"]["topics"] = list(DEFAULT_TOPICS)
            self._data = merged

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            tmp = self._path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                # 必须 fsync：os.replace 只在「换文件」这一步原子，并不保证内容
                # 真的落了盘。不 fsync 时断电/崩溃可能留下零长度或半截的 tmp，
                # 一次 replace 就把坏内容扶正成正式配置。
                os.fsync(handle.fileno())
            backup = self._path.with_suffix(".json.bak")
            if self._path.exists():
                # 留一份上一次落盘前的完好副本：解析失败时拿它恢复，比把用户
                # 全部设置与 5 个密钥静默重置成默认值友好得多。副本同样是密文，
                # 所以权限也要收紧。
                try:
                    shutil.copyfile(self._path, backup)
                    os.chmod(backup, 0o600)
                except Exception:
                    pass
            os.replace(tmp, self._path)
            try:
                os.chmod(self._path, 0o600)
            except Exception:
                pass

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def detached_copy(self, name: str = "config.probe.json") -> "Config":
        """复制一份**互不影响、永不落盘到用户配置**的副本，专供「测试连接」探针。

        探针必须临时改几个字段（provider/model/密钥）才能测出真实结果，但绝不能
        改到活配置：改前是直接在活配置上改，于是 base_url 被剥掉、其余字段却真的
        写进了内存——探针拿新协议打旧地址（结果误导），而且内存里的 provider 已经
        变了、磁盘没动，用户不点「保存」就一直错位到下次重启。
        副本的路径指向一个不存在的文件，且调用方一律 persist=False，
        所以它既读不到也写不到用户的 config.json。
        """
        clone = Config(path=self._path.with_name(name))
        with self._lock:
            clone._data = copy.deepcopy(self._data)
            clone.load_warning = self.load_warning
        return clone

    def section(self, name: str) -> Dict[str, Any]:
        with self._lock:
            value = self._data.setdefault(name, {})
            if not isinstance(value, dict):
                value = {}
                self._data[name] = value
            return value

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    def set(self, section: str, key: str, value: Any, autosave: bool = True) -> None:
        with self._lock:
            self.section(section)[key] = value
            if autosave:
                self.save()

    def update_section(self, section: str, values: Dict[str, Any], autosave: bool = True) -> None:
        with self._lock:
            target = self.section(section)
            for key, value in (values or {}).items():
                if key.endswith("_enc"):
                    continue  # 密文字段只能走 set_secret
                target[key] = value
            if autosave:
                self.save()

    # ---------- 密钥 ----------

    def get_secret(self, enc_field: str, section: str = "llm") -> str:
        cipher = self.section(section).get(enc_field, "")
        if not cipher:
            return ""
        return secrets.unprotect(cipher)

    def set_secret(self, enc_field: str, value: str, section: str = "llm", autosave: bool = True) -> None:
        """写入密钥；value 为空串表示清除；传本程序生成的掩码串视为「保持原值不变」。

        掩码判定统一走 secrets.is_mask_value()（判据与 masked_summary() 的形态对应，
        且与「测试连接 / 拉取模型」探针判断「这次有没有带新密钥」用的是同一套规则）。
        autosave=False 让调用方能真正推迟落盘（test-llm 这类 persist=False 的接口
        过去其实每次都写了盘）。
        """
        if secrets.is_mask_value(value, self.get_secret(enc_field, section)):
            return
        with self._lock:
            self.section(section)[enc_field] = secrets.protect(value or "")
            if autosave:
                self.save()

    def has_secret(self, enc_field: str, section: str = "llm") -> bool:
        return bool(self.section(section).get(enc_field))

    # ---------- 开发期覆盖 ----------

    @staticmethod
    def _dev_value(dotenv: Dict[str, str], name: str) -> str:
        """开发期配置的取值顺序：进程环境 → .env。"""
        return (os.environ.get(name) or "").strip() or (dotenv.get(name) or "").strip()

    def apply_dev_env(self, explicit: bool = False) -> bool:
        """仅未打包时生效：把 .env 里的开发配置灌进来，便于本机联调。

        变量名统一为 ``NTE_RAG_DEV_*``，没有旧名兼容层（项目从未对外发布）。
        密钥写入走 ``autosave=False``，最后只落盘一次。

        **两道闸门**（过去只有 `is_frozen()` 一道）：源码检出和便携分发都会在
        每次启动时用 .env 覆盖用户自己填的模型与密钥，而且**当场持久化**——
        用户的 Key 就这样被开发机的 .env 顶掉了，界面上还看不出发生过什么。

        1. 必须显式启用：环境变量 ``NTE_RAG_DEV_ENV=1``，或调用方自己是明确的
           开发工具（tools/ 下的脚本显式传 ``explicit=True``——显式调用本身
           就是一次意图声明）；
        2. 密钥字段只在**当前为空**时写入，绝不覆盖用户已经填好的 Key。

        返回是否真的应用了任何一项（便于测试与日志）。
        """
        if paths.is_frozen():
            return False
        if not explicit and not env_mod.get_bool(env_mod.DEV_ENV, False):
            return False
        dotenv: Dict[str, str] = {}
        env_path = paths.project_root() / ".env"
        if env_path.exists():
            dotenv = _read_dotenv(env_path)

        llm = self.section("llm")
        changed = False
        provider = self._dev_value(dotenv, env_mod.DEV_LLM_PROVIDER)
        if provider:
            llm["provider"] = provider
            changed = True
        base_url = self._dev_value(dotenv, env_mod.DEV_LLM_BASE_URL)
        if base_url:
            llm["base_url"] = base_url
            changed = True
        model = self._dev_value(dotenv, env_mod.DEV_LLM_MODEL)
        if model:
            llm["model"] = model
            changed = True
        api_key = self._dev_value(dotenv, env_mod.DEV_LLM_API_KEY)
        if api_key and not self.has_secret("key_enc"):
            self.set_secret("key_enc", api_key, autosave=False)
            changed = True
        search_provider = self._dev_value(dotenv, env_mod.DEV_SEARCH_PROVIDER)
        if search_provider:
            self.section("search")["provider"] = search_provider
            changed = True
        search_key = self._dev_value(dotenv, env_mod.DEV_SEARCH_API_KEY)
        if search_key and not self.has_secret("bocha_key_enc", "search"):
            self.set_secret("bocha_key_enc", search_key, section="search", autosave=False)
            changed = True
        if changed:
            self.save()
        return changed

    # ---------- 界面安全出口 ----------

    def masked_summary(self) -> Dict[str, Any]:
        """给前端的安全视图：绝不包含任何明文密钥。"""
        with self._lock:
            data = copy.deepcopy(self._data)

        def _blank(section: str, field: str) -> Dict[str, Any]:
            enc = data.get(section, {}).pop(field, "")
            return {"set": bool(enc), "preview": secrets.mask(secrets.unprotect(enc)) if enc else ""}

        data["llm"]["key"] = _blank("llm", "key_enc")
        data["embedding"]["key"] = _blank("embedding", "key_enc")
        data["search"]["bocha_key"] = _blank("search", "bocha_key_enc")
        data["search"]["tavily_key"] = _blank("search", "tavily_key_enc")
        data["search"]["serper_key"] = _blank("search", "serper_key_enc")
        # 配置读取失败时的说明（为空串表示一切正常）：界面据此提示用户，
        # 否则「设置被重置成默认值」只会静静躺在 app.log 里。
        data["_notice"] = self.load_warning
        data["_paths"] = paths.describe_layout()
        return data
