import * as vscode from "vscode";
import * as http from "http";
import * as https from "https";

interface AtsSession {
  id: string;
  developer: string;
  agent: string;
  scope: string[];
  description: string;
  status: string;
  branch: string;
  started_at: string;
  lock_count: number;
  decision_count: number;
}

interface LockCheckResult {
  path: string;
  locked: boolean;
  developer?: string;
  mode?: string;
  pattern?: string;
}

let pollTimer: NodeJS.Timeout | undefined;
let knownSessionIds: Set<string> = new Set();
let statusBarItem: vscode.StatusBarItem;

export function activate(context: vscode.ExtensionContext) {
  statusBarItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    50
  );
  statusBarItem.command = "aiTeamSync.showTeamStatus";
  statusBarItem.text = "$(people) ATS";
  statusBarItem.tooltip = "AI Team Sync — click to see team status";
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  // Register commands
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "aiTeamSync.showTeamStatus",
      showTeamStatus
    ),
    vscode.commands.registerCommand(
      "aiTeamSync.startSession",
      startSession
    ),
    vscode.commands.registerCommand(
      "aiTeamSync.completeSession",
      completeSession
    ),
    vscode.commands.registerCommand("aiTeamSync.checkLocks", checkLocks)
  );

  // Check locks when saving files
  context.subscriptions.push(
    vscode.workspace.onWillSaveTextDocument(async (e) => {
      const config = vscode.workspace.getConfiguration("aiTeamSync");
      if (!config.get<boolean>("showLockWarnings", true)) return;

      const relativePath = vscode.workspace.asRelativePath(e.document.uri);
      const results = await apiPost<LockCheckResult[]>("/locks/check", {
        paths: [relativePath],
      });
      if (!results) return;

      for (const r of results) {
        if (r.locked) {
          const action =
            r.mode === "exclusive" ? "is BLOCKED by" : "overlaps with";
          vscode.window.showWarningMessage(
            `${r.path} ${action} ${r.developer}'s lock (${r.pattern})`
          );
        }
      }
    })
  );

  // Start polling
  startPolling();

  // Re-start polling when config changes
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("aiTeamSync")) {
        stopPolling();
        startPolling();
      }
    })
  );
}

export function deactivate() {
  stopPolling();
}

function startPolling() {
  const config = vscode.workspace.getConfiguration("aiTeamSync");
  if (!config.get<boolean>("enabled", true)) return;

  const intervalSec = config.get<number>("pollIntervalSeconds", 15);

  // Initial fetch to seed known sessions
  pollForUpdates(true);

  pollTimer = setInterval(() => pollForUpdates(false), intervalSec * 1000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

async function pollForUpdates(initial: boolean) {
  const sessions = await apiGet<AtsSession[]>("/sessions?status=active");
  if (!sessions) {
    statusBarItem.text = "$(people) ATS (offline)";
    return;
  }

  statusBarItem.text = `$(people) ATS: ${sessions.length} active`;

  const currentIds = new Set(sessions.map((s) => s.id));

  if (!initial) {
    // Detect new sessions (toast notification)
    for (const s of sessions) {
      if (!knownSessionIds.has(s.id)) {
        const scope = s.scope.join(", ") || "unspecified scope";
        vscode.window.showInformationMessage(
          `${s.developer} started working on ${scope} with ${s.agent}`,
          "View Team Status"
        ).then((action) => {
          if (action) {
            vscode.commands.executeCommand("aiTeamSync.showTeamStatus");
          }
        });
      }
    }

    // Detect completed sessions
    for (const id of knownSessionIds) {
      if (!currentIds.has(id)) {
        // Session went away — was completed or expired
        // We could fetch the completed session for details, but keep it simple
      }
    }
  }

  knownSessionIds = currentIds;
}

async function showTeamStatus() {
  const sessions = await apiGet<AtsSession[]>("/sessions?status=active");
  if (!sessions || sessions.length === 0) {
    vscode.window.showInformationMessage("No active team sessions.");
    return;
  }

  const items: vscode.QuickPickItem[] = sessions.map((s) => ({
    label: `$(person) ${s.developer}`,
    description: `${s.agent} | ${s.branch}`,
    detail: `Scope: ${s.scope.join(", ")} | Locks: ${s.lock_count} | Decisions: ${s.decision_count}`,
  }));

  vscode.window.showQuickPick(items, {
    title: `AI Team Sync — ${sessions.length} Active Session(s)`,
    placeHolder: "Team members currently working with AI agents",
  });
}

async function startSession() {
  const scope = await vscode.window.showInputBox({
    prompt: "Scope pattern (e.g., src/auth/**)",
    placeHolder: "src/**",
  });
  if (!scope) return;

  const desc = await vscode.window.showInputBox({
    prompt: "What are you working on?",
    placeHolder: "Refactoring auth middleware",
  });

  const result = await apiPost<AtsSession>("/sessions", {
    developer:
      vscode.workspace.getConfiguration("git").get("userName") || "unknown",
    agent: "vscode",
    scope: [scope],
    description: desc || "",
    branch: "", // Could detect from git extension
    auto_lock: true,
  });

  if (result) {
    vscode.window.showInformationMessage(
      `Session started. ${result.lock_count} lock(s) created.`
    );
  }
}

async function completeSession() {
  const sessions = await apiGet<AtsSession[]>("/sessions?status=active");
  if (!sessions || sessions.length === 0) {
    vscode.window.showInformationMessage("No active sessions to complete.");
    return;
  }

  // Find sessions by current user
  const gitUser =
    vscode.workspace.getConfiguration("git").get<string>("userName") ||
    "unknown";
  const mySessions = sessions.filter((s) => s.developer === gitUser);

  if (mySessions.length === 0) {
    vscode.window.showInformationMessage("You have no active sessions.");
    return;
  }

  const session = mySessions[0]; // Complete the first one

  const summary = await vscode.window.showInputBox({
    prompt: "Session summary (what did you accomplish?)",
    placeHolder: "Refactored auth middleware to use JWT",
  });

  await apiPatch(`/sessions/${session.id}`, {
    status: "completed",
    summary: summary || "",
  });

  vscode.window.showInformationMessage("Session completed. Locks released.");
}

async function checkLocks() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showInformationMessage("No active editor.");
    return;
  }

  const relativePath = vscode.workspace.asRelativePath(editor.document.uri);
  const results = await apiPost<LockCheckResult[]>("/locks/check", {
    paths: [relativePath],
  });

  if (!results) return;

  for (const r of results) {
    if (r.locked) {
      vscode.window.showWarningMessage(
        `${r.path} is locked by ${r.developer} (pattern: ${r.pattern}, mode: ${r.mode})`
      );
    } else {
      vscode.window.showInformationMessage(`${r.path} — no active locks.`);
    }
  }
}

// --- HTTP helpers ---

function getServerUrl(): string {
  return vscode.workspace
    .getConfiguration("aiTeamSync")
    .get<string>("serverUrl", "http://localhost:8400");
}

function apiGet<T>(path: string): Promise<T | null> {
  return new Promise((resolve) => {
    const url = `${getServerUrl()}/api${path}`;
    const client = url.startsWith("https") ? https : http;

    client
      .get(url, { timeout: 5000 }, (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch {
            resolve(null);
          }
        });
      })
      .on("error", () => resolve(null))
      .on("timeout", () => resolve(null));
  });
}

function apiPost<T>(path: string, body: unknown): Promise<T | null> {
  return apiRequest<T>("POST", path, body);
}

function apiPatch<T>(path: string, body: unknown): Promise<T | null> {
  return apiRequest<T>("PATCH", path, body);
}

function apiRequest<T>(
  method: string,
  path: string,
  body: unknown
): Promise<T | null> {
  return new Promise((resolve) => {
    const url = new URL(`${getServerUrl()}/api${path}`);
    const client = url.protocol === "https:" ? https : http;
    const payload = JSON.stringify(body);

    const req = client.request(
      {
        hostname: url.hostname,
        port: url.port,
        path: url.pathname,
        method,
        timeout: 5000,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch {
            resolve(null);
          }
        });
      }
    );

    req.on("error", () => resolve(null));
    req.on("timeout", () => resolve(null));
    req.write(payload);
    req.end();
  });
}
