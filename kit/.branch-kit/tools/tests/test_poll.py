"""poll.py на поддельной площадке: события каждой роли, своё не событие,
исходное состояние, срок, сбои площадки, ключ и учётная запись, коды
выхода 0, 1, 2 и 130. В каждом запуске с ключом сверяется, что ключ не
попал в вывод."""

import contextlib
import io
import json
import os
import re
import unittest
from unittest import mock

import helpers
import fake_api
import kit_common as kit
import poll

ACCOUNT = {"author": "ai-dev", "reviewer": "ai-reviewer",
           "planner": "ai-planner"}
STAMP = re.compile(r"^\[\d\d:\d\d:\d\d\] ")
# Системные заметки площадки о пометке черновика, замерены на GitLab 17.11.2.
DRAFT_NOTE = "marked this merge request as **draft**"
READY_NOTE = "marked this merge request as **ready**"


class PollBase(unittest.TestCase):
    limit = 0

    def setUp(self):
        self.platform = fake_api.FakePlatform()
        self.addCleanup(self.platform.stop)
        for name, token in helpers.TOKENS.items():
            self.platform.user(token, name)
        self.cfg = helpers.config()
        self.cfg["площадка"]["адрес"] = self.platform.url
        self.cfg["опрос"]["не дольше"] = self.limit
        self.clone = helpers.Clone(self.cfg)
        self.addCleanup(self.clone.remove)

    def poll(self, role, *args, **kwargs):
        token = kwargs.pop("token", helpers.TOKENS[ACCOUNT[role]])
        run = self.clone.run("poll.py", "--role", role, *args, token=token,
                             **kwargs)
        for secret in list(helpers.TOKENS.values()) + [token]:
            if secret and secret.strip():
                self.assertNotIn(secret.strip(), run.out + run.err)
        return run

    def events(self, run):
        """Строки событий без метки времени; метка — [чч:мм:сс]."""
        self.assertRegex(run.last, r"^событий \d+$", run)
        for line in run.lines[:-1]:
            self.assertRegex(line, STAMP, run)
        return [STAMP.sub("", line) for line in run.lines[:-1]]

    def state_file(self, role, mr=None):
        name = role + ("-%d" % mr if mr is not None else "") + ".json"
        return os.path.join(self.clone.git_dir(), "branch-kit", "poll", name)

    def commit_as(self, account, message):
        """Изменение в клоне с подписью учётной записи из настройки; у
        прочих — подпись клона проверки, чужая для всех ролей."""
        accounts = self.cfg["учётные записи"]
        signs = dict((a["учётная запись"], a["подпись"])
                     for a in accounts["авторы"] + [accounts["проверяющий"]])
        sign = signs.get(account)
        options = []
        if sign:
            options = ["-c", "user.name=" + sign["имя"],
                       "-c", "user.email=" + sign["почта"]]
        self.clone.git(*(options + ["commit", "-q", "--allow-empty", "-m",
                                    message]))
        return self.clone.git("rev-parse", "HEAD")


class AuthorTest(PollBase):

    def setUp(self):
        PollBase.setUp(self)
        self.platform.add_request(12, "Средства ролей", "ai-dev", "feat/tools")
        self.platform.add_note(12, "ai-dev", "Модель автора: пример\nвторая")

    def first(self):
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 0, run)
        return run

    def test_first_run(self):
        """Первый запуск: исходное состояние как событие, код 0."""
        self.platform.add_note(12, "ai-dev", "approved", system=True)
        run = self.first()
        self.assertEqual(self.events(run), [
            "!12 исходное состояние: открыт «Средства ролей» от ai-dev, "
            "ветка feat/tools, вершина 11111111, утверждения: нет; последняя "
            "заметка 1000 от ai-dev: Модель автора: пример"])
        self.assertEqual(run.last, "событий 1")
        with open(self.state_file("author", 12), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["запросы"]["12"]["заметка"], 1001)

    def test_once_without_events(self):
        self.first()
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_events_between_runs(self):
        """Чужая заметка, смена вершины и утверждений, случившиеся, пока
        опрос не был поднят, — события; своя и системная заметки — нет."""
        self.first()
        self.platform.add_note(12, "ai-reviewer",
                               "Модель проверяющего: пример\nберу")
        self.platform.add_note(12, "ai-dev", "Модель автора: своё")
        self.platform.add_note(12, "ai-reviewer", "added 1 commit",
                               system=True)
        self.platform.set_sha(12, "2" * 40)
        self.platform.approve(12, "owner")
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!12 заметка 1001 от ai-reviewer: Модель проверяющего: пример",
            "!12 вершина ветки 11111111 → 22222222",
            "!12 утверждения: добавил owner"])
        self.assertEqual(run.last, "событий 3")
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 1, run)
        self.platform.unapprove(12, "owner")
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(self.events(run), ["!12 утверждения: снял owner"])

    def test_own_push(self):
        """Вершина, сохранённая подписью автора из настройки, — своё;
        правка проверяющего — событие, даже когда автор уже взял её в
        локальную ветку; вершина, которой нет в клоне, — событие."""
        self.first()
        own = self.commit_as("ai-dev", "своё изменение")
        self.platform.set_sha(12, own)
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 1, run)
        pulled = self.commit_as("ai-reviewer", "правка проверяющего")
        self.platform.set_sha(12, pulled)
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(self.events(run), ["!12 вершина ветки %s → %s"
                                            % (own[:8], pulled[:8])])
        self.platform.set_sha(12, "9" * 40)
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(self.events(run), ["!12 вершина ветки %s → 99999999"
                                            % pulled[:8]])

    def test_notes_over_page(self):
        """Больше 100 новых заметок — вторая страница; заметка, пришедшая
        между чтением страниц, сдвигает их, но события не повторяются, а
        пришедшие заметки покажет следующий запуск."""
        self.first()
        for n in range(150):
            self.platform.add_note(12, "ai-reviewer", "заметка %d" % n)
        later = []
        self.platform.when("/merge_requests/12/notes", 2, lambda: later.extend(
            self.platform.add_note(12, "owner", "позже %d" % n)
            for n in range(5)))
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 0, run)
        self.assertTrue(any("page=2" in p for p in
                            self.platform.paths("/merge_requests/12/notes")))
        self.assertEqual(len(later), 5)
        ids = [int(e.split()[2]) for e in self.events(run)]
        self.assertEqual(ids, list(range(1001, 1151)))
        self.assertEqual(run.last, "событий 150")
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual([int(e.split()[2]) for e in self.events(run)],
                         later)

    def test_waits_for_event(self):
        """Опрос ждёт: событие на третьем обращении — код 0."""
        self.first()
        self.platform.when("/merge_requests/12/notes", 3,
                           lambda: self.platform.add_note(
                               12, "owner", "Модель: утверждаю"))
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run),
                         ["!12 заметка 1001 от owner: Модель: утверждаю"])
        self.assertGreaterEqual(len(self.platform.paths("/notes")), 3)

    def test_merged_and_closed(self):
        self.first()
        self.platform.when("/notes", 2,
                           lambda: self.platform.set_state(12, "merged"))
        run = self.poll("author", "--mr", "12")
        self.assertEqual(self.events(run), ["!12 слит, слил ai-reviewer"])
        self.platform.set_state(12, "opened")
        self.assertEqual(self.events(self.poll("author", "--mr", "12")),
                         ["!12 открыт снова"])
        self.platform.set_state(12, "closed", by="owner")
        self.assertEqual(self.events(self.poll("author", "--mr", "12")),
                         ["!12 закрыт, закрыл owner"])

    def test_reset(self):
        """--reset забывает состояние без обращения к площадке; следующий
        запуск — как первый."""
        self.first()
        calls = len(self.platform.calls)
        run = self.poll("author", "--mr", "12", "--reset")
        self.assertEqual(run.code, 0, run)
        self.assertIn("состояние опроса забыто", run.out)
        self.assertFalse(os.path.exists(self.state_file("author", 12)))
        self.assertEqual(len(self.platform.calls), calls)
        run = self.poll("author", "--mr", "12", "--once")
        self.assertIn("исходное состояние", run.out)

    def test_bad_state(self):
        """Испорченное или чужое состояние — код 2 с советом --reset."""
        self.first()
        path = self.state_file("author", 12)
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        state["репозиторий"] = "example/other"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 2, run)
        self.assertIn("записано для другого запуска", run.err)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{")
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 2, run)
        self.assertIn("состояние опроса не читается", run.err)
        self.assertIn("--reset", run.err)

    def test_bad_state_entry(self):
        """Запись запроса не того вида — код 2 с файлом и советом --reset, а
        не падение и не ложные события; запись как есть — опрос идёт."""
        self.first()
        path = self.state_file("author", 12)
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        good = state["запросы"]["12"]
        cases = [
            ({"12": {}}, "пустая запись"),
            ({"12": []}, "запись — список"),
            ({"12": 5}, "запись — число"),
            ({"abc": good}, "номер запроса не число"),
            ({"12": dict(good, заметка="1001")}, "заметка — строка"),
            ({"12": dict(good, заметка=True)}, "заметка — истина"),
            ({"12": dict(good, вершина=5)}, "вершина — число"),
            ({"12": dict(good, утверждения=5)}, "утверждения — число"),
        ]
        for requests, name in cases:
            with self.subTest(name):
                state["запросы"] = requests
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False)
                run = self.poll("author", "--mr", "12", "--once")
                self.assertEqual(run.code, 2, run)
                self.assertIn("состояние опроса испорчено", run.err)
                self.assertIn("author-12.json", run.err)
                self.assertIn("--reset", run.err)
                self.assertNotIn("Traceback", run.err)
                self.assertEqual(run.out, "")
        state["запросы"] = {"12": good}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_arguments(self):
        cases = [
            (("author",), "у роли author ключ --mr обязателен"),
            (("reviewer", "--mr", "12"), "ключ --mr — только для роли "
                                         "author"),
            (("author", "--mr", "99"), "запроса !99 нет в example/app"),
        ]
        for args, text in cases:
            with self.subTest(args):
                run = self.poll(args[0], *args[1:])
                self.assertEqual(run.code, 2, run)
                self.assertIn(text, run.err)

    def test_not_own_request(self):
        self.platform.add_request(13, author="ai-reviewer")
        run = self.poll("author", "--mr", "13")
        self.assertEqual(run.code, 2, run)
        self.assertIn("запрос !13 подал ai-reviewer, а ключ — учётной "
                      "записи ai-dev", run.err)


class KeyTest(PollBase):

    def setUp(self):
        PollBase.setUp(self)
        self.platform.add_request(12)

    def test_bad_tokens(self):
        """Ключ с недопустимыми знаками — код 2 до первого обращения."""
        bad = ["ключ-root", "glpat-abc def", "glpat-ab\u00e9c",
               "\ufeff" + helpers.TOKENS["ai-dev"]]
        for token in bad:
            with self.subTest(token):
                run = self.poll("author", "--mr", "12", token=token)
                self.assertEqual(run.code, 2, run)
                self.assertIn("в GITLAB_TOKEN не ключ", run.err)
        self.assertEqual(self.platform.calls, [])

    def test_no_token(self):
        run = self.poll("author", "--mr", "12", token="")
        self.assertEqual(run.code, 2, run)
        self.assertIn("нет ключа", run.err)
        self.assertEqual(self.platform.calls, [])

    def test_edges(self):
        """Края ключа отбрасываются: на площадку уходит ключ без них."""
        token = helpers.TOKENS["ai-dev"]
        run = self.poll("author", "--mr", "12", token=" %s\r\n" % token)
        self.assertEqual(run.code, 0, run)
        self.assertEqual(set(t for _, t in self.platform.calls), {token})

    def test_foreign_account(self):
        """Ключ другой роли или чужой учётной записи — код 2, состояния
        нет."""
        for role, name in (("author", "ai-reviewer"),
                           ("reviewer", "ai-dev"),
                           ("planner", "stranger")):
            with self.subTest(role):
                args = ("--mr", "12") if role == "author" else ()
                run = self.poll(role, *args, token=helpers.TOKENS[name])
                self.assertEqual(run.code, 2, run)
                self.assertIn("ключ — учётной записи %s" % name, run.err)
        self.assertFalse(os.path.exists(os.path.dirname(
            self.state_file("author", 12))))

    def test_redirect_refused(self):
        """Переадресация на другой узел — код 2; ключ туда не дошёл."""
        other = fake_api.FakePlatform()
        self.addCleanup(other.stop)
        other.user(helpers.TOKENS["ai-dev"], "ai-dev")
        self.platform.redirect = other.url.replace(
            "127.0.0.1", "localhost") + "/api/v4/user"
        run = self.poll("author", "--mr", "12",
                        env={"NO_PROXY": "127.0.0.0/8,localhost"})
        self.assertEqual(run.code, 2, run)
        self.assertIn("ответила переадресацией 302", run.err)
        self.assertEqual(other.calls, [])

    def test_unknown_key(self):
        run = self.poll("author", "--mr", "12", token="glpat-unknown-key")
        self.assertEqual(run.code, 2, run)
        self.assertIn("площадка не приняла ключ", run.err)


class FailuresTest(PollBase):

    def setUp(self):
        PollBase.setUp(self)
        self.platform.add_request(12)
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 0, run)

    def fail_from_second(self, count):
        """Сбои площадки — со второго обращения к запросу: первое —
        сверка, что запрос свой."""
        self.platform.when("/merge_requests/12", 2,
                           lambda: setattr(self.platform, "failures", count))

    def test_five_failures(self):
        self.fail_from_second(100)
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 2, run)
        self.assertEqual(len(re.findall(r"сбой площадки \(\d из 5 подряд\)",
                                        run.err)), 4, run)
        self.assertIn("площадка не ответила 5 раз подряд", run.err)

    def test_four_failures_then_event(self):
        self.fail_from_second(4)
        self.platform.add_note(12, "ai-reviewer", "Модель проверяющего: да")
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(len(re.findall(r"сбой площадки", run.err)), 4, run)
        self.assertEqual(len(self.events(run)), 1)

    def test_once_failure(self):
        """С --once сбой площадки — сразу код 2."""
        self.fail_from_second(100)
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 2, run)
        self.assertNotIn("сбой площадки (", run.err)

    def test_refusal_not_retried(self):
        """Отказ площадки (404) — не сбой: код 2 без повторов."""
        self.platform.when("/merge_requests/12", 2,
                           lambda: setattr(self.platform, "project",
                                           "example/gone"))
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 2, run)
        self.assertIn("опрос не выполнен", run.err)
        self.assertNotIn("сбой площадки (", run.err)


class LimitTest(PollBase):
    limit = 0.01

    def test_limit(self):
        """«не дольше» вышло без событий — код 1, «событий 0»."""
        self.platform.add_request(12)
        self.poll("author", "--mr", "12")
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])
        self.assertIn("срок «не дольше» (0,01 мин) вышел без событий",
                      run.err)
        self.assertGreater(len(self.platform.paths("/merge_requests/12")), 3)


class LimitFailuresTest(PollBase):
    """Срок короче пяти сбоев: период 0,3 с, срок 0,75 с — не больше четырёх
    обращений за запуск."""

    limit = 0.0125

    def setUp(self):
        PollBase.setUp(self)
        self.cfg["опрос"]["период"] = 0.005
        self.clone.set_config(self.cfg)
        self.platform.add_request(12)
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 0, run)

    def fail_from(self, nth, count=1000):
        self.platform.when("/merge_requests/12", nth,
                           lambda: setattr(self.platform, "failures", count))

    def test_all_failed(self):
        """Все обращения срока сбойные — код 2 с причиной, а не код 1 «срок
        вышел без событий», и итога «событий 0» нет: иначе новый запуск
        считал бы сбои сначала и молчание длилось бы вечно. Второе обращение
        — первое за срок: первое — сверка, что запрос свой."""
        self.fail_from(2)
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 2, run)
        self.assertEqual(run.out, "")
        last = run.err.splitlines()[-1]
        self.assertIn("срок «не дольше» (0,0125 мин) вышел, ни одно обращение",
                      last)
        self.assertIn("последний сбой: ", last)
        self.assertIn("500 Internal Server Error", last)
        self.assertNotIn("вышел без событий", run.err)
        self.assertNotIn("раз подряд", run.err)
        with open(self.state_file("author", 12), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["запросы"]["12"]["заметка"], 0)

    def test_failure_then_success(self):
        """Опорный: сбой, затем успех — код 1, срок вышел без событий."""
        self.fail_from(2, count=1)
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])
        self.assertIn("сбой площадки (1 из 5 подряд)", run.err)
        self.assertIn("вышел без событий", run.err)

    def test_success_then_failures(self):
        """Опорный: успех, затем сбои до конца срока — не «все сбойные»:
        код 1. Первое обращение срока — запрос, заметки и утверждения."""
        self.fail_from(5)
        run = self.poll("author", "--mr", "12")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])
        self.assertIn("сбой площадки (1 из 5 подряд)", run.err)
        self.assertIn("вышел без событий", run.err)

    def test_all_failed_after_earlier_run(self):
        """Новый запуск считает сначала: успех прошлого запуска этому сроку
        не в зачёт."""
        self.assertEqual(self.poll("author", "--mr", "12").code, 1)
        self.fail_from(2)
        self.assertEqual(self.poll("author", "--mr", "12").code, 2)


class ReviewerTest(PollBase):

    def test_initial_empty(self):
        run = self.poll("reviewer")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run),
                         ["исходное состояние: открытых запросов в main нет"])

    def test_events(self):
        """Новый запрос, чужая заметка, смена вершины, утверждения —
        события; своя заметка и слитый запрос — нет."""
        self.platform.add_request(12, "Первый", branch="feat/a")
        self.platform.add_request(13, "Второй", branch="fix/b",
                                  sha="3" * 40)
        run = self.poll("reviewer")
        self.assertEqual([e.split(":")[0] for e in self.events(run)],
                         ["!12 исходное состояние", "!13 исходное состояние"])
        self.platform.add_request(14, "Третий", branch="feat/c")
        self.platform.add_note(12, "ai-dev", "Модель автора: ответ")
        self.platform.add_note(12, "ai-reviewer", "Модель проверяющего: беру")
        self.platform.set_sha(13, "4" * 40)
        self.platform.approve(13, "owner")
        self.platform.set_state(12, "merged")
        self.platform.add_request(15, "Четвёртый", branch="feat/d")
        self.platform.set_state(15, "closed")
        run = self.poll("reviewer")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!13 вершина ветки 33333333 → 44444444",
            "!13 утверждения: добавил owner",
            "!14 новый запрос «Третий» от ai-dev, ветка feat/c"])

    def test_vertex_merged_locally(self):
        """Отправка автора, которую проверяющий уже слил в свою локальную
        ветку для проверки результата слияния (AGENTS.md, 2.6), — событие;
        своя правка проверяющего — нет."""
        self.platform.add_request(12, branch="feat/a")
        self.poll("reviewer")
        base = self.clone.git("rev-parse", "HEAD")
        self.clone.git("checkout", "-q", "-b", "feat/a")
        pushed = self.commit_as("ai-dev", "отправка автора")
        self.clone.git("checkout", "-q", "-b", "review", base)
        self.clone.git("merge", "-q", "--no-ff", "-m", "результат слияния",
                       "feat/a")
        self.assertIn("refs/heads/review", self.clone.git(
            "for-each-ref", "--contains", pushed, "refs/heads"))
        self.platform.set_sha(12, pushed)
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), ["!12 вершина ветки 11111111 → %s"
                                            % pushed[:8]])
        self.clone.git("checkout", "-q", "feat/a")
        fix = self.commit_as("ai-reviewer", "правка проверяющего")
        self.platform.set_sha(12, fix)
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])


class PlannerTest(PollBase):

    def test_events(self):
        """Новый запрос, заметка, слияние и закрытие — события; вершина и
        утверждения постановщику — нет."""
        self.platform.add_request(12, "Первый")
        self.platform.add_request(13, "Второй")
        self.poll("planner")
        self.platform.add_note(13, "ai-dev", "Модель автора: вопрос")
        self.platform.add_note(13, "ai-planner", "Модель постановщика: ответ")
        self.platform.set_sha(13, "5" * 40)
        self.platform.approve(13, "owner")
        self.platform.set_state(12, "merged")
        self.platform.add_request(14, "Третий", branch="docs/x")
        run = self.poll("planner")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!13 заметка 1000 от ai-dev: Модель автора: вопрос",
            "!14 новый запрос «Третий» от ai-dev, ветка docs/x",
            "!12 слит, слил ai-reviewer"])
        self.platform.set_state(13, "closed", by="ai-dev")
        self.assertEqual(self.events(self.poll("planner")),
                         ["!13 закрыт, закрыл ai-dev"])

    def test_deleted(self):
        """Запрос, удалённый с площадки, — событие, и из состояния он
        уходит: следующий опрос его уже не читает."""
        self.platform.add_request(12, "Первый")
        self.platform.add_request(13, "Второй")
        self.poll("planner")
        with self.platform.lock:
            del self.platform.requests[12]
        run = self.poll("planner", "--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), ["!12 " + poll.DELETED])
        with open(self.state_file("planner"), encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)["запросы"]), ["13"])
        calls = len(self.platform.paths("/merge_requests/12"))
        run = self.poll("planner", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])
        self.assertEqual(len(self.platform.paths("/merge_requests/12")),
                         calls)

    def test_page_failure(self):
        """Полная страница без X-Next-Page: у опроса это сбой площадки, а не
        молча неполная очередь, в которой остальные запросы «пропали»."""
        with self.platform.lock:
            for iid in range(1, 151):
                self.platform.add_request(iid)
        self.poll("planner")
        self.platform.next_pages = [None]
        run = self.poll("planner", "--once")
        self.assertEqual(run.code, 2, run)
        self.assertIn("X-Next-Page", run.err)
        self.assertEqual(run.out, "")
        self.platform.next_pages = [None]
        self.platform.add_note(1, "ai-dev", "Модель автора: вопрос")
        run = self.poll("planner")
        self.assertEqual(run.code, 0, run)
        self.assertIn("сбой площадки (1 из 5 подряд)", run.err)
        self.assertEqual(self.events(run), [
            "!1 заметка 1000 от ai-dev: Модель автора: вопрос"])


class TargetTest(PollBase):
    """Очереди по цели запроса (AGENTS.md, 3.7, 4.3): постановщик и
    проверяющий ведут только запросы в основную ветку, проверяющий частей
    — только запросы в названную корневую ветку."""

    ROOT = "feat/vp-import"

    def setUp(self):
        PollBase.setUp(self)
        self.platform.main_repo.branches[self.ROOT] = "1" * 40
        self.platform.add_request(12, "Итоговый", branch=self.ROOT)
        self.platform.add_request(13, "Часть", branch=self.ROOT + "-parser",
                                  target_branch=self.ROOT)
        self.platform.add_request(14, "Часть другой задачи",
                                  branch="fix/b-one", target_branch="fix/b")

    def part(self, *args, **kwargs):
        """Опрос проверяющего частей — ключом автора: агент разработчика
        работает под учётной записью автора (AGENTS.md, 1.5)."""
        kwargs.setdefault("token", helpers.TOKENS["ai-dev"])
        return self.poll("reviewer", "--target", self.ROOT, *args, **kwargs)

    def part_state_file(self):
        return os.path.join(self.clone.git_dir(), "branch-kit", "poll",
                            "reviewer-feat%2Fvp-import.json")

    def test_main_queues(self):
        """Запрос в другую ветку не виден постановщику и проверяющему без
        --target: ни в исходном состоянии, ни новым запросом, ни заметкой,
        ни слиянием."""
        for role in ("reviewer", "planner"):
            with self.subTest(role):
                run = self.poll(role)
                self.assertEqual(run.code, 0, run)
                self.assertEqual([e.split(":")[0] for e in self.events(run)],
                                 ["!12 исходное состояние"])
        self.platform.add_request(15, "Новая часть", branch=self.ROOT + "-ui",
                                  target_branch=self.ROOT)
        self.platform.add_note(13, "owner", "заметка в части")
        self.platform.set_state(13, "merged", by="owner")
        for role in ("reviewer", "planner"):
            with self.subTest(role):
                run = self.poll(role, "--once")
                self.assertEqual(run.code, 1, run)
                self.assertEqual(run.lines, ["событий 0"])

    def test_part_queue(self):
        """С --target виден только запрос в названную ветку: не итоговый
        запрос в main и не часть другой задачи."""
        run = self.part()
        self.assertEqual(run.code, 0, run)
        self.assertEqual([e.split(":")[0] for e in self.events(run)],
                         ["!13 исходное состояние"])
        self.platform.add_request(15, "Новая часть", branch=self.ROOT + "-ui",
                                  target_branch=self.ROOT)
        self.platform.add_request(16, "Ещё в main", branch="docs/x")
        self.platform.add_note(12, "ai-reviewer", "Модель проверяющего: беру")
        self.platform.add_note(14, "owner", "заметка в чужой части")
        run = self.part("--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!15 новый запрос «Новая часть» от ai-dev, ветка "
            "feat/vp-import-ui"])

    def retarget(self, iid, target):
        with self.platform.lock:
            self.platform.requests[iid]["target_branch"] = target

    def single_reads(self, iid):
        """Обращения к самому запросу, без его заметок и утверждений."""
        tail = "/merge_requests/%d" % iid
        return [p for p in self.platform.paths(tail)
                if p.split("?")[0].endswith(tail)]

    def test_retarget(self):
        """Запрос, у которого сменили цель, — событие у проверяющего и у
        постановщика, один раз; дальше опрос его не ведёт и лишних обращений
        не делает; цель вернули — новый запрос."""
        for role in ("reviewer", "planner"):
            with self.subTest(role):
                self.retarget(12, "main")
                self.poll(role, "--reset")
                self.poll(role)
                self.retarget(12, "feat/other")
                run = self.poll(role, "--once")
                self.assertEqual(run.code, 0, run)
                self.assertEqual(self.events(run), [
                    "!12 цель сменена: main → feat/other — вне очереди"])
                reads = len(self.single_reads(12))
                self.platform.add_note(12, "owner", "заметка вне очереди")
                run = self.poll(role, "--once")
                self.assertEqual(run.code, 1, run)
                self.assertEqual(run.lines, ["событий 0"])
                self.assertEqual(len(self.single_reads(12)), reads)
                self.retarget(12, "main")
                self.assertEqual(self.events(self.poll(role, "--once")), [
                    "!12 новый запрос «Итоговый» от ai-dev, ветка "
                    "feat/vp-import"])

    def test_retarget_part_queue(self):
        """У проверяющего частей — то же: часть ушла из корневой ветки."""
        self.part()
        self.retarget(13, "main")
        self.assertEqual(self.events(self.part("--once")), [
            "!13 цель сменена: feat/vp-import → main — вне очереди"])

    def test_retarget_draft(self):
        """У проверяющего черновик не событие и со сменой цели; запрос на
        проверке, у которого в том же промежутке поставили пометку и сменили
        цель, — событие: он был в очереди."""
        with self.platform.lock:
            self.platform.requests[12]["draft"] = True
        self.poll("reviewer")
        self.retarget(12, "feat/other")
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])
        self.platform.add_request(15, "Другой", branch="docs/x")
        self.poll("reviewer", "--once")
        with self.platform.lock:
            self.platform.requests[15]["draft"] = True
        self.retarget(15, "feat/other")
        self.assertEqual(self.events(self.poll("reviewer", "--once")), [
            "!15 цель сменена: main → feat/other — вне очереди"])

    def test_merged_gives_no_target_event(self):
        """Опорный: слитый и закрытый запросы — как прежде: у проверяющего
        молча, у постановщика — со слившим; о цели события нет, и если её
        сменили перед слиянием. Удалённый — так же."""
        self.platform.add_request(15, "Закрытый", branch="docs/x")
        self.platform.add_request(16, "Удалённый", branch="docs/y")
        self.poll("reviewer")
        self.poll("planner")
        self.retarget(12, "feat/other")
        self.platform.set_state(12, "merged")
        self.platform.set_state(15, "closed", by="ai-dev")
        with self.platform.lock:
            del self.platform.requests[16]
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])
        self.assertEqual(self.events(self.poll("planner", "--once")), [
            "!12 слит, слил ai-reviewer", "!15 закрыт, закрыл ai-dev",
            "!16 " + poll.DELETED])

    def test_listed_requests_not_read(self):
        """Опорный: лишнее обращение — только на пропавший запрос: запрос в
        списке отдельно не читается ни у проверяющего, ни у постановщика."""
        for role in ("reviewer", "planner"):
            with self.subTest(role):
                self.poll(role)
                self.platform.calls.clear()
                run = self.poll(role, "--once")
                self.assertEqual(run.code, 1, run)
                self.assertEqual(self.single_reads(12), [])

    def test_answer_ahead_of_list(self):
        """Список уже без запроса, а ответ по номеру — открыт с той же целью:
        запрос успели закрыть и снова открыть. Событий нет, запрос остаётся в
        очереди, и заметка в нём не теряется."""
        for role in ("reviewer", "planner"):
            with self.subTest(role):
                self.poll(role)
                self.platform.set_state(12, "closed")
                self.platform.when("/merge_requests/12", 1, lambda: (
                    self.platform.requests[12].update(state="opened")))
                run = self.poll(role, "--once")
                self.assertEqual(run.code, 1, run)
                self.assertEqual(run.lines, ["событий 0"])
                self.platform.add_note(12, "owner", "заметка " + role)
                events = self.events(self.poll(role, "--once"))
                self.assertEqual(len(events), 1, events)
                self.assertIn("заметка " + role, events[0])

    def test_read_failure_keeps_request(self):
        """Сбой на чтении пропавшего запроса — сбой обращения: с --once код
        2, запрос остаётся в состоянии, и следующее обращение покажет событие
        о нём, а не потеряет его."""
        self.poll("reviewer")
        self.retarget(12, "feat/other")
        self.platform.when("/merge_requests/12", 1, lambda: setattr(
            self.platform, "failures", 1))
        self.assertEqual(self.poll("reviewer", "--once").code, 2)
        self.assertEqual(self.events(self.poll("reviewer", "--once")), [
            "!12 цель сменена: main → feat/other — вне очереди"])

    def test_part_queue_empty(self):
        """Ветка есть, запросов в неё нет — первый запуск говорит об этом,
        следующий — «событий 0»; имя ветки в адресе — часть пути."""
        self.platform.main_repo.branches["feat/none"] = "1" * 40
        run = self.poll("reviewer", "--target", "feat/none",
                        token=helpers.TOKENS["ai-dev"])
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "исходное состояние: открытых запросов в feat/none нет"])
        run = self.poll("reviewer", "--target", "feat/none", "--once",
                        token=helpers.TOKENS["ai-dev"])
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_target_in_address(self):
        """Ветка сверяется при запуске, имя в адресе — часть пути."""
        self.part()
        self.assertTrue(self.platform.paths(
            "/repository/branches/feat%2Fvp-import"))

    def test_target_typo(self):
        """Опечатка в --target и пробел по краю имени — код 2 с именем
        ветки: очередь иначе пуста навсегда. Состояния нет."""
        for name in ("feat/vp-imprt", self.ROOT + " "):
            with self.subTest(name):
                run = self.poll("reviewer", "--target", name,
                                token=helpers.TOKENS["ai-dev"])
                self.assertEqual(run.code, 2, run)
                self.assertIn("ветки «%s» нет" % name, run.err)
                self.assertEqual(run.out, "")
                self.assertFalse(os.path.exists(os.path.join(
                    self.clone.git_dir(), "branch-kit", "poll")))

    def test_target_refused(self):
        """Другой отказ площадки при сверке ветки — код 2 с именем ветки:
        ветка не сверена, а не «нет»."""
        self.platform.refuse[("GET", "feat%2Fvp-import")] = (403, "Forbidden")
        run = self.part()
        self.assertEqual(run.code, 2, run)
        self.assertIn("ветка «%s» не сверена" % self.ROOT, run.err)
        self.assertEqual(run.out, "")

    def test_shared_account(self):
        """Автор части и проверяющий частей — под одной учётной записью
        автора: её заметка и отправка с её подписью — события, иначе
        проверяющий частей пропустил бы ответ автора части."""
        self.part()
        self.platform.add_note(13, "ai-dev", "Модель автора: ответ на З-1")
        pushed = self.commit_as("ai-dev", "отправка автора части")
        self.platform.set_sha(13, pushed)
        self.platform.add_note(13, "ai-dev", "added 1 commit", system=True)
        run = self.part("--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!13 заметка 1000 от ai-dev: Модель автора: ответ на З-1",
            "!13 вершина ветки 11111111 → %s" % pushed[:8]])

    def test_part_reviewer_key(self):
        """Очередь частей — агента разработчика под учётной записью
        автора: ключ проверяющего или постановщика — отказ, состояния
        нет."""
        for name in ("ai-reviewer", "ai-planner"):
            with self.subTest(name):
                run = self.part(token=helpers.TOKENS[name])
                self.assertEqual(run.code, 2, run)
                self.assertIn("ключ — учётной записи %s, а у роли "
                              "«проверяющий частей» в настройке — ai-dev"
                              % name, run.err)
        self.assertFalse(os.path.exists(self.part_state_file()))

    def test_arguments(self):
        """--target — только у проверяющего и не основная ветка; отказ —
        до обращения к площадке."""
        cases = [
            (("planner", "--target", self.ROOT),
             "ключ --target — только для роли reviewer"),
            (("author", "--mr", "12", "--target", self.ROOT),
             "ключ --target — только для роли reviewer"),
            (("reviewer", "--target", "main"),
             "запросы в основную ветку main проверяющий ведёт без ключа "
             "--target"),
            (("reviewer", "--target", " "),
             "ключ --target — имя корневой ветки задачи"),
        ]
        for args, text in cases:
            with self.subTest(args):
                run = self.poll(args[0], *args[1:])
                self.assertEqual(run.code, 2, run)
                self.assertIn(text, run.err)
        self.assertEqual(self.platform.calls, [])

    def test_states_apart(self):
        """У очереди частей свой файл состояния с целью: она и очередь
        проверяющего друг друга не сбивают, --reset забывает только
        свою."""
        self.poll("reviewer")
        self.part()
        with open(self.state_file("reviewer"), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual((state["цель"], sorted(state["запросы"])),
                         ("main", ["12"]))
        with open(self.part_state_file(), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual((state["цель"], sorted(state["запросы"])),
                         (self.ROOT, ["13"]))
        self.assertEqual(self.poll("reviewer", "--once").code, 1)
        self.assertEqual(self.part("--once").code, 1)
        run = self.part("--reset")
        self.assertEqual(run.code, 0, run)
        self.assertFalse(os.path.exists(self.part_state_file()))
        self.assertTrue(os.path.exists(self.state_file("reviewer")))

    def test_state_without_target(self):
        """Состояние, записанное до фильтра по цели, — код 2 с советом
        --reset: в нём могут быть запросы частей."""
        self.poll("planner")
        path = self.state_file("planner")
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        del state["цель"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        run = self.poll("planner", "--once")
        self.assertEqual(run.code, 2, run)
        self.assertIn("записано для другого запуска: поле «цель» — нет, "
                      "сейчас — main", run.err)
        self.assertIn("--reset", run.err)


class PartAuthorTest(PollBase):
    """Автор запроса части и проверяющий частей могут работать под одной
    учётной записью автора (AGENTS.md, 1.5, 3.7): у запроса, цель которого не
    основная ветка, заметка этой записи и отправка с её подписью — события,
    иначе замечание до автора не дошло бы. Цель — из ответа площадки."""

    ROOT = "feat/vp-import"

    def setUp(self):
        PollBase.setUp(self)
        self.platform.add_request(12, "Итоговый", branch=self.ROOT)
        self.platform.add_request(13, "Часть", branch=self.ROOT + "-parser",
                                  target_branch=self.ROOT)

    def test_part_request(self):
        """Заметка и отправка под общей учётной записью — по одному событию;
        системная заметка — нет."""
        self.assertEqual(self.poll("author", "--mr", "13").code, 0)
        self.platform.add_note(13, "ai-dev",
                               "Модель проверяющего: пример\nЗ-1")
        pushed = self.commit_as("ai-dev", "правка мелочи проверяющим частей")
        self.platform.set_sha(13, pushed)
        self.platform.add_note(13, "ai-dev", "added 1 commit", system=True)
        run = self.poll("author", "--mr", "13", "--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!13 заметка 1000 от ai-dev: Модель проверяющего: пример",
            "!13 вершина ветки 11111111 → %s" % pushed[:8]])
        run = self.poll("author", "--mr", "13", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_main_request(self):
        """Опорный случай: запрос в основную ветку — своё не событие."""
        self.assertEqual(self.poll("author", "--mr", "12").code, 0)
        self.platform.add_note(12, "ai-dev", "Модель автора: своё")
        self.platform.set_sha(12, self.commit_as("ai-dev", "своя отправка"))
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_target_from_answer(self):
        """Цель — из ответа площадки при каждом запуске: запрос, цель которого
        сменили на основную ветку, дальше ведётся как запрос в неё."""
        self.assertEqual(self.poll("author", "--mr", "13").code, 0)
        self.platform.requests[13]["target_branch"] = "main"
        self.platform.add_note(13, "ai-dev", "Модель автора: своё")
        run = self.poll("author", "--mr", "13", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_other_events(self):
        """Чужая заметка и слияние части — события, как у любого автора."""
        self.assertEqual(self.poll("author", "--mr", "13").code, 0)
        self.platform.add_note(13, "owner", "Заметка разработчика")
        self.platform.set_state(13, "merged", by="owner")
        run = self.poll("author", "--mr", "13", "--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!13 заметка 1000 от owner: Заметка разработчика",
            "!13 слит, слил owner"])

    def test_not_own_part(self):
        """Запрос части, поданный другой учётной записью, — отказ, как и у
        запроса в основную ветку."""
        self.platform.add_request(14, author="ai-reviewer",
                                  target_branch=self.ROOT)
        run = self.poll("author", "--mr", "14")
        self.assertEqual(run.code, 2, run)
        self.assertIn("запрос !14 подал ai-reviewer", run.err)


class DraftTest(PollBase):
    """Черновик — работа автора (AGENTS.md, 3.7, пп. 1, 7): проверяющий,
    и с --target, его не ведёт, событие — снятие пометки; постановщик и
    автор видят черновик, как раньше (1.6). Признак — поле draft площадки
    или, без поля, имя с «Draft:»."""

    ROOT = "feat/vp-import"

    def draft(self, iid, on, field=True):
        """Пометка черновика, как её ставит площадка: имя и поле draft;
        field=False — площадка без поля draft, признак только в имени."""
        with self.platform.lock:
            item = self.platform.requests[iid]
            title = item["title"].replace("Draft: ", "")
            item["title"] = "Draft: " + title if on else title
            if field:
                item["draft"] = on
            else:
                item.pop("draft", None)

    def mark_draft(self, iid):
        """Пометка вместе с системной заметкой площадки о ней."""
        self.draft(iid, True)
        self.platform.add_note(iid, "ai-dev", DRAFT_NOTE, system=True)

    def touch(self, iid):
        """Заметка, отправка и утверждение в запросе."""
        self.platform.add_note(iid, "ai-dev", "Модель автора: вопрос")
        self.platform.set_sha(iid, "6" * 40)
        self.platform.approve(iid, "owner")

    def test_reviewer_skips_draft_field(self):
        """Признак площадки: поле draft при имени без «Draft:» —
        черновик; ни исходным состоянием, ни заметкой, ни вершиной, ни
        утверждением он проверяющего не будит."""
        self.platform.add_request(12, "Итоговый", draft=True)
        self.platform.add_request(13, "Обычный", branch="fix/b")
        run = self.poll("reviewer")
        self.assertEqual([e.split(":")[0] for e in self.events(run)],
                         ["!13 исходное состояние"])
        self.touch(12)
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_reviewer_skips_draft_title(self):
        """Площадка без поля draft: черновик — по имени с «Draft:»."""
        self.platform.add_request(12, "Итоговый")
        self.draft(12, True, field=False)
        run = self.poll("reviewer")
        self.assertEqual(self.events(run), [
            "исходное состояние: открытых запросов в main нет, кроме "
            "черновиков: !12"])
        self.touch(12)
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_new_draft(self):
        """Новый черновик — не событие; снятие пометки с него — событие,
        дальше запрос — как любой."""
        self.poll("reviewer")
        self.platform.add_request(12, "Итоговый", draft=True)
        self.draft(12, True)
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])
        self.draft(12, False)
        run = self.poll("reviewer", "--once")
        self.assertEqual(self.events(run), ["!12 " + poll.DRAFT_OFF])
        self.platform.add_note(12, "ai-dev", "Модель автора: готово")
        run = self.poll("reviewer", "--once")
        self.assertEqual(self.events(run), [
            "!12 заметка 1000 от ai-dev: Модель автора: готово"])

    def test_ready(self):
        """Снятие пометки будит проверяющего; заметка и отправка того же
        промежутка — события вслед за ним, а сделанное в черновике до
        него — нет."""
        self.platform.add_request(12, "Итоговый")
        self.draft(12, True)
        self.poll("reviewer")
        self.touch(12)
        self.poll("reviewer", "--once")
        self.draft(12, False)
        self.platform.add_note(12, "ai-dev", "Модель автора: части слиты")
        self.platform.set_sha(12, "7" * 40)
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!12 " + poll.DRAFT_OFF,
            "!12 заметка 1001 от ai-dev: Модель автора: части слиты",
            "!12 вершина ветки 66666666 → 77777777"])

    def test_back_to_draft(self):
        """Пометка, поставленная запросу на проверке, — событие; заметка
        после неё и всё дальнейшее до её снятия событий не дают. Порядок в
        одном промежутке — по системной заметке площадки о пометке."""
        self.platform.add_request(12, "Итоговый")
        self.poll("reviewer")
        self.mark_draft(12)
        self.platform.add_note(12, "ai-dev", "Модель автора: вопрос")
        run = self.poll("reviewer", "--once")
        self.assertEqual(self.events(run), ["!12 " + poll.DRAFT_ON])
        self.touch(12)
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 1, run)

    def test_note_before_draft(self):
        """Заметка, написанная до пометки в том же промежутке, показывается
        вместе с пометкой: иначе её не показал бы ни этот запуск, ни снятие
        пометки. Заметка после пометки — молчит."""
        self.platform.add_request(12, "Итоговый")
        self.poll("reviewer")
        self.platform.add_note(12, "owner", "Ответ проверяющему")
        self.mark_draft(12)
        self.platform.add_note(12, "ai-dev", "Модель автора: вопрос")
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.events(run), [
            "!12 " + poll.DRAFT_ON,
            "!12 заметка 1000 от owner: Ответ проверяющему"])
        self.draft(12, False)
        self.assertEqual(self.events(self.poll("reviewer", "--once")),
                         ["!12 " + poll.DRAFT_OFF])

    def test_note_order_unknown(self):
        """Системной заметки о пометке нет — порядок не определить: заметка
        показывается, лишнее дешевле потерянного. Своя заметка и системная
        — по-прежнему нет."""
        self.platform.add_request(12, "Итоговый")
        self.poll("reviewer")
        self.draft(12, True)
        self.platform.add_note(12, "ai-dev", "added 1 commit", system=True)
        self.platform.add_note(12, "ai-dev", "Модель автора: вопрос")
        self.platform.add_note(12, "ai-reviewer", "Модель проверяющего: своё")
        run = self.poll("reviewer", "--once")
        self.assertEqual(self.events(run), [
            "!12 " + poll.DRAFT_ON,
            "!12 заметка 1001 от ai-dev: Модель автора: вопрос"])

    def test_note_with_mark_words(self):
        """Обычная заметка со словами системной порядка не определяет: её мог
        написать кто угодно, и заметка после неё показывается."""
        self.platform.add_request(12, "Итоговый")
        self.poll("reviewer")
        self.draft(12, True)
        self.platform.add_note(12, "owner", DRAFT_NOTE)
        self.platform.add_note(12, "ai-dev", "Модель автора: вопрос")
        self.assertEqual(self.events(self.poll("reviewer", "--once")), [
            "!12 " + poll.DRAFT_ON,
            "!12 заметка 1000 от owner: " + DRAFT_NOTE,
            "!12 заметка 1001 от ai-dev: Модель автора: вопрос"])

    def test_note_between_two_marks(self):
        """Пометку ставили дважды в одном промежутке: порядок считается от
        последней, заметка между ними показана, заметка после — нет."""
        self.platform.add_request(12, "Итоговый")
        self.poll("reviewer")
        self.mark_draft(12)
        self.platform.add_note(12, "owner", "Между пометками")
        self.platform.add_note(12, "ai-dev", READY_NOTE, system=True)
        self.mark_draft(12)
        self.platform.add_note(12, "ai-dev", "Модель автора: после")
        self.assertEqual(self.events(self.poll("reviewer", "--once")), [
            "!12 " + poll.DRAFT_ON,
            "!12 заметка 1001 от owner: Между пометками"])

    def test_approvals_with_draft(self):
        """Смена утверждений в промежутке постановки пометки показывается с
        ней, в обе стороны: когда её сделали, площадка не говорит. Отправка
        того же промежутка — по-прежнему нет: снятие пометки и так велит
        читать запрос заново."""
        self.platform.add_request(12, "Итоговый")
        self.platform.approve(12, "ai-dev")
        self.poll("reviewer")
        self.platform.approve(12, "owner")
        self.platform.unapprove(12, "ai-dev")
        self.platform.set_sha(12, "6" * 40)
        self.mark_draft(12)
        self.assertEqual(self.events(self.poll("reviewer", "--once")), [
            "!12 " + poll.DRAFT_ON,
            "!12 утверждения: добавил owner; снял ai-dev"])

    def test_draft_stays_silent(self):
        """Опорный: в запросе, уже помеченном, заметка и смена утверждений
        молчат, и второго события о пометке нет."""
        self.platform.add_request(12, "Итоговый")
        self.poll("reviewer")
        self.mark_draft(12)
        self.events(self.poll("reviewer", "--once"))
        self.platform.add_note(12, "owner", "Вопрос в черновике")
        self.platform.approve(12, "owner")
        run = self.poll("reviewer", "--once")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["событий 0"])

    def test_planner_and_author_see_draft(self):
        """Постановщик видит черновик и вопрос автора в нём (AGENTS.md,
        1.6), автор — заметки в своём черновике: их роли не меняются."""
        self.platform.add_request(12, "Итоговый")
        self.draft(12, True)
        run = self.poll("planner")
        self.assertEqual(self.events(run)[0].split(" от ")[0],
                         "!12 исходное состояние: открыт «Draft: Итоговый»")
        self.platform.add_note(12, "ai-dev", "Модель автора: вопрос")
        run = self.poll("planner", "--once")
        self.assertEqual(self.events(run), [
            "!12 заметка 1000 от ai-dev: Модель автора: вопрос"])
        self.poll("author", "--mr", "12")
        self.platform.add_note(12, "ai-planner", "Модель постановщика: ответ")
        run = self.poll("author", "--mr", "12", "--once")
        self.assertEqual(self.events(run), [
            "!12 заметка 1001 от ai-planner: Модель постановщика: ответ"])

    def test_part_queue(self):
        """С --target то же: черновик части не будит проверяющего частей,
        снятие пометки будит."""
        self.platform.main_repo.branches[self.ROOT] = "1" * 40
        self.platform.add_request(13, "Часть", branch=self.ROOT + "-parser",
                                  target_branch=self.ROOT, draft=True)
        self.draft(13, True)
        run = self.poll("reviewer", "--target", self.ROOT,
                        token=helpers.TOKENS["ai-dev"])
        self.assertEqual(self.events(run), [
            "исходное состояние: открытых запросов в %s нет, кроме "
            "черновиков: !13" % self.ROOT])
        self.touch(13)
        run = self.poll("reviewer", "--target", self.ROOT, "--once",
                        token=helpers.TOKENS["ai-dev"])
        self.assertEqual(run.code, 1, run)
        self.draft(13, False)
        run = self.poll("reviewer", "--target", self.ROOT, "--once",
                        token=helpers.TOKENS["ai-dev"])
        self.assertEqual(self.events(run), ["!13 " + poll.DRAFT_OFF])


class CrashTest(PollBase):
    """Падение на негодном входе — код 2, а не код 1 «событий нет: поднимите
    снова»; в потоке ошибок вид и текст исключения, трассировки нет."""

    def setUp(self):
        PollBase.setUp(self)
        self.platform.add_request(12)

    def crashed(self, run, kind):
        self.assertEqual(run.code, 2, run)
        self.assertIn(kind, run.err)
        self.assertNotIn("Traceback", run.err)

    def test_wrong_answer(self):
        """Ответ площадки 200 не того вида: список вместо записи."""
        with mock.patch.object(self.platform, "route",
                               return_value=(200, ["не запись"], {})):
            run = self.poll("author", "--mr", "12")
        self.crashed(run, "AttributeError")

    def test_period_inf(self):
        """Период .inf проходит сверку настройки, а ждать столько время не
        умеет. Опорный: первый запуск без ожидания — код 0."""
        self.assertEqual(self.poll("author", "--mr", "12").code, 0)
        self.cfg["опрос"]["период"] = float("inf")
        self.clone.set_config(self.cfg)
        self.crashed(self.poll("author", "--mr", "12"), "OverflowError")

    def test_address_not_ascii(self):
        """Адрес площадки с буквами вне ASCII в пути настройка пропускает, а
        запрос с ним не отправить."""
        self.cfg["площадка"]["адрес"] = self.platform.url + "/путь"
        self.clone.set_config(self.cfg)
        self.crashed(self.poll("author", "--mr", "12"), "UnicodeEncodeError")


class InProcessBase(PollBase):
    """Опрос в том же процессе: так в его ход вставляются Ctrl+C и отказ
    файловой системы."""

    def setUp(self):
        PollBase.setUp(self)
        self.platform.add_request(12)
        self.assertEqual(self.poll("author", "--mr", "12").code, 0)
        patches = [
            mock.patch.object(kit, "clone_root",
                              return_value=self.clone.root),
            helpers.patch_env(GITLAB_TOKEN=helpers.TOKENS["ai-dev"]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with open(self.state_file("author", 12), "rb") as f:
            self.before = f.read()

    def main(self, *args):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = poll.main(["--role", "author", "--mr", "12", "--config",
                              self.clone.config_path] + list(args))
        for text in (out.getvalue(), err.getvalue()):
            self.assertNotIn(helpers.TOKENS["ai-dev"], text)
        return code, out.getvalue(), err.getvalue()

    def state(self):
        with open(self.state_file("author", 12), "rb") as f:
            return f.read()


class CrashKeyTest(InProcessBase):
    """Текст необработанного исключения выводится, а ключ — нет, даже если
    он в этот текст попал (main сверяет вывод)."""

    def test_key_hidden(self):
        with mock.patch.object(poll, "poll", side_effect=RuntimeError(
                "сбой, ключ %s" % helpers.TOKENS["ai-dev"])):
            code, out, err = self.main("--once")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("RuntimeError: сбой, ключ", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.state(), self.before)


class InterruptTest(InProcessBase):
    """Ctrl+C — в том же процессе: отдельному процессу его надёжно не
    послать в обеих системах."""

    def test_ctrl_c_while_waiting(self):
        with mock.patch.object(poll, "_sleep",
                               side_effect=KeyboardInterrupt):
            code, out, err = self.main()
        self.assertEqual(code, 130)
        self.assertIn("опрос прерван; состояние не изменено", err)
        self.assertEqual(self.state(), self.before)

    def test_ctrl_c_between_output_and_state(self):
        """Прерванный между выводом и записью запуск покажет те же события
        снова: состояние пишется после вывода."""
        self.platform.add_note(12, "ai-reviewer", "Модель проверяющего: да")
        with mock.patch.object(poll, "save_state",
                               side_effect=KeyboardInterrupt):
            code, out, _ = self.main()
        self.assertEqual(code, 130)
        self.assertIn("!12 заметка 1000 от ai-reviewer", out)
        self.assertEqual(self.state(), self.before)
        code, out, _ = self.main()
        self.assertEqual(code, 0)
        self.assertIn("!12 заметка 1000 от ai-reviewer", out)
        self.assertNotEqual(self.state(), self.before)


class StateWriteTest(InProcessBase):
    """Файл состояния не заменить или не удалить — код 2 с путём, а не
    трассировка; выведенные события следующий запуск покажет снова."""

    def test_replace_refused(self):
        self.platform.add_note(12, "ai-reviewer", "Модель проверяющего: да")
        refused = PermissionError(13, "отказано в доступе")
        with mock.patch.object(os, "replace", side_effect=refused):
            code, out, err = self.main()
        self.assertEqual(code, 2)
        self.assertIn("!12 заметка 1000 от ai-reviewer", out)
        self.assertIn("состояние опроса не записано: ", err)
        self.assertIn("author-12.json (", err)
        self.assertEqual(self.state(), self.before)
        self.assertFalse(os.path.exists(self.state_file("author", 12) +
                                        ".tmp"))
        code, out, _ = self.main()
        self.assertEqual(code, 0)
        self.assertIn("!12 заметка 1000 от ai-reviewer", out)

    def test_remove_refused(self):
        refused = PermissionError(13, "отказано в доступе")
        with mock.patch.object(os, "remove", side_effect=refused):
            code, out, err = self.main("--reset")
        self.assertEqual(code, 2)
        self.assertIn("состояние опроса не забыто: ", err)
        self.assertEqual(self.state(), self.before)

    @unittest.skipUnless(os.name == "nt", "открытый файл не даёт заменить "
                                          "и удалить себя только в Windows")
    def test_file_held(self):
        """В Windows файл, открытый другим процессом, не заменить и не
        удалить: так бывает, когда его держит второй опрос."""
        self.platform.add_note(12, "ai-reviewer", "Модель проверяющего: да")
        with open(self.state_file("author", 12), "rb"):
            run = self.poll("author", "--mr", "12", "--once")
            reset = self.poll("author", "--mr", "12", "--reset")
        self.assertEqual(run.code, 2, run)
        self.assertIn("!12 заметка 1000 от ai-reviewer", run.out)
        self.assertIn("состояние опроса не записано: ", run.err)
        self.assertNotIn("Traceback", run.err)
        self.assertEqual(reset.code, 2, reset)
        self.assertIn("состояние опроса не забыто: ", reset.err)
        self.assertNotIn("Traceback", reset.err)
        self.assertEqual(self.state(), self.before)


if __name__ == "__main__":
    unittest.main()
