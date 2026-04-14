# Netlify Keep-Alive Service

This is a small Node-based Netlify service that keeps the main Render app warm by pinging its liveness endpoint every 10 minutes.

## Will this work?

Yes, this pattern works technically:

- Netlify Scheduled Functions are available on all pricing plans.
- A scheduled function can send an outbound HTTP request to your Render app.
- Render Free web services only spin down after 15 minutes with no inbound traffic, so a 10-minute ping is enough to reset the timer.

The important limit is on the Render side:

- Keeping one Free Render web service awake for a full month will consume almost all of the workspace's 750 free instance hours.
- This is viable only if that Render service is effectively the only Free web service you need running all month.
- If you want predictable uptime, upgrading the main Render app to a paid plan is still the cleaner solution.

## Files

- `functions/ping-render.mjs`: scheduled function that performs the keep-alive ping
- `functions/ping-now.mjs`: regular function for manual testing
- `scripts/smoke-test.mjs`: local verification script

## Deployment on Netlify

1. Create a new Netlify site from this repository.
2. Set the site base directory to `netlify-keepalive`.
3. Keep the publish directory as `public`.
4. Set these environment variables in Netlify:
   - `KEEPALIVE_TARGET_URL=https://your-render-service.onrender.com/health/live`
   - `KEEPALIVE_METHOD=GET`
   - `KEEPALIVE_TIMEOUT_MS=10000`
5. Deploy the site from your production branch.

## Manual test after deploy

Call:

```text
/.netlify/functions/ping-now
```

That endpoint returns JSON with the status and latency from the latest ping attempt.

## Notes

- The scheduled function runs every 10 minutes in UTC.
- Scheduled functions only run on Netlify published deploys.
- Scheduled functions cannot be called directly by URL in production, so the `ping-now` function is included for manual checks.
- Use your Render app's `/health/live` endpoint for keep-alive pings. Reserve `/health` for readiness checks that verify the database too.
