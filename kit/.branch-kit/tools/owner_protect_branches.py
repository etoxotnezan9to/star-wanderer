"""Защита веток задач — запускает только владелец своим ключом.

Настройки проекта меняет только владелец (AGENTS.md, 3.4). Запуск из
корня клона, ключ владельца — в GITLAB_TOKEN на одну команду:
    python .branch-kit/tools/owner_protect_branches.py [--dry-run]
По каждому префиксу из настройки, поле «ветки задач», ставится правило
«<префикс>*»: отправка и слияние — Developer и Maintainer, принудительная
отправка запрещена. Правило, которое уже есть, не меняется; основная ветка
и настройки слияния только сверяются. По каждому правилу печатается «было
— стало», где «стало» — ответ площадки. Код выхода: 0 — всё как нужно;
1 — расхождение, а при --dry-run — и правило, которое ещё нужно создать;
2 — не выполнено; 130 — прервано сочетанием Ctrl+C: правила, о которых
строк ещё нет, не ставились. Подробно — README.md рядом.
"""

import fnmatch
import os
import sys
import urllib.parse

# Байт-код общей части не пишется: каталог __pycache__ в клоне сделал
# бы рабочую копию несохранённой.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kit_common as kit  # noqa: E402

TOOL = "owner_protect_branches.py"

NO_ONE = 0
DEVELOPER = 30  # уровень доступа GitLab: Developer и выше
MAINTAINER = 40
LEVEL_NAMES = {NO_ONE: "никто", DEVELOPER: "Developer и Maintainer",
               MAINTAINER: "Maintainer", 60: "администратор"}

# Нужные правила: (отправка, слияние); принудительная отправка запрещена.
TASK_RULE = (DEVELOPER, DEVELOPER)
MAIN_RULE = (NO_ONE, MAINTAINER)

GUI = "Settings → Repository → Protected branches"
MERGE_GUI = "Settings → Merge requests"


def describe_levels(levels):
    names = []
    for lvl in levels or []:
        if lvl.get("user_id"):
            names.append("пользователь %s" % lvl["user_id"])
        elif lvl.get("group_id"):
            names.append("группа %s" % lvl["group_id"])
        elif lvl.get("deploy_key_id"):
            names.append("ключ развёртывания %s" % lvl["deploy_key_id"])
        else:
            code = lvl.get("access_level")
            names.append(LEVEL_NAMES.get(
                code, lvl.get("access_level_description") or str(code)))
    return ", ".join(names) or "не задано"


def describe(rule):
    if rule is None:
        return "нет правила"
    force = "разрешена" if rule.get("allow_force_push") else "запрещена"
    return ("отправка: %s; слияние: %s; принудительная отправка: %s"
            % (describe_levels(rule.get("push_access_levels")),
               describe_levels(rule.get("merge_access_levels")), force))


def wanted_text(levels):
    push, merge = levels
    return ("отправка: %s; слияние: %s; принудительная отправка: запрещена"
            % (LEVEL_NAMES[push], LEVEL_NAMES[merge]))


def only_level(levels, level):
    """Ровно один уровень доступа роли, без учётных записей, групп и
    ключей развёртывания."""
    return (len(levels or []) == 1
            and levels[0].get("access_level") == level
            and not levels[0].get("user_id")
            and not levels[0].get("group_id")
            and not levels[0].get("deploy_key_id"))


def is_wanted(rule, levels):
    push, merge = levels
    return (rule is not None
            and only_level(rule.get("push_access_levels"), push)
            and only_level(rule.get("merge_access_levels"), merge)
            and not rule.get("allow_force_push"))


def patterns(cfg):
    """Маски веток задач по порядку настройки, без повторов."""
    result = []
    for item in cfg["ветки задач"]:
        pattern = item["префикс"] + "*"
        if pattern not in result:
            result.append(pattern)
    return result


class Protector:
    def __init__(self, client, cfg, dry_run):
        self.client = client
        self.project = kit.project_path(cfg)
        self.main = cfg["основная ветка"]
        self.patterns = patterns(cfg)
        self.dry_run = dry_run
        self.created = self.unchanged = self.to_create = 0
        self.problems = 0

    def task_rule(self, pattern, rules):
        head = "ветки %s: было — %s; " % (pattern, describe(rules.get(
            pattern)))
        before = rules.get(pattern)
        if before is not None:
            self.unchanged += 1
            if is_wanted(before, TASK_RULE):
                return head + "стало — без изменений"
            self.problems += 1
            return (head + "стало — без изменений: РАСХОЖДЕНИЕ — правило "
                    "есть и отличается от нужного (%s); средство его не "
                    "меняет — поправьте сами в %s"
                    % (wanted_text(TASK_RULE), GUI))
        if self.dry_run:
            self.to_create += 1
            return (head + "стало бы — правило будет создано: %s"
                    % wanted_text(TASK_RULE))
        try:
            self.client.post(self.project + "/protected_branches", {
                "name": pattern,
                "push_access_level": DEVELOPER,
                "merge_access_level": DEVELOPER,
                "allow_force_push": False})
            after = self.client.get("%s/protected_branches/%s" % (
                self.project, urllib.parse.quote(pattern, safe="")))
        except kit.ApiError as err:
            if err.transient:
                raise kit.ToolError("правило %s не поставлено, площадка не "
                                    "ответила: %s" % (pattern, err))
            self.problems += 1
            return head + "стало — РАСХОЖДЕНИЕ: площадка отказала: %s" % err
        self.created += 1
        if is_wanted(after, TASK_RULE):
            return head + "стало — %s" % describe(after)
        self.problems += 1
        return (head + "стало — %s; РАСХОЖДЕНИЕ — не совпадает с нужным "
                "(%s)" % (describe(after), wanted_text(TASK_RULE)))

    def main_rule(self, rules):
        """Основную ветку средство только сверяет. При нескольких правилах,
        подходящих к ветке, GitLab берёт самое мягкое, поэтому другое
        правило, подходящее к основной ветке, — тоже расхождение."""
        rule = rules.get(self.main)
        lines = []
        head = ("основная ветка %s: было — %s; стало — без изменений, "
                "средство её только сверяет" % (self.main, describe(rule)))
        if is_wanted(rule, MAIN_RULE):
            lines.append(head + " — как нужно")
        else:
            self.problems += 1
            lines.append(head + "; РАСХОЖДЕНИЕ — нужно: %s; поправьте "
                         "сами в %s" % (wanted_text(MAIN_RULE), GUI))
        for name in sorted(rules):
            if name != self.main and fnmatch.fnmatchcase(self.main, name):
                self.problems += 1
                lines.append("основная ветка %s: РАСХОЖДЕНИЕ — к ней "
                             "подходит и правило %s (%s): из подходящих "
                             "правил GitLab берёт самое мягкое"
                             % (self.main, name, describe(rules[name])))
        return lines

    def merge_settings(self, project):
        """Настройки слияния только сверяются."""
        lines = []

        def line(name, value, good, wanted):
            shown = "нет в ответе площадки" if value is None else value
            if value is not None and good:
                lines.append("%s: %s — как нужно" % (name, shown))
            else:
                self.problems += 1
                lines.append("%s: %s — РАСХОЖДЕНИЕ: нужно %s; поправьте "
                             "сами в %s" % (name, shown, wanted, MERGE_GUI))

        method = project.get("merge_method")
        line("способ слияния", method, method == "merge",
             "merge — «Merge commit»")
        squash = project.get("squash_option")
        line("склейка при слиянии", squash, squash == "never",
             "never — «Do not allow», склейка запрещена")
        remove = project.get("remove_source_branch_after_merge")
        line("удаление ветки после слияния по умолчанию",
             None if remove is None else ("включено" if remove
                                          else "выключено"),
             remove is False, "выключено — ветку после слияния не удаляют")
        return lines

    def run(self):
        try:
            rules = dict((r.get("name"), r) for r in self.client.get_all(
                self.project + "/protected_branches"))
            project = self.client.get(self.project) or {}
        except kit.ApiError as err:
            raise kit.ToolError("правила защиты и настройки проекта не "
                                "прочитаны: %s" % err)
        for pattern in self.patterns:
            print(self.task_rule(pattern, rules))
        for text in self.main_rule(rules):
            print(text)
        for text in self.merge_settings(project):
            print(text)
        total = len(self.patterns)
        if self.dry_run:
            print("итог без изменений (--dry-run): правил веток задач %d: "
                  "создать нужно %d, уже есть %d; расхождений %d"
                  % (total, self.to_create, self.unchanged, self.problems))
        else:
            print("итог: правил веток задач %d: создано %d, без изменений "
                  "%d; расхождений %d"
                  % (total, self.created, self.unchanged, self.problems))
        if self.problems or self.to_create:
            return kit.FOUND
        return kit.OK


def parse(argv):
    parser = kit.make_parser(
        TOOL,
        "Защита веток задач по префиксам из настройки, сверка защиты "
        "основной ветки и настроек слияния — запускает только владелец "
        "своим ключом в переменной GITLAB_TOKEN.")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что было бы сделано; "
                             "ничего не менять")
    parser.add_argument("--host", metavar="<адрес>",
                        help="другая площадка вместо поля «площадка», "
                             "«адрес» настройки")
    kit.add_config_argument(parser)
    return parser.parse_args(argv)


def main(argv=None):
    kit.setup_output()
    args = parse(argv)
    try:
        cfg = kit.load_config(args.config)
        host = kit.platform_host(cfg, args.host)
        client = kit.Client(host, kit.require_token())
        me = kit.check_account(client, cfg, "owner")
        print("учётная запись ключа: %s — владелец по настройке; "
              "репозиторий %s%s" % (me, cfg["площадка"]["репозиторий"],
                                    "; --dry-run: ничего не меняется"
                                    if args.dry_run else ""))
        return Protector(client, cfg, args.dry_run).run()
    except kit.ToolError as err:
        return kit.report(err)
    except KeyboardInterrupt:
        print("прервано; правила, о которых строк ещё нет, не ставились",
              file=sys.stderr)
        return kit.INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
