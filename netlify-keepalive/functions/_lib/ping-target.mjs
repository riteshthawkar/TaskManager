const DEFAULT_TIMEOUT_MS = 10000;
const BODY_PREVIEW_LIMIT = 200;
const ALLOWED_METHODS = new Set(["GET", "HEAD"]);

function parsePositiveInt(value, fallback) {
  const parsed = Number.parseInt(value ?? "", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function getTargetUrl() {
  const value = process.env.KEEPALIVE_TARGET_URL;
  if (!value) {
    throw new Error("KEEPALIVE_TARGET_URL is required");
  }

  let target;
  try {
    target = new URL(value);
  } catch {
    throw new Error("KEEPALIVE_TARGET_URL must be a valid absolute URL");
  }

  if (!["http:", "https:"].includes(target.protocol)) {
    throw new Error("KEEPALIVE_TARGET_URL must use http or https");
  }

  return target.toString();
}

function getMethod() {
  const value = (process.env.KEEPALIVE_METHOD || "GET").toUpperCase();
  if (!ALLOWED_METHODS.has(value)) {
    throw new Error("KEEPALIVE_METHOD must be GET or HEAD");
  }

  return value;
}

function buildHeaders() {
  const headers = {
    "user-agent": "taskmanager-netlify-keepalive/1.0"
  };

  const extraHeaderName = process.env.KEEPALIVE_HEADER_NAME;
  const extraHeaderValue = process.env.KEEPALIVE_HEADER_VALUE;

  if (extraHeaderName && extraHeaderValue) {
    headers[extraHeaderName] = extraHeaderValue;
  }

  return headers;
}

async function readBodyPreview(response, method) {
  if (method === "HEAD") {
    return "";
  }

  try {
    const text = await response.text();
    return text.slice(0, BODY_PREVIEW_LIMIT);
  } catch {
    return "";
  }
}

export async function readSchedulePayload(request) {
  try {
    return await request.json();
  } catch {
    return {};
  }
}

export async function pingTarget({ source, nextRun = null } = {}) {
  const targetUrl = getTargetUrl();
  const method = getMethod();
  const timeoutMs = parsePositiveInt(process.env.KEEPALIVE_TIMEOUT_MS, DEFAULT_TIMEOUT_MS);
  const controller = new AbortController();
  const startedAt = Date.now();
  const timeout = setTimeout(() => controller.abort(new Error("keep-alive ping timed out")), timeoutMs);

  try {
    const response = await fetch(targetUrl, {
      method,
      headers: buildHeaders(),
      redirect: "follow",
      signal: controller.signal
    });

    const durationMs = Date.now() - startedAt;
    const bodyPreview = await readBodyPreview(response.clone(), method);
    const result = {
      ok: response.ok,
      source,
      next_run: nextRun,
      method,
      target_url: targetUrl,
      status: response.status,
      duration_ms: durationMs,
      body_preview: bodyPreview
    };

    const logger = response.ok ? console.log : console.error;
    logger(JSON.stringify(result));
    return result;
  } catch (error) {
    const result = {
      ok: false,
      source,
      next_run: nextRun,
      method,
      target_url: targetUrl,
      status: null,
      duration_ms: Date.now() - startedAt,
      error: error instanceof Error ? error.message : "unknown error"
    };

    console.error(JSON.stringify(result));
    return result;
  } finally {
    clearTimeout(timeout);
  }
}
