"""Журнал слияний: записанные ответы merge_log_answers.json с одним
нарушением каждого вида, кроме склейки — её проверка ставит полем
слитого запроса, — таблица записей ссылок на запрос и разобранные
исключения. Средство запускается отдельным процессом против поддельной
площадки; к настоящей площадке проверки не обращаются."""

import contextlib
import copy
import io
import json
import os
import re
import unittest
from unittest import mock

import helpers
import fake_api
import check_merge_log as cml

with open(os.path.join(helpers.TESTS_DIR, "merge_log_answers.json"),
          encoding="utf-8") as _f:
    HISTORY = json.load(_f)

TOKEN = helpers.TOKENS["ai-reviewer"]
NOT_ON_LINE = "нет на первой линии"
LINE = re.compile(r"^(!\d+|[0-9a-f]{8}): (.+?): (.+)$")
EXPECTED = {
    cml.NOT_MERGE: ["33333333"],
    cml.SELF_MERGE: ["!3"],
    cml.NO_DECISION: ["!7"],
    cml.NO_OWNER_APPROVAL: ["!4"],
    cml.NO_TRACE: ["!5"],
    cml.REUSED_BRANCH: ["!6"],
    NOT_ON_LINE: ["!11"],
}


def load_history(platform, history=HISTORY, project=None):
    """Журнал основной ветки и слитые запросы на поддельной площадке."""
    repo = platform.repo(project or platform.project)
    repo.commits = copy.deepcopy(history["commits"])
    repo.branches["main"] = history["head"]
    for mr in history["merge_requests"]:
        merger = fake_api.person(mr["merged_by"])
        platform.add_request(
            mr["iid"], author=mr["author"], branch=mr["source_branch"],
            state="merged", project=repo.path,
            merge_commit_sha=mr["merge_commit_sha"],
            target_branch=mr["target_branch"], merged_at=mr["merged_at"],
            description=mr["description"], merged_by=merger,
            merge_user=merger)
    for iid, names in history["approvals"].items():
        for name in names:
            platform.approve(int(iid), name, project=repo.path)
    for iid, notes in history["notes"].items():
        for note in notes:
            platform.add_note(int(iid), note["author"], note["body"],
                              system=note["system"], project=repo.path)


def merged(platform, iid, description, branch=None, project=None,
           merger="ai-reviewer"):
    person = fake_api.person(merger)
    return platform.add_request(
        iid, author="ai-dev", branch=branch or "docs/x%d" % iid,
        state="merged", project=project, merge_commit_sha="%040d" % iid,
        target_branch="main", merged_at="2026-10-05T00:00:%02d.000Z"
        % (iid % 60), description=description, merged_by=person,
        merge_user=person)


class MergeLogBase(unittest.TestCase):

    def setUp(self):
        self.platform = fake_api.FakePlatform(
            web_host="gitlab.example.invalid")
        self.addCleanup(self.platform.stop)
        self.platform.user(TOKEN, "ai-reviewer")
        self.cfg = helpers.config()
        self.cfg["площадка"]["адрес"] = self.platform.url

    def run_tool(self, exceptions=None, token=TOKEN, cfg=None):
        clone = helpers.Clone(cfg or self.cfg, exceptions=exceptions)
        self.addCleanup(clone.remove)
        run = clone.run("check_merge_log.py", token=token)
        for value in helpers.TOKENS.values():
            self.assertNotIn(value, run.out + run.err)
        return run

    @staticmethod
    def violations(run):
        found = {}
        for line in run.lines:
            m = LINE.match(line)
            if m:
                found.setdefault(m.group(2), []).append(m.group(1))
        return found


class HistoryTest(MergeLogBase):

    def setUp(self):
        MergeLogBase.setUp(self)
        load_history(self.platform)

    def test_each_kind_once(self):
        """По одному нарушению каждого вида — каждое поймано и названо
        местом; чистые запросы, начальное изменение, изменение не на
        первой линии и запрос не в основную ветку нарушений не дают."""
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertEqual(self.violations(run), EXPECTED)
        self.assertEqual(run.last, "нарушений 7")
        self.assertEqual(run.lines[0], "основная ветка main репозитория "
                         "example/app: изменений первой линии 11, слитых "
                         "запросов 10; владелец — owner")
        self.assertIn("33333333: не слиянием: изменение «правка прямо в "
                      "основную ветку» пришло в main не слиянием запроса",
                      run.lines)
        self.assertIn("!3: сливший автор: слил сам автор запроса ai-dev",
                      run.lines)

    def test_only_reads(self):
        """Средство только читает: ни одного запроса POST или PUT; журнал
        читается по первой линии основной ветки и слитым в неё
        запросам."""
        self.run_tool()
        self.assertEqual(self.platform.written(), [])
        commits = [p for p in self.platform.paths("/repository/commits")]
        self.assertTrue(commits)
        self.assertTrue(all("first_parent=true" in p and "ref_name=main" in p
                            for p in commits), commits)
        listing = self.platform.paths("/merge_requests")
        self.assertTrue(any("state=merged" in p and "target_branch=main" in p
                            for p in listing), listing)

    def test_exception_covers(self):
        """Исключение снимает своё нарушение и только его; место изменения
        — первые 8 знаков или длиннее."""
        run = self.run_tool(
            "исключения:\n"
            "  - средство: check_merge_log.py\n    место: \"!6\"\n"
            "    вид: повторная ветка\n    причина: разобрано\n"
            "  - средство: check_merge_log.py\n    место: \"3333333333\"\n"
            "    вид: не слиянием\n    причина: разобрано\n")
        self.assertEqual(run.code, 1, run)
        expected = dict(EXPECTED)
        del expected[cml.REUSED_BRANCH]
        del expected[cml.NOT_MERGE]
        self.assertEqual(self.violations(run), expected)
        self.assertEqual(run.last, "нарушений 5")

    def test_all_covered(self):
        """Все нарушения разобраны исключениями — код 0."""
        text = "исключения:\n"
        for kind, places in EXPECTED.items():
            text += ("  - средство: check_merge_log.py\n    место: \"%s\"\n"
                     "    вид: %s\n    причина: разобрано\n"
                     % (places[0], kind))
        run = self.run_tool(text)
        self.assertEqual(run.code, 0, run)
        self.assertEqual(run.last, "нарушений 0")

    def test_exception_problems(self):
        """Исключение без причины, с чужим местом, с абзацем или ничего не
        покрывшее — нарушение «исключение», и оно ничего не снимает;
        запись другого средства не применяется."""
        entry = ("  - средство: check_merge_log.py\n    место: \"%s\"\n"
                 "    вид: %s\n")
        cases = [
            (entry % ("!6", "повторная ветка"), "нет причины"),
            (entry % ("docs/a.md", "повторная ветка") + "    причина: x\n",
             "место у check_merge_log.py — «!<номер>» запроса"),
            (entry % ("!6", "повторная ветка") + "    абзац: слова\n"
             "    причина: x\n", "поле «абзац» у check_merge_log.py"),
            (entry % ("!2", "сливший автор") + "    причина: устарело\n",
             "ничего не покрыло"),
            (entry % ("!6", "длина строки") + "    причина: x\n",
             "неизвестный вид «длина строки»"),
        ]
        for text, expected in cases:
            with self.subTest(expected):
                run = self.run_tool("исключения:\n" + text)
                self.assertEqual(run.code, 1, run)
                self.assertTrue(any(": исключение: " in line and expected
                                    in line for line in run.lines), run)
                self.assertEqual(self.violations(run), EXPECTED)
                self.assertEqual(run.last, "нарушений 8")
        run = self.run_tool(
            "исключения:\n  - средство: check_docs.py\n    место: a.md\n"
            "    вид: длина строки\n    причина: x\n")
        self.assertEqual(run.last, "нарушений 7", run)

    def test_squash(self):
        """Слияние со склейкой (AGENTS.md, 2.4) — нарушение «склейка» по
        изменению склейки или по флагу squash без него; флаг выключен и
        изменения склейки нет — не нарушение."""
        self.platform.requests[2]["squash_commit_sha"] = "a" * 40
        self.platform.requests[8]["squash"] = True
        self.assertIsNone(self.platform.requests[8]["squash_commit_sha"])
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        expected = dict(EXPECTED)
        expected[cml.SQUASH] = ["!2", "!8"]
        self.assertEqual(self.violations(run), expected)
        self.assertIn("!2: склейка: запрос слит со склейкой (squash), "
                      "изменение склейки aaaaaaaa; сливают способом merge "
                      "без склейки (AGENTS.md, 2.4)", run.lines)
        self.assertEqual(run.last, "нарушений 9")

    def test_line_not_complete(self):
        """Первая линия не получена целиком — код 2, а не молчаливо
        непроверенная часть истории."""
        repo = self.platform.repo(self.platform.project)
        repo.commits = [c for c in repo.commits
                        if not c["id"].startswith("3333")]
        run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertIn("первая линия основной ветки не получена целиком: "
                      "изменения 33333333 нет", run.err)


class CleanTest(MergeLogBase):

    def test_comment_opener_in_inline_code(self):
        """Начало комментария в обратных кавычках выше раздела и конец
        комментария в подвале шаблона: раздел цел, ложного «нет варианта
        решений» нет."""
        repo = self.platform.repo(self.platform.project)
        repo.commits = [
            {"id": "1" * 40, "parent_ids": ["0" * 40, "a" * 40],
             "title": "Merge"},
            {"id": "0" * 40, "parent_ids": [], "title": "Initial commit"}]
        repo.branches["main"] = "1" * 40
        merged(self.platform, 1,
               "## Что и зачем\n\nПишут `<!--` в тексте.\n\n## Решения\n\n"
               "- решений не несёт\n\n---\n\n<!--\nподвал шаблона\n-->\n")
        self.platform.requests[1]["merge_commit_sha"] = "1" * 40
        run = self.run_tool()
        self.assertEqual(run.code, 0, run)
        self.assertEqual(run.last, "нарушений 0", run)

    def test_new_decision_quoted_template(self):
        """Запрос цитирует новый вид шаблона блоком кода, ниже — настоящий
        раздел «несёт новое решение» без утверждения владельца: нарушение,
        а не «нарушений 0». Опорный — цитата-заголовок в «>» даёт то же."""
        repo = self.platform.repo(self.platform.project)
        repo.commits = [
            {"id": "1" * 40, "parent_ids": ["0" * 40, "a" * 40],
             "title": "Merge"},
            {"id": "0" * 40, "parent_ids": [], "title": "Initial commit"}]
        repo.branches["main"] = "1" * 40
        real = "## Решения\n\n- несёт новое решение: x — нужно утверждение\n"
        for name, quote in (
                ("блок кода",
                 "```\n## Решения\n\n- решений не несёт\n```\n"),
                ("цитата", "> ## Решения\n>\n> - решений не несёт\n")):
            with self.subTest(name):
                merged(self.platform, 1, "## Что изменено\n\n%s\n%s"
                       % (quote, real))
                self.platform.requests[1]["merge_commit_sha"] = "1" * 40
                run = self.run_tool()
                self.assertEqual(run.code, 1, run)
                self.assertEqual(self.violations(run),
                                 {cml.NO_OWNER_APPROVAL: ["!1"]})

    def test_clean_history(self):
        """Чистый журнал — «нарушений 0», код 0."""
        repo = self.platform.repo(self.platform.project)
        repo.commits = [
            {"id": "2" * 40, "parent_ids": ["1" * 40, "b" * 40],
             "title": "Merge"},
            {"id": "1" * 40, "parent_ids": ["0" * 40, "a" * 40],
             "title": "Merge"},
            {"id": "0" * 40, "parent_ids": [], "title": "Initial commit"}]
        repo.branches["main"] = "2" * 40
        merged(self.platform, 1, "## Решения\n\n- решений не несёт\n")
        self.platform.requests[1]["merge_commit_sha"] = "1" * 40
        merged(self.platform, 2, "## Решения\n\n- несёт новое решение: x — "
                                 "нужно утверждение владельца\n")
        self.platform.requests[2]["merge_commit_sha"] = "2" * 40
        self.platform.approve(2, "owner")
        run = self.run_tool()
        self.assertEqual(run.code, 0, run)
        self.assertEqual(run.last, "нарушений 0")

    def test_not_done(self):
        """Код 2: нет ключа, ключ не принят, сбой площадки, переадресация,
        нет основной ветки, настройка не сверилась — причина в потоке
        ошибок."""
        load_history(self.platform)
        bad = helpers.config()
        bad["площадка"]["адрес"] = self.platform.url
        bad["основная ветка"] = 5
        cases = [
            (dict(token=""), "нет ключа"),
            (dict(token=helpers.TOKENS["stranger"]),
             "площадка не приняла ключ"),
            (dict(cfg=bad), "поле «основная ветка»"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected):
                calls = len(self.platform.calls)
                run = self.run_tool(**kwargs)
                self.assertEqual(run.code, 2, run)
                self.assertIn(expected, run.err)
                if "cfg" in kwargs or not kwargs["token"]:
                    self.assertEqual(len(self.platform.calls), calls)
        self.platform.failures = 100
        run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertIn("журнал слияний не прочитан: GET", run.err)
        self.platform.failures = 0
        self.platform.redirect = "http://localhost:9/api/v4/user"
        run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertIn("переадресацией 302", run.err)
        self.platform.redirect = None
        del self.platform.repo(self.platform.project).branches["main"]
        run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertIn("404 Branch Not Found", run.err)

    def test_bad_web_url(self):
        """Адрес запроса в ответе площадки (web_url), узел которого urlsplit
        не разбирает, — код 2 с причиной, а не трассировка: по узлам этих
        адресов узнаются ссылки на запросы, и без узла проверка неполна."""
        load_history(self.platform)
        self.platform.requests[2]["web_url"] = (
            "http://[::1/example/app/-/merge_requests/2")
        run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertEqual(run.out, "")
        self.assertIn("журнал слияний не проверен: площадка отдала у запроса "
                      "!2 адрес «http://[::1/example/app/-/merge_requests/2», "
                      "узел которого не разбирается", run.err)
        self.assertNotIn("Traceback", run.err)


class RewrittenLineTest(MergeLogBase):
    """Перезапись истории основной ветки: слитый запрос в неё, чьего
    merge_commit_sha нет на первой линии, — нарушение (AGENTS.md, 3.4). Поле
    parent_ids обязано быть списком: без него конец линии неотличим от
    начального изменения."""

    def history(self):
        """Чистая история: начальное изменение, !1 и !2."""
        repo = self.platform.repo(self.platform.project)
        repo.commits = [
            {"id": "2" * 40, "parent_ids": ["1" * 40, "b" * 40],
             "title": "Merge"},
            {"id": "1" * 40, "parent_ids": ["0" * 40, "a" * 40],
             "title": "Merge"},
            {"id": "0" * 40, "parent_ids": [], "title": "Initial commit"}]
        repo.branches["main"] = "2" * 40
        for iid in (1, 2):
            merged(self.platform, iid, "## Решения\n\n- решений не несёт\n")
            self.platform.requests[iid]["merge_commit_sha"] = str(iid) * 40
        return repo

    def test_clean(self):
        """Опорный: чистая история, начальное изменение без родителей — не
        нарушение и не конец проверки."""
        self.history()
        run = self.run_tool()
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.violations(run), {})
        self.assertEqual(run.last, "нарушений 0")

    def test_replaced(self):
        """Основную ветку заменили одним изменением без родителей: ни одного
        из слияний на линии нет."""
        repo = self.history()
        repo.commits = [{"id": "e" * 40, "parent_ids": [], "title": "Замена"}]
        repo.branches["main"] = "e" * 40
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertEqual(self.violations(run), {NOT_ON_LINE: ["!1", "!2"]})
        self.assertIn("!2: нет на первой линии: изменения слияния 22222222 "
                      "нет на первой линии main: историю основной ветки "
                      "перезаписали (AGENTS.md, 3.4)", run.lines)
        self.assertEqual(run.last, "нарушений 2")

    def test_rolled_back(self):
        """Основную ветку откатили: слияния !2 на линии больше нет."""
        repo = self.history()
        repo.commits = repo.commits[1:]
        repo.branches["main"] = "1" * 40
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertEqual(self.violations(run), {NOT_ON_LINE: ["!2"]})

    def test_exception_covers(self):
        """Разобранное нарушение снимают исключением: историю назад не
        переписывают."""
        repo = self.history()
        repo.commits = repo.commits[1:]
        repo.branches["main"] = "1" * 40
        run = self.run_tool(
            "исключения:\n  - средство: check_merge_log.py\n    место: \"!2\""
            "\n    вид: нет на первой линии\n    причина: разобрано\n")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(run.last, "нарушений 0")

    def test_empty_merge_sha(self):
        """Пустой merge_commit_sha новым нарушением не считается: его слияние
        на первой линии остаётся нарушением «не слиянием», как было."""
        for value in (None, ""):
            with self.subTest(value):
                self.history()
                self.platform.requests[2]["merge_commit_sha"] = value
                run = self.run_tool()
                self.assertEqual(self.violations(run),
                                 {cml.NOT_MERGE: ["22222222"]})

    def test_bad_parents(self):
        """Поля parent_ids нет или оно не список — код 2: иначе линия молча
        кончается на этом изменении."""
        for name, change in (("нет поля", lambda c: c.pop("parent_ids")),
                             ("строка", lambda c: c.update(parent_ids="x")),
                             ("None", lambda c: c.update(parent_ids=None))):
            with self.subTest(name):
                repo = self.history()
                change(repo.commits[0])
                run = self.run_tool()
                self.assertEqual(run.code, 2, run)
                self.assertIn("22222222", run.err)
                self.assertIn("parent_ids", run.err)
                self.assertEqual(run.out, "")


class CrashTest(MergeLogBase):
    """Падение на негодном входе — код 2, а не код 1 «есть нарушения»; вид и
    текст в потоке ошибок, трассировки нет."""

    def test_wrong_answer(self):
        """Ответ площадки 200 не того вида: список вместо записи ветки."""
        with mock.patch.object(self.platform, "route",
                               return_value=(200, ["не запись"], {})):
            run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertIn("AttributeError", run.err)
        self.assertNotIn("Traceback", run.err)
        self.assertEqual(run.out, "")

    def test_address_not_ascii(self):
        """Адрес площадки с буквами вне ASCII в пути настройка пропускает."""
        self.cfg["площадка"]["адрес"] = self.platform.url + "/путь"
        run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertIn("UnicodeEncodeError", run.err)
        self.assertNotIn("Traceback", run.err)

    def test_key_hidden(self):
        """Ключ, попавший в текст исключения, в вывод не идёт. В том же
        процессе: исключение вставляется в ход средства."""
        clone = helpers.Clone(self.cfg)
        self.addCleanup(clone.remove)
        err = io.StringIO()
        with helpers.patch_env(GITLAB_TOKEN=TOKEN), \
                mock.patch.object(cml, "run", side_effect=RuntimeError(
                    "сбой, ключ %s" % TOKEN)), \
                contextlib.redirect_stderr(err):
            code = cml.main(["--config", clone.config_path])
        self.assertEqual(code, 2)
        self.assertIn("RuntimeError: сбой, ключ", err.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class InterruptTest(MergeLogBase):

    def test_ctrl_c(self):
        """Ctrl+C — код 130, итога нет. Проверяется в том же процессе:
        отдельному процессу его надёжно не послать в обеих системах."""
        clone = helpers.Clone(self.cfg)
        self.addCleanup(clone.remove)
        out, err = io.StringIO(), io.StringIO()
        with helpers.patch_env(GITLAB_TOKEN=TOKEN), \
                mock.patch.object(cml.MergeLog, "check",
                                  side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cml.main(["--config", clone.config_path])
        self.assertEqual(code, 130)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("проверка прервана", err.getvalue())


class TraceTableTest(MergeLogBase):
    """Ссылка на запрос — след, только если запись узнана однозначно:
    репозиторий и номер. Неузнанная или неоднозначная запись даёт «нет
    прямого следа».

    Ловушка — номер 8: у своего example/app!8 есть заметка владельца, а у
    example/other запроса !8 нет. Запись, которая по ошибке отдала бы
    номер своему репозиторию или чужой площадке, засчитала бы ложный
    след."""

    # Запись — засчитан ли след.
    CASES = (
        # верно узнанные ссылки на запрос со следом владельца
        ("утверждение запроса example/other !1", True),
        ("example/other!1", True),
        ("example/other !1.", True),
        ("утверждения запросов example/other !2, !3", True),
        ("example/other\n  !2 и !3", True),
        ("утверждение `example/other` !1", True),
        ("утверждение «example/other»: !3", True),
        ("утверждение „example/other“ !1", True),
        ("утверждение “example/other” !1", True),
        ("утверждение ‘example/other’ !1", True),
        ("утверждение (`example/other`) !1", True),
        ("утверждение `example/other!1`", True),
        ("http://gitlab.example.invalid/example/other/-/merge_requests/1",
         True),
        ("https://127.0.0.1:8443/example/other/-/merge_requests/1#note_5",
         True),
        ("gitlab.example.invalid/example/other/-/merge_requests/1", True),
        ("/example/other/-/merge_requests/1", True),
        ("[утверждение](http://gitlab.example.invalid/example/other/-/"
         "merge_requests/1)", True),
        # номер без имени — запрос этого репозитория
        ("утверждение !8 (AGENTS.md, 2.5)", True),
        ("утверждение `!8`", True),
        ("example/app!8", True),
        # след не того человека и несуществующий запрос
        ("утверждение запроса example/other !2", False),
        ("утверждение запроса example/other !99", False),
        ("http://127.0.0.1/example/other/-/merge_requests/2", False),
        # номер 8 у названного репозитория, а не у своего
        ("утверждение `example/other` !8", False),
        ("утверждение «example/other» !8", False),
        ("утверждение example/other: !8", False),
        ("утверждение „example/other“ !8", False),
        ("утверждение ‘example/other’ !8", False),
        ("example/other!8", False),
        # номер без имени, когда раздел называет другой репозиторий
        ("утверждение example/other !2; !8", False),
        ("утверждение example/other !2, и !8", False),
        ("см. example/other; след — !8", False),
        # ссылка Markdown: засчитывается адрес, а не текст
        ("[!8](http://gitlab.example.invalid/example/other/-/"
         "merge_requests/8)", False),
        ("[!8](http://unknown.example.invalid/x/y/-/merge_requests/8)",
         False),
        # другая площадка, репозиторий вне группы, номер после пути
        ("http://unknown.example.invalid/example/app/-/merge_requests/8",
         False),
        ("unknown.example.invalid/example/app/-/merge_requests/8", False),
        ("утверждение outside/repo !1", False),
        ("http://127.0.0.1/outside/repo/-/merge_requests/1", False),
        ("/outside/repo/-/merge_requests/1", False),
        ("http://unknown.example.invalid/other/x !8", False),
        ("см. tools/README.md !8", False),
        ("утверждение !8x", False),
        # узел, который не разбирается: не ссылка, средство идёт дальше
        ("см. http://[::1/example/other/-/merge_requests/1", False),
        ("http://[abc]/example/other/-/merge_requests/1", False),
        # блок кода — пример, а не ссылка
        ("пример:\n\n```\nglab mr view !8\n```\n", False),
        ("пример:\n\n~~~\n!8\n~~~\n", False),
    )
    FIRST = 100  # номера проверочных запросов в example/app

    def test_no_false_trace(self):
        """Каждая запись из CASES: верная — засчитана, неузнанная или
        неоднозначная — «нет прямого следа»."""
        load_history(self.platform)
        for n, (text, _) in enumerate(self.CASES):
            merged(self.platform, self.FIRST + n,
                   "## Решения\n\n- исполняет решение владельца от "
                   "01.10.2026: след — %s\n" % text)
        other, outside = "example/other", "outside/repo"
        for iid in (1, 2, 3):
            merged(self.platform, iid, "", project=other)
        self.platform.approve(1, "owner", project=other)
        self.platform.approve(2, "ai-reviewer", project=other)
        # владелец другого репозитория — не владелец этого: не след
        self.platform.add_note(2, "other-owner", "решение", project=other)
        self.platform.add_note(2, "owner", "mentioned", system=True,
                               project=other)
        self.platform.add_note(3, "owner", "решение", project=other)
        # вне группы: след есть, но ссылка не засчитывается
        merged(self.platform, 1, "", project=outside)
        self.platform.approve(1, "owner", project=outside)
        run = self.run_tool()
        untraced = set(int(where[1:]) for where in
                       self.violations(run).get(cml.NO_TRACE, []))
        wrong = ["%r: ждали %s" % (text, "след" if traced else "нет следа")
                 for n, (text, traced) in enumerate(self.CASES)
                 if ((self.FIRST + n) in untraced) == traced]
        self.assertEqual(wrong, [])
        self.assertEqual(untraced - set(range(self.FIRST, self.FIRST + len(
            self.CASES))), {5})


class ParseTest(unittest.TestCase):
    """Разбор описания без площадки."""

    def test_references(self):
        refs = cml.referenced_mrs
        hosts = {"127.0.0.1", "gitlab.example.invalid"}
        self.assertEqual(
            refs("\n- исполняет решение владельца: приняты его\n  "
                 "утверждением запросов example/other !11, !14, !17; "
                 "строка —\n  утверждением !18 (AGENTS.md, 2.5)\n\n",
                 "example/app", "example", hosts),
            [("example/other", 11), ("example/other", 14),
             ("example/other", 17)])
        self.assertEqual(refs("!5", "example/app", "example"),
                         [("example/app", 5)])
        self.assertEqual(refs("Example/App !5", "example/app", "example"),
                         [("example/app", 5)])
        self.assertEqual(refs("example/sub/a/-/merge_requests/7",
                              "example/app", "example"),
                         [("example/sub/a", 7)])
        # без известных узлов адрес с узлом не засчитывается
        self.assertEqual(refs("http://gitlab.example.invalid/example/a/-/"
                              "merge_requests/7", "example/app", "example"),
                         [])
        self.assertEqual(refs("номеров нет", "example/app", "example"), [])

    def test_bad_host(self):
        """Адрес с узлом, который urlsplit не разбирает (ValueError), — не
        ссылка: разбор идёт дальше, а не падает трассировкой."""
        hosts = {"127.0.0.1", "gitlab.example.invalid"}
        for text in ("см. http://[::1/example/app/-/merge_requests/5",
                     "http://[abc]/example/app/-/merge_requests/5",
                     "//[::1/example/app/-/merge_requests/5"):
            with self.subTest(text):
                self.assertEqual(cml.referenced_mrs(
                    text + "; утверждение example/other !11",
                    "example/app", "example", hosts),
                    [("example/other", 11)])

    def test_variants(self):
        """Неочищенный шаблон с тремя вариантами — три варианта; варианты
        в комментарии HTML и в следующем разделе не считаются."""
        section = cml.decisions_section(
            "## Решения\n\n- исполняет решение владельца от <дата>: <суть>\n"
            "- решений не несёт\n- несёт новое решение: <суть> — нужно "
            "утверждение владельца\n\n## Чем проверено\n")
        self.assertEqual(len(cml.decision_variants(section)), 3)
        section = cml.decisions_section(
            "## Решения\r\n<!-- - решений не несёт -->\r\n- несёт новое "
            "решение: x\r\n## Чем проверено\r\n- решений не несёт\r\n")
        self.assertEqual(cml.decision_variants(section),
                         [cml.NEW_DECISION])
        self.assertIsNone(cml.decisions_section("## Что и зачем\n"))
        self.assertEqual(cml.decision_variants(
            cml.decisions_section("### Решения\n\n- Решений не несет\n")),
            [cml.NO_DECISIONS])

    def test_heading_in_code_block(self):
        """Заголовок «Решения» в блоке кода выше настоящего раздела раздел не
        подменяет: ни с ``` , ни с ~~~, ни с длинным забором."""
        real = "## Решения\n\n- несёт новое решение: x\n\n## Чем проверено\n"
        quoted = "## Решения\n\n- решений не несёт\n"
        for fence in ("`" * 3, "~" * 3, "`" * 4):
            with self.subTest(fence):
                text = "## Что изменено\n\n%s\n%s%s\n\n%s" % (
                    fence, quoted, fence, real)
                self.assertEqual(
                    cml.decision_variants(cml.decisions_section(text)),
                    [cml.NEW_DECISION])

    def test_heading_in_quote(self):
        """Опорный: заголовок в цитате заголовком не считается, настоящий
        раздел — ниже неё."""
        text = ("## Что изменено\n\n> ## Решения\n>\n> - решений не несёт\n\n"
                "## Решения\n\n- несёт новое решение: x\n")
        self.assertEqual(cml.decision_variants(cml.decisions_section(text)),
                         [cml.NEW_DECISION])

    def test_comment_opener_in_code_block(self):
        """Начало комментария HTML в блоке кода — цитата: комментарий не
        тянется от него до чужого «-->» и настоящий раздел не съедает."""
        text = ("```\n<!--\n```\n\n## Решения\n\n<!-- подсказка шаблона -->\n"
                "- несёт новое решение: x\n")
        self.assertEqual(cml.decision_variants(cml.decisions_section(text)),
                         [cml.NEW_DECISION])

    def test_comment_opener_in_inline_code(self):
        """Начало комментария в строчном коде комментарий не открывает: конец
        комментария в подвале шаблона раздел не съедает. Длина кавычек — как
        в CommonMark: закрывает запись той же длины."""
        footer = "\n\n<!--\nподвал шаблона\n-->\n"
        real = "## Решения\n\n- несёт новое решение: x\n"
        for name, code in (("одна кавычка", "`<!--`"),
                           ("две кавычки", "``<!--``"),
                           ("кавычка внутри двух", "`` ` <!-- ``"),
                           ("вместе с концом", "`<!--` и `-->`"),
                           ("с текстом", "`a <!-- b`")):
            with self.subTest(name):
                text = "## Что и зачем\n\nПишут %s в тексте.\n\n%s%s" % (
                    code, real, footer)
                self.assertEqual(
                    cml.decision_variants(cml.decisions_section(text)),
                    [cml.NEW_DECISION])
        text = "## Решения\n\nПишут `<!--`.\n\n- несёт новое решение: x%s" % (
            footer)
        self.assertEqual(cml.decision_variants(cml.decisions_section(text)),
                         [cml.NEW_DECISION], "начало — в самом разделе")

    def test_inline_code_kept(self):
        """Опорный: строчный код в разделе остаётся, скрыт лишь комментарий."""
        self.assertEqual(
            cml.decisions_section(
                "## Решения\n\n- несёт новое решение: `x`<!-- y -->\n"),
            "\n\n- несёт новое решение: `x`\n")

    def test_comment_hidden(self):
        """Опорный: настоящий комментарий скрыт, и с заголовком «Решения»
        внутри. Строчный код перед ним, кавычка без пары и код через границу
        строки его не открывают: иначе скрытый вариант стал бы виден."""
        hidden = "<!--\n## Решения\n\n- решений не несёт\n-->\n"
        for name, before in (("просто", ""),
                             ("после строчного кода", "Пишут `<!--`. "),
                             ("кавычка без пары", "Одна ` кавычка. "),
                             ("разная длина", "`` ` <!-- `\n"),
                             ("код через границу строки", "`начало\n"),
                             ("с кавычкой внутри", "<!-- ` -->\n")):
            with self.subTest(name):
                self.assertIsNone(cml.decisions_section(
                    before + hidden + "конец`\n"))
        self.assertIsNone(cml.decisions_section(
            "\\`<!-- `\n## Решения\n\n- решений не несёт\n-->\n"),
            "кавычка после обратной косой код не открывает")
        real = "## Решения\n\n- несёт новое решение: x\n"
        for name, before in (("комментарий с заголовком", hidden),
                             ("с кодом перед ним", "Пишут `<!--`. " + hidden),
                             ("с кавычкой в комментарии",
                              "<!-- ` -->\n" + hidden)):
            with self.subTest(name):
                self.assertEqual(cml.decision_variants(cml.decisions_section(
                    before + real + "\nконец ` кавычки\n")),
                    [cml.NEW_DECISION])

    def test_only_heading_in_code_block(self):
        """Раздел только в блоке кода — раздела нет."""
        self.assertIsNone(cml.decisions_section(
            "```\n## Решения\n\n- решений не несёт\n```\n"))

    def test_variant_in_code_block(self):
        """Вариант в блоке кода внутри раздела вариантом не считается."""
        section = cml.decisions_section(
            "## Решения\n\n- несёт новое решение: x\n\n```\n- решений не "
            "несёт\n```\n")
        self.assertEqual(cml.decision_variants(section), [cml.NEW_DECISION])

    def test_fence_pairing(self):
        """Пары заборов — по CommonMark, как у check_docs.py: строка с
        обратными кавычками в сведениях блока не открывает, строка со
        сведениями его не закрывает. Иначе пара сбилась бы, настоящий раздел
        ушёл бы в «блок», а цитата стала бы разделом."""
        fence = "`" * 3
        real = "## Решения\n\n- несёт новое решение: x\n"
        quoted = "## Решения\n\n- решений не несёт\n"
        cases = (
            "%sx%s — пример\n\n%s%s\n%s%s\n" % (fence, fence, real, fence,
                                               quoted, fence),
            "%s\n%s%spython\n%s%s\n\n%s" % (fence, quoted, fence, quoted,
                                            fence, real),
        )
        for text in cases:
            with self.subTest(text):
                self.assertEqual(
                    cml.decision_variants(cml.decisions_section(text)),
                    [cml.NEW_DECISION])

    def test_heading_forms(self):
        """Заголовок с двоеточием и закрывающими «#» — тот же раздел;
        другое слово после «Решения» — другой раздел."""
        for heading in ("## Решения:", "## Решения ##", "## Решения: #",
                        "##Решения :"):
            with self.subTest(heading):
                self.assertEqual(cml.decision_variants(cml.decisions_section(
                    heading + "\n\n- решений не несёт\n\n## Чем проверено\n"
                    "- несёт новое решение: x\n")), [cml.NO_DECISIONS])
        for heading in ("## Решения и сроки", "## Решения##x",
                        "## Решения: что"):
            with self.subTest(heading):
                self.assertIsNone(cml.decisions_section(
                    heading + "\n\n- решений не несёт\n"))

    def test_group_with_dot(self):
        """Адрес без схемы, который начинается своей группой с точкой в
        имени, — путь, а не узел; адрес с узлом без схемы узнаётся, как
        прежде."""
        hosts = {"gitlab.example.invalid"}
        self.assertEqual(cml.referenced_mrs(
            "tmi.ru/sub/x/-/merge_requests/5", "tmi.ru/app", "tmi.ru",
            hosts), [("tmi.ru/sub/x", 5)])
        self.assertEqual(cml.referenced_mrs(
            "gitlab.example.invalid/tmi.ru/x/-/merge_requests/6",
            "tmi.ru/app", "tmi.ru", hosts), [("tmi.ru/x", 6)])
        self.assertEqual(cml.referenced_mrs(
            "other.example.invalid/tmi.ru/x/-/merge_requests/7",
            "tmi.ru/app", "tmi.ru", hosts), [])


if __name__ == "__main__":
    unittest.main()
