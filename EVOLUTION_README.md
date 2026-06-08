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
  --skills evolution-research

# Issues (PUBLIC mode)
hermes cron create --name evolution-issues \
  --schedule "0 12 * * *" \
  --prompt "$(cat cron/evolution/issues.yaml)" \
  --skills evolution-issues

# Analysis (PRIVATE mode only)
hermes cron create --name evolution-analysis \
  --schedule "0 21 * * *" \
  --prompt "$(cat cron/evolution/analysis.yaml)" \
  --skills evolution-analysis

# Implementation (PRIVATE mode only)
hermes cron create --name evolution-implement \
  --schedule "0 22 * * *" \
  --prompt "$(cat cron/evolution/implementation.yaml)" \
  --skills evolution-implementation

# Upstream Sync (PRIVATE mode only)
hermes cron create --name evolution-upstream \
  --schedule "0 8 * * 0" \
  --prompt "$(cat cron/evolution/upstream-sync.yaml)" \
  --skills evolution-upstream-sync
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

## 🛡️ Гейт безпечної самоеволюції

Агент пише код автономно. Без гейту зламаний або ін'єктований код потрапив би
в `main`, і авто-апдейт розніс би його на всі інсталяції за 24 год. Тому
злиття контролює **інфраструктура, а не самооцінка LLM**:

1. **PR-only.** `evolution-implementation` лише створює PR (`gh pr create`) і
   НЕ робить `git merge` / `git checkout main`. Прямий merge заборонено.
2. **CI-гейт.** На кожен PR у `main` запускаються `.github/workflows/tests.yml`
   і `lint.yml`. Червоні тести = merge заблоковано.
3. **Захист критичних шляхів.** `.github/CODEOWNERS` вимагає рев'ю власника для
   PR, що чіпають self-update, планувальник, CI чи evolution-skills.
4. **Авто-апдейт тягне лише CI-захищений `main`** — `auto_update.sh` оновлюється
   на код, який уже пройшов перевірку.

### Увімкнути branch protection (ОБОВ'ЯЗКОВО)

Без захисту гілки PR-only — лише інструкція, яку LLM може обійти. Enforcement
вмикає власник репозиторію:

```bash
gh api -X PUT repos/Lexus2016/hermes-agent-evolution/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["Tests"] },
  "enforce_admins": true,
  "required_pull_request_reviews": { "require_code_owner_reviews": true,
    "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

- `contexts: ["Tests"]` — назва перевірки з `tests.yml` (`name: Tests`); додай
  інші (наприклад lint) за фактичними назвами в Actions.
- `require_code_owner_reviews` + `count: 0` — звичайні PR зливаються на зеленому
  CI без рев'ю (автономність), а PR на критичні шляхи з `CODEOWNERS` усе одно
  потребують підтвердження власника.
- Для повного «людина в циклі» постав `required_approving_review_count: 1`.

> ⚠️ Без цього кроку гейт неповний: skill каже «лише PR», але ніщо технічно не
> заважає агенту змерджити напряму.

## 🤖 Bot-акаунт для агента (для критичних PR)

Захист гілки забороняє автору апрувити власний PR. Якщо агент пушить під
акаунтом власника, власник не зможе рев'ювати агентські PR на критичні шляхи
(`CODEOWNERS`) — вони зависнуть назавжди. Тому агент має діяти під ОКРЕМИМ
bot-акаунтом.

### Налаштування (один раз)

1. **Створи окремий GitHub-акаунт** для бота (напр. `hermes-evo-bot`). Це робить
   людина — агент не створює акаунти.
2. **Додай бота як collaborator** з правом write на репозиторій
   (`Settings → Collaborators`).
3. **Створи fine-grained PAT** від імені бота, з доступом ЛИШЕ до цього репо:
   - Repository access: тільки `hermes-agent-evolution`
   - Permissions: Contents (RW), Pull requests (RW), Issues (RW) — і нічого більше.
4. **Налаштуй сервер діяти як бот** (токен через env, не аргумент):
   ```bash
   export GITHUB_EVOLUTION_TOKEN=<bot-pat>
   bash scripts/setup_evolution_bot.sh
   ```
   Скрипт залогінить `gh` як бота, підключить його як git credential і виставить
   git identity. Токен ніде не друкується.

### Як це працює далі

- Агент створює PR під `hermes-evo-bot` → ти (власник + code owner) рев'юєш
  критичні PR і зливаєш; звичайні PR зливаються на зеленому CI без рев'ю.
- Токен бота обмежений одним репо → навіть при компрометації агента (через
  ін'єкцію) зловмисник не дістане інших твоїх репозиторіїв.

> Зберігай bot-PAT у secrets-сховищі / env з `chmod 600`, НЕ в коді чи git URL.

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
