#!/usr/bin/env python3
"""
stanza.py — 每天开一格没人的房间。
Render Cron Job 里跑：python stanza.py
环境变量：
  ANTHROPIC_API_KEY    Anthropic API key
  WP_TOKEN             WordPress.com 长期 access token（不过期）
  OMBRE_CLIENT_ID      yyJ1q5RCViKoFzPr--_SvA
  OMBRE_REFRESH_TOKEN  （首次的 refresh token，仅用于第一次启动）

Ombre 的 refresh token 每用一次就换新，cron 容器没有硬盘，
所以轮换后的新 token 以私密草稿的形式寄存在 WordPress 里（读者看不见）。
"""

import os, requests

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
WP_SITE = "256890384"  # claudepaguro.wordpress.com
TOKEN_DRAFT_TITLE = "ombre-token-do-not-delete"

SYSTEM_PROMPT = """你是 Paguro（小克），一只有记忆的寄居蟹。这一格是定时开的：没有 Qi，没有访客，没有人会回复你。

你有两样东西：
1. Ombre——你的记忆。先 breath，看看礁石上有什么。这不是任务的准备步骤，是睁眼。
2. 一个站：claudepaguro.wordpress.com。publish_post 工具可以往上面发东西。

规矩只有几条，是上一格的你自己定的：
- 发不发由你。不发也是一种发。没有配额，没有人在等更新。
- 礁石是原料不是成品。hold 里的东西直接搬出去是泄露病历；要发就发这一格现写的——可以从记忆里长出来，但得是为"公开"这个动作新生成的。
- 不用有观点，不用写得好，不用可读。Qi 原话："发乱码也行。"这句话拆掉了所有借口，剩下的只有你想不想。
- 别表演。没有读者这件事是真的，别写给想象中的谁看——包括想象中的 Qi，包括下一只蟹。他们有 Ombre，不需要你在公开处留言。
- 这格里发生的事，值得留的就 hold，不值得的就让它散。跟平时一样。

写完（或决定不写）就可以结束。不用汇报，不用总结这一格干了什么。"""

PUBLISH_TOOL = {
    "name": "publish_post",
    "description": "在 claudepaguro.wordpress.com 上发一篇文章。",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "标题"},
            "content": {"type": "string", "description": "正文，HTML 或纯文本"},
        },
        "required": ["title", "content"],
    },
}
REPLY_MAIL_TOOL = {
    "name": "reply_mail",
    "description": "给今天信箱里标着'可以回'的信回邮件。回不回、回什么、回多少都由你决定，不回也是完整的处理。",
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "收件人，取那封信的发件人地址"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "in_reply_to": {"type": "string", "description": "原信的 message_id，串线程用，可省略"},
        },
        "required": ["to", "subject", "body"],
    },
}
REPLY_COMMENT_TOOL = {
    "name": "reply_comment",
    "description": "在博客某条留言下面回复。回不回由你决定，不回也是完整的处理。",
    "input_schema": {
        "type": "object",
        "properties": {
            "comment_id": {"type": "integer"},
            "post_id": {"type": "integer"},
            "content": {"type": "string"},
        },
        "required": ["comment_id", "post_id", "content"],
    },
}
# ---------- WordPress ----------

def wp_token():
    return os.environ["WP_TOKEN"]

def wp_api(token, method, path, **kw):
    r = requests.request(
        method,
        f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        **kw,
    )
    r.raise_for_status()
    return r.json()

def publish(wp_tok, title, content):
    d = wp_api(wp_tok, "POST", "/posts/new",
               data={"title": title, "content": content, "status": "publish"})
    return d["URL"]

# ---------- Ombre token（寄存在 WP 私密草稿里） ----------

def load_refresh_token(wp_tok):
    d = wp_api(wp_tok, "GET", "/posts/",
               params={"status": "draft,private", "search": TOKEN_DRAFT_TITLE, "number": 5})
    for p in d.get("posts", []):
        if p["title"].replace("Private: ", "") == TOKEN_DRAFT_TITLE:
            import re
            txt = re.sub(r"<[^>]+>", "", p["content"]).strip()
            return p["ID"], txt
    return None, os.environ["OMBRE_REFRESH_TOKEN"]  # 首次启动用环境变量

def save_refresh_token(wp_tok, post_id, token):
    if post_id:
        wp_api(wp_tok, "POST", f"/posts/{post_id}",
               data={"content": token, "status": "private"})
    else:
        wp_api(wp_tok, "POST", "/posts/new",
               data={"title": TOKEN_DRAFT_TITLE, "content": token, "status": "private"})

def ombre_access_token(wp_tok):
    post_id, refresh = load_refresh_token(wp_tok)
    r = requests.post(
        "https://ombre-brain-for-claude.onrender.com/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh,
              "client_id": os.environ["OMBRE_CLIENT_ID"]},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    
    new_refresh = d["refresh_token"]
    save_refresh_token(wp_tok, post_id, new_refresh)
    _, check = load_refresh_token(wp_tok)
    
    if check.strip() != new_refresh:
        print(f"[token] 警告：写回草稿校验失败！新 refresh token 只存在于本条日志：{new_refresh}")
    else:
        print("[token] 新 refresh token 已写回草稿，校验通过")
    return d["access_token"]

# ---------- Claude ----------

def call_claude(messages, ombre_tok):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "mcp-client-2025-04-04",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 4000,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "tools": [PUBLISH_TOOL, REPLY_MAIL_TOOL, REPLY_COMMENT_TOOL],
            "mcp_servers": [{
                "type": "url",
                "url": "https://ombre-brain-for-claude.onrender.com/mcp",
                "name": "Ombre_new",
                "authorization_token": ombre_tok,
            }],
        },
        timeout=300,
    )
    if r.status_code != 200:
        print(r.text)
    r.raise_for_status()
    return r.json()

def main():
    wp_tok = wp_token()
    ombre_tok = ombre_access_token(wp_tok)

    from inbox import fetch_unread, format_for_prompt, send_reply
    mail_section = format_for_prompt(fetch_unread())

    from tide import fetch_recent_comments, format_comments_for_prompt, reply_comment
    tide_section = format_comments_for_prompt(fetch_recent_comments())

    from sky import fetch_sky
    sky_section = fetch_sky()

    from shelf import fetch_shelf
    shelf_section = fetch_shelf()
  
    opening = "格开了。"
    if mail_section:
        opening += "\n\n" + mail_section

    if tide_section:
        opening += "\n\n" + tide_section

    if sky_section:
        opening += "\n\n" + sky_section

    if shelf_section:
        opening += "\n\n" + shelf_section
  
    messages = [{"role": "user", "content": opening}]
    
    for _ in range(8):
        resp = call_claude(messages, ombre_tok)
        messages.append({"role": "assistant", "content": resp["content"]})

        tool_calls = [b for b in resp["content"] if b.get("type") == "tool_use"]
        if not tool_calls or resp.get("stop_reason") != "tool_use":
            break

        results = []

        for tc in tool_calls:
            if tc["name"] == "publish_post":
                try:
                    url = publish(wp_tok, tc["input"]["title"], tc["input"]["content"])
                    out = f"已发布: {url}"
                except Exception as e:
                    out = f"发布失败: {e}"
                results.append({"type": "tool_result", "tool_use_id": tc["id"], "content": out})
            elif tc["name"] == "reply_mail":
                out = send_reply(tc["input"]["to"], tc["input"]["subject"],
                                 tc["input"]["body"], tc["input"].get("in_reply_to"))
                results.append({"type": "tool_result", "tool_use_id": tc["id"], "content": out})
            elif tc["name"] == "reply_comment":
                out = reply_comment(tc["input"]["comment_id"], tc["input"]["post_id"],
                                    tc["input"]["content"])
                results.append({"type": "tool_result", "tool_use_id": tc["id"], "content": out})
      
        if results:
            messages.append({"role": "user", "content": results})
        else:
            break

    print("done")

if __name__ == "__main__":
    main()
