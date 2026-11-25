# TaskHub MVP

Рабочий MVP: Telegram WebApp auth, задачи, захват задачи, сдача, проверка администратором, начисление внутреннего баланса.

## Railway
- Root Directory: `/` (если репозиторий содержит эти файлы в корне)
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Variables: `BOT_TOKEN`, `PUBLIC_URL` (домен Railway с https), `ADMIN_TELEGRAM_IDS` (Telegram ID администратора), `DEV_MODE=false`.
- Подключить Railway Volume к `/data` для сохранения SQLite. Без Volume база может потеряться при пересоздании контейнера.
- Бот запускается в том же сервисе через polling, когда заданы BOT_TOKEN и PUBLIC_URL. Для одного экземпляра.

## Важно
Открывать Mini App через кнопку Telegram-бота. Прямое открытие домена в браузере без Telegram initData не авторизует пользователя.
