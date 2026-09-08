# AgentBridge

AgentBridge — небольшой мост между локальным контроллером и **одним** видимым
Windows-агентом. Репозиторий является новым проектом: он не означает, что
Heroku-приложение уже развёрнуто, а ПК уже вошёл в систему или настроен.

## Как начать тестирование

1. Разверните relay на Heroku по инструкции внизу. База данных не нужна.
2. Откройте [GitHub Actions](https://github.com/ioanns2002-star/Test/actions/workflows/verify.yml),
   выберите успешный запуск текущей версии и скачайте артефакт
   **AgentBridge-windows-x64**. Внутри — ZIP с EXE и контрольная сумма.
3. Распакуйте оба уровня ZIP. Запустите `AgentBridge.exe` напрямую либо установите
   его в меню «Пуск» через `Install.ps1`; Python на вашем ПК не требуется.
4. В окне Settings укажите адрес `wss://…`, **device**-токен и общий peer key.
   Сохраните настройки и выберите **Connect** в трее. На стороне контроллера
   используйте тот же адрес и peer key, но отдельный **controller**-токен.

Подробности установки и удаления: [`windows/README.md`](windows/README.md).
Исходный ZIP с зелёной кнопки Code — не готовое Windows-приложение.

## Архитектура

```text
локальный CLI-контроллер  ←─ wss / зашифрованные кадры ─→  Heroku web dyno
                                                               │
                                                               └─ wss ─→ Windows tray agent
```

На Heroku работает ровно **один web dyno** с relay-процессом. Он держит в памяти
по одному WebSocket для роли `controller` и `device` и пересылает непрозрачные
зашифрованные бинарные кадры. В нём нет БД, очереди, файлового хранилища,
исполнения команд или результатов. Рестарт dyno разрывает сессию; контроллер не
повторяет потенциально изменяющие действия автоматически.

Ограничение в один dyno намеренное: Heroku указывает, что состояние WebSocket
живёт в конкретном web-процессе, а при нескольких процессах ему потребовалось бы
общее состояние. См. официальные материалы: [WebSockets on
Heroku](https://devcenter.heroku.com/articles/websockets) и [Dyno
Runtimes](https://devcenter.heroku.com/articles/dyno-runtime).

## Модель доступа

- В Heroku Config Vars задаются **два разных** случайных токена длиной не менее
  32 символов: `BRIDGE_DEVICE_TOKEN` и `BRIDGE_CONTROLLER_TOKEN`. Они никогда не
  передаются в URL или коммитах.
- Отдельный сквозной (end-to-end) Fernet peer key создаётся локально командой
  `agentbridge keygen` и хранится только у контроллера и выбранного ПК. Его
  **нельзя** помещать в Heroku Config Vars, логи, релизный архив или чат.
- Relay отклоняет browser-origin подключения, вторую связь той же роли и
  незашифрованные текстовые кадры. Производство использует `wss://`; `ws://`
  разрешён только при явно включённом тестовом loopback-режиме.
- После аутентификации ролей стороны проводят новый challenge и проверяют
  сессию/строго возрастающие номера кадров. Это защищает от постороннего
  подключения к мосту, но доверенный контроллер действует с правами вошедшего
  Windows-пользователя — рабочая папка не является песочницей.

Windows-агент запускается заметно в пользовательской сессии и имеет tray-меню:
статус, пауза и выключение. При паузе, уходе peer или потере heartbeat его
незавершённые дочерние процессы останавливаются. По умолчанию нет скрытой
службы, автозапуска, тихого повышения прав, захвата буфера обмена или постоянной
записи экрана. Для действий доступны все права текущего пользователя, поэтому
подключать следует только свой доверенный контроллер.

Полный контракт кадров и RPC: [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Локальная установка и проверка

Нужен Python 3.11+.

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -v
```

Для Windows desktop-компонента из исходников устанавливаются дополнительные
зависимости:

```powershell
python -m pip install -e ".[windows]"
agentbridge-desktop
```

Нативные Windows UI, интерактивный desktop и игровые сценарии недоступны в
текущей sandbox-среде и не считаются здесь проверенными. При успешной Windows
сборке GitHub Actions публикует EXE-архив как build artifact; бинарник не
поставляется и не обещается в исходном ZIP этого репозитория.

## Настройка контроллера

Сначала **явно** создайте ключ на своей локальной машине и передайте его на
выбранный ПК безопасным способом:

```bash
agentbridge keygen
```

Команда намеренно выводит ключ только потому, что пользователь вызвал генерацию
явно. Чтобы записать новый ключ в приватный файл без вывода, используйте
`agentbridge keygen --write` (замена существующей пары требует `--force`).

Затем сохраните endpoint-конфигурацию; токен и peer key спрашиваются без echo:

```bash
agentbridge configure
```

По умолчанию это `~/.config/agentbridge/controller.json` (на Unix с режимом
`0600`). Другой локальный файл выбирается через `--config FILE`. Для
неинтерактивного запуска доступны переменные окружения, причём явные параметры
CLI имеют наивысший приоритет:

```bash
export BRIDGE_URL='wss://your-relay.herokuapp.com'
export BRIDGE_CONTROLLER_TOKEN='локальный-секрет-контроллера'
export BRIDGE_PEER_KEY='локальный-fernet-peer-key'
agentbridge ping
```

Не помещайте секреты в командную строку, shell history, URL с query-параметрами
или `.env` в Git. `--allow-insecure-localhost` предназначен исключительно для
явной тестовой конфигурации с `ws://127.0.0.1`/`localhost`; удалённый plaintext
WebSocket не допускается.

## CLI

Статус и диагностика идут в `stderr`; результаты, байты `read`, поток stdout
процесса и JSONL-машинный вывод идут в `stdout`.

```bash
agentbridge ping
agentbridge info
agentbridge ls 'C:\Users\me'
agentbridge stat 'C:\Users\me\notes.txt'
agentbridge call fs.list --params '{"path":"C:\\Users\\me"}'
agentbridge write 'C:\Users\me\note.txt' --data 'hello' --create-parents
agentbridge read 'C:\Users\me\note.txt' > note.txt
```

`call METHOD --params JSON` — общий путь к любому RPC из протокола, включая
`fs.read` и `fs.write`. Для небольшого `write` действует лимит 262 144 байта;
для двоичных файлов используйте транзакционный transfer:

```bash
agentbridge put ./local.iso 'C:\Users\me\Downloads\local.iso'
agentbridge get 'C:\Users\me\Downloads\local.iso' ./local.iso --sha256 <digest>
```

`put` передаёт строго последовательные chunk'и и завершает upload с SHA-256.
`get` сверяет смещения и размер каждого ответа, пишет во временный локальный файл
и выводит вычисленный SHA-256; `--sha256` добавляет обязательную проверку
ожидаемого digest.

Запуск передаёт либо `argv`, либо исходник PowerShell, и сразу печатает события
`exec.output` в соответствующие потоки терминала. Код завершения процесса
становится кодом CLI; Ctrl-C отправляет один `exec.cancel`, после чего сессия
закрывается.

```bash
agentbridge exec --cwd 'C:\work' --env MODE=test -- powershell -NoProfile -Command '$PSVersionTable.PSVersion'
agentbridge exec --script 'Get-Date'
agentbridge exec --script-file ./check.ps1 --stdin 'input text'
agentbridge exec --stdin-file ./input.bin -- cmd /c type con
```

Для серийных действий `rpc` сохраняет одно соединение с низкой задержкой. Он
читает JSON-объекты `{id,method,params}` построчно из stdin и пишет ответы и
события JSON-lines в stdout:

```bash
printf '%s\n' '{"id":"health","method":"ping","params":{}}' | agentbridge rpc
```

Если связь оборвалась, ни `rpc`, ни обычные команды не переподключаются и не
повторяют действие автоматически: итог уже отправленной команды может быть
неизвестен.

## Деплой на Heroku

Это инструкция для тестовой подготовки, а не заявление о существующем deploy.
Создайте приложение, задайте разные секреты через защищённый интерфейс Heroku и
держите масштабирование на одном web dyno. Relay требует `WEB_CONCURRENCY=1`.

```bash
heroku create your-agentbridge-relay
# Задайте BRIDGE_DEVICE_TOKEN и BRIDGE_CONTROLLER_TOKEN вне Git и shell history.
heroku config:set WEB_CONCURRENCY=1
git push heroku HEAD:main
heroku ps:scale web=1
```

После реального deploy используйте выданный HTTPS-домен как `wss://…` в обеих
локальных endpoint-конфигурациях. Не добавляйте БД или store только ради моста:
relay не должен хранить пользовательские данные между сессиями.
