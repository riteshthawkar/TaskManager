import http from "node:http";
import pingScheduled from "../functions/ping-render.mjs";
import pingNow from "../functions/ping-now.mjs";

async function main() {
  let requestCount = 0;

  const server = http.createServer((req, res) => {
    requestCount += 1;
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok", path: req.url }));
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();

  if (!address || typeof address === "string") {
    throw new Error("Failed to start local smoke server");
  }

  process.env.KEEPALIVE_TARGET_URL = `http://127.0.0.1:${address.port}/health`;
  process.env.KEEPALIVE_METHOD = "GET";
  process.env.KEEPALIVE_TIMEOUT_MS = "3000";

  try {
    await pingScheduled(
      new Request("http://localhost/.netlify/functions/ping-render", {
        method: "POST",
        body: JSON.stringify({ next_run: "2026-04-13T12:10:00.000Z" })
      })
    );

    const response = await pingNow();
    const payload = await response.json();

    if (response.status !== 200) {
      throw new Error(`Manual function returned ${response.status}`);
    }

    if (!payload.ok || payload.status !== 200) {
      throw new Error(`Unexpected ping payload: ${JSON.stringify(payload)}`);
    }

    if (requestCount < 2) {
      throw new Error(`Expected at least 2 keep-alive requests, received ${requestCount}`);
    }

    console.log("Netlify keep-alive smoke test passed");
  } finally {
    server.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
