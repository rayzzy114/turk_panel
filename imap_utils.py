import asyncio
import imaplib
import email
import re
import logging
from email.header import decode_header
from typing import Optional

LOGGER = logging.getLogger("imap_utils")

# Default IMAP servers for popular domains
IMAP_SERVERS = {
    "gmail.com": "imap.gmail.com",
    "mail.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "yandex.ru": "imap.yandex.ru",
    "ya.ru": "imap.yandex.ru",
    "rambler.ru": "imap.rambler.ru",
    "hotmail.com": "outlook.office365.com",
    "outlook.com": "outlook.office365.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "firstmail.com": "imap.firstmail.ltd",  # Common in farming
}


def guess_imap_server(email_address: str) -> str:
    """Try to guess IMAP server from email domain."""
    domain = email_address.split("@")[-1].lower()
    return IMAP_SERVERS.get(domain, f"imap.{domain}")


def _decode_header_value(header_val) -> str:
    """Decode email header value."""
    if not header_val:
        return ""

    decoded = decode_header(header_val)
    result = ""
    for content, charset in decoded:
        if isinstance(content, bytes):
            try:
                result += content.decode(charset or "utf-8", errors="replace")
            except LookupError:
                result += content.decode("utf-8", errors="replace")
        else:
            result += str(content)
    return result


async def get_facebook_code(
    email_login: str,
    email_password: str,
    imap_server: Optional[str] = None,
    timeout_sec: int = 120,
    poll_interval_sec: int = 10,
) -> Optional[str]:
    """
    Connects to IMAP server and waits for an email from Facebook with a verification code.
    Returns the code as a string, or None if not found within the timeout.
    """
    if not imap_server:
        imap_server = guess_imap_server(email_login)

    LOGGER.info(f"Looking for Facebook code for {email_login} via {imap_server}...")

    start_time = asyncio.get_event_loop().time()

    while (asyncio.get_event_loop().time() - start_time) < timeout_sec:
        try:
            # Run blocking IMAP operations in an executor
            code = await asyncio.to_thread(
                _check_inbox_sync, email_login, email_password, imap_server
            )
            if code:
                LOGGER.info(f"Found Facebook code: {code}")
                return code

        except Exception as e:
            LOGGER.error(f"IMAP error for {email_login}: {str(e)}")
            # Don't break immediately on error, might be a temporary network issue

        await asyncio.sleep(poll_interval_sec)

    LOGGER.warning(f"Timeout waiting for Facebook code for {email_login}")
    return None


def _check_inbox_sync(
    email_login: str, email_password: str, imap_server: str
) -> Optional[str]:
    """Blocking function to check inbox for Facebook verification code."""
    try:
        # Connect to server
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(email_login, email_password)

        # Select inbox
        mail.select("inbox")

        # Search for recent emails from Facebook
        # Using UNSEEN is risky as it might have been read by another client,
        # so we search all recent emails from security@facebookmail.com
        status, messages = mail.search(None, '(FROM "security@facebookmail.com")')

        if status != "OK" or not messages[0]:
            # Try searching just by "Facebook" in sender or subject if the specific email fails
            status, messages = mail.search(None, '(FROM "Facebook")')
            if status != "OK" or not messages[0]:
                mail.logout()
                return None

        # Get list of email IDs
        email_ids = messages[0].split()

        # Check the last 5 emails (newest first)
        for e_id in reversed(email_ids[-5:]):
            status, msg_data = mail.fetch(e_id, "(RFC822)")
            if status != "OK":
                continue

            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])

                    # Check subject for code (Facebook often puts the code in the subject like "123456 is your Facebook recovery code")
                    subject = _decode_header_value(msg["Subject"])

                    # Regex for 6-8 digit code in subject
                    code_match = re.search(r"\b(\d{6,8})\b", subject)
                    if code_match:
                        mail.logout()
                        return code_match.group(1)

                    # If not in subject, check body
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ["text/plain", "text/html"]:
                                body = part.get_payload(decode=True).decode(
                                    errors="ignore"
                                )
                                code_match = re.search(r"\b(\d{6,8})\b", body)
                                if code_match:
                                    mail.logout()
                                    return code_match.group(1)
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")
                        code_match = re.search(r"\b(\d{6,8})\b", body)
                        if code_match:
                            mail.logout()
                            return code_match.group(1)

        mail.logout()
    except Exception as e:
        raise Exception(f"IMAP sync check failed: {str(e)}")

    return None
