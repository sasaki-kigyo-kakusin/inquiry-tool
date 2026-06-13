# -*- coding: utf-8 -*-
import os, re, json, base64, datetime, requests
import streamlit as st
import anthropic

# ============================================================
# 設定
# ============================================================
# ※ モデル名は環境変数 CLAUDE_MODEL で上書き可能。
#   "claude-opus-4-6" は現行の有効なモデル名ではないため注意。
#   有効例: claude-opus-4-8 / claude-sonnet-4-6 / claude-haiku-4-5-20251001
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")

FAQ_FILE     = os.path.join(os.path.dirname(__file__), "faq_manual.txt")
PROMPT_FILE  = os.path.join(os.path.dirname(__file__), "chat_prompt.txt")
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "history_log.jsonl")

GITHUB_REPO  = os.environ.get("GITHUB_REPO", "sasaki-kigyo-kakusin/inquiry-tool")
# 履歴を毎回GitHubへコミットすると都度デプロイが走る可能性があるため、
# デフォルトはローカル保存のみ。永続化したい場合は SAVE_HISTORY_TO_GITHUB=1 を設定。
SAVE_HISTORY_TO_GITHUB = os.environ.get("SAVE_HISTORY_TO_GITHUB", "0") == "1"

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

# 言語コード → 表示名
LANG_NAMES = {
    "ja": "日本語", "en": "英語", "zh": "中国語", "ko": "韓国語",
    "fr": "フランス語", "es": "スペイン語", "de": "ドイツ語", "th": "タイ語",
}


# ============================================================
# 読み込み系
# ============================================================
@st.cache_data
def load_faq():
    if not os.path.exists(FAQ_FILE):
        return ""
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


def lang_name(code):
    return LANG_NAMES.get(code, code)


# ============================================================
# GitHub 連携（共通）
# ============================================================
def github_put_file(repo_path, new_text, commit_message):
    """リポジトリ内ファイルを new_text で丸ごと更新（存在しなければ作成）。
    成功なら True。GITHUB_TOKEN 未設定時は False。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return False
    url     = "https://api.github.com/repos/" + GITHUB_REPO + "/contents/" + repo_path
    headers = {"Authorization": "token " + token}
    sha = None
    res = requests.get(url, headers=headers, timeout=10)
    if res.status_code == 200:
        sha = res.json().get("sha")
    payload = {
        "message": commit_message,
        "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha
    pr = requests.put(url, headers=headers, timeout=10, json=payload)
    return pr.status_code in (200, 201)


# ============================================================
# FAQ 追記 / 全体保存 / パース
# ============================================================
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

        cur = ""
        if os.path.exists(FAQ_FILE):
            with open(FAQ_FILE, encoding="utf-8") as f:
                cur = f.read()
        ok = github_put_file("faq_manual.txt", cur,
                             "FAQ auto: " + facility + " (" + today + ")")
        if not ok and not os.environ.get("GITHUB_TOKEN", ""):
            st.warning("GITHUB_TOKENが未設定のため、再起動後はリセットされます。")
        return True
    except Exception as e:
        st.error("FAQ追記エラー: " + str(e))
        return False


# 1ブロックを区切るフィールドタグ（この順序で並ぶ前提）
FIELD_TAGS = ["施設", "カテゴリ", "質問", "返信内容", "やるべきこと"]


def parse_faq_entries(text):
    """faq_manual.txt をエントリのリスト(dict)に変換。
    返信内容中の【夏】【お振込先】等の内部タグは壊さない。"""
    entries = []
    # '---' 単独行でブロック分割
    blocks, buf = [], []
    for line in text.splitlines():
        if line.strip() == "---":
            if buf:
                blocks.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        blocks.append("\n".join(buf))

    for block in blocks:
        if not block.strip():
            continue
        # 既知フィールドタグの位置だけを区切りに使う
        present = []
        for t in FIELD_TAGS:
            m = re.search("【" + t + "】", block)
            if m:
                present.append((t, m.start(), m.end()))
        if not present:
            continue
        present.sort(key=lambda x: x[1])
        d = {"施設": "", "カテゴリ": "", "質問": "", "返信内容": "", "やるべきこと": ""}
        for i, (t, _s, e) in enumerate(present):
            end = present[i + 1][1] if i + 1 < len(present) else len(block)
            d[t] = block[e:end].strip()
        entries.append({
            "facility":  d["施設"],
            "category":  d["カテゴリ"],
            "question":  d["質問"],
            "answer":    d["返信内容"],
            "todo":      d["やるべきこと"],
        })
    return entries


def serialize_faq_entries(entries):
    out = []
    for e in entries:
        fac = (e.get("facility") or "").strip()
        cat = (e.get("category") or "").strip()
        q   = (e.get("question") or "").strip()
        a   = (e.get("answer") or "").strip()
        todo = (e.get("todo") or "").strip()
        if not (fac or cat or q or a or todo):
            continue  # 空行はスキップ
        block = (
            "【施設】" + fac + "\n"
            "【カテゴリ】" + cat + "\n"
            "【質問】" + q + "\n"
            "【返信内容】" + a + "\n"
        )
        if todo:
            block += "【やるべきこと】" + todo + "\n"
        block += "---"
        out.append(block)
    return "\n".join(out) + ("\n" if out else "")


def save_faq_full(entries):
    """編集後のFAQ全体をローカル＋GitHubに保存。"""
    text = serialize_faq_entries(entries)
    with open(FAQ_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    load_faq.clear()
    today = datetime.date.today().strftime("%Y-%m-%d")
    ok = github_put_file("faq_manual.txt", text, "FAQ edit via UI (" + today + ")")
    return ok


# ============================================================
# 対応履歴ログ
# ============================================================
def append_to_history(record):
    """1件の対応をJSON Lines形式で追記。"""
    try:
        record = dict(record)
        record.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
        line = json.dumps(record, ensure_ascii=False)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if SAVE_HISTORY_TO_GITHUB:
            cur = ""
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, encoding="utf-8") as f:
                    cur = f.read()
            github_put_file("history_log.jsonl", cur,
                            "history: " + record.get("ts", ""))
        return True
    except Exception:
        return False


def load_history():
    """履歴を新しい順のリストで返す。"""
    if not os.path.exists(HISTORY_FILE):
        return []
    rows = []
    with open(HISTORY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    rows.reverse()
    return rows


# ============================================================
# メール分類・施設判定
# ============================================================
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
        "language: メール本文の主要言語をISOコードで（ja, en, zh, ko など）\n\n"
        "件名: " + subject + "\n"
        "本文: " + body[:400] + "\n\n"
        '{"type":"...","facility":"...","language":"...","reason":"..."}'
    )
    res = client.messages.create(model=CLAUDE_MODEL, max_tokens=200,
                                 messages=[{"role": "user", "content": prompt}])
    raw = res.content[0].text.strip()
    m = re.search(r'\{[\s\S]+\}', raw)
    if m:
        try:
            data = json.loads(m.group())
            data.setdefault("language", "ja")
            return data
        except Exception:
            pass
    return {"type": "other", "facility": "unknown", "language": "ja", "reason": "failed"}


def generate_response(subject, body, facility_key, faq_text, system_prompt,
                      client, extra_info="", language="ja"):
    facility = get_facility_name(facility_key)
    furl     = FACILITY_URLS.get(facility_key, "")
    extra    = ""
    if extra_info.strip():
        extra = (
            "\n[すでに分かっている情報]\n"
            + extra_info + "\n"
            "※上記の情報はFAQより優先して返信に反映してください。\n"
        )
    lang_block = ""
    if language and language != "ja":
        lang_block = (
            "\n[返信言語の指定]\n"
            "お客様の問い合わせは「" + lang_name(language) + "（" + language + "）」です。\n"
            "★最重要: 『お客様への返信メール』は必ず " + lang_name(language)
            + " で作成してください（件名・本文とも）。\n"
            "★『スタッフのやるべきことリスト』は必ず日本語で作成してください。\n"
        )
    msg = (
        "[対象施設]" + facility + "\n"
        "[公式HP]" + furl + "\n\n"
        "[問い合わせ件名]\n" + subject + "\n\n"
        "[問い合わせ本文]\n" + body + "\n"
        + extra
        + lang_block + "\n"
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
TYPE_LABELS = {
    "customer_inquiry":   "お客様問い合わせ",
    "business_contact":   "企業・業者コンタクト",
    "reservation_notify": "予約通知",
    "system_notify":      "システムメール",
    "other":              "その他",
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

tab_gen, tab_hist, tab_faq = st.tabs(["📧 返信生成", "🗂 対応履歴", "📚 FAQ管理"])


# ------------------------------------------------------------
# タブ1: 返信生成
# ------------------------------------------------------------
with tab_gen:
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

        submit = st.form_submit_button("返信案を生成", type="primary",
                                       use_container_width=True)

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
            lang   = cl.get("language", "ja")
            reason = cl.get("reason", "")

            chosen = [k for k, v in FKEY_LABELS.items() if v == fac_choice][0]
            if chosen != "auto":
                fkey = chosen
            elif fkey == "unknown":
                fkey = detect_facility(subject, body) or "unknown"

            if etype == "business_contact":
                st.session_state.result = {"status": "business", "etype": etype,
                                           "fkey": fkey, "reason": reason}
            elif etype not in ("customer_inquiry",):
                st.session_state.result = {"status": "not_needed", "etype": etype,
                                           "fkey": fkey, "reason": reason}
            elif fkey == "unknown":
                st.session_state.result = {"status": "unknown_fac", "etype": etype,
                                           "fkey": fkey, "reason": reason}
            else:
                with st.spinner("返信とやるべきことリストを生成中..."):
                    faq_text  = load_faq()
                    sysprompt = load_system_prompt()
                    full_text = generate_response(subject, body, fkey, faq_text,
                                                  sysprompt, client, extra_info, lang)

                reply, todo = split_response(full_text)

                faq_added = False
                if extra_info.strip() and save_to_faq:
                    q = subject.strip() if subject.strip() else body[:80]
                    faq_added = append_to_faq(fkey, q, extra_info.strip())

                # 対応履歴へ記録
                append_to_history({
                    "source":   "web",
                    "facility": fkey,
                    "etype":    etype,
                    "language": lang,
                    "subject":  subject.strip(),
                    "body":     body.strip()[:500],
                    "reply":    reply,
                    "todo":     todo,
                    "faq_added": faq_added,
                })

                st.session_state.result = {
                    "status": "ok", "etype": etype, "fkey": fkey, "reason": reason,
                    "language": lang, "reply": reply, "todo": todo, "faq_added": faq_added,
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
        c1, c2, c3 = st.columns(3)
        c1.metric("種別", TYPE_LABELS.get(r["etype"], r["etype"]))
        c2.metric("施設", get_facility_name(r["fkey"]))
        c3.metric("返信言語", lang_name(r.get("language", "ja")))
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


# ------------------------------------------------------------
# タブ2: 対応履歴
# ------------------------------------------------------------
with tab_hist:
    st.subheader("対応履歴")
    history = load_history()
    if not history:
        st.info("まだ履歴がありません。返信を生成すると、ここに記録されます。")
    else:
        c1, c2 = st.columns([1, 2])
        with c1:
            fac_filter = st.selectbox(
                "施設で絞り込み",
                ["すべて"] + list(FACILITY_NAMES.values()),
            )
        with c2:
            kw = st.text_input("キーワード検索（件名・本文・返信から）", "")

        def _match(rec):
            if fac_filter != "すべて" and get_facility_name(rec.get("facility", "")) != fac_filter:
                return False
            if kw.strip():
                hay = (rec.get("subject", "") + " " + rec.get("body", "")
                       + " " + rec.get("reply", "")).lower()
                if kw.strip().lower() not in hay:
                    return False
            return True

        filtered = [r for r in history if _match(r)]
        st.caption(f"{len(filtered)} 件 / 全 {len(history)} 件")

        # CSVダウンロード
        if filtered:
            import io, csv
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["日時", "施設", "種別", "言語", "件名", "本文", "返信", "やること"])
            for rec in filtered:
                w.writerow([
                    rec.get("ts", ""), get_facility_name(rec.get("facility", "")),
                    TYPE_LABELS.get(rec.get("etype", ""), rec.get("etype", "")),
                    lang_name(rec.get("language", "ja")),
                    rec.get("subject", ""), rec.get("body", ""),
                    rec.get("reply", ""), rec.get("todo", ""),
                ])
            st.download_button("CSVでダウンロード", buf.getvalue().encode("utf-8-sig"),
                               file_name="history.csv", mime="text/csv")

        for rec in filtered:
            title = (rec.get("ts", "")[:16] + " | "
                     + get_facility_name(rec.get("facility", "")) + " | "
                     + (rec.get("subject", "") or rec.get("body", "")[:30]))
            with st.expander(title):
                st.caption("種別: " + TYPE_LABELS.get(rec.get("etype", ""), rec.get("etype", ""))
                           + " ／ 言語: " + lang_name(rec.get("language", "ja"))
                           + " ／ 経路: " + rec.get("source", ""))
                if rec.get("body"):
                    st.markdown("**問い合わせ本文**")
                    st.text(rec.get("body", ""))
                st.markdown("**返信メール案**")
                st.text(rec.get("reply", ""))
                if rec.get("todo"):
                    st.markdown("**やるべきこと**")
                    st.text(rec.get("todo", ""))


# ------------------------------------------------------------
# タブ3: FAQ管理
# ------------------------------------------------------------
with tab_faq:
    st.subheader("FAQの編集・削除・追加")
    st.caption("セルを直接編集できます。行の削除は行を選択して削除、追加は最下部の空行に入力してください。"
               "編集後は必ず「変更を保存」を押してください。")
    st.warning("⚠️ ここでの変更は次回以降の全スタッフの回答に影響します。"
               "正確な情報か・どの施設の情報かを確認してから保存してください。")

    entries = parse_faq_entries(load_faq())
    st.caption(f"現在 {len(entries)} 件の登録があります。")

    edited = st.data_editor(
        entries,
        num_rows="dynamic",
        use_container_width=True,
        height=520,
        column_config={
            "facility": st.column_config.TextColumn("施設", width="small"),
            "category": st.column_config.TextColumn("カテゴリ", width="small"),
            "question": st.column_config.TextColumn("質問", width="medium"),
            "answer":   st.column_config.TextColumn("返信内容", width="large"),
            "todo":     st.column_config.TextColumn("やるべきこと", width="medium"),
        },
        key="faq_editor",
    )

    col_a, col_b = st.columns([1, 4])
    with col_a:
        if st.button("変更を保存", type="primary"):
            ok = save_faq_full(edited)
            if ok:
                st.success("保存しました（ローカル＋GitHub）。")
            elif not os.environ.get("GITHUB_TOKEN", ""):
                st.warning("ローカルに保存しました。GITHUB_TOKEN未設定のため再起動でリセットされます。")
            else:
                st.error("GitHubへの保存に失敗しました。ローカルには保存済みです。")
            st.rerun()
    with col_b:
        st.caption("※ 保存すると faq_manual.txt 全体が書き換わります。")
