"""Тесты чистых функций proxy.py.

Запуск:  python3 -m unittest discover -s tests -v
         (или: python3 tests/test_core.py)

Модуль proxy.py намеренно импортируется без побочных эффектов: монитор цен
и веб-сервер запускаются только внутри if __name__ == "__main__".
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location("proxy", PROJECT_ROOT / "proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class TestEnvRefs(unittest.TestCase):
    """Ссылки {env:ПЕРЕМЕННАЯ} в настройках."""

    def test_resolves_known_variable(self):
        os.environ["PROXY_TEST_KEY"] = "sk-abcdef123456"
        self.assertEqual(proxy.resolve_env_refs("{env:PROXY_TEST_KEY}"), "sk-abcdef123456")
        del os.environ["PROXY_TEST_KEY"]

    def test_missing_variable_gives_empty_string(self):
        self.assertEqual(proxy.resolve_env_refs("{env:PROXY_TEST_ABSENT}"), "")

    def test_whitespace_inside_reference_is_tolerated(self):
        os.environ["PROXY_TEST_KEY"] = "value"
        self.assertEqual(proxy.resolve_env_refs("{env: PROXY_TEST_KEY }"), "value")
        del os.environ["PROXY_TEST_KEY"]

    def test_plain_values_pass_through(self):
        for value in ["sk-plain-key", "", "  ", "{env:", "{env:PROXY_TEST_KEY}x", None, 5000]:
            self.assertEqual(proxy.resolve_env_refs(value), value, value)

    def test_resolve_api_key_reports_missing_variable(self):
        key, error = proxy.resolve_api_key({"api_key": "{env:PROXY_TEST_ABSENT}"})
        self.assertEqual(key, "")
        self.assertIn("PROXY_TEST_ABSENT", error)

    def test_resolve_api_key_accepts_plain_key(self):
        key, error = proxy.resolve_api_key({"api_key": "sk-real"})
        self.assertEqual(key, "sk-real")
        self.assertIsNone(error)

    def test_resolve_api_key_rejects_placeholder(self):
        _, error = proxy.resolve_api_key({"api_key": "ВАШ_API_КЛЮЧ_ЗДЕСЬ"})
        self.assertIn("не настроен", error.lower())

    def test_malformed_reference_is_reported(self):
        # Кириллица в имени переменной — не молчаливый уход строки в RouterAI
        _, error = proxy.resolve_api_key({"api_key": "{env:НЕ_ТАКАЯ}"})
        self.assertIsNotNone(error)
        self.assertIn("Некорректная ссылка", error)


class TestFormatTokens(unittest.TestCase):
    """Компактный формат количества токенов."""

    def test_thousands_become_k(self):
        self.assertEqual(proxy.format_tokens(1048576), "1048K")
        self.assertEqual(proxy.format_tokens(65536), "65K")
        self.assertEqual(proxy.format_tokens(8192), "8K")

    def test_small_values_are_not_rounded_to_zero(self):
        # Раньше 800 // 1000 давало «0K»
        self.assertEqual(proxy.format_tokens(800), "800")
        self.assertEqual(proxy.format_tokens(512), "512")

    def test_empty_means_dash(self):
        self.assertEqual(proxy.format_tokens(0), "—")
        self.assertEqual(proxy.format_tokens(None), "—")
        self.assertEqual(proxy.format_tokens("abc"), "—")


class TestFormatExtraPrices(unittest.TestCase):
    """Дополнительные тарифы: русские названия и единицы."""

    def test_request_price_is_shown_per_call(self):
        ep = {
            "pricing": {"request": 4.4150496, "prompt": 0, "completion": 0},
            "pricing_units": {"request": "request"},
        }
        self.assertEqual(proxy.format_extra_prices(ep, inline=True), "Запрос: 4.42 ₽/1 запрос")

    def test_unknown_unit_is_not_guessed(self):
        # Часы/секунды не должны отображаться как токены
        ep = {
            "pricing": {"seconds": 0.0017},
            "pricing_units": {"seconds": "second"},
        }
        out = proxy.format_extra_prices(ep, inline=True)
        self.assertIn("сек", out)

    def test_known_tariffs_have_russian_labels(self):
        ep = {"pricing": {"image": 1e-8, "web_search": 1545.0}}
        out = proxy.format_extra_prices(ep, inline=True)
        self.assertIn("Входящие изображения", out)
        self.assertIn("Веб-поиск", out)

    def test_empty_pricing_gives_empty_string(self):
        self.assertEqual(proxy.format_extra_prices({}, inline=True), "")
        self.assertEqual(proxy.format_extra_prices({"pricing": {"prompt": 1e-6}}, inline=True), "")


class TestPatchOpencodeConfig(unittest.TestCase):
    """Хирургическая правка чужого конфига — самый опасный код в проекте."""

    CONFIG = '''{
  // мои настройки, которые нельзя потерять
  "$schema": "https://opencode.ai",
  "theme": "my-theme",
  "provider": {
    "anthropic": { "options": { "baseURL": "https://api.anthropic.com/v1" } },
    "custom": {
      "name": "RouterAI",
      "options": { "baseURL": "http://127.0.0.1:5000/v1", "apiKey": "local-auth-not-needed" },
      "models": {
        "routerai/proxy-model": {
          "limit": { "context": 1000, "output": 2000 }
        }
      }
    }
  },
  "compaction": { "auto": true, "reserved": 15000 }
}'''

    def test_updates_port_and_limits(self):
        new, changes, found = proxy.patch_opencode_config(self.CONFIG, 5555, 999999, 131000)
        self.assertTrue(found)
        self.assertIn('http://127.0.0.1:5555/v1', new)
        self.assertIn('"context": 999999', new)
        self.assertIn('"output": 131000', new)
        self.assertEqual(changes, ["порт 5000 → 5555", "context 1000 → 999999", "output 2000 → 131000"])

    def test_preserves_everything_else(self):
        new, _, _ = proxy.patch_opencode_config(self.CONFIG, 5555, 999999, 131000)
        self.assertIn("// мои настройки, которые нельзя потерять", new)
        self.assertIn('"theme": "my-theme"', new)
        self.assertIn('"baseURL": "https://api.anthropic.com/v1"', new)
        self.assertIn('"reserved": 15000', new)
        self.assertIn('"$schema"', new)

    def test_idempotent(self):
        once, _, _ = proxy.patch_opencode_config(self.CONFIG, 5555, 999999, 131000)
        twice, changes, _ = proxy.patch_opencode_config(once, 5555, 999999, 131000)
        self.assertEqual(once, twice)
        self.assertEqual(changes, [])

    def test_foreign_config_is_left_alone(self):
        foreign = '{"provider": {"other": {"options": {}}}}'
        new, changes, found = proxy.patch_opencode_config(foreign, 5555, 1, 1)
        self.assertEqual(new, foreign)
        self.assertEqual(changes, [])
        self.assertFalse(found)

    def test_missing_port_defaults_to_80(self):
        cfg = '{ "baseURL": "http://127.0.0.1/v1" }'
        new, changes, _ = proxy.patch_opencode_config(cfg, 5555, 1, 1)
        self.assertIn('http://127.0.0.1:5555/v1', new)
        self.assertEqual(changes, ["порт 80 → 5555"])


class TestAtomicWrite(unittest.TestCase):
    """Запись файлов не должна оставлять обрезанные файлы."""

    def test_writes_and_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            def read():
                with open(path, encoding="utf-8") as f:
                    return json.load(f)

            proxy.atomic_write(path, json.dumps({"a": 1}))
            self.assertEqual(read(), {"a": 1})
            proxy.atomic_write(path, json.dumps({"a": 2}))
            self.assertEqual(read(), {"a": 2})

    def test_leaves_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            proxy.atomic_write(path, "x")
            self.assertEqual(os.listdir(d), ["settings.json"])

    def test_unicode_survives(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "u.json")
            proxy.atomic_write(path, json.dumps({"модель": " deepseek"}, ensure_ascii=False))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["модель"], " deepseek")


class TestUpdateSettings(unittest.TestCase):
    """Обновление настроек под блокировкой."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = proxy.SETTINGS_FILE
        proxy.SETTINGS_FILE = os.path.join(self._tmp.name, "proxy_settings.json")

    def tearDown(self):
        proxy.SETTINGS_FILE = self._orig
        self._tmp.cleanup()

    def test_creates_file_with_defaults(self):
        result = proxy.update_settings(lambda s: None)
        self.assertEqual(result, proxy.default_settings)
        self.assertTrue(os.path.exists(proxy.SETTINGS_FILE))

    def test_preserves_unrelated_keys(self):
        proxy.update_settings(lambda s: s.__setitem__("api_key", "sk-1"))
        proxy.update_settings(lambda s: s.__setitem__("port", 6000))
        settings = proxy.load_settings()
        self.assertEqual(settings["api_key"], "sk-1")
        self.assertEqual(settings["port"], 6000)

    def test_sequential_updates_do_not_lose_each_other(self):
        # Раньше load + save в разных потоках затирали правки друг друга
        for i in range(20):
            proxy.update_settings(lambda s, i=i: s.__setitem__(f"k{i}", i))
        settings = proxy.load_settings()
        for i in range(20):
            self.assertEqual(settings[f"k{i}"], i)


class TestReadBody(unittest.TestCase):
    """Чтение тела запроса не должно терять данные из-за битого заголовка."""

    def _handler(self, headers):
        """Собирает объект с нужными атрибутами, не поднимая сервер."""
        import io

        handler = proxy.ProxyHandler.__new__(proxy.ProxyHandler)
        handler.headers = headers
        handler.rfile = io.BytesIO(b'{"a": 1}')
        return handler

    def test_returns_body_for_valid_length(self):
        self.assertEqual(self._handler({"Content-Length": "8"})._read_body(), b'{"a": 1}')

    def test_garbage_length_gives_empty_body(self):
        for bad in ["abc", "", None, "-5"]:
            self.assertEqual(self._handler({"Content-Length": bad})._read_body(), b"")

    def test_huge_length_is_clamped(self):
        self.assertEqual(self._handler({"Content-Length": "999999999"})._read_body(), b'{"a": 1}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
