import re
import os
import imaplib
import email
from email.header import decode_header
from email import policy
import html
import logging
import time
import asyncio
from bs4 import BeautifulSoup
import requests
import unicodedata

# ================= CONFIG =================


def load_config(config_path):
    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r") as file:
            for line in file:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    config[key] = value
    else:
        logging.error(f"❌ Config file {config_path} not found.")
    return config


config = load_config("config.txt")

IMAP_SERVER = config.get("IMAP_SERVER")
IMAP_USER = config.get("IMAP_USER")
IMAP_PASSWORD = config.get("IMAP_PASSWORD")
MTS_WEBHOOK_URL = config.get("MTS_WEBHOOK_URL")

if not (IMAP_SERVER and IMAP_USER and IMAP_PASSWORD and MTS_WEBHOOK_URL):
    logging.error("❌ Missing config values")
    exit(1)

CHECK_INTERVAL = 60
MAX_MSG_SIZE = 4000

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    filename="mail_to_mts.log",
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# ================= SEND =================


def send_to_mts(text):
    try:
        payload = {"text": text}
        response = requests.post(MTS_WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code != 200:
            logging.error(f"❌ MTS error: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"❌ Send exception: {e}")


async def send_to_mts_async(text):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_to_mts, text)


# ================= TEXT CLEANING =================

def decode_html_entities(text):
    """Рекурсивное декодирование HTML-сущностей"""
    prev = None
    while prev != text:
        prev = text
        text = html.unescape(text)
    return text

def remove_quotes(text):
    """Удаление цитирования (>), старых переписок и подписей"""
    lines = text.splitlines()
    cleaned = []
    quote_pattern = re.compile(r'^\s*>+\s*')
    for line in lines:
        stripped = line.strip()
        if quote_pattern.match(line):
            continue
        if re.search(r'(писал:|wrote:)', stripped, re.IGNORECASE):
            break
        if stripped.startswith('--') or stripped.startswith('С уважением,'):
            break
        cleaned.append(line)
    return '\n'.join(cleaned)

def clean_text(text):
    """Основная очистка – убирает невидимые символы, заменяя их пробелами"""
    import unicodedata
    # 1. Предварительное декодирование сущностей
    text = html.unescape(text)

    # 2. Замена проблемных символов на пробелы
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch == ' ':
            cleaned.append(' ')
        elif ch in '\n\r\t':
            cleaned.append(ch)
        elif cat in ('Cc', 'Cf', 'Zl', 'Zp'):
            cleaned.append(' ')
        elif cat == 'Zs' and ch != ' ':
            cleaned.append(' ')
        elif '\u2800' <= ch <= '\u28FF':  # брайль
            cleaned.append(' ')
        else:
            cleaned.append(ch)
    text = ''.join(cleaned)

    # 3. Схлопывание пробелов
    text = re.sub(r'[ \t]+', ' ', text)

    # 4. Удаление мусорных блоков
    text = re.sub(r'Links:\s*-+.*', '', text, flags=re.DOTALL)
    text = re.sub(r'\[\d+\]\s*https?://\S+', '', text)
    text = re.sub(r'(?<!\w)[*_|~-]+(?!\w)', '', text)

    # 5. Повторное декодирование (для &#43; и подобных)
    text = html.unescape(text)

    # 6. Убираем пустые строки
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    return '\n'.join(lines).strip()

def html_to_text(html_content):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')
    for tag in soup(['script', 'style']):
        tag.decompose()
    return soup.get_text(separator=' ', strip=True)

def force_remove_html_entities(text):
    """Для заголовков – принудительная замена &lt; &gt;"""
    if not text:
        return text
    text = text.replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&#60;', '<').replace('&#62;', '>')
    text = text.replace('&amp;lt;', '<').replace('&amp;gt;', '>')
    text = html.unescape(text)
    # Замена на безопасные символы для МТС Линк (раскомментировать при необходимости)
    text = text.replace('<', '‹').replace('>', '›')
    return text


# ================= MAIL =================


async def check_mail():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, "(UNSEEN)")
        if status != "OK":
            return

        for num in messages[0].split():
            status, data = mail.fetch(num, "(RFC822)")
            if status != "OK":
                continue

            for response_part in data:
                if not isinstance(response_part, tuple):
                    continue

                msg = email.message_from_bytes(response_part[1], policy=policy.default)

                # ----- Декодирование Subject -----
                raw_subject = msg.get("subject", "")
                if raw_subject:
                    decoded_parts = []
                    for part, encoding in decode_header(raw_subject):
                        if isinstance(part, bytes):
                            try:
                                decoded_parts.append(
                                    part.decode(encoding or "utf-8", errors="replace")
                                )
                            except (LookupError, TypeError):
                                decoded_parts.append(
                                    part.decode("utf-8", errors="replace")
                                )
                        else:
                            decoded_parts.append(part)
                    subject = " ".join(decoded_parts)
                else:
                    subject = "Без темы"
                subject = force_remove_html_entities(subject)

                # ----- Декодирование From -----
                raw_from = msg.get("From", "")
                decoded_parts = []
                for part, encoding in decode_header(raw_from):
                    if isinstance(part, bytes):
                        try:
                            decoded_parts.append(
                                part.decode(encoding or "utf-8", errors="replace")
                            )
                        except (LookupError, TypeError):
                            decoded_parts.append(part.decode("utf-8", errors="replace"))
                    else:
                        decoded_parts.append(part)
                from_ = " ".join(decoded_parts)
                from_ = force_remove_html_entities(from_)
                # logging.info(f"DEBUG from_ after cleaning: {repr(from_)}")

                # ----- Получение тела письма -----
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        disposition = str(part.get("Content-Disposition"))
                        if "attachment" in disposition:
                            continue
                        if content_type in ["text/plain", "text/html"]:
                            try:
                                charset = part.get_content_charset() or "utf-8"
                                raw = part.get_payload(decode=True).decode(
                                    charset, errors="replace"
                                )
                            except Exception as e:
                                logging.warning(f"Decode error: {e}, fallback to utf-8")
                                raw = part.get_payload(decode=True).decode(
                                    "utf-8", errors="replace"
                                )

                            if content_type == "text/html":
                                body = html_to_text(raw)
                            else:
                                body = raw
                            break
                else:
                    try:
                        charset = msg.get_content_charset() or "utf-8"
                        raw = msg.get_payload(decode=True).decode(
                            charset, errors="replace"
                        )
                    except Exception as e:
                        logging.warning(f"Decode error: {e}, fallback to utf-8")
                        raw = msg.get_payload(decode=True).decode(
                            "utf-8", errors="replace"
                        )

                    if msg.get_content_type() == "text/html":
                        body = html_to_text(raw)
                    else:
                        body = raw

                if not body:
                    continue

                # ----- Очистка тела письма -----
                body = decode_html_entities(body)  # &gt; → >
                body = remove_quotes(body)
                body = clean_text(body)
                body = decode_html_entities(body)  # повторная страховка

                if len(body) > MAX_MSG_SIZE:
                    body = body[:MAX_MSG_SIZE] + "\n\n✨ Сокращено..."

                message_text = (
                    f"✉ Новое письмо\n"
                    f"👤 От: {from_}\n"
                    f"📣 Тема: {subject}\n\n"
                    f"🔸🔸🔸\n\n{body}"
                )

                await send_to_mts_async(message_text)

                # Помечаем письмо как прочитанное
                mail.store(num, "+FLAGS", "\\Seen")

    except Exception as e:
        logging.error(f"❌ Mail error: {e}")
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass


# ================= SERVICE =================


async def clear_log():
    open("mail_to_mts.log", "w").close()


async def main():
    last_clear = time.time()
    while True:
        await check_mail()
        await asyncio.sleep(CHECK_INTERVAL)

        if time.time() - last_clear >= 7 * 24 * 3600:
            await clear_log()
            last_clear = time.time()


if __name__ == "__main__":
    asyncio.run(main())
