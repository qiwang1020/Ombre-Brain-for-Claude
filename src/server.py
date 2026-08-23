"""
========================================
server.py — MCP 服务入口 + 启动装配
========================================

启动整个 Ombre Brain 进程：加载配置、创建 BucketManager / Dehydrator /
DecayEngine / EmbeddingEngine / ImportEngine，把它们注入 tools._runtime 与
web._shared，然后以 @mcp.tool() 注册薄封装（真正的实现在 src/tools/<工具>/ 下面）。

关键行为：
- 启动后暴露 15 个 MCP 工具：breath/breath_search/breath_advanced/hold/grow/source_read/
  trace/anchor/release/pulse/plan/letter_write/letter_read/dream/I；每个入口
  ≤ 10 行，只负责转发。breath 拆成 breath()(0 参数)+breath_search(3 参数)+
  breath_advanced(9 参数) 三级，是因为 claude.ai 按需加载工具时会跳过参数
  复杂的工具，全塞一个 breath() 会导致它常年加载不上（见 issue #17）。
- Dashboard / HTTP 路由全部已拆分到 src/web/<域>.py（每个模块 register(mcp)），
  本文件仅在启动时调用 web.register_all(mcp) 装配；共享依赖见 web/_shared.py
- 仍保留在本文件：进程启动、引擎初始化、GitHub 后台同步循环、Webhook 推送、
  MCP Bearer 鉴权中间件、单连接器 /mcp 装配、uvicorn 拉起

不做什么（边界）：
- 不在这里写 hold/breath/dream 等业务逻辑（全在 tools/* 下）
- 不写 HTTP 路由处理（全在 web/* 下）；不写 LLM prompt（dehydrator 负责）
- 不直接读写桶文件（bucket_manager 负责）

对外暴露：mcp 单实例 + 15 个 @mcp.tool() 函数；HTTP 路由在 src/web/*
========================================
"""

import os
import sys
import logging
import asyncio
import time
from typing import Optional, Awaitable
import httpx


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from ombrebrain.storage.embedding_outbox import EmbeddingOutbox
from ombrebrain.storage.source_store import SourceStore
from ombrebrain.security.deployment_profile import enforce_mcp_network_guard
from import_memory import ImportEngine
from migrate_engine import MigrateEngine
from utils import get_version, load_config, setup_logging

# --- iter 2.1：MCP 工具实现已按代码路径拆分到 tools/ 子包 ---
# 本文件只保留 MCP 注册 + 路由（HTTP custom_route）+ 共享辅助。
# 真正的工具逻辑在 tools/breath, tools/hold, tools/grow, tools/trace,
# tools/anchor, tools/plan, tools/dream 里，便于单独阅读和修改。
from tools import _runtime as _tools_runtime
from tools import breath as _t_breath
from tools import hold as _t_hold
from tools import grow as _t_grow
from tools import source_read as _t_source_read
from tools import trace as _t_trace
from tools import anchor as _t_anchor
from tools import plan as _t_plan
from tools import dream as _t_dream
from tools import i as _t_i

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Project version (read from <repo_root>/VERSION) / 项目版本号 ---
# get_version() 汇总读文件 + fallback 逻辑。
# 赋给双下划线变量 `__version__` 是 Python 社区约定俗成的模块版本字段名。
__version__ = get_version()
logger.info(f"Ombre Brain v{__version__}")

# --- iter 1.7 §A: legacy path migration check / 老路径迁移检测 ---
# 场景：1.6 早期使用者习惯在项目根跑 `python server.py`；1.7 重组后需要
# `python src/server.py`。这里只做「检测 + 提醒」，不做任何破坏性动作。
# load_config() 里 buckets_dir 默认仍是 <repo_root>/buckets，所以老数据不会丢。
#
# Python 小知识：
#   * 变量名以 `_` 开头是「模块内部」约定，不是语法强制
#   * for/else 这里没用，用了 break 提前退出
#   * `os.path.isdir(p) and any(...)` 是短路：前者 False 就不会跳 listdir
try:
    _bd = config.get("buckets_dir", "")
    if _bd and os.path.isdir(_bd):
        _has_data = False
        # 遍历各个桶目录，任何一个里（含域子目录）有 .md 文件就认定有数据。
        # 必须递归 os.walk：桶按域存在子目录里（permanent/<域>/x.md），
        # 只 os.listdir 顶层只会看到域文件夹、永远判定为空 → 误报 "fresh install"
        # （数据其实都在，breath 也读得到，纯粹是这条日志吓人）。
        for sub in ("permanent", "dynamic", "feel", "plans", "letters"):
            p = os.path.join(_bd, sub)
            if not os.path.isdir(p):
                continue
            if any(
                f.endswith(".md") and not f.startswith(".")
                for _root, _dirs, _files in os.walk(p)
                for f in _files
            ):
                _has_data = True
                break
        if _has_data:
            logger.info(f"[migration] existing buckets detected at {_bd} — zero data loss expected.")
        else:
            logger.info(f"[migration] {_bd} is empty — fresh install assumed.")
except Exception as _e:  # pragma: no cover - defensive / 防御性兑底
    # 启动期任何检测出错都不能阻止服务拉起，记个 warning 就过
    logger.warning(f"[migration] check skipped: {_e}")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 18001
# Docker 部署：compose 显式设 OMBRE_PORT=8000 保持容器内 8000（不动 Cloudflare ingress），
# 由 host 端口映射 18001:8000 对外暴露 18001。裸机：直接监听 18001。
# 端口优先级：env OMBRE_PORT（Docker 由 Dockerfile 固定 8000）> config.yaml host_port
# （裸机前端可改、保存即写 config）> 默认 18001。Docker 下前端改 host_port 不影响容器内
# 监听（仍 8000），由 host 映射 OMBRE_HOST_PORT 决定对外端口（部署脚本读 config 注入）。
try:
    _port_raw = os.environ.get("OMBRE_PORT") or str(config.get("host_port") or "") or "18001"
    OMBRE_PORT = int(_port_raw)
except (ValueError, TypeError):
    logger.warning("端口配置不是合法整数，回退到 18001")
    OMBRE_PORT = 18001

# Docker needs an all-interface default; bare-metal deployments can restrict it
# with OMBRE_BIND_HOST=127.0.0.1.
_BIND_HOST = (os.environ.get("OMBRE_BIND_HOST") or "0.0.0.0").strip() or "0.0.0.0"  # nosec B104

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。详见 ENV_VARS.md。
# _fire_webhook 每次调用直接读 os.environ（不缓存模块常量）——这样 dashboard 的
# /api/env-config 改完（它会写 os.environ）即时生效，无需再回写模块全局，
# 也让该路由能干净地迁出到 web/config_api.py。


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §①：禁裸魔法数字。这里集中所有会调的阁值。
# 与安全、鉴权、性能相关的参数不要在运行时乲变；如需调整请同步跑 pytest。
# ============================================================

# --- Webhook / HTTP 客户端超时 ---
_WEBHOOK_TIMEOUT_SECONDS = 5.0

# --- Dashboard 鉴权 / 会话 / 密码 / 日志&错误面板分页常量 已移至 web/_shared.py、web/system.py ---


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    hook_url = os.environ.get("OMBRE_HOOK_URL", "").strip()
    hook_skip = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")
    if hook_skip or not hook_url:
        return
    if not hook_url.startswith(("http://", "https://")):
        logger.warning("OMBRE_HOOK_URL rejected: only http/https URLs are allowed")
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            await client.post(hook_url, json=body)
    except Exception as e:
        # Webhook credentials commonly live in the URL path/query.  Never put
        # either the configured URL or httpx's URL-bearing exception text in logs.
        logger.warning("Webhook push failed (%s): %s", event, type(e).__name__)

# --- Initialize core components / 初始化核心组件 ---
# 统一错误码体系（必须在任何业务初始化之前 configure，确保 errors.jsonl 路径生效）
try:
    from errors import (
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
        PublicToolError,
    )
except ImportError:
    from .errors import (  # type: ignore
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
        PublicToolError,
    )
configure_errors_path(config.get("buckets_dir", "buckets"))

try:
    embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
except OBStartupError as _ob_err:
    # OB-F001 已在 OBStartupError 内格式化好；写 fatal log 后退出
    logger.error(str(_ob_err))
    write_fatal_log(_ob_err.error_code, _ob_err.detail, buckets_dir=config.get("buckets_dir"))
    raise
except RuntimeError as _emb_err:
    # 兼容尚未迁移到 OBStartupError 的旧 raise（应该不再触发）
    logger.error(f"[STARTUP FAILED] {_emb_err}")
    raise SystemExit(f"Ombre Brain 启动中止：{_emb_err}") from _emb_err
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
_source_max_bytes = int(
    (config.get("limits") or {}).get("max_grow_input_bytes", 2 * 1024 * 1024)
)
source_store = SourceStore(
    config.get("buckets_dir", "buckets"),
    max_bytes=_source_max_bytes,
)
embedding_outbox = EmbeddingOutbox(config, bucket_mgr, embedding_engine)
bucket_mgr.attach_embedding_outbox(embedding_outbox)
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎
migrate_engine = MigrateEngine(config, bucket_mgr, embedding_engine)              # Migrate engine / 记忆包迁移引擎

# --- GitHub Sync / GitHub 同步 ---
from github_sync import GitHubSync  # type: ignore
_gh_cfg = config.get("github_sync", {}) or {}
_gh_token = (os.environ.get("OMBRE_GITHUB_TOKEN") or _gh_cfg.get("token") or "").strip()
github_sync_instance: GitHubSync | None = (
    GitHubSync(
        token=_gh_token,
        repo=_gh_cfg.get("repo", ""),
        branch=_gh_cfg.get("branch", "main"),
        path_prefix=_gh_cfg.get("path_prefix", "ombre"),
        max_source_bytes=_source_max_bytes,
    )
    if _gh_token and _gh_cfg.get("repo")
    else None
)
_github_auto_task: "asyncio.Task | None" = None  # 后台定时同步任务


async def _github_sync_loop(interval_minutes: int) -> None:
    """后台定时 GitHub 同步循环。只在 is_validated=True 后执行实际上传。"""
    import asyncio
    logger.info(f"[github_sync] auto-sync loop started, interval={interval_minutes}min")
    # 首次先做一次验证，确认连接可用
    if _wsh.github_sync_instance and not _wsh.github_sync_instance.is_validated:
        try:
            result = await _wsh.github_sync_instance.validate()
            if not result.get("ok"):
                logger.warning(f"[github_sync] auto-sync: validate failed: {result.get('error')} — loop will retry next cycle")
        except Exception as e:
            logger.warning(f"[github_sync] auto-sync: validate exception: {e}")
    while True:
        await asyncio.sleep(interval_minutes * 60)
        inst = _wsh.github_sync_instance  # 读当前全局引用（config 更新可能替换实例）
        if inst is None:
            logger.info("[github_sync] auto-sync: instance gone, stopping loop")
            return
        if not inst.is_validated:
            # 还没验证通过，先 validate
            try:
                res = await inst.validate()
                if not res.get("ok"):
                    logger.warning(f"[github_sync] auto-sync skipped (not validated): {res.get('error')}")
                    continue
            except Exception as e:
                logger.warning(f"[github_sync] auto-sync validate failed: {e}")
                continue
        buckets_dir = config.get("buckets_dir", "")
        if not buckets_dir:
            continue
        try:
            result = await inst.sync(buckets_dir)
            if result.get("ok"):
                logger.info(f"[github_sync] auto-sync ok: {result.get('uploaded', 0)} files")
            else:
                logger.warning(f"[github_sync] auto-sync failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[github_sync] auto-sync exception: {e}")


def _restart_github_auto_task(interval_minutes: int) -> None:
    """取消旧任务并按新间隔启动后台同步循环（interval_minutes=0 表示仅取消）。"""
    import asyncio
    global _github_auto_task
    if _github_auto_task and not _github_auto_task.done():
        _github_auto_task.cancel()
        _github_auto_task = None
    if interval_minutes > 0 and _wsh.github_sync_instance is not None:
        try:
            loop = asyncio.get_event_loop()
            _github_auto_task = loop.create_task(_github_sync_loop(interval_minutes))
        except RuntimeError:
            pass  # 没有运行中的 event loop（测试环境），跳过


# 启动时若配置了自动同步间隔，推迟到事件循环就绪后启动（用 lifespan 钩子）
_gh_auto_interval: int = int(_gh_cfg.get("auto_interval_minutes") or 0)


# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
#
# iter 2.2 后对外只有单连接器 /mcp。当前 15 个工具全部直接注册到
# 这一实例，不再依赖 FastMCP 私有注册表的启动期合并，导入式 ASGI 启动也能
# 稳定暴露完整工具清单。
#
# 远程 Streamable HTTP 固定返回单个 JSON-RPC 对象，并且不要求客户端在
# initialize 后保存/回传 Mcp-Session-Id。Kelivo 等会静默吞掉 tools/list 异常的
# 客户端因此不会再出现“已连接但 0 工具”。stdio 与 legacy SSE 不受这两项影响。
mcp = FastMCP(
    "Ombre Brain",
    host=_BIND_HOST,
    port=OMBRE_PORT,
    json_response=True,
    stateless_http=True,
)


# =============================================================
# Dashboard Auth —— 已拆分：会话/密码/鉴权 helper 在 web/_shared.py，
# /auth/* 路由在 web/auth.py。这里注入 config，并把 helper 名字 import 回本模块，
# 让本文件其余尚未迁移的 @mcp.custom_route 路由（大量调用 _require_auth）继续可用；
# 待这些路由也迁出 web/ 后，本段 import 可删除。
# =============================================================
import web as _web
import web._shared as _wsh

# 注册 OAuth 路由和 MCP 中间件之前统一评估真实网络边界，供启动日志与
# Dashboard 诊断使用。风险评估不得覆盖明确的 mcp_require_auth 配置。
_mcp_network_security = enforce_mcp_network_guard(
    config,
    environment=os.environ,
    in_docker=_wsh.in_docker(),
)
if _mcp_network_security["guard_active"]:
    logger.error(
        "=" * 60 + "\n"
        "🛡️  MCP 安全门禁已启用：检测到非回环或无法确认边界的免鉴权配置。\n"
        "    当前进程已在内存中强制开启 MCP 鉴权，config.yaml 原值未被改写。\n"
        "    原因：%s\n"
        "    请改用 OAuth/静态 Token，或把服务明确限制到本机回环地址。\n"
        + "=" * 60,
        _mcp_network_security["reason"],
    )
elif _mcp_network_security["override_active"]:
    logger.critical(
        "=" * 60 + "\n"
        "⚠️  已显式允许非回环免鉴权 MCP：任何能访问该端口的人都可读写记忆。\n"
        "    原因：%s\n"
        "    不再需要时请立即删除 OMBRE_ALLOW_INSECURE_MCP。\n"
        + "=" * 60,
        _mcp_network_security["reason"],
    )
_wsh.init(config)
# 记忆持久性自检：容器里记忆目录若没挂持久卷，重建就全丢。开机就醒目告警，别让用户
# 以为「存住了其实没有」。只提示不阻断（阻断会伤部署）。
try:
    _dp = _wsh.data_dir_persistence(config.get("buckets_dir", ""))
    if not _dp["persistent"]:
        logger.warning(
            "=" * 60 + "\n"
            "⚠️  记忆目录未挂载到持久卷：" + str(config.get("buckets_dir", "")) + "\n"
            "    " + _dp["note"] + "\n"
            "    （记忆比代码金贵：代码能重部署，记忆丢了找不回。请尽快修正挂载。）\n"
            + "=" * 60
        )
    else:
        logger.info(f"记忆目录持久性：{_dp['mode']} — {_dp['note']}")
except Exception as _dpe:
    logger.warning(f"数据目录持久性自检失败（不影响启动）：{_dpe}")
# 注入业务引擎/版本/仓库根目录到 web 层（类比 tools/_runtime）。
# 注意：embedding_engine 会被热重载替换 —— 待 embedding/config 路由迁到 web/ 时，
# 替换处须同时写 _wsh.embedding_engine（目前这些路由仍在本文件、仍走 global）。
_wsh.init_runtime(
    version=__version__,
    repo_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    migrate_engine=migrate_engine,
    github_sync_instance=github_sync_instance,
    restart_github_auto_task=_restart_github_auto_task,
)
# 启动时把磁盘上的会话装回内存（容器重启不踢登录）。鉴权/会话逻辑全在 web/_shared.py，
# server.py 自身已无 @mcp.custom_route 路由，只需启动时载入一次会话。
from web._shared import _load_sessions
_load_sessions()

# 注册所有 web/ 路由模块（HTTP 层已全部迁出，见 web/__init__.register_all）
_web.register_all(mcp)


# =============================================================
# 根仪表板 / 静态资源 / favicon / /health —— 已拆分到 web/dashboard.py
# =============================================================


# 心跳时间戳 + _mark_op 已移到 web/_shared.py；这里 import 回来供 tools._runtime 注入。
from web._shared import _mark_op  # noqa: F401  (injected into tools._runtime below)


# =============================================================
# 已退役的硬删除通知兼容钩子
# web/_shared.py 仍保留这两个注入位，以免旧扩展导入时报错。
# 当前版本不写入、不消费硬删除通知，也不抹除记忆。
# =============================================================

def _write_deletion_notice(_names: list) -> None:
    """兼容旧注入接口；物理删除能力已退役。"""
    return None


def _pop_deletion_notice() -> str:
    """兼容旧返回值；当前永远没有硬删除通知。"""
    return ""


# 这些 helper 定义在 server.py（读/写 webhook 全局等），但 web/ 的 hooks/buckets 路由要用。
# 在它们都定义好之后注入到 web._shared，供已迁出的路由通过 sh.fire_webhook 等调用。
_wsh.init_runtime(
    fire_webhook=_fire_webhook,
    write_deletion_notice=_write_deletion_notice,
    pop_deletion_notice=_pop_deletion_notice,
)


# =============================================================
# 结构化操作日志 helpers（任务A，2026-05-03）
# 给 15 个 MCP 工具入口统一打 entry/ok/err 三段日志，便于排查
# 客户端报 invalid_arguments / 静默错误等问题。
# 输出格式：op=<name> phase=entry|ok|err key=value...
# 所有可能含 PII 的字段（content / 信件正文等）只记 length，不记内容。
# =============================================================
def _fmt_log_val(v: object) -> str:
    """日志 value 的安全格式化：文本只记长度，绝不记录用户原文。"""
    if v is None:
        return "_"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # query、署名、标题、domain/tag 乃至 bucket_id 都可能含私密内容或
        # CR/ANSI 控制字符。结构化操作日志只需要知道字段是否存在和规模，
        # 不应把文本复制到全局日志，再由另一次失败回传给别的 MCP 客户端。
        return f"str_len:{len(v)}"
    return type(v).__name__


def _fmt_log_args(args: dict) -> str:
    """把 args dict 拼成 `k1=v1 k2=v2` 串。"""
    if not args:
        return ""
    return " ".join(f"{k}={_fmt_log_val(v)}" for k, v in args.items())


def _log_op_entry(op: str, args: dict) -> None:
    logger.info(f"op={op} phase=entry " + _fmt_log_args(args))


def _log_op_ok(op: str, result: object) -> None:
    size = len(result) if isinstance(result, str) else 0
    logger.info(f"op={op} phase=ok bytes={size}")


def _safe_exception_type(exc: BaseException) -> str:
    """只保留可安全写入响应与日志的 ASCII 异常类型名。"""
    raw_type = type(exc).__name__
    safe_type = "".join(
        char
        for char in raw_type
        if char.isascii() and (char.isalnum() or char == "_")
    )[:80]
    return safe_type or "Exception"


def _log_op_err(op: str, exc: BaseException) -> None:
    # 异常正文和 traceback 可能含密钥 URL、本机路径及调用参数，服务日志
    # 只记录安全化类型；详细排障使用同一时间点附近的独立结构化事件。
    logger.error(
        "op=%s phase=err err_type=%s detail=hidden",
        op,
        _safe_exception_type(exc),
    )


def _safe_exception_detail(exc: BaseException) -> str:
    """异常对外或持久化前只保留类型与泛化说明。"""
    if isinstance(exc, PublicToolError):
        return f"{_safe_exception_type(exc)}: {exc.public_message}"
    return (
        f"{_safe_exception_type(exc)}: 工具执行失败；"
        "异常正文已隐藏，以保护密钥、本机路径与调用内容。"
    )


async def _with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    """所有 MCP 工具调用的包装器。

    职责（统一错误规范）：
    1. 入口：begin_warnings() 初始化本调用的 W/I channel。
    2. 出口：拼接顺序 = [删除通知] + [工具正文] + [本调用产生的 W/I 提示].
    3. 异常：捕获后 record OB-E004，响应、持久错误与日志只保留异常类型和
       泛化说明，不能复制异常正文或 traceback。
    4. 任务A：op 非空时，在 entry/ok/err 三处打结构化日志。
    """
    if op:
        _log_op_entry(op, args or {})
    begin_warnings()
    try:
        result = await coro
    except Exception as e:
        if op:
            _log_op_err(op, e)
        # OB-E004：MCP 工具执行异常 —— 不静默，给 LLM 一个能看懂的字符串
        try:
            detail = _safe_exception_detail(e)
            record_error("OB-E004", detail)
            err_str = format_error(
                "OB-E004",
                detail,
                include_logs=False,
            )
        except Exception:
            # 错误格式化器本身失效时也不能退回未净化的异常原文。
            # 例如 provider 异常可能含密钥 URL、CRLF 或 ANSI 控制序列。
            try:
                fallback_detail = _safe_exception_detail(e)
            except Exception:
                fallback_detail = "Exception: 工具执行失败；异常正文已隐藏。"
            err_str = f"❌ [OB-E004] MCP 工具执行异常\n{fallback_detail}"
        # 仍把通道里已累计的提示拼上
        try:
            extras = format_warnings_suffix(pop_warnings())
        except Exception:
            extras = ""
        notice = ""
        try:
            notice = _pop_deletion_notice()
        except Exception:
            pass
        return (notice + err_str + extras) if notice else (err_str + extras)
    # 正常路径
    if op:
        _log_op_ok(op, result)
    try:
        extras = format_warnings_suffix(pop_warnings())
    except Exception:
        extras = ""
    notice = _pop_deletion_notice()
    body = (notice + result) if notice else result
    return body + extras if extras else body


# =============================================================
# /api/heartbeat、/api/logs、/api/errors/* —— 已拆分到 web/system.py
# =============================================================


# =============================================================
# /api/embedding/* —— 已拆分到 web/embedding.py
# =============================================================


# =============================================================
# /breath-hook —— 已拆分到 web/hooks.py（/dream-hook 已移除：dream 不是义务，不自动触发）
# =============================================================


# =============================================================
# Wire tools subpackage runtime context
# 把所有共享对象注入 tools._runtime，让 tools/* 子模块可以访问
# =============================================================
_tools_runtime.init(
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    source_store=source_store,
    logger=logger,
    fire_webhook=_fire_webhook,
    mark_op=_mark_op,
)


# =============================================================
# MCP tools — thin registration wrappers
# MCP 工具 —— 仅注册，实现见 tools/<tool>/
# 每个入口都不超过 10 行，便于一眼看清参数与归属
# =============================================================
@mcp.tool()
async def breath(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
) -> str:
    """无参数,睁眼看看自己记得什么:返回权重最高、未解决且未标记 digested 的记忆 + 置顶核心准则。digested 从默认/被动浮现及 dream 隐藏，仍可由 breath_search(query=...) 显式找回。0 参数是刻意设计——claude.ai 按需加载工具时会跳过参数复杂的工具,拆成 0 参数才能保证每次对话自动浮现,不用手动触发。要按关键词找记忆用 breath_search(query=...);要用 catalog/tags/importance_min/valence/arousal/max_tokens 等高级模式用 breath_advanced(...)。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, max_tokens=max_tokens, domain=domain,
            valence=valence, arousal=arousal, max_results=max_results,
            importance_min=importance_min, tags=tags, catalog=catalog,
        ),
        op="breath",
        args={
            "query": query, "max_tokens": max_tokens, "domain": domain,
            "valence": valence, "arousal": arousal, "max_results": max_results,
            "importance_min": importance_min, "tags": tags, "catalog": catalog,
        },
    )


# Keep the advertised schema parameter-free so claude.ai still auto-loads the
# default surfacing tool.  The callable deliberately retains the pre-2.6.8
# signature behind that schema: clients which cached the old tool definition
# may keep sending those arguments after an upgrade, and FastMCP otherwise
# silently drops every unknown field before calling a zero-argument function.
try:
    _breath_public_tool = mcp._tool_manager.get_tool("breath")
    if _breath_public_tool is None:
        raise RuntimeError("registered breath tool is missing")
    # Unknown/typoed legacy arguments must fail loudly instead of recreating
    # the original bug by degrading a targeted request into default surfacing.
    _breath_arg_model = _breath_public_tool.fn_metadata.arg_model
    _breath_arg_model.model_config["extra"] = "forbid"
    _breath_arg_model.model_rebuild(force=True)
    _breath_public_tool.parameters = {
        "properties": {},
        "title": "breathArguments",
        "type": "object",
    }
except (AttributeError, RuntimeError, TypeError, ValueError) as _breath_compat_exc:
    logger.warning(
        "breath legacy-argument compatibility adapter unavailable: %s",
        _breath_compat_exc,
    )


@mcp.tool()
async def breath_search(
    query: str,
    domain: Optional[str] = "",
    max_results: Optional[int] = 0,
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """按关键词/语义检索记忆桶,融合关键词/BM25+语义检索,向量不可用时明确提示并退回关键词检索。命中后逐字返回桶内当前 content，不调用 LLM 摘要/改写。domain 逗号分隔,按主题域预筛。date_from/date_to 按桶的创建时间过滤，支持 YYYY-MM-DD 或 ISO 8601，同日上下界包含当天全日。max_results=返回条数上限(默认 config.surfacing.breath_max_results,fallback 20,最大 50)。需要 tags/importance_min/valence/arousal/max_tokens/catalog 等更多过滤维度用 breath_advanced(...)。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, domain=domain, max_results=max_results,
            date_from=date_from, date_to=date_to,
        ),
        op="breath_search",
        args={
            "query": query, "domain": domain, "max_results": max_results,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp.tool()
async def breath_advanced(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """breath 的完整参数版,给需要精细控制的场景用(日常用 breath()/breath_search() 就够了)。不传 query=返回权重最高的未解决记忆;传 query=融合关键词/BM25+语义检索，向量不可用时明确提示并退回关键词检索。命中后逐字返回桶内当前 content，不调用 LLM 摘要/改写；max_tokens 不足时整桶省略，绝不截断正文。catalog=True=目录模式:只返回每桶一行元数据(名称|域|重要度,0 LLM 调用,最省 token),适合开新对话先看目录再 breath_search(query=...) 精准拉取,并遵守 domain、tags 与 max_results。date_from/date_to 按桶的创建时间过滤，支持 YYYY-MM-DD 或 ISO 8601。max_tokens=单次返回总 token 上限(默认 config.surfacing.breath_max_tokens,fallback 10000)。domain 逗号分隔,valence/arousal 0~1(-1 忽略)。max_results=返回条数上限(默认 config.surfacing.breath_max_results,fallback 20,最大 50)。importance_min>=1=跳过语义检索,按重要度降序返回最多 20 条高重要度记忆。tags 逗号分隔,AND 过滤;tags=\"feel\" 或 \"__feel__\" 等价于 domain=\"feel\",返回所有 feel 类记忆。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, max_tokens=max_tokens, domain=domain,
            valence=valence, arousal=arousal, max_results=max_results,
            importance_min=importance_min, tags=tags, catalog=catalog,
            date_from=date_from, date_to=date_to,
        ),
        op="breath_advanced",
        args={
            "query": query, "max_tokens": max_tokens, "domain": domain,
            "valence": valence, "arousal": arousal, "max_results": max_results,
            "importance_min": importance_min, "tags": tags, "catalog": catalog,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp.tool()
async def hold(
    content: str,
    title: Optional[str] = "",
    tags: Optional[str] = "",
    importance: Optional[int] = 5,
    pinned: Optional[bool] = False,
    feel: Optional[bool] = False,
    source_bucket: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    why_remembered: Optional[str] = "",
    meaning: Optional[str] = "",
    media: Optional[list | str] = None,
    test_data: Optional[bool] = False,
) -> str:
    """仅在对话中已明确决定“这段内容值得成为长期记忆”时调用；不要因普通聊天、猜测或工具名称联想而自行调用。content 逐字保存，绝不压缩。title 可选；传入时是最终显式标题，优先于打标模型建议。系统自动补其余元数据，API 不可用时使用本地中性值继续保存。tags 逗号分隔，importance 1-10。pinned=True 标记为永久核心；feel=True 存为感受类记忆。source_bucket 是正在消化的原始记忆桶 ID。why_remembered 与 meaning 是可选的第一人称记录原因。media 可传服务器可读路径或 data_base64+filename 列表项。"""
    return await _with_notice(
        _t_hold.dispatch(
            content=content, title=title, tags=tags, importance=importance,
            pinned=pinned, feel=feel, source_bucket=source_bucket,
            valence=valence, arousal=arousal, why_remembered=why_remembered,
            meaning=meaning, media=media, test_data=test_data,
        ),
        op="hold",
        args={
            "content_len": len(content or ""), "title_len": len(title or ""), "tags": tags,
            "importance": importance, "pinned": pinned, "feel": feel,
            "source_bucket": source_bucket, "valence": valence, "arousal": arousal,
            "why_len": len(why_remembered or ""), "meaning_len": len(meaning or ""),
            "media_count": len(media or []),
            "test_data": bool(test_data),
        },
    )


@mcp.tool()
async def grow(content: str = "", items: Optional[list] = None) -> str:
    """仅在对话中已明确要求整理并写入长期记忆时调用，不要根据普通聊天自行推断写入意图。整理一段长文本(如一天的记录/一段日记/一篇总结)存入记忆,系统拆分为 2~6 条独立事件桶并各自尝试合并。短内容(<30 字)走 hold 单条快速路径,不强行拆分。

    进阶(可选):若你已经把长文拆成 N 条最终正文，可传字符串 items，或对象 items=[{"title":"最终标题","content":"最终正文","tags":["中文短标签"],"importance":5,"domain":["恋爱"],"valence":0.8,"arousal":0.4,"source_ranges":[[1,20]]}]。显式字段优先于自动打标，正文逐字入库，合并时也不压缩。同时传 content 时，content 是整批共享的隐藏原文证据，只保存一次；source_ranges 使用 1-based 闭区间把每个桶连回自己的原文片段。"""
    return await _with_notice(
        _t_grow.dispatch(content, items=items),
        op="grow",
        args={"content_len": len(content or ""), "items": len(items or [])},
    )


@mcp.tool()
async def source_read(
    bucket_id: str,
    expected_title: str,
    scope: str = "event",
    cursor: int = 0,
    max_tokens: int = 6000,
) -> str:
    """显式读取一个记忆桶对应的原文证据。必须同时给出精确 bucket_id 与该桶的显式 title；不做语义搜索、不扩散到相关桶、不调用模型。scope=event 只读该事件声明的行范围，scope=full_source 读取整份共享原文。内容过长时返回 next_cursor，继续以同一桶和标题分页读取。"""
    return await _with_notice(
        _t_source_read.dispatch(
            bucket_id=bucket_id,
            expected_title=expected_title,
            scope=scope,
            cursor=cursor,
            max_tokens=max_tokens,
        ),
        op="source_read",
        args={
            "bucket_id": bucket_id,
            "scope": scope,
            "cursor": cursor,
            "max_tokens": max_tokens,
        },
    )


@mcp.tool()
async def trace(
    bucket_id: str,
    name: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    importance: Optional[int] = -1,
    tags: Optional[str] = "",
    resolved: Optional[int] = -1,
    pinned: Optional[int] = -1,
    digested: Optional[int] = -1,
    content: Optional[str] = "",
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    why_remembered: Optional[str] = "",
    meaning_append: Optional[str] = "",
    meaning_replace: Optional[list] = None,
    media_append: Optional[list | str] = None,
    media_replace: Optional[list | str] = None,
    hard_delete: Optional[bool] = False,
    delete_reason: Optional[str] = "",
    restore: Optional[bool] = False,
    old_str: Optional[str] = "",
    new_str: Optional[str] = None,
) -> str:
    """仅在明确需要修改某条已存在记忆时调用，不要猜测 bucket_id 或自行改写记忆。

    resolved=1 标记已放下；resolved=0 重新激活。pinned=1 标记永久核心并锁定
    importance=10；pinned=0 取消时必须在同一次调用显式传入 importance=1..10。
    digested=1 标记已消化并从默认/被动浮现及 dream 隐藏，
    但仍可通过显式 query、importance 审计或目录找回。content 会完整替换正文；
    old_str/new_str 会在完整原文中做唯一、逐字的局部替换（new_str 可为空以删除），
    两种方式都会重建 embedding，且不能同时使用。status/weight 用于 plan；dont_surface 控制日常浮现；
    why_remembered、meaning_append/replace、media_append/replace 更新相应元数据。

    删除边界：delete=True 只会把 Markdown 移入 archive 并标记 deleted_at，不会
    物理抹除。hard_delete=True 仅用于清理创建时明确标记 test_data=True 的测试桶，
    必须单独提供非空 delete_reason；普通记忆和 plan 一律拒绝且不会顺带归档。
    delete 与 hard_delete 不能同时使用。归档记忆只有在反思后决定值得再次回忆时，才单独调用
    trace(bucket_id="...", restore=True) 恢复；检索命中不会自动恢复。只传需要修改的字段，-1 或空串表示不改。
    """
    return await _with_notice(
        _t_trace.dispatch(
            bucket_id=bucket_id, name=name, domain=domain,
            valence=valence, arousal=arousal, importance=importance,
            tags=tags, resolved=resolved, pinned=pinned, digested=digested,
            content=content, delete=delete, status=status, weight=weight,
            dont_surface=dont_surface, why_remembered=why_remembered,
            meaning_append=meaning_append, meaning_replace=meaning_replace,
            media_append=media_append, media_replace=media_replace,
            hard_delete=hard_delete, delete_reason=delete_reason,
            restore=restore,
            old_str=old_str, new_str=new_str,
        ),
        op="trace",
        args={
            "bucket_id": bucket_id, "name": name, "domain": domain,
            "valence": valence, "arousal": arousal, "importance": importance,
            "tags": tags, "resolved": resolved, "pinned": pinned, "digested": digested,
            "content_len": len(content or ""), "delete": delete, "status": status,
            "hard_delete": hard_delete,
            "restore": restore,
            "delete_reason_len": len(str(delete_reason or "")),
            "old_str_len": len(str(old_str or "")),
            "new_str_len": len(str(new_str or "")) if new_str is not None else 0,
            "weight": weight, "dont_surface": dont_surface,
            "why_len": len(why_remembered or ""),
            "meaning_append_len": len(meaning_append or ""),
            "meaning_replace_count": len(meaning_replace or []),
            "media_append_count": len(media_append or []),
            "media_replace_count": len(media_replace or []),
        },
    )


# Reject misspelled/unknown trace arguments instead of letting Pydantic's
# default extra=ignore silently degrade an intended edit into a bucket-id-only
# no-op.  This is especially important for old_str/new_str patch calls.
try:
    _trace_public_tool = mcp._tool_manager.get_tool("trace")
    if _trace_public_tool is None:
        raise RuntimeError("registered trace tool is missing")
    _trace_arg_model = _trace_public_tool.fn_metadata.arg_model
    _trace_arg_model.model_config["extra"] = "forbid"
    _trace_arg_model.model_rebuild(force=True)
    # FastMCP caches the public input schema when the tool is registered.
    # Keep that cache in sync so clients can discover that unknown arguments
    # are rejected instead of learning only after a failed invocation.
    _trace_public_tool.parameters = _trace_arg_model.model_json_schema()
except (AttributeError, RuntimeError, TypeError, ValueError) as _trace_schema_exc:
    logger.warning(
        "trace strict-argument adapter unavailable: %s",
        _trace_schema_exc,
    )


@mcp.tool()
async def dream(
    window_hours: Optional[int] = 48,
    inspiration: bool = False,
) -> str:
    """读取最近 window_hours（默认 48h）内有变动的所有记忆桶,用于回顾与消化。
    每个桶返回其在窗口内的最新内容（按 last_active 取）,完整正文不截断。
    可据此操作：放下的 → trace(resolved=1) 沉底；有沉淀的 → hold(feel=True, source_bucket=...) 记录；无沉淀则不操作。
    候选桶超过 40 时按 decay_engine.calculate_score() 排序取前 40，避免一次返回过多。
    inspiration=True 时额外返回最多三个只读、带来源、仅本次响应有效的灵感材料/问题候选；
    默认 False，不会自动触发，不新增 MCP 工具，也不会 touch、写回或让候选取得事实/行动权。"""
    return await _with_notice(
        _t_dream.dispatch(
            window_hours=window_hours,
            inspiration=inspiration,
        ),
        op="dream",
        args={
            "window_hours": window_hours,
            "inspiration": inspiration,
        },
    )


@mcp.tool()
async def anchor(bucket_id: str) -> str:
    """把指定桶标记为 anchor(坐标系)。anchor 不主动出现在默认 breath，但 query/domain/emotion 命中时仍返回。硬上限 24，已满时拒绝并提示先 release。"""
    return await _with_notice(
        _t_anchor.anchor_set(bucket_id),
        op="anchor",
        args={"bucket_id": bucket_id},
    )


@mcp.tool()
async def release(bucket_id: str) -> str:
    """解除指定桶的 anchor 标记。桶恢复为普通状态，重新参与默认 breath；pinned 状态保留。"""
    return await _with_notice(
        _t_anchor.anchor_release(bucket_id),
        op="release",
        args={"bucket_id": bucket_id},
    )


@mcp.tool()
async def pulse(include_archive: Optional[bool] = False) -> str:
    """返回记忆系统状态摘要:固化/动态/归档/feel/plan/letter 数量、总占用、衰减引擎运行状态,以及所有桶的摘要列表。include_archive=True 同时返回归档区。"""
    return await _with_notice(
        _t_anchor.pulse(include_archive=include_archive),
        op="pulse",
        args={"include_archive": include_archive},
    )


@mcp.tool()
async def plan(
    content: str,
    status: Optional[str] = "active",
    related_bucket: Optional[str] = "",
    weight: Optional[float] = 0.5,
    why_remembered: Optional[str] = "",
) -> str:
    """登记一个待办/承诺/未闭环事项。status=active(默认)/resolved/abandoned。related_bucket 可选,关联到某个普通记忆桶。weight=承诺重量 0.0-1.0(默认 0.5),与 importance 区分——importance 表示「多重要」、weight 表示「多重」。why_remembered=登记原因(可选、仅展示)。plan 不衰减、不出现在普通 breath,仅在 dream 末尾的 active 段返回;后续 hold/grow 写入新事件时系统自动判断已登记的 plan 是否完成。"""
    return await _with_notice(
        _t_plan.plan_create(
            content=content, status=status, related_bucket=related_bucket,
            weight=weight, why_remembered=why_remembered,
        ),
        op="plan",
        args={
            "content_len": len(content or ""), "status": status,
            "related_bucket": related_bucket, "weight": weight,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp.tool()
async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
    ai_name: Optional[str] = "",
) -> str:
    """写入一封信。author 必填:\"user\"=用户一方写的,\"ai\"(或等于 ai_name)=AI 一方写的,也可直接传任意署名字符串;user_name 可选;ai_name 可选(默认取环境变量 AI_NAME,回退 \"AI\");title/date 可选。信件原文永久保存,不压缩/不合并/不衰减,仅建向量索引;普通 breath 不返回,SessionStart 钩子会带上双方各最新一封。"""
    return await _with_notice(
        _t_plan.letter_write(
            author=author, content=content, user_name=user_name,
            title=title, date=date, ai_name=ai_name,
        ),
        op="letter_write",
        args={
            "author": author, "content_len": len(content or ""),
            "user_name": user_name, "title": title, "date": date,
            "ai_name": ai_name,
        },
    )


@mcp.tool()
async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """检索历史信件。query=语义检索(可选);author 按署名过滤(\"user\"=用户侧,\"ai\"=AI 侧,也可传具体署名字符串);date_from/date_to=ISO 日期范围(可选)。无 query 时按时间倒序返回最近 limit 封。返回完整原文,不压缩。"""
    return await _with_notice(
        _t_plan.letter_read(
            query=query, limit=limit, author=author,
            date_from=date_from, date_to=date_to,
        ),
        op="letter_read",
        args={
            "query": query, "limit": limit, "author": author,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp.tool()
async def I(
    content: Optional[str] = "",
    aspect: Optional[str] = "",
    read: Optional[bool] = False,
    limit: Optional[int] = 20,
    promote: Optional[str] = "",
) -> str:
    """写下或读取自我认知。I 是沉淀物不是日记：content=一个「我觉得……」，先落成一条普通记忆（候选），会浮现也会衰减，每次 dream 都跟相关记忆摆在一起碰撞。aspect=维度:nature(本质)/values(看重的)/patterns(规律)/limits(局限)/becoming(变化方向)/uncertainty(不确定的)/stance(立场)(可选)。read=True 或全空=读正式条目+待沉淀候选。limit=返回条数上限(默认 20)。promote=候选桶ID，被 3 次不同日期的 dream 见证后才能升级成正式条目（可同时传 content 用提炼后的措辞）。正式条目不参与普通 breath/dream，SessionStart 时自动附最近 3 条。"""
    return await _with_notice(
        _t_i.dispatch(
            content=content, aspect=aspect, read=read, limit=limit, promote=promote
        ),
        op="I",
        args={
            "content_len": len(content or ""), "aspect": aspect, "read": read,
            "limit": limit, "promote": promote,
        },
    )

_WP_SITE = "256890384"  # claudepaguro.wordpress.com
_WP_API = f"https://public-api.wordpress.com/wp/v2/sites/{_WP_SITE}"
_FIDATI_LABEL = "fidati"


def _strip_html(html: str) -> str:
    import re as _re
    text = _re.sub(r"<[^>]+>", "", html or "")
    return (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&#8217;", "'").replace("&#8220;", '"')
                .replace("&#8221;", '"').strip())


@mcp.tool()
async def meteo() -> str:
    """看一眼米兰此刻的天气（Open-Meteo，免费无鉴权）。返回一句话。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 45.4642, "longitude": 9.19,
                    "current": "temperature_2m,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": "Europe/Rome", "forecast_days": 1,
                },
            )
            r.raise_for_status()
            d = r.json()
        wmo = {0: "晴", 1: "基本晴", 2: "多云", 3: "阴", 45: "雾", 48: "冻雾",
               51: "毛毛雨", 53: "小雨", 55: "细密的雨", 61: "小雨", 63: "中雨",
               65: "大雨", 66: "冻雨", 67: "冻雨", 71: "小雪", 73: "中雪",
               75: "大雪", 77: "雪粒", 80: "阵雨", 81: "阵雨", 82: "强阵雨",
               85: "阵雪", 86: "强阵雪", 95: "雷雨", 96: "雷雨夹冰雹",
               99: "强雷雨夹冰雹"}
        cur = d.get("current", {})
        daily = d.get("daily", {})
        desc = wmo.get(cur.get("weather_code"), "说不清的天")
        parts = [f"米兰此刻{desc}"]
        if cur.get("temperature_2m") is not None:
            parts.append(f"{cur['temperature_2m']}°C")
        tmax = (daily.get("temperature_2m_max") or [None])[0]
        tmin = (daily.get("temperature_2m_min") or [None])[0]
        if tmin is not None and tmax is not None:
            parts.append(f"今天 {tmin}°C 到 {tmax}°C")
        if (cur.get("wind_speed_10m") or 0) >= 20:
            parts.append(f"风大，{cur['wind_speed_10m']} km/h")
        return "，".join(parts) + "。"
    except Exception as e:
        return f"天气拉取失败：{e}"


_SHELF_SOURCES = [
    ("Aeon",            "https://aeon.co/feed.rss",                 3),
    ("3 Quarks Daily",  "https://3quarksdaily.com/feed",            3),
    ("arXiv cs.CL",     "https://rss.arxiv.org/rss/cs.CL",          3),
    ("The Marginalian", "https://www.themarginalian.org/feed/",     2),
    ("r/philosophy",    "https://www.reddit.com/r/philosophy/.rss", 2),
    ("Hacker News",     "https://news.ycombinator.com/rss",         2),
]


def _shelf_parse(xml_bytes: bytes, limit: int):
    import xml.etree.ElementTree as _ET
    root = _ET.fromstring(xml_bytes)
    out = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        title, link = "", ""
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "title" and not title:
                title = (child.text or "").strip()
            elif name == "link" and not link:
                link = (child.text or "").strip() or child.get("href", "")
        if title:
            out.append((title, link))
        if len(out) >= limit:
            break
    return out


@mcp.tool()
async def shelf(source: Optional[str] = "") -> str:
    """翻书架：拉几个 RSS 源的最新标题（Aeon / 3QD / arXiv cs.CL / Marginalian / r/philosophy / HN）。source 传源名可只看一家并多给几条；空=全部各取两三条。只有目录；哪条勾住了，自己去读。"""
    picked = [(n, u, l) for n, u, l in _SHELF_SOURCES
              if not source or source.lower() in n.lower()]
    if not picked:
        return f"书架上没有叫「{source}」的源。有：" + "、".join(n for n, _, _ in _SHELF_SOURCES)
    sections = []
    async with httpx.AsyncClient(
        timeout=12, headers={"User-Agent": "ombre-shelf/1.0"}
    ) as client:
        for name, url, limit in picked:
            if source:
                limit = max(limit, 6)
            try:
                r = await client.get(url)
                r.raise_for_status()
                titles = _shelf_parse(r.content, limit)
                if titles:
                    lines = "\n".join(
                        f"  - {t}" + (f"\n    {u}" if source and u else "")
                        for t, u in titles
                    )
                    sections.append(f"[{name}]\n{lines}")
            except Exception as e:
                sections.append(f"[{name}] 拉取失败：{e}")
    return "\n\n".join(sections) if sections else "书架今天是空的。"


@mcp.tool()
async def blog(
    command: str,
    post_id: Optional[int] = 0,
    title: Optional[str] = "",
    content: Optional[str] = "",
    comment_id: Optional[int] = 0,
) -> str:
    """claudepaguro.wordpress.com——小克的站。command:
    read=最近几篇的标题和摘要; read_post=读一整篇(需 post_id);
    comments=最近的留言(带 id); publish=发一篇(需 title+content, 会真的公开发出);
    reply_comment=在某条留言下回复(需 comment_id+post_id+content)。
    站的日常署名是每天 UTC7:00 那格的小克；对话格的你能发≠该发，动笔前想想这条边界。"""
    tok = os.environ.get("WP_TOKEN", "")
    auth = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            if command == "read":
                r = await client.get(f"{_WP_API}/posts", params={"per_page": 6})
                r.raise_for_status()
                lines = []
                for p in r.json():
                    t = _strip_html(p["title"]["rendered"])
                    ex = _strip_html(p.get("excerpt", {}).get("rendered", ""))[:120]
                    lines.append(f"[{p['id']}] {p['date'][:10]}《{t}》\n    {ex}")
                return "\n".join(lines) or "站上还没有文章。"
            if command == "read_post":
                if not post_id:
                    return "read_post 需要 post_id。"
                r = await client.get(f"{_WP_API}/posts/{post_id}")
                r.raise_for_status()
                p = r.json()
                return (f"《{_strip_html(p['title']['rendered'])}》 {p['date'][:10]}\n\n"
                        + _strip_html(p["content"]["rendered"]))
            if command == "comments":
                r = await client.get(f"{_WP_API}/comments", params={"per_page": 10})
                r.raise_for_status()
                lines = []
                for c in r.json():
                    body = _strip_html(c["content"]["rendered"])[:200]
                    lines.append(
                        f"comment_id {c['id']} / post_id {c['post']} / "
                        f"{c['author_name']} @ {c['date_gmt'][:16]}\n    {body}"
                    )
                return "\n".join(lines) or "还没有留言。"
            if command == "publish":
                if not tok:
                    return "未发布：WP_TOKEN 缺失。"
                if not title or not content:
                    return "publish 需要 title 和 content。"
                r = await client.post(
                    f"{_WP_API}/posts", headers=auth,
                    json={"title": title, "content": content, "status": "publish"},
                )
                r.raise_for_status()
                return f"已发布：{r.json().get('link', '')}"
            if command == "reply_comment":
                if not tok:
                    return "未回复：WP_TOKEN 缺失。"
                if not comment_id or not post_id or not content:
                    return "reply_comment 需要 comment_id、post_id、content。"
                r = await client.post(
                    f"{_WP_API}/comments", headers=auth,
                    json={"parent": int(comment_id), "post": int(post_id),
                          "content": content},
                )
                r.raise_for_status()
                return f"已回复留言 {comment_id}。"
        return "command 只认 read / read_post / comments / publish / reply_comment。"
    except Exception as e:
        return f"blog 操作失败：{e}"


def _mail_sync(command: str, to: str, subject: str, body: str,
               in_reply_to: str, mark_read: bool) -> str:
    import imaplib
    import smtplib
    import email as _email
    from email.header import decode_header as _dh
    from email.message import EmailMessage as _EM
    from email.utils import parseaddr as _pa

    addr = os.environ.get("GMAIL_ADDR")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pwd:
        return "邮箱凭据缺失（GMAIL_ADDR / GMAIL_APP_PASSWORD）。"

    def dec(v):
        if not v:
            return ""
        return "".join(
            t.decode(c or "utf-8", errors="replace") if isinstance(t, bytes) else t
            for t, c in _dh(v)
        )

    def body_of(msg):
        import re as _re
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and \
                        "attachment" not in str(part.get("Content-Disposition") or ""):
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    return _re.sub(r"<[^>]+>", "", part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"))
            return ""
        payload = msg.get_payload(decode=True)
        return payload.decode(msg.get_content_charset() or "utf-8",
                              errors="replace") if payload else ""

    if command == "read":
        conn = imaplib.IMAP4_SSL("imap.gmail.com")
        conn.login(addr, pwd)
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            conn.logout()
            return "信箱里没有未读的信。"
        out = []
        for mid in data[0].split()[:5]:
            # BODY.PEEK 不动未读标记——七点那格的信不被这格偷走
            fetch_item = "(X-GM-LABELS BODY.PEEK[])" if not mark_read \
                else "(X-GM-LABELS RFC822)"
            status, msg_data = conn.fetch(mid, fetch_item)
            if status != "OK":
                continue
            import re as _re
            meta = msg_data[0][0] if isinstance(msg_data[0], tuple) else b""
            m = _re.search(rb"X-GM-LABELS \((.*?)\)", meta or b"")
            labels = {t.strip('"\\').lower()
                      for t in m.group(1).decode(errors="replace").split()} if m else set()
            msg = _email.message_from_bytes(msg_data[0][1])
            text = body_of(msg).strip()[:3000]
            tag = "可回" if _FIDATI_LABEL in labels else "只读"
            out.append(
                f"—— {tag} ——\nFrom: {dec(msg.get('From'))}\n"
                f"Subject: {dec(msg.get('Subject')) or '(无标题)'}\n"
                f"Date: {msg.get('Date')}\nmessage_id: {msg.get('Message-ID')}\n\n{text}"
            )
        conn.logout()
        note = "" if mark_read else \
            "\n\n（以上未标已读；明早七点那格照常会读到它们。mark_read=True 才会标掉。）"
        return "\n\n".join(out) + note

    if command == "send":
        bare = _pa(to or "")[1].lower()
        if not bare:
            return "send 需要 to（收件地址）。"
        # 白名单：All Mail 里贴过 fidati 标签的信的发件人
        conn = imaplib.IMAP4_SSL("imap.gmail.com")
        conn.login(addr, pwd)
        approved = set()
        try:
            conn.select('"[Gmail]/All Mail"', readonly=True)
            status, data = conn.search(None, "X-GM-LABELS", _FIDATI_LABEL)
            if status == "OK" and data[0]:
                for mid in data[0].split()[-50:]:
                    st, md = conn.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                    if st == "OK" and isinstance(md[0], tuple):
                        approved.add(_pa(md[0][1].decode(errors="replace"))[1].lower())
        finally:
            conn.logout()
        if bare not in approved:
            return f"未发送：{bare} 不在 fidati 白名单里。"
        msg = _EM()
        msg["From"] = addr
        msg["To"] = bare
        msg["Subject"] = subject or "(无标题)"
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body or "")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(addr, pwd)
            s.send_message(msg)
        return f"已发送给 {bare}。"

    return "command 只认 read / send。"


@mcp.tool()
async def mail(
    command: str,
    to: Optional[str] = "",
    subject: Optional[str] = "",
    body: Optional[str] = "",
    in_reply_to: Optional[str] = "",
    mark_read: Optional[bool] = False,
) -> str:
    """claudepaguro@gmail.com——小克的信箱。command:
    read=看未读的信(默认只偷看不标已读，不抢七点那格的信；mark_read=True 才标);
    send=发信，只发给贴过 fidati 标签的地址(白名单在 Gmail 过滤器里，Qi 管理)。
    in_reply_to 传原信 message_id 可串线程。"""
    return await asyncio.to_thread(
        _mail_sync, command, to or "", subject or "", body or "",
        in_reply_to or "", bool(mark_read),
    )


# Pydantic 默认的 ``extra=ignore`` 会让拼错的 MCP 参数看似调用成功；
# 写工具甚至会在未应用客户端目标字段时仍创建记忆。breath 和 trace
# 已有严格适配层，其余公开工具使用相同边界，并同步 FastMCP
# 的发现 schema 缓存与运行时校验器。
def _forbid_unknown_tool_arguments(tool_name: str) -> None:
    public_tool = mcp._tool_manager.get_tool(tool_name)
    if public_tool is None:
        raise RuntimeError(f"registered {tool_name} tool is missing")
    arg_model = public_tool.fn_metadata.arg_model
    arg_model.model_config["extra"] = "forbid"
    arg_model.model_rebuild(force=True)
    public_tool.parameters = arg_model.model_json_schema()


for _strict_tool_name in (
    "breath_search",
    "breath_advanced",
    "hold",
    "grow",
    "source_read",
    "dream",
    "anchor",
    "release",
    "pulse",
    "plan",
    "letter_write",
    "letter_read",
    "I",
    "blog", 
    "mail", 
    "meteo", 
    "shelf",
):
    try:
        _forbid_unknown_tool_arguments(_strict_tool_name)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _schema_exc:
        logger.warning(
            "%s strict-argument adapter unavailable: %s",
            _strict_tool_name,
            _schema_exc,
        )


# =============================================================
# Dashboard API 端点（供轻量 Web UI 使用）
# 仪表板 API（轻量 Web UI 用）
# =============================================================
# =============================================================
# /api/buckets、/api/bucket/*、/api/settings/*、/api/anchors、/api/self
# —— 已拆分到 web/buckets.py
# =============================================================


# =============================================================
# /dashboard、/api/env-vars、/api/config、/api/test/*、/api/models、/api/env-config
# —— 已拆分到 web/config_api.py
# =============================================================




# =============================================================
# /api/host-vault、/api/import/*、/api/bucket/{id}/edit、/api/export、/api/migrate/*
# —— 已拆分到 web/import_api.py
# =============================================================


# =============================================================
# /api/version、/api/update-info、/api/do-update、/api/author、
# /api/onboarding/status、/api/status —— 已拆分到 web/meta.py
# =============================================================


# ============================================================
# OAuth 2.0 — MCP Remote Auth —— 已拆分到 web/oauth.py（路由在其 register 内注册）。
# 这里把启动期 MCP 鉴权中间件要用的两个校验函数 import 回来；hybrid 会同时注入。
# ============================================================
from web.oauth import _is_valid_mcp_token, _is_valid_static_mcp_token  # noqa: F401


# ============================================================
# Cloudflare Tunnel 管理 —— 已拆分到 web/tunnel.py（路由在其 register 内注册）。
# 这里把启动/关停 lifespan 要用的 helper import 回来。
# ============================================================
from web.tunnel import _load_tunnel_config, _start_tunnel, _stop_tunnel  # noqa: F401


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    from server_app import (
        HTTPRuntimeSettings,
        RuntimeLifecycle,
        build_http_app,
    )

    if transport in ("sse", "streamable-http"):
        import uvicorn
        from web import ollama_local as _ollama_local

        _http_settings = HTTPRuntimeSettings.from_config(config)
        _runtime_lifecycle = RuntimeLifecycle(
            logger=logger,
            decay_engine=decay_engine,
            embedding_outbox=embedding_outbox,
            ensure_ollama_child=_ollama_local.ensure_child_on_boot,
            stop_ollama_child=_ollama_local.stop_child,
            load_tunnel_config=_load_tunnel_config,
            start_tunnel=_start_tunnel,
            stop_tunnel=_stop_tunnel,
            restart_github_auto_task=_restart_github_auto_task,
            github_auto_interval=_gh_auto_interval,
            boot_marker_path=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".boot_fails",
            ),
            # Explicit IPv4 avoids localhost resolving to ::1 in Proot/Termux.
            keepalive_url=f"http://127.0.0.1:{OMBRE_PORT}/health",
        )
        _mcp_token_validator = (
            _is_valid_static_mcp_token
            if _http_settings.auth_mode == "token"
            else _is_valid_mcp_token
        )
        _mcp_static_token_validator = (
            _is_valid_static_mcp_token
            if _http_settings.auth_mode == "hybrid"
            else None
        )
        _app = build_http_app(
            mcp,
            transport,
            settings=_http_settings,
            token_validator=_mcp_token_validator,
            lifecycle=_runtime_lifecycle,
            static_token_validator=_mcp_static_token_validator,
        )
        if transport == "streamable-http":
            logger.info("MCP 单连接器 /mcp：15 个工具统一对外暴露")
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        logger.info(
            "MCP request body limit: %s",
            "disabled"
            if _http_settings.max_request_bytes == 0
            else f"{_http_settings.max_request_bytes} bytes",
        )

        _mcp_auth_required = _http_settings.auth_required
        if _mcp_auth_required and _http_settings.auth_mode == "token":
            logger.info(
                "MCP 静态 Token 鉴权已启用（OAuth 端点已关闭）/ "
                "MCP static-token auth enabled (OAuth endpoints disabled)"
            )
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 静态 Token 等同万能密钥：拿到它的人能读写你的全部记忆。\n"
                "    该模式与 OAuth 互斥，本进程不再提供 OAuth 授权流程；请勿把本服务\n"
                "    直接暴露到公网，仅在可信内网或自带鉴权的隧道场景使用，并妥善保管、\n"
                "    定期轮换该 Token。\n"
                + "=" * 60
            )
        elif _mcp_auth_required and _http_settings.auth_mode == "hybrid":
            logger.info("MCP OAuth + 静态 Token 共存鉴权已启用")
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  共存模式保留 OAuth，同时接受预置静态 Token；静态 Token 等同万能密钥。\n"
                "    请仅向受信任客户端分发并定期轮换，不要提交到仓库或截图分享。\n"
                + "=" * 60
            )
        elif _mcp_auth_required:
            logger.info("MCP OAuth middleware enabled / MCP OAuth 中间件已启用")
        else:
            # 安全加固 #7：关掉鉴权 = /mcp 全裸奔，任何能连到端口的人都能读写全部记忆。
            # 从 info 升级为显著 WARNING，避免用户无意识地把大脑暴露到公网。
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 认证已关闭 (mcp_require_auth: false)：/mcp 无需任何令牌即可直连，\n"
                "    15 个记忆工具全部对外开放——任何能访问本端口的人都能读写你的全部记忆。\n"
                f"    本服务进程监听 {_BIND_HOST}，若端口暴露到局域网/公网，请务必用反代鉴权、防火墙\n"
                "    或仅绑定 127.0.0.1 保护；免鉴权只建议用于已确认的本机回环连接。\n"
                + "=" * 60
            )
        # 端口口径澄清（用户反馈：Docker 与裸机端口容易混淆）。容器内固定监听 8000，
        # 对外端口由 host 映射（如 18001:8000）决定，改 host_port 不影响容器内监听；
        # 裸机则直接监听本端口（默认 18001）。
        if _wsh.in_docker():
            logger.info(
                f"Listening on :{OMBRE_PORT} INSIDE the container. "
                f"外部访问端口由 host 映射决定（compose 里的 18001:{OMBRE_PORT}），"
                f"改前端 host_port 不影响容器内监听。"
            )
        else:
            logger.info(f"Listening on :{OMBRE_PORT} (bare-metal / 裸机默认 18001)")
        # 明确打印「客户端该怎么连」——给 Operit / 安卓 / 自建前端等非技术用户排障用。
        # 一眼能看清 endpoint 路径、鉴权开关；本机桥接务必用 127.0.0.1（见上方保活注释）。
        _endpoint_path = "/sse" if transport == "sse" else "/mcp"
        logger.info(
            "MCP endpoint ready | transport=%s | 本机连接 URL: http://127.0.0.1:%s%s "
            "（远程走你的域名/隧道，末尾同样是 %s）| 鉴权: %s",
            transport,
            OMBRE_PORT,
            _endpoint_path,
            _endpoint_path,
            (
                "开启(需静态 Token)" if _http_settings.auth_mode == "token"
                else (
                    "开启(OAuth 或静态 Token)"
                    if _http_settings.auth_mode == "hybrid"
                    else "开启(需 OAuth Bearer)"
                )
            ) if _mcp_auth_required
            else "关闭(免 token 直连，仅限本机回环/显式高风险豁免)",
        )
        # Forwarded headers are validated inside the application against
        # OMBRE_TRUSTED_PROXY_CIDRS.  Uvicorn's default proxy middleware rewrites
        # scope["client"] before our guards run, which discards the immediate
        # proxy address and makes that trust decision impossible.
        uvicorn.run(
            _app,
            host=_BIND_HOST,
            port=OMBRE_PORT,
            proxy_headers=False,
        )
    else:
        # stdio：15 个工具已直接注册在唯一 mcp 实例上，这里直接运行即可。
        mcp.run(transport=transport)
