"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, TaskData } from "@/lib/api";
import { formatDuration, formatTokens, timeAgo } from "@/lib/format";

const STATUS_COLORS: Record<string, string> = {
  queued: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  running: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  success: "bg-green-500/20 text-green-400 border-green-500/30",
  failed: "bg-red-500/20 text-red-400 border-red-500/30",
  escalated: "bg-orange-500/20 text-orange-400 border-orange-500/30",
};

export default function TasksPage() {
  const [tasks, setTasks] = useState<TaskData[]>([]);
  const [filter, setFilter] = useState("all");
  const [error, setError] = useState("");
  const [submitOpen, setSubmitOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // Form state
  const [form, setForm] = useState({
    repo_id: "",
    ticket_id: "",
    title: "",
    body: "",
    task_type: "auto",
    priority: 1,
    open_pr: false,
  });

  const loadTasks = async () => {
    try {
      const data = await api.getTasks(filter);
      setTasks(data);
      setError("");
    } catch (e: any) {
      setError(e.message);
    }
  };

  useEffect(() => {
    loadTasks();
    const interval = setInterval(loadTasks, 5000);
    return () => clearInterval(interval);
  }, [filter]);

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      await api.submitTask(form);
      setSubmitOpen(false);
      setForm({
        repo_id: "",
        ticket_id: "",
        title: "",
        body: "",
        task_type: "auto",
        priority: 1,
        open_pr: false,
      });
      loadTasks();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (error && tasks.length === 0) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold">Tasks</h1>
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground">
              Cannot connect to API server. Start it with:
            </p>
            <pre className="mt-2 text-xs bg-muted p-3 rounded font-mono">
              cd ~/Desktop/context{"\n"}
              uvicorn minions.api.server:app --port 8111 --reload
            </pre>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Tasks</h1>

        <Dialog open={submitOpen} onOpenChange={setSubmitOpen}>
          <DialogTrigger className="inline-flex items-center justify-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90">
            Submit Task
          </DialogTrigger>
          <DialogContent className="max-w-lg">
            <DialogHeader>
              <DialogTitle>Submit New Task</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 pt-2">
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Repository</Label>
                  <Input
                    placeholder="owner/repo"
                    value={form.repo_id}
                    onChange={(e) =>
                      setForm({ ...form, repo_id: e.target.value })
                    }
                  />
                </div>
                <div className="space-y-2">
                  <Label>Ticket ID</Label>
                  <Input
                    placeholder="TICKET-123"
                    value={form.ticket_id}
                    onChange={(e) =>
                      setForm({ ...form, ticket_id: e.target.value })
                    }
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label>Title</Label>
                <Input
                  placeholder="Fix the login bug"
                  value={form.title}
                  onChange={(e) =>
                    setForm({ ...form, title: e.target.value })
                  }
                />
              </div>

              <div className="space-y-2">
                <Label>Description</Label>
                <Textarea
                  placeholder="Detailed description of the task..."
                  rows={4}
                  value={form.body}
                  onChange={(e) =>
                    setForm({ ...form, body: e.target.value })
                  }
                />
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div className="space-y-2">
                  <Label>Type</Label>
                  <Select
                    value={form.task_type}
                    onValueChange={(v) =>
                      setForm({ ...form, task_type: v ?? "auto" })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">Auto</SelectItem>
                      <SelectItem value="bug_fix">Bug Fix</SelectItem>
                      <SelectItem value="feature">Feature</SelectItem>
                      <SelectItem value="migration">Migration</SelectItem>
                      <SelectItem value="test_fix">Test Fix</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-2">
                  <Label>Priority</Label>
                  <Select
                    value={String(form.priority)}
                    onValueChange={(v) =>
                      setForm({ ...form, priority: parseInt(v ?? "1") })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="0">High (0)</SelectItem>
                      <SelectItem value="1">Normal (1)</SelectItem>
                      <SelectItem value="2">Low (2)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-2">
                  <Label>Open PR</Label>
                  <Select
                    value={form.open_pr ? "yes" : "no"}
                    onValueChange={(v) =>
                      setForm({ ...form, open_pr: (v ?? "no") === "yes" })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="yes">Yes</SelectItem>
                      <SelectItem value="no">No</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <Button
                onClick={handleSubmit}
                disabled={submitting || !form.repo_id || !form.ticket_id || !form.title}
                className="w-full"
              >
                {submitting ? "Submitting..." : "Submit"}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      {/* Filter tabs */}
      <Tabs value={filter} onValueChange={setFilter}>
        <TabsList>
          <TabsTrigger value="all">All</TabsTrigger>
          <TabsTrigger value="queued">Queued</TabsTrigger>
          <TabsTrigger value="running">Running</TabsTrigger>
          <TabsTrigger value="success">Success</TabsTrigger>
          <TabsTrigger value="failed">Failed</TabsTrigger>
          <TabsTrigger value="escalated">Escalated</TabsTrigger>
        </TabsList>
      </Tabs>

      {/* Task table */}
      <Card>
        <CardContent className="pt-4">
          {tasks.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              No tasks {filter !== "all" ? `with status "${filter}"` : "yet"}.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-20">Status</TableHead>
                  <TableHead className="w-20">ID</TableHead>
                  <TableHead>Ticket</TableHead>
                  <TableHead>Title</TableHead>
                  <TableHead className="w-16">Type</TableHead>
                  <TableHead className="w-16">Pri</TableHead>
                  <TableHead className="w-20">Tokens</TableHead>
                  <TableHead className="w-20">Duration</TableHead>
                  <TableHead className="w-24">Created</TableHead>
                  <TableHead className="w-16">PR</TableHead>
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
                        className="hover:underline text-primary"
                      >
                        {task.task_id}
                      </Link>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {task.ticket_id}
                    </TableCell>
                    <TableCell className="max-w-[250px] truncate text-sm">
                      {task.title}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {task.task_type}
                    </TableCell>
                    <TableCell className="text-xs text-center">
                      {task.priority}
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
                    <TableCell>
                      {task.pr_url ? (
                        <a
                          href={task.pr_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-xs text-blue-400 hover:underline"
                        >
                          PR
                        </a>
                      ) : (
                        <span className="text-xs text-muted-foreground">-</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
