# YandexToTelegramForwarder
Скрипт позволяет пересылать сообщения из папки `Входящие` почтового аккаунта, используя IMAP, в чат, группу или канал в Telegram. После пересылки письмо в почте помечается прочитанным.
Пересылается текст письма (обрезается, если превышает 4000 символов, это ограничение Telegram в 4096 символов), inline картинки в теле письма, а также все файлы (вложения), прикрепленные к письму.

В самом скрипте можно задать:
```python
MAX_TELEGRAM_MSG_SIZE = 4000 # ограничение количества символов, после которого текст письма будет обрезан
CHECK_INTERVAL = 60  # интервал проверки новых писем в секундах
```

При работе скрипта создается лог файл `mail_to_telegram.log`

Посмотреть содержимое:
```console
cat /путь-к-файлу/mail_to_telegram.log
```

Функция очищает его каждые 7 дней:
```python
async def main():
    while True:
        await check_mail()
        await asyncio.sleep(CHECK_INTERVAL)
        if time.time() % (7 * 24 * 60 * 60) < CHECK_INTERVAL:
            await clear_log()
```

1. Установите Python 3.10 или выше.
2. Установите модули из `requirements.txt`
3. Заполните файл конфигурации `config.txt`:
   
   - `IMAP_SERVER=imap.example.com` - IMAP сервер почты (в случае Яндекс: `imap.yandex.ru`)
   - `IMAP_USER=user@example.com` - аккаунт, с которого будем пересылать почту
   - `IMAP_PASSWORD=1234567890` - пароль аккаунта. В случае Яндекса используем пароль, созданный через [Пароли приложений.](https://yandex.ru/support/id/ru/authorization/app-passwords)
   - `TELEGRAM_TOKEN=1234567890` - токен от вашего бота в Telegram ([как получить](https://helpdesk.bitrix24.ru/open/17538378/))
   - `TELEGRAM_CHANNEL_ID=1234567890` - ID канала, чата или группы, в которую будем пересылать письма ([как узнать](https://t.me/getmyid_bot))

5. Запустите `telegramForwarder.py`

> TO DO: Как настроить и запустить такое приложение на VPS
