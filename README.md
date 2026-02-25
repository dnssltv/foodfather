\# Food Telegram Bot (Gemini + Railway)



Бот для группы:

\- анализ фото еды (Gemini Vision)

\- напоминания: вода/шаги/вес

\- трекинг веса и шагов



\## Команды

\- /bind — включить напоминания в этой группе

\- /unbind — выключить

\- /goal cut|maintain|bulk — цель

\- /rules — правила оценки

\- /stats — статистика веса



\## Переменные окружения

\- BOT\_TOKEN — токен Telegram бота

\- GEMINI\_API\_KEY — ключ Gemini API

\- TZ — Asia/Almaty



\## Локальный запуск

1\) Python 3.11+

2\) pip install -r requirements.txt

3\) Создай .env по примеру .env.example

4\) python bot.py



\## Деплой на Railway

1\) Залей проект на GitHub

2\) Railway → New Project → Deploy from GitHub

3\) Variables:

&nbsp;  - BOT\_TOKEN

&nbsp;  - GEMINI\_API\_KEY

&nbsp;  - TZ=Asia/Almaty

4\) Deploy

5\) В группе: /bind

