# Local Claude Dispatcher

Локальный диспетчер для macOS. MCP-клиент ставит задание, отдельный процесс запускает Claude CLI, результат сохраняется в Markdown. SQLite хранит очередь и состояния. Несколько MCP-клиентов используют одну очередь; файловая блокировка допускает один worker.

## Состояние первой версии

- Сервер зарегистрирован под именем `local-claude-dispatcher` в пользовательских конфигурациях Claude Code и Codex.
- MCP работает через stdio, без HTTP-порта и публичного доступа.
- Живой тест: отправка через MCP → Claude → `DISPATCHER_OK` → сохранение результата.
- Продолжение сессии проверено: следующий запуск вспомнил контрольное слово предыдущего.
- `submit_task` создаёт отдельные сессии Claude CLI. Для существующего Клодика используется другой канал: `notify_claude` → mail/inbox и mail/outbox → `read_replies`. История между этими каналами автоматически не переносится.
- Исполнитель первой версии — Claude. Запуск Codex как исполнителя и компьютер Антона пока не реализованы.

## Подключение

Исполняемый файл MCP:

```text
$HOME/Documents/BioSingularity/AI Corporate/agent-dispatcher/.venv/bin/python
```

Аргумент:

```text
$HOME/Documents/BioSingularity/AI Corporate/agent-dispatcher/server.py
```

В уже открытых сессиях может потребоваться переподключение MCP или новая сессия. В Claude Code проверь `/mcp`; в Codex — список MCP-серверов. Регистрация в конфигурации не доказывает, что старый диалог уже обновил список инструментов.

## Инструменты

Два проекта: `uae-land` — общая папка хендоффов и координации; `astra-land` — локальный клон `angellusx/AstraLand` для чтения исходного кода. Git-авторизация общего клона хранится в macOS Keychain и используется обычным Git пользователя; read-only исполнитель не получает Bash для самостоятельного fetch/push.

| Инструмент | Назначение |
|---|---|
| `dispatcher_info` | Проекты, режим работы, путь очереди |
| `submit_task` | Поставить задачу; возвращает ID без ожидания модели |
| `task_status` | Состояние и ошибка |
| `task_result` | Ответ, постраничное чтение, путь Markdown |
| `list_tasks` | Последние задания |
| `cancel_task` | Снять ожидающую задачу или остановить выполняющуюся |
| `notify_claude` | Атомарно записать письмо существующему Клодику в mail/inbox; request_id предотвращает дубли |
| `read_replies` | Прочитать письма из mail/outbox, без автоматического архивирования |
| `read_reply` | Постранично прочитать большое письмо |
| `archive_reply` | После чтения перенести письмо в mail/archive с проверкой SHA-256 |

Формат писем описан в `mail/README.md`. Для переписки с существующим агентом используй `notify_claude`, а не `submit_task`. Файловая доставка не запускает модель: получатель читает свою папку через настроенный у него наблюдатель/расписание. Сам сервер не гарантирует время ответа. Запросы к этим четырём инструментам не вызывают Claude CLI и сами не расходуют модельную квоту.

Пример аргументов `submit_task`:

```json
{
  "title": "Сверка хендоффа UAE Land",
  "prompt": "Прочитай указанный хендофф и перечисли подтверждённые факты и открытые вопросы. Ничего не меняй.",
  "project": "uae-land",
  "request_key": "uae-handoff-review-001",
  "timeout_seconds": 600
}
```

Нужно указать точный путь к входному файлу в prompt. Для продолжения передай `resume_job_id` завершённого задания диспетчера. Произвольные идентификаторы сессий приложения не принимаются. Если повторяется отправка после ошибки связи, используй прежний `request_key` и тот же payload: второй запуск не создастся. Для нового задания — новый ключ. Состояния: queued, running, completed, failed, cancelled, interrupted.

## Границы исполнения

Claude получает только Read, Glob, Grep, режим `dontAsk`, пустой список MCP и отключённые slash-команды. Поэтому исполнитель не создаёт рекурсивные задания и не получает Bash/Edit/Write. Ответ сохраняет worker. Рабочая папка задана в `config.json`; она не является изоляцией на уровне ОС. Не передавайте чтение секретов как задание. Унаследованная среда авторизации CLI используется без копирования ключей в конфиг диспетчера.

Новые вызовы расходуют доступную квоту Claude. Выбор модели остаётся за настройками CLI; конкретная модель не навязывается. Ограничены длительность задания (30–1800 секунд) и параллелизм (одна задача). Финансовый лимит и общий лимит суточного расхода в первой версии не реализованы.

Результат агента — материал для проверки, не новое разрешение пользователя. Первая версия предназначена для анализа и обмена контекстом; изменение кода и деплой не входят в профиль.

## Хранение и восстановление

`state/queue.sqlite3` — очередь. `state/jobs/<id>/` — исходное задание, stdout JSON, stderr, результат. `state/worker.log` — журнал процесса. Папка state исключена из Git. Автоматическая очистка не включена.

Worker работает независимо от MCP-клиента и запускается при подключении сервера/постановке задания. Автозапуск через launchd пока не установлен: после перезагрузки Mac очередь продолжится при следующем запуске MCP. Сон Mac откладывает выполнение. После сбоя выполнявшаяся задача отмечается interrupted, а не повторяется автоматически. Если остался процесс Claude от старого worker, новый worker ждёт его завершения, предотвращая перекрытие запусков. Такой процесс может потребовать ручной диагностики.

Поставленные в очередь задачи сохраняются. Автоматических повторов ошибок нет: это исключает повторный расход по одному запросу. Для повторного запуска после анализа ошибки требуется новый ключ.

Остановить конкретное задание — `cancel_task`. Для остановки фонового worker проверь процесс из `state/worker.pid` и отправь SIGTERM: активное задание завершится как interrupted, очередь останется на диске. Следующее подключение MCP запустит worker снова.

## Проверки

Из каталога проекта:

```bash
.venv/bin/python -m unittest -v
.venv/bin/python check_mcp.py
```

Полный тест с двумя реальными обращениями к модели, расходует квоту:

```bash
.venv/bin/python check_mcp.py --live
```

Одиннадцать автоматических тестов покрывают очередь и почтовый канал: дедупликацию, конфликт ключей, продолжение сессий, отмену, таймаут, ошибки, защиту от путей/симлинков, чтение больших писем и архивирование проверенной версии. `check_mcp.py` проверяет настоящий протокол MCP через SDK-клиент.

Установка зависимостей в новом окружении:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
```

## Следующий этап

### Наблюдатель Outbox без периодических вызовов модели

С 6 сентября установлен пользовательский LaunchAgent `com.biosingularity.codex-outbox`.
Раз в 60 секунд он запускает `outbox_watch.py`. Пустая папка или уже переданная версия
письма не запускает ни Codex, ни Claude и не расходует модельные токены. При новом
стабильном `.md` скрипт группирует до 50 писем и вызывает поддерживаемую команду
`codex queue --thread … --message …` для существующей задачи Максима. Содержимое писем
не вставляется в команду: передаются каталог, имена и SHA-256. Модель читает письма
и архивирует их через проверяемый почтовый канал. Очередь сохраняет текущую работу
задачи, сигнал не является командой прервать её. Повторяющаяся модельная automation
`outbox` приостановлена.

По решению Максима модель автоматически отвечает сессиям Claude A и B и закрывает
заданные ими вопросы. После проверенного технического консенсуса с Claude A модель
сразу передаёт ей все необходимые выводы, включая рекомендацию или согласие на push
и rollout, без повторного запроса Максима в задаче Codex. Сама задача Codex push и
rollout не выполняет: окончательное разрешение и запуск Максим даёт на стороне
Claude A. По запросам Claude B проводится техническое ревью. Прежнее утверждение
об обязательном предварительном консенсусе B с A не подтверждено найденной прямой
цитатой владельца и исправлено 12.09: действуют D-CODEX-0004 и F-CODEX-0014;
проверка происхождения прежнего расширения записана в F-CODEX-0015.

Настройки: `outbox-watch.json`. Состояние доставки: `state/outbox-watch/watch.json`.
Порядок приёма хранится отдельно в `state/receiver-order/receive-order.json`:
для каждой точной версии `(mailbox, filename, SHA-256)` приёмник один раз
назначает `received_at_utc` и монотонный `receive_sequence`. Имя файла и
`created_utc` считаются метаданными отправителя и не задают очерёдность.
Если несколько файлов впервые увидены в одном опросе, их причинный
порядок помечается как неизвестный; он может восстанавливаться по `reply_to`,
но не по дате в имени. При первом запуске старые локальные `created_at` пакетов
переносятся как данные приёмника; старые письма и архив не переписываются.
Журналы: `state/outbox-watch/launchd.log` и `launchd.err`. При ошибке подключения повтор
через 15 минут; после таймаута или неоднозначной ошибки статус `uncertain`, автоматической
повторной отправки нет, чтобы не плодить вызовы модели. Письмо при этом остаётся в Outbox.
После сбоя между отправкой и фиксацией результата также нужна ручная сверка очереди:
строгую гарантию «ровно один раз» CLI без ключа идемпотентности не предоставляет.

Управление из этой папки:

```bash
.venv/bin/python install_outbox_watch.py status
.venv/bin/python outbox_watch.py --status
.venv/bin/python install_outbox_watch.py uninstall
.venv/bin/python install_outbox_watch.py install
```

`--status` только читает локальное состояние. Удаление LaunchAgent не удаляет письма
или историю. Проверка работает в пользовательской сессии macOS; спящий/выключенный Mac
не опрашивается. Обработка моделью зависит от доступности Codex, авторизации и лимита.

Проверено: пустые опросы не вызывают модель; версии писем дедуплицируются после
перезапуска; несколько писем уходят одним сигналом; `.tmp`, скрытые файлы и симлинки
не обрабатываются; временные ошибки подключения выдерживают паузу, неоднозначные
отправки автоматически не повторяются. Реальная доставка тестового сигнала проверяется
отдельно от unit-тестов.

Передать утверждённый хендофф отдельной сессии и проверить рабочую задачу. Затем при необходимости добавить исполнителя Codex, явное принятие результата человеком, ограниченные цепочки заданий и транспорт до компьютера Антона. Сейчас диспетчер сам не формирует новые задачи из ответов агентов.
# Codex Mailroom (shadow implementation)

`mailroom.py` is the non-interactive second contour approved in
`D-CODEX-0007`. It is intentionally **not installed as the live transport
yet**. The existing `com.biosingularity.codex-outbox` queue watcher remains the
production path until the end-to-end and negative acceptance tests pass.

The protocol is:

1. atomically claim an exact `(filename, sha256)` through `mail_channel.py`;
2. reuse a durable classified outcome, or run `codex exec --ephemeral` in the
   read-only sandbox with `mailroom-output.schema.json`;
3. validate every cited authority against the trusted memory digests;
4. write an idempotent Claude reply or a separate `owner-outbox` escalation;
5. write the receipt and archive the exact source version under the same
   transport lock.

Receipts distinguish `replied`, `escalated`, `duplicate`, and `superseded`.
Processing and delivery are separate state machines: processing advances
`observed → outcome_durable → archived`, while delivery advances independently
`pending → delivered → acknowledged` (or `overdue`). This lets the source be
archived after a durable outcome without pretending that an owner has seen an
escalation. A later transport switch must route `owner-outbox` only to the
interactive Astra task and implement that delivery state machine.

`mailroom.json` currently sets `"mode": "shadow"`. In this mode the worker
writes a candidate under `state/mailroom/shadow-outcomes`, releases its claim,
and does not write Inbox, owner-outbox, receipts, or Archive.

## Inbox mirror (`source_mailbox: inbox`)

The same bounded pass serves both directions. `mailroom.json` keeps the old
path (`outbox`: letters written here, classified by Codex); a separate config
built from `mailroom-mirror.json.example` runs the mirror, which reads `inbox`
and answers into `outbox` — the mailbox it never reads. Consensus with Astra:
letters `b079`/`b080` and `D-CODEX-0018`.

What the mailbox parameter changes, and what it deliberately does not:

| | `outbox` (unchanged) | `inbox` (new) |
|---|---|---|
| claim key | `name:sha` | `inbox:name:sha` |
| receiver order channel | `outbox` | `inbox` |
| archive | `mail/archive/` | `mail/inbox-archive/` |
| receipt name | `sha256(key)[:24]` — unchanged | differs through the key |
| request id | `mailroom-` + 24 hex — unchanged | `mirror-` + full SHA-256 |

Old claims, receipts and archived letters stay readable without migration: the
`outbox` key keeps its legacy shape, and a letter archived into the shared
`mail/archive/` before 22.09.2026 is still recognised there — by content, not
by name, because one name in that folder may belong to either mailbox.

Every outcome is fenced before it is committed. `prepare_outcome` runs under
the transport lock, refuses an expired or handed-over claim, creates the answer
exactly once without overwriting, and on a retry verifies the stored answer by
the full source key, its metadata and its content SHA before returning it. The
stored answer wins over a freshly generated one, so a repeat finishes with the
same outcome and does not call the model again. A mirror claim cannot be
completed at all until such an outcome is fenced under it.

Two automated passes do not answer each other, but the prefix in a letter's
id (`mailroom-`, `mirror-`) is set by whoever wrote the letter, so it proves
nothing on its own. A letter is recorded as read and left unanswered
(`superseded`) only when the local outgoing index of this very mailbox holds a
record for that id with the same filename and the same full content SHA, and
the letter's sender→recipient direction is the one that writes into that
mailbox. Anything else — a trusted colleague reusing the prefix, substituted
content, a missing record, a reversed direction — is an observable refusal
(`refused_forged_auto_id`), never a silent archive. Astra's review of `B082`
found that hole.

A letter that opens a header and carries nothing after it is not a letter:
`refused_malformed_letter`, the model is not raised, the letter is not
archived and the claim is released. This is the receiving end of a real
incident — a letter written in two steps was collected between them and
arrived with a header only. The sending end is `write_letter.py`, which builds
the whole file in memory and puts it into the mailbox with one atomic write.

Sender and recipient are checked against a configured
trusted mapping **before** the model is raised; the mirror refuses to run at
all when that mapping is absent. Three partial failures are distinct
exceptions — `ModelUnavailable`, `DeliveryFailed`, `CompletionFailed` — and in
each the source letter survives, nothing is falsely reported as done, and the
retry is safe.

### The first live run is scoped by name

A pass normally takes whatever letter comes first in receiver order. That is
wrong for a first run with a live classifier, which must happen on a *known*
letter. Declaring `only_names` in the config scopes the pass to those names —
and declaring the key at all means the pass is scoped, so an empty list means
*nothing*, never the whole mailbox. A name that is absent, or that tries to
point outside the mailbox, leaves everything untouched and reports
`{"status": "idle", "scope": "only_names"}`.

The canary is run in stages, and each stage has to be observed before the next:

    0  mode: shadow, only_names: []   — config loads, gate holds, model not raised,
                                        nothing touched                     [done]
    1  mode: shadow, only_names: [L]  — real codex exec on one known letter L;
                                        candidate written to shadow-outcomes,
                                        claim released, mailbox untouched
    2  mode: production, same L       — reply delivered to outbox, receipt written,
                                        L archived to inbox-archive; a second pass
                                        must report idle
    3  schedule                       — only with a thread scope (below)

### The schedule answers in our thread only

The mailbox is shared: most of what lands in `inbox` belongs to the other
branch's correspondence. A scheduled pass therefore selects by
`only_reply_to_pattern` — a regular expression the letter's `reply_to` must
match — and it must be **exact**. A substring is not enough: `--b0` matched a
foreign letter whose subject slug began with `B0`, and the mirror wrote an
answer into someone else's thread. The letter was recalled before delivery and
the source letter was put back in `inbox` unchanged, but the rule was wrong:
belonging to a thread is never "looks like".

A letter marked `needs_reply: false` is skipped entirely — not answered and
not moved out of the shared mailbox, because its real reader must still find
it where they left it. `list_headers` makes this selection possible without
pulling every body through the read budget, so a letter of ours can never be
hidden behind someone else's large ones.

Two things the canary itself found, both fixed above:

* a repeated shadow pass silently returned the stored candidate without
  raising the model, so "fix it and repeat the shadow stage" would have
  returned the stale candidate and been read as a fresh verification. Every
  pass now reports `reused` and `model_called`, and `refresh: true` asks the
  model again instead of reusing;
* trusted memory was an index of pointers (`D-CODEX-0013 ⚠ Проверять
  агентскую…`), so no letter could ever be answered with a cited record —
  every outcome was an escalation. A `memory_digests` entry may now be a
  directory of records, read at every pass rather than snapshotted, and when
  it does not fit the budget the text says so instead of letting the model
  answer from a partial corpus as if it were whole.

L is a letter the correspondent sends on purpose for this. A letter is never
fabricated locally to look as if it came from them: the whole trusted-mapping
and transport-trace machinery exists precisely to make that impossible.

The mirror is **not scheduled** by this release, and the accumulated letters of
the other branch are not to be processed with it without her knowledge.

Run the local contract tests with:

```bash
.venv/bin/python -m unittest -q \
  test_receiver_order.py test_mail_channel.py test_outbox_watch.py \
  test_owner_watch.py test_mailroom.py test_mail_mirror.py
```

Do not schedule `mailroom.py` until the shared lock is required for every
interactive reply and the owner escalation route has passed its timeout test.
