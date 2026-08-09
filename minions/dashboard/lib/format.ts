export function formatDuration(seconds: number): string {
  if (seconds === 0) return "-";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

export function formatTokens(tokens: number): string {
  if (tokens === 0) return "-";
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
  return tokens.toString();
}

export function formatTimestamp(ts: number): string {
  if (ts === 0) return "-";
  return new Date(ts * 1000).toLocaleString();
}

export function timeAgo(ts: number): string {
  if (ts === 0) return "-";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
