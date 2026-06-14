# Повний аудит hermes-agent-evolution

**Дата аудиту:** 2026-06-13  
**Директорія:** `/Users/admin/_Projects/hermes-agent-evolution`  
**Версія проєкту:** `0.16.0` (`hermes_cli/__init__.py:17`)  
**Upstream sync:** `v2026.6.5` (`.evolution/upstream-sync-state.json`)

---

## 📊 Загальні метрики

| Показник | Значення |
|---|---|
| Python-файлів | 2239 |
| Рядків Python-коду | ~515 660 |
| Рядків Python-тестів | ~544 163 |
| Співвідношення тест/код | 1.06× |
| TypeScript/TSX-файлів | 833 |
| Рядків TS/TSX-коду | ~126 000 |
| Загальна кількість тестів | 30 619+ |
| Залежностей у venv | 136 |
| Розмір `uv.lock` | 618 KB |
| Активність за 30 днів | 3046 комітів |

---

## ✅ Що зроблено добре

1. **Ізоляція тестів** — `scripts/run_tests_parallel.py` запускає кожен тестовий файл у окремому процесі, що закриває міжфайлове забруднення стану.
2. **Hermetic fixtures** — `tests/conftest.py` скидає credential-змінні середовища, ізолює `HERMES_HOME`, фіксує `TZ=UTC` і `PYTHONHASHSEED=0`.
3. **Windows-footguns guard** — `scripts/check-windows-footguns.py` пройшов без знахідок (`624 file(s) scanned`).
4. **Guard оновлення** — `_validate_critical_files_syntax()` у `hermes_cli/main.py` перевіряє синтаксис критичних файлів після `git pull`.
5. **CODEOWNERS для evolution** — критичні шляхи self-update, CI, cron та evolution-skills вимагають рев’ю власника (`.github/CODEOWNERS`).
6. **UV lockfile синхронізований** — `uv lock --check` пройшов успішно.
7. **Політика pinning** — `pyproject.toml` використовує exact-pinned залежності в ядрі з чітким обґрунтуванням безпеки.
8. **Багатий CI/CD** — 17 workflow у `.github/workflows/`: тести, lint, supply-chain audit, OSV scanner, Nix, Docker.
9. **Безпечний YAML frontmatter** — `agent/skill_utils.py:76` використовує `CSafeLoader`/`SafeLoader`.
10. **Актуальна upstream sync-інформація** — `.evolution/upstream-sync-state.json` відображає synced_through_tag = `v2026.6.5`.

---

## 🔴 Критичні проблеми (треба виправити негайно)

### 1. Ліцензійна невідповідність
- `README.md:7` — значок `License: Apache 2.0`.
- `README.md:148` — текст `Apache License 2.0`.
- `EVOLUTION_README.md:261` — `Apache 2.0`.
- `LICENSE`, `pyproject.toml:22`, `package.json:29`, `README.ur-pk.md`, `README.zh-CN.md` — **MIT**.

**Виправлення:** привести `README.md` та `EVOLUTION_README.md` до MIT або змінити `LICENSE` і всі metadata на Apache 2.0. Рекомендовано MIT, оскільки більшість файлів уже вказують MIT.

### 2. `package.json` вказує на оригінальний репозиторій
- `package.json:3` — `"version": "1.0.0"` (не відповідає `pyproject.toml:10` = `0.16.0`).
- `package.json:27` — `repository.url` = `https://github.com/NousResearch/Hermes-Agent.git`.

**Виправлення:**
```json
"version": "0.16.0",
"repository": {
  "type": "git",
  "url": "git+https://github.com/Lexus2016/hermes-agent-evolution.git"
}
```

### 3. Тестова suite не проходить на macOS
Запущено 2 з 6 slices:

- **Slice 1/6**: 139 passed, **6 failed** (`tests/hermes_cli/test_gateway_service.py`) — через відсутність user systemd/D-Bus на macOS.
- **Slice 2/6**: 252 passed, **13 failed**:
  - `tests/hermes_cli/test_gateway_service.py` (6) — systemd;
  - `tests/hermes_cli/test_gateway_wsl.py` (2) — WSL-only;
  - `tests/agent/test_bedrock_integration.py` (1) — AWS credentials;
  - `tests/acp/test_permissions.py::test_scheduler_failure_closes_permission_coroutine` — coroutine frame cleanup;
  - `tests/tools/test_file_state_registry.py` (2) — `/var/folders/...` визнається sensitive system path;
  - `tests/test_tui_gateway_server.py::test_browser_manage_connect_default_local_reports_launch_hint`.

**Виправлення:** додати `@pytest.mark.skipif(sys.platform == "darwin", ...)` або подібні guards для платформо-специфічних тестів.

### 4. Ложне спрацьовування на macOS temp-шляхах
`tests/tools/test_file_state_registry.py:283` падає з помилкою:
```python
{'error': 'Refusing to write to sensitive system path: /var/folders/...'}
```
Логіка в `tools/file_operations.py`/`tools/write_file` вважає `/var/folders` системним шляхом, хоча це стандартний macOS temp.

**Виправлення:** розширити білий список temp-директорій для macOS (`/var/folders/`).

---

## 🟠 Серйозні недоліки

### 5. 8913 діагностик type-checker `ty`
Команда `ty check --output-format concise .` завершилася з:
```
Found 8913 diagnostics
WARN A fatal error occurred while checking some files.
```
Приклади:
- `utils.py:195:28` — `Expected Path, found str`.
- `utils.py:261:28` — `Expected Path, found str`.
- `tui_gateway/server.py:9308:50` — type mismatch.

**Вплив:** регресії типів не ловляться CI, бо `lint.yml` використовує `ty` лише в advisory-режимі (`--exit-zero`).

**Покращення:** виправляти критичні типи поступово, починаючи з core (`run_agent.py`, `model_tools.py`, `hermes_state.py`, `utils.py`).

### 6. God-файли без тестів
Великі модулі без відповідних тестів:

| Файл | Рядків |
|---|---|
| `gateway/run.py` | 16 015 |
| `cli.py` | 13 701 |
| `hermes_cli/main.py` | 11 730 |
| `tui_gateway/server.py` | 9 468 |
| `hermes_cli/auth.py` | 7 864 |
| `plugins/platforms/discord/adapter.py` | 6 633 |
| `gateway/platforms/telegram.py` | 6 179 |
| `run_agent.py` | 5 361 |
| `gateway/platforms/yuanbao.py` | 5 057 |
| `gateway/platforms/base.py` | 4 841 |
| `agent/conversation_loop.py` | 4 221 |

**Покращення:** рефакторити на mixins/модулі та додати unit-тести.

### 7. 6198 викликів `print()` у production-коді
Розподілені по `cli.py`, `batch_runner.py`, `hermes_cli/main.py`, `hermes_cli/web_server.py` тощо.

**Покращення:** замінити на structured logging (`hermes_logging.py`).

### 8. `yaml.load()` без safe loader
- `hermes_cli/xai_retirement.py:207`:
  ```python
  yaml = YAML(typ="rt")
  doc = yaml.load(fh)
  ```
  `ruamel.yaml.YAML(typ="rt")` — round-trip loader, безпечний для довірених файлів, але краще явно використовувати `SafeLoader`.

**Покращення:** додати коментар або перейти на `SafeLoader`.

### 9. `subprocess(..., shell=True)` у production
- `cli.py:7545` — quick commands (user-defined).
- `tools/transcription_tools.py:1236` — local STT command template з user env var `LOCAL_STT_COMMAND_ENV`.
- `hermes_cli/tools_config.py:813` — встановлення `cua-driver` через `curl | bash`.
- `hermes_cli/mcp_catalog.py:367` — bootstrap commands для MCP catalog.

**Ризик:** command injection у місцях, де вхідні дані можуть містити пробіли/метасимволи.

**Покращення:** де можливо, використовувати список аргументів замість shell; для обов’язкових shell-викликів додати валідацію вхідних даних.

### 10. `HERMES_*` env-змінні для non-secret конфігурації
У `.env.example` є:
- `HERMES_QWEN_BASE_URL` (рядок 116)
- `HERMES_DOCKER_BINARY` (рядок 179)
- `HERMES_HUMAN_DELAY_MODE` / `HERMES_HUMAN_DELAY_MIN_MS` / `HERMES_HUMAN_DELAY_MAX_MS` (рядки 375–377)

`AGENTS.md` прямо забороняє нові `HERMES_*` env-змінні для non-secret config; усе поведінкове налаштування має йти в `config.yaml`.

**Виправлення:** перенести ці параметри в `config.yaml`/`hermes_cli/config.py`.

---

## 🟡 Помилки та недоліки середньої важливості

### 11. Незареєстровані pytest marks
- `pytest.mark.xdist_group` у `tests/hermes_cli/test_dashboard_auth_*.py`.
- `pytest.mark.ssh` у `tests/tools/test_file_sync_perf.py`.

Викликає `PytestUnknownMarkWarning` і може ламати запуск з `--strict-markers`.

**Виправлення:** додати marks у `[tool.pytest.ini_options] markers` у `pyproject.toml`:
```toml
markers = [
    "integration: ...",
    "real_concurrent_gate: ...",
    "xdist_group: ...",
    "ssh: ...",
]
```

### 12. Неправильна інструкція в `EVOLUTION_README.md`
- `EVOLUTION_README.md:253`:
  > Запустіть тести: `pytest tests/`

Канонічний запуск — `python scripts/run_tests_parallel.py` або `scripts/run_tests.sh`.

**Виправлення:** оновити інструкцію.

### 13. Відсутні `__init__.py` в тестових пакетах
- `tests/acp_adapter/`
- `tests/hermes_state/`
- `tests/openviking_plugin/`
- `tests/plugins/dashboard_auth/`
- `tests/plugins/model_providers/`
- `tests/scripts/`
- `tests/skills/`
- `tests/stress/`
- `tests/tool_cache/`

Це не критично для pytest, але може викликати проблеми з імпортами.

### 14. `README.md` не відображає статус форку
`README.md` не згадує, що це форк, і спрощує ризики автономного self-merging.

**Покращення:** додати посилання на `SECURITY_EVOLUTION.md` та `EVOLUTION_README.md` в шапці `README.md`.

### 15. Велика кількість core tools у схемі
`toolsets.py:_HERMES_CORE_TOOLS` містить 35+ інструментів. Хоча багато gated через `check_fn`, сама схема велика.

**Покращення:** перенести менш універсальні інструменти (kanban, computer-use) у service-gated extras або MCP.

### 16. Підозрілий hardcoded запис у `scripts/release.py`
- `scripts/release.py:1303`:
  ```python
  "23yntong@stu.edu.cn": "iuyup",  # PR #6155 salvage (shell=True hardening)
  ```

**Перевірити**, чи це справжній обліковий запис/ключ. Коментар виглядає підозрілим.

### 17. `audioop` deprecation warning
`discord-py` імпортує `audioop`, який deprecated у Python 3.12 і буде видалений у 3.13.

**Покращення:** оновити `discord-py` або додати fallback.

### 18. Аномально висока активність комітів
3046 комітів за 30 днів. Ймовірно, це squash/rebase-імпорт upstream або автоматичні коміти evolution.

**Ризик:** важко рев’ювити історію та бісектити регресії.

**Покращення:** впровадити політику squash merge та осмислені commit messages.

---

## 🔧 Рекомендований план виправлень

### Терміново (цього тижня)
1. Виправити ліцензію в `README.md` та `EVOLUTION_README.md` на MIT.
2. Оновити `package.json`: `version` → `0.16.0`, `repository.url` → форк.
3. Пофіксити macOS-специфічні тести (`test_file_state_registry.py`, `test_gateway_service.py`, `test_gateway_wsl.py`).
4. Зареєструвати `xdist_group` та `ssh` marks у `pyproject.toml`.

### Короткостроково (2–4 тижні)
5. Перенести `HERMES_QWEN_BASE_URL`, `HERMES_DOCKER_BINARY`, `HERMES_HUMAN_DELAY_*` з `.env.example` у `config.yaml`.
6. Додати `__init__.py` у тестові пакети, де цього бракує.
7. Перевірити `scripts/release.py:1303` та видалити/пояснити hardcoded значення.
8. Додати platform-guards для systemd/WSL-тестів.
9. Виправити `yaml.load` у `hermes_cli/xai_retirement.py`.

### Середньостроково (1–3 місяці)
10. Почати рефакторинг god-файлів (`gateway/run.py`, `cli.py`, `hermes_cli/main.py`) на mixins.
11. Поступово знижувати кількість `ty` diagnostics (ціль: <1000).
12. Замінити частину `print()` на logging.
13. Зменшити кількість core tools у схемі, переносячи нішеві інструменти в extras/MCP.

---

## 🛡️ Підсумок

Проєкт **hermes-agent-evolution** — це масштабний, активно розвиваний форк Hermes Agent із сильними інженерними практиками (isolated tests, supply-chain pinning, Windows guards, evolution safety gate). Основні ризики зараз — це **документаційні невідповідності**, **платформо-специфічні падіння тестів на macOS** та **велика технічна заборгованість у типах і розмірі модулів**. Критичні проблеми не блокують production на Linux, але ускладнюють локальну розробку та довгострокову підтримку.
