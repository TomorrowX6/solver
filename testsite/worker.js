// Turnstile 测试站点(Cloudflare Worker)
//
//   GET  /         嵌入真实 Turnstile 组件的页面,浏览器里可手动验证
//   POST /verify   {"token": "..."} 或表单 cf-turnstile-response → 调 siteverify 校验,并核对域名与 action
//   GET  /config   公开的 sitekey 与 action,供端到端测试读取
//
// 变量:SITEKEY(wrangler.toml [vars]);密钥:TURNSTILE_SECRET(wrangler secret put 或控制台添加)

const SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
const ACTION = "e2e";

const html = (sitekey) => `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Turnstile 测试站点</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>
  body { font: 15px/1.6 system-ui, sans-serif; max-width: 560px; margin: 48px auto; padding: 0 16px; color: #222; }
  button { font: inherit; padding: 6px 16px; margin-top: 12px; }
  pre { background: #f4f4f5; padding: 12px; border-radius: 8px; white-space: pre-wrap; word-break: break-all; }
</style>
</head>
<body>
<h1>Turnstile 测试站点</h1>
${sitekey
    ? `<p>完成下面的验证后点「校验」,后端会调用 siteverify 检查 token。</p>
<div class="cf-turnstile" data-sitekey="${sitekey}" data-action="${ACTION}"></div>
<button id="verify">校验</button>`
    : `<p>尚未配置 SITEKEY。</p>`}
<pre id="out"></pre>
<script>
  document.getElementById("verify")?.addEventListener("click", async () => {
    const token = document.querySelector("[name=cf-turnstile-response]")?.value;
    const r = await fetch("/verify", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ token }) });
    document.getElementById("out").textContent = JSON.stringify(await r.json(), null, 2);
  });
</script>
</body>
</html>`;

async function verify(request, env, url) {
  if (!env.TURNSTILE_SECRET) {
    return Response.json({ ok: false, error: "TURNSTILE_SECRET 未配置" }, { status: 500 });
  }
  const type = request.headers.get("content-type") || "";
  const token = type.includes("application/json")
    ? (await request.json()).token
    : (await request.formData()).get("cf-turnstile-response");
  if (!token) {
    return Response.json({ ok: false, error: "缺少 token" }, { status: 400 });
  }

  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET);
  form.append("response", token);
  const outcome = await (await fetch(SITEVERIFY, { method: "POST", body: form })).json();

  // 真实站点除了 success,还应核对 token 是签给本域名、本 action 的
  const ok = outcome.success === true && outcome.hostname === url.hostname && outcome.action === ACTION;
  return Response.json(
    {
      ok,
      expected: { hostname: url.hostname, action: ACTION },
      token: { length: token.length, prefix: token.slice(0, 12) },
      siteverify: outcome,
    },
    { status: ok ? 200 : 400 },
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/") {
      return new Response(html(env.SITEKEY), { headers: { "content-type": "text/html; charset=utf-8" } });
    }
    if (request.method === "GET" && url.pathname === "/config") {
      return Response.json({ sitekey: env.SITEKEY || null, action: ACTION });
    }
    if (request.method === "POST" && url.pathname === "/verify") {
      return verify(request, env, url);
    }
    return new Response("Not found", { status: 404 });
  },
};
