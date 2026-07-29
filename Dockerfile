FROM aiogram/telegram-bot-api:latest AS tgapi
FROM python:3.11-slim
COPY --from=tgapi /usr/local/bin/telegram-bot-api /usr/local/bin/
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD sh -c 'if [ -n "$TG_API_ID" ] && [ -n "$TG_API_HASH" ]; then telegram-bot-api --local --api-id=$TG_API_ID --api-hash=$TG_API_HASH --http-port=8081 --dir=/tmp/tgapi & sleep 3; fi; exec python bot.py'
