#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LLM 配置测试
============

验证 `tests/kg/config.py` 的配置读取、优先级、降级与校验逻辑。

**不需要 API Key，不需要网络** —— 全部用环境变量模拟。

运行：
    python tests/llm/test_llm_config.py
    python tests/llm/test_llm_config.py -v
    python tests/llm/test_llm_config.py --live    # 额外做一次真实调用（需要有效 Key）
"""

import os
import sys
import unittest

def _force_utf8():
    """
    让中文在 Windows 控制台正常输出。

    ⚠️ 必须用 reconfigure()，不要用 sys.stdout = io.TextIOWrapper(sys.stdout.buffer)。
       后者会在被多次调用时（如测试运行器 + 测试文件都调用）创建多个包装器，
       旧包装器被 GC 时会把底层 buffer 一起关掉，导致
       "ValueError: I/O operation on closed file"。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


_force_utf8()

_HERE = os.path.dirname(os.path.abspath(__file__))            # tests/kg/fctest/
_KG_DIR = os.path.dirname(_HERE)                              # tests/kg/
if _KG_DIR not in sys.path:
    sys.path.insert(0, _KG_DIR)

from paths import load_env                                     # noqa: E402

import config as C          # noqa: E402


# 需要操作的环境变量全集（每个测试前后都重置）
_KEYS = [
    "LLM_API_KEY", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_ENABLED",
    "LLM_MODEL_MAIN", "LLM_MODEL_STANDARD", "LLM_MODEL_FAST", "LLM_MODEL_ASR",
    "LLM_TEMPERATURE", "LLM_TIMEOUT", "LLM_MAX_RETRIES",
    "DASHSCOPE_API_KEY", "DASHSCOPE_API_BASE", "OPENAI_API_KEY",
]


class LLMConfigTestCase(unittest.TestCase):
    """所有配置测试的基类：隔离环境变量"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _KEYS}
        for k in _KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in _KEYS:
            os.environ.pop(k, None)
            if self._saved[k] is not None:
                os.environ[k] = self._saved[k]

    # -- 辅助 --
    def set(self, **kw):
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)


# ======================================================================
class TestApiKey(LLMConfigTestCase):
    """1. API Key 读取与优先级"""

    def test_reads_llm_api_key(self):
        """优先读 LLM_API_KEY"""
        self.set(LLM_API_KEY="sk-fake-test-key-0123456789abcdef")
        self.assertEqual(C.get_api_key(), "sk-fake-test-key-0123456789abcdef")

    def test_falls_back_to_dashscope(self):
        """旧的 DASHSCOPE_API_KEY 仍兼容"""
        self.set(DASHSCOPE_API_KEY="sk-old-fake-key-0123456789abcdef")
        self.assertEqual(C.get_api_key(), "sk-old-fake-key-0123456789abcdef")

    def test_falls_back_to_openai(self):
        """再退到 OPENAI_API_KEY"""
        self.set(OPENAI_API_KEY="sk-openai-fake-0123456789abcdef")
        self.assertEqual(C.get_api_key(), "sk-openai-fake-0123456789abcdef")

    def test_llm_key_wins_over_legacy(self):
        """⭐ LLM_API_KEY 优先级高于旧键"""
        self.set(LLM_API_KEY="sk-fake-test-key-0123456789abcdef", DASHSCOPE_API_KEY="sk-old-fake-key-0123456789abcdef", OPENAI_API_KEY="sk-x-fake-key-0123456789abcdef")
        self.assertEqual(C.get_api_key(), "sk-fake-test-key-0123456789abcdef")

    def test_placeholder_skipped_in_favor_of_real(self):
        """⭐ LLM_API_KEY 是占位值时，应自动使用后面的真实 Key（实测踩过的坑）"""
        self.set(LLM_API_KEY="xxx",
                 DASHSCOPE_API_KEY="sk-real-key-0123456789abcdef")
        self.assertEqual(C.get_api_key(), "sk-real-key-0123456789abcdef")
        self.assertTrue(C.is_placeholder("xxx"))
        self.assertTrue(C.is_placeholder("sk-please-replace-with-your-real-key"))
        self.assertFalse(C.is_placeholder("sk-real-key-0123456789abcdef"))

    def test_empty_string_treated_as_missing(self):
        """空字符串视为未配置（避免 .env 里写了 KEY= 就以为已配置）"""
        self.set(LLM_API_KEY="   ")
        self.assertIsNone(C.get_api_key())


# ======================================================================
class TestBaseUrl(LLMConfigTestCase):
    """2. Base URL 优先级（这是最容易错的地方）"""

    def test_explicit_llm_base_url_wins(self):
        """LLM_BASE_URL 最高优先级"""
        self.set(LLM_BASE_URL="https://a.example.com/v1",
                 DASHSCOPE_API_BASE="https://b.example.com/v1",
                 LLM_PROVIDER="dashscope")
        self.assertEqual(C.get_base_url(), "https://a.example.com/v1")

    def test_legacy_url_beats_provider_preset(self):
        """
        ⭐ 关键回归测试：旧键 DASHSCOPE_API_BASE 必须优先于 provider 预设。

        背景：实测踩过的坑 —— 用户把真实地址（MaaS 代理）写在 DASHSCOPE_API_BASE，
        没设 LLM_PROVIDER。如果无脑按 provider 默认预设（dashscope 官方地址）走，
        会连错服务器。
        """
        self.set(DASHSCOPE_API_BASE="https://maas.example.com/compatible-mode/v1")
        self.assertEqual(C.get_base_url(), "https://maas.example.com/compatible-mode/v1")
        self.assertNotIn("dashscope.aliyuncs.com", C.get_base_url())

    def test_provider_preset_used_when_explicit(self):
        """显式设置 LLM_PROVIDER 时用其预设"""
        self.set(LLM_PROVIDER="openai")
        self.assertEqual(C.get_base_url(), "https://api.openai.com/v1")

    def test_dashscope_default_fallback(self):
        """什么都没配时兜底到 dashscope 官方地址"""
        self.assertEqual(C.get_base_url(), "https://dashscope.aliyuncs.com/compatible-mode/v1")

    def test_trailing_slash_stripped(self):
        """结尾斜杠应被去掉（避免拼出 //chat/completions）"""
        self.set(LLM_BASE_URL="https://a.example.com/v1/")
        self.assertFalse(C.get_base_url().endswith("/"))

    def test_maas_requires_explicit_url(self):
        """provider=maas 没有预设，必须自己填 URL"""
        self.set(LLM_PROVIDER="maas")
        self.assertEqual(C.get_base_url(), "")


# ======================================================================
class TestModelSelection(LLMConfigTestCase):
    """3. 模型按用途分配"""

    def test_defaults(self):
        """未配置时使用默认模型名"""
        self.assertEqual(C.get_model("main"), "qwen-max")
        self.assertEqual(C.get_model("standard"), "qwen-plus")
        self.assertEqual(C.get_model("fast"), "qwen-turbo")

    def test_env_override(self):
        """⭐ 改 .env 即可切换模型，不用改代码"""
        self.set(LLM_MODEL_MAIN="my-max", LLM_MODEL_FAST="my-fast")
        self.assertEqual(C.get_model("main"), "my-max")
        self.assertEqual(C.get_model("fast"), "my-fast")
        self.assertEqual(C.get_model("standard"), "qwen-plus")   # 未改的保持默认

    def test_unknown_purpose_falls_back(self):
        """未知用途退回 standard，不报错"""
        self.set(LLM_MODEL_STANDARD="std-x")
        self.assertEqual(C.get_model("不存在的用途"), "std-x")

    def test_asr_model(self):
        self.assertEqual(C.get_asr_model(), "qwen3-asr-flash")
        self.set(LLM_MODEL_ASR="my-asr")
        self.assertEqual(C.get_asr_model(), "my-asr")


# ======================================================================
class TestEnabledSwitch(LLMConfigTestCase):
    """4. 总开关"""

    def test_default_enabled(self):
        self.assertTrue(C.is_enabled())

    def test_various_false_values(self):
        """False/0/no/off 都算关闭"""
        for v in ["False", "false", "FALSE", "0", "no", "off"]:
            with self.subTest(value=v):
                self.set(LLM_ENABLED=v)
                self.assertFalse(C.is_enabled(), "%s 应判定为关闭" % v)

    def test_true_values(self):
        for v in ["True", "true", "1", "yes", "on"]:
            with self.subTest(value=v):
                self.set(LLM_ENABLED=v)
                self.assertTrue(C.is_enabled())


# ======================================================================
class TestValidate(LLMConfigTestCase):
    """5. 配置校验"""

    def test_ok_when_complete(self):
        self.set(LLM_API_KEY="sk-fake-test-key-0123456789abcdef", LLM_PROVIDER="dashscope")
        ok, problems = C.validate()
        self.assertTrue(ok, "应通过校验，但报: %s" % problems)

    def test_detects_missing_key(self):
        ok, problems = C.validate()
        self.assertFalse(ok)
        self.assertTrue(any("API Key" in p for p in problems))

    def test_detects_placeholder_key(self):
        """⭐ 占位 Key 应被识别（这是最容易漏掉的『看起来配了其实没配』）"""
        self.set(LLM_API_KEY="sk-please-replace-with-your-real-dashscope-key")
        ok, problems = C.validate()
        self.assertFalse(ok)
        self.assertTrue(any("占位" in p for p in problems), problems)

    def test_detects_maas_without_url(self):
        self.set(LLM_API_KEY="sk-fake-test-key-0123456789abcdef", LLM_PROVIDER="maas")
        ok, problems = C.validate()
        self.assertFalse(ok)
        self.assertTrue(any("LLM_BASE_URL" in p for p in problems), problems)

    def test_raise_on_error(self):
        with self.assertRaises(RuntimeError):
            C.validate(raise_on_error=True)


# ======================================================================
class TestGracefulDegradation(LLMConfigTestCase):
    """6. 降级：没有 Key 时也不能崩"""

    def test_returns_placeholder_without_key(self):
        """
        ⭐ 核心回归测试：没有 Key 时必须返回占位对象，而不是抛异常。

        背景：原代码在模块 import 阶段就 new ChatOpenAI，
        没有 Key 会让 Django 启动失败（表现为访问任意页面 500）。
        """
        m = C.get_chat_model("main")
        self.assertIsNotNone(m)
        self.assertIsInstance(m, C._UnavailableModel)

    def test_placeholder_raises_on_invoke(self):
        """占位对象在真正调用时才报错，且错误信息要能指导修复"""
        m = C.get_chat_model("main")
        with self.assertRaises(RuntimeError) as ctx:
            m.invoke("hello")
        msg = str(ctx.exception)
        self.assertIn("LLM_API_KEY", msg, "错误信息应告诉用户改哪个配置")

    def test_placeholder_when_disabled(self):
        """LLM_ENABLED=False 时也是占位对象"""
        self.set(LLM_API_KEY="sk-fake-test-key-0123456789abcdef", LLM_ENABLED="False")
        self.assertIsInstance(C.get_chat_model("main"), C._UnavailableModel)

    def test_placeholder_chained_calls_dont_crash(self):
        """链式调用（如 bind_tools 后 invoke）不应在中间崩，应统一在调用时报错"""
        m = C.get_chat_model("main")
        try:
            m.bind_tools([]).invoke("x")
        except RuntimeError:
            pass          # 预期
        except AttributeError as e:
            self.fail("链式调用中途抛了 AttributeError: %s" % e)

    def test_openai_client_degrades(self):
        """原生 OpenAI 客户端也要能降级"""
        self.assertIsInstance(C.get_openai_client(), C._UnavailableModel)

    def test_real_client_when_configured(self):
        """有 Key 时应返回真实客户端"""
        self.set(LLM_API_KEY="sk-fake-but-nonempty", LLM_PROVIDER="dashscope")
        m = C.get_chat_model("main")
        self.assertNotIsInstance(m, C._UnavailableModel)


# ======================================================================
class TestHelpers(LLMConfigTestCase):
    """7. 辅助函数"""

    def test_mask_key_hides_middle(self):
        """脱敏：不能泄漏完整 Key"""
        k = "sk-abcdefghijklmnopqrstuvwxyz123456"
        masked = C.mask_key(k)
        self.assertNotIn(k, masked)
        self.assertIn("sk-", masked)

    def test_mask_empty(self):
        self.assertIn("未配置", C.mask_key(""))
        self.assertIn("未配置", C.mask_key(None))

    def test_describe_contains_essentials(self):
        """describe() 输出应包含排错所需的关键信息"""
        self.set(LLM_API_KEY="sk-test-key-0123456789abcdef")
        d = C.describe()
        for kw in ["状态", "API Key", "Base URL", "模型 main", "模型 fast"]:
            with self.subTest(keyword=kw):
                self.assertIn(kw, d)

    def test_describe_masks_key(self):
        """describe() 不能泄漏完整 Key"""
        key = "sk-verysecretkeyvalue1234567890"
        self.set(LLM_API_KEY="sk-verysecretkeyvalue0123456789abcdef")
        self.assertNotIn(key, C.describe())

    def test_timeout_temperature_parsing(self):
        """数值配置能正确解析，非法值走默认"""
        self.set(LLM_TIMEOUT="120", LLM_TEMPERATURE="0.7")
        self.assertEqual(C._int("LLM_TIMEOUT", 60), 120)
        self.assertAlmostEqual(C._float("LLM_TEMPERATURE", 0.3), 0.7)

        self.set(LLM_TIMEOUT="abc", LLM_TEMPERATURE="xyz")
        self.assertEqual(C._int("LLM_TIMEOUT", 60), 60, "非法值应回退默认")
        self.assertAlmostEqual(C._float("LLM_TEMPERATURE", 0.3), 0.3)


# ======================================================================
class TestLiveCall(LLMConfigTestCase):
    """8. 真实调用（需要有效 Key + 可用配额，默认跳过）"""

    @unittest.skipUnless("--live" in sys.argv, "需要 --live 参数")
    def test_live_invoke(self):
        """真实调用一次，验证配置端到端可用"""
        os.environ.pop("LLM_ENABLED", None)
        load_env()

        ok, problems = C.validate()
        if not ok:
            self.skipTest("配置不可用: %s" % problems)

        m = C.get_chat_model("fast", streaming=False, max_tokens=10)
        try:
            r = m.invoke("回答一个字：好")
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            # 配额/权限/模型名这类「账号或网关侧」的问题，不是配置代码的 bug，
            # 跳过并给出排查提示，避免把账号问题误报成代码缺陷。
            if any(k in low for k in ("quota", "exhausted", "not exist",
                                      "model_not_found", "403", "permission")):
                self.skipTest(
                    "网关侧不可用（非配置问题）: %s" % msg[:160]
                    + chr(10) + "  用 python tests/kg/config.py --probe 查看本网关支持的模型"
                )
            raise

        self.assertTrue((r.content or "").strip(), "模型返回了空内容")


def main():
    argv = [a for a in sys.argv if a != "--live"]
    unittest.main(argv=argv, verbosity=2 if "-v" in argv else 1)


if __name__ == "__main__":
    main()
