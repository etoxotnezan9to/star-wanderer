# GitLab Access Tokens for branchkittest

Проект: `isavichev/branchkittest` (id 5375)
Площадка: `http://git.nic.etu`
Создано: 2026-11-24
Действуют до: 2027-11-24

## Токены по ролям

| Роль | Имя токена | Токен | Права |
|------|-----------|-------|-------|
| **claude-dev** (автор) | claude-dev | `glpat-T61_xKdrrTJzdaBo_Vs2` | api, read_repository, write_repository |
| **ai-dev** (автор) | ai-dev | `glpat-pJQJ9bcAGVFsVe1Rn65Q` | api, read_repository, write_repository |
| **claude-reviewer** (проверяющий) | claude-reviewer | `glpat-GhEEf1NvhrPFDb8LxsHx` | api, read_repository |
| **ai-reader** (постановщик) | ai-reader | `glpat-SRqGReKQLt_dTs1_CzEJ` | api, read_repository |

## Использование

### Через API (curl)

```bash
# Автор
curl -H "PRIVATE-TOKEN: glpat-T61_xKdrrTJzdaBo_Vs2" http://git.nic.etu/api/v4/...

# Проверяющий
curl -H "PRIVATE-TOKEN: glpat-GhEEf1NvhrPFDb8LxsHx" http://git.nic.etu/api/v4/...
```

### Через git (credential helper)

```bash
# Для claude-dev
git -c credential.helper='!f() { echo "username=oauth2"; echo "password=glpat-T61_xKdrrTJzdaBo_Vs2"; }; f' clone http://git.nic.etu/isavichev/branchkittest.git

# Для ai-dev
git -c credential.helper='!f() { echo "username=oauth2"; echo "password=glpat-pJQJ9bcAGVFsVe1Rn65Q"; }; f' clone http://git.nic.etu/isavichev/branchkittest.git
```

### Переменные окружения

```bash
# Автор
export GITLAB_TOKEN=glpat-T61_xKdrrTJzdaBo_Vs2

# Проверяющий
export GITLAB_TOKEN=glpat-GhEEf1NvhrPFDb8LxsHx

# Постановщик
export GITLAB_TOKEN=glpat-SRqGReKQLt_dTs1_CzEJ
```

## Примечание

Эти токены — **project access tokens**, привязанные к конкретному проекту.
Они не дают права на другие проекты или на администрирование GitLab.
