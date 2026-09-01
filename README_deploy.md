# Freqtrade — установка и использование

## Что это
Freqtrade — бесплатный торговый бот для криптобирж на Python. Поддерживает dry-run (без реальных денег), бэктестинг, оптимизацию стратегий, управление через Telegram и веб-интерфейс (FreqUI).

⚠️ Всегда сначала запускайте бота в режиме **dry-run** и не вкладывайте реальные деньги, пока не разберётесь, как он работает.

---

## Способ 1: Docker (рекомендуется, самый быстрый старт)

Требуется установленный [Docker](https://www.docker.com/products/docker).

```bash
# 1. Создаём рабочую директорию и скачиваем docker-compose.yml
mkdir ft_userdata
cd ft_userdata
curl https://raw.githubusercontent.com/freqtrade/freqtrade/stable/docker-compose.yml -o docker-compose.yml

# 2. Загружаем образ и создаём структуру каталогов (user_data)
docker compose pull
docker compose run --rm freqtrade create-userdir --userdir user_data

# 3. Создаём конфиг (бот задаст вопросы: биржа, пары, dry-run и т.д.)
docker compose run --rm freqtrade new-config --config user_data/config.json

# 4. Запускаем бота
docker compose up -d

# Логи
docker compose logs -f
```

Остановить: `docker compose down`.

---

## Способ 2: Установка через pip / venv (Linux/macOS/Windows)

Требования: Python ≥ 3.11, git, TA-Lib.

```bash
git clone https://github.com/freqtrade/freqtrade.git
cd freqtrade

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

./setup.sh -i                  # Linux/macOS: ставит TA-Lib и зависимости
# либо вручную:
pip install -e .

freqtrade create-userdir --userdir user_data
freqtrade new-config --config user_data/config.json
```

На Windows проще всего использовать Docker или WSL — нативная установка сложнее из-за TA-Lib.

---

## Настройка config.json

Мастер `new-config` спросит:
- биржу (Binance, Bybit, OKX и т.д.) и API-ключи (для dry-run можно оставить пустыми)
- торговые пары (whitelist)
- режим dry-run/live
- нужен ли Telegram-бот (токен и chat_id)

Ключевые параметры в `user_data/config.json`:
- `"dry_run": true` — обязательно на старте
- `"stake_currency"`, `"stake_amount"` — валюта и размер ставки
- `"exchange"` — биржа и API-ключи
- `"pair_whitelist"` — какие пары торговать

Стратегия по умолчанию — `SampleStrategy` в `user_data/strategies/`. Свою стратегию можно создать так:

```bash
freqtrade new-strategy --strategy MyAwesomeStrategy
```

---

## Основные команды

```bash
# Запуск торговли (или продолжайте через docker compose up -d)
freqtrade trade --config user_data/config.json --strategy SampleStrategy

# Скачать исторические данные
freqtrade download-data --exchange binance --pairs BTC/USDT --timeframe 5m --days 180

# Бэктестинг стратегии на исторических данных
freqtrade backtesting --config user_data/config.json --strategy SampleStrategy

# Подбор параметров стратегии (hyperopt)
freqtrade hyperopt --config user_data/config.json --strategy SampleStrategy --hyperopt-loss SharpeHyperOptLoss

# Веб-интерфейс (FreqUI) отдельно от торговли
freqtrade webserver --config user_data/config.json
```

---

## Управление ботом

**Telegram** (если настроен в конфиге):
- `/start`, `/stop` — старт/стоп торговли
- `/status` — открытые сделки
- `/profit` — суммарная прибыль
- `/forceexit <id>|all` — закрыть сделку(и) вручную
- `/balance` — баланс по валютам

**FreqUI (веб-интерфейс)**, порт 8086 (`http://127.0.0.1:8086`):
```bash
freqtrade install-ui   # если не через Docker-образ
```
Доступ настраивается в блоке `"api_server"` конфига (логин/пароль, `"enabled": true`). Порт задаётся полем `"listen_port"`:

```json
"api_server": {
    "enabled": true,
    "listen_ip_address": "127.0.0.1",
    "listen_port": 8086,
    ...
}
```

В `docker-compose.yml` порт также проброшен как `8086:8086` — при смене порта в конфиге меняйте оба значения в маппинге `ports`.

---

## Порядок действий для новичка
1. Установить (Docker или pip).
2. `new-config` → dry-run включён.
3. Скачать данные, прогнать `backtesting` на своей стратегии.
4. Запустить `trade` в dry-run и понаблюдать несколько дней.
5. Только потом — при полном понимании рисков — выключить `dry_run` и добавить реальные API-ключи.

Полная документация: https://www.freqtrade.io
