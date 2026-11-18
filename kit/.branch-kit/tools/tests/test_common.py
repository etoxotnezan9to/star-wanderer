"""Общая часть: сверка настройки, ключ, клиент площадки, вывод, ключи
командной строки. На каждый вид нарушения настройки — испорченный вход и
код 2 с именем поля."""

import argparse
import contextlib
import gc
import http.client
import io
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import yaml

import helpers
import fake_api
import kit_common as kit

TEMPLATE = os.path.join(helpers.KIT_DIR, "config.yaml")

# Места заготовки настройки и чем их заполнить; новое место в заготовке —
# новая строка здесь.
FILLED = {
    "<адрес репозитория комплекта>": "http://127.0.0.1/kit/branch-kit.git",
    "<8 знаков>": "1a2b3c4d",
    "<адрес площадки>": "http://127.0.0.1",
    "<группа>": "example",
    "<репозиторий>": "app",
    "<учётная запись владельца>": "owner",
    "<учётная запись постановщика, например ai-planner>": "ai-planner",
    "<учётная запись автора, например ai-dev>": "ai-dev",
    "<имя для подписи, например AI Developer>": "AI Developer",
    "<имя для подписи, например AI Reviewer>": "AI Reviewer",
    "<адрес почты>": "someone@example.invalid",
    "<учётная запись проверяющего, например ai-reviewer>": "ai-reviewer",
    "<учётная запись>": "analyst",
    "<аналитик или разработчик>": "аналитик",
    "<имя проверки, например build>": "build",
    "<команда в Windows>": "build.cmd",
    "<команда в Linux>": "./build.sh",
}


def problems_of(text):
    """Нарушения настройки из текста: [(строка, поле, что не так)]."""
    return kit.validate(yaml.safe_load(text),
                        yaml.compose(text, Loader=yaml.SafeLoader))


def problems_of_data(data):
    return problems_of(helpers.dump(data))


DROP = object()


def with_change(path, value):
    """Настройка, в которой поле по пути path заменено на value; DROP —
    поле убрано."""
    data = helpers.config()
    node = data
    for key in path[:-1]:
        node = node[key]
    if value is DROP:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


class ConfigTest(unittest.TestCase):

    def assertProblem(self, problems, field, text):
        found = [p for p in problems if p[1] == field and text in p[2]]
        self.assertTrue(found, "нет нарушения «%s: %s» в %s"
                        % (field, text, problems))

    def test_valid(self):
        """Заполненная настройка сверяется без нарушений."""
        self.assertEqual(problems_of_data(helpers.config()), [])

    def test_template_places(self):
        """Заготовка комплекта: код 2, названо каждое поле с местом <…>."""
        with self.assertRaises(kit.ToolError) as ctx:
            kit.load_config(TEMPLATE)
        text = "\n".join(ctx.exception.lines)
        self.assertIn("поле «площадка / адрес»: осталось место "
                      "«<адрес площадки>»", text)
        self.assertIn("поле «проверки / 2 / windows»", text)
        self.assertRegex(ctx.exception.lines[-1],
                         r"^настройка не сверилась: нарушений \d+$")

    def test_template_filled(self):
        """Заготовка, где места заполнены, сверяется без нарушений: поля
        заготовки и сверки совпадают."""
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        for place, value in FILLED.items():
            text = text.replace(place, value)
        self.assertEqual(kit.PLACE.findall(yaml.safe_dump(
            yaml.safe_load(text), allow_unicode=True)), [])
        self.assertEqual(problems_of(text), [])

    def test_missing_field(self):
        for path in (["площадка"], ["площадка", "адрес"],
                     ["учётные записи", "проверяющий", "подпись"],
                     ["опрос", "период"]):
            with self.subTest(path):
                self.assertProblem(problems_of_data(with_change(path, DROP)),
                                   " / ".join(path), "не задано")

    def test_optional_fields(self):
        """Необязательные поля можно не ставить."""
        for path in (["конвейер"], ["формат документов"],
                     ["учётные записи", "люди"]):
            with self.subTest(path):
                self.assertEqual(problems_of_data(with_change(path, DROP)),
                                 [])

    def test_wrong_kind(self):
        cases = [
            (["основная ветка"], 5, "ожидается строка, а здесь число"),
            (["ветки задач"], "docs/", "ожидается список"),
            (["опрос", "период"], "2", "ожидается число, а здесь строка"),
            (["опрос", "период"], True, "ожидается число"),
            (["конвейер", "есть"], "нет", "ожидается true или false"),
            (["формат документов", "длина строки"], 7.5,
             "ожидается целое число"),
            (["площадка"], "http://127.0.0.1", "ожидается запись с полями"),
            (["основная ветка"], None, "здесь пусто"),
        ]
        for path, value, text in cases:
            with self.subTest(path=path, value=value):
                data = with_change(path, value)
                self.assertProblem(problems_of_data(data), " / ".join(path),
                                   text)

    def test_place_left(self):
        data = with_change(["учётные записи", "авторы"],
                           [{"учётная запись": "<учётная запись автора>",
                             "переменная ключа": "BRANCH_KIT_AUTHOR_TOKEN",
                             "подпись": {"имя": "AI", "почта": "a@b"}}])
        self.assertProblem(problems_of_data(data),
                           "учётные записи / авторы / 1 / учётная запись",
                           "осталось место «<учётная запись автора>»")

    def test_shell_redirect_not_a_place(self):
        """Перенаправление в команде проверки местом <…> не считается."""
        data = helpers.config()
        data["проверки"][0]["команда"] = "sort <in.txt >out.txt"
        self.assertEqual(problems_of_data(data), [])

    def test_regex_syntax_not_a_place(self):
        """В образце слова именованная группа (?P<имя>…) и «<» в (?<=…) и
        (?<!…) местом <…> не считаются; «<…>» как текст образца — место,
        как в любом другом поле, в том числе после «\\(»."""
        field = "формат документов / слова / 1 / образец"
        for pattern in (r"(?P<корень>фич)\w*", r"(?P<a>фич)(?P=a)",
                        r"(?<!-)фич\w*>?", r"(?<=\s)фич(?=>)"):
            with self.subTest(pattern):
                data = helpers.config()
                data["формат документов"]["слова"][0]["образец"] = pattern
                self.assertEqual(problems_of_data(data), [])
        for pattern, place in (("<образец>", "<образец>"),
                               (r"(?P<a>фич)<b>", "<b>"),
                               (r"\(?P<x>фич", "<x>")):
            with self.subTest(pattern):
                data = helpers.config()
                data["формат документов"]["слова"][0]["образец"] = pattern
                self.assertProblem(problems_of_data(data), field,
                                   "осталось место «%s»" % place)

    def test_unknown_field(self):
        data = helpers.config()
        data["площадка"]["адресс"] = "http://127.0.0.1"
        data["опросы"] = {"период": 2}
        problems = problems_of_data(data)
        self.assertProblem(problems, "площадка / адресс", "неизвестное поле")
        self.assertProblem(problems, "опросы", "неизвестное поле")

    def test_duplicate_field(self):
        text = helpers.dump(helpers.config()) + "основная ветка: master\n"
        self.assertProblem(problems_of(text), "основная ветка",
                           "поле повторяется")

    def test_empty(self):
        self.assertProblem(problems_of_data(with_change(["основная ветка"],
                                                        "  ")),
                           "основная ветка", "пустая строка")
        self.assertProblem(problems_of_data(with_change(["ветки задач"], [])),
                           "ветки задач", "пустой список")

    def test_values(self):
        cases = [
            (["площадка", "адрес"], "127.0.0.1", "с http:// или https://"),
            (["площадка", "адрес"], "http://user:secret@127.0.0.1",
             "учётные данные"),
            (["площадка", "репозиторий"], "other/app", "с группой «example»"),
            (["учётные записи", "постановщик", "переменная ключа"],
             "КЛЮЧ ПОСТАНОВЩИКА", "имя переменной окружения"),
            (["рабочие каталоги", "переменная базы"], "1BASE",
             "имя переменной окружения"),
            (["опрос", "период"], 0, "нужно больше нуля"),
            (["опрос", "не дольше"], -1, "нужно 0 или больше"),
            (["формат документов", "кодировка"], "cp1251",
             "допустимо: utf-8 без BOM, utf-8 с BOM"),
            (["формат документов", "концы строк"], "lf",
             "допустимо: LF, CRLF"),
            (["учётные записи", "люди"],
             [{"учётная запись": "x", "роль": "тестировщик"}],
             "допустимо: аналитик, разработчик"),
        ]
        for path, value, text in cases:
            with self.subTest(path=path, value=value):
                problems = problems_of_data(with_change(path, value))
                if path[-1] == "люди":
                    path = path + ["1", "роль"]
                self.assertProblem(problems, " / ".join(path), text)

    def test_url_text_hides_credentials(self):
        """Адрес с учётными данными не печатается в нарушении."""
        problems = problems_of_data(with_change(
            ["площадка", "адрес"], "http://user:secret@127.0.0.1"))
        self.assertNotIn("secret", str(problems))

    def test_checks(self):
        cases = [
            ({"имя": "сборка", "команда": "make"}, "проверки / 1 / имя",
             "латиницей"),
            ({"имя": "build"}, "проверки / 1",
             "нужно поле «команда» либо оба поля"),
            ({"имя": "build", "windows": "a.cmd"}, "проверки / 1",
             "нужно поле «команда» либо оба поля"),
            ({"имя": "build", "команда": "make", "linux": "make"},
             "проверки / 1", "вместе не ставят"),
            ({"имя": "build", "команда": "make", "не дольше": 0},
             "проверки / 1 / не дольше", "нужно больше нуля"),
            ({"имя": "build", "команда": "make", "успех": {"код": 0}},
             "проверки / 1 / успех / код", "неизвестное поле"),
        ]
        for check, field, text in cases:
            with self.subTest(check=check):
                self.assertProblem(
                    problems_of_data(helpers.config(проверки=[check])),
                    field, text)

    def test_check_names_unique(self):
        data = helpers.config(проверки=[{"имя": "a", "команда": "x"},
                                        {"имя": "a", "команда": "y"}])
        self.assertProblem(problems_of_data(data), "проверки / 2 / имя",
                           "уже есть у проверки 1")

    def test_bad_word_pattern(self):
        data = helpers.config()
        data["формат документов"]["слова"][0]["образец"] = "фич(а"
        self.assertProblem(problems_of_data(data),
                           "формат документов / слова / 1 / образец",
                           "не разбирается как регулярное выражение")

    def test_pattern_same_everywhere(self):
        """Образец, который Python 3.8 и новее разбирают по-разному, —
        нарушение на любом из них: атомарная группа и повтор без возврата
        (3.11 и новее разбирают, 3.8–3.10 — нет), флаги не в начале
        (наоборот), условие с группой «+1» (3.12 и новее не разбирают),
        «\\z» (разбирает только 3.14). Число повторов больше, чем знает
        re, — нарушение, а не трассировка. Образец, который разбирают все,
        — без нарушений."""
        field = "формат документов / слова / 1 / образец"
        cases = [
            ("фич(?>а)", "атомарная группа (?>…)"),
            ("фич*+", "повтор без возврата"),
            ("фич{1,2}+", "повтор без возврата"),
            ("фич(?i)а", "флаги вида (?i)"),
            ("фича|(?i)фичи", "флаги вида (?i)"),
            ("(фич)(?(+1)а|и)", "в условии (?(…)…)"),
            (r"фич\z", r"\z — только с Python 3.14"),
        ]
        for pattern, text in cases:
            with self.subTest(pattern):
                data = helpers.config()
                data["формат документов"]["слова"][0]["образец"] = pattern
                self.assertProblem(problems_of_data(data), field,
                                   kit.NOT_EVERYWHERE_TEXT + text)
        data = helpers.config()
        data["формат документов"]["слова"][0]["образец"] = "фич{99999999999}"
        self.assertProblem(problems_of_data(data), field,
                           "не разбирается как регулярное выражение")
        for pattern in (r"\bфич\w*", "(?i)(?s)фича", "(?x) фич # (?>а)",
                        "фич{x}+", "фич*?", "[+?]+фич", r"фич\Z",
                        "(фич)(?(1)а|и)"):
            with self.subTest(pattern):
                data = helpers.config()
                data["формат документов"]["слова"][0]["образец"] = pattern
                self.assertEqual(problems_of_data(data), [])

    def test_line_numbers(self):
        """Нарушение называет строку файла настройки."""
        text = helpers.dump(helpers.config()).replace(
            "основная ветка: main", "основная ветка: <ветка>")
        line = text.splitlines().index("основная ветка: <ветка>") + 1
        self.assertIn((line, "основная ветка",
                       "осталось место «<ветка>» — заполните"),
                      problems_of(text))

    def test_load_errors(self):
        """Нет файла, не разбирается, не запись с полями — код 2."""
        clone = helpers.Clone()
        self.addCleanup(clone.remove)
        cases = [
            (None, "нет настройки"),
            ("основная ветка: [main\n", "настройка не разбирается"),
            ("- a\n- b\n", "поле «(весь файл)»: ожидается запись с полями"),
            ("", "здесь пусто"),
        ]
        for text, expected in cases:
            with self.subTest(expected):
                path = clone.path("bad.yaml")
                if text is None:
                    path = clone.path("missing.yaml")
                else:
                    clone.write("bad.yaml", text)
                with self.assertRaises(kit.ToolError) as ctx:
                    kit.load_config(path)
                self.assertIn(expected, "\n".join(ctx.exception.lines))

    def test_every_tool_refuses_bad_config(self):
        """Каждое средство до любого действия сверяет настройку: код 2 с
        именем поля, и площадка не тронута."""
        platform = fake_api.FakePlatform()
        self.addCleanup(platform.stop)
        platform.user(helpers.TOKENS["ai-dev"], "ai-dev")
        cfg = helpers.config()
        cfg["площадка"]["адрес"] = platform.url
        cfg["опрос"]["перод"] = 2
        clone = helpers.Clone(cfg)
        self.addCleanup(clone.remove)
        runs = {
            "check_repo.py": clone.run("check_repo.py"),
            "check_repo.py --config-only": clone.run("check_repo.py",
                                                     "--config-only"),
            "check_docs.py": clone.run("check_docs.py"),
            "poll.py": clone.run("poll.py", "--role", "author", "--mr", "1",
                                 token=helpers.TOKENS["ai-dev"]),
        }
        for name, run in runs.items():
            with self.subTest(name):
                self.assertEqual(run.code, 2, run)
                self.assertIn("поле «опрос / перод»: неизвестное поле",
                              run.err)
                self.assertNotIn(helpers.TOKENS["ai-dev"], run.out + run.err)
        self.assertEqual(platform.calls, [])

    def test_bad_date(self):
        """Дата вне календаря в настройке: PyYAML бросает ValueError, а не
        YAMLError — код 2 у всех средств, причина названа, трассировки нет,
        площадка не тронута."""
        platform = fake_api.FakePlatform()
        self.addCleanup(platform.stop)
        platform.user(helpers.TOKENS["ai-dev"], "ai-dev")
        cfg = helpers.config()
        cfg["площадка"]["адрес"] = platform.url
        clone = helpers.Clone(helpers.dump(cfg) + "дата: 2026-02-30\n")
        self.addCleanup(clone.remove)
        token = helpers.TOKENS["ai-dev"]
        runs = {
            "check_repo.py": clone.run("check_repo.py"),
            "check_repo.py --config-only": clone.run("check_repo.py",
                                                     "--config-only"),
            "check_docs.py": clone.run("check_docs.py"),
            "check_merge_log.py": clone.run("check_merge_log.py", token=token),
            "poll.py": clone.run("poll.py", "--role", "author", "--mr", "1",
                                 token=token),
        }
        for name, run in runs.items():
            with self.subTest(name):
                self.assertEqual(run.code, 2, run)
                self.assertIn("настройка не разбирается", run.err)
                self.assertIn("настройка не сверилась: нарушений 1", run.err)
                self.assertNotIn("Traceback", run.err)
        self.assertEqual(platform.calls, [])

    def test_no_yaml(self):
        """Нет PyYAML — код 2 с советом, как поставить."""
        with mock.patch.object(kit, "yaml", None):
            with self.assertRaises(kit.ToolError) as ctx:
                kit.load_config(TEMPLATE)
        self.assertIn("python -m pip install pyyaml", str(ctx.exception))


GOOD = "glpat-AbC_0123456789.xyz"
BAD = {
    "кириллица": "ключ-root",
    "пробел внутри": "glpat-abc def",
    "табуляция внутри": "glpat-abc\tdef",
    "перевод строки внутри": "glpat-abc\ndef",
    "возврат каретки внутри": "glpat-abc\rdef",
    "знак вне ASCII": "glpat-abéc",
}
# Файл ключа, записанный PowerShell 5.1 «Set-Content -Encoding utf8»: метка
# порядка байтов в начале, $(cat <файл>) её оставляет.
BOM = chr(0xFEFF) + GOOD
# Края, которые даёт файл ключа: $(cat <файл>) оставляет \r от CRLF,
# Get-Content -Raw — перевод строки. Края отбрасываются, ключ годен.
EDGES = {
    "CR от CRLF": GOOD + "\r",
    "перевод строки в конце": GOOD + "\n",
    "CRLF в конце": GOOD + "\r\n",
    "пробелы по краям": "  " + GOOD + " \t",
}


class TokenTest(unittest.TestCase):

    def test_bad_tokens(self):
        for name, token in BAD.items():
            with self.subTest(name):
                with mock.patch.dict(os.environ, {"GITLAB_TOKEN": token}):
                    with self.assertRaises(kit.ToolError) as ctx:
                        kit.require_token()
                self.assertEqual(str(ctx.exception), kit.BAD_TOKEN_TEXT)

    def test_bom_named(self):
        """Ключ с меткой порядка байтов — отказ, и причина называет метку:
        иначе человек проверял бы сам ключ, а не файл. Ни ключ, ни его
        знаки в причину не попадают."""
        for token in (BOM, BOM + "\r\n"):
            with self.subTest(repr(token[-2:])):
                with mock.patch.dict(os.environ, {"GITLAB_TOKEN": token}):
                    with self.assertRaises(kit.ToolError) as ctx:
                        kit.require_token()
                self.assertEqual(str(ctx.exception), kit.BOM_TOKEN_TEXT)
                self.assertNotIn(GOOD, str(ctx.exception))
        with mock.patch("urllib.request.OpenerDirector.open") as op:
            with self.assertRaises(kit.ToolError) as ctx:
                kit.Client("http://127.0.0.1:9", BOM)
        self.assertEqual(str(ctx.exception), kit.BOM_TOKEN_TEXT)
        op.assert_not_called()
        self.assertFalse(kit.token_valid(BOM))

    def test_no_token(self):
        for env in ({"GITLAB_TOKEN": ""}, {"GITLAB_TOKEN": " \r\n"}, {}):
            with self.subTest(env):
                with mock.patch.dict(os.environ, env):
                    if not env:
                        os.environ.pop("GITLAB_TOKEN", None)
                    with self.assertRaises(kit.ToolError) as ctx:
                        kit.require_token()
                self.assertEqual(str(ctx.exception), kit.NO_TOKEN_TEXT)

    def test_edges_stripped(self):
        for name, token in EDGES.items():
            with self.subTest(name):
                with mock.patch.dict(os.environ, {"GITLAB_TOKEN": token}):
                    self.assertEqual(kit.require_token(), GOOD)

    def test_client_refuses_bad_token(self):
        with mock.patch("urllib.request.OpenerDirector.open") as op:
            with self.assertRaises(kit.ToolError):
                kit.Client("http://127.0.0.1:9", BAD["кириллица"])
        op.assert_not_called()

    def test_host(self):
        cfg = helpers.config()
        self.assertEqual(kit.platform_host(cfg), "http://127.0.0.1:9")
        self.assertEqual(kit.platform_host(cfg, "https://127.0.0.1/"),
                         "https://127.0.0.1")
        for host in ("127.0.0.1", "ftp://127.0.0.1"):
            with self.subTest(host):
                with self.assertRaises(kit.ToolError):
                    kit.platform_host(cfg, host)
        with self.assertRaises(kit.ToolError) as ctx:
            kit.platform_host(cfg, "http://u:" + GOOD + "@127.0.0.1")
        self.assertNotIn(GOOD, str(ctx.exception))


class UrlTest(unittest.TestCase):
    """Адрес площадки в настройке и в ключе --host: итог сверки один на
    всех Python 3.8 и новее."""

    def assertRefused(self, value, text):
        problems = problems_of_data(with_change(["площадка", "адрес"], value))
        found = [p for p in problems
                 if p[1] == "площадка / адрес" and text in p[2]]
        self.assertTrue(found, "нет нарушения «%s» в %s" % (text, problems))
        with self.assertRaises(kit.ToolError) as ctx:
            kit.platform_host(helpers.config(), value)
        self.assertIn(text, str(ctx.exception))

    def test_spaces(self):
        """Пробелы по краям отбрасываются, как в platform_host; пробел или
        управляющий знак внутри — нарушение. urlsplit Python 3.8.10,
        3.10.11 и 3.11.9 отбрасывает такие знаки по-разному."""
        for value in (" http://127.0.0.1", "\thttp://127.0.0.1\r\n"):
            with self.subTest(value):
                self.assertEqual(problems_of_data(
                    with_change(["площадка", "адрес"], value)), [])
                self.assertEqual(kit.platform_host(helpers.config(), value),
                                 "http://127.0.0.1")
        for value in ("\x01http://127.0.0.1", "ht\ttp://127.0.0.1",
                      "http://127.0.\n0.1"):
            with self.subTest(value):
                self.assertRefused(value, "пробел или управляющий знак")

    def test_brackets(self):
        """Незакрытая квадратная скобка — нарушение с кодом 2, а не
        трассировка; узел в скобках — только адрес IPv6: Python 3.8–3.10
        и 3.11 и новее сверяют его по-разному."""
        for value in ("http://[::1", "http://::1]", "http://[abc]",
                      "http://[127.0.0.1]", "http://a[::1]",
                      "http://[::1]a"):
            with self.subTest(value):
                self.assertRefused(value, "квадратные скобки")
        self.assertEqual(problems_of_data(
            with_change(["площадка", "адрес"], "http://[::1]:8080")), [])
        self.assertEqual(kit.platform_host(helpers.config(),
                                           "http://[::1]:8080/"),
                         "http://[::1]:8080")
        clone = helpers.Clone(with_change(["площадка", "адрес"],
                                          "http://[::1"))
        self.addCleanup(clone.remove)
        run = clone.run("check_repo.py", "--config-only")
        self.assertEqual(run.code, 2, run)
        self.assertIn("поле «площадка / адрес»: квадратные скобки", run.err)
        self.assertNotIn("Traceback", run.err)


class ClientTest(unittest.TestCase):

    def setUp(self):
        self.platform = fake_api.FakePlatform()
        self.addCleanup(self.platform.stop)
        self.platform.user(GOOD, "ai-dev")
        self.env = helpers.patch_env()
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = kit.Client(self.platform.url, GOOD)

    def test_get_and_token_header(self):
        self.assertEqual(self.client.get("user")["username"], "ai-dev")
        self.assertEqual(self.platform.calls[-1][1], GOOD)

    def test_pages(self):
        for iid in range(1, 251):
            self.platform.add_request(iid)
        items = self.client.get_all("projects/example%2Fapp/merge_requests")
        self.assertEqual(len(items), 250)
        self.assertEqual(len(self.platform.paths("merge_requests")), 3)

    def pages_of(self, count):
        """Обход выдачи из count запросов."""
        with self.platform.lock:
            for iid in range(1, count + 1):
                self.platform.add_request(iid)
        return self.client.get_all("projects/example%2Fapp/merge_requests")

    def test_pages_number_not_growing(self):
        """Тот же или меньший номер следующей страницы — сбой, а не повтор
        без конца: срок опроса такой обход не прервал бы. Заголовок подменён
        только на несколько обращений: прежний код здесь не зависает, а
        отдаёт лишнее, и проверка падает, а не ждёт."""
        for name, values, asked in (("тот же", ["1"] * 5, 1),
                                    ("меньший", ["3", "2", "1"], 2),
                                    ("нулевой", ["0"] * 5, 1)):
            with self.subTest(name):
                self.platform.next_pages = list(values)
                before = len(self.platform.paths("merge_requests"))
                with self.assertRaises(kit.ApiError) as ctx:
                    self.pages_of(5)
                self.assertTrue(ctx.exception.transient)
                self.assertIn("X-Next-Page", str(ctx.exception))
                self.assertEqual(len(self.platform.paths("merge_requests"))
                                 - before, asked)

    def test_pages_number_not_a_number(self):
        """Заголовок — не номер: сбой площадки, а не исключение Python."""
        self.platform.next_pages = ["next"]
        with self.assertRaises(kit.ApiError) as ctx:
            self.pages_of(5)
        self.assertTrue(ctx.exception.transient)
        self.assertIn("X-Next-Page «next»", str(ctx.exception))

    def test_pages_header_missing_on_full_page(self):
        """Заголовка нет вовсе, а страница полна — сбой: прокси срезал
        заголовок, и выдача молча оборвалась бы на сотне записей."""
        self.platform.next_pages = [None]
        with self.assertRaises(kit.ApiError) as ctx:
            self.pages_of(150)
        self.assertTrue(ctx.exception.transient)
        self.assertIn("X-Next-Page", str(ctx.exception))

    def test_pages_header_empty_on_full_page(self):
        """Опорный: ровно 100 записей, заголовок есть и пуст — конец выдачи
        без сбоя. Так GitLab отвечает на полную последнюю страницу."""
        self.assertEqual(len(self.pages_of(100)), 100)
        self.assertEqual(len(self.platform.paths("merge_requests")), 1)

    def test_pages_header_blank_on_full_page(self):
        """Заголовок из пробелов — тоже пуст."""
        self.platform.next_pages = ["  "]
        self.assertEqual(len(self.pages_of(100)), 100)

    def test_pages_header_missing_on_short_page(self):
        """Опорный: заголовка нет, страница неполна — конец выдачи."""
        self.platform.next_pages = [None]
        self.assertEqual(len(self.pages_of(99)), 99)

    def test_pages_growing_number(self):
        """Опорный: номер растёт, пропуск страниц не сбой."""
        self.platform.next_pages = ["3", ""]
        self.assertEqual(len(self.pages_of(250)), 100 + 50)

    def test_pages_empty_after_full(self):
        """Опорный: полная страница называет следующую, а та пуста — конец
        выдачи без сбоя. Так площадка отвечает об изменениях ветки: замерено
        на GitLab 17.11.2."""
        self.platform.next_pages = ["2", ""]
        self.assertEqual(len(self.pages_of(100)), 100)
        self.assertEqual(len(self.platform.paths("merge_requests")), 2)

    def test_http_error(self):
        with self.assertRaises(kit.ApiError) as ctx:
            self.client.get("projects/other%2Fapp/merge_requests")
        self.assertEqual(ctx.exception.code, 404)
        self.assertFalse(ctx.exception.transient)
        self.assertIn("404 Project Not Found", str(ctx.exception))
        self.platform.failures = 1
        with self.assertRaises(kit.ApiError) as ctx:
            self.client.get("user")
        self.assertEqual(ctx.exception.code, 500)
        self.assertTrue(ctx.exception.transient)

    def test_no_answer(self):
        client = kit.Client("http://127.0.0.1:9", GOOD, timeout=5)
        with self.assertRaises(kit.ApiError) as ctx:
            client.get("user")
        self.assertIsNone(ctx.exception.code)
        self.assertTrue(ctx.exception.transient)
        self.assertIn("площадка не отвечает", str(ctx.exception))
        self.assertNotIn(GOOD, str(ctx.exception))

    def test_redirect_not_followed(self):
        """Переадресацию клиент не выполняет: ключ на другой узел не уходит,
        отказ — с кодом ответа 3xx, не сбой."""
        other = fake_api.FakePlatform()
        self.addCleanup(other.stop)
        other.user(GOOD, "ai-dev")
        self.platform.redirect = other.url.replace(
            "127.0.0.1", "localhost") + "/api/v4/user"
        with mock.patch.dict(os.environ,
                             {"NO_PROXY": "127.0.0.0/8,localhost"}):
            client = kit.Client(self.platform.url, GOOD)
            with self.assertRaises(kit.ApiError) as ctx:
                client.get("user")
        self.assertEqual(ctx.exception.code, 302)
        self.assertFalse(ctx.exception.transient)
        self.assertIn("ответила переадресацией 302 на http://localhost:",
                      str(ctx.exception))
        self.assertNotIn(GOOD, str(ctx.exception))
        self.assertEqual(other.calls, [])

    def test_post_and_put(self):
        """POST и PUT: тело — JSON, ключ — в заголовке; отказ — с кодом и
        текстом ответа площадки; переадресация — отказ, а не переход."""
        item = self.client.post("projects/example%2Fapp/protected_branches",
                                {"name": "docs/*", "push_access_level": 30,
                                 "merge_access_level": 30,
                                 "allow_force_push": False})
        self.assertEqual(item["name"], "docs/*")
        self.assertEqual(self.platform.writes[-1][2:], (GOOD, {
            "name": "docs/*", "push_access_level": 30,
            "merge_access_level": 30, "allow_force_push": False}))
        self.platform.add_request(1)
        state = self.client.put("projects/example%2Fapp/merge_requests/1",
                                {"state_event": "close"})["state"]
        self.assertEqual(state, "closed")
        with self.assertRaises(kit.ApiError) as ctx:
            self.client.put("projects/example%2Fapp/merge_requests/1/merge",
                            {"sha": "1" * 40})
        self.assertEqual((ctx.exception.code, ctx.exception.answer),
                         (401, "401 Unauthorized"))
        self.assertIn("PUT projects/example%2Fapp/merge_requests/1/merge: "
                      "401 401 Unauthorized", str(ctx.exception))
        other = fake_api.FakePlatform()
        self.addCleanup(other.stop)
        self.platform.redirect = other.url + "/api/v4/user"
        with self.assertRaises(kit.ApiError) as ctx:
            self.client.post("projects/example%2Fapp/protected_branches",
                             {"name": "x"})
        self.assertEqual(ctx.exception.code, 302)
        self.assertEqual(other.calls, [])

    def test_error_body_cut(self):
        """Тело ответа-ошибки оборвалось при чтении (IncompleteRead — не
        OSError) — отказ ApiError с кодом и причиной из строки ответа, а не
        трассировка."""
        class Cut(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b"{\"mess", 40)

        answer = urllib.error.HTTPError(self.platform.url + "/api/v4/user",
                                        502, "Bad Gateway", {}, Cut())
        with mock.patch.object(self.client._opener, "open",
                               side_effect=answer):
            with self.assertRaises(kit.ApiError) as ctx:
                self.client.get("user")
        self.assertEqual(ctx.exception.code, 502)
        self.assertEqual(str(ctx.exception), "GET user: 502 Bad Gateway")
        self.assertTrue(answer.fp.closed)

    def test_error_answer_closed(self):
        """Ответ-ошибку — отказ и переадресацию — клиент закрывает сам: его
        соединение не ждёт уборки мусора, и ResourceWarning «unclosed
        <socket…>» нет. Ответ — ошибка HTTPError, на которой клиент
        отказал: она в __context__ отказа."""
        for redirect in (None, "http://localhost:9/api/v4/user"):
            with self.subTest(redirect=redirect):
                self.platform.redirect = redirect
                with self.assertRaises(kit.ApiError) as ctx:
                    self.client.get("projects/other%2Fapp/merge_requests")
                answer = ctx.exception.__context__
                self.assertIsInstance(answer, urllib.error.HTTPError)
                self.assertEqual(answer.code, 302 if redirect else 404)
                self.assertTrue(answer.fp.closed)

    def test_account(self):
        cfg = helpers.config()
        self.assertEqual(kit.check_account(self.client, cfg, "author"),
                         "ai-dev")
        with self.assertRaises(kit.ToolError) as ctx:
            kit.check_account(self.client, cfg, "reviewer")
        self.assertIn("ключ — учётной записи ai-dev", str(ctx.exception))
        stranger = kit.Client(self.platform.url, "glpat-unknown")
        with self.assertRaises(kit.ToolError) as ctx:
            kit.check_account(stranger, cfg, "author")
        self.assertIn("площадка не приняла ключ", str(ctx.exception))


class TlsKeyLogTest(unittest.TestCase):
    """Ключи TLS клиент площадки в файл из SSLKEYLOGFILE не пишет и файла
    не создаёт: по этим ключам расшифровывается запрос вместе с ключом
    площадки. urllib по умолчанию пишет их на всех Python 3.8 и новее, а
    с 3.12 создаёт файл уже при сборке клиента, в том числе для площадки
    по http. Проверка сертификата и имени узла — как у Python по
    умолчанию."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="bk-tls-test-"))
        self.addCleanup(helpers.remove_tree, self.tmp)
        # Клиент, который открыл файл (порча), держит его до сборки мусора;
        # открытый файл Windows не даст удалить.
        self.addCleanup(gc.collect)
        self.keylog = os.path.join(self.tmp, "keys.log")
        env = helpers.patch_env(SSLKEYLOGFILE=self.keylog)
        env.start()
        self.addCleanup(env.stop)

    def platform(self, tls):
        platform = fake_api.FakePlatform(tls=self.tmp if tls else None)
        self.addCleanup(platform.stop)
        platform.user(GOOD, "ai-dev")
        return platform

    def assertNoKeyLog(self, when):
        self.assertFalse(os.path.exists(self.keylog),
                         "файл SSLKEYLOGFILE есть " + when)

    def test_http(self):
        platform = self.platform(tls=False)
        client = kit.Client(platform.url, GOOD)
        self.assertNoKeyLog("после сборки клиента")
        self.assertEqual(client.get("user")["username"], "ai-dev")
        self.assertNoKeyLog("после запроса")

    def test_https(self):
        platform = self.platform(tls=True)
        # Сертификату поддельной площадки клиент доверяет так же, как
        # любому из SSL_CERT_FILE: через пути по умолчанию OpenSSL.
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": platform.cert}):
            client = kit.Client(platform.url, GOOD)
            self.assertNoKeyLog("после сборки клиента")
            self.assertEqual(client.get("user")["username"], "ai-dev")
        self.assertEqual(platform.calls[-1][1], GOOD)
        self.assertNoKeyLog("после запроса по https")

    def test_https_certificate_checked(self):
        """Сертификат, которому нет доверия, и чужое имя узла — отказ до
        запроса: ключ на такую площадку не уходит."""
        platform = self.platform(tls=True)
        with self.assertRaises(kit.ApiError) as ctx:
            kit.Client(platform.url, GOOD).get("user")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(ctx.exception))
        other = platform.url.replace("127.0.0.1", "localhost")
        with mock.patch.dict(os.environ, {
                "SSL_CERT_FILE": platform.cert,
                "NO_PROXY": "127.0.0.0/8,localhost"}):
            with self.assertRaises(kit.ApiError) as ctx:
                kit.Client(other, GOOD).get("user")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(ctx.exception))
        self.assertEqual(platform.calls, [])
        self.assertNoKeyLog("после отказов")

    def test_context(self):
        """Клиент отдаёт urllib свой контекст TLS: файла ключей у него нет,
        а настройки проверки — те же, что у ssl.create_default_context без
        SSLKEYLOGFILE."""
        client = kit.Client("https://127.0.0.1:9", GOOD)
        handlers = [h for h in client._opener.handlers
                    if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(handlers), 1)
        own = handlers[0]._context
        self.assertIsNotNone(own, "контекст строит urllib, а не клиент")
        self.assertIsNone(own.keylog_filename)
        with mock.patch.dict(os.environ):
            del os.environ["SSLKEYLOGFILE"]
            default = ssl.create_default_context()
        for name in ("verify_mode", "check_hostname", "verify_flags",
                     "options", "minimum_version", "maximum_version",
                     "security_level"):
            if hasattr(default, name):
                with self.subTest(name):
                    self.assertEqual(getattr(own, name),
                                     getattr(default, name))
        self.assertEqual([c["name"] for c in own.get_ciphers()],
                         [c["name"] for c in default.get_ciphers()])
        self.assertEqual(own.cert_store_stats(), default.cert_store_stats())
        self.assertEqual(own.get_ca_certs(), default.get_ca_certs())
        self.assertNotEqual(own.post_handshake_auth, False)
        self.assertNoKeyLog("после сборки клиента")

    def test_alpn_as_default(self):
        """Протокол ALPN клиент предлагает тот же, что urllib этой версии
        Python со своим контекстом: на Python 3.8 и 3.9 — никакого, с 3.10 —
        «http/1.1»."""
        platform = self.platform(tls=True)
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": platform.cert}):
            kit.Client(platform.url, GOOD).get("user")
            # urllib со своим контекстом записал бы ключи TLS в файл.
            del os.environ["SSLKEYLOGFILE"]
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
            request = urllib.request.Request(
                platform.url + "/api/v4/user", headers={"PRIVATE-TOKEN": GOOD})
            with opener.open(request, timeout=30) as resp:
                self.assertEqual(resp.status, 200)
        own, default = platform.alpn
        self.assertEqual(own, default)
        self.assertNoKeyLog("после запросов")


def proxy_settings(client):
    """Прокси клиента: словарь каждого его обработчика ProxyHandler.
    Обработчик без прокси urllib в клиент не включает: прокси нет —
    список пуст."""
    return [handler.proxies for handler in client._opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)]


def proxy_env(**proxy):
    """Окружение без переменных *_proxy, к которому добавлены proxy; его
    подают с clear=True, чтобы переменные машины не просочились."""
    env = dict((k, v) for k, v in helpers.base_env().items()
               if not k.upper().endswith("_PROXY"))
    env.update(proxy)
    return env


class NoProxyTest(unittest.TestCase):

    def test_entries(self):
        cases = [
            ("198.51.100.0/24", "198.51.100.7", 80, True),
            ("10.0.0.0/8,198.51.100.0/24", "198.51.100.9", 80, True),
            ("198.51.100.0/24", "198.51.101.1", 80, False),
            ("127.0.0.1", "127.0.0.1", 80, True),
            (".example.invalid", "git.example.invalid", 80, True),
            ("*.example.invalid", "git.example.invalid", 80, True),
            ("example.invalid", "example.invalid", 80, True),
            ("example.invalid", "badexample.invalid", 80, False),
            ("*", "any.host", 80, True),
            ("other.invalid, *", "any.host", 80, True),
            ("", "127.0.0.1", 80, False),
            ("198.51.100.7:80", "198.51.100.7", 80, True),
            ("198.51.100.7:8080", "198.51.100.7", 80, False),
            ("198.51.100.7:", "198.51.100.7", 443, True),
            ("198.51.100.7:http", "198.51.100.7", 80, False),
            (".example.invalid:443", "git.example.invalid", 443, True),
            ("[2001:db8::1]:443", "2001:db8::1", 443, True),
            ("[2001:db8::1]:443", "2001:db8::1", 80, False),
            ("[2001:db8::1]", "2001:db8::1", 80, True),
            ("2001:db8::1", "2001:db8::1", 80, True),
        ]
        for value, host, port, expected in cases:
            with self.subTest(value=value, host=host, port=port):
                with mock.patch.dict(os.environ, {"NO_PROXY": value,
                                                  "no_proxy": value}):
                    self.assertEqual(kit.no_proxy_for(host, port), expected)

    def test_client_port(self):
        """Порт площадки, не названный в адресе, — 80 у http и 443 у https:
        запись «узел:порт» с ним обходит прокси, с другим портом — нет.
        urllib сам запись с портом при адресе без порта не узнаёт."""
        dead = "http://127.0.0.1:9"
        cases = [
            ("http://198.51.100.7", "198.51.100.7:80", True),
            ("https://198.51.100.7", "198.51.100.7:443", True),
            ("http://198.51.100.7:8080", "198.51.100.7:8080", True),
            ("http://198.51.100.7", "198.51.100.7:443", False),
        ]
        for url, value, bypass in cases:
            with self.subTest(url=url, value=value):
                env = proxy_env(HTTP_PROXY=dead, HTTPS_PROXY=dead,
                                NO_PROXY=value)
                with mock.patch.dict(os.environ, env, clear=True):
                    client = kit.Client(url, GOOD)
                expected = [] if bypass else [{"http": dead, "https": dead,
                                               "no": value}]
                self.assertEqual(proxy_settings(client), expected)


class SystemProxyTest(unittest.TestCase):
    """Системный прокси — в Windows настройки Интернета в реестре — клиент
    не читает ни на одной системе: прокси только из переменных окружения,
    как у glab и git. urllib по умолчанию в Windows при пустых переменных
    *_proxy берёт прокси из реестра (getproxies_registry), а обход для
    него — из исключений там же (proxy_bypass_registry). Системный прокси
    изображает вторая поддельная площадка: она записывает каждое
    обращение с ключом. Функции реестра подменены, а где их нет, как в
    Linux, — созданы, и getproxies подменён на то, как он устроен в
    Windows: проверка идёт на любой системе."""

    def setUp(self):
        self.platform = fake_api.FakePlatform()
        self.addCleanup(self.platform.stop)
        self.platform.user(GOOD, "ai-dev")
        self.system = fake_api.FakePlatform()
        self.addCleanup(self.system.stop)
        self.registry = []  # обращения к функциям реестра
        system = {"http": self.system.url, "https": self.system.url}

        def registry_proxies():
            self.registry.append("getproxies_registry")
            return dict(system)

        def registry_bypass(host):
            self.registry.append("proxy_bypass_registry")
            return False

        def windows_getproxies():
            return (urllib.request.getproxies_environment()
                    or urllib.request.getproxies_registry())

        patches = [mock.patch.dict(os.environ, proxy_env(), clear=True)]
        if not hasattr(urllib.request, "getproxies_registry"):
            patches.append(mock.patch("urllib.request.getproxies",
                                      windows_getproxies))
        patches += [
            mock.patch("urllib.request.getproxies_registry",
                       registry_proxies, create=True),
            mock.patch("urllib.request.proxy_bypass_registry",
                       registry_bypass, create=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_system_proxy_not_read(self):
        """Переменных *_proxy нет, системный прокси задан: клиент идёт к
        площадке напрямую, системного прокси не читает, и ключ к нему не
        приходит."""
        client = kit.Client(self.platform.url, GOOD)
        self.assertEqual(proxy_settings(client), [])
        self.assertEqual(client.get("user")["username"], "ai-dev")
        self.assertEqual(self.platform.calls[-1][1], GOOD)
        self.assertEqual(self.system.calls, [])
        self.assertEqual(self.registry, [])

    def test_environment_only(self):
        """Переменные заданы: прокси — из них, а обход внутри urllib
        (proxy_bypass) сверяет NO_PROXY, а не исключения системного прокси.
        Ключ уходит в прокси из переменных — здесь несуществующий, — а не
        в системный."""
        dead = "http://127.0.0.1:9"
        with mock.patch.dict(os.environ, {"HTTP_PROXY": dead,
                                          "NO_PROXY": "192.0.2.1"}):
            client = kit.Client(self.platform.url, GOOD, timeout=5)
            self.assertEqual(proxy_settings(client),
                             [{"http": dead, "no": "192.0.2.1"}])
            with self.assertRaises(kit.ApiError) as ctx:
                client.get("user")
        self.assertIn("площадка не отвечает", str(ctx.exception))
        self.assertEqual(self.platform.calls, [])
        self.assertEqual(self.system.calls, [])
        self.assertEqual(self.registry, [])


# Переменные прокси машины, которых проверка в этом процессе видеть не
# должна: строчные, как их часто задают в Linux, и all_proxy, которой
# helpers.base_env не задаёт. no_proxy не называет 127.0.0.1, прокси —
# несуществующий: оставшаяся переменная уведёт запрос к поддельной
# площадке в прокси, и проверка упадёт с «Connection refused».
MACHINE_PROXIES = {"no_proxy": "192.0.2.1",
                   "http_proxy": "http://127.0.0.1:9",
                   "https_proxy": "http://127.0.0.1:9",
                   "all_proxy": "http://127.0.0.1:9"}

# Проверки, которые выполняют средство в этом процессе с окружением
# helpers.patch_env, — все, кроме двух проверок класса TlsKeyLogTest,
# test_https и test_alpn_as_default: сертификату поддельной площадки они
# доверяют через SSL_CERT_FILE, а эту переменную OpenSSL читает из
# окружения процесса, мимо словаря на месте os.environ, и с ним они
# упали бы и при верной подмене. Новая проверка с helpers.patch_env —
# новая строка здесь.
IN_PROCESS = ["test_common.ClientTest",
              "test_common.TlsKeyLogTest.test_http",
              "test_common.TlsKeyLogTest.test_https_certificate_checked",
              "test_common.TlsKeyLogTest.test_context",
              "test_poll.InterruptTest",
              "test_poll.StateWriteTest",
              "test_owner_protect_branches.ProtectTest.test_ctrl_c",
              "test_check_merge_log.InterruptTest"]


def proxy_vars():
    """Переменные прокси в os.environ, в любом регистре."""
    return dict((k, v) for k, v in os.environ.items()
                if k.upper().endswith("_PROXY"))


class InProcessEnvTest(unittest.TestCase):
    """Проверки, которые выполняют средство в этом процессе, подменяют
    os.environ помощником helpers.patch_env, и переменные прокси машины
    не остаются ни в каком регистре. 20.10.2026 на Fedora 36 с no_proxy
    машины такие проверки упали: подмена без clear=True оставляла
    строчную no_proxy, средство брало её раньше NO_PROXY из
    helpers.base_env и шло к поддельной площадке через несуществующий
    прокси."""

    def test_machine_proxies_removed(self):
        """После подмены переменные прокси — только из base_env. В Windows
        регистр в именах не различается: no_proxy там — та же NO_PROXY, а
        all_proxy без clear=True осталась бы и там."""
        expected = dict((k, v) for k, v in helpers.base_env().items()
                        if k.upper().endswith("_PROXY"))
        with mock.patch.dict(os.environ, MACHINE_PROXIES):
            with helpers.patch_env():
                self.assertEqual(proxy_vars(), expected)
                self.assertTrue(kit.no_proxy_for("127.0.0.1", 80))

    def test_like_linux(self):
        """Окружение, где имена различают регистр, как в Linux, изображает
        словарь на месте os.environ: в нём переменные машины и рядом с
        прописными — строчные из MACHINE_PROXIES. Подмена без clear=True
        оставляет в нём no_proxy машины, и средство не обходит прокси для
        127.0.0.1; с helpers.patch_env проверки из IN_PROCESS проходят."""
        machine = dict(os.environ)
        machine.update(MACHINE_PROXIES)
        suite = unittest.defaultTestLoader.loadTestsFromNames(IN_PROCESS)
        result = unittest.TestResult()
        with mock.patch.object(os, "environ", machine):
            with mock.patch.dict(os.environ, helpers.base_env()):
                self.assertEqual(os.environ["no_proxy"], "192.0.2.1")
                self.assertFalse(kit.no_proxy_for("127.0.0.1", 80))
            suite.run(result)
        failed = ["%s: %s" % (test, text.strip().splitlines()[-1])
                  for test, text in result.failures + result.errors]
        self.maxDiff = None
        self.assertEqual(failed, [])


class GitTest(unittest.TestCase):

    def test_clone_not_replaced_by_environment(self):
        """GIT_DIR и GIT_WORK_TREE из окружения клон не подменяют: корень —
        рабочая копия, в которой лежат средства."""
        clone = helpers.Clone()
        self.addCleanup(clone.remove)
        expected = kit.clone_root()
        with mock.patch.dict(os.environ, {
                "GIT_DIR": clone.git_dir(), "GIT_WORK_TREE": clone.root}):
            self.assertEqual(kit.clone_root(), expected)
            code, out, _ = kit.git(clone.root, "rev-parse", "--git-dir")
        self.assertEqual(code, 0)
        self.assertEqual(os.path.normpath(os.path.join(clone.root,
                                                       out.strip())),
                         clone.git_dir())


class OutputTest(unittest.TestCase):

    def test_utf8_under_hostile_encoding(self):
        """Вывод — UTF-8 даже при кодировке ascii, в которой русский текст
        не выводится: helpers.Clone.run декодирует вывод строго."""
        clone = helpers.Clone()
        self.addCleanup(clone.remove)
        run = clone.run("check_repo.py", "--config-only")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(run.last, "настройка сверена: "
                         ".branch-kit/config.yaml; нарушений 0")
        probe = subprocess.run(
            [sys.executable, "-c", "print('нарушений 0')"],
            env=helpers.base_env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.assertNotEqual(probe.returncode, 0, "кодировка ascii не "
                            "враждебна: проверка ничего не доказывает")

    def test_arguments_in_russian(self):
        clone = helpers.Clone()
        self.addCleanup(clone.remove)
        run = clone.run("poll.py", "--role", "owner")
        self.assertEqual(run.code, 2, run)
        self.assertIn("ошибка: ключ --role: недопустимое значение 'owner'",
                      run.err)
        run = clone.run("check_docs.py", "--help")
        self.assertEqual(run.code, 0, run)
        self.assertTrue(run.out.startswith("вызов: check_docs.py"), run)

    def test_argument_error_as_before_311(self):
        """Ошибка в ключе по-русски и там, где argparse собирает строку
        «argument <ключ>: …» без перевода, как на Python 3.8–3.10: на время
        проверки ArgumentError.__str__ — как в тех версиях. Со своим
        __str__ этого Python — то же самое, без двойного перевода."""
        variants = (("argparse 3.8–3.10", lambda: mock.patch.object(
                        argparse.ArgumentError, "__str__", _str_before_311)),
                    ("argparse этого Python", contextlib.nullcontext))
        cases = ((["--role", "owner"],
                  "poll.py: ошибка: ключ --role: недопустимое значение "
                  "'owner'"),
                 (["--mr", "x"],
                  "poll.py: ошибка: ключ --mr: недопустимое значение 'x'"))
        for name, patch in variants:
            for argv, expected in cases:
                with self.subTest(name, argv=argv):
                    err = io.StringIO()
                    with patch(), contextlib.redirect_stderr(err):
                        parser = kit.make_parser("poll.py", "опрос")
                        parser.add_argument("--role",
                                            choices=("planner", "author"))
                        parser.add_argument("--mr", type=int)
                        with self.assertRaises(SystemExit) as stop:
                            parser.parse_args(argv)
                    self.assertEqual(stop.exception.code, 2)
                    self.assertIn(expected, err.getvalue())
                    self.assertNotIn("argument", err.getvalue())


def _str_before_311(self):
    """ArgumentError.__str__ из argparse Python 3.8–3.10: строка вокруг
    причины — без перевода _(), в отличие от 3.11 и новее."""
    if self.argument_name is None:
        text = "%(message)s"
    else:
        text = "argument %(argument_name)s: %(message)s"
    return text % dict(message=self.message,
                       argument_name=self.argument_name)


class ExceptionsFileTest(unittest.TestCase):
    """Строение файла исключений; применение — в test_check_docs."""

    def load(self, text):
        clone = helpers.Clone(exceptions=text)
        self.addCleanup(clone.remove)
        return kit.load_exceptions(clone.exceptions_path, "check_docs.py",
                                   ("длина строки",), "exceptions.yaml")

    def test_kit_file_is_empty_list(self):
        exemptions, problems = kit.load_exceptions(
            os.path.join(helpers.KIT_DIR, "exceptions.yaml"),
            "check_docs.py", ("длина строки",), "exceptions.yaml")
        self.assertEqual((exemptions, problems), ([], []))

    def test_problems(self):
        entry = ("  - средство: check_docs.py\n    место: a.md\n"
                 "    вид: длина строки\n")
        cases = [
            ("исключения:\n" + entry, "нет причины"),
            ("исключения:\n" + entry + "    причина: x\n    файл: a\n",
             "неизвестное поле «файл»"),
            ("исключения:\n  - средство: check_docs.py\n    вид: длина "
             "строки\n    причина: x\n", "нет поля «место»"),
            ("исключения:\n  - средство: lint.py\n    место: a.md\n"
             "    вид: x\n    причина: x\n", "неизвестное средство"),
            ("исключения:\n  - средство: check_docs.py\n    место: a.md\n"
             "    вид: слово\n    причина: x\n", "неизвестный вид «слово»"),
            ("исключения:\n  - просто строка\n", "запись не разбирается"),
            ("исключения: [\n", "файл не разбирается"),
            ("исключения: []\nдата: 2026-02-30\n", "файл не разбирается"),
            ("исключение: []\n", "нет списка «исключения»"),
        ]
        for text, expected in cases:
            with self.subTest(expected):
                exemptions, problems = self.load(text)
                self.assertEqual(exemptions, [])
                self.assertTrue(any(expected in p.text for p in problems),
                                [str(p) for p in problems])
                self.assertTrue(all(p.kind == "исключение"
                                    for p in problems))

    def test_unknown_top_field_line(self):
        """Неизвестное поле верхнего уровня — строкой его ключа: под
        записями его иначе искали бы в первой строке."""
        exemptions, problems = self.load(
            "исключения:\n  - средство: check_docs.py\n    место: a.md\n"
            "    вид: длина строки\n    причина: x\n\nисключение: []\n")
        self.assertEqual(len(exemptions), 1)
        self.assertEqual([str(p) for p in problems],
                         ["exceptions.yaml:7: исключение: неизвестное поле "
                          "«исключение»"])

    def test_other_tool_entries_skipped(self):
        """Записи другого средства этим средством не применяются."""
        exemptions, problems = self.load(
            "исключения:\n  - средство: check_merge_log.py\n"
            "    место: \"!5\"\n    вид: нет прямого следа\n"
            "    причина: x\n")
        self.assertEqual((exemptions, problems), ([], []))


if __name__ == "__main__":
    unittest.main()
