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
from bs4 import BeautifulSoup, Comment
import requests

# ================= CONFIG =================

def load_config(config_path):
    config = {}
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as file:
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
    raise SystemExit(1)

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
    """Send text to MTS Link and report whether delivery succeeded."""
    try:
        response = requests.post(
            MTS_WEBHOOK_URL,
            json={"text": text},
            timeout=10
        )

        if 200 <= response.status_code < 300:
            logging.info("✅ Message sent to MTS Link")
            return True

        logging.error(
            f"❌ MTS error: {response.status_code} {response.text}"
        )
        return False

    except requests.RequestException as e:
        logging.error(f"❌ Send exception: {e}")
        return False


async def send_to_mts_async(text):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, send_to_mts, text)

# ================= TEXT CLEANING =================

def decode_header_value(value, default=''):
    """Decode a MIME header containing one or more encoded parts."""
    if not value:
        return default

    decoded_parts = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(
                    part.decode(encoding or 'utf-8', errors='replace')
                )
            except (LookupError, TypeError):
                decoded_parts.append(part.decode('utf-8', errors='replace'))
        else:
            decoded_parts.append(str(part))

    return html.unescape(''.join(decoded_parts)).strip()


def normalize_whitespace(text):
    """Normalize spaces while preserving meaningful line breaks."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\xa0', ' ')

    # Trailing spaces on individual lines.
    text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
    # More than one space inside a line.
    text = re.sub(r'[ \t]+', ' ', text)
    # More than two consecutive empty lines.
    text = re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', text)

    return text.strip()


def remove_quotes(text):
    """Remove obvious quoted replies without cutting ordinary sentences."""
    lines = text.splitlines()
    cleaned = []
    quote_header = re.compile(
        r'^\s*(?:On .+wrote:|.+(?:писал|писала|написал|написала):)\s*$',
        re.IGNORECASE
    )
    quote_pattern = re.compile(r'^\s*>+\s?')

    for line in lines:
        stripped = line.strip()

        if quote_pattern.match(line):
            continue

        # Stop only on a line that is clearly a quoted-message header.
        if quote_header.match(stripped):
            break

        # Common signature separator. Do not treat a normal sentence containing
        # "С уважением," as a cut point unless it starts the line.
        if stripped == '--' or stripped == 'С уважением,':
            break

        cleaned.append(line)

    return '\n'.join(cleaned)


def clean_text(text):
    """Final cleanup of already extracted plain text."""
    if not text:
        return ''

    # Legacy html2text-style reference blocks, if they occur in a plain-text part.
    text = re.sub(r'^\s*Links:\s*-+\s*$', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'^\s*\[\d+\]\s*https?://\S+\s*$', '', text, flags=re.MULTILINE)

    text = html.unescape(text)
    text = normalize_whitespace(text)
    return text.strip()


def html_to_text(html_content):
    """Convert HTML email to readable plain text while preserving structure."""
    soup = BeautifulSoup(html_content, 'html.parser')

    # Remove technical/non-visible content.
    for tag in soup(['script', 'style', 'noscript', 'template', 'head']):
        tag.decompose()

    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    # Images are usually logos, tracking pixels or decorative elements.
    # Keep meaningful alt text, otherwise remove the image entirely.
    for img in soup.find_all('img'):
        alt = img.get('alt', '').strip()
        if alt:
            img.replace_with(f'[{alt}]')
        else:
            img.decompose()

    # Explicit line breaks.
    for br in soup.find_all('br'):
        br.replace_with('\n')

    # Lists become readable bullet points.
    for li in soup.find_all('li'):
        li.insert_before('\n- ')
        li.insert_after('\n')

    # Tables: keep rows and cells visually separated.
    for tr in soup.find_all('tr'):
        tr.insert_before('\n')
        tr.insert_after('\n')

    for cell in soup.find_all(['td', 'th']):
        cell.insert_after(' | ')

    # Block-level elements should not be glued together.
    block_tags = [
        'p', 'div', 'section', 'article', 'header', 'footer',
        'blockquote', 'address', 'figure', 'figcaption',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    ]
    for tag in soup.find_all(block_tags):
        tag.insert_before('\n')
        tag.insert_after('\n')

    text = soup.get_text()
    return normalize_whitespace(html.unescape(text))


# ================= MAIL =================

def get_message_body(msg):
    """Extract the best available body from an email.

    Prefer text/plain. Fall back to text/html when plain text is absent or empty.
    Attachments are ignored.
    """
    plain_parts = []
    html_parts = []

    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = [msg]

    for part in parts:
        content_type = part.get_content_type()
        disposition = (part.get('Content-Disposition') or '').lower()

        if 'attachment' in disposition:
            continue
        if content_type not in ('text/plain', 'text/html'):
            continue

        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            charset = part.get_content_charset() or 'utf-8'
            raw = payload.decode(charset, errors='replace')
        except (LookupError, UnicodeDecodeError, AttributeError, TypeError) as e:
            logging.warning(f"Decode error for {content_type}: {e}; using utf-8")
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                raw = payload.decode('utf-8', errors='replace')
            except Exception as fallback_error:
                logging.warning(f"Fallback decode error: {fallback_error}")
                continue

        if content_type == 'text/plain':
            plain_parts.append(raw)
        else:
            html_parts.append(raw)

    plain_text = clean_text('\n'.join(plain_parts))
    if plain_text:
        return plain_text

    if html_parts:
        return clean_text(html_to_text('\n'.join(html_parts)))

    return ''


async def check_mail():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        mail.select('inbox')

        status, messages = mail.search(None, '(UNSEEN)')
        if status != 'OK':
            logging.error(f"❌ IMAP search error: {status}")
            return

        for num in messages[0].split():
            status, data = mail.fetch(num, '(RFC822)')
            if status != 'OK':
                logging.error(f"❌ Error fetching message {num!r}")
                continue

            for response_part in data:
                if not isinstance(response_part, tuple):
                    continue

                msg = email.message_from_bytes(response_part[1], policy=policy.default)

                subject = decode_header_value(msg.get('subject'), 'Без темы')
                from_ = decode_header_value(msg.get('From'), 'Неизвестный отправитель')
                body = get_message_body(msg)

                if not body:
                    logging.warning(
                        f"⚠️ Empty body, message {num!r} was not marked as read"
                    )
                    continue

                message_text = (
                    "✉ Новое письмо\n"
                    f"👤 От: {from_}\n"
                    f"📣 Тема: {subject}\n\n"
                    "🔸🔸🔸\n\n"
                    f"{body}"
                )

                # Limit the complete message, including metadata and truncation marker.
                if len(message_text) > MAX_MSG_SIZE:
                    suffix = "\n\n✨ Сокращено..."
                    message_text = message_text[:MAX_MSG_SIZE - len(suffix)] + suffix

                # Mark as read only after confirmed successful delivery.
                if await send_to_mts_async(message_text):
                    mail.store(num, '+FLAGS', '\\Seen')
                else:
                    logging.error(
                        f"❌ Message {num!r} was not marked as read; it will be retried"
                    )

    except Exception as e:
        logging.error(f"❌ Mail error: {e}")
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass

# ================= SERVICE =================

async def clear_log():
    open('mail_to_mts.log', 'w').close()


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
