# Ktolk API daemon

Python API-демон для резолва пользователей Active Directory в `ktalk_mention_id` Kontur Talk.

## Установка зависимостей

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Настройка `cnf.py`

Отредактируйте значения в `cnf.py`:

- PostgreSQL (`pg_dsn`, `table_name`)
- AD / LDAP (`ad_host`, `ad_user`, `ad_password`, `ad_base_dn`)
- Kontur Talk (`ktalk_base_url`, `ktalk_bearer_token`, `ktalk_host`, `ktalk_talk_host`)
- Runtime (`verify_ssl`, `request_timeout`, `log_file`, `log_file_size`)

> В проекте все рабочие переменные вынесены в `cnf.py` (в `CONFIG`), а модули читают значения только оттуда.

## Ручной запуск API

```bash
uvicorn api_daemon:app --host 0.0.0.0 --port 8000
```

Endpoint:

- `GET /resolve`

## Вызов `GET /resolve`

### Один логин

```bash
curl "http://127.0.0.1:8000/resolve?ad_login=ivanov"
```

### Несколько логинов

```bash
curl "http://127.0.0.1:8000/resolve?ad_login=ivanov&ad_login=petrov"
```

Нужно передать хотя бы один параметр: `ad_login` или `ktalk_mention_id`. Каждый может передаваться несколько раз.

### Один mention_id

```bash
curl "http://127.0.0.1:8000/resolve?ktalk_mention_id=@ivanov:matrix-9.ktalk.ru"
```

### Смешанный запрос

```bash
curl "http://127.0.0.1:8000/resolve?ktalk_mention_id=@ivanov:matrix-9.ktalk.ru&ad_login=petrov"
```

Поддерживаются оба query-параметра: `ad_login` и `ktalk_mention_id`.

## Логика обработки

1. Нормализация списков `ad_login` и `ktalk_mention_id`.
2. Поиск совпадений в PostgreSQL по обоим ключам.
3. Для отсутствующих `ad_login` — запрос в AD и strict match в KTalk.
4. Для отсутствующих `ktalk_mention_id` — получение KTalk-профиля и извлечение AD login.
5. Upsert только положительных соответствий в PostgreSQL.
6. Возврат итогового JSON c `users`, `not_found_ad_logins`, `not_found_ktalk_mention_ids`, `without_ktalk_mention_id`.

Weak match не используется.

## Логи

Единый лог пишется в файл `CONFIG["log_file"]` (по умолчанию `/tmp/ktolkapi.log`) с ротацией по размеру `CONFIG["log_file_size"]` (например, `10Mb`).

В лог пишутся:

- IP клиента;
- дата/время запроса;
- endpoint;
- нормализованные логины;
- количество найденных в БД;
- количество недостающих;
- запуск AD-поиска;
- запуск KTalk-поиска;
- результат strict match;
- факт upsert в БД;
- итоговый статус ответа API;
- основные ошибки.

Секреты (пароли, bearer token, полный DSN с паролем) в лог не пишутся.

## Systemd (`ktolkapi.service`)

1. Скопируйте unit-файл:

```bash
sudo cp ktolkapi.service /etc/systemd/system/ktolkapi.service
```

2. Перечитайте конфигурацию и включите сервис:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ktolkapi.service
```

3. Проверка:

```bash
sudo systemctl status ktolkapi.service
journalctl -u ktolkapi.service -f
```

## Вызов `POST /push`

Требуется заголовок `X-Push-Token` со значением `CONFIG["api_push_secret_token"]`.

```bash
curl -X POST "http://127.0.0.1:8000/push" \
  -H "Content-Type: application/json" \
  -H "X-Push-Token: change_me" \
  -d '{
    "event_id": "123456789",
    "event_value": "1",
    "event_time": "2026.01.22 07:49:32",
    "trigger_name": "High CPU usage on host",
    "host_groups": ["Linux servers", "Production"],
    "host_name": "srv-app-01",
    "operation_data": "CPU load is 95%",
    "severity": "Disaster",
    "trigger_url": "https://zabbix.example.local/tr_events.php?triggerid=12345",
    "users": ["ivanov", "@651ff94812fgja993c:matrix-9.ktalk.ru"]
  }'
```

- `event_value = "1"` — отправка start-сообщения в личный room.
- `event_value = "0"` — отправка resolve-сообщения (reply на start, если есть `ktalk_start_event_id`).

Отправка в KTalk выполняется через Matrix API методом `PUT`:

```bash
curl -X PUT "https://chat.ktalk.ru/_matrix/client/r0/rooms/!roomid:matrix-9.ktalk.ru/send/m.room.message/m1777537548708.1" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KTALK_BEARER_TOKEN>" \
  -d '{"msgtype":"m.text","body":"🔴 Disaster","m.mentions":{}}'
```
