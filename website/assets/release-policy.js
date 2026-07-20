const PUBLIC_RELEASE_CHANNELS = new Set(["stable", "alpha", "beta", "rc"]);

export function releaseChannel(row) {
  if (!row || typeof row !== "object") return null;
  if (row.channel === null || row.channel === undefined || row.channel === "") {
    return "stable";
  }
  if (typeof row.channel !== "string") return null;
  const channel = row.channel.trim().toLowerCase();
  return channel || "stable";
}

export function isPublicRelease(row) {
  const channel = releaseChannel(row);
  if (!channel || !PUBLIC_RELEASE_CHANNELS.has(channel)) return false;
  const url = typeof row.url === "string" ? row.url : "";
  return !/DONOTPUBLISH/i.test(url);
}

export function isStableRelease(row) {
  return releaseChannel(row) === "stable";
}
