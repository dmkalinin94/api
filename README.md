# Ktolk API daemon

Python API-демон для:
- `GET /resolve` (AD -> KTalk mention resolver)
- `POST /push` (webhook от Zabbix и отправка личных сообщений в KTalk)

## Endpoint'ы
- `GET /resolve`
- `POST /push`

## Авторизация `/push`
Используется заголовок `X-Push-Token` со значением из `cnf.py` (`api_push_secret_token`).

## Пример payload `/push`
```json
{
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
}
```

## Пример curl `/push`
```bash
curl -X POST "http://127.0.0.1:8000/push" \
  -H "Content-Type: application/json" \
  -H "X-Push-Token: change_me" \
  -d '{"event_id":"123456789","event_value":"1","event_time":"2026.01.22 07:49:32","trigger_name":"High CPU usage on host","host_name":"srv-app-01","severity":"Disaster","users":["ivanov"]}'
```

## `event_value`
- `"1"` — start: отправка стартового сообщения с цветом по severity.
- `"0"` — resolve/cancel: отправка зеленого сообщения `🟢 АВАРИЯ ЗАВЕРШЕНА`.

Resolve отправляется reply к start, если сохранен `ktalk_start_event_id`, иначе обычным сообщением.

## Отправка в KTalk (PUT)
```
/_matrix/client/r0/rooms/{room_id}/send/m.room.message/{txn_id}
```

Body:
```json
{
  "msgtype": "m.text",
  "body": "<message_text>",
  "m.mentions": {}
}
```
