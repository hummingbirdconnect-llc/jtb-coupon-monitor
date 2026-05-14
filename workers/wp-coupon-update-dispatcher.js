const REPO_OWNER = "hummingbirdconnect-llc";
const REPO_NAME = "jtb-coupon-monitor";
const WORKFLOW_FILE = "wp-coupon-update.yml";
const DEFAULT_REF = "main";

const ALLOWED_PAGES = {
  yakushimafan: new Set(["his-coupon", "jtb-first", "knt-coupon"]),
  welltrip: new Set([
    "his-coupon",
    "jtb-coupon",
    "jtb-domestic-coupon",
    "jtb-overseas-coupon",
    "jtb-shinkansen-coupon",
    "jtb-first-coupon",
    "knt-coupon",
  ]),
};

const ACTIONS_URL =
  `https://github.com/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_FILE}`;

function corsHeaders(request, env) {
  const requestOrigin = request.headers.get("Origin") || "*";
  const allowedOrigins = (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean);
  const origin =
    allowedOrigins.length === 0 || allowedOrigins.includes(requestOrigin)
      ? requestOrigin
      : allowedOrigins[0];

  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Admin-Key",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function jsonResponse(request, env, status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...corsHeaders(request, env),
    },
  });
}

function validatePayload(payload) {
  const siteId = String(payload.site_id || "");
  const pageSlug = String(payload.page_slug || "");
  const dryRun = payload.dry_run === true || payload.dry_run === "true";
  const requestId = String(payload.request_id || crypto.randomUUID()).slice(0, 80);

  if (!Object.prototype.hasOwnProperty.call(ALLOWED_PAGES, siteId)) {
    return { ok: false, status: 400, error: "invalid_site_id" };
  }
  if (!ALLOWED_PAGES[siteId].has(pageSlug)) {
    return { ok: false, status: 400, error: "invalid_page_slug" };
  }

  return { ok: true, siteId, pageSlug, dryRun, requestId };
}

async function dispatchWorkflow(env, input) {
  const ref = env.GITHUB_REF || DEFAULT_REF;
  const url =
    `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}` +
    `/actions/workflows/${WORKFLOW_FILE}/dispatches`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "coupon-dashboard-dispatcher",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      ref,
      inputs: {
        site_id: input.siteId,
        page_slug: input.pageSlug,
        dry_run: String(input.dryRun),
        request_id: input.requestId,
      },
    }),
  });

  if (response.status === 204) {
    return { ok: true, ref };
  }

  const text = await response.text();
  return {
    ok: false,
    status: response.status,
    error: text.slice(0, 500),
    ref,
  };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders(request, env) });
    }

    if (request.method !== "POST") {
      return jsonResponse(request, env, 405, { ok: false, error: "method_not_allowed" });
    }

    if (!env.GITHUB_TOKEN || !env.ADMIN_KEY) {
      return jsonResponse(request, env, 500, { ok: false, error: "worker_not_configured" });
    }

    const adminKey = request.headers.get("X-Admin-Key") || "";
    if (adminKey !== env.ADMIN_KEY) {
      return jsonResponse(request, env, 401, { ok: false, error: "invalid_admin_key" });
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return jsonResponse(request, env, 400, { ok: false, error: "invalid_json" });
    }

    const input = validatePayload(payload);
    if (!input.ok) {
      return jsonResponse(request, env, input.status, { ok: false, error: input.error });
    }

    const dispatched = await dispatchWorkflow(env, input);
    if (!dispatched.ok) {
      return jsonResponse(request, env, 502, {
        ok: false,
        error: "github_dispatch_failed",
        github_status: dispatched.status,
        detail: dispatched.error,
      });
    }

    return jsonResponse(request, env, 200, {
      ok: true,
      request_id: input.requestId,
      site_id: input.siteId,
      page_slug: input.pageSlug,
      dry_run: input.dryRun,
      actions_url: ACTIONS_URL,
      ref: dispatched.ref,
    });
  },
};
