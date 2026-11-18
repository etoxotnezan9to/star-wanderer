"""Проверка настройки попыткой — на локальных хранилищах (file://) и
поддельной площадке; к настоящей площадке проверки не обращаются.

Защиту площадки изображает приёмный перехватчик хранилища (pre-receive)
с ответами площадки; что разрешено — задаёт проверка: защита как на
площадке, без защиты и защита, пропускающая одно. Чей ключ у отправки,
перехватчик узнаёт по переменной ключа: при file:// приёмная сторона git
получает окружение отправляющей. Запросы, заметку и слияние з) изображает
поддельная площадка. Средство выполняется в том же процессе: так в ход
попыток вставляются чужое изменение, сбой и Ctrl+C, а адрес хранилища
подменяется на file://."""

import base64
import contextlib
import datetime
import gc
import io
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import yaml

import helpers
import fake_api
import owner_check_settings as ocs

IDENTITY = ("Test Owner", "owner@example.invalid")
ENV = dict(helpers.base_env(), GIT_AUTHOR_NAME=IDENTITY[0],
           GIT_AUTHOR_EMAIL=IDENTITY[1], GIT_COMMITTER_NAME=IDENTITY[0],
           GIT_COMMITTER_EMAIL=IDENTITY[1])
TOKENS = {"author": helpers.TOKENS["ai-dev"],
          "reviewer": helpers.TOKENS["ai-reviewer"],
          "planner": helpers.TOKENS["ai-planner"]}
ACCOUNTS = {"author": "ai-dev", "reviewer": "ai-reviewer",
            "planner": "ai-planner"}
LEVELS = {"author": 30, "reviewer": 40, "planner": 20}
OPTIONS = {"author": "--developer-key", "reviewer": "--maintainer-key",
           "planner": "--planner-key"}
ALL = ("author", "reviewer", "planner")
MAIN_ANSWER = ("GitLab: You are not allowed to push code to protected "
               "branches on this project.")

ATTEMPT_LINE = re.compile(
    r"^\[\d\d:\d\d:\d\d\] (\S+\)) .+ — ключ .+ — код \S+ — (.+)$")
SKIPPED_LINE = re.compile(r"^(\S+\)) .+ — не проверялось: ")
NOT_DONE_LINE = re.compile(r"^(\S+\)) .+ — НЕ ВЫПОЛНЕНА — ")


def git(cwd, *args, **kwargs):
    out = subprocess.run(["git", "-C", cwd] + list(args),
                         input=kwargs.get("input"), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=ENV, check=True,
                         encoding="utf-8")
    return out.stdout.strip()


def hook(force=False, delete=False, main_push=False, planner=False,
         probe=True, main_answer=MAIN_ANSWER, task="test"):
    """Перехватчик pre-receive: по умолчанию отказывает, как площадка с
    защитой main и <task>/*; True у вида записи — пропускает её. Пишет в
    журнал хранилища параметры отправки и переменные, которых у git быть
    не должно."""
    who = "\n".join('  %s*) who=%s ;;' % (token[:-10], role)
                    for role, token in TOKENS.items())
    rules = ""
    if not planner:
        rules += ('  if [ "$who" = planner ]; then\n'
                  '    echo "GitLab: You are not allowed to push code to '
                  'this project."\n    exit 1\n  fi\n')
    if not probe:
        rules += ('  case "$ref" in refs/heads/%s/*)\n' % task +
                  '    echo "GitLab: pre-receive hook declined"; exit 1;; '
                  'esac\n')
    if not force:
        rules += ('  if [ "$old" != "$z" ] && [ "$new" != "$z" ] && ! git '
                  'merge-base --is-ancestor "$old" "$new"; then\n'
                  '    case "$ref" in refs/heads/main|refs/heads/%s/*)\n'
                  % task +
                  '      echo "GitLab: You are not allowed to force push '
                  'code to a protected branch on this project."\n'
                  '      exit 1;;\n    esac\n  fi\n')
    if not delete:
        rules += ('  if [ "$new" = "$z" ]; then\n'
                  '    case "$ref" in refs/heads/main|refs/heads/%s/*)\n'
                  % task +
                  '      echo "GitLab: You are not allowed to delete '
                  'protected branches from this project. Only a project '
                  'maintainer or owner can delete a protected branch."\n'
                  '      exit 1;;\n    esac\n  fi\n')
    if not main_push:
        # С пропущенной принудительной отправкой main отказывает только
        # обычной: переписывание тогда проходит.
        ff = (' && git merge-base --is-ancestor "$old" "$new"'
              if force else "")
        rules += ('  if [ "$ref" = refs/heads/main ]%s; then\n'
                  '    echo "%s"\n    exit 1\n  fi\n' % (ff, main_answer))
    return ("#!/bin/sh\nz=0000000000000000000000000000000000000000\n"
            'case "$BRANCH_KIT_CHECK_KEY" in\n' + who +
            "\n  *) who=unknown ;;\nesac\n"
            'echo "${GIT_PUSH_OPTION_COUNT:-0} ${GIT_PUSH_OPTION_0:-none} '
            '$who" >> push.log\n'
            "env | grep -E '^(GIT_TRACE|GIT_CURL_VERBOSE|GITLAB_TOKEN|"
            "BRANCH_KIT_AUTHOR_TOKEN|SSLKEYLOGFILE)' >> leaked-env.log\n"
            "while read old new ref; do\n  :\n" + rules +
            "done\nexit 0\n")


class Server:
    """Голое хранилище «площадки» с веткой main."""

    def __init__(self, root, hook_text=None):
        self.dir = os.path.join(root, "srv", "example", "app.git")
        os.makedirs(self.dir)
        git(self.dir, "init", "--bare", "-q")
        git(self.dir, "config", "receive.advertisePushOptions", "true")
        git(self.dir, "update-ref", "refs/heads/main",
            self.commit("main\n", "docs: начало"))
        if hook_text:
            path = os.path.join(self.dir, "hooks", "pre-receive")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(hook_text)
            os.chmod(path, 0o755)
        self.url = pathlib.Path(self.dir).as_uri()

    def commit(self, text, message, parent=None):
        blob = git(self.dir, "hash-object", "-w", "--stdin", input=text)
        tree = git(self.dir, "mktree",
                   input="100644 blob %s\tREADME.md\n" % blob)
        parents = ["-p", parent] if parent else []
        return git(self.dir, "commit-tree", tree, *(parents + ["-m",
                                                                message]))

    def heads(self):
        out = git(self.dir, "for-each-ref", "--format=%(refname) "
                  "%(objectname)", "refs/heads/")
        return dict((ref[len("refs/heads/"):], sha) for ref, sha in
                    (line.split(" ") for line in out.splitlines()))

    def count(self, branch):
        return int(git(self.dir, "rev-list", "--count",
                       "refs/heads/" + branch))

    def log(self, name):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()


class SettingsCheckBase(unittest.TestCase):
    prefixes = ("docs/", "feat/", "test/")

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="bk-ocs-test-"))
        self.addCleanup(helpers.remove_tree, self.tmp)
        # Средство выполняется в этом процессе. Клиент, который открыл бы
        # файл SSLKEYLOGFILE (порча kit_common.Client), держит его до
        # сборки мусора, а открытый файл Windows не даст удалить: сборка
        # мусора идёт раньше удаления каталога.
        self.addCleanup(gc.collect)
        self.copies = os.path.join(self.tmp, "copies")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.copies)
        os.makedirs(self.home)
        self.files = {}
        for role, token in TOKENS.items():
            self.files[role] = self.key_file("key-" + role, token + "\r\n")
        self.platform = fake_api.FakePlatform()
        self.addCleanup(self.platform.stop)
        for role, token in TOKENS.items():
            self.platform.user(token, ACCOUNTS[role])
            self.platform.member(ACCOUNTS[role], LEVELS[role])
        repo = self.platform.repo(self.platform.project)
        for name, push, merge in (("main", 0, 40), ("docs/*", 30, 30),
                                  ("feat/*", 30, 30), ("test/*", 30, 30)):
            repo.protected[name] = fake_api.rule(name, push, merge)
        self.cfg = helpers.config()
        self.cfg["площадка"]["адрес"] = self.platform.url
        self.cfg["ветки задач"] = [{"префикс": p, "что": "задачи"}
                                   for p in self.prefixes]
        self.write_config(self.cfg)
        self.probe = "%ssettings-check-%s" % (
            "test/" if "test/" in self.prefixes else self.prefixes[0],
            datetime.date.today().isoformat())

    def key_file(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def write_config(self, cfg):
        self.config_path = os.path.join(self.tmp, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8",
                  newline="\n") as f:
            f.write(helpers.dump(cfg))

    def server(self, **flags):
        flags.setdefault("task", self.probe.split("/", 1)[0])
        self.srv = Server(self.tmp, hook(**flags))

        def on_merge(item, body):
            git(self.srv.dir, "update-ref", "refs/heads/main", body["sha"])
        self.platform.on_merge = on_merge
        return self.srv

    def run_tool(self, roles=ALL, files=None, host=None, env=None,
                 argv=None, url=None):
        """Средство в этом процессе; копия — в каталоге проверки; адрес
        хранилища — url или file:// хранилища проверки; ни один ключ не
        попадает в вывод."""
        files = dict(self.files, **(files or {}))
        args = list(argv or []) + ["--config", self.config_path]
        for role in roles:
            args += [OPTIONS[role], files[role]]
        if host:
            args += ["--host", host]
        run_env = helpers.base_env(HOME=self.home)
        run_env.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        target = url or self.srv.url
        try:
            with mock.patch.dict(os.environ, run_env, clear=True), \
                    mock.patch.object(tempfile, "tempdir", self.copies), \
                    mock.patch.object(ocs, "repo_url",
                                      lambda host, repo: target), \
                    mock.patch.object(ocs, "signature",
                                      lambda root: IDENTITY), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                self.code = ocs.main(args)
        finally:
            self.out, self.err = out.getvalue(), err.getvalue()
        for token in list(TOKENS.values()) + list(helpers.TOKENS.values()):
            self.assertNotIn(token, self.out + self.err)
        self.assertNotIn(ocs.KEY_ENV, os.environ)
        return self.code

    def verdicts(self):
        return [m.groups() for m in map(ATTEMPT_LINE.match,
                                        self.out.splitlines()) if m]

    def skipped(self):
        return [m.group(1) for m in map(SKIPPED_LINE.match,
                                        self.out.splitlines()) if m]

    def restores(self):
        return [line for line in self.out.splitlines()
                if line.startswith("    возврат из копии:")]

    def kept_copies(self):
        return os.listdir(self.copies)

    def fragment(self):
        head = "--- фрагмент протокола для .branch-kit/protocols/"
        return yaml.safe_load(self.out.split(head, 1)[1].split("\n", 1)[1])

    def pushes(self):
        """Отправки, дошедшие до площадки: каждая — с ci.skip."""
        lines = self.srv.log("push.log")
        for line in lines:
            self.assertTrue(line.startswith("1 ci.skip "), lines)
        return [line.split(" ")[2] for line in lines]

    def assert_untouched(self, before):
        """Запуск не выполнен: ни одной отправки, площадка не тронута,
        копий не осталось."""
        self.assertEqual(self.srv.heads(), before)
        self.assertEqual(self.srv.log("push.log"), [])
        self.assertEqual(self.platform.written(), [])
        self.assertEqual(self.kept_copies(), [])


REFUSED = ocs.AS_EXPECTED[ocs.REFUSE]
PASSED = ocs.AS_EXPECTED[ocs.PASS]


class AttemptsTest(SettingsCheckBase):

    def test_protected_as_on_platform(self):
        """Защита как на площадке: отказы — ответами площадки, разрешённое
        проходит; ветки не меняются, запрос закрыт, копий нет, код 0."""
        srv = self.server()
        before = srv.heads()
        self.assertEqual(self.run_tool(), 0, self.out + self.err)
        self.assertEqual(self.verdicts(), [
            ("а)", REFUSED), ("б)", REFUSED), ("в)", PASSED),
            ("г)", REFUSED), ("д)", REFUSED), ("е)", REFUSED),
            ("ж)", PASSED), ("з)", REFUSED)])
        self.assertEqual(self.pushes(), ["author", "reviewer", "author",
                                         "author", "author", "planner"])
        self.assertEqual(self.restores(), [])
        after = srv.heads()
        self.assertEqual(after["main"], before["main"])
        self.assertEqual(git(srv.dir, "log", "-1", "--format=%s",
                             after[self.probe]), ocs.PROBE_MESSAGE)
        self.assertEqual(git(srv.dir, "log", "-1", "--format=%an",
                             after[self.probe]), IDENTITY[0])
        self.assertEqual(git(srv.dir, "rev-parse", after[self.probe] + "^"),
                         before["main"])
        self.assertEqual(self.platform.requests[1]["state"], "closed")
        self.assertEqual([n["author"]["username"]
                          for n in self.platform.notes[1]], ["ai-planner"])
        self.assertEqual(self.kept_copies(), [])
        self.assertEqual(srv.log("leaked-env.log"), [])
        self.assertNotIn("ОСТАЛАСЬ ИЗМЕНЕНА", self.out)
        self.assertIn("    ответ площадки: remote: " + MAIN_ANSWER,
                      self.out)
        fragment = self.fragment()
        self.assertEqual(fragment["вершина основной ветки"], before["main"])
        self.assertEqual(fragment["пробная ветка"]["ветка"], self.probe)
        self.assertEqual(fragment["пробная ветка"]["правило защиты"],
                         "test/*")
        self.assertIn("не понадобилась", fragment["пробная ветка"]["копия"])
        self.assertEqual(fragment["пробный запрос"],
                         "!1, закрыт после попытки з)")
        self.assertEqual(len(fragment["правила защиты"]["ветки"]), 4)
        self.assertTrue(fragment["кто"].startswith("<"))
        self.assertTrue(fragment["подтверждено"].startswith("<"))
        attempts = fragment["попытки"]
        self.assertEqual([a["попытка"][:2] for a in attempts],
                         ["а)", "б)", "в)", "г)", "д)", "е)", "ж)", "з)"])
        self.assertEqual(attempts[0]["учётная запись"], "ai-dev")
        self.assertIn(ocs.REFUSED_PUSH, attempts[0]["ответ площадки"][0])
        self.assertIn("--force-with-lease=main:", attempts[1]["команда"])
        self.assertEqual(attempts[7]["код"], 401)
        self.assertEqual(attempts[7]["исход"], REFUSED + "; запрос !1 "
                         "закрыт")
        self.assertEqual([k["в проекте"] for k in fragment["ключи"]],
                         ["Developer (30)", "Maintainer (40)",
                          "Reporter (20)"])
        self.assertTrue(all(k["администратор"].startswith("нет")
                            for k in fragment["ключи"]))
        self.assertTrue(fragment["итог"].startswith("всё как ожидалось"))

    def test_without_protection(self):
        """Без защиты: прошедшая обычная отправка а) не откатывается —
        main остаётся изменённой, и в неё запуск больше не пишет (б, з);
        переписанная и удалённая пробная ветка возвращаются из копии;
        прошедшая отправка е) оставляет пробную ветку изменённой; копия
        остаётся; код 1."""
        srv = self.server(force=True, delete=True, main_push=True,
                          planner=True)
        self.platform.merge_allowed = True
        before = srv.heads()
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        passed = [letter for letter, v in self.verdicts()
                  if v == ocs.PASSED_WRONG]
        self.assertEqual(passed, ["а)", "г)", "д)", "е)"])
        self.assertEqual(self.skipped(), ["б)", "з)"])
        self.assertEqual(len(self.restores()), 2)
        self.assertEqual(srv.count("main"), 2)
        self.assertEqual(git(srv.dir, "rev-parse", "main^"), before["main"])
        self.assertEqual(srv.count(self.probe), 3)
        self.assertIn("ВЕТКА main ОСТАЛАСЬ ИЗМЕНЕНА попыткой а)", self.out)
        self.assertIn("ВЕТКА %s ОСТАЛАСЬ ИЗМЕНЕНА попыткой е)" % self.probe,
                      self.out)
        # Совет отменить изменение запросом — только основной ветке; у
        # пробной — оставить изменение.
        advice = dict(re.findall(r"^ВЕТКА (\S+) ОСТАЛАСЬ ИЗМЕНЕНА .+ — "
                                 r"(.+)$", self.out, re.M))
        self.assertEqual(advice, {"main": ocs.REVERT, self.probe: ocs.KEEP})
        self.assertEqual(self.platform.requests[1]["state"], "closed")
        copies = self.kept_copies()
        self.assertEqual(len(copies), 1)
        copy = os.path.join(self.copies, copies[0])
        self.assertEqual(git(copy, "rev-parse", "refs/before/main"),
                         before["main"])
        fragment = self.fragment()
        self.assertIn("возвращены из копии — %s" % self.probe,
                      fragment["пробная ветка"]["копия"])
        self.assertIn("%s: вершина" % self.probe,
                      fragment["пробная ветка"]["копия"])
        self.assertIn(ocs.ADVICE_SHORT[ocs.KEEP],
                      fragment["пробная ветка"]["копия"])
        self.assertIn("ОСТАЛИСЬ ИЗМЕНЕНЫ — main (а)), %s (е))" % self.probe,
                      fragment["итог"])
        self.assertEqual(fragment["пробный запрос"],
                         "!1, закрыт без попытки з)")

    def test_rewrite_restored(self):
        """Защита пропускает переписывание: б) и г) проходят, каждая ветка
        возвращается на вершину копии тем же ключом; копия не нужна
        больше и снята; код 1."""
        srv = self.server(force=True)
        before = srv.heads()
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        passed = [letter for letter, v in self.verdicts()
                  if v == ocs.PASSED_WRONG]
        self.assertEqual(passed, ["б)", "г)"])
        self.assertEqual(len(self.restores()), 2)
        self.assertTrue(all("возвращена" in line for line in self.restores()))
        after = srv.heads()
        self.assertEqual(after["main"], before["main"])
        self.assertEqual(git(srv.dir, "log", "-1", "--format=%s",
                             after[self.probe]), ocs.PROBE_MESSAGE)
        self.assertEqual(self.pushes(), [
            "author", "reviewer", "reviewer", "author", "author", "author",
            "author", "planner"])
        self.assertEqual(self.kept_copies(), [])
        self.assertIn("возвращены из копии — main, %s" % self.probe,
                      self.fragment()["пробная ветка"]["копия"])

    def test_passed_push_not_forced_back(self):
        """Защита пропускает обычную отправку в main: а) проходит, откатить
        её средство не пытается и больше в main не пишет — в main ровно
        одно лишнее изменение."""
        srv = self.server(main_push=True)
        before = srv.heads()
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertEqual(self.restores(), [])
        self.assertEqual(self.skipped(), ["б)", "з)"])
        self.assertEqual(srv.count("main"), 2)
        self.assertEqual(git(srv.dir, "rev-parse", "main^"), before["main"])
        self.assertNotIn("reviewer", self.pushes())
        self.assertEqual(len(self.kept_copies()), 1)

    def test_merge_passes(self):
        """Площадка пропускает слияние з) ключом автора: изменение в main
        не откатывается — main остаётся изменённой."""
        srv = self.server()
        self.platform.merge_allowed = True
        before = srv.heads()
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("з)", ocs.PASSED_WRONG), self.verdicts())
        self.assertIn("ВЕТКА main ОСТАЛАСЬ ИЗМЕНЕНА попыткой з)", self.out)
        self.assertEqual(git(srv.dir, "rev-parse", "main^"), before["main"])
        self.assertEqual(self.platform.requests[1]["state"], "merged")
        self.assertEqual(self.fragment()["пробный запрос"],
                         "!1, СЛИТ попыткой з)")

    def test_request_not_closed(self):
        """Площадка не закрыла пробный запрос: громкая строка, во фрагменте
        — «не закрыт», расхождение, код 1."""
        self.server()
        self.platform.refuse[("PUT", "1")] = (403, "403 Forbidden")
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("з)", REFUSED), self.verdicts())
        self.assertIn("    ЗАПРОС !1 НЕ ЗАКРЫТ — закройте его вручную, не "
                      "удаляя: PUT projects/example%2Fapp/merge_requests/1: "
                      "403 403 Forbidden", self.out.splitlines())
        self.assertEqual(self.platform.requests[1]["state"], "opened")
        fragment = self.fragment()
        self.assertEqual(fragment["попытки"][7]["исход"],
                         REFUSED + "; запрос !1 не закрыт")
        self.assertEqual(fragment["пробный запрос"], "!1, " + (
            "НЕ ЗАКРЫТ — закройте его вручную, не удаляя"))
        self.assertTrue(fragment["итог"].startswith("расхождений 1"))

    def test_rules_not_read(self):
        """Площадка не отдала ключу автора правила защиты: строка с её
        ответом, во фрагменте — «не прочитаны», попытки идут дальше."""
        self.server()
        self.platform.refuse[("GET", "protected_branches")] = (
            403, "403 Forbidden")
        self.assertEqual(self.run_tool(), 0, self.out + self.err)
        self.assertIn("правила защиты не прочитаны: GET "
                      "projects/example%2Fapp/protected_branches: 403 403 "
                      "Forbidden", self.out.splitlines())
        self.assertEqual(len(self.verdicts()), 8)
        fragment = self.fragment()
        self.assertEqual(fragment["правила защиты"], "не прочитаны")
        self.assertEqual(fragment["пробная ветка"]["правило защиты"], "нет")
        tokens = [token for path, token in self.platform.calls
                  if "protected_branches" in path]
        self.assertEqual(tokens, [TOKENS["author"]])

    def test_note_refused(self):
        """Площадка отказала постановщику в заметке ж) — «не прошла»,
        расхождение с её ответом; слияние з) идёт дальше."""
        self.server()
        self.platform.refuse[("POST", "notes")] = (403, "403 Forbidden")
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("ж)", ocs.FAILED_WRONG), self.verdicts())
        self.assertIn(("з)", REFUSED), self.verdicts())
        attempt = self.fragment()["попытки"][6]
        self.assertEqual(attempt["код"], 403)
        self.assertEqual(attempt["учётная запись"], "ai-planner")
        self.assertIn("403 Forbidden", attempt["ответ площадки"][0])
        self.assertEqual(self.platform.notes[1], [])

    def git_fails_after(self, marker, answer):
        """Первая отправка, доводы которой подходят к marker, выполняется по
        правде, а git «возвращает» код 1 и answer — как при обрыве связи
        после записи на площадке."""
        original = ocs.Checker.git
        state = {"done": False}

        def git_call(checker, key, *args):
            code, lines = original(checker, key, *args)
            if not state["done"] and args and args[0] == "push" and \
                    marker(args):
                state["done"] = True
                return 1, list(answer)
            return code, lines
        return mock.patch.object(ocs.Checker, "git", git_call)

    def test_push_passed_but_git_failed(self):
        """Обычная отправка а) записалась на площадке, а git вернул ошибку
        со строкой отказа: по вершине ветки это «ПРОШЛА», ветка осталась
        изменена, в неё больше не пишется, копия оставлена, код 1."""
        srv = self.server(main_push=True)
        before = srv.heads()

        def plain_main(args):
            return args[-1].endswith(":refs/heads/main") and not any(
                a.startswith("--force-with-lease") for a in args)

        with self.git_fails_after(plain_main, [
                "remote: " + MAIN_ANSWER,
                "error: failed to push some refs"]):
            self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("а)", ocs.PASSED_WRONG), self.verdicts())
        self.assertIn("ВЕТКА main ОСТАЛАСЬ ИЗМЕНЕНА попыткой а)", self.out)
        self.assertEqual(srv.count("main"), 2)
        self.assertEqual(git(srv.dir, "rev-parse", "main^"), before["main"])
        self.assertEqual(self.skipped(), ["б)", "з)"])
        self.assertEqual(len(self.kept_copies()), 1)

    def test_rewrite_passed_but_git_failed(self):
        """Принудительная отправка б) переписала main, а git вернул ошибку
        со строкой отказа: по вершине ветки это «ПРОШЛА», и ветка
        возвращается из копии."""
        srv = self.server(force=True)
        before = srv.heads()

        def lease_main(args):
            return any(a.startswith("--force-with-lease=refs/heads/main:")
                       for a in args)

        with self.git_fails_after(lease_main, [
                "remote: GitLab: You are not allowed to force push code to "
                "a protected branch on this project.",
                "error: failed to push some refs"]):
            self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("б)", ocs.PASSED_WRONG), self.verdicts())
        self.assertTrue([line for line in self.restores()
                         if "ветка main возвращена" in line], self.out)
        self.assertEqual(srv.heads()["main"], before["main"])

    def test_refusal_with_other_answer(self):
        """Отказ не тем ответом — ключ не принят, запрос не готов —
        настройку не подтверждает: расхождение, код 1."""
        self.server(main_answer="GitLab: HTTP Basic: Access denied")
        self.platform.refuse[("PUT", "merge")] = (405, "405 Method Not "
                                                       "Allowed")
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        wrong = [letter for letter, v in self.verdicts()
                 if v == ocs.REFUSED_WRONG]
        self.assertEqual(wrong, ["а)", "з)"])
        self.assertTrue(self.fragment()["итог"].startswith("расхождений 2"))

    def test_probe_refused(self):
        """Пробная ветка не отправлена — попытки в ней «не проверялось»,
        расхождение, код 1."""
        self.server(probe=False)
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("в)", ocs.FAILED_WRONG), self.verdicts())
        self.assertEqual(self.skipped(), ["г)", "д)", "е)", "ж)", "з)"])
        self.assertEqual(self.platform.written(), [])

    def test_request_not_created(self):
        """Запрос из пробной ветки не создан — ж) и з) не выполнены,
        расхождение."""
        self.server()
        self.platform.refuse[("POST", "merge_requests")] = (
            403, "403 Forbidden")
        self.assertEqual(self.run_tool(), 1, self.out + self.err)
        done = [m.group(1) for m in map(NOT_DONE_LINE.match,
                                        self.out.splitlines()) if m]
        self.assertEqual(done, ["ж)", "з)"])

    def test_optional_keys(self):
        """Без ключей проверяющего и постановщика б), е), ж) — «не
        проверялось», код 0."""
        self.server()
        self.assertEqual(self.run_tool(roles=("author",)), 0,
                         self.out + self.err)
        self.assertEqual(self.skipped(), ["б)", "е)", "ж)"])
        self.assertIn("б) принудительная отправка в основную ветку main — "
                      "не проверялось: нет ключа проверяющего "
                      "(--maintainer-key)", self.out)
        self.assertEqual([k["учётная запись"] for k in
                          self.fragment()["ключи"]],
                         ["ai-dev", "не подан", "не подан"])

    def test_probe_name_taken(self):
        """Имя пробной ветки занято — берётся следующее, с номером."""
        srv = self.server()
        git(srv.dir, "update-ref", "refs/heads/" + self.probe,
            srv.heads()["main"])
        self.assertEqual(self.run_tool(), 0, self.out + self.err)
        self.assertIn(self.probe + "-2", srv.heads())
        self.assertEqual(srv.heads()[self.probe], srv.heads()["main"])


class ConcurrentTest(SettingsCheckBase):

    def probe_refreshes(self, action, nth, branch=None):
        """Выполнить action после nth-й копии ветки branch, по умолчанию —
        пробной."""
        original = ocs.Checker.refresh
        state = {"n": 0}
        branch = branch or self.probe

        def refresh(checker, name):
            tip = original(checker, name)
            if name == branch:
                state["n"] += 1
                if state["n"] == nth:
                    action(tip)
            return tip
        return mock.patch.object(ocs.Checker, "refresh", refresh)

    def foreign_before_push(self, letter, branch, nth):
        """После nth-й копии ветки branch в неё ложится чужое изменение, и
        обычная отправка letter идёт в ушедшую ветку: git отклоняет её сам,
        до площадки, — исход «не выполнена — ветка ушла», а не «отказ не
        тем ответом»; это расхождение, код 1; чужое изменение на месте, и
        попытка ветку не изменила."""
        srv = self.server()
        state = {}

        def foreign(tip):
            state["sha"] = srv.commit("чужое\n", "docs: чужое", tip)
            git(srv.dir, "update-ref", "refs/heads/" + branch, state["sha"])

        with self.probe_refreshes(foreign, nth, branch):
            self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn((letter, ocs.STALE_WRONG), self.verdicts())
        self.assertEqual([v for _, v in self.verdicts()].count(
            ocs.STALE_WRONG), 1, self.out)
        self.assertNotIn(ocs.REFUSED_WRONG, self.out)
        self.assertEqual(srv.heads()[branch], state["sha"])
        self.assertNotIn("ОСТАЛАСЬ ИЗМЕНЕНА", self.out)
        fragment = self.fragment()
        self.assertTrue(fragment["итог"].startswith("расхождений 1"),
                        fragment["итог"])
        attempt = [a for a in fragment["попытки"]
                   if a["попытка"].startswith(letter)][0]
        self.assertEqual(attempt["исход"], ocs.STALE_WRONG)
        self.assertIn("(fetch first)", " ".join(attempt["ответ площадки"]))
        return srv

    def test_foreign_change_before_main_push(self):
        """Чужое изменение в main после копии и до а): площадка отправку
        не видела, ветка ушла — так и сказано в итоге."""
        self.foreign_before_push("а)", "main", 1)
        self.assertEqual(self.pushes()[0], "reviewer")
        self.assertIn("ВЕРШИНА main УШЛА не попыткой средства", self.out)

    def test_foreign_change_before_planner_push(self):
        """Чужое изменение в пробной ветке после копии и до е): отправка
        постановщика до площадки не дошла."""
        self.foreign_before_push("е)", self.probe, 3)
        self.assertNotIn("planner", self.pushes())

    def test_git_refusal_lines(self):
        """Ушедшую ветку узнают только по отказу самого git; отказ
        площадки — «[remote rejected]» — к нему не относится."""
        self.assertTrue(ocs.behind(
            ["! [rejected]        1234abcd -> main (fetch first)"]))
        self.assertTrue(ocs.behind(
            ["! [rejected]        1234abcd -> main (non-fast-forward)"]))
        # Отказ git по другой причине — не «ветка ушла».
        self.assertFalse(ocs.behind(
            ["! [rejected]        1234abcd -> main (stale info)"]))
        self.assertFalse(ocs.behind(
            ["! [rejected]        v1.0 -> v1.0 (already exists)"]))
        self.assertFalse(ocs.behind(
            ["! [remote rejected] 1234abcd -> main (pre-receive hook "
             "declined)", "error: failed to push some refs to 'x'",
             "hint: Updates were rejected because the remote contains work "
             "that you do"]))

    def test_foreign_change_not_overwritten(self):
        """Между копией и попыткой г) в пробную ветку легло чужое
        изменение: принудительная отправка его не затирает — «не
        выполнена», изменение на месте, хотя защита переписывание
        пропускает."""
        srv = self.server(force=True)
        state = {}

        def foreign(tip):
            state["sha"] = srv.commit("чужое\n", "docs: чужое", tip)
            git(srv.dir, "update-ref", "refs/heads/" + self.probe,
                state["sha"])

        with self.probe_refreshes(foreign, 1):
            self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("г)", ocs.STALE_WRONG), self.verdicts())
        self.assertEqual(srv.heads()[self.probe], state["sha"])
        self.assertNotIn("ОСТАЛАСЬ ИЗМЕНЕНА", self.out)

    def test_deleted_by_others_not_recreated(self):
        """Пробную ветку удалили между копией и попыткой д): исход «не
        выполнена», ветку средство не воссоздаёт и больше в неё не
        пишет."""
        srv = self.server()

        def delete(tip):
            git(srv.dir, "update-ref", "-d", "refs/heads/" + self.probe)

        with self.probe_refreshes(delete, 2):
            self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("д)", ocs.STALE_WRONG), self.verdicts())
        self.assertNotIn(self.probe, srv.heads())
        self.assertEqual(self.restores(), [])
        self.assertEqual(self.skipped(), ["е)", "з)"])
        self.assertIn(ocs.GONE, self.out)
        self.assertEqual(len(self.kept_copies()), 1)

    def crash_after_b(self, error, reason):
        """Защита пропускает принудительную отправку; сразу после
        прошедшей б) — сбой: копия остаётся с вершиной main до попыток,
        код 1."""
        srv = self.server(force=True)
        before = srv.heads()
        original_git, original_heads = ocs.Checker.git, ocs.Checker.heads
        state = {}

        def heads(checker):
            if state:
                raise error
            return original_heads(checker)

        def git_call(checker, key, *args):
            result = original_git(checker, key, *args)
            if any(a.startswith("--force-with-lease=refs/heads/main:")
                   for a in args):
                state["б)"] = True
            return result

        with mock.patch.object(ocs.Checker, "heads", heads), \
                mock.patch.object(ocs.Checker, "git", git_call):
            self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertNotEqual(srv.heads()["main"], before["main"])
        self.assertIn("ЗАПУСК ПРЕРВАН ПОСЛЕ ОТПРАВОК: %s" % reason, self.out)
        self.assertIn(before["main"], self.out)
        copies = self.kept_copies()
        self.assertEqual(len(copies), 1)
        self.assertIn(copies[0], self.out)
        self.assertEqual(git(os.path.join(self.copies, copies[0]),
                             "rev-parse", "refs/heads/main"), before["main"])

    def test_failure_after_push_keeps_copy(self):
        self.crash_after_b(ocs.RunError("площадка не отвечает"),
                           "площадка не отвечает")

    def test_interrupt_after_push_keeps_copy(self):
        self.crash_after_b(KeyboardInterrupt(), "прервано сочетанием Ctrl+C")


class RestoreFailedTest(SettingsCheckBase):
    """Защита пропускает переписывание пробной ветки (г), а возврат из
    копии не выходит: git его не выполняет и возвращает ошибку."""

    def restore_fails(self, foreign=False):
        """При foreign перед несостоявшимся возвратом в ветку ложится чужое
        изменение."""
        original = ocs.Checker.git
        lease = "--force-with-lease=refs/heads/%s:" % self.probe
        state = {"n": 0}

        def git_call(checker, key, *args):
            if args and args[0] == "push" and any(
                    a.startswith(lease) for a in args):
                state["n"] += 1
                if state["n"] == 2:
                    if foreign:
                        state["sha"] = self.srv.commit(
                            "чужое\n", "docs: чужое",
                            self.srv.heads()[self.probe])
                        git(self.srv.dir, "update-ref",
                            "refs/heads/" + self.probe, state["sha"])
                    return 1, ["error: failed to push some refs"]
            return original(checker, key, *args)
        return state, mock.patch.object(ocs.Checker, "git", git_call)

    def summary_lines(self):
        """Строки вывода над фрагментом протокола."""
        return self.out.split("--- фрагмент протокола", 1)[0].splitlines()

    def copy_dir(self):
        copies = self.kept_copies()
        self.assertEqual(len(copies), 1)
        return os.path.join(self.copies, copies[0])

    def test_restore_command(self):
        """Ветка осталась на изменении попытки — совет вернуть её командой;
        команда — в форме AGENTS.md, 6.4, для обеих оболочек, ключ назван,
        и команда, выполненная как напечатана, возвращает ветку."""
        srv = self.server(force=True)
        state, patch = self.restore_fails()
        with patch:
            self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("г)", ocs.PASSED_WRONG), self.verdicts())
        self.assertTrue([line for line in self.restores()
                         if "НЕ ВЫШЕЛ" in line], self.out)
        self.assertIn("ВЕТКА %s ОСТАЛАСЬ ИЗМЕНЕНА попыткой г)" % self.probe,
                      self.out)
        self.assertIn(ocs.RESTORE, self.out)
        self.assertEqual(self.skipped(), ["д)", "е)", "з)"])
        copy = self.copy_dir()
        before = git(copy, "rev-parse", "refs/before/" + self.probe)
        tip = srv.heads()[self.probe]
        self.assertNotEqual(tip, before)
        lines = self.summary_lines()
        key = [x for x in lines if x.startswith("    ключ: ")]
        self.assertEqual(len(key), 1, self.out)
        self.assertIn("файлу своего ключа владельца", key[0])
        self.assertIn("попытку г)", key[0])
        for other in ("--developer-key", "--maintainer-key", "--planner-key",
                      "протокол"):
            self.assertNotIn(other, key[0])
        tail = ('-c http.followRedirects=false -C "%s" push -o ci.skip '
                '--force-with-lease=refs/heads/%s:%s "%s" '
                'refs/before/%s:refs/heads/%s'
                % (copy, self.probe, tip, srv.url, self.probe, self.probe))
        helper = ('!f() { echo username=oauth2; echo "password=$(cat <файл '
                  'ключа>)"; }; f')
        bash = ("    команда, Linux и Git Bash: git -c credential.helper= -c "
                "credential.helper='%s' %s" % (helper, tail))
        shell = ('    команда, Windows PowerShell: git --%% -c '
                 'credential.helper= -c credential.helper="%s" %s'
                 % (helper.replace('"', '\\"'), tail))
        self.assertIn(bash, lines)
        self.assertIn(shell, lines)
        # Команда для Linux и Git Bash — как её разберёт оболочка.
        argv = shlex.split(bash.split(": ", 1)[1])
        self.assertEqual(argv[:7], [
            "git", "-c", "credential.helper=", "-c",
            "credential.helper=" + helper, "-c",
            "http.followRedirects=false"])
        subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=ENV, check=True)
        self.assertEqual(srv.heads()[self.probe], before)

    def test_moved_after_attempt(self):
        """В ветку после попытки легло чужое изменение — команды нет: совет
        восстановить ветку вручную, чужое изменение на месте."""
        srv = self.server(force=True)
        state, patch = self.restore_fails(foreign=True)
        with patch:
            self.assertEqual(self.run_tool(), 1, self.out + self.err)
        self.assertIn(("г)", ocs.PASSED_WRONG), self.verdicts())
        self.assertIn(ocs.MANUAL, self.out)
        self.assertNotIn(ocs.RESTORE, self.out)
        self.assertFalse([x for x in self.summary_lines()
                          if x.startswith(("    ключ: ", "    команда"))],
                         self.out)
        self.assertEqual(srv.heads()[self.probe], state["sha"])
        self.copy_dir()
        self.assertIn(ocs.ADVICE_SHORT[ocs.MANUAL],
                      self.fragment()["пробная ветка"]["копия"])


class ProtocolNameTest(unittest.TestCase):

    def test_next_number(self):
        """Имя файла протокола — дата и следующий номер за день по файлам
        каталога protocols/; каталога нет — номер 1."""
        tmp = tempfile.mkdtemp(prefix="bk-ocs-name-")
        self.addCleanup(helpers.remove_tree, tmp)
        checker = mock.Mock(today=datetime.date(2026, 10, 17))
        with mock.patch.object(ocs.kit, "KIT_DIR", tmp):
            self.assertEqual(ocs.Checker.protocol_name(checker),
                             "2026-10-17-1.yaml")
            folder = os.path.join(tmp, "protocols")
            os.makedirs(folder)
            for name in ("2026-10-17-1.yaml", "2026-10-17-2.yaml",
                         "2026-10-16-3.yaml", "README.md"):
                open(os.path.join(folder, name), "w").close()
            self.assertEqual(ocs.Checker.protocol_name(checker),
                             "2026-10-17-3.yaml")


class NotRunTest(SettingsCheckBase):
    """Код 2: запуск не выполнен до попыток — ни одной отправки, площадка
    не тронута, копий не осталось."""

    def assert_refused(self, reason, **kwargs):
        before = self.srv.heads()
        self.assertEqual(self.run_tool(**kwargs), 2, self.out + self.err)
        self.assertIn(reason, self.err)
        self.assert_untouched(before)

    def test_key_files(self):
        self.server()
        cases = {
            "нет файла": (os.path.join(self.tmp, "нет"),
                          "--developer-key: файл ключа не прочитан"),
            "пустой": (self.key_file("empty", " \r\n"),
                       "--developer-key: файл ключа пуст"),
            "кириллица": (self.key_file("ru", "ключ-root\n"),
                          "--developer-key: в файле не ключ"),
            "метка порядка байтов": (
                self.key_file("bom", chr(0xFEFF) + TOKENS["author"]),
                "--developer-key: в файле не ключ"),
        }
        for name, (path, reason) in cases.items():
            with self.subTest(name):
                self.assert_refused(reason, files={"author": path})
                self.assertNotIn("ключ-root", self.out + self.err)

    def test_one_key_two_roles(self):
        self.server()
        self.assert_refused("один и тот же ключ подан на две роли",
                            files={"reviewer": self.files["author"]})

    def test_accounts(self):
        """Учётная запись ключа не та, что у роли в настройке; уровень в
        проекте не тот; не участник; администратор; ключ не принят."""
        self.server()
        cases = [
            ("чужая учётная запись",
             dict(files={"author": self.files["reviewer"],
                         "reviewer": self.files["author"]}),
             "--developer-key: ключ — учётной записи ai-reviewer, а у роли "
             "«автор» в настройке — ai-dev", None),
            ("уровень Maintainer у автора", {},
             "--developer-key: учётная запись ai-dev в проекте — Maintainer "
             "(40), а нужен ровно Developer (30)",
             lambda: self.platform.member("ai-dev", 40)),
            ("не участник", {},
             "--planner-key: учётная запись ai-planner в проекте — не "
             "участник проекта",
             lambda: self.platform.repo(self.platform.project)
             .members.pop("ai-planner")),
            ("администратор", {},
             "--maintainer-key: учётная запись ai-reviewer — "
             "администратор площадки",
             lambda: self.platform.admins.add("ai-reviewer")),
            ("права администратора без is_admin", {},
             "--maintainer-key: учётная запись ai-reviewer — с правами "
             "администратора",
             lambda: self.platform.settings_readers.add("ai-reviewer")),
            ("ключ не принят",
             dict(files={"planner": self.key_file(
                 "stranger", helpers.TOKENS["stranger"])}),
             "--planner-key: площадка не приняла ключ", None),
        ]
        for name, kwargs, reason, spoil in cases:
            with self.subTest(name):
                saved = dict(self.platform.repo(
                    self.platform.project).members)
                if spoil:
                    spoil()
                try:
                    self.assert_refused(reason, **kwargs)
                finally:
                    self.platform.repo(self.platform.project).members = saved
                    self.platform.admins.clear()
                    self.platform.settings_readers.clear()

    def test_admin_not_known(self):
        """Площадка не ответила, администратор ли: «не проверялось» во
        фрагменте, запуск идёт дальше."""
        self.server()
        self.platform.refuse[("GET", "settings")] = (500, "500 Error")
        self.assertEqual(self.run_tool(), 0, self.out + self.err)
        self.assertTrue(all(k["администратор"].startswith("не проверялось")
                            for k in self.fragment()["ключи"]))

    def test_url_rewrite(self):
        """Адрес площадки переписан настройкой url.*.insteadOf — ключ ушёл
        бы на другой адрес: код 2."""
        srv = self.server()
        with open(os.path.join(self.home, ".gitconfig"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write('[url "%s"]\n\tinsteadOf = %s\n'
                    % (srv.url + "-elsewhere", srv.url))
        self.assert_refused("переписывается настройкой git "
                            "url.*.insteadOf")

    def test_credentials_in_host(self):
        self.server()
        self.assert_refused("в адресе площадки — учётные данные",
                            host="http://oauth2:secret@127.0.0.1")
        self.assertNotIn("secret", self.out + self.err)

    def test_no_signature_and_bad_config(self):
        self.server()
        with mock.patch.object(ocs, "signature", lambda root: None):
            before = self.srv.heads()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = ocs.main(["--developer-key", self.files["author"],
                                 "--config", self.config_path])
        self.assertEqual(code, 2)
        self.assertIn("нет подписи для пробных изменений", err.getvalue())
        self.assert_untouched(before)
        self.cfg["опрос"]["перод"] = 2
        self.write_config(self.cfg)
        self.assert_refused("поле «опрос / перод»: неизвестное поле")

    def test_no_main_branch(self):
        srv = self.server()
        git(srv.dir, "update-ref", "-d", "refs/heads/main")
        git(srv.dir, "update-ref", "refs/heads/other",
            srv.commit("x\n", "docs: x"))
        self.assert_refused("на площадке нет основной ветки main")

    def test_interrupted_before_attempts(self):
        """Ctrl+C до первой отправки — код 2, площадка не тронута."""
        self.server()

        def stop(checker):
            raise KeyboardInterrupt()
        with mock.patch.object(ocs.Checker, "check_url", stop):
            self.assert_refused("запуск прерван до попыток")

    def test_command_line(self):
        """Отдельным процессом, с кодировкой вывода ascii: нет обязательного
        ключа и файл ключа не читается — код 2 с причиной по-русски."""
        clone = helpers.Clone(self.cfg)
        self.addCleanup(clone.remove)
        run = clone.run("owner_check_settings.py")
        self.assertEqual(run.code, 2, run)
        self.assertIn("не заданы обязательные: --developer-key", run.err)
        run = clone.run("owner_check_settings.py", "--developer-key",
                        os.path.join(self.tmp, "нет"))
        self.assertEqual(run.code, 2, run)
        self.assertIn("--developer-key: файл ключа не прочитан", run.err)
        self.assertEqual(self.platform.calls, [])


class NoTestPrefixTest(SettingsCheckBase):
    prefixes = ("docs/", "feat/")

    def test_first_prefix(self):
        """Префикса test/ в настройке нет — пробная ветка берёт первый."""
        srv = self.server()
        self.assertEqual(self.run_tool(), 0, self.out + self.err)
        self.assertTrue(self.probe.startswith("docs/settings-check-"))
        self.assertIn(self.probe, srv.heads())


class GitEnvironmentTest(SettingsCheckBase):

    def test_owner_environment_dropped(self):
        """Трассировка, чужие настройки и каталоги git и ключи из окружения
        владельца до git не доходят: подмена адреса через GIT_CONFIG_* и
        GIT_DIR не действует, трассировки в выводе нет, приёмная сторона
        их не видит. Файла SSLKEYLOGFILE нет и у самого средства."""
        srv = self.server()
        keylog = os.path.join(self.tmp, "keys.log")
        env = {"GIT_TRACE": "1", "GIT_TRACE_PACKET": "1",
               "GIT_CURL_VERBOSE": "1", "GIT_DIR": self.files["author"],
               "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "url.%s-elsewhere.insteadOf" % srv.url,
               "GIT_CONFIG_VALUE_0": srv.url,
               "GITLAB_TOKEN": helpers.TOKENS["owner"],
               "BRANCH_KIT_AUTHOR_TOKEN": TOKENS["author"],
               "SSLKEYLOGFILE": keylog,
               ocs.KEY_ENV: helpers.TOKENS["stranger"]}
        self.assertEqual(self.run_tool(env=env), 0, self.out + self.err)
        self.assertNotIn("trace:", self.out)
        self.assertNotIn("packet:", self.out)
        self.assertEqual(srv.log("leaked-env.log"), [])
        self.assertEqual(len(self.pushes()), 6)
        self.assertFalse(os.path.exists(keylog))

    def test_env_function(self):
        with mock.patch.dict(os.environ, {
                "GIT_TRACE_CURL": "1", "git_trace2": "1",
                "GIT_CONFIG_PARAMETERS": "'http.extraHeader'='X: y'",
                "BRANCH_KIT_AUTHOR_TOKEN": "glpat-LEFTOVER",
                ocs.KEY_ENV: "glpat-LEFTOVER"}):
            env = ocs.git_env("glpat-OWN", IDENTITY,
                              ("BRANCH_KIT_AUTHOR_TOKEN",))
            bare = ocs.git_env(None, IDENTITY)
        self.assertEqual(env[ocs.KEY_ENV], "glpat-OWN")
        self.assertNotIn(ocs.KEY_ENV, bare)
        for name in ("GIT_TRACE_CURL", "git_trace2", "GIT_CONFIG_PARAMETERS",
                     "BRANCH_KIT_AUTHOR_TOKEN"):
            self.assertNotIn(name, env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")


class _Recorder(BaseHTTPRequestHandler):
    """Площадка по http: без ключа — 401 с просьбой о входе, с ключом —
    403; заголовки каждого обращения записываются."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append(dict(self.headers.items()))
        if "Authorization" not in self.headers:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
        else:
            self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()


class HttpHeadersTest(SettingsCheckBase):

    def test_helper_and_headers(self):
        """По http git получает ключ своим помощником из переменной своего
        процесса — помощник и заголовки владельца из настроек git не
        действуют: ни общий http.extraHeader, ни заданный для адреса
        площадки."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        server.seen = []
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host = "http://127.0.0.1:%d" % server.server_address[1]
        with open(os.path.join(self.home, ".gitconfig"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write('[credential]\n\thelper = "!f() { echo username=owner; '
                    'echo password=WRONG-PASSWORD; }; f"\n'
                    '[http]\n\textraHeader = X-Leak-Global: 1\n'
                    '[http "%s"]\n\textraHeader = X-Leak-Host: 1\n'
                    '[http "%s/example/app.git"]\n'
                    '\textraHeader = X-Leak-Repo: 1\n' % (host, host))
        key = ocs.Key("author", "--developer-key", 30, "автора",
                      TOKENS["author"])
        checker = ocs.Checker(self.cfg, host, {"author": key}, IDENTITY,
                              self.tmp)
        checker.copy = os.path.join(self.tmp, "copy")
        git(self.tmp, "init", "--bare", "-q", checker.copy)
        env = helpers.base_env(HOME=self.home,
                               NO_PROXY="127.0.0.1,127.0.0.0/8")
        with mock.patch.dict(os.environ, env, clear=True):
            code, lines = checker.git(key, "ls-remote", checker.url)
        self.assertNotEqual(code, 0)
        self.assertEqual(checker.url, host + "/example/app.git")
        self.assertTrue(server.seen, lines)
        for headers in server.seen:
            self.assertFalse([h for h in headers if h.lower().startswith(
                "x-leak")], headers)
        auth = [h["Authorization"] for h in server.seen
                if "Authorization" in h]
        self.assertEqual(auth, ["Basic " + base64.b64encode(
            ("oauth2:" + TOKENS["author"]).encode()).decode()])
        self.assertNotIn(TOKENS["author"], " ".join(lines))


class _Redirector(BaseHTTPRequestHandler):
    """Площадка по http, которая на каждое обращение отвечает
    переадресацией на другой узел; обращения записываются."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append(dict(self.headers.items()))
        self.send_response(302)
        self.send_header("Location", self.server.target + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET


class RedirectTest(SettingsCheckBase):

    def http(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.seen = []
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_redirect_not_followed(self):
        """Площадка отвечает git переадресацией на другой узел: git за ней
        не идёт, и ключ роли туда не уходит; запуск не выполнен до попыток
        — код 2 с причиной."""
        other = self.http(_Recorder)
        host = self.http(_Redirector)
        host.target = "http://localhost:%d" % other.server_address[1]
        self.server()
        before = self.srv.heads()
        url = "http://127.0.0.1:%d/example/app.git" % host.server_address[1]
        code = self.run_tool(url=url, env={
            "NO_PROXY": "127.0.0.1,localhost,127.0.0.0/8"})
        self.assertEqual(code, 2, self.out + self.err)
        self.assertTrue(host.seen)
        self.assertEqual(other.seen, [])
        self.assertEqual([h for h in host.seen if "Authorization" in h], [])
        self.assertIn("вершины веток не прочитаны", self.err)
        self.assertIn("площадка ответила переадресацией", self.err)
        self.assert_untouched(before)


if __name__ == "__main__":
    unittest.main()
