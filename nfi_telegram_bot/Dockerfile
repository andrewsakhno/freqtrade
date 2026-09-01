FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py bots.py bot.py formatting.py freqtrade_client.py json_utils.py \
     ./nfi_telegram_bot/

CMD ["python", "-m", "nfi_telegram_bot.bot"]
