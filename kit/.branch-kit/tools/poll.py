"""Опрос площадки: ждёт события в очереди роли и завершается на нём
(AGENTS.md, 4.3).

Запуск из корня клона, ключ — в GITLAB_TOKEN на одну команду:
    python .branch-kit/tools/poll.py --role planner|author|reviewer
        [--mr <номер>] [--target <ветка>] [--once] [--reset]
Постановщик и проверяющий ведут запросы в основную ветку, проверяющий
частей (--role reviewer --target) — запросы в корневую ветку задачи
(AGENTS.md, 3.7). Черновик проверяющему не событие: событие — снятие
пометки черновика.
Вывод — строка на событие «[чч:мм:сс] !N …», последняя — «событий N».
Код выхода: 0 — есть события, в том числе исходное состояние первого
запуска; 1 — событий нет: вышел срок «не дольше» или --once ничего не
нашёл; 2 — опрос не выполнен, в том числе ни одно обращение за срок
«не дольше» не удалось; 130 — прерван Ctrl+C, состояние не изменено.
Подробно — README.md рядом.
"""

import json
import os
import re
import sys
import time
import urllib.parse

# Байт-код общей части не пишется: каталог __pycache__ в клоне сделал
# бы рабочую копию несохранённой.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kit_common as kit  # noqa: E402

STATE_VERSION = 1
FAILURES_LIMIT = 5
NOTE_WIDTH = 120
STATES = {"opened": "открыт", "merged": "слит", "closed": "закрыт",
          "locked": "заблокирован"}
DELETED = "удалён: площадка не отдаёт запрос по номеру (404)"
DRAFT_OFF = "снята пометка черновика — на проверке"
DRAFT_ON = "поставлена пометка черновика — до её снятия событий нет"
RETARGETED = "цель сменена: %s → %s — вне очереди"
# Системная заметка площадки о постановке пометки: «marked this merge request
# as **draft**». О снятии — «… as **ready**».
DRAFT_MARK = re.compile(r"\*\*draft\*\*", re.IGNORECASE)


def clock():
    return time.strftime("%H:%M:%S")


def _sleep(seconds):
    time.sleep(seconds)


def one_line(text, width=NOTE_WIDTH):
    """Первая непустая строка текста без управляющих знаков."""
    for line in (text or "").splitlines():
        line = "".join(ch if ch.isprintable() else " " for ch in line)
        line = " ".join(line.split())
        if line:
            return line if len(line) <= width else line[:width - 1] + "…"
    return "(пусто)"


def short(sha):
    return (sha or "—")[:8]


def username(user):
    return (user or {}).get("username") or "?"


def is_draft(item):
    """Черновик: площадка ставит в ответе о запросе поле draft; площадка
    без этого поля — по имени, которое начинается с «Draft:» (AGENTS.md,
    1.6; 3.7, пп. 1, 7)."""
    return bool(item.get("draft")) or \
        (item.get("title") or "").startswith("Draft:")


def draft_mark(notes):
    """Номер последней системной заметки о постановке пометки: заметка с
    номером меньше написана до пометки. None — такой заметки нет, и порядок
    заметок и пометки не определить."""
    marks = [n["id"] for n in notes if n.get("system")
             and DRAFT_MARK.search(n.get("body") or "")]
    return max(marks) if marks else None


class Poller:
    """Очередь роли на площадке и её сравнение с запомненной."""

    def __init__(self, role, mr, client, project, root, me, emails=(),
                 target=None):
        self.role = role
        self.mr = mr
        self.target = target
        self.client = client
        self.project = project
        self.root = root
        self.me = me
        self.emails = set(e.strip().lower() for e in emails if e.strip())
        self.track_vertex = role in ("author", "reviewer")
        self.track_approvals = role in ("author", "reviewer")
        # Черновик — работа автора: проверяющий, и с --target, его не
        # берёт (AGENTS.md, 3.7, п. 7). Постановщик видит его: в черновике
        # автор задаёт вопрос по заданию (1.6).
        self.skip_drafts = role == "reviewer"

    def quiet(self, item):
        """Запрос вне очереди роли: черновик у проверяющего."""
        return self.skip_drafts and is_draft(item)

    # --- обращения к площадке

    def mr_path(self, iid):
        return "%s/merge_requests/%s" % (self.project, iid)

    def open_requests(self):
        """Открытые запросы в ветку очереди: у постановщика и проверяющего
        — в основную, у проверяющего частей — в корневую ветку задачи
        (AGENTS.md, 3.7). Запрос части в чужую очередь не попадает."""
        items = self.client.get_all(self.project + "/merge_requests",
                                    {"state": "opened",
                                     "target_branch": self.target})
        return dict((str(item["iid"]), item) for item in items)

    def request(self, iid):
        return self.client.get(self.mr_path(iid))

    def approvals(self, iid):
        data = self.client.get(self.mr_path(iid) + "/approvals") or {}
        return sorted(username(a.get("user"))
                      for a in data.get("approved_by") or [])

    def latest_notes(self, iid):
        """Первая страница заметок, от новых к старым."""
        return next(self.client.pages(self.mr_path(iid) + "/notes",
                                      {"sort": "desc",
                                       "order_by": "created_at"}))

    def new_notes(self, iid, last_id):
        """Заметки с номером больше last_id, от старых к новым, без
        повторов: заметка, пришедшая между чтением двух страниц, сдвигает
        страницы, и одна и та же заметка попала бы на обе."""
        notes = {}
        for page in self.client.pages(self.mr_path(iid) + "/notes",
                                      {"sort": "desc",
                                       "order_by": "created_at"}):
            fresh = [n for n in page if n["id"] > last_id]
            for note in fresh:
                notes[note["id"]] = note
            if len(fresh) < len(page):
                break
        return [notes[key] for key in sorted(notes)]

    # --- git

    def own_vertex(self, sha):
        """Своя отправка: изменение на вершине сохранено подписью роли из
        настройки — почта сохранившего его (у git — committer) совпадает с
        почтой подписи. Чужое изменение — событие, даже если оно уже есть
        в локальной ветке клона: проверяющий берёт ветку в свою локальную
        ветку для проверки результата слияния (AGENTS.md, 2.6), автор —
        правку проверяющего. Изменения нет в клоне — оно тоже чужое."""
        if not self.emails or not re.match(r"^[0-9a-fA-F]{7,64}$", sha):
            return False
        code, out, _ = kit.git(self.root, "log", "-1", "--format=%ce", sha,
                               "--")
        return code == 0 and out.strip().lower() in self.emails

    # --- исходное состояние и сравнение

    def entry(self, item, notes):
        entry = {
            "название": item.get("title") or "",
            "автор": username(item.get("author")),
            "ветка": item.get("source_branch") or "",
            "состояние": item.get("state") or "",
            "заметка": max([0] + [n["id"] for n in notes]),
        }
        if self.track_vertex:
            entry["вершина"] = item.get("sha") or ""
        if self.track_approvals:
            entry["утверждения"] = self.approvals(item["iid"])
        if self.skip_drafts:
            entry["черновик"] = is_draft(item)
        return entry

    def initial(self):
        """Исходное состояние: (строки событий, запросы)."""
        requests = {}
        lines = []
        drafts = []
        if self.role == "author":
            items = {str(self.mr): self.request(self.mr)}
        else:
            items = self.open_requests()
        for iid in sorted(items, key=int):
            item = items[iid]
            notes = self.latest_notes(iid)
            entry = self.entry(item, notes)
            requests[iid] = entry
            if self.quiet(item):
                drafts.append("!" + iid)
                continue
            text = "исходное состояние: %s «%s» от %s, ветка %s" % (
                STATES.get(entry["состояние"], entry["состояние"]),
                one_line(entry["название"]), entry["автор"], entry["ветка"])
            text += ", вершина %s" % short(item.get("sha"))
            if self.track_approvals:
                text += ", утверждения: %s" % (
                    ", ".join(entry["утверждения"]) or "нет")
            user_notes = [n for n in notes if not n.get("system")]
            if user_notes:
                last = user_notes[0]
                text += "; последняя заметка %d от %s: %s" % (
                    last["id"], username(last.get("author")),
                    one_line(last.get("body")))
            else:
                text += "; заметок нет"
            lines.append((iid, text))
        if not lines:
            text = "исходное состояние: открытых запросов в %s нет" % (
                self.target)
            if drafts:
                text += ", кроме черновиков: " + ", ".join(drafts)
            lines.append((None, text))
        return lines, requests

    def compare(self, old):
        """События с прошлого обращения: (строки событий, запросы)."""
        if self.role == "author":
            items = {str(self.mr): self.request(self.mr)}
        else:
            items = self.open_requests()
        events = []
        requests = {}
        for iid in sorted(items, key=int):
            item = items[iid]
            if iid not in old:
                requests[iid] = self.entry(item, self.latest_notes(iid))
                if self.quiet(item):
                    continue  # черновик: событие — снятие пометки
                events.append((iid, "новый запрос «%s» от %s, ветка %s" % (
                    one_line(item.get("title")),
                    username(item.get("author")),
                    item.get("source_branch"))))
                continue
            requests[iid] = self.changes(iid, item, old[iid], events)
        for iid in sorted(old, key=int):
            if iid in items:
                continue
            self.left_queue(iid, old[iid], events, requests)
        return events, requests

    def left_queue(self, iid, prev, events, requests):
        """Запрос, пропавший из списка: сверка по номеру. Обращение — только
        на пропавший запрос и один раз: из состояния он уходит, кроме случая,
        когда ответ площадки расходится со списком."""
        try:
            item = self.request(iid)
        except kit.ApiError as err:
            if err.code != 404:
                raise
            # Список открытых запросов площадка отдала, а этот запрос по
            # номеру — нет: его удалили. Из состояния он уходит, иначе
            # каждый следующий опрос кончался бы тем же отказом.
            if self.role == "planner":
                events.append((iid, DELETED))
            return
        if item.get("state") != "opened":
            # Проверяющему слитый или закрытый запрос не нужен.
            if self.role == "planner":
                events.append((iid, state_text(item)))
            return
        target = item.get("target_branch")
        if target == self.target:
            # Открыт, цель та же, а в списке его не было: запрос успели
            # закрыть и снова открыть. Он ещё в очереди.
            requests[iid] = prev
        elif not (self.quiet(item) and prev.get("черновик")):
            # Черновик и до смены цели не был в очереди проверяющего. Цель
            # вернут — запрос придёт как новый.
            events.append((iid, RETARGETED % (self.target, target)))

    def changes(self, iid, item, prev, events):
        """События запроса из прошлого состояния. У черновика проверяющего
        состояние обновляется молча: событие — только смена пометки; после
        снятия пометки показываются и изменения с прошлого обращения. С
        постановкой пометки показываются заметки до неё и смена утверждений
        того же промежутка: при снятии пометки их уже не показать. Лишнее
        дешевле потерянного: порядок не определить — заметка показывается, а
        об утверждениях площадка времени не говорит."""
        found = []
        with_mark = []
        entry = dict(prev)
        entry["название"] = item.get("title") or ""
        notes = self.new_notes(iid, prev["заметка"])
        mark = draft_mark(notes)
        for note in notes:
            author = username(note.get("author"))
            if note.get("system") or author == self.me:
                continue
            event = (iid, "заметка %d от %s: %s" % (
                note["id"], author, one_line(note.get("body"))))
            found.append(event)
            if mark is None or note["id"] < mark:
                with_mark.append(event)
        entry["заметка"] = max([prev["заметка"]] + [n["id"] for n in notes])
        if self.track_vertex:
            sha = item.get("sha") or ""
            if sha != prev.get("вершина"):
                if not self.own_vertex(sha):
                    found.append((iid, "вершина ветки %s → %s"
                                  % (short(prev.get("вершина")),
                                     short(sha))))
                entry["вершина"] = sha
        if self.track_approvals:
            current = self.approvals(iid)
            before = prev.get("утверждения") or []
            added = [u for u in current if u not in before]
            removed = [u for u in before if u not in current]
            parts = []
            if added:
                parts.append("добавил " + ", ".join(added))
            if removed:
                parts.append("снял " + ", ".join(removed))
            if parts:
                event = (iid, "утверждения: " + "; ".join(parts))
                found.append(event)
                with_mark.append(event)
            entry["утверждения"] = current
        state = item.get("state") or ""
        if state != prev.get("состояние"):
            found.append((iid, state_text(item)))
            entry["состояние"] = state
        if self.skip_drafts:
            # Состояние без поля «черновик» записано до отбора черновиков.
            was = prev.get("черновик", False)
            entry["черновик"] = is_draft(item)
            if entry["черновик"]:
                found = [] if was else [(iid, DRAFT_ON)] + with_mark
            elif was:
                found.insert(0, (iid, DRAFT_OFF))
        events.extend(found)
        return entry


def state_text(item):
    state = item.get("state")
    if state == "merged":
        user = item.get("merged_by") or item.get("merge_user")
        return "слит, слил %s" % username(user)
    if state == "closed":
        return "закрыт, закрыл %s" % username(item.get("closed_by"))
    if state == "opened":
        return "открыт снова"
    return "состояние: %s" % STATES.get(state, state)


# --- Состояние --------------------------------------------------------------

def state_path(root, role, mr, target=None):
    """Очередь частей — в своём файле: имя ветки с «/» и другими знаками
    записано через %XX, и у каждой корневой ветки файл свой."""
    name = role + ("-%d" % mr if mr is not None else "")
    if target is not None:
        name += "-" + urllib.parse.quote(target, safe="")
    return os.path.join(kit.git_dir(root), "branch-kit", "poll",
                        name + ".json")


def state_header(role, mr, target, host, cfg):
    """Цель — ветка, запросы в которую ведёт очередь; у автора её нет:
    он ведёт свой запрос. Состояние постановщика или проверяющего без
    цели записано до фильтра по цели, и в нём могут быть запросы частей:
    его начинают заново."""
    return {"версия": STATE_VERSION, "роль": role, "запрос": mr,
            "цель": target, "площадка": host,
            "репозиторий": cfg["площадка"]["репозиторий"]}


def entry_fits(iid, entry):
    """Запись запроса того вида, что пишет Poller.entry: иначе сравнение с ней
    упало бы или дало ложные события."""
    return (re.fullmatch(r"[0-9]+", iid) is not None
            and isinstance(entry, dict)
            and type(entry.get("заметка")) is int
            and isinstance(entry.get("вершина", ""), str)
            and isinstance(entry.get("утверждения", []), list))


def load_state(path, header):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as err:
        raise kit.ToolError("состояние опроса не читается: %s (%s); начните "
                            "заново ключом --reset" % (kit.shown(path), err))
    if not isinstance(data, dict) or not isinstance(data.get("запросы"),
                                                    dict):
        raise kit.ToolError("состояние опроса испорчено: %s; начните заново "
                            "ключом --reset" % kit.shown(path))
    for key, value in header.items():
        if data.get(key) != value:
            # Поля «цель» нет в состоянии, записанном до фильтра по цели.
            was = data[key] if key in data else "нет"
            raise kit.ToolError(
                "состояние опроса %s записано для другого запуска: поле "
                "«%s» — %s, сейчас — %s; начните заново ключом --reset"
                % (kit.shown(path), key, was, value))
    for iid, entry in data["запросы"].items():
        if not entry_fits(iid, entry):
            raise kit.ToolError("состояние опроса испорчено: %s, запись запроса "
                                "«%s»; начните заново ключом --reset"
                                % (kit.shown(path), iid))
    return data["запросы"]


def save_state(path, header, requests):
    """Запись целиком через временный файл: прерванная запись не портит
    прежнее состояние. Не записалось — например, в Windows файл держит
    другой процесс, — код 2 с путём, а не трассировка."""
    data = dict(header)
    data["запросы"] = requests
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as err:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise kit.ToolError(
            "состояние опроса не записано: %s (%s); выведенные события "
            "следующий запуск покажет снова. Файл может держать другой "
            "процесс, например второй опрос той же роли из этого клона"
            % (kit.shown(path), err))


def finish(events, path, header, requests):
    """Вывести события, затем записать состояние: прерванный между выводом
    и записью запуск покажет те же события снова, но не потеряет их."""
    stamp = clock()
    for iid, text in events:
        prefix = "!%s " % iid if iid is not None else ""
        print("[%s] %s%s" % (stamp, prefix, text), flush=True)
    print("событий %d" % len(events), flush=True)
    save_state(path, header, requests)
    return kit.OK if events else kit.FOUND


# --- Запуск -----------------------------------------------------------------

def parse(argv):
    parser = kit.make_parser(
        "poll.py",
        "Опрос площадки: ждёт события в очереди роли и завершается на нём "
        "(AGENTS.md, 4.3). Ключ — в переменной GITLAB_TOKEN.")
    parser.add_argument("--role", required=True,
                        choices=("planner", "author", "reviewer"),
                        help="planner — постановщик, author — автор, "
                             "reviewer — проверяющий")
    parser.add_argument("--mr", type=int, metavar="<номер>",
                        help="у автора обязательно: номер своего запроса")
    parser.add_argument("--target", metavar="<ветка>",
                        help="только у проверяющего: очередь запросов в "
                             "эту ветку вместо основной — проверка частей "
                             "(AGENTS.md, 3.7)")
    parser.add_argument("--once", action="store_true",
                        help="одно обращение без ожидания: события с "
                             "прошлого раза")
    parser.add_argument("--reset", action="store_true",
                        help="забыть запомненное состояние; следующий "
                             "запуск — как первый")
    parser.add_argument("--host", metavar="<адрес>",
                        help="другая площадка вместо поля «площадка», "
                             "«адрес» настройки")
    kit.add_config_argument(parser)
    return parser.parse_args(argv)


def main(argv=None):
    kit.setup_output()
    args = parse(argv)
    try:
        return poll(args)
    except kit.ToolError as err:
        return kit.report(err)
    except KeyboardInterrupt:
        print("опрос прерван; состояние не изменено", file=sys.stderr)
        return kit.INTERRUPTED
    except Exception as err:
        return kit.report_crash(err)


def poll(args):
    cfg = kit.load_config(args.config)
    if args.role == "author" and args.mr is None:
        raise kit.ToolError("у роли author ключ --mr обязателен: номер "
                            "своего запроса")
    if args.role != "author" and args.mr is not None:
        raise kit.ToolError("ключ --mr — только для роли author")
    target = check_target(args, cfg)
    root = kit.clone_root()
    path = state_path(root, args.role, args.mr, args.target)
    if args.reset:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as err:
                raise kit.ToolError("состояние опроса не забыто: %s (%s)"
                                    % (kit.shown(path), err))
        print("состояние опроса забыто: %s; следующий запуск — как первый"
              % kit.shown(path))
        return kit.OK
    host = kit.platform_host(cfg, args.host)
    token = kit.require_token()
    client = kit.Client(host, token)
    project = kit.project_path(cfg)
    if args.target is None:
        me = kit.check_account(client, cfg, args.role)
        poller = Poller(args.role, args.mr, client, project, root, me,
                        role_emails(cfg, args.role, me), target)
    else:
        # Проверяющий частей и автор части — агенты разработчика и могут
        # работать под одной учётной записью автора (AGENTS.md, 1.5):
        # своё от чужого опрос по ней не отличит. Поэтому каждая заметка,
        # кроме системной, и каждая смена вершины — событие: своё лишнее
        # событие дешевле пропущенного ответа автора части.
        kit.check_account(client, cfg, "part-reviewer")
        check_branch(client, project, target, cfg)
        poller = Poller(args.role, None, client, project, root, None, (),
                        target)
    if args.role == "author":
        item = check_own_request(poller, cfg)
        # То же со стороны автора части: заметка и отправка проверяющего
        # частей под той же учётной записью были бы своими, и замечание до
        # автора не дошло бы. Цель — из ответа площадки.
        if item.get("target_branch") != cfg["основная ветка"]:
            poller.me = None
            poller.emails = set()
    header = state_header(args.role, args.mr, target, host, cfg)
    old = load_state(path, header)
    if old is None:
        try:
            events, requests = poller.initial()
        except kit.ApiError as err:
            raise kit.ToolError("исходное состояние не получено: %s" % err)
        return finish(events, path, header, requests)
    return wait(poller, cfg, args, path, header, old)


def check_target(args, cfg):
    """Ветка, запросы в которую ведёт очередь: у постановщика и
    проверяющего — основная (AGENTS.md, 4.3), с --target — названная
    корневая ветка задачи (3.7); у автора — нет, он ведёт свой запрос."""
    main = cfg["основная ветка"]
    if args.target is None:
        return None if args.role == "author" else main
    if args.role != "reviewer":
        raise kit.ToolError("ключ --target — только для роли reviewer: "
                            "очередь проверяющего частей")
    if not args.target.strip():
        raise kit.ToolError("ключ --target — имя корневой ветки задачи, "
                            "а не пустая строка")
    if args.target == main:
        raise kit.ToolError("запросы в основную ветку %s проверяющий ведёт "
                            "без ключа --target: с ним — очередь частей в "
                            "корневую ветку задачи (AGENTS.md, 3.7)" % main)
    return args.target


def check_branch(client, project, target, cfg):
    """Корневая ветка очереди частей есть на площадке: фильтр с опечаткой в
    имени отдал бы пустой список, и очередь была бы пуста навсегда."""
    try:
        client.get("%s/repository/branches/%s"
                   % (project, urllib.parse.quote(target, safe="")))
    except kit.ApiError as err:
        if err.code == 404:
            raise kit.ToolError("ветки «%s» нет в %s: проверьте имя в ключе "
                                "--target"
                                % (target, cfg["площадка"]["репозиторий"]))
        raise kit.ToolError("ветка «%s» не сверена: %s" % (target, err))


def role_emails(cfg, role, me):
    """Почта подписи учётной записи me в роли по настройке: по ней опрос
    узнаёт свою отправку. У постановщика подписи нет: вершины он не
    ведёт."""
    accounts = cfg["учётные записи"]
    if role == "reviewer":
        entries = [accounts["проверяющий"]]
    elif role == "author":
        entries = [a for a in accounts["авторы"]
                   if a["учётная запись"] == me]
    else:
        entries = []
    return [e["подпись"]["почта"] for e in entries]


def check_own_request(poller, cfg):
    """У автора --mr — свой запрос: опечатка в номере иначе молча вела бы
    опрос чужого запроса. Возвращает ответ площадки о запросе."""
    try:
        item = poller.request(poller.mr)
    except kit.ApiError as err:
        if err.code == 404:
            raise kit.ToolError("запроса !%d нет в %s"
                                % (poller.mr,
                                   cfg["площадка"]["репозиторий"]))
        raise kit.ToolError("запрос !%d не получен: %s" % (poller.mr, err))
    author = username(item.get("author"))
    if author != poller.me:
        raise kit.ToolError("запрос !%d подал %s, а ключ — учётной записи %s:"
                            " автор опрашивает свой запрос"
                            % (poller.mr, author, poller.me))
    return item


def wait(poller, cfg, args, path, header, old):
    period = cfg["опрос"]["период"] * 60
    limit = cfg["опрос"]["не дольше"] * 60
    deadline = time.monotonic() + limit if limit > 0 else None
    failures = 0
    answered = False
    last_failure = None
    requests = old
    while True:
        try:
            events, fresh = poller.compare(requests)
        except kit.ApiError as err:
            if args.once or not err.transient:
                raise kit.ToolError("опрос не выполнен: %s" % err)
            failures += 1
            last_failure = err
            if failures >= FAILURES_LIMIT:
                raise kit.ToolError("площадка не ответила %d раз подряд; "
                                    "последний сбой: %s"
                                    % (failures, err))
            print("[%s] сбой площадки (%d из %d подряд), повтор через "
                  "период: %s" % (clock(), failures, FAILURES_LIMIT, err),
                  file=sys.stderr, flush=True)
        else:
            failures = 0
            answered = True
            requests = fresh
            if events:
                return finish(events, path, header, requests)
        if args.once:
            return finish([], path, header, requests)
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                minutes = ("%g" % (limit / 60)).replace(".", ",")
                if not answered:
                    # Срок короче пяти сбоев: счётчик живёт один запуск, и
                    # код 1 каждый новый запуск скрывал бы молчащую площадку.
                    raise kit.ToolError(
                        "срок «не дольше» (%s мин) вышел, ни одно обращение "
                        "к площадке за него не удалось: сбоев %d; последний "
                        "сбой: %s" % (minutes, failures, last_failure))
                print("срок «не дольше» (%s мин) вышел без событий — "
                      "поднимите опрос снова" % minutes, file=sys.stderr)
                return finish([], path, header, requests)
            _sleep(min(period, left))
        else:
            _sleep(period)


if __name__ == "__main__":
    sys.exit(main())
