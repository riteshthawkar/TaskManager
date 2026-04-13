import { pingTarget } from "./_lib/ping-target.mjs";

export default async () => {
  const result = await pingTarget({ source: "manual" });

  return Response.json(result, {
    status: result.ok ? 200 : 502
  });
};
