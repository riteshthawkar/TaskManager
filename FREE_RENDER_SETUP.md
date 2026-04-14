# Free Render Hobby Setup

This setup matches a single-user hobby deployment:

- Render free web service
- External Postgres via `DATABASE_URL`
- Netlify scheduled pings to keep the web service awake
- In-process reminder scheduler
- Email delivery over HTTPS via Resend

## Required Render Environment Variables

```env
APP_ENV=production
APP_USERNAME=admin
APP_PASSWORD=your-login-password
SESSION_SECRET=your-random-session-secret
SESSION_HTTPS_ONLY=true

DATABASE_URL=postgresql://...
OPENAI_API_KEY=...

ENABLE_SCHEDULER=true
NOTIFICATION_CHECK_MINUTES=10

EMAIL_PROVIDER=resend
RESEND_API_KEY=re_...
EMAIL_FROM=TaskManager <onboarding@resend.dev>
NOTIFY_EMAIL=your-email@example.com
```

## Netlify Keep-Alive

Point your Netlify keep-alive service at:

```text
https://your-render-service.onrender.com/health/live
```

Use a 10-minute schedule.

## Reminder Delivery

This app now supports two delivery paths:

1. `EMAIL_PROVIDER=resend`
   Use this on free Render. It sends email over HTTPS and avoids blocked SMTP ports.
2. `EMAIL_PROVIDER=smtp`
   Use this only where outbound SMTP is allowed.

## Manual Verification

1. Log in.
2. Create a task with a deadline within the next 24 hours.
3. Open the Schedule page.
4. Click `Run Reminder Check Now`.
5. Confirm the alert summary appears.
6. Confirm the reminder email arrives in `NOTIFY_EMAIL`.

## Notes

- `EMAIL_FROM` must be a sender that your Resend account allows.
- The free Render service can still restart unexpectedly, so this is suitable for personal use, not strict production uptime.
