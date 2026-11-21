export function formatPlanUsage(
  plan: string | undefined,
  usagePercent: number | null | undefined,
): string {
  const usage =
    usagePercent === undefined || usagePercent === null ? 'unknown' : `${usagePercent}% used`;
  return `Plan: ${plan ?? 'unknown'} · weekly allowance ${usage}`;
}
