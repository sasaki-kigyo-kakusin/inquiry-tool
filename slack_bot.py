# -*- coding: utf-8 -*-
import os, re, json, requests, base64, datetime, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic

CLAUDE_MODEL = "claude-opus-4-6"
FAQ_FILE     = os.path.join(os.path.dirname(__file__), "faq_manual.txt")
PROMPT_FILE  = os.path.join(os.path.dirname(__file__), "chat_prompt.txt")

FACILITY_URLS = {
    "karuizawa": "https://karuizawa-house-villa.com/",
    "tryhaku":   "https://www.tryhaku.jp/",
    "riveret":   "https://www.booking.com/hotel/jp/riveret-karuizawa.ja.html",
}
FACILITY_NAMES = {
    "karuizawa": "軽井沢ハウスヴィラ",
    "tryhaku":   "トライハク",
    "riveret":   "リベレット軽井沢",
}
SYSTEM_KEYWORDS = [
    "ログインコード", "login code", "週間レポート", "ウィークリーレポート",
    "新しいデバイスのログイン", "PayPay 週間", "Money Forward", "Airhost One", "[Airhost One]",
]
RESERVATION_KEYWORDS = [
    "新規.A00", "キャンセル.A00", "予約受付", "予約取消", "予約確定",
    "予約キャンセル", "事前決済ズミ", "事前決済済", "OTAの新規受注",
    "予約確認メール", "[新規.", "[キャンセル.",
]
FACILITY_KEYWORDS = {
    "karuizawa": ["ハウスヴィラ", "house villa", "A棟", "B棟", "別邸", "サウナ棟", "檜風呂"],
    "tryhaku":   ["トライハク", "trihaku"],
    "riveret":   ["リベレット", "riveret", "RIVERET"],
}


def load_faq():
    with open(FAQ_FILE, encoding="utf-8") as f:
        return f.read()


def load_system_prompt():
    with open(PROMPT_FILE, encoding="utf-8") as f:
        text = f.read()
    idx = text.find("## FAQ")
    if idx >= 0:
        text = text[:idx].rstrip()
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def get_facility_name(key):
    return FACILITY_NAMES.get(key, key)


def is_system_email(subject, sender=""):
    c = (subject + " " + sender).lower()
    return any(k.lower() in c for k in SYSTEM_KEYWORDS)


def is_reservation_notification(subject):
    return any(k in subject for k in RESERVATION_KEYWORDS)


def detect_facility(subject, body):
    c = (subject + " " + body).lower()
    for key, kws in FACILITY_KEYWORDS.items():
        if any(k.lower() in c for k in kws):
            return key
    return None


def classify_email(subject, body, client):
    prompt = (
        "以下のメールを分析してJSON形式のみで回答してください。\n\n"
        "分類:\n"
        "- customer_inquiry: お客様からの問い合わせ・質問・要望\n"
        "- business_contact: OTA・業者・メディアからの連絡\n"
        "- reservation_notify: 予約・キャンセル・決済の自動通知\n"
        "- system_notify: ログインコード・週次レポート等\n"
        "- other: 広告・ニュースレター等\n\n"
        "施設:\n"
        "- karuizawa: ハウスヴィラ・A棟・B棟・別邸・サウナ棟・檜風呂棟\n"
        "- tryhaku: トライハク\n"
        "- riveret: リベレット\n"
        "- unknown: 不明\n\n"
        "件名: " + subject + "\n"
        "本文: " + body[:400] + "\n\n"
        '{"type":"...","facility":"...","reason":"..."}'
    )
    res = client.messages.create(model=CLAUDE_MODEL, max_tokens=200,
                                 messages=[{"role": "user", "content": prompt}])
    raw = res.content[0].text.strip()
    m = re.search(r'\{[\s\S]+\}', raw)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {"type": "other", "facility": "unknown", "reason": "failed"}


def generate_response(subject, body, facility_key, faq_text, system_prompt, client, extra_info=""):
    facility = get_facility_name(facility_key)
    furl     = FACILITY_URLS.get(facility_key, "")
    extra    = ""
    if extra_info.strip():
        extra = (
            "\n[すでに分かっている情報]\n"
            + extra_info + "\n"
            "※上記の情報はFAQより優先して返信に反映してください。\n"
        )
    msg = (
        "[対象施設]" + facility + "\n"
        "[公式HP]" + furl + "\n\n"
        "[問い合わせ件名]\n" + subject + "\n\n"
        "[問い合わせ本文]\n" + body + "\n"
        + extra + "\n"
        "[社内FAQ・マニュアル]\n" + faq_text + "\n\n"
        "上記の情報をもとに、以下の2つを作成してください。\n\n"
        "## お客様への返信メール\n"
        "件名と本文を作成してください。\n\n"
        "## スタッフのやるべきことリスト\n"
        "この問い合わせを受けてスタッフが取るべき具体的なアクションを番号付きリストで作成してください。"
        "FAQの【やるべきこと】欄を参考にしてください。\n\n"
        "出力形式:\n"
        "===返信メール===\n"
        "（件名と本文）\n\n"
        "===やるべきことリスト===\n"
        "（番号付きアクション）\n"
    )
    res = client.messages.create(model=CLAUDE_MODEL, max_tokens=1500,
                                 system=system_prompt,
                                 messages=[{"role": "user", "content": msg}])
    return res.content[0].text.strip()


def split_response(text):
    reply_part = ""
    todo_part  = ""
    if "===やるべきことリスト===" in text:
        parts      = text.split("===やるべきことリスト===")
        reply_part = parts[0].replace("===返信メール===", "").strip()
        todo_part  = parts[1].strip() if len(parts) > 1 else ""
    else:
        reply_part = text
    return reply_part, todo_part


def append_to_faq(facility_key, question, answer):
    try:
        facility = get_facility_name(facility_key)
        today = datetime.date.today().strftime("%Y-%m-%d")
        entry = (
            "\n【施設】" + facility + "\n"
            "【カテゴリ】確認済み対応（" + today + "）\n"
            "【質問】" + question + "\n"
            "【返信内容】" + answer + "\n"
            "---\n"
        )
        with open(FAQ_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        github_token = os.environ.get("GITHUB_TOKEN", "")
        github_repo  = os.environ.get("GITHUB_REPO", "sasaki-kigyo-kakusin/inquiry-tool")
        if github_token:
            url     = "https://api.github.com/repos/" + github_repo + "/contents/faq_manual.txt"
            headers = {"Authorization": "token " + github_token}
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                fd      = res.json()
                cur_txt = base64.b64decode(fd["content"]).decode("utf-8")
                sha     = fd["sha"]
                new_b64 = base64.b64encode((cur_txt + entry).encode("utf-8")).decode("utf-8")
                requests.put(url, headers=headers, timeout=10, json={
                    "message": "FAQ auto: " + facility + " (" + today + ")",
                    "content": new_b64, "sha": sha,
                })
        return True
    except Exception:
        return False


def parse_slack_message(text):
    text = re.sub(r'<@[A-Z0-9]+>', '', text).strip()
    subject    = ""
    sender     = ""
    fac_manual = None
    lines = text.split('\n')
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^件名[:：]', stripped):
            subject    = re.sub(r'^件名[:：]\s*', '', stripped)
            body_start = i + 1
        elif re.match(r'^送信者[:：]', stripped):
            sender     = re.sub(r'^送信者[:：]\s*', '', stripped)
            body_start = i + 1
        elif re.match(r'^施設[:：]', stripped):
            fac_manual = re.sub(r'^施設[:：]\s*', '', stripped).strip()
            body_start = i + 1
        elif stripped == '---':
            body_start = i + 1
            break
    body_text = '\n'.join(lines[body_start:]).strip()
    extra_info = ""
    if '【補足】' in body_text:
        parts      = body_text.split('【補足】', 1)
        body_text  = parts[0].strip()
        extra_info = parts[1].strip()
    return subject, sender, fac_manual, body_text, extra_info


HELP_TEXT = (
    "*📬 問い合わせ返信レコメンドBOT の使い方*\n\n"
    "メール内容をそのまま貼り付けるだけでOKです。\n\n"
    "```\n"
    "件名: チェックアウト時間について\n"
    "---\n"
    "（メール本文をここに貼り付け）\n\n"
    "【補足】すでに分かっている情報（FAQに自動保存されます）\n"
    "```\n\n"
    "*施設コード:*  `karuizawa` / `tryhaku` / `riveret`\n"
    "`ヘルプ` と送るとこの説明を表示します。"
)


def process_inquiry(text, say, thread_ts=None):
    text = text.strip()
    if not text:
        say(text=HELP_TEXT, thread_ts=thread_ts)
        return
    if text.lower() in ("ヘルプ", "help", "使い方", "?", "？"):
        say(text=HELP_TEXT, thread_ts=thread_ts)
        return

    subject, sender, fac_manual, body, extra_info = parse_slack_message(text)
    if not body:
        say(text="⚠️ メール本文が見つかりませんでした。`ヘルプ` で使い方を確認できます。", thread_ts=thread_ts)
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        say(text="⚠️ ANTHROPIC_API_KEY が未設定です。", thread_ts=thread_ts)
        return

    if is_system_email(subject, sender):
        say(text="ℹ️ システムメールと判定されました。対応不要です。", thread_ts=thread_ts)
        return
    if is_reservation_notification(subject):
        say(text="ℹ️ 予約通知メールと判定されました。対応不要です。", thread_ts=thread_ts)
        return

    say(text="⏳ 分析中...", thread_ts=thread_ts)

    client = anthropic.Anthropic(api_key=api_key)
    cl     = classify_email(subject, body, client)
    etype  = cl.get("type", "other")
    fkey   = cl.get("facility", "unknown")
    reason = cl.get("reason", "")

    if fac_manual and fac_manual in FACILITY_NAMES:
        fkey = fac_manual
    elif fkey == "unknown":
        fkey = detect_facility(subject, body) or "unknown"

    TYPE_LABELS = {
        "customer_inquiry":   "お客様問い合わせ",
        "business_contact":   "企業・業者コンタクト",
        "reservation_notify": "予約通知",
        "system_notify":      "システムメール",
        "other":              "その他",
    }
    type_label = TYPE_LABELS.get(etype, etype)
    fac_label  = get_facility_name(fkey)

    if etype == "business_contact":
        say(text=f"📋 *分類:* {type_label}　|　*施設:* {fac_label}\n⚠️ 企業・業者からのコンタクトです。担当者が直接対応してください。", thread_ts=thread_ts)
        return
    elif etype not in ("customer_inquiry",):
        say(text=f"📋 *分類:* {type_label}　|　*施設:* {fac_label}\nℹ️ 対応不要のメールです。", thread_ts=thread_ts)
        return
    elif fkey == "unknown":
        say(text=f"📋 *分類:* {type_label}\n⚠️ 施設を特定できませんでした。\nメッセージ先頭に `施設: karuizawa`（または `tryhaku` / `riveret`）を追加して再送してください。", thread_ts=thread_ts)
        return

    faq_text  = load_faq()
    sysprompt = load_system_prompt()
    full_text = generate_response(subject, body, fkey, faq_text, sysprompt, client, extra_info)
    reply, todo = split_response(full_text)

    faq_added = False
    if extra_info.strip():
        q = subject.strip() if subject.strip() else body[:80]
        faq_added = append_to_faq(fkey, q, extra_info.strip())

    header = f"📋 *分類:* {type_label}　|　*施設:* {fac_label}"
    if reason:
        header += f"\n_判定理由: {reason}_"
    if faq_added:
        header += "\n✅ 補足情報をデータベースに追加しました。"

    reply_block = f"*📧 返信メール案*\n```\n{reply}\n```"
    todo_block  = f"\n\n*✅ スタッフのやるべきことリスト*\n{todo}" if todo else ""

    say(text=header + "\n\n" + reply_block + todo_block, thread_ts=thread_ts)


# ── Slack イベントハンドラ ──
app = App(token=os.environ.get("SLACK_BOT_TOKEN", ""))


@app.event("app_mention")
def handle_mention(event, say):
    process_inquiry(event.get("text", ""), say, thread_ts=event.get("ts"))


@app.event("message")
def handle_dm(event, say):
    if event.get("channel_type") == "im" and not event.get("subtype"):
        process_inquiry(event.get("text", ""), say, thread_ts=event.get("ts"))


# ── Render.com Web Service 用ヘルスチェックサーバー ──
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


if __name__ == "__main__":
    # ヘルスチェックサーバーをバックグラウンドで起動
    threading.Thread(target=start_health_server, daemon=True).start()
    print("Health check server started.")
    # Slack Bot 起動
    print("Slack Bot starting...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
