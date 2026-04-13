import { pingTarget, readSchedulePayload } from "./_lib/ping-target.mjs";

export default async (request) => {
  const payload = await readSchedulePayload(request);
  const result = await pingTarget({
    source: "scheduled",
    nextRun: payload.next_run ?? null
  });

  if (!result.ok) {
    throw new Error(
      `Scheduled keep-alive ping failed (${result.status ?? "network"}): ${result.error ?? "unexpected response"}`
    );
  }
};

export const config = {
  schedule: "*/10 * * * *"
};
