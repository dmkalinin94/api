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

<<<<<<< codex/create-python-api-daemon-for-ad-to-ktalk-resolution-idwlnp
> В проекте все рабочие переменные вынесены в `cnf.py` (в `CONFIG`), а модули читают значения только оттуда.
=======
Также поддерживаются env-переопределения (например, `KTOLKAPI_PG_DSN`, `KTOLKAPI_LOG_FILE`).
>>>>>>> test

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

Параметр `ad_login` обязателен и может передаваться несколько раз.

## Логика обработки

1. Нормализация списка `ad_login`.
2. Поиск совпадений в PostgreSQL.
3. Для отсутствующих логинов — запрос в AD.
4. Для активных AD-пользователей — запрос в KTalk и strict match.
5. Upsert результатов в PostgreSQL.
6. Возврат итогового JSON.

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
