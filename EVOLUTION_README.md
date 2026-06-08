# Hermes Evolution 🧬

> Self-evolving AI Agent based on [Hermes Agent](https://github.com/nousresearch/hermes-agent) by Nous Research

**Це форк Hermes Agent з вбудованим функціоналом саморозвитку.**

## 🎯 Концепція

Hermes Evolution — це AI агент, який:
- Досліджує інших AI агентів та академічні статті
- Створює proposals для покращення
- Аналізує та пріоритезує зміни
- Реалізує покращення та самооновлюється

## 🔄 Як це працює

```
┌─────────────────────────────────────────────────────────────┐
│                    HERMES EVOLUTION AGENT                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  PUBLIC Mode (всі інсталяції):                              │
│  ┌──────────────────┐         ┌──────────────────┐         │
│  │ DAILY RESEARCH   │────────▶│  ISSUE/PR CREATE │         │
│  │  (24h cron)      │         │  (read-only)     │         │
│  └──────────────────┘         └──────────────────┘         │
│           │                                                   │
│           ▼                                                   │
│    [Пропозиції змін]                                         │
│           │                                                   │
│  PRIVATE Mode (тільки власник):                              │
│           ▼                                                   │
│  ┌──────────────────┐         ┌──────────────────┐         │
│  │ ISSUE ANALYSIS   │────────▶│  IMPLEMENTATION  │         │
│  │  (24h cron)      │         │  (write + merge) │         │
│  └──────────────────┘         └──────────────────┘         │
│                                         │                    │
│                                         ▼                    │
│                                  ┌─────────────┐            │
│                                  │ SELF-UPDATE │            │
│                                  │ + RESTART   │            │
│                                  └─────────────┘            │
└─────────────────────────────────────────────────────────────┘
```

## 📅 Щоденний цикл

| Час | Задача | Режим |
|-----|-------|-------|
| 08:00 (неділя) | Sync з upstream Hermes Agent | PRIVATE |
| 09:00 | Research інших агентів та статей | PUBLIC |
| 12:00 | Створення issues/PR з пропозиціями | PUBLIC |
| 21:00 | Аналіз та пріоритезація issues | PRIVATE |
| 22:00 | Реалізація покращень | PRIVATE |

## 🆚 Відмінності від оригінального Hermes Agent

| Функціонал | Hermes Agent | Hermes Evolution |
|------------|--------------|------------------|
| Базові можливості агента | ✅ | ✅ |
| Skills & Tools | ✅ | ✅ |
| Cron Jobs | ✅ | ✅ |
| **Еволюційні skills** | ❌ | ✅ |
| **Автоматичний research** | ❌ | ✅ |
| **Автоматичне створення issues** | ❌ | ✅ |
| **Аналіз пріоритетів** | ❌ | ✅ |
| **Самооновлення** | ❌ | ✅ |
| **Sync з upstream** | ❌ | ✅ |

## 🚀 Встановлення

### 1. Клонування

```bash
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution
```

### 2. Налаштування

```bash
# Запустіть детектор режиму
python evolution/detect_mode.py

# Для PUBLIC mode (всі користувачі)
export GITHUB_TOKEN="your..."

# Для PRIVATE mode (власник репозиторію)
export GITHUB_PRIVATE_TOKEN="your..."
```

### 3. Налаштування cron jobs

```bash
# Research (PUBLIC mode)
hermes cron create --name evolution-research \
  --schedule "0 9 * * *" \
  --prompt "$(cat cron/evolution/research.yaml)" \
  --skills evolution/research

# Issues (PUBLIC mode)
hermes cron create --name evolution-issues \
  --schedule "0 12 * * *" \
  --prompt "$(cat cron/evolution/issues.yaml)" \
  --skills evolution/issues

# Analysis (PRIVATE mode only)
hermes cron create --name evolution-analysis \
  --schedule "0 21 * * *" \
  --prompt "$(cat cron/evolution/analysis.yaml)" \
  --skills evolution/analysis

# Implementation (PRIVATE mode only)
hermes cron create --name evolution-implement \
  --schedule "0 22 * * *" \
  --prompt "$(cat cron/evolution/implementation.yaml)" \
  --skills evolution/implementation

# Upstream Sync (PRIVATE mode only)
hermes cron create --name evolution-upstream \
  --schedule "0 8 * * 0" \
  --prompt "$(cat cron/evolution/upstream-sync.yaml)" \
  --skills evolution/upstream-sync
```

## 📚 Evolution Skills

### evolution/research
Досліджує інших агентів, статті, тренди для генерації ідей.

### evolution/issues
Створює GitHub issues та PR з пропозиціями.

### evolution/analysis
Аналізує issues та пріоритезує для реалізації (PRIVATE only).

### evolution/implementation
Реалізує обрані зміни та самооновлюється (PRIVATE only).

### evolution/upstream-sync
Синхронізується з upstream Hermes Agent (PRIVATE only).

## 🔐 Режими роботи

### PUBLIC Mode
- ✅ Дослідження
- ✅ Створення issues/PR
- ❌ Мердж PR
- ❌ Модифікація коду

### PRIVATE Mode
- ✅ Все що в PUBLIC mode
- ✅ Мердж PR
- ✅ Модифікація коду
- ✅ Самооновлення

## 🔄 Sync з Upstream

Hermes Evolution регулярно синхронізується з оригінальним Hermes Agent:

1. Отримує зміни з upstream
2. Аналізує кожну зміну
3. Визначає пріоритет інтеграції
4. Створює proposals для конфліктуючих змін
5. Інтегрує сумісні зміни

## 📖 Документація

- [AGENTS.md](AGENTS.md) — документація Hermes Agent (оригінальна)
- [CONTRIBUTING.md](CONTRIBUTING.md) — як контрибьютити
- [evolution/README.md](evolution/README.md) — документація еволюції (в розробці)

## 🤝 Контрибьюшн

Контриб'юшн вітається! Перш ніж PR:

1. Перевірте [CONTRIBUTING.md](CONTRIBUTING.md)
2. Запустіть тести: `pytest tests/`
3. Оновіть документацію

## 📄 Ліцензія

Apache 2.0 (наслідується від [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent))

## 🙏 Вдячність

- [Nous Research](https://nousresearch.com/) — оригінальний Hermes Agent
- Всім контриб'юторам Hermes Agent

---

**Це експеримент в self-improving AI systems.** ⚗️
