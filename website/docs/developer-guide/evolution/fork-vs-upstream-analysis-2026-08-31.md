# Форк vs upstream: що краще і що гірше для агента

**Дата:** 2026-08-31
**Форк:** `Lexus2016/hermes-agent-evolution` (`HEAD` = `2ada986657`)
**Upstream:** `NousResearch/hermes-agent` (`upstream/main`)
**Merge-base:** `fc0a10a924`

Це повний запис аналізу, зробленого в сесії: порівняння форку з основним проєктом
за **фактичною поведінкою агента**, а не за наявністю файлів у дереві.

---

## Метод

1. `git fetch upstream main`, підрахунок розбіжності комітів.
2. Список файлів, доданих у форку (`git diff --diff-filter=A upstream/main...HEAD`).
3. Перевірка, які форк-only модулі **реально імпортуються** з `run_agent.py`,
   `agent/conversation_loop.py`, `agent/prompt_builder.py`, `agent/system_prompt.py`,
   `model_tools.py`, `agent/agent_init.py`.
4. Різниця `DEFAULT_CONFIG` (top-level і `agent.*` / `memory.*`).
5. Різниця `toolsets.py` і schema-footprint.
6. Різниця блоків system prompt у `agent/prompt_builder.py`.
7. Нотатки tqmemory про sync, loop_guard, evolution-конвеєр.

Критерій класифікації: **чи змінює це дефолтний хід чату** (промпт, схема
інструментів, зайві LLM-виклики, hard-stop, cron поза чатом). Модулі, які
лежать у дереві, але `enabled: false` / не імпортуються — у «краще/гірше»
не зараховані як runtime-фіча.

---

## Масштаб розбіжності

| Метрика | Значення |
|---|---|
| Комітів форку попереду upstream | ~1390 |
| Комітів upstream попереду форку | 3 |
| Унікальних non-merge комітів форку | 1212 |
| Доданих файлів (усі шляхи) | 847 |
| Форк-only модулів у `agent/` | ~70 |
| Cron-джобів `cron/evolution/` | 22 |
| Скілів `skills/evolution/` | 9 |

Гарячі файли циклу агента (`git diff --stat upstream/main...HEAD`):

```
agent/conversation_loop.py |  695 ++++++++++++++-
agent/prompt_builder.py    |  569 +++++++++++-
agent/system_prompt.py     |  158 +++-
agent/tool_guardrails.py   |  758 ++++++++++++++--
model_tools.py             |  456 +++++++++-
run_agent.py               | 2075 ++++++++++++++++++++++++++++++++++++--------
tools/memory_tool.py       | 1282 +++++++++++++++++++++++++--
tools/terminal_tool.py     | 1414 ++++++++++++++++++++++++------
toolsets.py                |   12 +-
9 files changed, 6602 insertions(+), 817 deletions(-)
```

Унікальні top-level ключі `DEFAULT_CONFIG` форку: `patch`, `request_dump`.
Унікальні core-інструменти в `_HERMES_CORE_TOOLS`: `repo_map`, `team_task`,
`team_message`.

---

## Що в нас краще за основний агент

Це реальні переваги на дефолтному шляху, або з очевидним виграшем, коли фіча
ввімкнена свідомо.

### 1. Зупинка tool-спіралей

Upstream має `tool_loop_guardrails`, але **немає** `_SPIRAL_PRONE_TOOLS` і
always-on `spiral_failure_cap`.

У форку (`agent/tool_guardrails.py`):

- cap **5** (для `memory` — **3**) на `terminal`, `execute_code`, `read_file`,
  `search_files`, `patch`, `write_file`, `process`, `tool_call`, `tool_describe`;
- окремий `agent/loop_guard.py` (модуля в upstream немає): радить змінити
  стратегію; у cron після кількох ігнорованих nudge **жорстко ріже** застряглий
  job (`loop_guard_cron_hard_stop`).

На практиці: менше спалювання бюджету на 15–65 однакових `terminal`/`patch`.
Upstream у таких випадках часто докручує до `max_iterations`.

Джерела: `agent/loop_guard.py`, `agent/conversation_loop.py` (~рядки 2410–2526),
`agent/tool_guardrails.py` (`_SPIRAL_PRONE_TOOLS`, `spiral_failure_cap`).

### 2. Діагностика відмов інструментів

У `tools/terminal_tool.py` є класифікація `retry_spiral`, урахування timeout на
exception-path, enrichment помилок `search_files` / `patch`. Модель частіше бачить
**чому** впало і **що змінити**, а не сирий stderr.

Пов’язані модулі: `tools/terminal_failure_classifier.py`,
`tools/tool_failure_classifier.py`, `agent/tool_diagnostics.py`,
`tools/recovery_strategy_dispatcher.py` (останній — config-gated, default-off).

### 3. Відновлення після м’якої відмови

До 2 synthetic nudge (`advisory` → `directive`), якщо модель пише «не можу /
немає доступу» замість виклику інструмента (`agent/conversation_loop.py` ~9207).

Разом зі статичним `RECOVERY_BEFORE_REFUSAL_GUIDANCE` (~180 токенів у кешованому
префіксі) це зменшує клас «відмовився, хоча `terminal` / `search_files` уже є».

### 4. Пам’ять, яка менше отруює наступні сесії

У форку, на відміну від upstream:

- **auto-evict** найстаріших записів при переповненні (увімкнено за замовчуванням
  у `MemoryStore`, `auto_evict_on_full=True`);
- **TEPA** (`#154`, коміт `2ada986657`) — явний `validity` / `revoked` на
  епізодичних подіях; відкликані не потрапляють у звичайний retrieval
  (`agent/memory_importance.py`);
- provenance / trust-tier на записах (`tools/memory_tool.py`:
  `_make_provenance`, `encode_provenance`, …);
- опційний **MemGuard** (block/warn/strip за політикою; default-off,
  `agent/memory_guard.py`).

Upstream-пам’ять тупіша: переповнення й суперечності гірше обробляються,
застаріле «я знаю X» живе довше.

### 5. `repo_map`

Окремий read-only інструмент (`tools/repo_map.py`, issue `#320`) у file/core
toolset. Один огляд символів Python-дерева замість сліпого `search_files` →
`read_file`. Для роботи по Python-репозиторію це сильніше за «лиш grep».

Обмеження (важливо для оцінки вартості): stdlib `ast`, лише Python, без JS/TS/Go,
без кешу. Для не-Python дерев інструмент слабкий.

### 6. Безпека інструкцій ззовні

- Статичний `UNTRUSTED_CONTENT_GUIDANCE` (~280 токенів) у system prompt: вивід
  cron, subagent, файли стану — **дані, не команди**.
- Скіли `skills/security/skill-audit`, `skills/security/ai-safe-audit`.
- `write_guard` (`agent/write_guard.py`, default `mode=off`) дає default-deny на
  destructive, якщо його ввімкнути.

Upstream цього шару в промпті не має.

### 7. Самопокращення як продукт (якщо це мета форку)

22 cron-джоби в `cron/evolution/` + 9 скілів `evolution-*` + гейт PR/CI
(`EVOLUTION_README.md`, `scripts/register_evolution_cron.py`). Upstream цього
конвеєра не має.

Для **власника форку**, який хоче автономний R&D, це унікальна перевага. Для
звичайного користувача чату — див. розділ «гірше».

Реєстрація: `upgrade.sh` викликає `scripts/register_evolution_cron.py` під час
`hermes update` (ідемпотентно за ім’ям джоби).

### 8. `tqmemory` як друге сховище

`TQMEMORY_GUIDANCE` інжектиться **лише** коли MCP-інструменти `mcp_tqmemory_*`
реально є в `valid_tool_names` (`agent/system_prompt.py` ~608–615). Тоді агент
має дешеве міжсесійне сховище рішень/уроків без роздування вбудованої MEMORY.md.

CLI: `hermes_cli/tqmemory_setup.py`. Upstream цього інтеграційного контракту не має.

### 9. Командна робота агентів (gated)

`team_task` / `team_message` (`tools/agent_team_tools.py`) стоять у
`_HERMES_CORE_TOOLS`, але `check_fn=check_agent_team_requirements` ховає їх без
`HERMES_TEAM_ID`. У звичайному чаті schema **не їде**. У teammate-сесії — фіча,
якої в upstream немає.

---

## Що в нас погіршує роботу агента порівняно з основним

Це те, за що користувач **платить на кожному ході**, або чим агент **заважає
собі**, навіть якщо фіча «правильна на папері».

### 1. Завжди ввімкнений зайвий system prompt (~1000 токенів)

Форк додає в **кешований** префікс блоки, яких немає в upstream
(`agent/prompt_builder.py` → `agent/system_prompt.py`):

| Блок | ~символів | ~токенів | Ефект |
|---|---|---|---|
| `ATTENTION_RESET_GUIDANCE` | 1112 | ~280 | дешево, корисно |
| `UNTRUSTED_CONTENT_GUIDANCE` | 1125 | ~280 | дешево, безпека |
| `RECOVERY_BEFORE_REFUSAL_GUIDANCE` | 738 | ~180 | дешево, корисно |
| `DELIBERATE_WORK_GUIDANCE` | 937 | ~230 | **проблема** |
| `TQMEMORY_GUIDANCE` (лише з MCP) | 3185 | ~800 | gated |

`TASK_COMPLETION_GUIDANCE` є і в upstream — у різницю не входить.

«Оціни роботу 1–10 і крути, доки не буде 10» (`DELIBERATE_WORK_GUIDANCE`) штовхає
модель на **зайві ходи** навіть на середніх задачах: ще один `read_file`, ще один
self-review, ще один патч. Upstream цього ритуалу не нав’язує. На довгій сесії
це множиться на вартість кожного API-виклику.

Блоки статичні → prompt-cache префікса **не ламається**. Платиш токенами
префікса на **кожній** новій сесії і увагою моделі всередині сесії.

### 2. Synthetic user-повідомлення від `loop_guard` і refusal-nudge

Охоронці **дописують `role: user`** у середину ходу
(`agent/conversation_loop.py`):

```python
messages.append({"role": "user", "content": _lg_nudge})
```

Це б’є по інваріантах Hermes (див. `AGENTS.md`):

- ламає prefix-cache (суфікс після nudge — новий);
- порушує «не інжектити синтетичний user mid-loop»;
- додає 1–2 зайві LLM-виклики.

На спіралі це рятує гроші. На легітимній роботі (п’ять `search_files` з різними
патернами, серія `read_file`, повторний `process poll`) — **збиває стратегію** і
іноді ріже хід як `loop_guard_interactive_hard_stop` / `session_hard_stop`.

Upstream так не робить. Відомий побічний ефект: desktop E2E
`interim-messages.spec.ts` ламався саме через цей інжект (нотатка tqmemory
`a20ab583a4684379`).

### 3. Агресивний always-on spiral cap

Cap **5** на `read_file` і `search_files` — занадто низький для реальної розвідки
кодової бази. П’ять порожніх/неточних пошуків або п’ять невдалих `patch` з
однаковим класом помилки **завершують хід**, навіть якщо агент уже близько.

Upstream дозволяє довшу розвідку (і платить за це спіралями). Форк інвертує
помилку: **рідше зациклюється, частіше здається рано**.

Окремо: streak **не скидається одним успіхом** (`_SUCCESSES_TO_DECAY = 2` у
`agent/tool_guardrails.py`). Патерн fail → `pwd`/`ls` → fail, який сам гайд
рекомендує як діагностику, **накручує cap**. Це прод-кейс `#1585`.

### 4. `repo_map` у schema кожного виклику

Інструмент корисний, але він у `_HERMES_CORE_TOOLS` і в toolset `file`. Кожен
API-виклик несе його schema. Upstream цього не платить.

Для не-Python репозиторіїв інструмент слабкий, тож модель може викликати його
**даремно**.

`team_task`/`team_message` у core list schema в звичайному чаті не їдуть
(`check_fn` на `HERMES_TEAM_ID`) — це не той самий клас вартості.

### 5. Evolution-конвеєр на звичайній установці

`upgrade.sh` **автоматично** реєструє ~22 cron-джоби. На публічній установці
щодня йдуть research / issues; на private — ще analysis / implementation /
hydra / dream / pr-reflection тощо.

Наслідки проти «чистого» Hermes:

- спалювання токенів і rate-limit **поза** чатом користувача;
- шум у GitHub (слабкі пропозиції, які потім треба тріажити);
- конкуренція з користувацькими cron за scheduler;
- 9 скілів `evolution-*` лежать у `skills/` і потрапляють у каталог скілів
  звичайної сесії — модель може піти в «research other agents» замість задачі
  користувача.

Для чат-агента це **регресія корисності**. Для еволюційного бота — фіча.

Список скілів у дефолтному дереві:

- `skills/evolution/evolution-research`
- `skills/evolution/evolution-issues`
- `skills/evolution/evolution-analysis`
- `skills/evolution/evolution-implementation`
- `skills/evolution/evolution-integration`
- `skills/evolution/evolution-introspection`
- `skills/evolution/evolution-orchestrator`
- `skills/evolution/evolution-extract`
- `skills/evolution/evolution-upstream-sync`

Плюс не-evolution форк-скіли: `skills/a2a`, `skills/memory-consolidation`,
`skills/productivity/adhd-output`, `skills/productivity/gui-automation`,
`skills/productivity/memory-audit`, `skills/security/ai-safe-audit`,
`skills/security/skill-audit`, `skills/software-development/predict-then-act`.

### 6. Мертва й напівжива інфраструктура в гарячому шляху

У `agent/` десятки форк-only модулів. Більшість **off by default**, але:

- `task_decoupling` і `architecture_router` **імпортуються на кожному ході**
  (`agent/conversation_loop.py` ~2218–2266) — try/except no-op, якщо `enabled`
  false. Сам роутер коментує: *without changing how this turn executes*;
- `echoleak_defense.py` **ніде не імпортується** поза тестами — dead weight;
- ~50 файлів `evolution/lib/` — research-spike, не runtime чату.

Це не робить відповідь розумнішою. Це робить sync з upstream важчим, тести
крихкішими, а «фічі» виглядають існуючими, поки їх не ввімкнеш.

Приклади модулів, вимкнених за замовчуванням (перевірено по `enabled: False` /
`mode: "off"` у коді):

| Модуль | Гейт | Default |
|---|---|---|
| `agent/architecture_router.py` | `architecture_router.enabled` | off |
| `agent/task_decoupling.py` | `task_decoupling` | off |
| `agent/skill_routing.py` | `skill_routing.listwise_rerank` | off |
| `agent/plan_mode.py` | `plan_mode` / `HERMES_PLAN_MODE` | off |
| `agent/plan_feasibility.py` | `plan_feasibility.enabled` | off |
| `agent/write_guard.py` | `write_guard.mode` | `off` |
| `agent/memory_guard.py` | `memory.guard` | off (повертає `None`) |
| `agent/experience_bank.py` | `agent.experience_injection` | false |
| `agent/failure_diagnosis.py` | `failure_diagnosis.mode` | `off` |
| recovery dispatcher | `tool_failure_recovery.enabled` | false |
| `agent/recheck_suppression.py` | `recheck_suppression.enabled` | off |

### 7. Шви після sync з upstream

Повторюваний клас багів: після злиття тегів на кшталт `v2026.8.27` **зникали**
fork-контракти (`classify_file_error`, batch-read, `tool_describe` flat contract,
spiral на timeout). Агент у цей момент **гірший за обидва**: ні повний upstream,
ні повний форк.

Основний репозиторій цього класу не має, бо в нього немає другого шару патчів.
Перевірка виживання фіч: `scripts/verify_fork_features.py`.

### 8. Відмова, яку не треба було «лікувати»

Refusal-nudge спрацьовує на текст «I can't». Якщо обмеження **реальне** (немає
toolset, sandbox, policy) — форк витрачає ще 1–2 ітерації, перш ніж здатися.
Upstream просто відповідає.

---

## Ні те, ні інше, поки не ввімкнеш

Ці речі **є тільки в нас**, але default-off, тож на чесному порівнянні «як агент
працює з коробки» вони майже не грають:

- `plan_mode` / `plan_feasibility` — зайвий LLM-виклик на старті, якщо увімкнути;
- `write_guard` enforce — безпечніше, але ламає автономні правки;
- `skill_routing` listwise rerank;
- `failure_diagnosis`, `tool_failure_recovery`, `experience_injection`;
- A2A CLI (`hermes_cli/a2a.py`, `skills/a2a`);
- observational context engine (`plugins/context_engine/observational`);
- task-shield plugin (`plugins/task-shield`);
- `agent/agent_judge.py`, `agent/judge_calibration.py` — не в гарячому циклі чату.

Вмикати їх «бо є» — шлях до погіршення: зайві виклики, зайва схема, зайві гейти.

---

## Форк-only файли, які варто знати (не вичерпно)

**Цикл агента**

- `agent/loop_guard.py` — always-on
- `agent/tool_guardrails.py` — розширений relative до upstream
- `agent/prompt_builder.py` — додаткові guidance-блоки
- `agent/write_guard.py`, `agent/policy_interceptors.py`
- `agent/memory_guard.py`, `agent/memory_importance.py` (TEPA)
- `agent/runtime_harness.py` — wired у `tools/delegate_tool.py`

**Інструменти**

- `tools/repo_map.py`
- `tools/agent_team_tools.py`
- `tools/memory_governance.py`
- `tools/tool_circuit_breaker.py`
- `tools/content_provenance.py`

**Еволюція**

- `evolution/` (детектор режиму, lib, тести)
- `cron/evolution/*.yaml` (22 джоби)
- `skills/evolution/*`
- `hermes_cli/evolution_cmd.py`
- `scripts/register_evolution_cron.py`, `scripts/verify_fork_features.py`

**Документація форку**

- `EVOLUTION_README.md`
- `CONTRIBUTING_EVOLUTION.md`
- `SECURITY_EVOLUTION.md`
- `EVOLUTION_IMPROVEMENT_RECOMMENDATIONS.md`

---

## Висновок

Форк **кращий** там, де upstream палить бюджет на спіралях і м’яких відмовах, і
там, де пам’ять має не брехати завтрашньому «я». Форк **гірший** як щоденний
робочий агент там, де він:

1. роздуває system prompt ритуалом self-review-до-10;
2. ріже легітимну розвідку spiral cap’ом;
3. інжектить synthetic user і ламає кеш;
4. вішає на установку evolution-cron і evolution-скіли;
5. тягне шар модулів, які не керують ходом, але ускладнюють злиття з Nous.

Якщо ціль — **кращий агент для реальної роботи**, виграш форку майже весь у
вузькому поясі: loop/spiral/refusal + memory validity + `repo_map` +
untrusted-content. Решта evolution-поверхні для чату радше шум.

Якщо ціль — **агент, що змінює сам себе**, той шум і є продуктом — тоді
порівняння з Nous інше, і «гірше для чату» стає прийнятною ціною.

---

## Як відтворити вимір

```bash
git fetch upstream main
git rev-list --count upstream/main..HEAD          # попереду
git rev-list --count HEAD..upstream/main          # позаду
git log --oneline --no-merges upstream/main..HEAD | wc -l
git diff --name-status --diff-filter=A upstream/main...HEAD | wc -l
git diff --stat upstream/main...HEAD -- \
  agent/conversation_loop.py agent/prompt_builder.py agent/system_prompt.py \
  agent/tool_guardrails.py model_tools.py run_agent.py \
  tools/memory_tool.py tools/terminal_tool.py toolsets.py
```

Перевірка, що fork-фіча не зникла після sync:

```bash
python3 scripts/verify_fork_features.py snapshot \
  --fork origin/main --upstream upstream/main -o .evolution/fork-baseline.json
python3 scripts/verify_fork_features.py check \
  --baseline .evolution/fork-baseline.json
```
