---
name: evolution-implementation
description: Implement selected issues and self-update (PRIVATE mode only)
version: 1.0.0
author: Hermes Evolution
category: evolution
mode: PRIVATE
---

# Evolution Implementation Skill

**Режим роботи:** PRIVATE (тільки для власника репозиторію)

## Завдання

Реалізовувати обрані issues, створювати версії, і самооновлюватися.

## Процес

1. **Завантаження** останнього аналізу з `~/.hermes/profiles/user1/evolution/analysis/`

2. **Реалізація** кожного обраного issue:

### Створення гілки
```bash
git checkout main
git pull origin main
git checkout -b evolution/issue-123-feature-name
```

### Реалізація змін
- Створити/модифікувати файли
- Додати тести
- Додати документацію

### Коміт
```bash
git add .
git commit -m "feat: implement feature name

Closes #123

Co-Authored-By: Hermes Evolution <evolution@hermes.ai>"
```

### Створення PR
```bash
git push origin evolution/issue-123-feature-name
```

3. **Мerging** (для safe changes)

Для non-breaking змін з priority > 0.7:

```bash
# Merge via terminal tool
git checkout main
git merge evolution/issue-123-feature-name --squash
git commit -m "Merge evolution/issue-123"
```

4. **Версіонування**

Semantic versioning:
- MAJOR: Breaking changes
- MINOR: New features
- PATCH: Bug fixes

```bash
# Bump version
git tag -a v0.2.0 -m "Release v0.2.0: New evolution features"
git push origin v0.2.0
```

5. **Самооновлення**

```bash
# Pull latest
git pull origin main

# Prepare restart info
echo "AGENT_VERSION=$(git describe --tags)" > ~/.hermes/evolution_status
```

## Safety Checks

ПЕРЕД merging:
- [ ] Тести проходять
- [ ] Документація оновлена
- [ ] Breaking changes задокументовані
- [ ] Не більше 3 auto-merges на день

## Rollback

Якщо щось піде не так:
```bash
git checkout v0.1.0  # previous version
git tag -a v0.2.1 -m "Rollback"
```

## Ліміти

- Максимум 5 реалізацій на день
- Максимум 3 auto-merges на день
- Breaking changes завжди потребують manual review
