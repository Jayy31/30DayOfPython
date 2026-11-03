import "server-only";

// Server-side calls to the engine API. The token never reaches the browser.
export class ApiError extends Error {}

export async function api<T>(path: string, params?: Record<string, string | undefined>): Promise<T> {
  const base = process.env.ENGINE_API_URL || "http://127.0.0.1:8000";
  const url = new URL(path, base);
  for (const [k, v] of Object.entries(params || {})) if (v) url.searchParams.set(k, v);
  const headers: Record<string, string> = {};
  if (process.env.ENGINE_API_TOKEN) headers.Authorization = `Bearer ${process.env.ENGINE_API_TOKEN}`;
  let res: Response;
  try {
    res = await fetch(url, { headers, cache: "no-store", signal: AbortSignal.timeout(8000) });
  } catch {
    throw new ApiError(`Engine API unreachable at ${base}`);
  }
  if (!res.ok) throw new ApiError(`Engine API ${res.status} on ${path}`);
  return (await res.json()) as T;
}
