export async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

export function lateSeamSpine(storeId) {
  const q = storeId ? `?store_id=${encodeURIComponent(storeId)}` : "";
  return api(`/api/late/seam-spine${q}`);
}

export function lateSeamAttest(storeId) {
  const q = storeId ? `?store_id=${encodeURIComponent(storeId)}` : "";
  return api(`/api/late/seam-attest${q}`);
}

export function lateSeamLookup(storeId, asOf) {
  const q = `?store_id=${encodeURIComponent(storeId)}&as_of=${encodeURIComponent(asOf)}`;
  return api(`/api/late/seam-lookup${q}`);
}
