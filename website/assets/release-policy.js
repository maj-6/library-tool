const PUBLIC_RELEASE_CHANNELS = new Set(["stable", "alpha", "beta", "rc"]);

export function releaseChannel(row) {
  if (!row || typeof row !== "object") return null;
  if (row.channel === null || row.channel === undefined || row.channel === "") {
    return "stable";
  }
  if (typeof row.channel !== "string") return null;
  const channel = row.channel.trim().toLowerCase();
  return channel || null;
}

export function isPublicRelease(row) {
  const channel = releaseChannel(row);
  if (!channel || !PUBLIC_RELEASE_CHANNELS.has(channel)) return false;
  const rawUrl = typeof row.url === "string" ? row.url : "";
  if (/DONOTPUBLISH/i.test(rawUrl)) return false;
  let url;
  try {
    url = new URL(rawUrl);
    if (/DONOTPUBLISH/i.test(decodeURIComponent(url.href))) return false;
  } catch {
    return false;
  }
  return url.protocol === "https:" || url.protocol === "http:";
}

export function isStableRelease(row) {
  return releaseChannel(row) === "stable";
}
