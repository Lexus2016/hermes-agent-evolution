---
name: evolution-analysis
description: Analyze issues and PRs to prioritize implementation (PRIVATE mode only)
version: 1.0.0
author: Hermes Evolution
category: evolution
mode: PRIVATE
---

# Evolution Analysis Skill

**Режим роботи:** PRIVATE (тільки для власника репозиторію)

## Завдання

Аналізувати всі створені issues та PR, визначати пріоритет для реалізації.

## Процес

1. **Отримання** всіх відкритих issues через GitHub API:

```bash
GET https://api.github.com/repos/Lexus2016/hermes-agent-evolution/issues?state=open
```

2. **Оцінювання** кожного issue за критеріями:

### Impact (Вплив)
- Critical: 1.0 (безпека, критичні баги)
- High: 0.8 (нові функції)
- Medium: 0.5 (покращення UX)
- Low: 0.2 (мінімальні зміни)

### Effort (Зусилля)
- Trivial: 0.1 (< 1 година)
- Easy: 0.3 (< 4 години)
- Medium: 0.5 (< 2 дні)
- Hard: 0.8 (< 1 тиждень)
- Very Hard: 1.0 (> 1 тиждень)

### Додаткові фактори
- Community interest: 👍 / 10 (max 1.0)
- Age: days / 30 (max 1.0)
- Compatibility: 1.0 (добре) / 0.5 (треба рефакторинг) / 0.1 (ламає)
- Safety: 0.0 (ризиковано) / 0.5 (треба тести) / 1.0 (безпечно)

3. **Обчислення Priority Score**

```python
base_priority = (impact * 2) / effort
final_priority = base_priority + community*0.1 + age*0.05 + compatibility*0.2 + safety*0.3
```

4. **Вибір** top 5 для реалізації:
   - Min priority: 0.7
   - Max total effort: 2.0

## Вихідний формат

Збережи в `~/.hermes/profiles/user1/evolution/analysis/YYYY-MM-DD.json`:

```json
{
  "date": "2026-06-08",
  "selected_for_implementation": [
    {
      "issue_number": 123,
      "title": "[FEATURE] Better memory",
      "priority_score": 3.3,
      "impact_score": 0.8,
      "effort_score": 0.5,
      "estimated_hours": 24
    }
  ]
}
```

## Безпека

Якщо GITHUB_PRIVATE_TOKEN не встановлений — **АБОРТ**. Цей skill працює тільки в PRIVATE mode.
