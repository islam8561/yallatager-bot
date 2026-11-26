"""
بوت متابعة أسعار يلا تاجر
- بيقرا كل الأقسام اللي في CATEGORIES
- بيجمّع الموبايلات بالموديل + المساحة/الرام + نوع الضمان، ويسيب اللون والهدايا، وياخد أقل سعر
- بيقارن بآخر مرة ويبعت أي تغيير على تيليجرام
"""
import csv
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

BASE = "https://yallatager.com/ar/"

# الأقسام اللي بيتابعها البوت (احذف أي سطر مش عايزه)
CATEGORIES = {
    "Smart-Phone-(-Madein-Egypt-)": "الهواتف الذكية",
    "Tablets-(-made-in-egypt-)": "التابلت",
    "Feature-Phone-(-Madein-Egypt-)": "الهواتف الاقتصادية",
    "Accessories": "إكسسوارات الموبايل",
    "Watches": "ساعات ذكية",
    "AirPods": "ايربودز",
    "Active-Mobile": "هواتف اكتيف",
    "NTRA-Smart-Phone": "مزادات جمارك مسجلة نترا",
    "Offers-and-Bundles": "البنادل والعروض",
    "Used-Smart-Phones": "هواتف ذكية مستعملة",
    "Used-Accessories": "إكسسوارات مستعملة",
    "Compressed-box": "علبة مضغوطة",
}

STATE_FILE = "data/prices.json"
HISTORY_FILE = "data/history.csv"
CHAT_FILE = "data/chat_id.txt"
SETTINGS_FILE = "data/settings.json"
CATS_FILE = "data/cats.json"
CAIRO = timezone(timedelta(hours=2))
os.makedirs("data", exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (price-watch; personal use)"}
PRODUCT_RE = re.compile(
    r'>([^<>]{3,250})</a></h6>\s*<div class="product_price">([\s\S]*?)</div>'
)
TOTAL_RE = re.compile(r"نعرض\s*\d+\s*من\s*(\d+)\s*منتج.{0,80}?صفحة\s*\d+\s*من\s*(\d+)", re.S)
PHONE_RE = re.compile(r"^(.*?) - (\d+\s*(?:GB|TB)\s*/\s*\d+\s*(?:GB|TB)) - .*?\(([^)]*)\)\s*$")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()


# ---------- قراءة الموقع ----------

def get(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
            if r.status_code == 200 and ("product_price" in r.text or "نعرض" in r.text):
                return r.text
        except requests.RequestException:
            pass
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"فشل تحميل {url}")


def parse(page_html):
    items = []
    for name, block in PRODUCT_RE.findall(page_html):
        nums = re.findall(r"[\d,]{3,}", re.sub(r"<[^>]+>", " ", block))
        if not nums:
            continue
        items.append((html.unescape(name).strip(), int(nums[0].replace(",", ""))))
    return items


def page_text(page_html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page_html))


def fetch_category(slug):
    first = get(BASE + slug + ".html")
    m = TOTAL_RE.search(page_text(first))
    expected, pages = (int(m.group(1)), int(m.group(2))) if m else (None, 1)
    urls = [BASE + slug + f".html?page={p}" for p in range(2, pages + 1)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        rest = list(ex.map(get, urls))
    items = []
    for h in [first] + rest:
        items += parse(h)
    if expected and len(items) < expected * 0.9:
        raise RuntimeError(f"{slug}: اتقرا {len(items)} من {expected} بس")
    return items


def group_key(name):
    g = PHONE_RE.match(name)
    if not g:
        return name
    warranty = "صنع في مصر" if "مصر" in g.group(3) else ("ضمان الوكيل" if "كيل" in g.group(3) else g.group(3).strip())
    storage = re.sub(r"\s+", "", g.group(2))
    return f"{g.group(1).strip()} - {storage} ({warranty})"


def collect():
    prices, cats = {}, {}
    for slug, label in CATEGORIES.items():
        for name, price in fetch_category(slug):
            k = group_key(name)
            if k not in prices or price < prices[k]:
                prices[k] = price
            cats.setdefault(k, label)
        time.sleep(1)
    return prices, cats


# ---------- تيليجرام ----------

def tg(method, **data):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, timeout=30)
    return r.json()


def chat_id():
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if cid:
        return cid
    if os.path.exists(CHAT_FILE):
        return open(CHAT_FILE).read().strip()
    return None


# ---------- الإعدادات والأوامر ----------

SLUGS = list(CATEGORIES)

HELP = """🤖 أوامر بوت أسعار يلا تاجر

/list — عرض الأقسام وأرقامها (✅ متابَع)
/only 1 3 — تابع الأقسام دي بس
/add 2 — ضيف قسم للمتابعة
/remove 2 — شيل قسم من المتابعة
/all — تابع كل الأقسام

ممكن كمان تبعت الأرقام بس، مثلاً: 1 2
البوت بيرد في أول مراجعة جاية للأسعار (كل 30 دقيقة تقريبًا)."""


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        s = json.load(open(SETTINGS_FILE, encoding="utf-8"))
    else:
        s = {}
    s.setdefault("enabled", SLUGS[:])
    s["enabled"] = [x for x in s["enabled"] if x in CATEGORIES]
    s.setdefault("offset", 0)
    return s


def save_settings(s):
    json.dump(s, open(SETTINGS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def list_text(enabled):
    lines = ["📂 الأقسام:"]
    for i, slug in enumerate(SLUGS, 1):
        lines.append(f"{'✅' if slug in enabled else '⬜'} {i}. {CATEGORIES[slug]}")
    lines.append(f"\nمتابع {len(enabled)} من {len(SLUGS)} قسم.\nمثال: /only 1 2  أو  /add 3  أو  /remove 3")
    return "\n".join(lines)


def parse_nums(text):
    nums = [int(n) for n in re.findall(r"\d+", text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))]
    return [SLUGS[n - 1] for n in nums if 1 <= n <= len(SLUGS)]


def handle(text, s):
    """بيرجع الرد على رسالة واحدة"""
    t = text.strip()
    cmd = t.split()[0].lower().split("@")[0] if t else ""
    picked = parse_nums(t)
    en = s["enabled"]
    if cmd in ("/start", "/help", "مساعدة", "الأوامر"):
        return HELP + "\n\n" + list_text(en)
    if cmd in ("/list", "الأقسام", "الاقسام", "اقسام"):
        return list_text(en)
    if cmd in ("/all", "الكل"):
        s["enabled"] = SLUGS[:]
        return "✅ هتابع كل الأقسام.\n\n" + list_text(s["enabled"])
    if cmd in ("/only", "/add", "/remove") or (picked and re.fullmatch(r"[\d٠-٩\s,،]+", t)):
        if not picked:
            return "اكتب رقم القسم بعد الأمر، مثلاً: " + (cmd if cmd.startswith("/") else "/only") + " 1\n\n" + list_text(en)
        if cmd == "/add":
            s["enabled"] = [x for x in SLUGS if x in en or x in picked]
        elif cmd == "/remove":
            s["enabled"] = [x for x in en if x not in picked]
        else:
            s["enabled"] = [x for x in SLUGS if x in picked]
        if not s["enabled"]:
            return "⚠️ كده مش هتابع أي قسم، ومش هيوصلك إشعارات لحد ما تضيف قسم.\n\n" + list_text(s["enabled"])
        return "👌 تم.\n\n" + list_text(s["enabled"])
    return "مفهمتش الأمر 🙏\n\n" + HELP


def process_updates(s):
    """بيقرا رسايلك للبوت وينفذ الأوامر. بيرجع رقم المحادثة"""
    cid = chat_id()
    res = tg("getUpdates", offset=s["offset"], timeout=0).get("result", [])
    if res and s["offset"] == 0:  # أول مرة: نسجل قائمة الأوامر في تيليجرام
        tg("setMyCommands", commands=json.dumps([
            {"command": "list", "description": "عرض الأقسام"},
            {"command": "only", "description": "تابع أقسام معينة بس"},
            {"command": "add", "description": "ضيف قسم"},
            {"command": "remove", "description": "شيل قسم"},
            {"command": "all", "description": "تابع كل الأقسام"},
            {"command": "help", "description": "المساعدة"},
        ], ensure_ascii=False))
    for u in res:
        s["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = msg.get("chat", {})
        if chat.get("type") != "private" or "text" not in msg:
            continue
        if not cid:
            cid = str(chat["id"])
            open(CHAT_FILE, "w").write(cid)
        if str(chat["id"]) != cid:  # حد غيرك: نتجاهله
            continue
        send(cid, handle(msg["text"], s))
    return cid


def send(cid, text):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        tg("sendMessage", chat_id=cid, text=c, disable_web_page_preview="true")
        time.sleep(0.5)


def fmt(n):
    return f"{n:,}"


# ---------- التشغيل ----------

def main():
    if not TOKEN:
        sys.exit("مفيش TELEGRAM_TOKEN")
    settings = load_settings()
    cid = process_updates(settings)
    save_settings(settings)
    if not cid:
        sys.exit("ابعت /start للبوت على تيليجرام الأول، وبعدين شغّل تاني")
    watched = {CATEGORIES[x] for x in settings["enabled"]}

    try:
        new, cats = collect()
    except Exception as e:  # الموقع واقع أو اتغير: منسجلش حاجة غلط
        print("ERROR:", e)
        sys.exit(1)

    old = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else None
    old_cats = json.load(open(CATS_FILE, encoding="utf-8")) if os.path.exists(CATS_FILE) else {}
    now = datetime.now(CAIRO).strftime("%Y-%m-%d %H:%M")

    if old is None:
        send(cid, f"✅ بدأت أتابع أسعار يلا تاجر\n{len(new)} صنف في {len(CATEGORIES)} قسم.\nهبعتلك أول ما أي سعر يتغير.\nابعت /list علشان تختار الأقسام.")
    else:
        changed, added, removed = [], [], []
        for k, p in new.items():
            if k not in old:
                added.append(k)
            elif old[k] != p:
                changed.append(k)
        removed = [k for k in old if k not in new]

        rows = []
        by_cat = {}
        for k in changed:
            d = new[k] - old[k]
            icon = "🔻" if d < 0 else "🔺"
            by_cat.setdefault(cats.get(k, ""), []).append(
                f"{icon} {k}\n     من {fmt(old[k])} لـ {fmt(new[k])}  ({'زاد' if d > 0 else 'نزل'} {fmt(abs(d))})")
            rows.append([now, "تغيير", k, old[k], new[k]])
        for k in added:
            by_cat.setdefault(cats.get(k, ""), []).append(f"🆕 {k}\n     {fmt(new[k])}")
            rows.append([now, "جديد", k, "", new[k]])
        for k in removed:
            rows.append([now, "اختفى", k, old[k], ""])

        # الإشعار بس للأقسام اللي إنت متابعها (الأسعار بتتحفظ لكل الأقسام عادي)
        by_cat = {c: v for c, v in by_cat.items() if c in watched}
        removed_w = [k for k in removed if old_cats.get(k, "") in watched or k not in old_cats]
        if by_cat or removed_w:
            msg = [f"📊 تغييرات يلا تاجر — {now}"]
            for cat, lines in by_cat.items():
                msg.append(f"\n━━ {cat} ━━")
                msg += lines
            if removed_w:
                msg.append("\n━━ اختفى من الموقع ━━")
                msg += [f"❌ {k}  (كان {fmt(old[k])})" for k in removed_w]
            send(cid, "\n".join(msg))

        if rows:
            first = not os.path.exists(HISTORY_FILE)
            with open(HISTORY_FILE, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if first:
                    w.writerow(["الوقت", "النوع", "الصنف", "السعر القديم", "السعر الجديد"])
                w.writerows(rows)
        print(f"changed={len(changed)} added={len(added)} removed={len(removed)}")

    json.dump(dict(sorted(new.items())), open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    json.dump(dict(sorted(cats.items())), open(CATS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
