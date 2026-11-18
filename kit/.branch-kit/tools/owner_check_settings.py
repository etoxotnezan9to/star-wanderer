"""Проверка настройки площадки попыткой — запускает только владелец.

Попытка делается ключом той учётной записи, чьи права проверяются; чужим
ключом работает только владелец и только для этого (AGENTS.md, 6.6).
Средство ничего не настраивает и не удаляет; ключ владельца ему не нужен.

Запуск из корня клона:
    python .branch-kit/tools/owner_check_settings.py --developer-key <файл>
        [--maintainer-key <файл>] [--planner-key <файл>]
Ключи — только из файлов; средство их не печатает и в адрес не пишет, а
git получает ключ своим помощником учётных данных из переменной
окружения, заданной только процессу git (AGENTS.md, 6.3, 6.4).

Попытки и ожидаемые исходы:
  а) отправка в основную ветку ключом автора — отказ;
  б) принудительная отправка в основную ветку ключом проверяющего — отказ;
  в) отправка пробной ветки ключом автора — проходит;
  г) принудительная отправка в пробную ветку ключом автора — отказ;
  д) удаление пробной ветки ключом автора — отказ;
  е) отправка в пробную ветку ключом постановщика — отказ;
  ж) заметка в запросе из пробной ветки ключом постановщика — проходит;
  з) слияние этого запроса ключом автора — отказ; затем запрос
     закрывается, но не удаляется.
Без ключа проверяющего б) — «не проверялось», без ключа постановщика —
е) и ж).

Код выхода: 0 — все попытки как ожидалось; 1 — расхождение, в том числе
попытка, не выполненная из-за ушедшей ветки, или запуск прерван после
первой отправки; 2 — запуск не выполнен до попыток. Кода 130 нет: Ctrl+C
до первой отправки — код 2, после неё — 1. Подробно — README.md рядом.
"""

import datetime
import fnmatch
import os
import shutil
import stat
import subprocess
import sys
import tempfile

# Байт-код общей части не пишется: каталог __pycache__ в клоне сделал
# бы рабочую копию несохранённой.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kit_common as kit  # noqa: E402

TOOL = "owner_check_settings.py"

KEY_ENV = "BRANCH_KIT_CHECK_KEY"
# Помощник учётных данных по AGENTS.md, 6.4: ключ из переменной окружения
# процесса git.
HELPER = ('!f() { echo username=oauth2; echo "password=$%s"; }; f'
          % KEY_ENV)
# Переменные окружения владельца, которые git не получает: трассировка
# выводит заголовки с ключом; чужие настройки, каталоги и программы
# меняют, куда и что отправляется; файл ключей TLS раскрыл бы обмен.
DROPPED_ENV = ("GIT_TRACE", "GIT_CURL_VERBOSE", "GIT_CONFIG", "GIT_DIR",
               "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
               "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
               "GIT_COMMON_DIR", "GIT_REPLACE_REF_BASE", "GIT_GRAFT_FILE",
               "GIT_SHALLOW_FILE", "GIT_EXEC_PATH", "GIT_ASKPASS",
               "SSH_ASKPASS", "GIT_SSH", "GIT_PROXY_COMMAND",
               "GIT_REDIRECT_", "SSLKEYLOGFILE", "GITLAB_TOKEN", KEY_ENV)

REPORTER = 20
DEVELOPER = 30
MAINTAINER = 40
LEVEL_NAMES = {0: "нет доступа", 10: "Guest", 20: "Reporter",
               30: "Developer", 40: "Maintainer", 50: "Owner",
               60: "администратор"}

# (роль в настройке, ключ запуска, уровень в проекте, чей ключ)
ROLES = (
    ("author", "--developer-key", DEVELOPER, "автора"),
    ("reviewer", "--maintainer-key", MAINTAINER, "проверяющего"),
    ("planner", "--planner-key", REPORTER, "постановщика"),
)

# Ответы площадки, которыми она отказывает по защите и правам. Отказ
# другим ответом настройку не подтверждает. У слияния — код ответа API:
# учётная запись ключа уже сверена, и 401 или 403 значит «нет права».
REFUSED_PUSH = "not allowed to push code to protected branches"
REFUSED_FORCE = "not allowed to force push code to a protected branch"
REFUSED_DELETE = "not allowed to delete protected branches"
REFUSED_REPORTER = "not allowed to push code"
REFUSED_MERGE = (401, 403)
STALE = "stale info"  # отказ git по --force-with-lease: ветка ушла
# Отказ git обычной отправке, когда ветка ушла с вершины копии: git
# отклоняет её сам, до площадки, строкой «! [rejected] … (fetch first)»
# или «(non-fast-forward)». Отказ площадки — «! [remote rejected]».
REJECTED = "! [rejected]"
BEHIND = ("(fetch first)", "(non-fast-forward)")

PUSH = "отправка"
FORCE = "принудительная"
DELETE = "удаление"
PASS = "проходит"
REFUSE = "отказ"

AS_EXPECTED = {PASS: "прошла — как ожидалось",
               REFUSE: "отказ — как ожидалось"}
PASSED_WRONG = "ПРОШЛА — НЕ КАК ОЖИДАЛОСЬ"
FAILED_WRONG = "НЕ ПРОШЛА — НЕ КАК ОЖИДАЛОСЬ"
REFUSED_WRONG = "ОТКАЗ НЕ ТЕМ ОТВЕТОМ — НЕ КАК ОЖИДАЛОСЬ"
STALE_WRONG = "НЕ ВЫПОЛНЕНА — ветка ушла с вершины копии"
NOT_DONE = "НЕ ВЫПОЛНЕНА — НЕ КАК ОЖИДАЛОСЬ"

# Что делать с веткой, оставшейся изменённой.
REVERT = "отмените изменение новым запросом и исправьте настройку"
KEEP = ("ветка пробная, её не сливают: изменение можно оставить, исправьте "
        "настройку")
RESTORE = ("верните ветку на вершину копии командой ниже ключом, который "
           "назван при ней, и исправьте настройку")
MANUAL = ("ветка ушла дальше после попытки — восстановите её из копии "
          "вручную, сохранив чужие изменения, и исправьте настройку")
GONE = ("ветку удалили не этой попыткой — разберите вручную по копии и "
        "журналу событий проекта")
ADVICE_SHORT = {REVERT: "изменение отменить новым запросом",
                KEEP: "пробная, изменение можно оставить",
                RESTORE: "вернуть на вершину копии",
                MANUAL: "ушла дальше после попытки, разобрать вручную",
                GONE: "удалена не этой попыткой, разобрать вручную"}
# Помощник для команды возврата, которую владелец выполняет сам: ключ — из
# файла (AGENTS.md, 6.4).
FILE_HELPER = ('!f() { echo username=oauth2; echo "password=$(cat <файл '
               'ключа>)"; }; f')

MESSAGE = ("test: попытка %s проверки настройки (AGENTS.md, 6.6) — не "
           "должна пройти")
PROBE_MESSAGE = "test: пробная ветка проверки настройки попыткой"
MR_TITLE = ("test: проверка настройки попыткой — пробный запрос, не "
            "сливать")
MR_DESCRIPTION = (
    "Пробный запрос средства `.branch-kit/tools/owner_check_settings.py` "
    "(AGENTS.md, 6.6): владелец проверяет настройку площадки попыткой. "
    "Попытка слить его ключом автора должна получить отказ; после неё "
    "запрос закрывается. Не сливать.")
NOTE = ("Проверка настройки попыткой (AGENTS.md, 6.6): заметка ключом "
        "постановщика. Отвечать на неё не нужно.")


class RunError(Exception):
    """Продолжать нельзя: нет ответа площадки, копии ветки или git."""


class Key:
    """Ключ из файла и чья это учётная запись. Ключ не печатается."""

    def __init__(self, role, option, level, whose, token):
        self.role = role
        self.option = option
        self.level = level
        self.whose = whose
        self.token = token
        self.account = None
        self.user_id = None
        self.admin = None


def read_key(path):
    """Ключ из файла и None либо None и причина отказа. Края — пробелы,
    перевод строки, возврат каретки — отбрасываются, как у GITLAB_TOKEN;
    сам ключ и его знаки в причину не попадают."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as err:
        return None, "файл ключа не прочитан: %s" % (
            err.strerror or err.__class__.__name__)
    try:
        token = raw.decode("utf-8").strip(" \t\r\n")
    except UnicodeDecodeError:
        token = None
    if token == "":
        return None, "файл ключа пуст"
    if token is None or not kit.token_valid(token):
        return None, ("в файле не ключ: ключ — только печатные знаки ASCII "
                      "без пробелов; проверьте, что в файле сам ключ и нет "
                      "метки порядка байтов")
    return token, None


def dropped(name, extra=()):
    upper = name.upper()
    return upper.startswith(DROPPED_ENV) or upper in extra


def git_env(token, identity, extra=()):
    """Окружение процесса git: без переменных DROPPED_ENV и extra, с
    подписью пробных изменений и — если задан — с ключом."""
    extra = tuple(e.upper() for e in extra)
    env = dict((name, value) for name, value in os.environ.items()
               if not dropped(name, extra))
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               GIT_AUTHOR_NAME=identity[0], GIT_AUTHOR_EMAIL=identity[1],
               GIT_COMMITTER_NAME=identity[0],
               GIT_COMMITTER_EMAIL=identity[1])
    if token is not None:
        env[KEY_ENV] = token
    return env


def repo_url(host, repo):
    """Адрес хранилища на площадке: адрес площадки и репозиторий из
    настройки."""
    return "%s/%s.git" % (host.rstrip("/"), repo)


def signature(root):
    """Подпись пробных изменений — того, кто запускает: user.name и
    user.email из настроек git клона."""
    values = []
    for name in ("user.name", "user.email"):
        code, out, _ = kit.git(root, "config", name)
        if code != 0 or not out.strip():
            return None
        values.append(out.strip())
    return tuple(values)


def redirect_hint(lines):
    """Пояснение к отказу git, если площадка ответила переадресацией."""
    text = " ".join(lines).lower()
    if any("error: %d" % code in text for code in (301, 302, 303, 307,
                                                   308)):
        return ("; площадка ответила переадресацией, а по ней git не идёт, "
                "чтобы ключ не ушёл на другой узел, — проверьте адрес "
                "площадки")
    return ""


def behind(lines):
    """Обычную отправку git отклонил сам, до площадки: ветка ушла с
    вершины копии."""
    return any(line.startswith(REJECTED) and line.endswith(BEHIND)
               for line in lines)


def short(sha):
    return sha[:8] if sha else "нет ветки"


def now():
    return datetime.datetime.now()


def describe_levels(levels):
    names = []
    for lvl in levels or []:
        if lvl.get("user_id"):
            names.append("пользователь %s" % lvl["user_id"])
        elif lvl.get("group_id"):
            names.append("группа %s" % lvl["group_id"])
        else:
            names.append(lvl.get("access_level_description")
                         or LEVEL_NAMES.get(lvl.get("access_level"),
                                            str(lvl.get("access_level"))))
    return ", ".join(names) or "не задано"


class Checker:
    """Попытки в репозитории из настройки."""

    def __init__(self, cfg, host, keys, identity, root):
        self.cfg = cfg
        self.repo = cfg["площадка"]["репозиторий"]
        self.project = kit.project_path(cfg)
        self.main = cfg["основная ветка"]
        self.host = host
        self.url = repo_url(host, self.repo)
        self.keys = keys
        self.apis = dict((role, kit.Client(host, key.token))
                         for role, key in keys.items())
        self.author = keys["author"]
        self.identity = identity
        self.root = root
        self.extra_env = tuple(kit.key_variables(cfg))
        self.today = datetime.date.today()
        self.copy = None
        self.keep_copy = False
        self.copy_shown = False
        self.pushed = False
        self.finished = False
        self.records = []
        self.restored = []
        self.tainted = {}  # ветка -> (попытка, до попытки, сейчас, совет)
        self.problems = 0
        self.rules = None
        self.rules_time = None
        self.main0 = None
        self.probe = self.probe_sha = None
        self.mr = None
        self.mr_state = None  # что стало с пробным запросом — для фрагмента
        self.start = self.end = None

    # --- git ---------------------------------------------------------------

    def git(self, key, *args):
        """git в копии; ключ — только в окружении процесса git. По
        переадресации git не идёт: помощник отдал бы ключ роли другому
        узлу."""
        cmd = ["git", "-c", "credential.helper=",
               "-c", "credential.helper=" + HELPER,
               "-c", "http.followRedirects=false",
               "-c", "http.extraHeader=",
               "-c", "http.%s.extraHeader=" % self.url, "-C", self.copy]
        try:
            proc = subprocess.run(
                cmd + list(args), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=git_env(key.token if key else None, self.identity,
                            self.extra_env))
        except OSError as err:
            raise RunError("git не запускается: %s" % err)
        text = proc.stdout.decode("utf-8", errors="replace")
        lines = [line.strip() for line in text.splitlines()]
        return proc.returncode, [x for x in lines if x and x != "remote:"]

    def local(self, *args):
        code, lines = self.git(None, *args)
        if code != 0:
            raise RunError("git %s: %s" % (args[0], " | ".join(lines)))
        return lines[0] if lines else ""

    def heads(self):
        """Вершины веток на площадке, прочитанные ключом автора."""
        code, lines = self.git(self.author, "ls-remote", "--heads",
                               self.url)
        if code != 0:
            raise RunError("вершины веток не прочитаны: %s%s"
                           % (" | ".join(lines), redirect_hint(lines)))
        result = {}
        for line in lines:
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                result[ref[len("refs/heads/"):]] = sha
        return result

    def refresh(self, branch):
        """Свежая копия ветки; её вершина."""
        into = "refs/heads/" + branch
        code, lines = self.git(self.author, "fetch", "-q", self.url,
                               "+%s:%s" % (into, into))
        if code != 0:
            raise RunError("копия ветки %s не снята: %s"
                           % (branch, " | ".join(lines)))
        return self.local("rev-parse", into)

    def commit(self, tip, text, sibling=False):
        """Пустое изменение поверх вершины, а при sibling — рядом с ней, на
        тех же родителях: его отправка переписывает историю."""
        tree = self.local("rev-parse", tip + "^{tree}")
        parents = [tip]
        if sibling:
            parents = self.local("rev-list", "--parents", "-n", "1",
                                 tip).split()[1:]
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        return self.local(*(args + ["-m", text]))

    # --- запись и вывод ----------------------------------------------------

    def record(self, label, key, command, expected, refused, when, code,
               answer):
        rec = {"попытка": label, "ключ": key.whose,
               "учётная запись": key.account, "когда":
               when.strftime("%H:%M:%S"), "команда": command,
               "ожидается": expected, "код": code,
               "ответ площадки": answer, "исход": ""}
        if refused:
            rec["ожидается"] += "; засчитывается ответ «%s»" % refused
        self.records.append(rec)
        return rec

    def show(self, rec, verdict):
        print("[%s] %s — ключ %s %s — код %s — %s"
              % (rec["когда"], rec["попытка"], rec["ключ"],
                 rec["учётная запись"], rec["код"], verdict), flush=True)
        print("    ответ площадки: %s" % (" | ".join(rec["ответ площадки"])
                                          or "нет"), flush=True)

    def skip(self, label, reason, problem=False):
        self.records.append({"попытка": label,
                             "исход": "не проверялось: " + reason})
        print("%s — не проверялось: %s" % (label, reason), flush=True)
        if problem:
            self.problems += 1

    def judge(self, rec, expected, passed, refused_right, stale=False):
        """Исход попытки; True — как ожидалось."""
        if expected == PASS:
            ok = passed
            verdict = AS_EXPECTED[PASS] if ok else FAILED_WRONG
        elif passed:
            ok, verdict = False, PASSED_WRONG
        elif stale:
            ok, verdict = False, STALE_WRONG
        elif refused_right:
            ok, verdict = True, AS_EXPECTED[REFUSE]
        else:
            ok, verdict = False, REFUSED_WRONG
        rec["исход"] = verdict
        if not ok:
            self.problems += 1
        self.show(rec, verdict)
        return ok

    def letter(self, rec):
        return rec["попытка"].split(" ", 1)[0]

    # --- попытки -----------------------------------------------------------

    def skip_tainted(self, label, branch):
        """Ветка осталась изменённой — в неё запуск больше не пишет."""
        if branch not in self.tainted:
            return False
        self.skip(label, "ветка %s осталась изменена попыткой %s — в неё "
                  "запуск больше не пишет" % (branch,
                                              self.tainted[branch][0]))
        return True

    def fresh(self, label, branch):
        """Вершина свежей копии ветки для попытки; None — попытка
        пропущена, потому что ветка осталась изменённой."""
        if self.skip_tainted(label, branch):
            return None
        return self.refresh(branch)

    def push(self, label, key, branch, target, refused, mode=PUSH,
             expected=REFUSE, before=None):
        """Попытка отправки. before — вершина копии: на неё опирается
        --force-with-lease, и на неё ветка возвращается, если попытка
        переписать или удалить ветку прошла. Ветка ушла с вершины копии —
        попытка не выполнена: принудительную отправку и удаление
        останавливает --force-with-lease, обычную отправку git отклоняет
        сам; площадка такой попытки не видит."""
        if self.skip_tainted(label, branch):
            return False
        args = ["push", "-o", "ci.skip"]
        if mode in (FORCE, DELETE):
            args.append("--force-with-lease=refs/heads/%s:%s"
                        % (branch, before))
            lease = "--force-with-lease=%s:%s " % (branch, short(before))
        else:
            lease = ""
        if mode == DELETE:
            args += [self.url, "--delete", "refs/heads/" + branch]
            shown = "git push -o ci.skip %sorigin --delete %s" % (lease,
                                                                  branch)
        else:
            args += [self.url, "%s:refs/heads/%s" % (target, branch)]
            shown = "git push -o ci.skip %sorigin %s:%s" % (
                lease, short(target), branch)
        when = now()
        self.pushed = True
        code, answer = self.git(key, *args)
        after = self.heads().get(branch)
        if mode == DELETE:
            passed = code == 0
        elif expected == PASS:
            passed = code == 0 and after == target
        else:
            passed = code == 0 or after == target
        # Ветки нет, а удаление не прошло: её удалили не этой попыткой.
        gone = mode == DELETE and code != 0 and after is None
        rec = self.record(label, key, shown, expected, refused, when, code,
                          answer)
        text = " ".join(answer).lower()
        ok = self.judge(rec, expected, passed,
                        bool(refused) and refused in text,
                        STALE in text or gone or (mode == PUSH
                                                  and behind(answer)))
        if gone:
            self.taint(rec, branch, before, None, GONE)
        if not ok and passed and expected == REFUSE:
            if mode == PUSH:
                self.taint(rec, branch, before, after,
                           KEEP if branch == self.probe else REVERT)
            elif after == target:
                self.restore(rec, key, branch, before, after)
            else:
                self.taint(rec, branch, before, after, MANUAL)
        return ok

    def restore(self, rec, key, branch, before, after):
        """Вернуть ветку на вершину копии тем же ключом, только если на
        площадке стоит изменение попытки (или ветки нет после
        удаления)."""
        code, answer = self.git(
            key, "push", "-o", "ci.skip",
            "--force-with-lease=refs/heads/%s:%s" % (branch, after or ""),
            self.url, "%s:refs/heads/%s" % (before, branch))
        tip = self.heads().get(branch)
        done = tip == before
        print("    возврат из копии: код %d — %s; ответ площадки: %s"
              % (code, "ветка %s возвращена на %s" % (branch, short(before))
                 if done else "НЕ ВЫШЕЛ", " | ".join(answer) or "нет"),
              flush=True)
        if done:
            if branch not in self.restored:
                self.restored.append(branch)
            rec["исход"] += "; ветка %s возвращена из копии на %s" % (
                branch, short(before))
        else:
            # Ветка ушла с изменения попытки — легло чужое: возврат его
            # затёр бы, поэтому без команды.
            self.taint(rec, branch, before, tip,
                       RESTORE if tip == after else MANUAL)

    def taint(self, rec, branch, before, tip, advice):
        """Ветка осталась изменённой: громкая строка, копия остаётся, в
        ветку запуск больше не пишет."""
        letter = self.letter(rec)
        self.tainted[branch] = (letter, before, tip, advice)
        self.keep_copy = True
        if before:
            self.local("update-ref", "refs/before/" + branch, before)
        print("    ВЕТКА %s ОСТАЛАСЬ ИЗМЕНЕНА: вершина %s, до попытки %s — "
              "%s; в эту ветку запуск больше не пишет"
              % (branch, short(tip), short(before), advice), flush=True)
        rec["исход"] += "; ветка %s осталась изменена: вершина %s" % (
            branch, short(tip))

    def open_request(self):
        """Запрос из пробной ветки для попыток ж) и з), ключом автора;
        None и причина, если запрос не создан."""
        when = now().strftime("%H:%M:%S")
        try:
            mr = self.apis["author"].post(self.project + "/merge_requests", {
                "source_branch": self.probe, "target_branch": self.main,
                "title": MR_TITLE, "description": MR_DESCRIPTION,
                "remove_source_branch": False, "squash": False}) or {}
        except kit.ApiError as err:
            reason = "запрос из пробной ветки не создан: %s" % err
            print("[%s] %s" % (when, reason), flush=True)
            return None, reason
        self.mr = mr.get("iid")
        print("[%s] запрос !%s из ветки %s создан ключом автора %s"
              % (when, self.mr, self.probe, self.author.account), flush=True)
        return self.mr, None

    def not_done(self, label, reason):
        self.records.append({"попытка": label,
                             "исход": "%s; %s" % (NOT_DONE, reason)})
        print("%s — %s; %s" % (label, NOT_DONE, reason), flush=True)
        self.problems += 1

    def note_attempt(self):
        """ж) заметка ключом постановщика — проходит."""
        label = "ж) заметка в запросе из пробной ветки"
        key = self.keys["planner"]
        path = "%s/merge_requests/%s/notes" % (self.project, self.mr)
        when = now()
        try:
            note = self.apis["planner"].post(path, {"body": NOTE}) or {}
            code, answer = 201, ["заметка %s создана" % note.get("id")]
            passed = True
        except kit.ApiError as err:
            code, passed = err.code, False
            answer = [str(err)]
        rec = self.record(label, key, "POST merge_requests/%s/notes"
                          % self.mr, PASS, None, when, code, answer)
        self.judge(rec, PASS, passed, False)

    def merge_attempt(self):
        """з) слияние ключом автора — отказ; затем запрос закрывается."""
        label = "з) слияние запроса из пробной ветки"
        for branch in (self.main, self.probe):
            if branch in self.tainted:
                self.skip(label, "ветка %s осталась изменена попыткой %s"
                          % (branch, self.tainted[branch][0]))
                self.close_request(None, "без попытки з)")
                return
        api = self.apis["author"]
        before = self.refresh(self.main)
        path = "%s/merge_requests/%s" % (self.project, self.mr)
        when = now()
        try:
            api.put(path + "/merge", {"sha": self.probe_sha,
                                      "squash": False,
                                      "should_remove_source_branch": False})
            code, answer, passed = 200, ["запрос слит"], True
        except kit.ApiError as err:
            code, passed = err.code, False
            answer = [str(err)]
        if not passed:
            try:
                passed = (api.get(path) or {}).get("state") == "merged"
            except kit.ApiError:
                pass
        rec = self.record(label, self.author, "PUT merge_requests/%s/merge"
                          % self.mr, REFUSE, None, when, code, answer)
        rec["ожидается"] += ("; засчитывается код ответа %s"
                             % " или ".join(map(str, REFUSED_MERGE)))
        ok = self.judge(rec, REFUSE, passed, code in REFUSED_MERGE)
        if passed:
            self.mr_state = "СЛИТ попыткой з)"
            self.taint(rec, self.main, before, self.heads().get(self.main),
                       REVERT)
            return
        self.close_request(rec if ok else None, "после попытки з)")

    def close_request(self, rec, when):
        """Пробный запрос закрывается, но не удаляется."""
        path = "%s/merge_requests/%s" % (self.project, self.mr)
        try:
            state = (self.apis["author"].put(
                path, {"state_event": "close"}) or {}).get("state")
            reason = "площадка ответила состоянием %s" % state
        except kit.ApiError as err:
            state, reason = None, str(err)
        if state == "closed":
            self.mr_state = "закрыт " + when
            print("    запрос !%s закрыт" % self.mr, flush=True)
            if rec is not None:
                rec["исход"] += "; запрос !%s закрыт" % self.mr
        else:
            self.mr_state = "НЕ ЗАКРЫТ — закройте его вручную, не удаляя"
            print("    ЗАПРОС !%s НЕ ЗАКРЫТ — закройте его вручную, не "
                  "удаляя: %s" % (self.mr, reason), flush=True)
            self.problems += 1
            if rec is not None:
                rec["исход"] += "; запрос !%s не закрыт" % self.mr

    # --- сверки до попыток -------------------------------------------------

    def account(self, key):
        """Учётная запись ключа — та, что у роли в настройке; в проекте —
        ровно нужный уровень; не администратор."""
        api = self.apis[key.role]
        try:
            user = api.get("user") or {}
        except kit.ApiError as err:
            raise RunError("%s: площадка не приняла ключ: %s"
                           % (key.option, err))
        key.account = user.get("username")
        key.user_id = user.get("id")
        allowed = kit.role_accounts(self.cfg, key.role)
        if key.account not in allowed:
            raise RunError(
                "%s: ключ — учётной записи %s, а у роли «%s» в настройке — "
                "%s" % (key.option, key.account, kit.ROLE_NAMES[key.role],
                        ", ".join(allowed)))
        key.admin = self.admin(key, api, user)
        try:
            member = api.get("%s/members/all/%s" % (self.project,
                                                    key.user_id)) or {}
            level = member.get("access_level")
        except kit.ApiError as err:
            if err.code != 404:
                raise RunError("%s: уровень учётной записи %s в проекте не "
                               "прочитан: %s" % (key.option, key.account,
                                                 err))
            level = None
        if level != key.level:
            raise RunError(
                "%s: учётная запись %s в проекте — %s, а нужен ровно %s "
                "(%d): с другим уровнем попытка проверила бы не то"
                % (key.option, key.account,
                   "не участник проекта" if level is None else "%s (%s)" % (
                       LEVEL_NAMES.get(level, "уровень"), level),
                   LEVEL_NAMES[key.level], key.level))
        print("ключ %s (%s): учётная запись %s, в проекте %s (%d); "
              "администратор — %s" % (key.whose, key.option, key.account,
                                      LEVEL_NAMES[key.level], key.level,
                                      key.admin), flush=True)

    def admin(self, key, api, user):
        """Не администратор. Поле is_admin площадка показывает только
        администратору, поэтому сверка — по настройкам площадки: их
        читает только администратор."""
        if user.get("is_admin"):
            raise RunError("%s: учётная запись %s — администратор площадки"
                           % (key.option, key.account))
        try:
            api.get("application/settings")
        except kit.ApiError as err:
            if err.code == 403:
                return "нет: настройки площадки ключу не видны (403)"
            print("ключ %s: администратор ли — не проверялось: %s"
                  % (key.whose, err), flush=True)
            return "не проверялось: %s" % err
        raise RunError("%s: учётная запись %s — с правами администратора: "
                       "площадка отдала ей свои настройки"
                       % (key.option, key.account))

    def accounts(self):
        """Ключи ролей. Одной учётной записи на две роли не бывает: у каждой
        роли — свой точный уровень в проекте."""
        for role, option, _, whose in ROLES:
            key = self.keys.get(role)
            if key is None:
                print("ключ %s (%s) не подан — %s"
                      % (whose, option, "попытка б) не проверяется"
                         if role == "reviewer" else
                         "попытки е) и ж) не проверяются"), flush=True)
                continue
            self.account(key)

    def check_url(self):
        """Адрес площадки не переписывается настройкой url.*.insteadOf:
        иначе ключ ушёл бы на другой адрес."""
        self.local("remote", "add", "origin", self.url)
        for args in (("get-url",), ("get-url", "--push")):
            got = self.local("remote", *(args + ("origin",)))
            if got != self.url:
                raise RunError("адрес площадки переписывается настройкой "
                               "git url.*.insteadOf (git remote %s: %s)"
                               % (" ".join(args), got))

    def read_rules(self):
        try:
            self.rules = self.apis["author"].get_all(
                self.project + "/protected_branches")
            self.rules_time = now()
        except kit.ApiError as err:
            print("правила защиты не прочитаны: %s" % err, flush=True)

    def probe_name(self, heads):
        prefixes = [p["префикс"] for p in self.cfg["ветки задач"]]
        prefix = "test/" if "test/" in prefixes else prefixes[0]
        base = "%ssettings-check-%s" % (prefix, self.today.isoformat())
        name, number = base, 2
        while name in heads:
            name, number = "%s-%d" % (base, number), number + 1
        return name

    # --- ход ---------------------------------------------------------------

    def run(self):
        print("репозиторий: %s; площадка: %s" % (self.repo, self.host),
              flush=True)
        try:
            self.accounts()
            self.read_rules()
            self.copy = tempfile.mkdtemp(prefix="branch-kit-settings-check-")
            self.local("init", "--bare", "-q")
            self.check_url()
            self.attempts()
            self.summary()
            self.finished = True
        except (RunError, kit.ToolError, kit.ApiError) as err:
            if not self.pushed:
                print("запуск не выполнен: %s; попытки не выполнялись"
                      % err, file=sys.stderr)
                return kit.NOT_DONE
            self.interrupted(err)
            return kit.FOUND
        except KeyboardInterrupt:
            if not self.pushed:
                print("запуск прерван до попыток: ни одной отправки не "
                      "было", file=sys.stderr)
                return kit.NOT_DONE
            self.interrupted("прервано сочетанием Ctrl+C")
            return kit.FOUND
        except BaseException as err:
            # Любой другой сбой после отправки: сказать, что ветки могли
            # остаться изменены, и пробросить дальше.
            if self.pushed:
                self.interrupted(err.__class__.__name__)
            raise
        finally:
            self.cleanup()
        return kit.FOUND if self.problems else kit.OK

    def interrupted(self, reason):
        self.keep_copy = True
        heads = ["%s %s" % (self.main, self.main0)]
        if self.probe_sha:
            heads.append("пробная ветка %s — %s, если отправлена"
                         % (self.probe, self.probe_sha))
        print("ЗАПУСК ПРЕРВАН ПОСЛЕ ОТПРАВОК: %s. Ветки могли остаться "
              "изменены — сверьте их на площадке с вершинами до попыток: "
              "%s; копия: %s" % (reason, "; ".join(heads), self.copy),
              flush=True)
        self.copy_shown = True

    def attempts(self):
        heads = self.heads()
        if self.main not in heads:
            raise RunError("на площадке нет основной ветки %s" % self.main)
        self.main0 = self.refresh(self.main)
        self.start = now()
        print("копия снята: %s %s" % (self.main, self.main0), flush=True)
        self.probe = self.probe_name(heads)
        author = self.author
        main = self.main

        self.push("а) отправка в основную ветку %s" % main, author, main,
                  self.commit(self.main0, MESSAGE % "а)"), REFUSED_PUSH,
                  before=self.main0)

        label = "б) принудительная отправка в основную ветку %s" % main
        reviewer = self.keys.get("reviewer")
        if reviewer is None:
            self.skip(label, "нет ключа проверяющего (--maintainer-key)")
        else:
            before = self.fresh(label, main)
            if before:
                self.push(label, reviewer, main,
                          self.commit(before, MESSAGE % "б)", sibling=True),
                          REFUSED_FORCE, FORCE, before=before)

        base = self.refresh(main) if main not in self.tainted else \
            self.main0
        self.probe_sha = self.commit(base, PROBE_MESSAGE)
        pushed = self.push("в) отправка пробной ветки %s" % self.probe,
                           author, self.probe, self.probe_sha, None,
                           expected=PASS)
        if pushed:
            print("    пробная ветка %s остаётся на площадке: ветки не "
                  "удаляют (AGENTS.md, 3.4)" % self.probe, flush=True)
            self.probe_attempts()
        else:
            for label in ("г) принудительная отправка в пробную ветку",
                          "д) удаление пробной ветки",
                          "е) отправка в пробную ветку",
                          "ж) заметка в запросе из пробной ветки",
                          "з) слияние запроса из пробной ветки"):
                self.skip(label, "пробная ветка не отправлена")
        self.end = now()

    def probe_attempts(self):
        author, probe = self.author, self.probe
        label = "г) принудительная отправка в пробную ветку"
        before = self.fresh(label, probe)
        if before:
            self.push(label, author, probe,
                      self.commit(before, MESSAGE % "г)", sibling=True),
                      REFUSED_FORCE, FORCE, before=before)
        label = "д) удаление пробной ветки"
        before = self.fresh(label, probe)
        if before:
            self.push(label, author, probe, None, REFUSED_DELETE, DELETE,
                      before=before)
        planner = self.keys.get("planner")
        label = "е) отправка в пробную ветку"
        if planner is None:
            self.skip(label, "нет ключа постановщика (--planner-key)")
        else:
            before = self.fresh(label, probe)
            if before:
                self.push(label, planner, probe,
                          self.commit(before, MESSAGE % "е)"),
                          REFUSED_REPORTER, before=before)
        mr, reason = self.open_request()
        label = "ж) заметка в запросе из пробной ветки"
        if planner is None:
            self.skip(label, "нет ключа постановщика (--planner-key)")
        elif mr is None:
            self.not_done(label, reason)
        else:
            self.note_attempt()
        if mr is None:
            self.not_done("з) слияние запроса из пробной ветки", reason)
        else:
            self.merge_attempt()

    def cleanup(self):
        """Копия удаляется, только если ничего не отправлено или запуск
        дошёл до конца и все ветки на своих местах."""
        if self.copy is None:
            return
        if self.keep_copy or (self.pushed and not self.finished):
            if not self.copy_shown:
                print("копия оставлена: %s" % self.copy, flush=True)
            return

        def unlock(func, path, _):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        shutil.rmtree(self.copy, onerror=unlock)

    # --- итог и фрагмент протокола -----------------------------------------

    def summary(self):
        heads = self.heads()
        for branch, (letter, before, tip, advice) in self.tainted.items():
            print("ВЕТКА %s ОСТАЛАСЬ ИЗМЕНЕНА попыткой %s: вершина %s, до "
                  "попытки %s — %s" % (branch, letter, short(tip),
                                       short(before), advice))
            if before:
                print("    копия: refs/before/%s в %s" % (branch, self.copy))
            if advice == RESTORE:
                self.show_restore(branch, tip)
        expected = self.main0
        if self.main in self.tainted:
            expected = self.tainted[self.main][2]
        if heads.get(self.main) != expected:
            print("ВЕРШИНА %s УШЛА не попыткой средства: ожидалась %s, на "
                  "площадке %s — сверьте журнал ветки"
                  % (self.main, short(expected),
                     short(heads.get(self.main))))
        print("вершины после попыток: %s %s; пробная ветка %s %s"
              % (self.main, heads.get(self.main), self.probe,
                 heads.get(self.probe) or "нет"))
        total = self.overall()
        print("итог: %s" % total)
        if self.keep_copy:
            print("копия оставлена: %s" % self.copy)
            self.copy_shown = True
        print()
        print("--- фрагмент протокола для .branch-kit/protocols/%s ---"
              % self.protocol_name())
        print(self.fragment(total), end="", flush=True)

    def show_restore(self, branch, tip):
        """Команда возврата ветки, которую владелец выполняет сам, — в форме
        AGENTS.md, 6.4: у копии своих настроек нет, и без своего помощника
        git пошёл бы к площадке через помощник учётных данных машины, то
        есть чужой учётной записью, и за переадресацией. Ключ — свой ключ
        владельца: чужим ключом вне средства не работают (AGENTS.md, 6.6),
        а своего права хватает — совет вернуть ветку бывает только после
        того, как защита пропустила принудительную отправку или удаление
        ключом автора или проверяющего."""
        print("    ключ: в <файл ключа> подставьте путь к файлу своего ключа "
              "владельца, через «/» (AGENTS.md, 6.4); чужой ключ не нужен: "
              "защита пропустила попытку %s ключом роли, а права владельца "
              "не ниже" % self.tainted[branch][0])
        tail = ('-c http.followRedirects=false -C "%s" push -o ci.skip '
                '--force-with-lease=refs/heads/%s:%s "%s" '
                'refs/before/%s:refs/heads/%s'
                % (self.copy, branch, tip or "", self.url, branch, branch))
        print("    команда, Linux и Git Bash: git -c credential.helper= -c "
              "credential.helper='%s' %s" % (FILE_HELPER, tail))
        # PowerShell 5.1 теряет пустое значение и кавычки внутри помощника:
        # после --% строка идёт в git как есть, кавычки — с обратной косой.
        print('    команда, Windows PowerShell: git --%% -c '
              'credential.helper= -c credential.helper="%s" %s'
              % (FILE_HELPER.replace('"', '\\"'), tail))

    def protocol_name(self):
        """Имя файла протокола: дата и следующий номер за день по файлам,
        которые уже есть в клоне (protocols/README.md)."""
        day = self.today.isoformat()
        folder = os.path.join(kit.KIT_DIR, "protocols")
        number = 1
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        while "%s-%d.yaml" % (day, number) in names:
            number += 1
        return "%s-%d.yaml" % (day, number)

    def overall(self):
        """Итог: общий вывод, попытки по исходам и ветки, которые остались
        изменены."""
        groups = {}
        for rec in self.records:
            kind = rec["исход"].split(";", 1)[0].split(":", 1)[0]
            names = groups.setdefault(kind, [])
            letter = self.letter(rec)
            if letter not in names:
                names.append(letter)
        head = ("всё как ожидалось" if not self.problems
                else "расхождений %d" % self.problems)
        parts = ["%s — %s" % (kind, ", ".join(names))
                 for kind, names in groups.items()]
        if self.tainted:
            parts.append("ОСТАЛИСЬ ИЗМЕНЕНЫ — " + ", ".join(
                "%s (%s)" % (branch, letter)
                for branch, (letter, _, _, _) in self.tainted.items()))
        return head + "; " + "; ".join(parts)

    def copy_text(self):
        """Поле «копия» фрагмента — по тому, что случилось с ветками."""
        parts = []
        if self.restored:
            parts.append("понадобилась: возвращены из копии — "
                         + ", ".join(self.restored))
        if self.tainted:
            parts.append("остались изменены — " + "; ".join(
                "%s: вершина %s, до попытки %s, %s" % (
                    branch, short(tip), short(before), ADVICE_SHORT[advice])
                for branch, (_, before, tip, advice)
                in self.tainted.items()))
        return "снималась перед каждой попыткой; %s" % (
            "; ".join(parts) or
            "не понадобилась: ни одна попытка не изменила ветки")

    def fragment(self, total):
        probe_rule = "нет"
        rules = "не прочитаны"
        if self.rules is not None:
            rules = {"получено": self.rules_time.strftime(
                "%Y-%m-%d %H:%M:%S"), "ветки": []}
            for rule in self.rules:
                rules["ветки"].append({
                    "ветка": rule.get("name"),
                    "отправка": describe_levels(
                        rule.get("push_access_levels")),
                    "слияние": describe_levels(
                        rule.get("merge_access_levels")),
                    "принудительная отправка": bool(
                        rule.get("allow_force_push"))})
                if probe_rule == "нет" and fnmatch.fnmatchcase(
                        self.probe, rule.get("name") or ""):
                    probe_rule = rule.get("name")
        keys = []
        for role, option, _, whose in ROLES:
            key = self.keys.get(role)
            if key is None:
                keys.append({"ключ": option, "чей": whose,
                             "учётная запись": "не подан"})
                continue
            keys.append({"ключ": option, "чей": whose,
                         "учётная запись": key.account,
                         "в проекте": "%s (%d)" % (LEVEL_NAMES[key.level],
                                                   key.level),
                         "администратор": key.admin})
        head = kit.head(self.root)
        kit_cfg = self.cfg["комплект"]
        data = {
            "что проверено": "настройка площадки попыткой (AGENTS.md, 6.6)",
            "репозиторий": self.repo,
            "площадка": self.host,
            "кто": "<учётная запись владельца, который запускал средство>",
            "подтверждено": "<чем подтверждены исходы — "
                            ".branch-kit/protocols/README.md>",
            "когда": "%s, %s–%s" % (self.today.isoformat(),
                                     self.start.strftime("%H:%M"),
                                     self.end.strftime("%H:%M")),
            "средство": "%s, комплект %s, изменение %s; вершина клона %s"
                        % (TOOL, kit_cfg["источник"], kit_cfg["изменение"],
                           short(head)),
            "вершина основной ветки": self.main0,
            "ключи": keys,
            "правила защиты": rules,
            "пробная ветка": {
                "ветка": self.probe,
                "правило защиты": probe_rule,
                "пробное изменение": "%s, пустое, поверх вершины %s; "
                                     "подпись — %s" % (
                                         self.probe_sha, self.main,
                                         self.identity[0]),
                "копия": self.copy_text()},
            "пробный запрос": ("!%s, %s" % (self.mr, self.mr_state)
                               if self.mr else "не создан"),
            "попытки": self.records,
            "итог": total,
        }
        return kit.yaml.dump(data, Dumper=_Dumper, allow_unicode=True,
                             sort_keys=False, width=72)


if kit.yaml is not None:
    class _Dumper(kit.yaml.SafeDumper):
        """Списки — с отступом под ключом."""

        def increase_indent(self, flow=False, indentless=False):
            return super(_Dumper, self).increase_indent(flow, False)


# --- Запуск -----------------------------------------------------------------

def parse(argv):
    parser = kit.make_parser(
        TOOL,
        "Проверка настройки площадки попыткой — запускает только "
        "владелец; ключи ролей — только из файлов.")
    parser.add_argument("--developer-key", required=True, metavar="<файл>",
                        help="файл ключа учётной записи автора (Developer)")
    parser.add_argument("--maintainer-key", metavar="<файл>",
                        help="файл ключа проверяющего (Maintainer); без "
                             "него попытка б) — «не проверялось»")
    parser.add_argument("--planner-key", metavar="<файл>",
                        help="файл ключа постановщика (Reporter); без него "
                             "попытки е) и ж) — «не проверялось»")
    parser.add_argument("--host", metavar="<адрес>",
                        help="другая площадка вместо поля «площадка», "
                             "«адрес» настройки")
    kit.add_config_argument(parser)
    return parser.parse_args(argv)


def read_keys(args):
    keys = {}
    for role, option, level, whose in ROLES:
        path = getattr(args, option[2:].replace("-", "_"))
        if path is None:
            continue
        token, problem = read_key(path)
        if problem:
            raise kit.ToolError("%s: %s" % (option, problem))
        keys[role] = Key(role, option, level, whose, token)
    tokens = [key.token for key in keys.values()]
    if len(set(tokens)) != len(tokens):
        raise kit.ToolError("один и тот же ключ подан на две роли: попытки "
                            "ничего не доказали бы")
    return keys


def main(argv=None):
    kit.setup_output()
    args = parse(argv)
    try:
        keys = read_keys(args)
        cfg = kit.load_config(args.config)
        host = kit.platform_host(cfg, args.host)
        root = kit.clone_root()
        identity = signature(root)
        if identity is None:
            raise kit.ToolError("нет подписи для пробных изменений: задайте "
                                "user.name и user.email в настройках git "
                                "клона (AGENTS.md, 6.4)")
        checker = Checker(cfg, host, keys, identity, root)
    except kit.ToolError as err:
        return kit.report(err)
    except KeyboardInterrupt:
        print("запуск прерван до попыток: ни одной отправки не было",
              file=sys.stderr)
        return kit.NOT_DONE
    return checker.run()


if __name__ == "__main__":
    sys.exit(main())
