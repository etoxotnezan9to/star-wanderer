"""Журнал слияний основной ветки: она менялась только по правилам разделов
2 и 3 AGENTS.md.

Запуск из корня клона, ключ — в GITLAB_TOKEN на одну команду:
    python .branch-kit/tools/check_merge_log.py
Средство только читает: первую линию основной ветки, слитые запросы, их
утверждения и заметки. Вывод — строка на нарушение «<где>: <вид>: <что не
так>», где — «!N» у запроса или первые 8 знаков изменения; последняя —
«нарушений N». Код выхода: 0 — нарушений нет; 1 — есть; 2 — проверка не
выполнена; 130 — прервана Ctrl+C. Подробно — README.md рядом.
"""

import os
import re
import sys
import urllib.parse

# Байт-код общей части не пишется: каталог __pycache__ в клоне сделал
# бы рабочую копию несохранённой.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kit_common as kit  # noqa: E402

TOOL = "check_merge_log.py"

# Виды нарушений; так же они называются в файле исключений.
NOT_MERGE = "не слиянием"
SQUASH = "склейка"
SELF_MERGE = "сливший автор"
NO_DECISION = "нет варианта решений"
NO_OWNER_APPROVAL = "нет утверждения владельца"
NO_TRACE = "нет прямого следа"
REUSED_BRANCH = "повторная ветка"
NOT_ON_LINE = "нет на первой линии"
KINDS = (NOT_MERGE, SQUASH, SELF_MERGE, NO_DECISION, NO_OWNER_APPROVAL,
         NO_TRACE, REUSED_BRANCH, NOT_ON_LINE)

# Варианты раздела «Решения» шаблона запроса (AGENTS.md, 2.3).
OWNER_DECISION = "исполняет решение владельца"
NO_DECISIONS = "решений не несёт"
NEW_DECISION = "несёт новое решение"
VARIANTS = (OWNER_DECISION, NO_DECISIONS, NEW_DECISION)


def username(user):
    return (user or {}).get("username")


def short(sha):
    return (sha or "")[:8]


# --- Описание запроса -------------------------------------------------------

def plain(text):
    return (text or "").replace("ё", "е").replace("Ё", "Е").lower()


def decisions_section(description):
    """Текст раздела «Решения» без блоков кода и комментариев HTML; None,
    если раздела нет. Заголовок — «Решения», в том числе с двоеточием и
    закрывающими «#»: «## Решения:», «## Решения ##». Раздел кончается
    следующим заголовком любого уровня. Заголовок и вариант в блоке кода —
    цитата: иначе цитата шаблона выше настоящего раздела подменила бы его.
    Начало комментария в строчном коде — тоже цитата: иначе комментарий
    тянулся бы от него до конца комментария в подвале шаблона и съел раздел."""
    text = (description or "").replace("\r\n", "\n").replace("\r", "\n")
    text = CODE_BLOCK.sub(" ", text)
    text = COMMENT_OR_CODE.sub(lambda m: m.group(0) if m.group(1) else "",
                               text)
    m = re.search(r"(?m)^#{1,6}[ \t]*Решения[ \t]*:?(?:[ \t]+#+)?[ \t]*$",
                  text)
    if not m:
        return None
    rest = text[m.end():]
    end = re.search(r"(?m)^#{1,6}[ \t]", rest)
    return rest[:end.start()] if end else rest


def decision_variants(section):
    """Варианты шаблона, с которых начинается строка раздела."""
    found = []
    for line in plain(section).splitlines():
        line = re.sub(r"^\s*(?:[-*+]\s+)?", "", line)
        for variant in VARIANTS:
            if line.startswith(plain(variant)) and variant not in found:
                found.append(variant)
    return found


# Разбор ссылок держится одного правила: запись либо узнаётся верно
# (репозиторий и номер), либо не засчитывается. Неоднозначная запись
# следом не считается: ложное «нет прямого следа» разбирают исключением, а
# ложный след прошёл бы молча.

# Блок кода между ``` или ~~~: в нём команды и примеры, а не ссылки. Заборы —
# как в CommonMark: в сведениях забора из обратных кавычек их быть не может, а
# закрывает блок строка из одного забора того же знака, не короче открывшего.
CODE_BLOCK = re.compile(
    r"(?ms)^[ \t]*(?:(`{3,})[^`\n]*$|(~{3,})[^\n]*$).*?"
    r"(?:^[ \t]*(?:\1`*|\2~*)[ \t]*$|\Z)")
# Комментарий HTML или строчный код — что началось раньше, то и действует, как в
# CommonMark. Строчный код закрывает запись той же длины; код считается только
# в пределах строки: комментарий на своей строке прерывает абзац, и кавычки
# вокруг него не должны открыть скрытый им вариант.
COMMENT_OR_CODE = re.compile(
    r"(?s)<!--.*?-->|(?<![`\\])(`+)(?!`)[^\n]*?(?<!`)\1(?!`)")
# Ссылка Markdown «[текст](адрес)»: засчитывается адрес, текст не читается.
MD_LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]*)>?[^)\n]*\)")
# Адрес запроса со схемой и узлом, без схемы с узлом или путь от корня
# площадки.
MR_URL = re.compile(
    r"(?:(?:https?:)?//(?P<host>[^/\s]+)/)?"
    r"(?P<path>[\w.-]+(?:/[\w.-]+)+)/-/merge_requests/(?P<iid>\d+)(?!\w)")
# Закрывающие кавычки и скобки после имени репозитория: «`группа/имя` !5».
# Открывающие перечислять не нужно: перед именем запрещены лишь буква,
# цифра и «/.!-».
CLOSING = "`'\"»“”’›)"
# «группа/имя !5», «группа/имя!5», «группа/имя: !5» и перечисление после
# имени: «группа/имя !5, !6 и !7».
QUALIFIED_REF = re.compile(
    r"(?<![\w/.!-])((?:[\w.-]+/)+[\w.-]+)"
    r"[" + re.escape(CLOSING) + r"]*:?\s*!(\d+)(?!\w)"
    r"((?:\s*(?:,|и)\s*!\d+(?!\w))*)")
# Номер сразу после пути, за которым стоят лишь знаки препинания:
# репозиторий не узнан, это имя файла или адрес не этой площадки.
AFTER_PATH = re.compile(r"[\w.-]+/[\w./-]*[^\w\s!]*\s*$")
BARE_REF = re.compile(r"(?<![\w/!])!(\d+)(?!\w)")


def in_group(path, group):
    return path.lower().startswith(group.lower() + "/")


def names_other_repo(text, repo, group):
    """Назван ли в тексте другой репозиторий своей группы — по имени, в
    адресе или в пути. Тогда номер без имени неоднозначен: «группа/а !11;
    !18» может значить и группа/а!18, и запрос этого репозитория."""
    own = repo.lower()
    name = re.compile(r"(?<![\w.-])(%s/[\w.-]+(?:/[\w.-]+)*)"
                      % re.escape(group), re.IGNORECASE)
    for m in name.finditer(text):
        path = m.group(1).lower()
        if path != own and not path.startswith(own + "/"):
            return True
    return False


def referenced_mrs(section, repo, group, hosts=()):
    """Запросы, на которые ссылается раздел, парами (репозиторий, номер).

    Узнаются «!5» — запрос этого репозитория; «группа/имя!5» и «группа/имя
    !5», в том числе с именем в кавычках или скобках и двоеточием перед
    номером; перечисление «группа/имя !5, !6 и !7»; адрес запроса на
    площадке, узел которой есть в hosts, и путь «группа/имя/-/
    merge_requests/5». Не засчитываются: репозиторий вне своей группы
    (AGENTS.md, 7.1), адрес другой площадки, адрес с узлом, который не
    разбирается (например, с незакрытой квадратной скобкой), ссылка в
    блоке кода, текст ссылки Markdown (засчитывается её адрес), номер
    сразу после неузнанного пути и любой номер без имени, если раздел
    называет другой репозиторий группы."""
    hosts = set(h.lower() for h in hosts if h)
    refs = set()

    def add(path, iid):
        if path.lower() == repo.lower():
            path = repo
        refs.add((path, int(iid)))

    def add_url(m):
        host, path = m.group("host"), m.group("path")
        first, _, tail = path.partition("/")
        if host is None and "." in first and "/" in tail and \
                not in_group(path, group):
            # Адрес без схемы: первым стоит узел, если путь не начинается
            # своей группой — в имени группы тоже бывает точка.
            host, path = first, tail
        if host is not None:
            try:
                host = urllib.parse.urlsplit("//" + host).hostname
            except ValueError:
                return " "  # узел не разбирается: не адрес этой площадки
            if host not in hosts:
                return " "  # другая площадка
        if in_group(path, group):
            add(path, m.group("iid"))
        return " "

    def add_link(m):
        MR_URL.sub(add_url, m.group(1))
        return " "

    def add_qualified(m):
        name = m.group(1)
        if in_group(name, group):
            for num in [m.group(2)] + re.findall(r"!(\d+)", m.group(3)):
                add(name, num)
        return " "

    rest = CODE_BLOCK.sub(" ", section)
    ambiguous = names_other_repo(rest, repo, group)
    rest = MD_LINK.sub(add_link, rest)
    rest = MR_URL.sub(add_url, rest)
    rest = QUALIFIED_REF.sub(add_qualified, rest)
    if not ambiguous:
        for m in BARE_REF.finditer(rest):
            if not AFTER_PATH.search(rest[:m.start()]):
                add(repo, m.group(1))
    return sorted(refs)


# --- Проверка ---------------------------------------------------------------

def first_line(commits, head):
    """Первая линия основной ветки от вершины к началу. Изменения, которых
    нет на ней, отбрасываются: так проверка не зависит от того, учла ли
    площадка first_parent и в каком порядке отдала изменения. Пустой список
    родителей — начальное изменение; поля нет или оно не список — код 2:
    иначе конец линии был бы неотличим от начала истории."""
    by_id = dict((c.get("id"), c) for c in commits)
    line = []
    sha = head
    while sha is not None:
        commit = by_id.get(sha)
        if commit is None:
            raise kit.ToolError(
                "первая линия основной ветки не получена целиком: изменения "
                "%s нет в ответе площадки" % short(sha))
        line.append(commit)
        parents = commit.get("parent_ids")
        if not isinstance(parents, list):
            raise kit.ToolError(
                "первая линия основной ветки не получена: у изменения %s поля "
                "parent_ids нет или оно не список" % short(sha))
        sha = parents[0] if parents else None
    return line


class MergeLog:
    """Журнал слияний одного репозитория по ответам площадки."""

    def __init__(self, client, cfg, host):
        self.client = client
        self.repo = cfg["площадка"]["репозиторий"]
        self.group = cfg["площадка"]["группа"]
        self.owner = cfg["учётные записи"]["владелец"]
        self.branch = cfg["основная ветка"]
        self.hosts = set([urllib.parse.urlsplit(host).hostname])
        self.approvals = {}
        self.traces = {}
        self.stats = {}

    @staticmethod
    def project(repo):
        return "projects/" + urllib.parse.quote(repo, safe="")

    def mr_path(self, repo, iid):
        return "%s/merge_requests/%d" % (self.project(repo), iid)

    def approvers(self, repo, iid):
        key = (repo.lower(), iid)
        if key not in self.approvals:
            answer = self.client.get(self.mr_path(repo, iid) + "/approvals")
            self.approvals[key] = set(
                username(a.get("user"))
                for a in (answer or {}).get("approved_by") or [])
        return self.approvals[key]

    def owner_trace(self, repo, iid):
        """Есть ли у запроса утверждение владельца или его заметка;
        системная заметка площадки следом не считается."""
        key = (repo.lower(), iid)
        if key not in self.traces:
            traced = self.owner in self.approvers(repo, iid)
            if not traced:
                notes = self.client.get_all(self.mr_path(repo, iid) +
                                            "/notes")
                traced = any(username(n.get("author")) == self.owner
                             and not n.get("system") for n in notes)
            self.traces[key] = traced
        return self.traces[key]

    def named_trace(self, repo, iid):
        """След в названном запросе. Запроса нет или ключу он не виден —
        следа нет; другой сбой площадки — проверка не выполнена."""
        try:
            return self.owner_trace(repo, iid)
        except kit.ApiError as err:
            if err.code in (403, 404):
                return False
            raise

    def check(self):
        project = self.project(self.repo)
        branch = self.client.get("%s/repository/branches/%s" % (
            project, urllib.parse.quote(self.branch, safe=""))) or {}
        head = (branch.get("commit") or {}).get("id")
        if not head:
            raise kit.ToolError("площадка не назвала вершину ветки %s"
                                % self.branch)
        commits = self.client.get_all(
            project + "/repository/commits",
            {"ref_name": self.branch, "first_parent": "true"})
        line = first_line(commits, head)
        mrs = self.client.get_all(
            project + "/merge_requests",
            {"state": "merged", "target_branch": self.branch})
        mrs.sort(key=lambda mr: (mr.get("merged_at") or "", mr.get("iid")))
        for mr in mrs:
            url = mr.get("web_url") or ""
            try:
                host = urllib.parse.urlsplit(url).hostname
            except ValueError as err:
                # По узлам этих адресов узнаются ссылки на запросы (2.5):
                # без узла ссылка с ним не засчиталась бы, и итог «нарушений
                # N» был бы неверным, поэтому — проверка не выполнена.
                raise kit.ToolError(
                    "журнал слияний не проверен: площадка отдала у запроса "
                    "!%s адрес «%s», узел которого не разбирается (%s)"
                    % (mr.get("iid"), url, err))
            if host:
                self.hosts.add(host.lower())
        violations = self.check_line(line, mrs)
        violations += self.check_requests(mrs)
        self.stats = {"line": len(line), "requests": len(mrs)}
        return violations

    def check_line(self, line, mrs):
        """Каждое изменение первой линии пришло слиянием запроса (AGENTS.md,
        3.1). Начальное изменение без родителей создаёт площадка при
        заведении проекта — его не проверяют. И обратно: слияние каждого
        запроса лежит на первой линии, иначе историю перезаписали (3.4);
        запрос без merge_commit_sha так не сверить."""
        merges = set(mr.get("merge_commit_sha") for mr in mrs)
        found = []
        for commit in reversed(line):
            if not commit.get("parent_ids"):
                continue
            if commit.get("id") not in merges:
                found.append(kit.Violation(
                    short(commit.get("id")), 0, NOT_MERGE,
                    "изменение «%s» пришло в %s не слиянием запроса"
                    % (commit.get("title", ""), self.branch)))
        on_line = set(commit.get("id") for commit in line)
        for mr in mrs:
            sha = mr.get("merge_commit_sha")
            if sha and sha not in on_line:
                found.append(kit.Violation(
                    "!%d" % mr["iid"], 0, NOT_ON_LINE,
                    "изменения слияния %s нет на первой линии %s: историю "
                    "основной ветки перезаписали (AGENTS.md, 3.4)"
                    % (short(sha), self.branch)))
        return found

    def check_requests(self, mrs):
        """Нарушения слитых запросов: склейка, сливший автор, варианты
        раздела «Решения», утверждение и след владельца, повторная
        ветка."""
        found = []
        used = {}
        for mr in mrs:
            iid = mr["iid"]
            where = "!%d" % iid

            def add(kind, text):
                found.append(kit.Violation(where, 0, kind, text))

            # Слит способом merge, без склейки (AGENTS.md, 2.4). Изменение
            # склейки площадка называет в squash_commit_sha; флаг squash
            # засчитывается и без него: пропущенная склейка прошла бы
            # молча, а лишнее нарушение разбирают исключением.
            if mr.get("squash_commit_sha"):
                add(SQUASH, "запрос слит со склейкой (squash), изменение "
                            "склейки %s; сливают способом merge без склейки "
                            "(AGENTS.md, 2.4)"
                    % short(mr.get("squash_commit_sha")))
            elif mr.get("squash") is True:
                add(SQUASH, "у слитого запроса включена склейка (squash); "
                            "сливают способом merge без склейки (AGENTS.md, "
                            "2.4)")

            # Запрос слил не его автор (AGENTS.md, 1.3).
            author = username(mr.get("author"))
            merger = username(mr.get("merge_user") or mr.get("merged_by"))
            if not merger:
                add(SELF_MERGE, "площадка не назвала, кто слил запрос")
            elif merger == author:
                add(SELF_MERGE, "слил сам автор запроса %s" % author)

            # Ровно один вариант в разделе «Решения» (AGENTS.md, 2.3).
            section = decisions_section(mr.get("description"))
            variants = decision_variants(section) if section else []
            if section is None:
                add(NO_DECISION, "в описании нет раздела «Решения»")
            elif not variants:
                add(NO_DECISION, "в разделе «Решения» нет ни одного "
                                 "варианта шаблона")
            elif len(variants) > 1:
                add(NO_DECISION, "в разделе «Решения» оставлено "
                                 "вариантов: %d, нужен один"
                    % len(variants))

            # Новое решение утверждено владельцем (AGENTS.md, 2.2).
            if NEW_DECISION in variants and \
                    self.owner not in self.approvers(self.repo, iid):
                add(NO_OWNER_APPROVAL, "«%s», но утверждения владельца %s "
                                       "нет" % (NEW_DECISION, self.owner))

            # Исполнение решения владельца — с его прямым следом (2.5).
            if OWNER_DECISION in variants and \
                    not self.traced(iid, section):
                add(NO_TRACE, "«%s», но нет ни утверждения, ни заметки "
                              "владельца %s в запросе, ни однозначной "
                              "ссылки на запрос с ними"
                    % (OWNER_DECISION, self.owner))

            # Ветку слитого запроса повторно не используют (3.5).
            source = mr.get("source_branch")
            if source in used:
                add(REUSED_BRANCH, "ветка %s уже использована слитым "
                                   "запросом !%d" % (source, used[source]))
            else:
                used[source] = iid
        return found

    def traced(self, iid, section):
        """След владельца: утверждение или заметка в самом запросе либо в
        запросе, на который раздел ссылается однозначно. Владелец — этого
        репозитория, из настройки, и в названном запросе тоже: решение
        по этому репозиторию даёт его владелец (AGENTS.md, 1.1)."""
        if self.owner_trace(self.repo, iid):
            return True
        for repo, ref in referenced_mrs(section, self.repo, self.group,
                                        self.hosts):
            if (repo, ref) == (self.repo, iid):
                continue
            if self.named_trace(repo, ref):
                return True
        return False


def load_exemptions(path, rel):
    """Исключения средства и нарушения файла исключений. Место — «!N»
    запроса или первые 8 знаков изменения; абзаца у этого средства нет:
    исключение действует на запрос или изменение целиком."""
    exemptions, problems = kit.load_exceptions(path, TOOL, KINDS, rel)
    kept = []
    for e in exemptions:
        if re.match(r"^![0-9]+$", e.place):
            pass
        elif re.match(r"^[0-9a-fA-F]{8,40}$", e.place):
            e.place = e.place[:8].lower()
        else:
            problems.append(kit.Violation(
                rel, e.line, "исключение",
                "место у %s — «!<номер>» запроса или первые 8 знаков "
                "изменения основной ветки, а здесь «%s»" % (TOOL, e.place)))
            continue
        if e.paragraph is not None:
            problems.append(kit.Violation(
                rel, e.line, "исключение",
                "поле «абзац» у %s не задают: исключение действует на "
                "запрос или изменение целиком" % TOOL))
            continue
        kept.append(e)
    return kept, problems


# --- Запуск -----------------------------------------------------------------

def parse(argv):
    parser = kit.make_parser(
        TOOL,
        "Журнал слияний основной ветки: она менялась только по правилам "
        "разделов 2 и 3 AGENTS.md. Только чтение; ключ — в переменной "
        "GITLAB_TOKEN.")
    parser.add_argument("--host", metavar="<адрес>",
                        help="другая площадка вместо поля «площадка», "
                             "«адрес» настройки")
    kit.add_config_argument(parser)
    return parser.parse_args(argv)


def main(argv=None):
    kit.setup_output()
    args = parse(argv)
    try:
        return run(args)
    except kit.ToolError as err:
        return kit.report(err)
    except KeyboardInterrupt:
        print("проверка прервана", file=sys.stderr)
        return kit.INTERRUPTED
    except Exception as err:
        return kit.report_crash(err)


def run(args):
    cfg = kit.load_config(args.config)
    root = kit.clone_root()
    exceptions_path = kit.DEFAULT_EXCEPTIONS
    rel = (kit.relative(exceptions_path, root)
           or kit.shown(exceptions_path))
    exemptions, problems = load_exemptions(exceptions_path, rel)
    host = kit.platform_host(cfg, args.host)
    token = kit.require_token()
    log = MergeLog(kit.Client(host, token), cfg, host)
    try:
        found = log.check()
    except kit.ApiError as err:
        if err.code == 401:
            raise kit.ToolError("площадка не приняла ключ: %s" % err)
        raise kit.ToolError("журнал слияний не прочитан: %s" % err)
    found = kit.apply_exceptions(found, exemptions, KINDS, {})
    problems += kit.unused_exceptions(exemptions, rel)
    print("основная ветка %s репозитория %s: изменений первой линии %d, "
          "слитых запросов %d; владелец — %s"
          % (log.branch, log.repo, log.stats["line"],
             log.stats["requests"], log.owner))
    for v in found:
        print("%s: %s: %s" % (v.path, v.kind, v.text))
    for p in problems:
        print(p)
    total = len(found) + len(problems)
    print("нарушений %d" % total)
    return kit.FOUND if total else kit.OK


if __name__ == "__main__":
    sys.exit(main())
