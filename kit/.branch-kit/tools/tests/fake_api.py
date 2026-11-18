"""Поддельная площадка для проверок: сервер HTTP или HTTPS на 127.0.0.1
отвечает по записанным ответам answers.json и к настоящей площадке не
обращается.

Состояние площадки — запросы, заметки, утверждения, журнал основной
ветки, правила защиты, участники — меняют проверки; when() меняет его на
n-м обращении к пути, то есть пока опрос ждёт. Запросы POST и PUT
записываются в writes.
"""

import copy
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fake_tls

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(TESTS_DIR, "answers.json"), encoding="utf-8") as _f:
    ANSWERS = json.load(_f)

LEVEL_DESCRIPTIONS = {0: "No one", 30: "Developers + Maintainers",
                      40: "Maintainers", 60: "Admins"}


def person(name, uid=None):
    return {"id": uid or 100 + sum(map(ord, name)) % 900, "username": name,
            "name": name}


def level(value):
    return {"id": 1, "access_level": value, "user_id": None,
            "group_id": None, "deploy_key_id": None,
            "access_level_description": LEVEL_DESCRIPTIONS.get(value,
                                                               str(value))}


def rule(name, push, merge, force=False):
    """Правило защиты ветки в виде ответа площадки."""
    item = copy.deepcopy(ANSWERS["protected_branch"])
    item.update(name=name, push_access_levels=[level(push)],
                merge_access_levels=[level(merge)], allow_force_push=force)
    return item


class Project:
    """Состояние одного репозитория на площадке."""

    def __init__(self, path):
        self.path = path
        self.requests = {}  # номер запроса -> запрос
        self.notes = {}  # номер запроса -> [заметка]
        self.approved = {}  # номер запроса -> [учётная запись]
        self.commits = []  # журнал основной ветки, от новых к старым
        self.branches = {}  # ветка -> вершина
        self.protected = {}  # маска -> правило
        self.settings = copy.deepcopy(ANSWERS["project"])
        self.settings.update(path_with_namespace=path)
        self.members = {}  # учётная запись -> уровень доступа


class FakePlatform:
    """tls — каталог для ключа и сертификата: площадка тогда отвечает по
    https, а cert — путь к её сертификату."""

    def __init__(self, project="example/app", web_host="127.0.0.1",
                 tls=None):
        self.project = project
        self.web_host = web_host
        self.projects = {}
        main = self.main_repo = self.repo(project)
        self.requests = main.requests
        self.notes = main.notes
        self.approved = main.approved
        self.tokens = {}  # ключ -> учётная запись
        self.admins = set()  # is_admin в ответе и настройки площадки
        self.settings_readers = set()  # только настройки площадки
        self.next_note = 1000
        self.failures = 0  # столько следующих обращений — ответ 500
        # Заголовок X-Next-Page следующих страниц вместо настоящего; None —
        # заголовка нет вовсе.
        self.next_pages = []
        self.redirect = None  # адрес: на всё — переадресация 302 туда
        self.hooks = []  # [часть пути, номер обращения к ней, действие]
        self.calls = []  # [(путь, ключ)]
        self.writes = []  # [(метод, путь, ключ, тело)]
        # Отказ на запись: (метод, последняя часть пути) -> (код, текст).
        self.refuse = {}
        self.merge_allowed = False
        self.on_merge = None  # действие(запрос, тело) при слиянии
        self.on_protect = None  # правка правила, которое площадка хранит
        self.alpn = []  # https: протокол ALPN каждого соединения или None
        self.lock = threading.RLock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.platform = self
        scheme, self.cert = "http", None
        if tls:
            context, self.cert = fake_tls.server_context(tls)
            # Площадка согласна на «http/1.1»: клиент, который его не
            # предложил, остаётся без ALPN — в alpn тогда None.
            context.set_alpn_protocols(["http/1.1"])
            self.server.socket = context.wrap_socket(self.server.socket,
                                                     server_side=True)
            scheme = "https"
        self.url = "%s://127.0.0.1:%d" % (scheme,
                                          self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # --- состояние площадки

    def repo(self, path):
        if path not in self.projects:
            self.projects[path] = Project(path)
        return self.projects[path]

    def user(self, token, name):
        self.tokens[token] = name

    def member(self, name, access_level, project=None):
        self.repo(project or self.project).members[name] = access_level

    def add_request(self, iid, title="Запрос", author="ai-dev",
                    branch="feat/example", sha="1" * 40, state="opened",
                    project=None, **fields):
        with self.lock:
            repo = self.repo(project or self.project)
            item = copy.deepcopy(ANSWERS["merge_request"])
            item.update(iid=iid, id=300 + iid, title=title,
                        author=person(author), source_branch=branch, sha=sha,
                        state=state,
                        web_url="http://%s/%s/-/merge_requests/%d"
                                % (self.web_host, repo.path, iid))
            item.update(fields)
            repo.requests[iid] = item
            repo.notes.setdefault(iid, [])
            repo.approved.setdefault(iid, [])
            return item

    def add_note(self, iid, author, body, system=False, project=None):
        with self.lock:
            note = copy.deepcopy(
                ANSWERS["system_note" if system else "note"])
            note.update(id=self.next_note, body=body, author=person(author),
                        noteable_iid=iid, system=system)
            self.next_note += 1
            self.repo(project or self.project).notes[iid].append(note)
            return note["id"]

    def set_sha(self, iid, sha):
        with self.lock:
            self.requests[iid]["sha"] = sha

    def approve(self, iid, name, project=None):
        with self.lock:
            self.repo(project or self.project).approved[iid].append(name)

    def unapprove(self, iid, name):
        with self.lock:
            self.approved[iid].remove(name)

    def set_state(self, iid, state, by="ai-reviewer"):
        with self.lock:
            item = self.requests[iid]
            item["state"] = state
            if state == "merged":
                item["merged_by"] = item["merge_user"] = person(by)
            elif state == "closed":
                item["closed_by"] = person(by)

    def when(self, part, nth, action):
        """Выполнить action перед ответом на nth-е с этой минуты обращение
        к пути, в котором есть part."""
        with self.lock:
            self.hooks.append([part, len(self.paths(part)) + nth, action])

    def paths(self, part=""):
        return [p for p, _ in self.calls
                if part in urllib.parse.urlsplit(p).path]

    def written(self, method=None):
        return [w for w in self.writes if method in (None, w[0])]

    # --- ответы

    def answer(self, method, raw_path, token, body=None):
        with self.lock:
            self.calls.append((raw_path, token))
            if method != "GET":
                self.writes.append((method, raw_path, token, body))
            route = urllib.parse.urlsplit(raw_path).path
            for hook in list(self.hooks):
                part, nth, action = hook
                if part in route and len(self.paths(part)) >= nth:
                    self.hooks.remove(hook)
                    action()
            if self.failures:
                self.failures -= 1
                return 500, {"message": "500 Internal Server Error"}, {}
            if self.redirect:
                return 302, {"message": "302 Found"}, {
                    "Location": self.redirect}
            name = self.tokens.get(token)
            if name is None:
                return 401, {"message": "401 Unauthorized"}, {}
            last = route.rstrip("/").rsplit("/", 1)[-1]
            if (method, last) in self.refuse:
                status, text = self.refuse[(method, last)]
                return status, {"message": text}, {}
            if method == "GET":
                return self.route(raw_path, name)
            return self.write(method, route, name, body or {})

    def find(self, route):
        """Репозиторий и части пути после него; (None, None) — не путь
        репозитория. Свой репозиторий отвечает под именем self.project:
        проверка, сменившая имя, изображает пропавший проект."""
        served = {self.project: self.main_repo}
        for path, repo in self.projects.items():
            if repo is not self.main_repo:
                served.setdefault(path, repo)
        for path, repo in served.items():
            prefix = "/api/v4/projects/%s" % urllib.parse.quote(path,
                                                                safe="")
            if route == prefix:
                return repo, []
            if route.startswith(prefix + "/"):
                return repo, route[len(prefix) + 1:].split("/")
        return None, None

    def route(self, raw_path, name):
        parts = urllib.parse.urlsplit(raw_path)
        query = dict(urllib.parse.parse_qsl(parts.query))
        route = parts.path
        if route == "/api/v4/user":
            user = copy.deepcopy(ANSWERS["user"])
            user.update(person(name))
            if name in self.admins:
                user["is_admin"] = True
            return 200, user, {}
        if route == "/api/v4/application/settings":
            if name in self.admins or name in self.settings_readers:
                return 200, copy.deepcopy(ANSWERS["application_settings"]), {}
            return 403, {"message": "403 Forbidden"}, {}
        repo, rest = self.find(route)
        if repo is None:
            return 404, {"message": "404 Project Not Found"}, {}
        if rest == []:
            return 200, copy.deepcopy(repo.settings), {}
        if rest[:2] == ["repository", "commits"]:
            return self.paged(copy.deepcopy(repo.commits), query)
        if rest[:2] == ["repository", "branches"] and len(rest) == 3:
            branch = urllib.parse.unquote(rest[2])
            if branch not in repo.branches:
                return 404, {"message": "404 Branch Not Found"}, {}
            return 200, {"name": branch,
                         "commit": {"id": repo.branches[branch]}}, {}
        if rest == ["protected_branches"]:
            return self.paged(copy.deepcopy(list(repo.protected.values())),
                              query)
        if rest[:1] == ["protected_branches"] and len(rest) == 2:
            item = repo.protected.get(urllib.parse.unquote(rest[1]))
            if item is None:
                return 404, {"message": "404 Not found"}, {}
            return 200, copy.deepcopy(item), {}
        if rest[:2] == ["members", "all"] and len(rest) == 3:
            for member, access in repo.members.items():
                if str(person(member)["id"]) == rest[2]:
                    found = copy.deepcopy(ANSWERS["member"])
                    found.update(person(member), access_level=access)
                    return 200, found, {}
            return 404, {"message": "404 Not found"}, {}
        if rest == ["merge_requests"]:
            items = [copy.deepcopy(r) for _, r in
                     sorted(repo.requests.items(), reverse=True)
                     if query.get("state") in (None, "all", r["state"])
                     and query.get("target_branch") in (
                         None, r["target_branch"])]
            return self.paged(items, query)
        if len(rest) < 2 or rest[0] != "merge_requests" or \
                not rest[1].isdigit() or int(rest[1]) not in repo.requests:
            return 404, {"message": "404 Not found"}, {}
        iid = int(rest[1])
        if len(rest) == 2:
            return 200, copy.deepcopy(repo.requests[iid]), {}
        if rest[2:] == ["notes"]:
            notes = sorted(repo.notes[iid], key=lambda n: n["id"],
                           reverse=query.get("sort", "desc") == "desc")
            return self.paged(copy.deepcopy(notes), query)
        if rest[2:] == ["approvals"]:
            answer = copy.deepcopy(ANSWERS["approvals"])
            answer["approved_by"] = [{"user": person(n)}
                                     for n in repo.approved[iid]]
            answer["approved"] = bool(repo.approved[iid])
            return 200, answer, {}
        return 404, {"message": "404 Not found"}, {}

    def write(self, method, route, name, body):
        repo, rest = self.find(route)
        if repo is None:
            return 404, {"message": "404 Project Not Found"}, {}
        if method == "POST" and rest == ["protected_branches"]:
            pattern = body.get("name")
            if pattern in repo.protected:
                return 409, {"message": "Protected branch '%s' already "
                                        "exists" % pattern}, {}
            item = rule(pattern, body.get("push_access_level", 40),
                        body.get("merge_access_level", 40),
                        bool(body.get("allow_force_push")))
            if self.on_protect:
                self.on_protect(item)
            repo.protected[pattern] = item
            return 201, copy.deepcopy(item), {}
        if method == "POST" and rest == ["merge_requests"]:
            iid = max(list(repo.requests) + [0]) + 1
            item = self.add_request(
                iid, title=body.get("title", ""), author=name,
                branch=body.get("source_branch"),
                sha=repo.branches.get(body.get("source_branch"), "0" * 40),
                project=repo.path)
            item["target_branch"] = body.get("target_branch")
            item["description"] = body.get("description")
            return 201, copy.deepcopy(item), {}
        if len(rest) < 2 or rest[0] != "merge_requests" or \
                not rest[1].isdigit() or int(rest[1]) not in repo.requests:
            return 404, {"message": "404 Not found"}, {}
        iid = int(rest[1])
        item = repo.requests[iid]
        if method == "POST" and rest[2:] == ["notes"]:
            note_id = self.add_note(iid, name, body.get("body", ""),
                                    project=repo.path)
            return 201, {"id": note_id, "body": body.get("body", "")}, {}
        if method == "PUT" and rest[2:] == ["merge"]:
            if not self.merge_allowed:
                return 401, {"message": "401 Unauthorized"}, {}
            item["state"] = "merged"
            item["merged_by"] = item["merge_user"] = person(name)
            if self.on_merge:
                self.on_merge(item, body)
            return 200, copy.deepcopy(item), {}
        if method == "PUT" and rest[2:] == []:
            if body.get("state_event") == "close":
                item["state"] = "closed"
                item["closed_by"] = person(name)
            return 200, copy.deepcopy(item), {}
        return 404, {"message": "404 Not found"}, {}

    def paged(self, items, query):
        per_page = int(query.get("per_page", 20))
        page = int(query.get("page", 1))
        chunk = items[(page - 1) * per_page:page * per_page]
        more = page * per_page < len(items)
        if self.next_pages:
            value = self.next_pages.pop(0)
            return 200, chunk, {} if value is None else {"X-Next-Page": value}
        return 200, chunk, {"X-Next-Page": str(page + 1) if more else ""}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        selected = getattr(self.connection, "selected_alpn_protocol", None)
        if selected is not None:
            with self.server.platform.lock:
                self.server.platform.alpn.append(selected())

    def reply(self, method):
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        status, data, headers = self.server.platform.answer(
            method, self.path, self.headers.get("PRIVATE-TOKEN"), body)
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply("GET")

    def do_POST(self):
        self.reply("POST")

    def do_PUT(self):
        self.reply("PUT")
