import { getJson } from "./api";

export type JobState<T> = { id: number; kind: string; status: string; error: string; result: T | null };

/** Poll an onboarding job (identify, verify, SNMP apply, scan) until it is done or failed. */
export async function waitForJob<T>(id: number, opts: { every?: number; tries?: number; onTick?: (j: JobState<T>) => void } = {}): Promise<JobState<T>> {
  const every = opts.every ?? 1500;
  for (let i = 0; i < (opts.tries ?? 80); i++) {
    const job = await getJson<JobState<T>>(`/api/v1/onboard/jobs/${id}`);
    opts.onTick?.(job);
    if (job.status === "done" || job.status === "error") return job;
    await new Promise((r) => setTimeout(r, every));
  }
  throw new Error("The background job is taking long; check ./forgesre jobs on the appliance.");
}
