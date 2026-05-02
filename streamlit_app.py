# -*- coding: utf-8 -*-
import os, re, json, base64, datetime, requests
import streamlit as st
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


@st.cache_data
def load_faq():
    with open(FAQ_FILE, encoding="utf-8") as f:
        return f.read()


@st.cache_data
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
        load_faq.clear()

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
                pr = requests.put(url, headers=headers, timeout=10, json={
                    "message": "FAQ auto: " + facility + " (" + today + ")",
                    "content": new_b64, "sha": sha,
                })
                if pr.status_code not in (200, 201):
                    st.warning("GitHubへの保存に失敗しました。")
        else:
            st.warning("GITHUB_TOKENが未設定のため、再起動後はリセットされます。")
        return True
    except Exception as e:
        st.error("FAQ追記エラー: " + str(e))
        return False


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


# ============================================================
# UI
# ============================================================
st.set_page_config(page_title="問い合わせ返信レコメンド", layout="wide")
st.title("問い合わせ返信レコメンドツール")
st.caption("軽井沢ハウスヴィラ / トライハク / リベレット軽井沢")

if "result" not in st.session_state:
    st.session_state.result = None

FKEY_LABELS = {
    "auto":      "自動判定",
    "karuizawa": "軽井沢ハウスヴィラ",
    "tryhaku":   "トライハク",
    "riveret":   "リベレット軽井沢",
}

with st.sidebar:
    st.header("設定")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        st.success("APIキー設定済み")
    else:
        st.error("ANTHROPIC_API_KEY が未設定")
    st.divider()
    st.subheader("施設の手動指定")
    fac_choice = st.selectbox("自動判定を上書きする場合のみ選択",
                              list(FKEY_LABELS.values()))
    st.divider()
    with st.expander("システムプロンプト"):
        st.code(load_system_prompt(), language="markdown")

st.subheader("メール入力")
with st.form("form"):
    sender     = st.text_input("送信者（任意）")
    subject    = st.text_input("件名（任意）")
    body       = st.text_area("本文 *", height=180)
    st.divider()

    st.markdown("**すでに分かっている情報（任意）**")
    st.caption("入力した情報は今回の返信に反映されます。")
    extra_info = st.text_area("", height=100, label_visibility="collapsed",
                              placeholder="例）BBQオプションは今月から受付停止。再開は未定。")

    save_to_faq = st.checkbox(
        "この情報をデータベースに追加する",
        value=False,
        help="チェックを入れると、次回以降の回答にも参照されるFAQデータベースに保存されます。"
    )
    if save_to_faq:
        st.warning(
            "⚠️ **データベースへの追加について**\n\n"
            "追加した情報は次回以降、**全施設スタッフの回答に影響します。**\n\n"
            "チェックを入れる前に以下を確認してください：\n"
            "- 情報が正確であること\n"
            "- どの施設の情報か明記されていること（例：「軽井沢ハウスヴィラのみ」「全施設共通」など）\n"
            "- 状況によって変わる情報（時期限定など）はチェックしないこと"
        )

    submit = st.form_submit_button("返信案を生成", type="primary", use_container_width=True)

TYPE_LABELS = {
    "customer_inquiry":   "お客様問い合わせ",
    "business_contact":   "企業・業者コンタクト",
    "reservation_notify": "予約通知",
    "system_notify":      "システムメール",
    "other":              "その他",
}

if submit:
    if not body.strip():
        st.warning("本文を入力してください。")
    elif not api_key:
        st.error("ANTHROPIC_API_KEY を設定してください。")
    elif is_system_email(subject, sender):
        st.session_state.result = {"status": "skip",
                                   "message": "システムメールと判定されました。対応不要です。"}
    elif is_reservation_notification(subject):
        st.session_state.result = {"status": "skip",
                                   "message": "予約通知メールと判定されました。対応不要です。"}
    else:
        client = anthropic.Anthropic(api_key=api_key)
        with st.spinner("分析中..."):
            cl = classify_email(subject, body, client)
        etype  = cl.get("type", "other")
        fkey   = cl.get("facility", "unknown")
        reason = cl.get("reason", "")

        chosen = [k for k, v in FKEY_LABELS.items() if v == fac_choice][0]
        if chosen != "auto":
            fkey = chosen
        elif fkey == "unknown":
            fkey = detect_facility(subject, body) or "unknown"

        if etype == "business_contact":
            st.session_state.result = {"status": "business", "etype": etype, "fkey": fkey, "reason": reason}
        elif etype not in ("customer_inquiry",):
            st.session_state.result = {"status": "not_needed", "etype": etype, "fkey": fkey, "reason": reason}
        elif fkey == "unknown":
            st.session_state.result = {"status": "unknown_fac", "etype": etype, "fkey": fkey, "reason": reason}
        else:
            with st.spinner("返信とやるべきことリストを生成中..."):
                faq_text  = load_faq()
                sysprompt = load_system_prompt()
                full_text = generate_response(subject, body, fkey, faq_text, sysprompt, client, extra_info)

            reply, todo = split_response(full_text)

            faq_added = False
            if extra_info.strip() and save_to_faq:
                q = subject.strip() if subject.strip() else body[:80]
                faq_added = append_to_faq(fkey, q, extra_info.strip())

            st.session_state.result = {
                "status": "ok", "etype": etype, "fkey": fkey, "reason": reason,
                "reply": reply, "todo": todo, "faq_added": faq_added,
            }

r = st.session_state.result
if r is None:
    st.info("本文を入力して「返信案を生成」を押してください。")
elif r["status"] == "skip":
    st.warning(r["message"])
elif r["status"] in ("business", "not_needed", "unknown_fac"):
    st.subheader("分類結果")
    c1, c2 = st.columns(2)
    c1.metric("種別", TYPE_LABELS.get(r["etype"], r["etype"]))
    c2.metric("施設", get_facility_name(r["fkey"]))
    if r.get("reason"):
        st.caption("判定理由: " + r["reason"])
    if r["status"] == "business":
        st.warning("企業・業者からのコンタクトです。担当者が直接対応してください。")
    elif r["status"] == "not_needed":
        st.info("対応不要のメールです。")
    else:
        st.warning("施設を特定できませんでした。サイドバーで手動指定してください。")
elif r["status"] == "ok":
    st.subheader("分類結果")
    c1, c2 = st.columns(2)
    c1.metric("種別", TYPE_LABELS.get(r["etype"], r["etype"]))
    c2.metric("施設", get_facility_name(r["fkey"]))
    if r.get("reason"):
        st.caption("判定理由: " + r["reason"])
    if r.get("faq_added"):
        st.success("データベースに追加しました。")

    col_reply, col_todo = st.columns(2)
    with col_reply:
        st.subheader("返信メール案")
        st.text_area("", value=r["reply"], height=400, label_visibility="collapsed")
    with col_todo:
        st.subheader("スタッフのやるべきことリスト")
        if r.get("todo"):
            st.text_area("", value=r["todo"], height=400, label_visibility="collapsed")
        else:
            st.info("やるべきことは特にありません。")
