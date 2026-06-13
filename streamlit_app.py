# -*- coding: utf-8 -*-
import os, re, json, base64, datetime, requests
import streamlit as st
import streamlit.components.v1 as components
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
CANDIDATES_FILE = os.path.join(os.path.dirname(__file__), "faq_candidates.jsonl")

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
        "上記の情報をもとに、以下を作成してください。\n\n"
        "## お客様への返信メール\n"
        "件名と本文を作成してください。\n\n"
        "## スタッフのやるべきことリスト\n"
        "この問い合わせを受けてスタッフが取るべき具体的なアクションを番号付きリストで作成してください。"
        "FAQの【やるべきこと】欄を参考にしてください。\n\n"
        "## 参照したFAQ\n"
        "返信の根拠にした社内FAQの該当項目を、【質問】見出しの形で箇条書きにしてください。"
        "FAQを使っていなければ「なし」と書いてください。\n\n"
        "## FAQ未カバー\n"
        "お客様の質問のうち、社内FAQに情報が無く推測で答えられなかった点を箇条書きにしてください。"
        "全てFAQで答えられたなら「なし」と書いてください。\n\n"
        "出力形式（この4見出しを必ず使うこと）:\n"
        "===返信メール===\n"
        "（件名と本文）\n\n"
        "===やるべきことリスト===\n"
        "（番号付きアクション）\n\n"
        "===参照したFAQ===\n"
        "（箇条書き or なし）\n\n"
        "===FAQ未カバー===\n"
        "（箇条書き or なし）\n"
    )
    res = client.messages.create(model=CLAUDE_MODEL, max_tokens=1800,
                                 system=system_prompt,
                                 messages=[{"role": "user", "content": msg}])
    return res.content[0].text.strip()


def split_response(text):
    """4セクション（返信／やること／参照FAQ／未カバー）をdictで返す。
    見出しが欠けても壊れないようにする。"""
    markers = [
        ("reply", "===返信メール==="),
        ("todo", "===やるべきことリスト==="),
        ("refs", "===参照したFAQ==="),
        ("uncovered", "===FAQ未カバー==="),
    ]
    result = {"reply": "", "todo": "", "refs": "", "uncovered": ""}
    present = [(k, mk, text.find(mk)) for k, mk in markers]
    present = [(k, mk, p) for (k, mk, p) in present if p >= 0]
    present.sort(key=lambda x: x[2])
    if not present:
        result["reply"] = text.strip()
        return result
    # 最初の見出しより前にテキストがあれば返信扱い
    if present[0][2] > 0 and present[0][0] != "reply":
        result["reply"] = text[:present[0][2]].strip()
    for i, (k, mk, p) in enumerate(present):
        start = p + len(mk)
        end = present[i + 1][2] if i + 1 < len(present) else len(text)
        result[k] = text[start:end].strip()
    return result


def _is_uncovered(text):
    t = (text or "").strip().strip("　").lower()
    return bool(t) and t not in ("なし", "無し", "特になし", "ありません", "なし。", "none", "-")


def append_to_candidate(record):
    """FAQ未カバーの問い合わせを追加候補として記録。"""
    try:
        record = dict(record)
        record.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
        with open(CANDIDATES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if SAVE_HISTORY_TO_GITHUB:
            cur = ""
            if os.path.exists(CANDIDATES_FILE):
                with open(CANDIDATES_FILE, encoding="utf-8") as f:
                    cur = f.read()
            github_put_file("faq_candidates.jsonl", cur, "faq候補: " + record.get("ts", ""))
        return True
    except Exception:
        return False


def load_candidates():
    if not os.path.exists(CANDIDATES_FILE):
        return []
    rows = []
    with open(CANDIDATES_FILE, encoding="utf-8") as f:
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


def clear_candidates():
    try:
        open(CANDIDATES_FILE, "w", encoding="utf-8").close()
        if SAVE_HISTORY_TO_GITHUB:
            github_put_file("faq_candidates.jsonl", "", "faq候補クリア")
        return True
    except Exception:
        return False


# ============================================================
# ACO 見積・定型文
# ============================================================
ACO_BANK = (
    "【お振込先】\n"
    "金融機関名：ＧＭＯあおぞらネット銀行\n"
    "支店名：法人第二営業部\n"
    "口座種別：普通口座\n"
    "口座番号：1256913\n"
    "口座名義：株式会社ＷｉｓｔｅｒｉａＦｏｒｅｓｔ\n"
    "ふりがな：カ）ウィステリアフォレスト\n"
    "※軽井沢ハウスヴィラの運営会社名になります。"
)
ACO_OPTION_LIST = (
    "-オプション例-\n"
    "・アーリーチェックイン【4,400円/1時間】\n"
    "・レイトチェックアウト【4,400円/1時間】\n"
    "・ペット同伴希望の場合【9,000円/1泊】\n"
    "・BBQご利用の場合【4,400円/1台】\n"
    "・焚き火台のご利用の場合【3,300円/1台】\n"
    "・サウナご利用の場合【12,000円/1泊】\n"
    "※サウナはサウナ棟またはB棟のみご利用可"
)
JP_WEEK = ["月", "火", "水", "木", "金", "土", "日"]


def jp_date(d):
    return f"{d.month}月{d.day}日（{JP_WEEK[d.weekday()]}）"


def aco_quote(base):
    """エアホスト基本料金にOTA手数料21%（小数点以下繰上げ）を加算。"""
    base = int(base)
    total = (base * 121 + 99) // 100  # = base + ceil(base*0.21)
    return total, total - base


def aco_tmpl_estimate(name):
    return (
        f"{name}様\n"
        "軽井沢ハウスヴィラでございます。\n"
        "このたびのお見積りリクエスト誠にありがとうございます。\n"
        "お見積りをお送りさせて頂きますのでご検討の程宜しくお願いいたします。\n"
        "オプションをご利用の場合には別途追加料金が発生致します。\n"
        "ご利用ご希望の場合には事前にお申し付け下さい。\n"
        + ACO_OPTION_LIST + "\n"
        "ご予約をご希望の場合には、マイページより「予約リクエスト」の送信手続きをお願い致します。\n"
        "ご予約確定日より7日以内にご宿泊代のお支払い（振込）をお願いしております。\n"
        "ご入金の確認をもってご予約確定となりますので予めご了承ください。\n"
        "ぜひご検討のほど宜しくお願い致します。\n"
        "軽井沢ハウスヴィラ"
    )


def aco_tmpl_other_tou(name, date_str, req_tou, free_tou):
    return (
        f"{name}様\n"
        "軽井沢ハウスヴィラでございます。\n"
        "このたびのお問い合わせ誠にありがとうございます。\n"
        f"リクエスト頂いております{date_str}{req_tou}につきましては満室のためご案内不可となります。\n"
        "せっかくご検討候補にあげて頂いたのに受け入れのご案内が出来ず申し訳ございません。\n"
        f"現在、{free_tou}でも宜しければ空室のためご案内が可能です。\n"
        "再度ご検討の上、もしご興味がございましたらお見積りリクエストの申請をお願いいたします。\n"
        "軽井沢ハウスヴィラ"
    )


def aco_tmpl_full(name, date_str):
    return (
        f"{name}様\n"
        "軽井沢ハウスヴィラでございます。\n"
        "このたびのお問い合わせ誠にありがとうございます。\n"
        f"リクエスト頂いております{date_str}につきましては全棟満室のためご案内不可となります。\n"
        "せっかくご検討候補にあげて頂いたのに受け入れのご案内が出来ず申し訳ございません。\n"
        "また機会がございましたらぜひご検討頂けますと幸いです。\n"
        "軽井沢ハウスヴィラ"
    )


def aco_tmpl_request(name, amount, deadline_str):
    return (
        f"{name}様\n"
        "軽井沢ハウスヴィラでございます。\n"
        "このたびのご予約リクエスト誠にありがとうございます。\n"
        "宿泊代のお支払いにつきましてご連絡です。\n"
        "下記に振込口座先を記載しておりますので期日までにお振込をお願い致します。\n"
        "期日までにご入金の確認が取れない場合にはご予約は無効となりますので予めご了承ください。\n"
        "ご案内は以上となりますが、ご不明点等ございましたらお気軽にご連絡下さい。\n"
        + ACO_BANK + "\n"
        "【金額】\n"
        f"{amount:,}円\n"
        "※お振込手数料はご負担いただきますようお願いいたします。\n"
        "【お支払い期日】\n"
        f"{deadline_str}まで\n"
        "※万が一お支払い期日までにお支払いが難しい場合には一度ご連絡の上ご相談ください。\n"
        "軽井沢HOUSE VILLA"
    )


def aco_tmpl_confirmed(name, tou, ci_date, nights, adults, kids, pet, options):
    period = (f"{ci_date.year}年{ci_date.month}月{ci_date.day}日"
              f"（{JP_WEEK[ci_date.weekday()]}）〜{nights}泊")
    lines = [f"{tou}", period, f"大人　{adults}名様"]
    if kids and int(kids) > 0:
        lines.append(f"未就学児お子様　{int(kids)}名様")
    if pet:
        lines.append("（ペット同伴あり）")
    lines.append(f"オプション：{options if options.strip() else 'なし'}")
    kakutei = "\n".join(lines)

    pet_block = ""
    if pet:
        pet_block = (
            "【ペットについて】（ペットオプションをご選択の方のみ）\n"
            "室内に入る前に足を拭いてからお入りください。\n"
            "ソファーやベッドをご利用の際は、臭い防止のためブランケット等をご持参のうえご使用ください。\n"
        )

    return (
        f"{name}様\n"
        "軽井沢ハウスヴィラでございます。\n"
        "この度は数ある宿泊施設から当施設をご予約いただきまして誠にありがとうございます。\n"
        "ご入金の確認が取れましたのでご予約確定にて承ります。\n"
        "【確定内容】\n"
        + kakutei + "\n"
        "※炭、食材、調味料はレンタル内容に含まれておりませんのでご持参ください。\n"
        "お客様のご旅行がより良くなりますよう、スタッフ一同誠心誠意ご対応させていただきます。\n"
        "ご予約内容等に何かご不明点やご不安点はございませんでしょうか。\n"
        "些細なことでも構いませんので何かございましたらお気軽にご連絡いただければ幸いです。\n"
        "注意事項について下記記載させていただきますのでご確認の程宜しくお願い致します。\n"
        "【チェックインについて】\n"
        "・当施設は無人施設のためセルフチェックイン（16:00～）となります。\n"
        "・お部屋のパスコードはご宿泊日当日にお部屋の準備が整い次第、ご案内させていただきます。\n"
        "※チェックイン後はお部屋に外出用の鍵もございますので、パスワード・鍵 どちらかの方法でご利用ください。\n"
        "【駐車場について】\n"
        "・A棟：敷地内のガレージ（1台分）をご利用いただくか、隣接するB棟敷地内にある「A棟専用駐車場」という看板がありますので、そちらの前のスペース（2台分）をご利用下さい。\n"
        "ガレージについては室内にシャッターリモコンがございますので、チェックイン後そちらで開閉お願い致します。\n"
        "・B棟：建物前面の駐車スペース（3台分）をご利用ください。\n"
        "・別邸4棟：現地にあります敷地案内図に記載のある駐車スペース（3台分）をご利用ください。\n"
        "※4台以上でお越しの場合、最寄りのコインパーキング（信濃追分駅前駐車場）をご利用ください。当施設よりお車で5分程度の距離にございます。\n"
        "【当施設の場所について】\n"
        "地図アプリやカーナビによっては住所が異なる場合があります。HPの「アクセス」をご確認ください。\n"
        "・A棟B棟：長野県北佐久郡軽井沢町長倉５５７５ー８\n"
        "・別邸シリーズ：長野県北佐久郡軽井沢町長倉４５８８ー３８\n"
        "【夏の注意点】\n"
        "軽井沢という場所柄、夏は虫や蟻が大変多く発生致します。室内への侵入を防ぐことは不可能ですので、見つけても驚かず外へ逃がしてあげてください。\n"
        "【冬の注意点】\n"
        "軽井沢の冬はとても寒いです。冬の時期のみ凍結防止のために浴槽に水が張ったままとなっておりますので、ご利用の際は水を抜いて一度流してからご利用下さい。\n"
        "また、お風呂をご利用いただいた後に水を排水してしまいますと夜間に凍結してしまう恐れがございます。ご利用後は水を抜かないようにお願い致します。\n"
        + pet_block +
        "【Instagramストーリー投稿キャンペーン】\n"
        "滞在中に撮影した写真や動画をInstagramストーリーに投稿し、\n"
        "公式アカウント housevilla.karuizawa をメンションしていただくと、500円分のチケットをプレゼントしております。\n"
        "公開アカウントでの投稿が対象となります。\n"
        "詳細はお部屋のリビングに設置のチラシをご確認ください。\n"
        "キャンペーンに関するお問い合わせはInstagramのDMよりお願いいたします。\n"
        "【お問い合わせ先】\n"
        "電話：0267-46-9811\n"
        "緊急時：080-7822-2345\n"
        "LINE：https://lin.ee/EscoQGb\n"
        "Instagram：https://instagram.com/housevilla.karuizawa\n"
        "それでは、当日お客様のお越しを心よりお待ちしております。\n"
        "軽井沢ハウスヴィラ\n"
        "https://karuizawa-house-villa.com"
    )


# ============================================================
# オプション料金（3施設）
# ============================================================
# 各項目: (キー, 表示名, 単位, 公式料金, OTA料金)  ※OTAが無い施設は同額
OPTION_PRICES = {
    "karuizawa": {
        "has_ota": True,
        "items": [
            ("early",  "アーリーチェックイン", "時間", 4400, 4400),
            ("late",   "レイトチェックアウト", "時間", 4400, 4400),
            ("pet",    "ペット同伴",           "泊",  9000, 9000),
            ("bbq",    "BBQ機材レンタル",       "台",  3850, 4400),
            ("fire",   "焚き火台",             "台",  2750, 3300),
            ("sauna",  "サウナ（サウナ棟・B棟のみ）", "泊", 10000, 12000),
            ("people", "人数変更",             "人",  7700, 8800),
        ],
    },
    "riveret": {
        "has_ota": False,
        "items": [
            ("bbq",        "BBQセット（炭・トング込み）", "滞在", 5000, 5000),
            ("maki_set",   "薪暖炉 薪セット（7〜10本）", "回",  2000, 2000),
            ("maki_extra", "薪 追加",                   "本",   100,  100),
            ("maki_free",  "薪 使い放題",               "滞在", 4000, 4000),
            ("sauna_d1",   "サウナ 初日",               "人",  1500, 1500),
            ("sauna_dn",   "サウナ 2日目以降",          "人日", 2000, 2000),
        ],
    },
    "tryhaku": {
        "has_ota": False,
        # ※BBQ・焚き火・薪ストーブ・愛犬の金額は社内資料に未記載のため0（要確認）。
        #   正しい金額が分かったらここを更新してください。
        "items": [
            ("people", "人数変更（増減）",        "人", 8800, 8800),
            ("pet",    "愛犬オプション（要確認）", "頭",    0,    0),
            ("bbq",    "BBQ・夏季（要確認）",      "台",    0,    0),
            ("fire",   "焚き火（要確認）",         "回",    0,    0),
            ("stove",  "薪ストーブ（要確認）",     "回",    0,    0),
        ],
    },
}


# ============================================================
# 締切（オプション・変更）
# ============================================================
def deadline_info(facility_key, stay_date):
    """宿泊日から施設別の締切を算出。返り値: [(項目, 期日文字列)]。"""
    d = stay_date
    out = []
    if facility_key == "karuizawa":
        prev = d - datetime.timedelta(days=1)
        d4   = d - datetime.timedelta(days=4)
        out.append(("オプション・人数変更の受付＆支払い", jp_date(prev) + " 17:00まで"))
        out.append(("BBQ等オプションのキャンセル",       jp_date(prev) + " 17:00まで"))
        out.append(("薪ストーブの申し込み（原則）",       jp_date(d4) + "まで"))
    elif facility_key == "riveret":
        d2 = d - datetime.timedelta(days=2)
        out.append(("オプション申し込み（支払い完了まで）", jp_date(d2) + "まで"))
        out.append(("サウナ申し込み",                     jp_date(d2) + "まで"))
    elif facility_key == "tryhaku":
        d7 = d - datetime.timedelta(days=7)
        d3 = d - datetime.timedelta(days=3)
        d14 = d - datetime.timedelta(days=14)
        out.append(("BBQ・焚き火・薪ストーブの注文",     jp_date(d7) + "まで"))
        out.append(("オプションの支払い",                 jp_date(d3) + "まで"))
        out.append(("BBQ等オプションのキャンセル料発生", jp_date(d14) + "から"))
    return out


# ============================================================
# PayPay / 振込
# ============================================================
QR_DIR = os.path.join(os.path.dirname(__file__), "qr")

# 棟 → QR画像ファイル名（ファイル名はそのままでOK）
PAYPAY_QR_FILES = {
    "A棟":  "スクリーンショット 2026-05-02 112929.png",
    "B棟":  "スクリーンショット 2026-05-02 112947.png",
    "別邸": "スクリーンショット 2026-05-02 112905.png",
}


def qr_path_for(tou):
    """棟に対応するQR画像のパスを返す。qr/ フォルダ→本体フォルダの順で探す。無ければNone。"""
    fname = PAYPAY_QR_FILES.get(tou)
    if not fname:
        return None
    for base in (QR_DIR, os.path.dirname(__file__)):
        p = os.path.join(base, fname)
        if os.path.exists(p):
            return p
    return None


def img_mime(path):
    return "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"


def file_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def image_copy_button(b64, mime="image/png"):
    """画像をクリップボードにコピーするボタン（HTTPS環境で動作）。"""
    html = (
        '<button id="cpb" style="padding:8px 16px;border:0;border-radius:6px;'
        'background:#ff4b4b;color:#fff;font-size:14px;cursor:pointer;">'
        '📋 画像をクリップボードにコピー</button>'
        '<span id="cpm" style="margin-left:10px;font-size:13px;"></span>'
        '<script>'
        'const _d="data:' + mime + ';base64,' + b64 + '";'
        'document.getElementById("cpb").onclick=async()=>{'
        'try{const r=await fetch(_d);const b=await r.blob();'
        'await navigator.clipboard.write([new ClipboardItem({[b.type]:b})]);'
        'document.getElementById("cpm").innerText="コピーしました。メールに貼り付けできます。";}'
        'catch(e){document.getElementById("cpm").innerText="コピー不可: "+e+"（下のダウンロードをご利用ください）";}'
        '};'
        '</script>'
    )
    components.html(html, height=48)


def paypay_message(tou, option_name, amount, deadline_str):
    bettei_note = ""
    if tou == "別邸":
        bettei_note = ('※PayPayでお支払い後は"A棟"と表示されますが、'
                       'ご予約は別邸でお間違いございません。\n')
    amt = f"{int(amount):,}円" if amount else "〇円"
    return (
        "軽井沢ハウスヴィラです。\n"
        "ご連絡ありがとうございます。\n"
        f"{option_name}【{amt}】につきまして、添付のPayPay QRコードにて"
        "お手続きいただければと存じます。\n"
        "※現地決済は不可となります。お支払い完了後はメールでご連絡ください。\n"
        + bettei_note +
        "【お支払い金額】\n"
        f"{amt}\n"
        "【お支払い期日】\n"
        f"{deadline_str}\n"
        "何卒、よろしくお願いいたします。\n"
        "軽井沢ハウスヴィラ"
    )


def furikomi_message(amount, deadline_str):
    amt = f"{int(amount):,}円" if amount else "〇円"
    return (
        "下記に振込口座先を記載しておりますので期日までにお振込をお願い致します。\n"
        + ACO_BANK + "\n"
        "【金額】\n"
        f"{amt}\n"
        "※お振込手数料はご負担いただきますようお願いいたします。\n"
        "【お支払い期日】\n"
        f"{deadline_str}まで"
    )


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

tab_gen, tab_aco, tab_calc, tab_pay, tab_hist, tab_faq = st.tabs(
    ["📧 返信生成", "💰 ACO見積", "🧮 料金・期限", "💳 PayPay・振込",
     "🗂 対応履歴", "📚 FAQ管理"])


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

                sec = split_response(full_text)
                reply, todo = sec["reply"], sec["todo"]
                refs, uncovered = sec["refs"], sec["uncovered"]

                faq_added = False
                if extra_info.strip() and save_to_faq:
                    q = subject.strip() if subject.strip() else body[:80]
                    faq_added = append_to_faq(fkey, q, extra_info.strip())

                # FAQ未カバーなら追加候補として記録
                candidate_logged = False
                if _is_uncovered(uncovered):
                    candidate_logged = append_to_candidate({
                        "facility": fkey,
                        "subject":  subject.strip(),
                        "body":     body.strip()[:500],
                        "uncovered": uncovered,
                    })

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
                    "refs":     refs,
                    "uncovered": uncovered,
                    "faq_added": faq_added,
                })

                st.session_state.result = {
                    "status": "ok", "etype": etype, "fkey": fkey, "reason": reason,
                    "language": lang, "reply": reply, "todo": todo,
                    "refs": refs, "uncovered": uncovered,
                    "faq_added": faq_added, "candidate_logged": candidate_logged,
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

        # FAQ未カバーの警告
        if _is_uncovered(r.get("uncovered", "")):
            msg = ("⚠️ **FAQに無い内容が含まれています（推測では回答していません）**\n\n"
                   + r.get("uncovered", ""))
            if r.get("candidate_logged"):
                msg += "\n\n→ この問い合わせを「FAQ追加候補」に記録しました（対応履歴タブで確認）。"
            st.warning(msg)

        # 参照したFAQ（出典）
        refs = r.get("refs", "")
        with st.expander("📎 参照したFAQ（根拠の確認用）"):
            st.markdown(refs if refs else "（記載なし）")


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

    st.divider()
    st.subheader("📝 FAQ追加候補（FAQで答えられなかった質問）")
    cands = load_candidates()
    if not cands:
        st.info("追加候補はありません。FAQに無い質問が来ると、ここに自動でたまります。")
    else:
        st.caption(f"{len(cands)} 件。FAQ化したら「クリア」で消せます。")
        for c in cands:
            ttl = (c.get("ts", "")[:16] + " | "
                   + get_facility_name(c.get("facility", "")) + " | "
                   + (c.get("subject", "") or c.get("body", "")[:30]))
            with st.expander(ttl):
                if c.get("body"):
                    st.markdown("**問い合わせ本文**")
                    st.text(c.get("body", ""))
                st.markdown("**FAQに無かった点**")
                st.text(c.get("uncovered", ""))
        if st.button("追加候補をすべてクリア"):
            clear_candidates()
            st.rerun()


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


# ------------------------------------------------------------
# タブ: ACO見積
# ------------------------------------------------------------
with tab_aco:
    st.subheader("ACO 見積・定型文")

    with st.expander("📌 見積の手順・運用メモ"):
        st.markdown(
            "**【ACO見積提示方法】**\n"
            "1. エアホストの予約カレンダー\n"
            "2. 右上の予約追加\n"
            "3. 該当施設・該当日付を選択\n"
            "4. 料金プランはペット無しなら「公式基本x棟」、ペット有なら「公式ペットx棟」を選択\n"
            "5. 人数入力 →「料金を見る」で料金表示\n"
            "6. OTA手数料に総額の21%分を追加（小数点以下繰上げ）← 下の計算機で自動計算\n"
            "7. できた料金をACOで提示。エアホスト上はブロック不要なので一度消す\n\n"
            "**【運用メモ】**\n"
            "- 初期段階はペット有無が不明なことが多い。備考にペット記載があればペットプラン金額、"
            "無ければオプション項目として送る\n"
            "- リクエストから24時間以内に送らないと催促が来るが、あくまで目安。ペナルティは無いので"
            "できる範囲で当日中に返信する気持ちでOK\n"
            "- 見積り辞退はスルーで問題なし\n"
            "- 問い合わせ後に別サイトから予約が入った場合は施設取消でOK\n\n"
            "**【予約リクエストが来たら】** ACOで【予約確定】を押し「予約リクエスト後メッセージ」を送って"
            "振込依頼。【予約確定】で個人情報が見えるので、それを使ってエアホストに仮入力（手入力）。\n\n"
            "**【入金したら】** エアホストの仮予約を本予約にして「入金確認後メッセージ」を送る。"
        )

    st.markdown("### 1. 見積額の計算")
    base = st.number_input("エアホストの基本料金（円）", min_value=0, step=1000, value=0,
                           help="エアホストで「料金を見る」で出た金額を入力")
    aco_total = 0
    if base > 0:
        aco_total, aco_fee = aco_quote(base)
        m1, m2, m3 = st.columns(3)
        m1.metric("基本料金", f"{int(base):,}円")
        m2.metric("OTA手数料(21%)", f"{aco_fee:,}円")
        m3.metric("ACO提示額", f"{aco_total:,}円")
        st.caption("OTA提示額 = 基本料金 × 21% を加算（小数点以下繰上げ）")

    st.divider()
    st.markdown("### 2. 定型文の作成")
    tmpl = st.selectbox("テンプレートを選択", [
        "① 見積提示",
        "② 別棟提案（リクエスト棟が満室）",
        "③ 全棟満室",
        "予約リクエスト後（振込依頼）",
        "入金確認後（予約確定）",
    ])
    name = st.text_input("お客様のお名前", "", placeholder="例）山田")

    text = ""
    if tmpl == "① 見積提示":
        text = aco_tmpl_estimate(name or "◯◯")

    elif tmpl == "② 別棟提案（リクエスト棟が満室）":
        c1, c2, c3 = st.columns(3)
        d = c1.date_input("宿泊日", value=datetime.date.today())
        req_tou = c2.text_input("リクエストの棟", placeholder="例）A棟")
        free_tou = c3.text_input("空きのある棟", placeholder="例）B棟")
        text = aco_tmpl_other_tou(name or "◯◯", jp_date(d),
                                  req_tou or "◯◯棟", free_tou or "◯棟")

    elif tmpl == "③ 全棟満室":
        d = st.date_input("宿泊日", value=datetime.date.today())
        text = aco_tmpl_full(name or "◯◯", jp_date(d))

    elif tmpl == "予約リクエスト後（振込依頼）":
        c1, c2 = st.columns(2)
        amount = c1.number_input("金額（円）", min_value=0, step=1000,
                                 value=int(aco_total))
        deadline = c2.date_input("お支払い期日",
                                 value=datetime.date.today() + datetime.timedelta(days=7),
                                 help="目安は1週間後")
        text = aco_tmpl_request(name or "◯", int(amount), jp_date(deadline))

    elif tmpl == "入金確認後（予約確定）":
        c1, c2, c3 = st.columns(3)
        tou = c1.text_input("棟", placeholder="例）A棟")
        ci = c2.date_input("チェックイン日", value=datetime.date.today())
        nights = c3.number_input("泊数", min_value=1, step=1, value=1)
        c4, c5, c6 = st.columns(3)
        adults = c4.number_input("大人（名）", min_value=0, step=1, value=2)
        kids = c5.number_input("未就学児（名）", min_value=0, step=1, value=0)
        pet = c6.checkbox("ペット同伴あり")
        options = st.text_input("オプション", placeholder="例）サウナ、BBQ ×2セット、ベットガード")
        text = aco_tmpl_confirmed(name or "◯", tou or "◯棟", ci, int(nights),
                                  int(adults), int(kids), pet, options)

    st.markdown("**コピー用テキスト**")
    st.text_area("", value=text, height=420, label_visibility="collapsed")


# ------------------------------------------------------------
# タブ: 料金・期限
# ------------------------------------------------------------
with tab_calc:
    st.subheader("オプション料金の計算")
    cf1, cf2 = st.columns([1, 1])
    calc_fac_label = cf1.selectbox("施設", list(FACILITY_NAMES.values()),
                                   key="calc_fac")
    calc_fkey = [k for k, v in FACILITY_NAMES.items() if v == calc_fac_label][0]
    cfg = OPTION_PRICES[calc_fkey]

    route = "公式"
    if cfg["has_ota"]:
        route = cf2.radio("予約経路", ["公式", "OTA"], horizontal=True, key="calc_route")
    else:
        cf2.caption("この施設は公式/OTAで料金差はありません。")

    total = 0
    for key, label, unit, off, ota in cfg["items"]:
        price = ota if (route == "OTA" and cfg["has_ota"]) else off
        c1, c2, c3 = st.columns([3, 1, 2])
        c1.markdown(f"**{label}**" + (f"　({price:,}円/{unit})" if price else f"　(金額未設定/{unit})"))
        qty = c2.number_input(f"数量({unit})", min_value=0, step=1, value=0,
                              key=f"opt_{calc_fkey}_{key}", label_visibility="collapsed")
        sub = price * qty
        total += sub
        c3.markdown(f"小計： **{sub:,}円**")

    st.divider()
    st.metric("オプション合計", f"{total:,}円")
    if calc_fkey == "tryhaku":
        st.caption("※「要確認」の項目は社内資料に金額の記載がないため0円です。正しい金額は"
                   "コード内 OPTION_PRICES の tryhaku を更新してください。")

    st.divider()
    st.subheader("オプション・変更の締切チェッカー")
    dc1, dc2 = st.columns([1, 1])
    dl_fac_label = dc1.selectbox("施設", list(FACILITY_NAMES.values()), key="dl_fac")
    dl_fkey = [k for k, v in FACILITY_NAMES.items() if v == dl_fac_label][0]
    stay = dc2.date_input("宿泊日（チェックイン日）", value=datetime.date.today(),
                          key="dl_stay")
    st.markdown(f"**{dl_fac_label}** の締切：")
    for label, when in deadline_info(dl_fkey, stay):
        st.markdown(f"- {label}： **{when}**")


# ------------------------------------------------------------
# タブ: PayPay・振込
# ------------------------------------------------------------
with tab_pay:
    st.subheader("PayPay・振込 クイック案内")
    st.caption("棟を選ぶと該当QRと案内文が出ます。金額・期日を入れて文面をコピー、QRは"
               "「コピー」ボタンでそのままメールに貼り付けできます。")

    p1, p2, p3 = st.columns(3)
    tou = p1.selectbox("棟（PayPay QR）", ["A棟", "B棟", "別邸"])
    opt_name = p2.text_input("項目名", value="ペットプラン",
                             help="例）ペットプラン、サウナ、人数追加 など")
    pay_amount = p3.number_input("金額（円）", min_value=0, step=1000, value=0)
    pay_deadline = st.date_input("お支払い期日",
                                 value=datetime.date.today() + datetime.timedelta(days=7))
    deadline_str = jp_date(pay_deadline)

    method = st.radio("支払い方法", ["PayPay", "銀行振込"], horizontal=True)

    if method == "PayPay":
        st.markdown("#### 案内文（コピー用）")
        if tou == "別邸":
            st.caption('別邸はA棟のQRで支払い後 "A棟" と表示されます（案内文に注記済み）。')
        st.text_area("", value=paypay_message(tou, opt_name, pay_amount, deadline_str),
                     height=300, label_visibility="collapsed")

        st.markdown(f"#### QRコード（{tou}）")
        qp = qr_path_for(tou)
        if qp:
            ic1, ic2 = st.columns([1, 2])
            with ic1:
                st.image(qp, caption=tou, width=220)
            with ic2:
                image_copy_button(file_b64(qp), mime=img_mime(qp))
                st.download_button("QR画像をダウンロード", data=open(qp, "rb").read(),
                                   file_name=os.path.basename(qp), mime=img_mime(qp))
        else:
            st.warning(f"{tou}のQR画像が見つかりません。`{PAYPAY_QR_FILES.get(tou, '')}` を"
                       "お問い合わせbotフォルダ（または qr/ フォルダ）に置いてください。")
    else:
        st.markdown("#### 振込案内（コピー用）")
        st.text_area("", value=furikomi_message(pay_amount, deadline_str),
                     height=320, label_visibility="collapsed")
