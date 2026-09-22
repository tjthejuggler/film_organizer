/* JSON fetch wrapper: consistent error handling + content types. */
export async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", ...opts };
  if (opts.body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.body);
  } else {
    delete init.body; // no body -> no JSON content-type (avoids 422 on DELETE)
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}
