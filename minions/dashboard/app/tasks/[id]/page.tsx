"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { api, TaskData } from "@/lib/api";
import { formatDuration, formatTokens, formatTimestamp } from "@/lib/format";

const STATUS_COLORS: Record<string, string> = {
  queued: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  running: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  success: "bg-green-500/20 text-green-400 border-green-500/30",
  failed: "bg-red-500/20 text-red-400 border-red-500/30",
  escalated: "bg-orange-500/20 text-orange-400 border-orange-500/30",
};

const NODE_TYPES: Record<string, { label: string; color: string }> = {
  setup_env: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  assemble_context: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  reproduce_bug: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  explore: { label: "[A]", color: "bg-purple-500/20 text-purple-300" },
  write_code: { label: "[A]", color: "bg-purple-500/20 text-purple-300" },
  run_lint: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  lint_gate: { label: "[G]", color: "bg-amber-500/20 text-amber-300" },
  fix_lint: { label: "[A]", color: "bg-purple-500/20 text-purple-300" },
  run_tests: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  test_gate: { label: "[G]", color: "bg-amber-500/20 text-amber-300" },
  apply_autofixes: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  fix_tests: { label: "[A]", color: "bg-purple-500/20 text-purple-300" },
  build_check: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  code_review: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  review_gate: { label: "[G]", color: "bg-amber-500/20 text-amber-300" },
  fix_review: { label: "[A]", color: "bg-purple-500/20 text-purple-300" },
  create_pr: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
  escalate: { label: "[D]", color: "bg-red-500/20 text-red-300" },
  notify: { label: "[D]", color: "bg-blue-500/20 text-blue-300" },
};

export default function TaskDetailPage() {
  const params = useParams();
  const taskId = params.id as string;
  const [task, setTask] = useState<TaskData | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = async () => {
      try {
        const data = await api.getTask(taskId);
        setTask(data);
      } catch (e: any) {
        setError(e.message);
      }
    };
    load();
    const interval = setInterval(load, 3000);
    return () => clearInterval(interval);
  }, [taskId]);

  if (error) {
    return (
      <div className="space-y-4">
        <Link href="/tasks" className="text-sm text-muted-foreground hover:text-foreground">
          Back to tasks
        </Link>
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-destructive">{error}</p>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!task) {
    return (
      <div className="text-sm text-muted-foreground">Loading...</div>
    );
  }

  return (
    <div className="space-y-6">
      <Link
        href="/tasks"
        className="text-sm text-muted-foreground hover:text-foreground"
      >
        Back to tasks
      </Link>

      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold">{task.ticket_id}</h1>
            <Badge
              variant="outline"
              className={STATUS_COLORS[task.status] || ""}
            >
              {task.status}
            </Badge>
            <span className="text-xs text-muted-foreground font-mono">
              {task.task_id}
            </span>
          </div>
          <p className="text-sm text-muted-foreground mt-1">{task.title}</p>
        </div>
        {task.pr_url && (
          <a
            href={task.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm text-blue-400 hover:underline"
          >
            View PR
          </a>
        )}
      </div>

      {/* Info grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card>
          <CardContent className="pt-4 pb-4">
            <div className="text-xs text-muted-foreground">Repo</div>
            <div className="text-sm font-mono mt-1">{task.repo_id}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-4">
            <div className="text-xs text-muted-foreground">Type</div>
            <div className="text-sm mt-1">{task.task_type}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-4">
            <div className="text-xs text-muted-foreground">Total Tokens</div>
            <div className="text-sm font-mono mt-1">
              {formatTokens(task.total_tokens)}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-4">
            <div className="text-xs text-muted-foreground">Duration</div>
            <div className="text-sm mt-1">
              {formatDuration(task.total_duration)}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Timestamps */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Timeline</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-3 gap-4 text-sm">
            <div>
              <div className="text-xs text-muted-foreground">Created</div>
              <div className="mt-1">{formatTimestamp(task.created_at)}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Started</div>
              <div className="mt-1">{formatTimestamp(task.started_at)}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Completed</div>
              <div className="mt-1">{formatTimestamp(task.completed_at)}</div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Node Execution Timeline */}
      {task.nodes_executed.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Node Execution Path</CardTitle>
            <p className="text-xs text-muted-foreground">
              {task.nodes_executed.length} nodes executed.{" "}
              <span className="text-blue-300">[D]</span> deterministic{" "}
              <span className="text-purple-300">[A]</span> agentic{" "}
              <span className="text-amber-300">[G]</span> gate
            </p>
          </CardHeader>
          <CardContent>
            <div className="relative">
              {task.nodes_executed.map((node, i) => {
                const nodeInfo = NODE_TYPES[node] || {
                  label: "[?]",
                  color: "bg-muted text-muted-foreground",
                };
                const isLast = i === task.nodes_executed.length - 1;
                const isFailPoint =
                  isLast &&
                  (task.status === "failed" || task.status === "escalated");

                return (
                  <div key={`${node}-${i}`} className="flex items-center gap-3 mb-3 last:mb-0">
                    {/* Vertical line */}
                    <div className="flex flex-col items-center w-6">
                      <div
                        className={`w-3 h-3 rounded-full ${
                          isFailPoint
                            ? "bg-red-500"
                            : "bg-primary/60"
                        }`}
                      />
                      {!isLast && (
                        <div className="w-px h-6 bg-border mt-1" />
                      )}
                    </div>

                    {/* Node */}
                    <div
                      className={`flex items-center gap-2 px-3 py-1.5 rounded border ${
                        isFailPoint
                          ? "border-red-500/30 bg-red-500/10"
                          : "border-border bg-card"
                      }`}
                    >
                      <span
                        className={`text-xs font-mono px-1.5 py-0.5 rounded ${nodeInfo.color}`}
                      >
                        {nodeInfo.label}
                      </span>
                      <span className="text-sm font-mono">{node}</span>
                      {isFailPoint && (
                        <Badge
                          variant="outline"
                          className="text-xs border-red-500/30 text-red-400 ml-2"
                        >
                          stuck here
                        </Badge>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Error */}
      {task.error && (
        <Card className="border-red-500/20">
          <CardHeader>
            <CardTitle className="text-base text-red-400">Error</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-xs font-mono bg-red-500/10 p-3 rounded whitespace-pre-wrap overflow-x-auto">
              {task.error}
            </pre>
          </CardContent>
        </Card>
      )}

      {/* Description */}
      {task.body && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Description</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-sm whitespace-pre-wrap text-muted-foreground">
              {task.body}
            </pre>
          </CardContent>
        </Card>
      )}

      {/* Metadata */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Metadata</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-y-3 text-sm">
            <div>
              <span className="text-xs text-muted-foreground">Source</span>
              <div>{task.source}</div>
            </div>
            <div>
              <span className="text-xs text-muted-foreground">Requester</span>
              <div>{task.requester || "-"}</div>
            </div>
            <div>
              <span className="text-xs text-muted-foreground">Priority</span>
              <div>{task.priority}</div>
            </div>
            <div>
              <span className="text-xs text-muted-foreground">Open PR</span>
              <div>{task.open_pr ? "Yes" : "No"}</div>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
