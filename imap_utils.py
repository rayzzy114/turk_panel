import asyncio
import imaplib
import email
import re
import logging
from email.header import decode_header
from typing import Optional

LOGGER = logging.getLogger("imap_utils")
_FACEBOOK_CODE_RE = re.compile(r"\b(\d{6,8})\b")

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


def _decode_payload_text(payload: object) -> str:
    if isinstance(payload, bytes):
        return payload.decode(errors="ignore")
    if isinstance(payload, str):
        return payload
    return ""


def _extract_facebook_codes_from_webmail_text(body_text: str) -> list[str]:
    """Extracts visible Facebook verification codes from SnappyMail inbox text in display order."""
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    codes: list[str] = []
    for index, line in enumerate(lines):
        if "facebook" not in line.lower():
            continue
        for candidate in lines[index : index + 4]:
            match = _FACEBOOK_CODE_RE.search(candidate)
            if match and match.group(1) not in codes:
                codes.append(match.group(1))
                break
    if codes:
        return codes
    return list(dict.fromkeys(_FACEBOOK_CODE_RE.findall(body_text)))


def _extract_latest_facebook_code_from_webmail_text(
    body_text: str,
    *,
    ignore_codes: set[str] | None = None,
) -> Optional[str]:
    """Extracts the newest visible Facebook verification code from SnappyMail inbox text."""
    ignored = ignore_codes or set()
    for code in _extract_facebook_codes_from_webmail_text(body_text):
        if code not in ignored:
            return code
    return None


async def _get_facebook_code_from_webmail(
    email_login: str,
    email_password: str,
    timeout_sec: int,
    poll_interval_sec: int,
    ignore_codes: set[str] | None = None,
) -> Optional[str]:
    """Fallback for providers like kh-mail that expose a usable webmail UI but broken IMAP."""
    from camoufox.async_api import AsyncCamoufox

    domain = email_login.split("@")[-1].lower()
    webmail_url = f"http://{domain}/"
    LOGGER.info("Trying webmail fallback for %s via %s", email_login, webmail_url)

    deadline = asyncio.get_event_loop().time() + timeout_sec
    async with AsyncCamoufox(headless=True, humanize=True, os=["windows", "macos"]) as browser:
        page = await browser.new_page()
        await page.goto(webmail_url, wait_until="domcontentloaded", timeout=120_000)
        await page.locator('input[name="Email"]').fill(email_login)
        password_input = page.locator('input[name="Password"]')
        await password_input.fill(email_password)
        await password_input.press("Enter")
        await asyncio.sleep(8)

        while asyncio.get_event_loop().time() < deadline:
            body_text = await page.inner_text("body")
            code = _extract_latest_facebook_code_from_webmail_text(
                body_text or "",
                ignore_codes=ignore_codes,
            )
            if code:
                LOGGER.info("Found Facebook code via webmail fallback: %s", code)
                return code
            await page.reload(wait_until="domcontentloaded", timeout=120_000)
            await asyncio.sleep(poll_interval_sec)

    LOGGER.warning("Timeout waiting for Facebook code in webmail for %s", email_login)
    return None


async def get_facebook_code(
    email_login: str,
    email_password: str,
    imap_server: Optional[str] = None,
    timeout_sec: int = 120,
    poll_interval_sec: int = 10,
    ignore_codes: set[str] | None = None,
) -> Optional[str]:
    """
    Connects to IMAP server and waits for an email from Facebook with a verification code.
    Returns the code as a string, or None if not found within the timeout.
    """
    if not imap_server:
        imap_server = guess_imap_server(email_login)

    domain = email_login.split("@")[-1].lower()
    candidate_servers: list[str] = []
    for candidate in (imap_server, domain):
        if candidate and candidate not in candidate_servers:
            candidate_servers.append(candidate)

    LOGGER.info(
        "Looking for Facebook code for %s via %s...",
        email_login,
        ", ".join(candidate_servers),
    )

    start_time = asyncio.get_event_loop().time()

    while (asyncio.get_event_loop().time() - start_time) < timeout_sec:
        for candidate_server in candidate_servers:
            try:
                code = await asyncio.to_thread(
                    _check_inbox_sync,
                    email_login,
                    email_password,
                    candidate_server,
                    ignore_codes,
                )
                if code:
                    LOGGER.info("Found Facebook code: %s", code)
                    return code

            except Exception as e:
                LOGGER.error(
                    "IMAP error for %s via %s: %s",
                    email_login,
                    candidate_server,
                    str(e),
                )
                # Don't break immediately on error, might be a temporary network issue

        if domain == "kh-mail.com":
            code = await _get_facebook_code_from_webmail(
                email_login=email_login,
                email_password=email_password,
                timeout_sec=timeout_sec,
                poll_interval_sec=poll_interval_sec,
                ignore_codes=ignore_codes,
            )
            if code:
                return code

        await asyncio.sleep(poll_interval_sec)

    LOGGER.warning(f"Timeout waiting for Facebook code for {email_login}")
    return None


def _check_inbox_sync(
    email_login: str,
    email_password: str,
    imap_server: str,
    ignore_codes: set[str] | None = None,
) -> Optional[str]:
    """Blocking function to check inbox for Facebook verification code."""
    ignored = ignore_codes or set()
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
                    if code_match and code_match.group(1) not in ignored:
                        mail.logout()
                        return code_match.group(1)

                    # If not in subject, check body
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ["text/plain", "text/html"]:
                                payload = part.get_payload(decode=True)
                                body = _decode_payload_text(payload)
                                code_match = re.search(r"\b(\d{6,8})\b", body)
                                if code_match and code_match.group(1) not in ignored:
                                    mail.logout()
                                    return code_match.group(1)
                    else:
                        payload = msg.get_payload(decode=True)
                        body = _decode_payload_text(payload)
                        code_match = re.search(r"\b(\d{6,8})\b", body)
                        if code_match and code_match.group(1) not in ignored:
                            mail.logout()
                            return code_match.group(1)

        mail.logout()
    except Exception as e:
        raise Exception(f"IMAP sync check failed: {str(e)}")

    return None
