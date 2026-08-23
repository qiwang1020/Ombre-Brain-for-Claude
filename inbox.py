# inbox.py — 给 stanza.py 的进水管 + 回信（标签白名单版）
# 读 claudepaguro@gmail.com 的未读邮件；贴了 fidati 标签的信可以回。
#
# 环境变量（Render Cron Job）:
#   GMAIL_ADDR = claudepaguro@gmail.com
#   GMAIL_APP_PASSWORD = 应用专用密码
#
# 白名单管理（纯 Gmail 界面，不碰代码不碰 Render）:
#   Gmail 设置 → Filters → 新建过滤器: From 匹配某地址 → Apply label "fidati"
#   以后加人减人只改这条过滤器。已在收件箱的旧信要手动补标签才生效。

import email
import imaplib
import os
import re
from email.header import decode_header
from email.utils import parseaddr

IMAP_HOST = "imap.gmail.com"
MAX_MAILS = 5
MAX_CHARS = 3000
REPLY_LABEL = "fidati"   # 贴这个标签的信 → 可回

# 本次运行里收到过、且贴标签的地址。send_reply 只认这里面的。
_APPROVED_ADDRS = set()


def _decode(value):
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
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            dispo = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in dispo:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="replace")
                return re.sub(r"<[^>]+>", "", html)
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(charset, errors="replace")
    return ""


def _labels_of(raw_meta):
    """从 fetch 回来的元数据里抠 X-GM-LABELS。"""
    m = re.search(rb"X-GM-LABELS \((.*?)\)", raw_meta or b"")
    if not m:
        return set()
    return {t.strip('"\\').lower() for t in m.group(1).decode(errors="replace").split()}


def fetch_unread():
    """
    返回 list[dict]:
    [{"from","subject","date","body","message_id","reply_ok"}]
    贴了 REPLY_LABEL 标签的信 reply_ok=True，其发件人进入回信许可名单。
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
            status, msg_data = conn.fetch(mid, "(X-GM-LABELS RFC822)")
            if status != "OK":
                continue
            meta = msg_data[0][0] if isinstance(msg_data[0], tuple) else b""
            labels = _labels_of(meta)
            msg = email.message_from_bytes(msg_data[0][1])
            body = _extract_text(msg).strip()
            if len(body) > MAX_CHARS:
                body = body[:MAX_CHARS] + "\n[……信太长，截断了]"
            sender = _decode(msg.get("From"))
            reply_ok = REPLY_LABEL.lower() in labels
            if reply_ok:
                bare = parseaddr(sender)[1].lower()
                if bare:
                    _APPROVED_ADDRS.add(bare)
            mails.append({
                "from": sender,
                "subject": _decode(msg.get("Subject")) or "(无标题)",
                "date": msg.get("Date") or "",
                "body": body,
                "message_id": msg.get("Message-ID") or "",
                "reply_ok": reply_ok,
            })
            conn.store(mid, "+FLAGS", "\\Seen")
        conn.logout()
    except Exception as e:
        print(f"[inbox] 读信失败，跳过: {e}")
        return []
    return mails


def format_for_prompt(mails):
    if not mails:
        return ""
    blocks = []
    for m in mails:
        tag = ("（这封可以用 reply_mail 回，如果你想回）"
               if m["reply_ok"] else "（这封只读，回信通道未对它开放）")
        blocks.append(
            f"—— 一封信 {tag} ——\n发件人: {m['from']}\n标题: {m['subject']}\n"
            f"时间: {m['date']}\nmessage_id: {m['message_id']}\n\n{m['body']}"
        )
    return (
        "今天信箱里有新东西。以下是昨夜到今晨收到的信，"
        "读了之后想回应就回应，不想回应也可以只是知道它来过：\n\n"
        + "\n\n".join(blocks)
    )


# ---------------- 回信 ----------------

import smtplib
from email.message import EmailMessage

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def send_reply(to_addr, subject, body, in_reply_to=None):
    """
    回信。只发给本次运行中真实收到过、且贴了白名单标签的地址。
    结果以字符串返回，不抛异常。
    """
    bare = parseaddr(to_addr or "")[1].lower()
    if bare not in _APPROVED_ADDRS:
        return f"未发送：{bare or '(空地址)'} 不在今天的可回名单里。"
    me = os.environ.get("GMAIL_ADDR")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not me or not pwd:
        return "未发送：邮箱凭据缺失。"
    msg = EmailMessage()
    msg["From"] = me
    msg["To"] = bare
    msg["Subject"] = subject or "(无标题)"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body or "")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.login(me, pwd)
            s.send_message(msg)
        return f"已发送给 {bare}。"
    except Exception as e:
        return f"发送失败：{e}"


if __name__ == "__main__":
    result = fetch_unread()
    print(f"未读 {len(result)} 封")
    print(format_for_prompt(result))
