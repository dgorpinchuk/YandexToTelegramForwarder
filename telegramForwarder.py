import re
import os
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from email import policy
from io import BytesIO
import html2text
import logging
import time
import asyncio
from bs4 import BeautifulSoup
from telegram import Bot, InputFile
import tracemalloc

# Включение tracemalloc
tracemalloc.start(10)  # Сохранять данные о последних 10 фреймах

# Функция для чтения конфигурационного файла
def load_config(config_path):
    config = {}
    if os.path.exists(config_path):
        with open(config_path, 'r') as file:
            for line in file:
                if '=' in line:
                    key, value = line.strip().split('=', 1)
                    config[key] = value
    else:
        logging.error(f"❌ Config file {config_path} not found.")
    return config


# Загрузка конфигурации
config = load_config('config.txt')

# Настройки из конфигурационного файла
IMAP_SERVER = config.get('IMAP_SERVER')
IMAP_USER = config.get('IMAP_USER')
IMAP_PASSWORD = config.get('IMAP_PASSWORD')
TELEGRAM_TOKEN = config.get('TELEGRAM_TOKEN')
TELEGRAM_CHANNEL_ID = config.get('TELEGRAM_CHANNEL_ID')

if not (IMAP_SERVER and IMAP_USER and IMAP_PASSWORD and TELEGRAM_TOKEN and TELEGRAM_CHANNEL_ID):
    logging.error(
        "❌ One or more configuration values are missing. Please check config.txt.")
    exit(1)

# Максимальный размер сообщения в Telegram (приблизительно)
MAX_TELEGRAM_MSG_SIZE = 4000
CHECK_INTERVAL = 60  # Проверка писем каждые 30 секунд

# Настройка логирования
logging.basicConfig(level=logging.INFO, filename='mail_to_telegram.log', filemode='a',
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Подключение к Telegram
bot = Bot(token=TELEGRAM_TOKEN)

# Функция для проверки использования памяти
def display_top(snapshot, key_type='lineno', limit=10):
    snapshot = snapshot.filter_traces((
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        tracemalloc.Filter(False, "<unknown>"),
    ))
    top_stats = snapshot.statistics(key_type)

    logging.info("Top %d lines" % limit)
    for index, stat in enumerate(top_stats[:limit], 1):
        frame = stat.traceback[0]
        logging.info("#%s: %s:%s: %.1f KiB"
                     % (index, frame.filename, frame.lineno, stat.size / 1024))
        display_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        logging.info(f"Time: {display_time}\n")

    other = top_stats[limit:]
    if other:
        size = sum(stat.size for stat in other)
        logging.info("%d other: %.1f KiB" % (len(other), size / 1024))
    total = sum(stat.size for stat in top_stats)
    logging.info("Total allocated size: %.1f KiB" % (total / 1024))

# удаление лишнего перед отправкой в Telegram
def clean_text(text):
    
    text = re.sub(r'<\s*img\s+[^>]*?((title|alt)\s*=\s*"(?P<alt>[^"]+)")?[^>]*?/?\s*>',
                     '\g<alt>', text, flags=(re.DOTALL | re.MULTILINE | re.IGNORECASE))

    # remove multiple line breaks and spaces (regular Browser logic)
    text = re.sub(r'\s\s+', ' ', text).strip()

    # remove attributes from elements but href of "a"- elements
    text = re.sub(r'<\s*?(?P<elem>\w+)\b\s*?[^>]*?(?P<ref>\s+href\s*=\s*"[^"]+")?[^>]*?>',
                  '<\g<elem>\g<ref>>', text, flags=(re.DOTALL | re.MULTILINE | re.IGNORECASE))

    # remove style and script elements/blocks
    text = re.sub(r'<\s*(?P<elem>script|style)\s*>.*?</\s*(?P=elem)\s*>',
                  '', text, flags=(re.DOTALL | re.MULTILINE | re.IGNORECASE))

    # translate paragraphs and line breaks (block elements)
    text = re.sub(r'</?\s*(?P<elem>(p|div|table|h\d+))\s*>', '\n', text,
                  flags=(re.MULTILINE | re.IGNORECASE))
    text = re.sub(r'</\s*(?P<elem>(tr))\s*>', '\n', text,
                  flags=(re.MULTILINE | re.IGNORECASE))
    text = re.sub(r'</?\s*(br)\s*[^>]*>', '\n',
                  text, flags=(re.MULTILINE | re.IGNORECASE))

    # prepare list items (migrate list items to "- <text of li element>")
    text = re.sub(r'(<\s*[ou]l\s*>[^<]*)?<\s*li\s*>',
                  '\n- ', text, flags=(re.MULTILINE | re.IGNORECASE))
    text = re.sub(r'</\s*li\s*>([^<]*</\s*[ou]l\s*>)?',
                  '\n', text, flags=(re.MULTILINE | re.IGNORECASE))

    # убираем неподдерживаемые теги
    # https://core.telegram.org/api/entities
    regex_filter_elem = re.compile(
        r'<\s*(?!/?\s*(?P<elem>bold|strong|i|em|u|ins|s|strike|del|b|a|code|pre)\b)[^>]*>',
        flags=(re.MULTILINE | re.IGNORECASE))
    text = re.sub(regex_filter_elem, ' ', text)

    # убираем пустые ссылки
    text = re.sub(r'<\s*a\s*>(?P<link>[^<]*)</\s*a\s*>', '\g<link> ', text,
                  flags=(re.DOTALL | re.MULTILINE | re.IGNORECASE))

    # убираем ссылки без текста (счетчики и т.п.)
    text = re.sub(r'<\s*a\s*[^>]*>\s*</\s*a\s*>', ' ', text,
                  flags=(re.DOTALL | re.MULTILINE | re.IGNORECASE))

    # убираем пустые элементы
    text = re.sub(r'<\s*\w\s*>\s*</\s*\w\s*>', ' ',
                  text, flags=(re.DOTALL | re.MULTILINE))

    # убираем множественные разрывы строк
    text = re.sub(r'\s*[\r\n](\s*[\r\n])+', "\n", text)

    # убираем NBSPs
    text = re.sub(r'&nbsp;', ' ', text, flags=re.IGNORECASE)

    # удаление излишних переноса строки в середине предложения
    text = re.sub(r'([^\.\!\?\n])\n([^\n])', r'\1 \2', text)

    return text

# Убираем цитирование
def remove_quotes(text):
    lines = text.split('\n')
    new_lines = [line for line in lines if not line.strip().startswith('> >')]
    return '\n'.join(new_lines)

# Основная функция
async def check_mail():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        mail.select('inbox')

        status, messages = mail.search(None, '(UNSEEN)')

        if status == 'OK':
            for num in messages[0].split():
                status, data = mail.fetch(num, '(RFC822)')
                if status != 'OK':
                    logging.error('❌ Ошибка получения письма')
                    continue

                for response_part in data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(
                            response_part[1], policy=policy.default)

                        subject_tuple = decode_header(msg['subject'])[0] if msg['subject'] else (None, None)
                        if subject_tuple[0] is not None:
                            try:
                                subject = subject_tuple[0].decode(subject_tuple[1] or 'utf-8') if isinstance(subject_tuple[0], bytes) else subject_tuple[0]
                            except Exception as e:
                                subject = str(subject_tuple[0])
                                logging.error(f"❌ Ошибка декодирования темы письма: {e}")
                        else:
                            subject = "Без темы"

                        from_ = msg.get('From')

                        if msg.is_multipart():
                            body = None
                            attachments = []
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                content_disposition = str(
                                    part.get("Content-Disposition"))

                                # Обработка текста письма
                                if "attachment" not in content_disposition and content_type in ["text/plain", "text/html"]:
                                    body = part.get_payload(decode=True).decode(
                                        part.get_content_charset())
                                    if content_type == "text/html":
                                        # Используем BeautifulSoup для очистки HTML от тегов <div>
                                        soup = BeautifulSoup(
                                            body, 'html.parser')
                                        for div in soup.find_all('div'):
                                            div.unwrap()
                                        body = str(soup)
                                        body = html2text.html2text(body)

                                    # Делаем чистку
                                    body = clean_text(body)
                                    body = remove_quotes(body)

                                # Обработка вложений
                                if "attachment" in content_disposition or content_type.startswith("image/"):
                                    filename = part.get_filename()
                                    if not filename:
                                        ext = content_type.split('/')[-1]
                                        filename = f"image.{ext}"
                                    content = part.get_payload(decode=True)
                                    attachments.append((filename, content))

                        else:
                            body = msg.get_payload(decode=True).decode(
                                msg.get_content_charset())
                            if msg.get_content_type() == "text/html":
                                # Используем BeautifulSoup для очистки HTML от тегов <div>
                                soup = BeautifulSoup(body, 'html.parser')
                                for div in soup.find_all('div'):
                                    div.unwrap()
                                body = str(soup)
                                body = html2text.html2text(body)

                            # Делаем чистку
                            body = clean_text(body)
                            body = remove_quotes(body)

                        # Отправка текста сообщения
                        if body:
                            if len(body) > MAX_TELEGRAM_MSG_SIZE:
                                body = body[:MAX_TELEGRAM_MSG_SIZE] + \
                                    "\n\n✨ Сокращено..."
                            await bot.send_message(
                                chat_id=TELEGRAM_CHANNEL_ID,
                                text=f"✉ Новое письмо olimp@iproficlub.ru\n👤 От: {from_}\n📣 Тема: {subject}\n\n🔸🔸🔸\n\n{body}"
                            )

                        # Отправка вложений и инлайн-изображений
                        for filename, content in attachments:
                            file = BytesIO(content)
                            file.name = filename
                            await bot.send_document(chat_id=TELEGRAM_CHANNEL_ID, document=file)
    except Exception as e:
        logging.error(f"❌ Произошла ошибка: {e}")
    finally:
        mail.logout()


async def clear_log():
    open('mail_to_telegram.log', 'w').close()


async def main():
    while True:
        await check_mail()
        await asyncio.sleep(CHECK_INTERVAL)
        if time.time() % (7 * 24 * 60 * 60) < CHECK_INTERVAL:
            await clear_log()

        # Каждый интервал CHECK_INTERVAL, выводим отчет по памяти
        snapshot = tracemalloc.take_snapshot()
        display_top(snapshot)

if __name__ == "__main__":
    asyncio.run(main())
