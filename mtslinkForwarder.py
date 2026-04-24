import re
import os
import imaplib
import email
from email.header import decode_header
from email import policy
import html2text
import logging
import time
import asyncio
from bs4 import BeautifulSoup
import tracemalloc
import requests

# Включение tracemalloc
tracemalloc.start(10)

# ================= CONFIG =================

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


config = load_config('config.txt')

IMAP_SERVER = config.get('IMAP_SERVER')
IMAP_USER = config.get('IMAP_USER')
IMAP_PASSWORD = config.get('IMAP_PASSWORD')
MTS_WEBHOOK_URL = config.get('MTS_WEBHOOK_URL')

if not (IMAP_SERVER and IMAP_USER and IMAP_PASSWORD and MTS_WEBHOOK_URL):
    logging.error("❌ Missing config values")
    exit(1)

CHECK_INTERVAL = 60
MAX_MSG_SIZE = 4000

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    filename='mail_to_mts.log',
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ================= SEND =================

def send_to_mts(text):
    try:
        payload = {
            "text": text
        }

        response = requests.post(
            MTS_WEBHOOK_URL,
            json=payload,
            timeout=10
        )

        if response.status_code != 200:
            logging.error(
                f"❌ MTS error: {response.status_code} {response.text}"
            )

    except Exception as e:
        logging.error(f"❌ Send exception: {e}")


async def send_to_mts_async(text):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_to_mts, text)

# ================= CLEAN =================

def clean_text(text):
    text = re.sub(r'<\s*img\s+[^>]*?((title|alt)\s*=\s*"(?P<alt>[^"]+)")?[^>]*?/?\s*>',
                  '\g<alt>', text, flags=re.DOTALL | re.IGNORECASE)

    text = re.sub(r'\s\s+', ' ', text).strip()

    text = re.sub(r'<\s*?(?P<elem>\w+)\b\s*?[^>]*?(?P<ref>\s+href\s*=\s*"[^"]+")?[^>]*?>',
                  '<\g<elem>\g<ref>>', text, flags=re.DOTALL | re.IGNORECASE)

    text = re.sub(r'<\s*(script|style)\s*>.*?</\s*(script|style)\s*>',
                  '', text, flags=re.DOTALL | re.IGNORECASE)

    text = re.sub(r'</?\s*(p|div|table|h\d+)\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</\s*tr\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?\s*br\s*/?>', '\n', text, flags=re.IGNORECASE)

    text = re.sub(r'(<\s*[ou]l\s*>[^<]*)?<\s*li\s*>',
                  '\n- ', text, flags=re.IGNORECASE)
    text = re.sub(r'</\s*li\s*>', '\n', text, flags=re.IGNORECASE)

    regex_filter_elem = re.compile(
        r'<\s*(?!/?\s*(b|strong|i|em|u|a|code|pre)\b)[^>]*>',
        flags=re.IGNORECASE
    )
    text = re.sub(regex_filter_elem, ' ', text)

    text = re.sub(r'<\s*a\s*>([^<]*)</\s*a\s*>', r'\1 ', text)
    text = re.sub(r'<\s*a\s*[^>]*>\s*</\s*a\s*>', ' ', text)

    text = re.sub(r'\s*[\r\n](\s*[\r\n])+', "\n", text)
    text = re.sub(r'&nbsp;', ' ', text)

    text = re.sub(r'([^\.\!\?\n])\n([^\n])', r'\1 \2', text)

    return text


def remove_quotes(text):
    lines = text.split('\n')
    return '\n'.join(
        line for line in lines if not line.strip().startswith('> >')
    )

# ================= MAIL =================

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
                    continue

                for response_part in data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(
                            response_part[1], policy=policy.default)

                        subject_tuple = decode_header(msg['subject'])[0] if msg['subject'] else (None, None)

                        if subject_tuple[0]:
                            try:
                                subject = subject_tuple[0].decode(
                                    subject_tuple[1] or 'utf-8'
                                ) if isinstance(subject_tuple[0], bytes) else subject_tuple[0]
                            except:
                                subject = str(subject_tuple[0])
                        else:
                            subject = "Без темы"

                        from_ = msg.get('From')
                        body = ""

                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                disposition = str(part.get("Content-Disposition"))

                                if "attachment" not in disposition and content_type in ["text/plain", "text/html"]:
                                    body = part.get_payload(decode=True).decode(
                                        part.get_content_charset() or 'utf-8',
                                        errors='ignore'
                                    )

                                    if content_type == "text/html":
                                        soup = BeautifulSoup(body, 'html.parser')
                                        for div in soup.find_all('div'):
                                            div.unwrap()
                                        body = html2text.html2text(str(soup))

                                    body = clean_text(body)
                                    body = remove_quotes(body)
                                    break
                        else:
                            body = msg.get_payload(decode=True).decode(
                                msg.get_content_charset() or 'utf-8',
                                errors='ignore'
                            )

                            if msg.get_content_type() == "text/html":
                                soup = BeautifulSoup(body, 'html.parser')
                                for div in soup.find_all('div'):
                                    div.unwrap()
                                body = html2text.html2text(str(soup))

                            body = clean_text(body)
                            body = remove_quotes(body)

                        if not body:
                            continue

                        if len(body) > MAX_MSG_SIZE:
                            body = body[:MAX_MSG_SIZE] + "\n\n✨ Сокращено..."

                        message_text = (
                            f"✉ Новое письмо\n"
                            f"👤 От: {from_}\n"
                            f"📣 Тема: {subject}\n\n"
                            f"🔸🔸🔸\n\n{body}"
                        )

                        await send_to_mts_async(message_text)

    except Exception as e:
        logging.error(f"❌ Mail error: {e}")
    finally:
        try:
            mail.logout()
        except:
            pass

# ================= SERVICE =================

async def clear_log():
    open('mail_to_mts.log', 'w').close()


async def main():
    while True:
        await check_mail()
        await asyncio.sleep(CHECK_INTERVAL)

        if time.time() % (7 * 24 * 60 * 60) < CHECK_INTERVAL:
            await clear_log()

        snapshot = tracemalloc.take_snapshot()

if __name__ == "__main__":
    asyncio.run(main())