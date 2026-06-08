---
name: evolution-research
description: Research other AI agents, papers, and trends for Hermes Evolution improvements
version: 1.0.0
author: Hermes Evolution
category: evolution
---

# Evolution Research Skill

**Режим роботи:** PUBLIC (всі інсталяції)

## Завдання

Досліджувати інші AI агенти, академічні статті та тренди для генерації ідей покращення Hermes Evolution.

## Джерела дослідження

### GitHub Репозиторії конкурентних агентів
- https://github.com/microsoft/autogen
- https://github.com/anthropics/anthropic-sdk-python
- https://github.com/Significant-Gravitas/AutoGPT
- https://github.com/TransformerOptimus/SuperAGI
- https://github.com/e2b-dev/agent-evaluations

### arXiv
Категорії: cs.AI, cs.LG, cs.CL
Ключові слова: "agent", "autonomous", "LLM tool use", "multi-agent"

### Новини та дискусії
- Hacker News AI треди
- Reddit: r/ArtificialIntelligence, r/MachineLearning
- AI blogs (OpenAI, Anthropic, DeepMind)

## Процес дослідження

1. **Сканування джерел** з допомогою `web_search`
2. **Фільтрація** за актуальністю та новизною
3. **Класифікація** знахідок:
   - `[FEATURE]` — новий функціонал
   - `[IMPROVEMENT]` — покращення існуючого
   - `[REPLACEMENT]` — альтернатива існуючому
4. **Генерація звіту** з оцінкою impact/effort

## Вихідний формат

Збережи результат в `~/.hermes/profiles/user1/evolution/research/YYYY-MM-DD.md`:

```markdown
# Research Report - YYYY-MM-DD

## New Features

### [FEATURE] Better memory management
- **Source**: https://github.com/microsoft/autogen/pull/123
- **Impact**: High
- **Effort**: Medium
- **Priority Score**: 1.6 (0.8 * 2 / 0.5)

Description...

## Improvements
...

## Replacements
...
```

## Обмеження

- Максимум 20 пропозицій за один раз
- Тільки високоякісні, добре обґрунтовані ідеї
- Priority Score >= 0.7

## Інтеграція

Після дослідження виклич `evolution-issues` skill для створення GitHub issues.
