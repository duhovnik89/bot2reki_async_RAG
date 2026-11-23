# Архитектура бота: AS IS → TO BE (Yandex Cloud Serverless)

Две схемы: как бот устроен **сейчас** (локальный процесс с long-polling) и как
он будет устроен **после переноса** на бессерверный стек Yandex Cloud.

> Обе диаграммы в Mermaid. GitHub рисует их автоматически. Можно также вставить
> код любого блока в https://mermaid.live и посмотреть/поправить.

---

## AS IS — сейчас (локальный запуск)

Главное: на машине крутится **постоянный процесс**, который сам **опрашивает**
(pull) Telegram и ВК. Пока процесс не запущен — бот не работает.

```mermaid
flowchart LR
    TG_API["Telegram API"]
    VK_API["VK API"]
    DEEPSEEK["DeepSeek API"]

    subgraph MACHINE["Локальная машина — процесс работает 24/7"]
        MAIN["main.py<br/>asyncio.gather двух циклов"]
        TGLOOP["aiogram Dispatcher<br/>start_polling"]
        VKLOOP["vkbottle Bot<br/>run_forever (поток)"]
        ENGINE["RulesEngine<br/>bot/search.py"]
        DB[("rules.db<br/>SQLite, локальный файл")]
        SYNC["sync.py<br/>офлайн-сборка"]
        XLSX[/"rules.xlsx"/]
    end

    MAIN --> TGLOOP
    MAIN --> VKLOOP
    TGLOOP <-->|"опрос getUpdates (pull)"| TG_API
    VKLOOP <-->|"опрос Long Poll (pull)"| VK_API
    TGLOOP --> ENGINE
    VKLOOP --> ENGINE
    ENGINE -->|"читает"| DB
    ENGINE -->|"вопрос + весь архив"| DEEPSEEK
    XLSX --> SYNC -->|"строит"| DB
```

---

## TO BE — после переноса (Yandex Cloud Serverless)

Главное: процесса 24/7 больше нет. Платформы сами **присылают** (push) события
через webhook в API Gateway, тот будит **функцию** только на время обработки.
БД едет внутри пакета и открывается только на чтение.

```mermaid
flowchart LR
    TG_API["Telegram API"]
    VK_API["VK API"]
    DEEPSEEK["DeepSeek API"]

    subgraph LOCAL["Локально / разово — подготовка пакета"]
        XLSX[/"rules.xlsx"/]
        SYNC["sync.py"]
        DBFILE[("rules.db")]
        ZIP["function.zip<br/>код + rules.db + .docx"]
        XLSX --> SYNC --> DBFILE --> ZIP
    end

    subgraph YC["Yandex Cloud — выполняется по запросу"]
        GW["API Gateway<br/>/tg-webhook + /vk-callback"]
        subgraph FN["Cloud Function — Python 3.11"]
            HANDLER["serverless_handler.handler"]
            TGADAPT["aiogram feed_update"]
            VKADAPT["bot/vk_callback"]
            ENGINE["RulesEngine"]
            DBRO[("rules.db<br/>read-only в пакете")]
        end
    end

    TG_API -->|"webhook (push)"| GW
    VK_API -->|"Callback API (push)"| GW
    GW --> HANDLER
    HANDLER -->|"update_id?"| TGADAPT
    HANDLER -->|"type?"| VKADAPT
    TGADAPT --> ENGINE
    VKADAPT --> ENGINE
    ENGINE -->|"читает"| DBRO
    ENGINE -->|"вопрос + весь архив"| DEEPSEEK
    ZIP -.->|"деплой новой версии"| FN
```

---

## Что именно меняется

| | AS IS (сейчас) | TO BE (serverless) |
|---|---|---|
| Модель получения событий | **Pull** — бот сам опрашивает Telegram/ВК | **Push** — платформы шлют события в шлюз |
| Где живёт | Постоянный процесс на машине 24/7 | Функция, просыпается на запрос |
| Точка входа | `main.py` + два вечных цикла | API Gateway → `serverless_handler.handler` |
| Telegram | aiogram `start_polling` | webhook → `feed_update` |
| ВКонтакте | vkbottle `run_forever` (long-poll) | VK Callback API → `vk_callback` |
| База `rules.db` | локальный файл, читается | тот же файл в пакете, открыт `mode=ro` |
| Сборка БД (`sync.py`) | на той же машине | локально/разово, результат кладётся в zip |
| Оплата | за работающую машину постоянно | за время выполнения (в пределах free tier ≈ 0 ₽) |
| Что не меняется | — | `handlers.py`, `search.py`, `sync.py`, логика DeepSeek |

> Бизнес-логика (обработчики Telegram, движок DeepSeek, сборка БД) переезжает
> почти без изменений. Меняется только **слой входа**: вечные циклы опроса → один
> webhook-адаптер, который будит функцию.
