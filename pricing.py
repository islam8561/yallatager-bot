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
    st.setdefault("whole_add", 100)   # الجملة = أرخص مورد + كام
    st.setdefault("retail_add", 500)  # القطاعي = أرخص مورد + كام
    st.setdefault("threshold", 500)   # أقل فرق يستاهل تنبيه
    st.setdefault("applied", {})      # باركود -> [قطاعي, جملة] آخر أسعار البوت حطها (عشان نعرف اللي عدّلته بإيدك)
    st.setdefault("min_change", 50)   # أقل فرق يستاهل نغيّر السعر عشانه
    if st.get("rule_v") != 2:         # القاعدة: قطاعي = أرخص مورد + 500، جملة = أرخص مورد + 100
        st.update(rule_v=2, retail_add=500, whole_add=100)
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
            "buy": float(r[ix["سعر الشراء"]] or 0),   # للعرض بس — عمره ما بيتغير
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
    st["manual"] = []
    stock = (st.get("stock") or {}).get("items")
    for it in volt:
        if not it["barcode"]:
            no_barcode.append(it["name"])
            continue
        if stock is not None and stock.get(it["barcode"], 0) <= 0:
            continue          # مخزونه صفر: مش محتاجين نقارنه
        same = [o for o in offers if o["model"] == it["model"] and o["is5g"] == it["is5g"]
                and (it["brand"] is None or o["brand"] in (None, it["brand"]))]
        if it["storage"] is None:
            (no_storage if same else no_offer).append(it["name"])
            continue
        exact = [o for o in same if o["storage"] == it["storage"] and o["ram"] == it["ram"]]
        if not exact:
            no_offer.append(it["name"])
            continue
        ap = st["applied"].get(it["barcode"])
        if ap and (round(it["sell"]) != round(ap[0]) or round(it["whole"]) != round(ap[1])):
            st["manual"].append(it["name"])   # إنت غيّرت سعره بإيدك في فولت: مش هنلمسه
            continue
        best = {}
        for o in exact:      # أرخص سعر لكل تاجر
            if o["supplier"] not in best or o["price"] < best[o["supplier"]]["price"]:
                best[o["supplier"]] = o
        opts = sorted(({"supplier": s, "price": o["price"],
                        "cost": o["price"] + (ship if o["is_yt"] else 0), "is_yt": o["is_yt"]}
                       for s, o in best.items()), key=lambda x: x["cost"])
        entries.append({"barcode": it["barcode"], "name": it["name"], "sell": it["sell"],
                        "buy": it.get("buy", 0),
                        "stock": stock.get(it["barcode"]) if stock is not None else None,
                        "whole": it["whole"], "opts": opts, "pick": opts[0]["supplier"],
                        "new_sell": None, "new_whole": None, "skip": False})
    return entries, no_storage, no_offer, no_barcode


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
        cost, sell, whole = final_prices(e, st)
        d = whole - e["whole"]
        if not e["whole"] or abs(d) < st["threshold"]:
            continue
        if st["alerted"].get(e["barcode"]) == cost:   # اتبعت قبل كده بنفس السعر
            continue
        st["alerted"][e["barcode"]] = cost
        e["n"] = "🔺" if d > 0 else "🔻"
        lines.append(one_line(e, st) + ("\n    ⚠️ أقل من سعر شرائك (" + fmt(e["buy"]) + ") — مش هيتغير إلا لو قلت"
                                       if status(e, st) == "loss" else ""))
    if not lines:
        return None
    head = f"🔔 فرق {fmt(st['threshold'])}ج أو أكتر في {len(lines)} صنف:\n"
    if st["shipping"] is None:
        head += "⚠️ شحن يلا تاجر مش متحدد (محسوب صفر) — ابعت مثلاً: /shipping 100\n"
    return head + "\n" + "\n".join(lines) + "\n\nاكتب: مراجعة"


def picked(e):
    return next((o for o in e["opts"] if o["supplier"] == e["pick"]), e["opts"][0])


def round10(v):
    return int(-(-v // 10) * 10)   # نقرّب لأقرب 10 لفوق


def rule_prices(e, st):
    """الأسعار حسب القاعدة قبل أي تعديل منك"""
    cost = picked(e)["cost"]
    return cost, round10(cost + st["retail_add"]), round10(cost + st["whole_add"])


def final_prices(e, st):
    cost, sell, whole = rule_prices(e, st)
    if e["new_sell"] is not None:
        sell = e["new_sell"]
    if e["new_whole"] is not None:
        whole = e["new_whole"]
    return cost, sell, whole


def status(e, st):
    """changed / same / loss"""
    cost, sell, whole = final_prices(e, st)
    if abs(sell - e["sell"]) < st["min_change"] and abs(whole - e["whole"]) < st["min_change"] \
            and e["new_sell"] is None and e["new_whole"] is None:
        return "same"
    if e.get("buy") and min(sell, whole) < e["buy"] and not e.get("force"):
        return "loss"
    return "changed"


def short(name, n=38):
    return name if len(name) <= n else name[:n - 1] + "…"


def one_line(e, st):
    cost, sell, whole = final_prices(e, st)
    p = picked(e)
    sup = f"{p['supplier']} {fmt(p['price'])}" + (f"+{fmt(p['cost'] - p['price'])}" if p["cost"] != p["price"] else "")
    def arrow(old, new):
        return fmt(new) if round(old) == round(new) else f"{fmt(old) if old else '—'} ← {fmt(new)}"
    return (f"{e['n']}) {short(e['name'])}\n"
            f"    قطاعي {arrow(e['sell'], sell)} • جملة {arrow(e['whole'], whole)}  ({sup})")


def money_diff(v):
    return f"+{fmt(v)}" if v >= 0 else f"خسارة {fmt(-v)} 🔴"


def card(e, st, head):
    """كارت صنف واحد بكلام واضح"""
    cost, sell, whole = final_prices(e, st)
    stock = f" — عندك {fmt(e['stock'])} قطعة" if e.get("stock") else ""
    lines = [f"{head} {e['name']}{stock}"]
    lines.append(f"   💰 سعرك دلوقتي: قطاعي {fmt(e['sell'])} • جملة {fmt(e['whole']) if e['whole'] else 'مش متحدد'}")
    ch = []
    if round(whole) != round(e["whole"]):
        ch.append(f"جملة {fmt(whole)}")
    if round(sell) != round(e["sell"]):
        ch.append(f"قطاعي {fmt(sell)}")
    if ch:
        same = "" if round(sell) != round(e["sell"]) else " (القطاعي زي ما هو)"
        lines.append(f"   ✏️ المقترح: {' • '.join(ch)}{same}")
    else:
        lines.append("   ✏️ المقترح: مفيش تغيير")
    if sell < cost:
        lines.append(f"   ⚠️ القطاعي بتاعك أقل من سعر المورد النهارده ({fmt(cost)})")
    if e.get("buy"):
        lines.append(f"   📦 اشتريته بـ {fmt(e['buy'])} — ربحك لو وافقت: "
                     f"قطاعي {money_diff(sell - e['buy'])} • جملة {money_diff(whole - e['buy'])}")
    opts = []
    for o in e["opts"]:
        p = fmt(o["price"]) + (f" + {fmt(o['cost'] - o['price'])} شحن" if o["cost"] != o["price"] else "")
        opts.append(("✅ " if o["supplier"] == e["pick"] else "") + f"{o['supplier']} {p}")
    lines.append("   🏪 الموردين النهارده: " + " • ".join(opts))
    return "\n".join(lines)


def entry_text(e, st):
    if e["skip"]:
        return f"{e['n']}) ⏭️ {e['name']}  (متخطّي — مش هيتعدل)"
    return card(e, st, f"{e['n']})")


LEGEND = ("ℹ️ «سعرك دلوقتي» = اللي في فولت • «المقترح» = اللي هيتكتب في فولت لو وافقت\n"
          "«اشتريته بـ» = سعر الشراء الأصلي في فولت (مبيتغيرش) • ✅ = المورد اللي الجملة محسوبة عليه")


def review_text(st, show_all=False):
    r = st["review"]
    es = [e for e in r["entries"]]
    changed = [e for e in es if not e["skip"] and status(e, st) == "changed"]
    loss = [e for e in es if not e["skip"] and status(e, st) == "loss"]
    skipped = [e for e in es if e["skip"]]
    same = [e for e in es if not e["skip"] and status(e, st) == "same"]
    out = [f"📋 الأسعار الجديدة — {r['date']}",
           f"القاعدة: قطاعي = أرخص مورد + {fmt(st['retail_add'])} • جملة = أرخص مورد + {fmt(st['whole_add'])}"
           + (f" • شحن يلا تاجر {fmt(st['shipping'])}" if st["shipping"] is not None else " • ⚠️ شحن يلا تاجر مش متحدد (/shipping 100)")]
    if changed:
        out.append(f"\n✅ هيتغيروا ({len(changed)}):")
        out += [one_line(e, st) for e in changed]
    else:
        out.append("\n✅ مفيش أسعار محتاجة تتغير.")
    if loss:
        out.append(f"\n⚠️ مش هيتغيروا إلا لو قلت ({len(loss)}) — السعر الجديد أقل من سعر شرائك:")
        for e in loss:
            cost, sell, whole = final_prices(e, st)
            out.append(f"{e['n']}) {short(e['name'])}\n"
                       f"    اشتريته بـ {fmt(e['buy'])} • السوق النهارده {fmt(cost)} ← قطاعي {fmt(sell)} • جملة {fmt(whole)}\n"
                       f"    لو عايز تنزّل اكتب: {e['n']} نزّل")
    if skipped:
        out.append(f"\n⏭️ متخطّيين: " + "، ".join(f"{e['n']}" for e in skipped))
    if show_all and same:
        out.append("\n➖ مفيش تغيير:")
        out += [one_line(e, st) for e in same]
    elif same:
        out.append(f"\n➖ {len(same)} صنف أسعارهم مظبوطة (اكتب «الكل» لو عايز تشوفهم)")
    if st.get("manual"):
        out.append(f"\n✋ سايبهم زي ما هم لأنك عدّلت سعرهم بإيدك في فولت ({len(st['manual'])}). "
                   "لو عايزهم يرجعوا للقاعدة اكتب: /auto")
    if r["no_storage"] or r["no_offer"] or r["no_barcode"]:
        out.append("\n— أصناف مش هقدر أسعّرها —")
    if r["no_offer"]:
        out.append("مالقتلهمش سعر عند أي مورد: " + "، ".join(short(n, 30) for n in r["no_offer"]))
    if r["no_storage"]:
        out.append("اسمهم من غير سعة (ضيف السعة في فولت): " + "، ".join(short(n, 30) for n in r["no_storage"]))
    if r["no_barcode"]:
        out.append("من غير باركود: " + "، ".join(short(n, 30) for n in r["no_barcode"]))
    out.append("\n👍 لو تمام اكتب: موافق\n"
               "✏️ تعديل (اختياري): 3 بيع 12500 • 3 جملة 12000 • 3 تخطي • 3 العزيزي • تفاصيل 3")
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
    m0 = re.match(r"^(تفاصيل|تفصيل)\s+(\d+)$", lines[0]) if lines else None
    if m0 and len(lines) == 1:
        e = {x["n"]: x for x in r["entries"]}.get(int(m0.group(2)))
        return card(e, st, f"{e['n']})") if e else "مفيش صنف بالرقم ده"
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
        elif rest in ("تخطي", "تخطى", "شيل", "سيبه"):
            e["skip"] = True
        elif rest in ("نزّل", "نزل", "نزله", "نزّله", "غيّر", "غير"):
            e["force"] = True
            e["skip"] = False
        elif rest in ("رجوع", "رجع"):
            e.update(skip=False, force=False, new_sell=None, new_whole=None, pick=e["opts"][0]["supplier"])
        else:
            sup = find_supplier(e, rest.replace("يلا", YT) if rest == "يلا" else rest)
            if not sup:
                errors.append(f"{n}: مش فاهم «{rest}». التجار المتاحين: " + "، ".join(o["supplier"] for o in e["opts"]))
                continue
            e["pick"] = sup
        done.append(e)
    uniq = list({e["n"]: e for e in done}.values())
    out = ["👌 اتعدّل:"] + [one_line(e, st) + ("  ⏭️ متخطّي" if e["skip"] else "") for e in uniq]
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
    changes, done, rule_of = {}, {}, {}
    for e in st["review"]["entries"]:
        if e["skip"]:
            continue
        if status(e, st) != "changed":
            continue
        cost, sell, whole = final_prices(e, st)
        changes[e["barcode"]] = (e, round(sell), round(whole))
    for row in range(ws.max_row, 1, -1):
        bc = str(ws.cell(row, ix["الباركود"]).value or "").strip()
        if bc not in changes:
            ws.delete_rows(row)
            continue
        e, sell, whole = changes.pop(bc)
        done[bc] = (sell, whole)
        if e["new_sell"] is not None or e["new_whole"] is not None:
            # سعر كتبته بإيدك: نسجّل سعر القاعدة، فالبوت يعتبره «متعدّل بإيدك» وميرجعهوش
            _, rs, rw = rule_prices(e, st)
            rule_of[bc] = (rs, rw)
        ws.cell(row, ix["سعر البيع"]).value = sell
        ws.cell(row, ix["سعر بيع الجملة"]).value = whole
        ws.cell(row, ix["الكمية"]).value = 0          # صفر = المخزون مبيتغيرش
        ws.cell(row, ix["قائمة السيريالات"]).value = ""
    out = io.BytesIO()
    wb.save(out)
    # الأسعار الجديدة تبقى هي الأساس للمقارنة الجاية
    for it in st["volt"]["items"]:
        if it["barcode"] in done:
            it["sell"], it["whole"] = done[it["barcode"]]
            st["applied"][it["barcode"]] = list(rule_of.get(it["barcode"], done[it["barcode"]]))
    return out.getvalue(), ws.max_row - 1


def volt_age_warning(st):
    d = datetime.strptime(st["volt"]["date"], "%Y-%m-%d %H:%M").replace(tzinfo=CAIRO)
    if datetime.now(CAIRO) - d > timedelta(hours=24):
        return ("⚠️ ملف فولت ده من " + st["volt"]["date"] +
                ". لو اشتريت بضاعة من ساعتها، صدّر ملف جديد قبل الاستيراد عشان سعر الشراء ميرجعش قديم.")
    return ""


# ---------- استقبال الرسايل ----------

PRICING_HELP = """💰 مراجعة أسعار MK
1) صدّر من فولت «تصدير Excel للاستيراد» و«تصدير (CSV)» (من غير بحث) وابعت الملفين هنا.
2) انسخ قوايم أسعار التجار من الواتس والصقها هنا.
3) اكتب: مراجعة
4) عدّل لو حابب، وبعدين اكتب: موافق ← هيجيلك ملف تستورده في فولت.

/shipping 100 — شحن يلا تاجر على القطعة
/retail 500 — القطاعي = أرخص مورد + كام
/wholesale 100 — الجملة = أرخص مورد + كام
/auto — رجّع الأصناف اللي عدّلتها بإيدك للقاعدة
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
    if cmd in ("/retail", "قطاعي"):
        if arg:
            st["retail_add"] = int(arg[0])
        return f"القطاعي = أرخص مورد + {fmt(st['retail_add'])}", None
    if cmd in ("/auto",):
        st["applied"] = {}
        return "🔄 كل الأصناف رجعت تمشي على القاعدة (حتى اللي عدّلتها بإيدك).", None
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
    if name.lower().endswith(".csv"):
        import csv
        data = download(doc["file_id"]).decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(data)))
        if not rows or "المخزون" not in rows[0] or "الباركود" not in rows[0]:
            return "⚠️ ده مش ملف «تصدير (CSV)» بتاع فولت."
        if len(rows) < 20:
            return f"⚠️ الملف فيه {len(rows)} صنف بس. صدّر من غير ما تكون عامل بحث، والتصنيف على «الكل»."
        items = {}
        for r in rows:
            bc = (r.get("الباركود") or "").strip()
            try:
                q = float((r.get("المخزون") or "0").translate(AR_DIGITS))
            except ValueError:
                q = 0
            if bc:
                items[bc] = q
        st["stock"] = {"date": now(), "items": items}
        if st.get("review") and st.get("volt"):
            build_review(st)
        n = sum(1 for q in items.values() if q > 0)
        return f"📊 اتسجّل المخزون: {n} صنف ليهم مخزون. هقارن الموجود بس.\nاكتب: مراجعة"
    if not name.lower().endswith(".xlsx"):
        return "ابعت ملف «تصدير Excel للاستيراد» (xlsx) أو «تصدير (CSV)» من فولت."
    data = download(doc["file_id"])
    try:
        items = parse_volt_xlsx(data)
    except Exception as ex:
        return f"⚠️ مش قادر أقرا الملف: {ex}"
    if len(items) < 5:
        return f"⚠️ الملف فيه {len(items)} موبايل بس. صدّر من غير ما تكون عامل بحث."
    st["volt"] = {"file_id": doc["file_id"], "date": now(), "items": items}
    st["review"] = None
    return (f"📦 اتسجّل ملف فولت: {len(items)} موبايل وتابلت.\n"
            + ("" if st.get("stock") else "ابعت كمان ملف «تصدير (CSV)» عشان أعرف المخزون وأقارن الموجود بس.\n")
            + "الصق قوايم الواتس، أو اكتب: مراجعة")
