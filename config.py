# -*- coding: utf-8 -*-
"""
LLM 统一配置中心
================

把散落在各处的 LLM 配置（模型名、API Key、Base URL、超时…）集中到 `.env`，
一处修改全局生效。同时解决「没有 API Key 时 Django 启动崩溃」的问题。

用法
----
    from config import get_chat_model, get_asr_model, describe

    llm       = get_chat_model("main")       # 主对话/总结
    llm_fast  = get_chat_model("fast")       # 意图识别等快任务
    asr_model = get_asr_model()              # 语音识别模型名

    print(describe())                        # 打印当前生效配置（Key 已脱敏）

.env 配置项
-----------
    # ── 供应商与凭据 ──────────────────────────────
    LLM_PROVIDER=maas                 # dashscope | openai | maas | custom
    LLM_API_KEY=sk-xxxxxxxx
    LLM_BASE_URL=                     # 留空则用 provider 预设；custom 必须填

    # ── 模型分配（按用途，可各自不同）─────────────
    LLM_MODEL_MAIN=qwen-max           # 主对话、总结生成
    LLM_MODEL_STANDARD=qwen-plus      # 标准推理
    LLM_MODEL_FAST=qwen-turbo         # 意图识别、路由（要快）
    LLM_MODEL_ASR=qwen3-asr-flash     # 语音识别（需 dashscope 原生 SDK）

    # ── 通用参数 ─────────────────────────────────
    LLM_TEMPERATURE=0.3
    LLM_TIMEOUT=60
    LLM_MAX_RETRIES=2
    LLM_ENABLED=True                  # False 时全部降级，便于离线开发

向后兼容
--------
旧的 `DASHSCOPE_API_KEY` / `DASHSCOPE_API_BASE` / `OPENAI_API_KEY` 仍会被识别，
优先级低于 `LLM_*`。这样老配置不用改也能跑。
"""

import os
import re
import logging

from paths import load_env

logger = logging.getLogger(__name__)

# 导入即加载 .env（幂等，不覆盖已设的环境变量）。
# ⚠️ 早先只有 db.py 会加载 .env，config 是靠「调用链恰好先碰到 db」才拿到配置，
#    单独 `import config` 会读不到 API Key。现在谁 import 谁就能拿到。
load_env()

# ----------------------------------------------------------------------
# 供应商预设
# ----------------------------------------------------------------------
PROVIDER_PRESETS = {
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openai":    "https://api.openai.com/v1",
    # maas / custom 没有默认地址，必须由 LLM_BASE_URL 提供
    "maas":      "",
    "custom":    "",
}

# 各用途对应的模型配置键
PURPOSE_KEYS = {
    "main":     "LLM_MODEL_MAIN",
    "standard": "LLM_MODEL_STANDARD",
    "fast":     "LLM_MODEL_FAST",
}

# 模型默认值（仅当 .env 未配置时兜底）
DEFAULTS = {
    "LLM_MODEL_MAIN":     "qwen-max",
    "LLM_MODEL_STANDARD": "qwen-plus",
    "LLM_MODEL_FAST":     "qwen-turbo",
    "LLM_MODEL_ASR":      "qwen3-asr-flash",
    "LLM_TEMPERATURE":    "0.3",
    "LLM_TIMEOUT":        "60",
    "LLM_MAX_RETRIES":    "2",
}


# ----------------------------------------------------------------------
# 基础读取
# ----------------------------------------------------------------------
def _env(key, default=None):
    v = os.getenv(key)
    return v.strip() if v and v.strip() else default


# 常见的占位值/无效值特征。踩过的坑：
#   .env 里同时存在一个未注释的占位行 `DASHSCOPE_API_KEY=xxx`
#   和真正的 key，结果读到了 xxx，调用时报 401 Invalid API-key，
#   但错误信息完全指不到是配置问题。这里显式识别并跳过。
_PLACEHOLDER_RE = re.compile(
    r"^(x+|sk-x+|sk-please.*|sk-your.*|your[-_].*|change[-_]?me.*|"
    r"placeholder.*|todo.*|none|null|test|dummy)$", re.I)


def is_placeholder(v):
    """判断是不是占位值（不是真实可用的 Key）"""
    if not v:
        return True
    v = v.strip()
    if len(v) < 20:                 # 真实 Key 都很长，短的一律当占位
        return True
    return bool(_PLACEHOLDER_RE.match(v))


def get_api_key():
    """
    API Key。优先 LLM_API_KEY，兼容旧的 DASHSCOPE_API_KEY / OPENAI_API_KEY。

    ⚠️ 会自动跳过占位值：如果 LLM_API_KEY 是 xxx 而 DASHSCOPE_API_KEY 是真实值，
       会返回后者而不是 xxx。避免「看起来配了其实没配」。
    """
    for k in ("LLM_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
        v = _env(k)
        if v and not is_placeholder(v):
            return v
    # 全是占位值或没配 → 返回原值（让 validate() 去报错）
    return (_env("LLM_API_KEY")
            or _env("DASHSCOPE_API_KEY")
            or _env("OPENAI_API_KEY"))


def get_provider():
    """当前供应商。未显式配置时返回默认值，用 is_provider_explicit() 可区分"""
    return (_env("LLM_PROVIDER") or "dashscope").lower()


def is_provider_explicit():
    """用户是否在 .env 里显式写了 LLM_PROVIDER"""
    return bool(_env("LLM_PROVIDER"))


def get_base_url():
    """
    Base URL。优先级（**顺序很关键，写错过一次**）：

      1. LLM_BASE_URL            —— 显式配置，最高优先级
      2. DASHSCOPE_API_BASE      —— 旧配置兼容（项目原有 .env 用的是这个）
      3. LLM_PROVIDER 的预设      —— 仅当用户**显式**设置了 provider 时才用
      4. dashscope 默认地址       —— 最后兜底

    ⚠️ 为什么第 3 步要限定「显式设置」：
       如果无脑按 provider 预设走，那么当一个用户把真实地址写在旧键
       DASHSCOPE_API_BASE（如 MaaS 代理 https://maas.xxx/compatible-mode/v1）、
       而没设 LLM_PROVIDER 时，会被默认预设 dashscope 覆盖成官方地址 —— 结果连错服务器。
    """
    url = _env("LLM_BASE_URL")
    if url:
        return url.rstrip("/")

    legacy = _env("DASHSCOPE_API_BASE")
    if legacy:
        return legacy.rstrip("/")

    # 用户显式写了 LLM_PROVIDER
    explicit = _env("LLM_PROVIDER")
    if explicit:
        preset = PROVIDER_PRESETS.get(explicit.lower(), "")
        if preset:
            return preset
        # ⚠️ maas / custom 这类没有预设的供应商，**不能**静默退回 dashscope 官方地址。
        #    否则用户以为连的是自己的网关，实际连到了阿里云官方 —— 排查起来非常费劲。
        #    宁可返回空串，让 validate() 明确报「缺少 LLM_BASE_URL」。
        return ""

    return PROVIDER_PRESETS.get("dashscope", "")


def get_model(purpose="standard"):
    """按用途取模型名"""
    key = PURPOSE_KEYS.get(purpose, "LLM_MODEL_STANDARD")
    return _env(key) or DEFAULTS.get(key, "qwen-plus")


def get_asr_model():
    return _env("LLM_MODEL_ASR") or DEFAULTS["LLM_MODEL_ASR"]


def is_enabled():
    """
    总开关。设为 False 时所有 LLM 调用降级，便于：
      * 没有 API Key 的离线开发
      * 只调试图谱/数据库逻辑
      * 单元测试
    """
    return (_env("LLM_ENABLED", "True") or "True").lower() not in ("false", "0", "no", "off")


def _float(key, default):
    try:
        return float(_env(key) or default)
    except (TypeError, ValueError):
        return float(default)


def _int(key, default):
    try:
        return int(_env(key) or default)
    except (TypeError, ValueError):
        return int(default)


# ----------------------------------------------------------------------
# 配置健康检查
# ----------------------------------------------------------------------
def validate(raise_on_error=False):
    """
    检查配置是否完整。返回 (是否可用, 问题列表)

    典型问题：没有 API Key、provider=custom 但没配 base_url。
    """
    problems = []

    if not is_enabled():
        problems.append("LLM_ENABLED=False，所有 LLM 调用已停用")

    key = get_api_key()
    if not key:
        problems.append("缺少 API Key（请配置 .env 的 LLM_API_KEY）")
    elif is_placeholder(key):
        problems.append("API Key 看起来是占位值（%s...），请替换为真实 Key" % key[:14])

    if get_provider() in ("maas", "custom") and not get_base_url():
        problems.append("provider=%s 时必须配置 LLM_BASE_URL" % get_provider())

    ok = not problems or (len(problems) == 1 and not is_enabled())

    if raise_on_error and problems:
        raise RuntimeError("LLM 配置有问题:\n  - " + "\n  - ".join(problems))

    return ok, problems


# ----------------------------------------------------------------------
# 客户端工厂
# ----------------------------------------------------------------------
def get_chat_model(purpose="standard", **overrides):
    """
    构造 ChatOpenAI 客户端。

    ⚠️ 重要设计：**永远不抛异常**。
       原代码在模块加载时就 new ChatOpenAI，一旦没有 Key，Django 直接启动失败
       （表现为访问任意页面都 500）。这里改成：
         * 有 Key   → 返回真实客户端
         * 没有 Key → 返回一个「惰性报错」的占位对象，只有在真正调用时才会报错
       这样网站能正常启动，只是 AI 功能不可用。

    参数
    ----
    purpose   : main | standard | fast
    overrides : 覆盖默认参数，如 temperature=0.8, max_tokens=100
    """
    model = get_model(purpose)
    api_key = get_api_key()
    base_url = get_base_url()
    dry_run = (not is_enabled()) or (not api_key)

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.error("未安装 langchain-openai，无法创建 LLM 客户端")
        return _UnavailableModel("未安装 langchain-openai")

    if dry_run:
        reason = "LLM_ENABLED=False" if not is_enabled() else "未配置 API Key"
        logger.warning("[LLM] %s，'%s'(%s) 已降级为占位模型", reason, purpose, model)
        return _UnavailableModel(reason, model)

    kwargs = dict(
        model=model,
        api_key=api_key,
        temperature=_float("LLM_TEMPERATURE", 0.3),
        timeout=_int("LLM_TIMEOUT", 60),
        max_retries=_int("LLM_MAX_RETRIES", 2),
    )
    if base_url:
        kwargs["base_url"] = base_url
    kwargs.update(overrides)

    try:
        client = ChatOpenAI(**kwargs)
    except Exception as e:
        logger.error("[LLM] 创建客户端失败: %s", e)
        return _UnavailableModel(str(e), model)

    logger.info("[LLM] 已创建 %s 用途的客户端: model=%s base_url=%s",
                purpose, model, base_url or "(默认)")
    return client


def get_openai_client(**overrides):
    """构造原生 OpenAI 客户端（用于 Embedding、多模态等非 Chat 接口）"""
    api_key = get_api_key()
    if (not is_enabled()) or (not api_key):
        return _UnavailableModel("未配置 API Key 或 LLM_ENABLED=False")

    try:
        from openai import OpenAI
    except ImportError:
        return _UnavailableModel("未安装 openai")

    kwargs = dict(api_key=api_key, timeout=_int("LLM_TIMEOUT", 60))
    base_url = get_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    kwargs.update(overrides)

    try:
        return OpenAI(**kwargs)
    except Exception as e:
        logger.error("[LLM] 创建 OpenAI 客户端失败: %s", e)
        return _UnavailableModel(str(e))


def get_dashscope_model_name(purpose="asr"):
    """
    DashScope **原生 SDK** 用的模型名（语音识别走 MultiModalConversation，不兼容 OpenAI 协议）。
    同时把 SDK 的全局 api_key 一并设置好。
    """
    key = get_api_key()
    if key:
        try:
            import dashscope
            dashscope.api_key = key
        except ImportError:
            pass
    return get_asr_model() if purpose == "asr" else get_model(purpose)


# ----------------------------------------------------------------------
# 占位模型：构造成功，调用时报错
# ----------------------------------------------------------------------
class _UnavailableModel(object):
    """
    LLM 不可用时的占位对象。

    为什么需要它：原代码在模块 import 阶段就 new ChatOpenAI，
    没有 Key 会直接让 Django 启动失败。用占位对象可以让网站先跑起来，
    只有真正用到 AI 功能时才报错，并且报错信息清晰可定位。
    """

    def __init__(self, reason, model=""):
        self._reason = reason
        self._model = model

    # 属性访问一律返回自身，避免链式调用中途炸掉
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self

    def __call__(self, *a, **kw):
        self._fail()

    def _fail(self):
        raise RuntimeError(
            "LLM 不可用：%s\n"
            "请检查 LLM_server/.env：\n"
            "  LLM_API_KEY=sk-xxx\n"
            "  LLM_PROVIDER=dashscope|openai|maas|custom\n"
            "  LLM_ENABLED=True" % self._reason
        )

    def invoke(self, *a, **kw):
        self._fail()

    def stream(self, *a, **kw):
        self._fail()

    def batch(self, *a, **kw):
        self._fail()

    def bind_tools(self, *a, **kw):
        self._fail()

    def __repr__(self):
        return "<UnavailableModel: %s>" % self._reason


# ----------------------------------------------------------------------
# 诊断
# ----------------------------------------------------------------------
def probe_models(models=None, timeout=20):
    """
    探测当前网关**实际支持哪些模型**。

    为什么需要：模型名写错会得到 404「Model not exist」，很难排查。
    实测发现不同网关支持的模型集差别很大 —— 例如某个 MaaS 网关上
    `qwen-turbo` 不存在、`qwen-max-latest` 不存在，而
    `qwen-plus` / `qwen-long` 存在但额度用尽。

    用法：
        python -c "from config import probe_models; print(probe_models())"

    返回：[{"model": 名称, "status": 可用|不存在|额度用尽|无权限|错误, "detail": 说明}]
    """
    if models is None:
        # 先问网关要模型清单（/models 接口），再逐个测可用性。
        # ⚠️ 不要硬编码模型名 —— 各网关命名差异很大，实测踩过坑：
        #    同一个网关上有 qwen3.8-flash，却没有 qwen-turbo / qwen-max，
        #    用旧名探测会全部报「不存在」，从而误判成「没有可用模型」。
        models = None          # 见下方动态获取

    api_key = get_api_key()
    if not api_key:
        return [{"model": "-", "status": "错误", "detail": "未配置 API Key"}]

    try:
        from openai import OpenAI
    except ImportError:
        return [{"model": "-", "status": "错误", "detail": "未安装 openai"}]

    client = OpenAI(api_key=api_key, base_url=get_base_url() or None, timeout=timeout)

    # 动态获取网关的模型清单（只测对话类，跳过图像/音频/向量等）
    if models is None:
        try:
            all_ids = [m.id for m in client.models.list().data]
            skip = ("image", "audio", "video", "tts", "asr", "embed",
                    "rerank", "realtime", "live", "speech", "ocr")
            models = [m for m in all_ids
                      if not any(k in m.lower() for k in skip)]
            models = sorted(models)[:40]          # 上限，避免探测过久
        except Exception as e:
            return [{"model": "-", "status": "错误",
                     "detail": "无法获取模型清单: %s" % str(e)[:90]}]

    out = []
    for m in models:
        try:
            client.chat.completions.create(
                model=m, messages=[{"role": "user", "content": "hi"}], max_tokens=5
            )
            out.append({"model": m, "status": "可用", "detail": ""})
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if "404" in msg or "not exist" in low or "not found" in low:
                status = "不存在"
            elif "quota" in low or "exhausted" in low or "balance" in low:
                status = "额度用尽"
            elif "403" in msg:
                status = "无权限"
            elif "401" in msg:
                status = "Key无效"
            else:
                status = "错误"
            out.append({"model": m, "status": status, "detail": msg[:110].replace("\n", " ")})
    return out


def suggest_working_model(probed=None):
    """
    从探测结果里挑一个可用的模型，返回 (模型名, 说明)。
    没有可用模型时返回 (None, 原因)。
    """
    res = probed if probed is not None else probe_models()
    avail = [r["model"] for r in res if r["status"] == "可用"]
    if not avail:
        pass
    else:
        # 按「质量优先」排序，别把 1.5B 小模型当首选
        def rank(m):
            n = m.lower()
            for i, kw in enumerate(("max", "pro", "72b", "32b", "27b", "14b", "plus", "7b")):
                if kw in n:
                    return i
            return 99          # flash / turbo / mini 等轻量模型排最后
        avail.sort(key=lambda m: (rank(m), len(m)))
        return avail[0], "探测到 %d 个可用模型: %s" % (len(avail), ", ".join(avail[:6]))

    # 没有可用的，给出最可能的原因

    # 没有可用的，给出最可能的原因
    statuses = {r["status"] for r in res}
    if "额度用尽" in statuses:
        return None, ("模型存在但免费额度已用尽。"
                      "请到供应商控制台充值，或关闭「仅用免费额度」模式")
    if "无权限" in statuses:
        return None, "当前 Key 无权访问这些模型，请检查控制台里的模型授权"
    if "Key无效" in statuses:
        return None, "API Key 无效，请检查 .env 的 LLM_API_KEY"
    if statuses == {"不存在"}:
        return None, "该网关不支持这些模型名，请向供应商确认可用模型清单"
    return None, "未探测到可用模型"


def mask_key(k):
    """脱敏显示 API Key"""
    if not k:
        return "(未配置)"
    if len(k) <= 12:
        return k[:3] + "***"
    return "%s...%s (%d字符)" % (k[:7], k[-4:], len(k))


def describe():
    """返回当前 LLM 配置的可读摘要（Key 已脱敏），用于排查问题"""
    ok, problems = validate()
    return "\n".join([
        "LLM 配置",
        "  状态        : %s" % ("✅ 可用" if ok else "⚠️ 不可用"),
        "  总开关      : %s" % ("开启" if is_enabled() else "关闭（LLM_ENABLED=False）"),
        "  供应商      : %s%s" % (get_provider(), "" if is_provider_explicit() else " (默认)"),
        "  API Key     : %s" % mask_key(get_api_key()),
        "  Base URL    : %s" % (get_base_url() or "(未配置)"),
        "  模型 main   : %s" % get_model("main"),
        "  模型 standard: %s" % get_model("standard"),
        "  模型 fast   : %s" % get_model("fast"),
        "  模型 ASR    : %s" % get_asr_model(),
        "  温度/超时   : %s / %ss" % (_float("LLM_TEMPERATURE", 0.3), _int("LLM_TIMEOUT", 60)),
    ] + (["  问题        : " + p for p in problems] if problems else []))


if __name__ == "__main__":
    # 自测：python knowledge/config.py
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # .env 已在模块导入时加载（见文件顶部 load_env()）

    print(describe())
    print()
    ok, _ = validate()
    print("可以创建客户端:", ok)

    if "--probe" in sys.argv:
        print()
        print("探测网关支持的模型（约 20 秒）...")
        for r in probe_models():
            mark = "OK " if r["status"] == "可用" else "   "
            print("  %s %-24s %s %s" % (mark, r["model"], r["status"], r["detail"][:60]))
        m, why = suggest_working_model()
        print()
        print("建议模型:", m or ("无 —— " + why))
