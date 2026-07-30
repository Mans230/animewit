FROM aiogram/telegram-bot-api:latest AS tgapi
# NOTE: final stage must be Alpine (musl) — the telegram-bot-api binary from the
# aiogram image is musl-linked and will NOT run on Debian slim ("not found").
FROM python:3.11-alpine
RUN apk add --no-cache libstdc++
COPY --from=tgapi /usr/local/bin/telegram-bot-api /usr/local/bin/
# Fail the build early if the binary cannot run in this stage.
RUN telegram-bot-api --help > /dev/null
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD sh -c 'if [ -n "$TG_API_ID" ] && [ -n "$TG_API_HASH" ]; then telegram-bot-api --local --api-id=$TG_API_ID --api-hash=$TG_API_HASH --http-port=8081 --dir=/tmp/tgapi & fi; exec python bot.py'
