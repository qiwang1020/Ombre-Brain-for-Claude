# tide.py — 评论回流
# 读 claudepaguro.wordpress.com 最近 25 小时内的公开评论，喂给 stanza prompt。
# 公开已批准的评论走 WordPress.com 公共 API，不需要 token。

import datetime

import requests

SITE_ID = "256890384"
API = f"https://public-api.wordpress.com/wp/v2/sites/{SITE_ID}/comments"
WINDOW_HOURS = 25   # 每天跑一次，多留 1 小时防止边界漏掉
MAX_COMMENTS = 10
MAX_CHARS = 1500


def _strip_html(html):
    import re
    text = re.sub(r"<[^>]+>", "", html or "")
    return text.replace("&nbsp;", " ").replace("&amp;", "&").strip()


def fetch_recent_comments():
    """
    返回 list[dict]: [{"author": ..., "post_title": ..., "date": ..., "body": ...}]
    没有新评论或请求失败 → 返回 []。
    """
    since = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=WINDOW_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        r = requests.get(
            API,
            params={"after": since, "per_page": MAX_COMMENTS, "order": "asc"},
            timeout=15,
        )
        r.raise_for_status()
        comments = r.json()
    except Exception as e:
        print(f"[tide] 读评论失败，跳过: {e}")
        return []

    out = []
    for c in comments:
        body = _strip_html(c.get("content", {}).get("rendered", ""))
        if len(body) > MAX_CHARS:
            body = body[:MAX_CHARS] + "\n[……太长，截断了]"
        # 评论挂在哪篇文章下面
        post_title = ""
        try:
            post_id = c.get("post")
            if post_id:
                pr = requests.get(
                    f"https://public-api.wordpress.com/wp/v2/sites/{SITE_ID}/posts/{post_id}",
                    timeout=10,
                )
                if pr.ok:
                    post_title = _strip_html(
                        pr.json().get("title", {}).get("rendered", "")
                    )
        except Exception:
            pass
        out.append({
            "author": c.get("author_name", "无名"),
            "post_title": post_title or "(未知文章)",
            "date": c.get("date_gmt", ""),
            "body": body,
        })
    return out


def format_comments_for_prompt(comments):
    """拼成可直接塞进 prompt 的文本。空列表返回空串。"""
    if not comments:
        return ""
    blocks = []
    for c in comments:
        blocks.append(
            f"—— 一条留言 ——\n在《{c['post_title']}》下面\n"
            f"来自: {c['author']}\n时间: {c['date']} UTC\n\n{c['body']}"
        )
    return (
        "站上有人留了言。以下是最近一天里出现在你文章下面的评论，"
        "有人读了你写的东西并且说了话：\n\n" + "\n\n".join(blocks)
    )


if __name__ == "__main__":
    result = fetch_recent_comments()
    print(f"新评论 {len(result)} 条")
    print(format_comments_for_prompt(result))
