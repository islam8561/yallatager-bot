// وسيط بوت MK على Cloudflare
// تيليجرام يبعت الرسالة هنا على طول ← نحفظها ← نشغّل البوت على GitHub فوراً
// المطلوب في إعدادات الـ Worker:
//   Variables/Secrets: TELEGRAM_TOKEN, GH_TOKEN, BOT_SECRET, REPO (islam8561/yallatager-bot)
//   KV binding باسم Q
export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const keyOk = req.headers.get("X-Key") === env.BOT_SECRET;

    // 1) تيليجرام بيبعت الرسايل هنا
    if (url.pathname === "/tg" && req.method === "POST") {
      if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.BOT_SECRET)
        return new Response("forbidden", { status: 403 });
      const u = await req.json();
      const id = String(u.update_id).padStart(12, "0");
      await env.Q.put("u:" + id, JSON.stringify(u), { expirationTtl: 3 * 86400 });
      ctx.waitUntil(fetch(`https://api.github.com/repos/${env.REPO}/dispatches`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "mk-price-bot",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ event_type: "tg" }),
      }));
      return new Response("ok");
    }

    // 2) البوت على GitHub بيسحب الرسايل من هنا
    if (url.pathname === "/updates") {
      if (!keyOk) return new Response("forbidden", { status: 403 });
      const offset = Number(url.searchParams.get("offset") || 0);
      const list = await env.Q.list({ prefix: "u:" });
      const out = [];
      for (const k of list.keys) {
        const id = Number(k.name.slice(2));
        if (id < offset) { await env.Q.delete(k.name); continue; }
        const v = await env.Q.get(k.name);
        if (v) out.push(JSON.parse(v));
      }
      out.sort((a, b) => a.update_id - b.update_id);
      return Response.json({ ok: true, result: out });
    }

    // 3) تشغيل مرة واحدة: افتح الرابط ده/setup?key=... من المتصفح
    if (url.pathname === "/setup") {
      if (url.searchParams.get("key") !== env.BOT_SECRET) return new Response("forbidden", { status: 403 });
      const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/setWebhook`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: `${url.origin}/tg`,
          secret_token: env.BOT_SECRET,
          allowed_updates: ["message", "my_chat_member"],
        }),
      });
      const t = await r.json();
      return new Response(t.ok ? "✅ تمام، البوت اتربط بالوسيط" : "❌ " + JSON.stringify(t),
        { headers: { "Content-Type": "text/plain; charset=utf-8" } });
    }

    return new Response("MK bot relay");
  },
};
