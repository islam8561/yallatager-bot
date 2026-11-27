"""
مراجعة أسعار MK وتجهيز ملف الاستيراد لفولت
- قوائم أسعار الواتس: انسخها والصقها للبوت
- ملف فولت: صدّر «Excel للاستيراد» وابعته للبوت
- مراجعة: البوت يقارن ويقترح، وإنت توافق أو تعدّل
- موافق: البوت يبعتلك ملف استيراد فولت (الكمية صفر = المخزون مبيتغيرش)

كل البيانات دي (أسعار الشراء وقوائم التجار) بتتحفظ مشفّرة في data/pricing.enc
لأن الريبو عام. مفتاح التشفير مأخوذ من TELEGRAM_TOKEN.
"""
import base64
import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone, timedelta

import requests
from cryptography.fernet import Fernet, InvalidToken

STATE_FILE = "data/pricing.enc"
CAIRO = timezone(timedelta(hours=2))
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
YT = "يلا تاجر"


# ---------- الحفظ المشفّر ----------

def _fernet(token):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("mk-pricing:" + token).encode()).digest()))


def load_state(token):
    st = {}
    if os.path.exists(STATE_FILE):
        try:
            st = json.loads(_fernet(token).decrypt(open(STATE_FILE, "rb").read()))
        except (InvalidToken, ValueError):
            st = {}
    st.setdefault("offers", {})       # التاجر -> {date, items}
    st.setdefault("volt", None)       # {file_id, date, items}
    st.setdefault("review", None)     # آخر مراجعة
    st.setdefault("shipping", None)   # شحن يلا تاجر للقطعة
    st.setdefault("whole_add", 50)    # الجملة = سعر المورد + كام
    st.setdefault("threshold", 500)   # أقل فرق يستاهل تنبيه
    st.setdefault("alerted", {})      # باركود -> آخر تكلفة اتبعتلك عنها تنبيه
    return st


def save_state(token, st):
    data = json.dumps(st, ensure_ascii=False).encode()
    open(STATE_FILE, "wb").write(_fernet(token).encrypt(data))


def now():
    return datetime.now(CAIRO).strftime("%Y-%m-%d %H:%M")


def fmt(n):
    return f"{int(round(n)):,}"


# ---------- توحيد اسم الموديل ----------

DROP_WORDS = {"xiaomi", "redmi", "samsung", "galaxy", "honor", "oppo", "apple",
              "infinix", "vivo", "realme", "tecno", "nokia", "itel"}
COLORS = r"\b(black|white|blue|grey|gray|green|gold|silver|purple|pink|red|orange)\b"


def model_key(txt):
    t = txt.lower()
    t = re.sub(r"\(?\b[45]\s*g\b\)?", " ", t)                       # 4G / 5G
    t = re.sub(r"\d+\s*(gb|tb)?\s*/\s*\d+\s*(gb)?", " ", t)           # السعة
    t = re.sub(COLORS, " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    key = "".join(w for w in t.split() if w not in DROP_WORDS)
    return key.replace("playe", "play")


def is5g(txt):
    return bool(re.search(r"5\s*g", txt, re.I))


# ---------- قوائم الواتس ----------

BRAND_HEADERS = {"شاوم": "xiaomi", "هونر": "honor", "اوبو": "oppo", "أوبو": "oppo",
                 "سامسونج": "samsung", "ريلمي": "realme", "انفنكس": "infinix",
                 "فيفو": "vivo", "ابل": "apple", "ايفون": "apple", "تكنو": "tecno"}
KNOWN_SUPPLIERS = {"العزيزي": "العزيزي", "أبوعلي": "أبوعلي", "ابوعلي": "أبوعلي",
                   "أبو علي": "أبوعلي", "ابو علي": "أبوعلي"}
NUM = re.compile(r"(?<![A-Za-z\d])\d+(?![A-Za-z\d])")


def clean_label(line):
    return re.sub(r"[^؀-ۿ ]", " ", line).split()


def parse_whatsapp(text):
    """بيرجع {اسم التاجر أو None: قائمة الأسعار} — الرسالة ممكن يبقى فيها أكتر من تاجر"""
    text = text.translate(AR_DIGITS)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    supplier = None
    if lines and not re.search(r"[A-Za-z\d]", lines[0]) and \
            not any(k in lines[0] for k in list(KNOWN_SUPPLIERS) + list(BRAND_HEADERS)):
        supplier = " ".join(clean_label(lines[0])) or None   # اسم مكتوب في أول سطر
    out, brand = {}, None
    for line in lines:
        if not re.search(r"[A-Za-z]", line):          # سطر عنوان
            for k, v in KNOWN_SUPPLIERS.items():
                if k in line:
                    supplier = v
                    break
            for k, v in BRAND_HEADERS.items():
                if k in line:
                    brand = v
            continue
        nums = [(m.group(), m.start()) for m in NUM.finditer(line)]
        if len(nums) < 3:
            continue
        (st, pos), (ram, _), (price, _) = nums[-3], nums[-2], nums[-1]
        if int(price) < 300:            # مش سعر
            continue
        model_txt = line[:pos]
        out.setdefault(supplier, []).append(
            {"brand": brand, "raw": line, "model": model_key(model_txt), "is5g": is5g(model_txt),
             "storage": int(st), "ram": int(ram), "price": int(price)})
    return out


# ---------- أسعار يلا تاجر (من ملفات البوت) ----------

YT_RE = re.compile(r"^(.*?) - (\d+)\s*(?:GB|TB)\s*/\s*(\d+)\s*GB \(([^)]*)\)$")


def yallatager_offers(prices_path="data/prices.json", cats_path="data/cats.json"):
    if not os.path.exists(prices_path):
        return []
    p = json.load(open(prices_path, encoding="utf-8"))
    c = json.load(open(cats_path, encoding="utf-8")) if os.path.exists(cats_path) else {}
    out = []
    for k, price in p.items():
        if c.get(k) not in ("الهواتف الذكية", "التابلت"):
            continue
        m = YT_RE.match(k)
        if not m:
            continue
        name, st, ram, war = m.groups()
        out.append({"supplier": YT + (" (وكيل)" if "وكيل" in war else ""), "is_yt": True,
                    "brand": name.split()[0].lower(), "raw": k, "model": model_key(name),
                    "is5g": is5g(name), "storage": int(st), "ram": int(ram), "price": price})
    return out


# ---------- ملف فولت ----------

CAT_BRAND = {"شاومي": "xiaomi", "هونر": "honor", "اوبو": "oppo", "سامسونج": "samsung",
             "ريلمي": "realme", "انفنكس": "infinix", "فيفو": "vivo", "ابل": "apple"}
VOLT_COLS = ["اسم المنتج", "الباركود", "التصنيف", "سعر الشراء", "سعر البيع",
             "سعر بيع الجملة", "نظام التتبع", "وحدة القياس", "الكمية", "قائمة السيريالات"]


def parse_volt_xlsx(data):
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    head = [c.value for c in ws[1]]
    missing = [c for c in VOLT_COLS if c not in head]
    if missing:
        raise ValueError("الأعمدة دي مش موجودة: " + "، ".join(missing))
    ix = {c: head.index(c) for c in VOLT_COLS}
    items = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        name = (r[ix["اسم المنتج"]] or "").strip()
        bc = str(r[ix["الباركود"]] or "").strip()
        cat = (r[ix["التصنيف"]] or "").strip()
        if not name:
            continue
        if not (cat.startswith("موبايلات") or cat == "تاب") or cat == "موبايلات زراير":
            continue
        base = re.split(r"\s\+\s|\(wi", name, flags=re.I)[0]
        m = re.search(r"(\d+)\s*(?:GB)?\s*/\s*(\d+)", name, re.I)
        items.append({
            "name": name, "barcode": bc, "cat": cat,
            "brand": next((v for k, v in CAT_BRAND.items() if k in cat), None),
            "model": model_key(base), "is5g": is5g(name),
            "storage": int(m.group(1)) if m else None, "ram": int(m.group(2)) if m else None,
            "sell": float(r[ix["سعر البيع"]] or 0), "whole": float(r[ix["سعر بيع الجملة"]] or 0),
        })
    return items


# ---------- المراجعة ----------

def compute_entries(st):
    volt = st["volt"]["items"]
    ship = st["shipping"] or 0
    offers = yallatager_offers()
    for sup, o in st["offers"].items():
        offers += [{**x, "supplier": sup, "is_yt": False} for x in o["items"]]
    entries, no_storage, no_offer, no_barcode = [], [], [], []
    for it in volt:
        if not it["barcode"]:
            no_barcode.append(it["name"])
            continue
        same = [o for o in offers if o["model"] == it["model"] and o["is5g"] == it["is5g"]
                and (it["brand"] is None or o["brand"] in (None, it["brand"]))]
        if it["storage"] is None:
            (no_storage if same else no_offer).append(it["name"])
            continue
        exact = [o for o in same if o["storage"] == it["storage"] and o["ram"] == it["ram"]]
        if not exact:
            no_offer.append(it["name"])
            continue
        best = {}
        for o in exact:      # أرخص سعر لكل تاجر
            if o["supplier"] not in best or o["price"] < best[o["supplier"]]["price"]:
                best[o["supplier"]] = o
        opts = sorted(({"supplier": s, "price": o["price"],
                        "cost": o["price"] + (ship if o["is_yt"] else 0), "is_yt": o["is_yt"]}
                       for s, o in best.items()), key=lambda x: x["cost"])
        entries.append({"barcode": it["barcode"], "name": it["name"], "sell": it["sell"],
                        "whole": it["whole"], "opts": opts, "pick": opts[0]["supplier"],
                        "new_sell": None, "new_whole": None, "skip": False})
    return entries, no_storage, no_offer, no_barcode


def important(e, st):
    """الصنف يستاهل يظهر: فرق الجملة كبير، أو مفيش جملة، أو بيعك أقل من التكلفة، أو إنت عدّلته"""
    cost, sell, whole = final_prices(e, st["whole_add"])
    return (abs(whole - e["whole"]) >= st["threshold"] or not e["whole"] or sell < cost
            or e["skip"] or e["new_sell"] is not None or e["new_whole"] is not None
            or e["pick"] != e["opts"][0]["supplier"])


def build_review(st):
    entries, no_storage, no_offer, no_barcode = compute_entries(st)
    for i, e in enumerate(entries, 1):
        e["n"] = i
    st["review"] = {"date": now(), "entries": entries, "no_storage": no_storage,
                    "no_offer": no_offer, "no_barcode": no_barcode}


def alerts(st):
    """أصناف اتغيرت تكلفتها لدرجة إن الجملة المقترحة بعدت عن جملتك بـ threshold أو أكتر"""
    if not st["volt"]:
        return None
    entries = compute_entries(st)[0]
    lines = []
    for e in entries:
        cost, sell, whole = final_prices(e, st["whole_add"])
        d = whole - e["whole"]
        if not e["whole"] or abs(d) < st["threshold"]:
            continue
        if st["alerted"].get(e["barcode"]) == cost:   # اتبعت قبل كده بنفس السعر
            continue
        st["alerted"][e["barcode"]] = cost
        best = e["opts"][0]
        lines.append(f"{'🔺' if d > 0 else '🔻'} {e['name']}\n"
                     f"   جملتك {fmt(e['whole'])} ← {fmt(whole)} ({'زاد' if d > 0 else 'نزل'} {fmt(abs(d))})"
                     f" | قطاعي {fmt(sell)}{'  ⚠️' if sell < cost else ''}\n"
                     f"   أرخص: {best['supplier']} {fmt(best['price'])}")
    if not lines:
        return None
    return (f"🔔 فرق {fmt(st['threshold'])}ج أو أكتر في {len(lines)} صنف:\n\n" + "\n".join(lines)
            + "\n\nاكتب: مراجعة")


def picked(e):
    return next((o for o in e["opts"] if o["supplier"] == e["pick"]), e["opts"][0])


def final_prices(e, whole_add):
    cost = picked(e)["cost"]
    sell = e["new_sell"] if e["new_sell"] is not None else e["sell"]
    whole = e["new_whole"] if e["new_whole"] is not None else cost + whole_add
    return cost, sell, whole


def entry_text(e, st):
    cost, sell, whole = final_prices(e, st["whole_add"])
    if e["skip"]:
        return f"{e['n']}) ⏭️ {e['name']}  (متخطّي)"
    warn = "  ⚠️ أقل من التكلفة" if sell < cost else ""
    sell_txt = fmt(e["sell"]) if sell == e["sell"] else f"{fmt(e['sell'])} ← {fmt(sell)}"
    whole_txt = fmt(e["whole"]) if round(whole) == round(e["whole"]) else f"{fmt(e['whole'])} ← {fmt(whole)}"
    opts = []
    for o in e["opts"]:
        p = f"{fmt(o['price'])}+{fmt(o['cost'] - o['price'])}" if o["cost"] != o["price"] else fmt(o["price"])
        opts.append(("✅ " if o["supplier"] == e["pick"] else "") + f"{o['supplier']} {p}")
    return (f"{e['n']}) {e['name']}\n"
            f"   قطاعي {sell_txt}{warn} | جملة {whole_txt}\n"
            f"   " + " • ".join(opts))


def review_text(st, show_all=False):
    r = st["review"]
    out = [f"📋 مراجعة الأسعار — {r['date']}"]
    if st["shipping"] is None:
        out.append("⚠️ مصاريف شحن يلا تاجر مش متحددة (اعتبرتها صفر). اكتب مثلاً: /shipping 100")
    else:
        out.append(f"🚚 شحن يلا تاجر: {fmt(st['shipping'])}ج على القطعة")
    srcs = [f"{s} ({o['date']})" for s, o in st["offers"].items()]
    out.append("📥 قوائم الواتس: " + ("، ".join(srcs) if srcs else "مفيش"))
    out.append(f"📦 ملف فولت: {st['volt']['date']}")
    out.append(f"الجملة = سعر المورد المختار + {st['whole_add']}\n")
    shown = [e for e in r["entries"] if show_all or important(e, st)]
    hidden = len(r["entries"]) - len(shown)
    out += [entry_text(e, st) for e in shown]
    if not shown:
        out.append(f"✅ مفيش أي صنف فرقه {fmt(st['threshold'])}ج أو أكتر.")
    if hidden:
        out.append(f"\n🙈 {hidden} صنف تانيين فرقهم أقل من {fmt(st['threshold'])}ج — اكتب: الكل")
    if r["no_storage"]:
        out.append("\n❔ ليهم أسعار بس اسمهم في فولت من غير سعة (ضيف السعة للاسم):")
        out += [f"• {n}" for n in r["no_storage"]]
    if r["no_barcode"]:
        out.append("\n❗ من غير باركود في فولت (مش هيتعدلوا):")
        out += [f"• {n}" for n in r["no_barcode"]]
    if r["no_offer"]:
        out.append(f"\n➖ {len(r['no_offer'])} صنف مالقتلهمش سعر عند أي تاجر.")
    out.append("\n✏️ للتعديل اكتب سطر أو أكتر:\n"
               "3 بيع 12500  •  3 جملة 12000  •  3 العزيزي  •  3 تخطي  •  3 رجوع\n"
               "ولما تخلص اكتب: موافق")
    return "\n".join(out)


def find_supplier(e, word):
    for o in e["opts"]:
        if word in o["supplier"] or o["supplier"].startswith(word):
            return o["supplier"]
    return None


def apply_edits(text, st):
    """بيرجع رد لو الرسالة تعديلات، أو None"""
    r = st.get("review")
    if not r:
        return None
    lines = [l.strip() for l in text.translate(AR_DIGITS).splitlines() if l.strip()]
    if not lines or not all(re.match(r"^\d+\s+[^\d\s]", l) for l in lines):
        return None
    byn = {e["n"]: e for e in r["entries"]}
    done, errors = [], []
    for l in lines:
        m = re.match(r"^(\d+)\s+(.+)$", l)
        n, rest = int(m.group(1)), m.group(2).strip()
        e = byn.get(n)
        if not e:
            errors.append(f"مفيش صنف رقم {n}")
            continue
        m2 = re.match(r"^(بيع|قطاعي|جملة|جمله)\s+([\d,]+)$", rest)
        if m2:
            val = float(m2.group(2).replace(",", ""))
            if m2.group(1) in ("جملة", "جمله"):
                e["new_whole"] = val
            else:
                e["new_sell"] = val
        elif rest in ("تخطي", "تخطى", "شيل"):
            e["skip"] = True
        elif rest in ("رجوع", "رجع"):
            e.update(skip=False, new_sell=None, new_whole=None, pick=e["opts"][0]["supplier"])
        else:
            sup = find_supplier(e, rest.replace("يلا", YT) if rest == "يلا" else rest)
            if not sup:
                errors.append(f"{n}: مش فاهم «{rest}». التجار المتاحين: " + "، ".join(o["supplier"] for o in e["opts"]))
                continue
            e["pick"] = sup
            e["new_whole"] = None
        done.append(e)
    uniq = list({e["n"]: e for e in done}.values())
    out = ["👌 اتعدّل:"] + [entry_text(e, st) for e in uniq]
    if errors:
        out.append("\n⚠️ " + "\n⚠️ ".join(errors))
    out.append("\nلما تخلص اكتب: موافق")
    return "\n".join(out)


def build_volt_file(st, data):
    """بياخد ملف فولت الأصلي ويسيب الأصناف اللي اتغيرت بس، والكمية صفر"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.active
    head = [c.value for c in ws[1]]
    ix = {c: head.index(c) + 1 for c in VOLT_COLS}
    changes = {}
    for e in st["review"]["entries"]:
        if e["skip"]:
            continue
        cost, sell, whole = final_prices(e, st["whole_add"])
        if round(sell) != round(e["sell"]) or round(whole) != round(e["whole"]):
            changes[e["barcode"]] = (e, round(sell), round(whole))
    for row in range(ws.max_row, 1, -1):
        bc = str(ws.cell(row, ix["الباركود"]).value or "").strip()
        if bc not in changes:
            ws.delete_rows(row)
            continue
        e, sell, whole = changes.pop(bc)
        ws.cell(row, ix["سعر البيع"]).value = sell
        ws.cell(row, ix["سعر بيع الجملة"]).value = whole
        ws.cell(row, ix["الكمية"]).value = 0          # صفر = المخزون مبيتغيرش
        ws.cell(row, ix["قائمة السيريالات"]).value = ""
    out = io.BytesIO()
    wb.save(out)
    # الأسعار الجديدة تبقى هي الأساس للمقارنة الجاية
    newp = {e["barcode"]: final_prices(e, st["whole_add"]) for e in st["review"]["entries"] if not e["skip"]}
    for it in st["volt"]["items"]:
        if it["barcode"] in newp:
            _, it["sell"], it["whole"] = newp[it["barcode"]]
    return out.getvalue(), ws.max_row - 1


def volt_age_warning(st):
    d = datetime.strptime(st["volt"]["date"], "%Y-%m-%d %H:%M").replace(tzinfo=CAIRO)
    if datetime.now(CAIRO) - d > timedelta(hours=24):
        return ("⚠️ ملف فولت ده من " + st["volt"]["date"] +
                ". لو اشتريت بضاعة من ساعتها، صدّر ملف جديد قبل الاستيراد عشان سعر الشراء ميرجعش قديم.")
    return ""


# ---------- استقبال الرسايل ----------

PRICING_HELP = """💰 مراجعة أسعار MK
1) صدّر من فولت «تصدير Excel للاستيراد» (من غير بحث) وابعت الملف هنا.
2) انسخ قوايم أسعار التجار من الواتس والصقها هنا.
3) اكتب: مراجعة
4) عدّل لو حابب، وبعدين اكتب: موافق ← هيجيلك ملف تستورده في فولت.

/shipping 100 — شحن يلا تاجر على القطعة
/wholesale 50 — الجملة = سعر المورد + كام
/threshold 500 — نبّهني لما الفرق يوصل كام
الكل — اعرض كل الأصناف في المراجعة
/suppliers — القوايم المحفوظة
/clear — امسح قوايم الواتس"""


def handle_text(text, st):
    """بيرجع (رد، ملف أو None) أو None لو الرسالة مش تبع الأسعار"""
    t = text.strip().translate(AR_DIGITS)
    cmd = t.split()[0].lower().split("@")[0] if t else ""
    arg = re.findall(r"\d+", t)
    if cmd in ("/prices", "الأسعار", "الاسعار"):
        return PRICING_HELP, None
    if cmd in ("/shipping", "شحن"):
        if not arg:
            return f"شحن يلا تاجر دلوقتي: {st['shipping'] if st['shipping'] is not None else 'مش متحدد'}\nاكتب مثلاً: /shipping 100", None
        st["shipping"] = int(arg[0])
        if st["review"] and st["volt"]:
            build_review(st)
        return f"🚚 تمام، هزوّد {fmt(st['shipping'])}ج على كل سعر من يلا تاجر.", None
    if cmd in ("/wholesale", "جملة"):
        if arg:
            st["whole_add"] = int(arg[0])
        return f"الجملة = سعر المورد المختار + {st['whole_add']}", None
    if cmd in ("/suppliers", "التجار"):
        if not st["offers"]:
            return "مفيش قوايم واتس محفوظة.", None
        return "📥 القوايم المحفوظة:\n" + "\n".join(
            f"• {s}: {len(o['items'])} سعر ({o['date']})" for s, o in st["offers"].items()), None
    if cmd in ("/clear", "مسح"):
        st["offers"] = {}
        return "🧹 اتمسحت قوايم الواتس.", None
    if cmd in ("/all", "الكل"):
        if not st["review"]:
            return "مفيش مراجعة. اكتب: مراجعة", None
        return review_text(st, show_all=True), None
    if cmd in ("/threshold", "فرق"):
        if arg:
            st["threshold"] = int(arg[0])
            st["alerted"] = {}
        return f"🔔 هنبهك لما فرق الجملة يوصل {fmt(st['threshold'])}ج أو أكتر.", None
    if cmd in ("/review", "مراجعة", "مراجعه"):
        if not st["volt"]:
            return "ابعتلي الأول ملف «تصدير Excel للاستيراد» من فولت.", None
        build_review(st)
        return review_text(st), None
    if cmd in ("/approve", "موافق"):
        if not st["review"]:
            return "مفيش مراجعة. اكتب: مراجعة", None
        return "APPROVE", None
    edit = apply_edits(t, st)
    if edit:
        return edit, None
    found = parse_whatsapp(text)
    total = sum(len(v) for v in found.values())
    if total >= 2:
        msg = []
        for sup, rows in found.items():
            if sup:
                st["offers"][sup] = {"date": now(), "items": rows}
                msg.append(f"📥 اتسجّل {len(rows)} سعر من {sup}.")
        if None in found:
            msg.append(f"⚠️ فيه {len(found[None])} سعر مش عارف تاجرهم مين — "
                       "ابعتهم تاني واكتب اسم التاجر في أول سطر.")
        al = alerts(st)
        if al:
            msg.append("\n" + al)
        else:
            msg.append("ابعت قوايم تانية أو اكتب: مراجعة")
        return "\n".join(msg), None
    return None


def handle_document(doc, st, download):
    name = doc.get("file_name", "")
    if not name.lower().endswith(".xlsx"):
        return "ابعت ملف «تصدير Excel للاستيراد» من فولت (xlsx) — مش CSV."
    data = download(doc["file_id"])
    try:
        items = parse_volt_xlsx(data)
    except Exception as ex:
        return f"⚠️ مش قادر أقرا الملف: {ex}"
    if len(items) < 5:
        return f"⚠️ الملف فيه {len(items)} موبايل بس. صدّر من غير ما تكون عامل بحث."
    st["volt"] = {"file_id": doc["file_id"], "date": now(), "items": items}
    st["review"] = None
    return f"📦 اتسجّل ملف فولت: {len(items)} موبايل وتابلت.\nالصق قوايم الواتس، أو اكتب: مراجعة"
