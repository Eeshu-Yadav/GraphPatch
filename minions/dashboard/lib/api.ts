const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8111";

export interface TaskData {
  task_id: string;
  repo_id: string;
  ticket_id: string;
  title: string;
  body: string;
  task_type: string;
  source: string;
  requester: string;
  priority: number;
  open_pr: boolean;
  status: string;
  created_at: number;
  started_at: number;
  completed_at: number;
  pr_url: string;
  error: string;
  total_tokens: number;
  total_duration: number;
  nodes_executed: string[];
}

export interface StatsData {
  total: number;
  counts: Record<string, number>;
  success_rate: number;
  avg_tokens: number;
  avg_duration: number;
  node_frequency: Record<string, number>;
  bottlenecks: Record<string, number>;
}

export interface SubmitRequest {
  repo_id: string;
  ticket_id: string;
  title: string;
  body?: string;
  task_type?: string;
  priority?: number;
  open_pr?: boolean;
  draft?: boolean;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

export const api = {
  getStats: () => apiFetch<StatsData>("/api/stats"),
  getTasks: (status?: string) =>
    apiFetch<TaskData[]>(`/api/tasks${status && status !== "all" ? `?status=${status}` : ""}`),
  getTask: (id: string) => apiFetch<TaskData>(`/api/tasks/${id}`),
  submitTask: (data: SubmitRequest) =>
    apiFetch<{ task_id: string; status: string }>("/api/tasks/submit", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  getHealth: () => apiFetch<{ status: string; store: string }>("/api/health"),
};
