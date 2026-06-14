# Рекомендації по покращенню до цілі — автономний самоеволюціонуючий агент

**Дата:** 2026-06-13
**Базується на:** аудиті 7 скілів, 9 cron-задач, 7 допоміжних скриптів, конфігурації безпеки

---

## РІВЕНЬ 1 — ВИПРАВИТИ ЗАРАЗ (тиждень)

### 1.1. БАГ ACCESS GATE — Bearer ***

`scripts/evolution_access_gate.sh`, рядок 30. Замінити:

```diff
-        curl -fsS -H "Authorization: Bearer *** \
+        curl -fsS -H "Authorization: Bearer $_tok" \
```

Це блокує всю самоеволюцію на установках без persistent gh auth. Fallback на сирий токен з env-файлу ЗАВЖДИ падає, бо змінна ніколи не підставляється.

### 1.2. ПРІОРИТЕТНА ФОРМУЛА — прибрати знаменник

Зараз: `base_priority = (impact * 2) / effort`

effort у знаменнику означає: trivial задача (effort=0.1, impact=0.2) = priority 4.0, тоді як критична (impact=1.0, effort=0.8) = лише 2.5. Агент постійно обирає дрібниці.

Запропоновані варіанти:

**Варіант A** — пом'якшити:
```
base_priority = impact * 2 * (1.0 - effort * 0.4)
```

**Варіант B** — effort як штраф, не дільник:
```
final_priority = impact * 1.5 + age_bonus + community_bonus - effort_penalty
```
де effort_penalty ∈ [0, 0.5]. Це робить impact головним драйвером.

### 1.3. ЗОВНІШНІЙ REVIEW GATE — детермінований, замість self-audit

Додати `scripts/evolution_review_gate.py` (no_agent), який ПЕРЕД merge перевіряє кожен PR:

- **Dead-code check (AST-аналіз):** чи викликається новий символ хоча б з одного non-test модуля в runtime-доступному шлязі.
- **Diff-size anomaly:** PR > 500 рядків коду (без тестів) → ручне підтвердження. PR > 1000 → автоматичний reject з лейблом `needs-split`.
- **Coverage delta:** PR додає код, але не додає тестів → reject.
- **Dependency audit:** PR додає нову залежність → flag (supply-chain ризик).
- **Breaking-change detection:** PR модифікує public API → ручне рев'ю.

Скрипт запускається в integration ДО виклику LLM. PR не пройшов → автоматично закривається з конкретним reason.

### 1.4. CANARY ROLLOUT — затримка перед поширенням

Проблема: `hermes update --yes` (щоденний cron) підтягує main на КОЖНУ установку. Поганий merge розповсюджується за 24 години.

Рішення:

- **Мінімум:** понизити auto-update до щотижневого (раз на неділю). PR-и мержуються щодня, але поширення — раз на тиждень = 7 днів "карантину".
- **Повне:** integration створює tag `canary-YYYY-MM-DD`. Лише через 48 годин чистого watchdog tag підвищується до `stable-YYYY-MM-DD`. Щойно тоді `hermes update` на інших установках підтягує.

---

## РІВЕНЬ 2 — КОРОТКОСТРОКОВО (2-4 тижні)

### 2.1. МЕТА-ЕВОЛЮЦІЯ — адаптивні інструкції (lessons.json)

Проблема: скіли не можуть адаптуватись в межах циклу. Уроки вшиті в текст SKILL.md, оновлення вимагає PR + CI.

Рішення: `~/.hermes/profiles/user1/evolution/lessons.json` — структурований журнал:

```json
{
  "lessons": [
    {
      "date": "2026-06-13",
      "source": "integration-review",
      "pattern": "dead-code: PR added module with no call sites",
      "rule": "ALWAYS run evolution_review_gate.py dead-code check before merge",
      "severity": "high",
      "occurrences": 3
    }
  ]
}
```

Integration СКІЛ читає цей файл на початку кожного циклу і застосовує правила. Файл еволюціонує швидше за SKILL.md і є per-installation. Якщо правило стабільно працює 10 циклів — "підвищується" до пропозиції оновити SKILL.md через нормальний PR-процес.

### 2.2. ЗВОРОТНИЙ ЗВ'ЯЗОК FUNNEL → RESEARCH

Проблема: funnel metrics write-only. Ніщо в конвеєрі їх не читає.

Рішення:
- evolution-research читає останні 7 записів `metrics.jsonl`.
- Якщо reject_rate > 70% — директива: "Бути вдвічі вибірковішим, лише високодоказові ідеї".
- Якщо merged=0 три цикли поспіль — flag у watchdog-звіті.

Реалізація: `python scripts/evolution_funnel.py --summary --last=7` виводить one-liner з якістю сигналу.

### 2.3. КОРИСНИЦЬКИЙ ЗВОРОТНИЙ ЗВ'ЯЗОК — "task resolved?" сигнал

Проблема: introspection не знає, чи користувач був задоволений.

Рішення: після сесії з 5+ інструментами або 10+ ходів, агент запитує через MEMORY: "Чи розв'язав я твою задачу? (так/частково/ні)". Відповідь зберігається в session metadata.

introspection_extract.py агрегує ці сигнали і додає `task_completion_rate` в digest. Non-blocking: немає відповіді і немає retry → вважати успішною.

### 2.4. СПІЛЬНИЙ ДЕДУП-КАШ ДЛЯ ВСІХ УСТАНОВОК

Проблема: dedup-cache.json локальний. Сотні установок досліджують ті ж тренди.

Варіанти:
- **A:** `.evolution/dedup-cache.json` у репозиторії (committed). Кожна установка перевіряє через `git pull`, записує через push (з retry при конфлікті).
- **B:** GitHub labels як колективний дедуп: `evolution-filed-<hash>`.
- **C:** При gh issue list >50 results — семантичний dedup через делегований subagent (не в головному контексті).

### 2.5. RATE-LIMIT-AWARE SCHEDULING

Додати `scripts/evolution_rate_limit.py` — pre-check перед кожною стадією:

```bash
gh api rate_limit --jq '.resources.core.remaining'
```

Якщо <500 залишилось — стадія відкладається на годину. Запобігає ситуаціям, де integration не може змержити бо research+issues+analysis вичерпали ліміт.

Cron YAML: `script: evolution_rate_limit.py` (exit 0 = proceed, exit 1 = defer).

---

## РІВЕНЬ 3 — АРХІТЕКТУРНІ ЗМІНИ (1-3 місяці)

### 3.1. БАГАТОШАРОВЕ РЕВ'Ю замість одного LLM-self-review

Проблема: implementation і integration працюють на одній моделі. Self-audit (крок 2b) просить модель оцінити себе 1-10 — гарантовано сходиться до 10.

Рішення — 4 послідовні шари, жоден не використовує ту ж модель, що писала код:

- **Шар 1 — Детермінований** (`evolution_review_gate.py`): dead-code, coverage, diff-size, dependencies, breaking-changes. БЕЗ LLM.
- **Шар 2 — Крос-модельний рев'ю:** PR диф відправляється в ОКРЕМУ subagent з ДРУГОЮ моделлю (delegate_task з іншим provider). Інша модель = інші сліпі плями.
- **Шар 3 — Semantic test:** для кожного PR генерується тест-сценарій "що цей код робить?" і перевіряється, чи результат відповідає опису issue.
- **Шар 4 — (опційно) Human review** для PR > 200 рядків або critical paths (вже частково є через CODEOWNERS).

Fail на будь-якому → PR не мержиться.

### 3.2. DISTRIBUTED PRIVATE MODE — fault tolerance

Проблема: одна PRIVATE installation = єдина точка відмови.

Рішення:
- "Owner pool": 2+ серверів з GITHUB_PRIVATE_TOKEN. Distributed lock через GitHub: перший, хто пише comment "claiming cycle YYYY-MM-DD" на спеціальному issue, виконує.
- Якщо заявлений сервер не завершив за 2 години — watchdog знімає lock.
- PUBLIC стадії вже розподілені — лише PRIVATE потребує координації.

### 3.3. UPSTREAM SYNC — ПРІОРИТЕТНА ЧЕРГА замість FIFO

Проблема: 25 найстаріших комітів/запуск. Upstream має ~100 комітів/день. Форк НІКОЛИ не наздожене.

Рішення: `scripts/evolution_classify_upstream.py` (no_agent) класифікує коміти:

- CRITICAL: security, auth, data-loss fixes → processed first (без обмеження)
- HIGH: bug fixes affecting core
- MEDIUM: features, perf
- LOW: docs, tests, cosmetic

Ліміт 25/запуск: спершу всі CRITICAL, потім HIGH, потім решта. Security fixes не застрягають за 200 комітами з doc-оновленнями.

### 3.4. КОРИСНИЦЬКИЙ ВПЛИВ НА ДОСЛІДЖЕННЯ

Проблема: research читає фіксовані джерела (autogen, AutoGPT). introspection аналізує сесії заднім числом.

Рішення:
- introspection ranked feed: топ-5 problems за частотою → research отримує як "priorities to find solutions for".
- Configurable sources: `config.yaml` дозволяє додати власні джерела дослідження.
- Trend detection: динамічні web_search запити на основі introspection findings замість читання фіксованого списку.

---

## РІВЕНЬ 4 — ФУНДАМЕНТАЛЬНІ НАПРЯМКИ (дослідницькі)

### 4.1. САМОМОДИФІКАЦІЯ З ГАРАНТОВАНИМИ МЕЖАМИ

CI перевіряє "чи не зламалося", а не "чи покращилося". Замість тестів — formal verification constraints:

- Інваріанти, що МАЮТЬ виконуватись завжди: "prompt cache розмір не зростає", "кількість core tools не перевищує N".
- PR, що порушує інваріант — відхиляється автоматично.

### 4.2. CURRICULUM LEARNING ДЛЯ САМОЕВОЛЮЦІЇ

Замість випадкового порядку — навчальна траєкторія:

- Фаза 1 (місяць 1): лише bug fixes (висока ймовірність успіху). Success rate target: 80%.
- Фаза 2 (місяць 2): дрібні features (medium ризик).
- Фаза 3 (місяць 3+): архітектурні зміни (високий ризик).

Якщо success rate падає — повернутись на попередню фазу.

### 4.3. META-EVOLUTION METRICS

Відповісти на питання: "чи еволюціонує агент КРАЩЕ з часом?"

Метрики (почати збирати негайно — безкоштовно):
- **Cycle success rate:** % циклів, що produced merged code (має зростати).
- **Rework rate:** % PR, що потребують rework (має знижуватись).
- **User task completion rate:** зростає з часом?
- **Reject reason diversity:** різноманітність причин відхилення (має зростати).
- **Time-to-merge:** час від issue до merge (має знижуватись для trivial задач).

Якщо метрики НЕ покращуються за 30 днів — самоеволюція стагнує.

### 4.4. ADVERSARIAL SELF-TESTING

Замість тестування "чи працює" — тестування "чи можна зламати":

Для кожного змержженого PR, no_agent скрипт шукає edge case: fuzzing inputs, boundary conditions, error paths. Знайдено → issue з категорією `regression-risk`. Доповнює coverage-delta: код може мати 100% coverage, але покривати лише happy path.

---

## ПРІОРИТЕЗАЦІЯ

### Негайно (цієї неділі)
1. Фікс access gate баг (1.1)
2. Пріоритетна формула (1.2)
3. External review gate — детермінований (1.3)
4. Canary rollout — мінімум понизити auto-update до щотижневого (1.4)

### Цього місяця
5. Meta-evolution lessons.json (2.1)
6. Funnel feedback loop (2.2)
7. User feedback signal (2.3)
8. Спільний дедуп-каш (2.4)
9. Rate-limit-aware scheduling (2.5)

### Наступний квартал
10. Multi-layer review (3.1)
11. Distributed private mode (3.2)
12. Priority upstream queue (3.3)
13. Користувацький вплив на дослідження (3.4)

### Дослідницькі
14. Formal verification constraints (4.1)
15. Curriculum learning (4.2)
16. Meta-evolution metrics (4.3) — почати збирати негайно
17. Adversarial self-testing (4.4)

---

## КЛЮЧОВИЙ ПРИНЦИП

Зараз система оптимізована для THROUGHPUT (більше PR, швидше merge). Для автономної самоеволюції треба оптимізувати для CALIBRATION — здатності агента знати, що він не знає, і НЕ робити те, у чому він не впевнений:

- Відхиляти більше, ніж впроваджувати (reject rate 60-70% — здоровий).
- Зупинятись при невпевненості, а не гадати.
- Враховувати границі власної компетенції (якщо LLM пише код, то LLM не повинен бути єдиним рев'юером цього коду).

Найкраща самоеволюція — та, що еволюціонує повільно, але кожна зміна реально корисна. Швидка самоеволюція з 30% сміттєвих PR — це ентропія, а не еволюція.
