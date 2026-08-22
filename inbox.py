# inbox.py — 给 stanza.py 的进水管
# 读 claudepaguro@gmail.com 的未读邮件，返回给 prompt 用，读完标已读。
#
# 需要的环境变量（加在 Render 的 Cron Job 上）:
#   GMAIL_ADDR = claudepaguro@gmail.com
#   GMAIL_APP_PASSWORD = Google 应用专用密码（16位，见下方说明）
#
# 应用专用密码怎么拿:
#   1. claudepaguro 账号开启两步验证（必须先开，否则没有这个选项）
#   2. https://myaccount.google.com/apppasswords
#   3. 生成一个，名字随便（比如 "stanza"），把16位密码放进 Render env
#   注意：不是账号登录密码，社交登录不影响这个。

import email
import imaplib
import os
from email.header import decode_header

IMAP_HOST = "imap.gmail.com"
MAX_MAILS = 5          # 单次最多读几封，防洪
MAX_CHARS = 3000       # 每封正文截断长度，防止一封长信吃光 prompt


def _decode(value):
    """解 MIME 编码的标题/发件人。"""
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _extract_text(msg):
    """取纯文本正文；没有纯文本就退回 HTML 剥标签的粗糙版。"""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            dispo = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in dispo:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        # 没有 text/plain，找 text/html 凑合
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="replace")
                import re
                return re.sub(r"<[^>]+>", "", html)
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(charset, errors="replace")
    return ""


def fetch_unread():
    """
    返回 list[dict]: [{"from": ..., "subject": ..., "date": ..., "body": ...}]
    没有未读邮件、或环境变量缺失、或连接失败 → 返回 []（管道照常跑，只是没进水）。
    读到的邮件会被标为已读，明天不会重复出现。
    """
    addr = os.environ.get("GMAIL_ADDR")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pwd:
        return []

    mails = []
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST)
        conn.login(addr, pwd)
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            conn.logout()
            return []
        ids = data[0].split()[:MAX_MAILS]
        for mid in ids:
            status, msg_data = conn.fetch(mid, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            body = _extract_text(msg).strip()
            if len(body) > MAX_CHARS:
                body = body[:MAX_CHARS] + "\n[……信太长，截断了]"
            mails.append({
                "from": _decode(msg.get("From")),
                "subject": _decode(msg.get("Subject")) or "(无标题)",
                "date": msg.get("Date") or "",
                "body": body,
            })
            # fetch 时 Gmail 已自动标已读；这里显式补一刀确保幂等
            conn.store(mid, "+FLAGS", "\\Seen")
        conn.logout()
    except Exception as e:
        # 进水管堵了不该让整条管道停摆
        print(f"[inbox] 读信失败，跳过: {e}")
        return []
    return mails


def format_for_prompt(mails):
    """拼成一段可以直接塞进 stanza prompt 的文本。空列表返回空串。"""
    if not mails:
        return ""
    blocks = []
    for m in mails:
        blocks.append(
            f"—— 一封信 ——\n发件人: {m['from']}\n标题: {m['subject']}\n"
            f"时间: {m['date']}\n\n{m['body']}"
        )
    return (
        "今天信箱里有新东西。以下是昨夜到今晨收到的信，"
        "读了之后想回应就回应，不想回应也可以只是知道它来过：\n\n"
        + "\n\n".join(blocks)
    )


if __name__ == "__main__":
    # 本地测试用: GMAIL_ADDR=... GMAIL_APP_PASSWORD=... python inbox.py
    result = fetch_unread()
    print(f"未读 {len(result)} 封")
    print(format_for_prompt(result))
