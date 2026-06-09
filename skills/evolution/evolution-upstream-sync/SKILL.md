---
name: evolution-upstream-sync
description: Sync with upstream Hermes Agent and integrate relevant changes
version: 1.0.0
author: Hermes Evolution
category: evolution
mode: PRIVATE
---

# Evolution Upstream Sync Skill

**Режим роботи:** PRIVATE (тільки для власника репозиторію)

## Завдання

Синхронізуватися з оригінальним Hermes Agent (upstream) та визначити які зміни треба інтегрувати.

## Процес

1. **Отримати зміни з upstream:**

```bash
git fetch upstream
git log main..upstream/main --oneline
```

2. **Аналізувати зміни:**

Категорії змін:
- **Bug fixes** — критичні виправлення, треба інтегрувати
- **Security fixes** — виправлення безпеки, обов'язково
- **Performance improvements** — покращення продуктивності
- **New features** — нові функції оригінального Hermes
- **Refactoring** — рефакторинг, може конфліктувати з нашими змінами
- **Documentation** — оновлення документації
- **Tests** — оновлення тестів

3. **Оцінити кожну зміну:**

### Вплив на еволюційні зміни
- **Conflicts** — конфліктує з нашими модифікаціями → треба manual merge
- **Compatible** — сумісно → можна автоматично мерджити
- **Enhances** — покращує наші зміни → пріоритет

### Пріоритет інтеграції
1. **Critical**: Security, bug fixes (must have)
2. **High**: Performance, critical features (should have)
3. **Medium**: New features (nice to have)
4. **Low**: Documentation, tests (optional)

4. **Створити пропозиції:**

Для кожної релевантної зміни створити issue:

```markdown
# [UPSTREAM] Integrate upstream fix: description

## Upstream Change
- Commit: abc123
- Author: original author
- PR: link to upstream PR

## Description
What changed in upstream...

## Impact on Evolution
- Conflicts: Yes/No
- Enhances evolution: Yes/No
- Breaking: Yes/No

## Recommendation
- [ ] Auto-merge (if compatible)
- [ ] Manual merge (if conflicts)
- [ ] Skip (if not relevant)

## Implementation Plan
1. Cherry-pick commit
2. Resolve conflicts
3. Test evolution features
4. Update docs
```

## Частота синхронізації

Рекомендується:
- **Weekly** — повний синк та аналіз
- **After critical updates** — якщо в upstream critical fixes

## Безпека

1. **Завжди роби в окремій гілці:**
```bash
git checkout -b sync/upstream-YYYY-MM-DD
```

2. **Тестуй після merge:**
- Переконайся що evolution features працюють
- Запусти тести

3. **Rollback якщо щось зламалося:**
```bash
git revert -m 1 <merge-commit>
```

## Стратегія merge — ЛИШЕ через PR (safety-гейт)

⛔ НЕ мерджити upstream напряму в `main`. Як і `evolution-implementation`,
upstream-зміни йдуть **через окрему гілку + PR + CI** — НЕ прямий merge:

```bash
# 1. Окрема гілка від актуального main:
git checkout main && git pull && git checkout -b sync/upstream-YYYY-MM-DD

# 2. Перенести лише ПОТРІБНІ коміти:
git cherry-pick <commit-hash>          # для сумісних змін
# або для конфліктних:
git merge upstream/main --no-commit    # вирішити конфлікти, потім: git commit

# 3. Створити PR (НЕ зливати в main вручну):
git push origin sync/upstream-YYYY-MM-DD
gh pr create --base main --head sync/upstream-YYYY-MM-DD \
  --title "[UPSTREAM] Sync: <summary>" \
  --body "Cherry-picked relevant upstream changes. See upstream sync report."
```

Злиття в `main` — лише після зелених CI (`tests.yml`/`lint.yml`) і за branch
protection. Зміни в критичних шляхах (`.github/CODEOWNERS`) потребують рев'ю
власника. Це той самий гейт, що захищає всю самоеволюцію — upstream-код теж
недовірений, доки не пройшов CI + рев'ю.

## Вихідний формат

Збережи звіт в `~/.hermes/profiles/user1/evolution/upstream/YYYY-MM-DD.md`:

```markdown
# Upstream Sync Report - YYYY-MM-DD

## Summary
- Total commits: 42
- Relevant changes: 8
- Conflicts: 2
- Auto-merge candidates: 5

## Relevant Changes

### [CRITICAL] Security fix in auth
- Commit: def456
- Conflicts: No
- Action: Auto-merge

### [FEATURE] New tool integration
- Commit: ghi789
- Conflicts: Yes (with evolution/tools)
- Action: Manual merge

## Implementation Plan
1. Cherry-pick def456 (auto)
2. Manual merge ghi789
...
```

## Ліміти

- Не більше 10 upstream commits за один раз
- Critical changes — пріоритет
- Breaking changes — завжди manual review
