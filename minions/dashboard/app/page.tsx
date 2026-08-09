"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, StatsData, TaskData } from "@/lib/api";
import { formatDuration, formatTokens, timeAgo } from "@/lib/format";

const STATUS_COLORS: Record<string, string> = {
  queued: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  running: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  success: "bg-green-500/20 text-green-400 border-green-500/30",
  failed: "bg-red-500/20 text-red-400 border-red-500/30",
  escalated: "bg-orange-500/20 text-orange-400 border-orange-500/30",
};

export default function OverviewPage() {
  const [stats, setStats] = useState<StatsData | null>(null);
  const [tasks, setTasks] = useState<TaskData[]>([]);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    const load = async () => {
      try {
        const [s, t] = await Promise.all([api.getStats(), api.getTasks()]);
        setStats(s);
        setTasks(t.slice(0, 10));
      } catch (e: any) {
        setError(e.message);
      }
    };
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, []);

  if (error) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold">Overview</h1>
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground">
              Cannot connect to API server. Start it with:
            </p>
            <pre className="mt-2 text-xs bg-muted p-3 rounded font-mono">
              cd ~/Desktop/context{"\n"}
              uvicorn minions.api.server:app --port 8111 --reload
            </pre>
            <p className="mt-3 text-xs text-destructive">{error}</p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-semibold">Overview</h1>

      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Tasks
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">{stats?.total ?? "-"}</div>
            <div className="text-xs text-muted-foreground mt-1">
              {stats?.counts.queued ?? 0} queued, {stats?.counts.running ?? 0} running
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Success Rate
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">
              {stats ? `${stats.success_rate}%` : "-"}
            </div>
            <div className="text-xs text-muted-foreground mt-1">
              {stats?.counts.success ?? 0} succeeded, {stats?.counts.failed ?? 0} failed
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Avg Tokens
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">
              {stats ? formatTokens(stats.avg_tokens) : "-"}
            </div>
            <div className="text-xs text-muted-foreground mt-1">
              per completed task
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Avg Duration
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">
              {stats ? formatDuration(stats.avg_duration) : "-"}
            </div>
            <div className="text-xs text-muted-foreground mt-1">
              per completed task
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Bottlenecks */}
      {stats && Object.keys(stats.bottlenecks).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Pipeline Bottlenecks</CardTitle>
            <p className="text-xs text-muted-foreground">
              Last node executed before failure/escalation
            </p>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {Object.entries(stats.bottlenecks).map(([node, count]) => (
                <div
                  key={node}
                  className="flex items-center gap-2 rounded border border-red-500/20 bg-red-500/10 px-3 py-1.5"
                >
                  <span className="text-sm font-mono">{node}</span>
                  <Badge variant="outline" className="text-xs border-red-500/30 text-red-400">
                    {count}x
                  </Badge>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Recent Tasks */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">Recent Tasks</CardTitle>
          <Link
            href="/tasks"
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            View all
          </Link>
        </CardHeader>
        <CardContent>
          {tasks.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4 text-center">
              No tasks yet. Submit one from the Tasks page.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-20">Status</TableHead>
                  <TableHead>Ticket</TableHead>
                  <TableHead>Title</TableHead>
                  <TableHead className="w-20">Type</TableHead>
                  <TableHead className="w-20">Tokens</TableHead>
                  <TableHead className="w-24">Duration</TableHead>
                  <TableHead className="w-24">Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tasks.map((task) => (
                  <TableRow key={task.task_id}>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className={STATUS_COLORS[task.status] || ""}
                      >
                        {task.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      <Link
                        href={`/tasks/${task.task_id}`}
                        className="hover:underline"
                      >
                        {task.ticket_id}
                      </Link>
                    </TableCell>
                    <TableCell className="max-w-[300px] truncate text-sm">
                      {task.title}
                    </TableCell>
                    <TableCell>
                      <span className="text-xs text-muted-foreground">
                        {task.task_type}
                      </span>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {formatTokens(task.total_tokens)}
                    </TableCell>
                    <TableCell className="text-xs">
                      {formatDuration(task.total_duration)}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {timeAgo(task.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Node Frequency */}
      {stats && Object.keys(stats.node_frequency).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Node Execution Frequency</CardTitle>
            <p className="text-xs text-muted-foreground">
              How often each node runs across all tasks
            </p>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {Object.entries(stats.node_frequency).map(([node, count]) => {
                const max = Math.max(
                  ...Object.values(stats.node_frequency)
                );
                const pct = (count / max) * 100;
                return (
                  <div key={node} className="flex items-center gap-3">
                    <span className="w-36 text-xs font-mono truncate">
                      {node}
                    </span>
                    <div className="flex-1 h-5 bg-muted rounded overflow-hidden">
                      <div
                        className="h-full bg-primary/50 rounded"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className="w-8 text-xs text-muted-foreground text-right">
                      {count}
                    </span>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
