# Архитектура MVP

## Сервисы

```
[React Frontend] <--Socket.IO--> [Python Backend] <--HTTP REST--> [ML Service]
                                        |
                                   [PostgreSQL]
```

### Frontend (React)
- Интерфейс в стиле ChatGPT: левая панель со списком сессий, правая -- активная сессия
- Кнопка "Новая сессия" открывает пустой чат-заглушку на фронте без обращения к бэкенду; сессия создаётся в БД только при отправке первого сообщения
- Первое сообщение в сессии -- бизнес-описание, генерирует диаграмму
- Следующие сообщения -- инструкции по редактированию диаграммы
- После первой генерации в левой панели отображается короткое название сессии (приходит от бэкенда)
- Под каждым ответом отображается актуальная версия диаграммы с кнопками экспорта (PNG, XML)
- Подключается к бэкенду через Socket.IO при открытии страницы

### Backend (Python, FastAPI + python-socketio)
- Принимает соединения от фронта через Socket.IO
- Хранит в БД сессии пользователей: `user_id -> [session]`, каждая сессия содержит `session_id`, `name` и историю сообщений; каждое сообщение ассистента содержит полный `bpmn_xml` на тот момент
- При получении первого сообщения сессии вызывает ML `/generate`, при последующих -- ML `/edit`
- Возвращает результат в тот же сокет

### ML Service (Python, FastAPI)
- `/generate` -- принимает бизнес-описание, возвращает BPMN 2.0 XML и короткое название сессии
- `/edit` -- принимает промпт пользователя и текущий BPMN XML, возвращает обновлённый XML
- Оба метода вызывают LLM и валидируют XML перед возвратом

---

## Контракт: Frontend -- Backend (Socket.IO)

Все сообщения передаются через единственный ивент `new_action_event`. Тип задаётся полем `action`.

### Клиент отправляет: `new_action_event`

**action: init** -- при подключении. `user_id` из localStorage или `null`.
```json
{
  "action": "init",
  "user_id": "uuid-or-null"
}
```

**action: open_session** -- открыть существующую сессию из левой панели.
```json
{
  "action": "open_session",
  "session_id": "uuid"
}
```

**action: message** -- отправить сообщение. Если сессия новая, `session_id` равен `null` -- бэкенд создаёт сессию в БД и вызывает ML `/generate`. Для существующей сессии передаётся `session_id` и вызывается ML `/edit`.
```json
{
  "action": "message",
  "session_id": "uuid-or-null",
  "text": "Текст пользователя"
}
```

### Сервер отвечает: `new_action_event`

**action: init_data** -- ответ на `init`. Содержит `user_id` и список всех сессий пользователя для левой панели.
```json
{
  "action": "init_data",
  "user_id": "uuid",
  "sessions": [
    { "session_id": "uuid", "name": "Процесс найма сотрудника" },
    { "session_id": "uuid", "name": "Обработка заявки" }
  ]
}
```

**action: session_data** -- ответ на `open_session`. Полная история и текущая диаграмма.
```json
{
  "action": "session_data",
  "session_id": "uuid",
  "name": "Процесс найма сотрудника",
  "bpmn_xml": "<definitions xmlns=...>...</definitions>",
  "history": [
    { "role": "user", "text": "Опиши процесс..." },
    { "role": "assistant", "bpmn_xml": "<definitions...>" },
    { "role": "user", "text": "Добавь развилку после второго шага" },
    { "role": "assistant", "bpmn_xml": "<definitions...>" }
  ]
}
```

**action: result** -- ответ на `message`. При первой генерации (новая сессия) дополнительно содержит `session_id` и `session_name` -- фронт добавляет сессию в левую панель. При редактировании эти поля отсутствуют.
```json
{
  "action": "result",
  "bpmn_xml": "<definitions xmlns=...>...</definitions>",
  "session_id": "uuid",
  "session_name": "Процесс найма сотрудника"
}
```

**action: error**
```json
{
  "action": "error",
  "message": "Описание ошибки"
}
```

**Состояние** хранится в БД по `user_id`. Клиент сохраняет `user_id` в localStorage.

---

## Контракт: Backend -- ML Service (HTTP REST)

### POST /generate
**Request:**
```json
{
  "description": "Текстовое описание бизнес-процесса"
}
```
**Response 200:**
```json
{
  "bpmn_xml": "<definitions xmlns=...>...</definitions>",
  "session_name": "Короткое название (3-5 слов)"
}
```

### POST /edit
**Request:**
```json
{
  "prompt": "Добавь развилку после второго шага",
  "bpmn_xml": "<definitions xmlns=...>...</definitions>"
}
```
**Response 200:**
```json
{
  "bpmn_xml": "<definitions xmlns=...>...</definitions>"
}
```

**Response 500 (оба метода):**
```json
{
  "detail": "Описание ошибки"
}
```

---

## Стек

| Сервис | Технологии |
|--------|-----------|
| Frontend | React, socket.io-client, bpmn-js (отрисовка) |
| Backend | Python, FastAPI, python-socketio, httpx, PostgreSQL |
| ML Service | Python, FastAPI, openai |
| Инфраструктура | Docker, Docker Compose |

---

## Docker Compose

Четыре контейнера: `frontend`, `backend`, `ml`, `db`. Фронт собирается в статику и раздаётся nginx. Бэкенд и ML общаются по внутренней сети Docker. БД доступна только бэкенду.

```
frontend:  80
backend:   8000
ml:        8001  (доступен только бэкенду, не публичный)
db:        5432  (доступна только бэкенду, не публичная)
```
