# SESSION CONTEXT — Service Portal

## Что это за система

Service Portal — Flask-приложение для централизованного управления Windows-сервисами на удалённых серверах через WinRM.

Основные функции:
- хранение серверов и защищённых учётных данных для WinRM;
- конфигурирование, какие сервисы на каких серверах управляются;
- группировка сервисов для массовых операций;
- live-статусы и действия start/stop/restart;
- аудит действий;
- сбор снапшотов конфигов сервисов с диффом (ручной, плановый, pre-action).

Технологический стек:
- backend: Flask + SQLAlchemy;
- БД: PostgreSQL;
- frontend: Jinja templates + Bootstrap + vanilla JS;
- удалённое управление: pywinrm + PowerShell;
- фоновые задачи: APScheduler.

---

## Структура кода (файлы и роли)

- `app.py`
  - точка входа, инициализация Flask/DB;
  - page routes (`/`, `/servers`, `/services`, `/groups`, `/configs`, `/logs`);
  - основной REST API: servers, service-configs, groups, group actions, snapshots, scheduler, logs;
  - шифрование/дешифрование паролей через Fernet;
  - запись `AuditLog` для действий над сервисами.

- `models.py`
  - ORM-модели:
    - `Server`;
    - `ServiceConfig` (уникальность `server_id + service_name`, поля `config_dir`, `config_dir_detected_at`, `config_dir_source`);
    - `ServiceGroup`, `ServiceGroupItem`;
    - `GroupConfig` (1:1 с `ServiceGroup`, `base_config` JSON-as-text);
    - `ServiceConfigOverride` (1:1 с `ServiceConfig`, `override_config` JSON-as-text);
    - `ConfigRevision` (версионирование `group/instance/effective`);
    - `ServiceConfigDir` (несколько директорий конфига на сервис);
    - `ConfigSnapshot`, `ConfigSnapshotFile`;
    - `AuditLog`.

- `winrm_manager.py`
  - обёртка WinRM-сессии;
  - список сервисов, получение статусов и деталей сервиса;
  - start/stop/restart;
  - обнаружение `Config`-директории;
  - чтение списка файлов и содержимого текстовых конфигов.

- `scheduler.py`
  - фоновые опросы конфигов (`config_poll`);
  - `take_snapshot` с hash-based дедупликацией;
  - хранение последнего результата запуска для API статуса.

- `config.py`, `.env.example`
  - конфигурация приложения, БД, ключей и таймаутов.

- `templates/`
  - `index.html` — дерево групп и live-управление;
  - `servers.html` — CRUD серверов + тест соединения;
  - `services.html` — глобальный реестр управляемых сервисов + привязка к группам;
  - `groups.html` — CRUD групп;
  - `configs.html` — hierarchical config editor (group base / instance override / effective / group revisions) + snapshots/diff + scheduler status/run-now;
  - `logs.html` — журнал действий;
  - `base.html` — навигация и общие JS-хелперы (`apiCall`, toast).

---

## Ключевые маршруты и API

Страницы:
- `GET /`
- `GET /servers`
- `GET /services`
- `GET /groups`
- `GET /configs`
- `GET /logs`

Серверы:
- `GET/POST /api/servers`
- `GET/PUT/DELETE /api/servers/<server_id>`
- `POST /api/servers/<server_id>/test`

Сервисы по серверу / live:
- `GET/POST /api/servers/<server_id>/service-configs`
- `PUT/DELETE /api/servers/<server_id>/service-configs/<cfg_id>`
- `GET /api/servers/<server_id>/services` (`?all=1` для browse полного списка)
- `GET /api/servers/<server_id>/services/<service_name>`
- `POST /api/servers/<server_id>/services/<service_name>/action`

Глобальные service-configs:
- `GET/POST /api/service-configs`
- `PUT/DELETE /api/service-configs/<cfg_id>`
- `GET/PUT/DELETE /api/service-configs/<cfg_id>/config-override`
- `GET /api/service-configs/<cfg_id>/effective-config`

Группы:
- `GET /api/groups/tree`
- `GET/POST /api/groups`
- `PUT/DELETE /api/groups/<group_id>`
- `POST /api/groups/<group_id>/items`
- `DELETE /api/groups/<group_id>/items/<item_id>`
- `GET /api/groups/<group_id>/services`
- `POST /api/groups/<group_id>/action`
- `GET/PUT /api/groups/<group_id>/config`
- `GET/POST /api/groups/<group_id>/config/revisions`

Config dirs + snapshots:
- `GET/POST /api/service-configs/<cfg_id>/config-dirs`
- `PUT/DELETE /api/service-configs/<cfg_id>/config-dirs/<dir_id>`
- `POST /api/service-configs/<cfg_id>/detect-config-dir`
- `GET/POST /api/service-configs/<cfg_id>/snapshots`
- `GET/DELETE /api/service-configs/<cfg_id>/snapshots/<snap_id>`

Scheduler + logs:
- `GET /api/scheduler/status`
- `POST /api/scheduler/run-now`
- `GET /api/logs`

---

## Ключевые пользовательские сценарии

1. Онбординг инфраструктуры:
   - добавить сервер в `/servers`;
   - проверить WinRM-связность;
   - добавить управляемые сервисы через `/services` (вручную или browse `?all=1`).

2. Организация операционного управления:
   - создать группы в `/groups`;
   - привязать сервисы к группам;
   - работать из `/` по дереву групп (статусы + массовые действия).

3. Точечное управление сервисом:
   - открыть карточку сервиса на главной;
   - посмотреть live-детали (status/start type/account/pid/path);
   - выполнить start/stop/restart;
   - получить запись в audit log.

4. Контроль конфигураций:
   - для service-config назначить `config-dirs`;
   - использовать group base config + instance override;
   - получать effective config через deterministic deep-merge (override > base);
   - при изменениях base/override формируются записи `ConfigRevision`;
   - делать ручные snapshots;
   - запускать scheduler run-now;
   - смотреть историю и diff конфигов в `/configs`.

---

## Новая backend-модель конфигурации (group + override + effective)

- Хранение JSON:
  - в API принимаются/возвращаются JSON-объекты;
  - в БД `GroupConfig.base_config`, `ServiceConfigOverride.override_config`, `ConfigRevision.content` хранятся как текст JSON.

- Иерархия:
  - `GroupConfig.base_config` — базовый конфиг группы;
  - `ServiceConfigOverride.override_config` — override для конкретного инстанса сервиса;
  - `effective` вычисляется как deep-merge словарей с приоритетом override.

- Версионирование:
  - `ConfigRevision(scope_type, scope_id, version)` уникален;
  - при изменении group base создаётся ревизия `scope_type=group`;
  - при изменении/удалении instance override создаётся ревизия `scope_type=instance`;
  - также создаётся пересчитанная ревизия `scope_type=effective`.

- Автодетект config dir при создании сервиса:
  - в обоих create endpoint для `ServiceConfig` выполняется best-effort вызов `WinRMManager.get_service_config_dir(...)`;
  - при успехе заполняются `config_dir`, `config_dir_detected_at`, `config_dir_source='auto'`;
  - при ошибке создание сервиса не прерывается.

### Frontend updates (configs/services)

- `templates/configs.html` переведён на новые API иерархической модели:
  - `GET/PUT /api/groups/<group_id>/config` для блока **Group Base Config**;
  - `GET/PUT/DELETE /api/service-configs/<cfg_id>/config-override` для блока **Instance Override Config**;
  - `GET /api/service-configs/<cfg_id>/effective-config` для read-only блока **Effective Config** (поддержан query-параметр `group_id` для явного расчёта по выбранной группе);
  - `GET/POST /api/groups/<group_id>/config/revisions` для блока **Group Revisions**.
- В редакторах добавлена клиентская валидация JSON (через `JSON.parse`) перед отправкой `PUT`; при ошибке показывается `alert`, запрос не отправляется.
- Legacy-вызовы single endpoint `PUT /api/service-configs/<id>/config-dir` удалены из `/configs`; snapshot-инструменты сохранены как отдельная секция.
- `templates/services.html`: после успешного добавления сервиса показывается статус автоопределения `config_dir` (успех с путём или информативное предупреждение при отсутствии авто-детекта).

### Review fixes (mandatory before manual test)

- `GET /api/service-configs/<cfg_id>/effective-config`:
  - добавлен `group_id` в query-string;
  - при передаче выполняется валидация, что сервис состоит в указанной группе;
  - при отсутствии `group_id` сохранён fallback на primary group / пустую базу.
- `templates/configs.html`:
  - `loadEffectiveConfig()` теперь передаёт выбранный `group_id` из group picker.
- `scheduler.py`:
  - `poll_configs()` выбирает сервисы не только по `config_dirs.any()`, но и по заполненному legacy-полю `config_dir`;
  - `take_snapshot()` использует приоритет пути: `config_dir` → затем элементы `config_dirs` (с дедупликацией одинаковых путей).
- Ревизии конфигов:
  - в `_create_config_revision(...)` разрешён `source='ui'` без принудительного затирания в `manual`.

---

## Текущие риски/техдолг

1. Несоответствие API и фронтенда в `configs`:
   - шаблон использует `PUT /api/service-configs/<id>/config-dir` и поле `config_dir`;
   - backend реализует только plural API `config-dirs`.
   - Риск: часть UI-функций может быть нерабочей/устаревшей.

2. Устаревшие комментарии и naming drift:
   - в `scheduler.py` и части docstring ещё встречается логика «single config_dir», хотя модель уже `config_dirs`.
   - Риск: ложные ожидания при сопровождении.

3. Транзакционность групповых операций:
   - bulk actions по группам частично успешны и частично неуспешны без общей транзакционной семантики rollback.
   - Это ожидаемо для ops, но требует явной политики ретраев/идемпотентности.

4. Безопасность транспортного слоя WinRM:
   - `server_cert_validation="ignore"` и частое использование non-SSL конфигураций повышают риски MITM в ненадёжных сетях.

5. Масштабируемость live-опросов:
   - UI активно дергает API для обновления статусов; при росте количества групп/сервисов возможны задержки.

---

## Рекомендованный порядок доработок

1. Синхронизировать контракт `configs`-страницы и backend:
   - либо вернуть совместимый endpoint/поле `config_dir`,
   - либо полностью перевести фронтенд на `config-dirs`.

2. Привести docstring и комментарии к текущей модели `config_dirs` во всех модулях.

3. Добавить минимальный integration smoke для критических API-потоков:
   - server CRUD + test,
   - service action,
   - group bulk action,
   - snapshot create/list/get.

4. Усилить WinRM-безопасность и конфигурируемость:
   - режим строгой TLS-валидации как опция;
   - явная документация рекомендованных production-настроек.

5. Оптимизировать обновления live-статусов:
   - батчирование/дебаунс на UI,
   - ограничение параллелизма запросов,
   - кэширование краткоживущих результатов на backend при необходимости.

---

## Быстрый старт для следующей сессии

1. Открыть и быстро пройти:
   - `app.py` (маршруты и API-контракты);
   - `models.py` (фактическая схема данных);
   - `templates/configs.html` + API-блоки config dirs/snapshots в `app.py`.

2. Первичная проверка согласованности:
   - сверить все вызовы `apiCall` в `templates/configs.html` с существующими endpoint в `app.py`.

3. Проверить цепочку snapshot:
   - `winrm_manager.py` (`list_config_files`, `read_config_file`),
   - `scheduler.py` (`take_snapshot`, `poll_configs`),
   - API `snapshots` в `app.py`.

4. Проверить операционные сценарии:
   - single action: `/api/servers/<id>/services/<name>/action`;
   - bulk action: `/api/groups/<id>/action`;
   - аудит: `/api/logs`.

5. После этого приоритизировать правки по пунктам из раздела «Рекомендованный порядок доработок».
