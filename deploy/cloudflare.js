// Cloudflare API:为自有服务器渠道创建 / 删除隧道与 DNS 记录。
// API Token 需要:账户 Cloudflare Tunnel 编辑;区域(000.moe)DNS 编辑、区域读取。

// cfg:{ token, accountId, zone, api }(api 为空时用官方地址,本地调试可指向模拟服务)
async function cf(cfg, method, path, body) {
  const resp = await fetch((cfg.api || "https://api.cloudflare.com/client/v4") + path, {
    method,
    headers: { authorization: `Bearer ${cfg.token}`, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!data.success) {
    const reason = (data.errors || []).map((e) => e.message).join("; ") || `HTTP ${resp.status}`;
    throw new Error(`Cloudflare API ${method} ${path.split("?")[0]}:${reason}`);
  }
  return data.result;
}

async function zoneId(cfg) {
  const zones = await cf(cfg, "GET", `/zones?name=${encodeURIComponent(cfg.zone)}`);
  if (!zones.length) throw new Error(`找不到域名 ${cfg.zone}(API Token 需要该区域的读取权限)`);
  return zones[0].id;
}

/** 建隧道(由 Cloudflare 托管配置)→ 把主机名指向网关 → 建 CNAME。失败时删除已建的隧道。 */
export async function createTunnel(cfg, slot, suffix) {
  const host = `solver-${slot}.${cfg.zone}`;
  const tunnel = await cf(cfg, "POST", `/accounts/${cfg.accountId}/cfd_tunnel`, {
    name: `turnstile-solver-${slot}-${suffix}`,
    config_src: "cloudflare",
  });
  try {
    await cf(cfg, "PUT", `/accounts/${cfg.accountId}/cfd_tunnel/${tunnel.id}/configurations`, {
      config: { ingress: [{ hostname: host, service: "http://localhost:8686" }, { service: "http_status:404" }] },
    });
    const zone = await zoneId(cfg);
    const record = { type: "CNAME", name: host, content: `${tunnel.id}.cfargotunnel.com`, proxied: true, comment: "turnstile-solver 服务器渠道" };
    // 已有同名记录(例如以前手动建过)时改为指向新隧道
    const existing = await cf(cfg, "GET", `/zones/${zone}/dns_records?name=${encodeURIComponent(host)}`);
    const dns = existing.length
      ? await cf(cfg, "PUT", `/zones/${zone}/dns_records/${existing[0].id}`, record)
      : await cf(cfg, "POST", `/zones/${zone}/dns_records`, record);
    return { host, tunnelId: tunnel.id, dnsRecordId: dns.id };
  } catch (err) {
    await cf(cfg, "DELETE", `/accounts/${cfg.accountId}/cfd_tunnel/${tunnel.id}`).catch(() => {});
    throw err;
  }
}

export async function tunnelToken(cfg, tunnelId) {
  return cf(cfg, "GET", `/accounts/${cfg.accountId}/cfd_tunnel/${tunnelId}/token`);
}

/** 删除 DNS 记录与隧道;记录或隧道已不存在时忽略。 */
export async function deleteTunnel(cfg, tunnelId, dnsRecordId) {
  const notFound = (err) => /not found|does not exist|\b404\b|1001|1003/i.test(String(err && err.message));
  if (dnsRecordId) {
    const zone = await zoneId(cfg);
    await cf(cfg, "DELETE", `/zones/${zone}/dns_records/${dnsRecordId}`).catch((err) => {
      if (!notFound(err)) throw err;
    });
  }
  if (tunnelId) {
    // 先断开仍在线的连接器,否则隧道删除会失败
    await cf(cfg, "DELETE", `/accounts/${cfg.accountId}/cfd_tunnel/${tunnelId}/connections`).catch(() => {});
    await cf(cfg, "DELETE", `/accounts/${cfg.accountId}/cfd_tunnel/${tunnelId}`).catch((err) => {
      if (!notFound(err)) throw err;
    });
  }
}
