export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(import.meta.env.BASE_URL + 'api/' + path.replace(/^\/+/, ''), {
    ...options, headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!response.ok) { throw new Error(await response.text() || `HTTP ${response.status}`); }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
