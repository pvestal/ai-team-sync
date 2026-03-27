import * as vscode from "vscode";
import * as http from "http";
import * as https from "https";

// --- Types ---

interface Presence {
  developer: string;
  agent: string;
  files: string[];
}

// --- State ---

let ws: any = null;
let statusBar: vscode.StatusBarItem;
let fileDecoEmitter: vscode.EventEmitter<vscode.Uri | vscode.Uri[] | undefined>;
let sidebar: SidebarProvider;
let teammates: Presence[] = [];
let myName = "unknown";
let myAgent = "vscode";
let serverUrl = "";
let reconnectTimer: NodeJS.Timeout | undefined;

// Colors per teammate
const PALETTE = [
  { hex: "#f0883e", theme: "charts.orange" },
  { hex: "#58a6ff", theme: "charts.blue" },
  { hex: "#3fb950", theme: "charts.green" },
  { hex: "#bc8cff", theme: "charts.purple" },
  { hex: "#f85149", theme: "charts.red" },
  { hex: "#d29922", theme: "charts.yellow" },
];
const colorMap = new Map<string, (typeof PALETTE)[0]>();
let colorIdx = 0;

function colorFor(dev: string) {
  if (!colorMap.has(dev)) {
    colorMap.set(dev, PALETTE[colorIdx % PALETTE.length]);
    colorIdx++;
  }
  return colorMap.get(dev)!;
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .map((w) => w[0]?.toUpperCase() || "")
    .join("")
    .slice(0, 2);
}

// --- Activate ---

export async function activate(context: vscode.ExtensionContext) {
  // Status bar
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusBar.text = "$(sync~spin) ATS";
  statusBar.show();
  context.subscriptions.push(statusBar);

  // File decorations
  fileDecoEmitter = new vscode.EventEmitter();
  context.subscriptions.push(
    vscode.window.registerFileDecorationProvider({
      onDidChangeFileDecorations: fileDecoEmitter.event,
      provideFileDecoration(uri) {
        const rel = vscode.workspace.asRelativePath(uri, false);
        if (rel === uri.fsPath) return undefined;
        for (const t of teammates) {
          if (t.files.includes(rel)) {
            const c = colorFor(t.developer);
            return new vscode.FileDecoration(
              initials(t.developer),
              `${t.developer} (${t.agent})`,
              new vscode.ThemeColor(c.theme)
            );
          }
        }
        return undefined;
      },
    })
  );

  // Sidebar panel (lives in explorer)
  sidebar = new SidebarProvider();
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("aiTeamSync.sidebar", sidebar)
  );

  // Detect identity
  myName = await getGitUser();
  if (process.env.CLAUDE_CODE) myAgent = "claude-code";
  else if (process.env.CURSOR_SESSION) myAgent = "cursor";

  // Report open files whenever tabs change
  context.subscriptions.push(
    vscode.window.onDidChangeVisibleTextEditors(() => sendPresence()),
    vscode.workspace.onDidOpenTextDocument(() => sendPresence()),
    vscode.workspace.onDidCloseTextDocument(() => sendPresence())
  );

  // Find server and connect
  serverUrl = await discover();
  if (serverUrl) {
    statusBar.text = "$(people) ATS";
    statusBar.tooltip = `Team Sync — ${serverUrl}`;
    connect();
  } else {
    statusBar.text = "$(people) ATS (no server)";
    statusBar.tooltip = "Team Sync — server not found";
  }
}

export function deactivate() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
}

// --- WebSocket ---

function connect() {
  const wsUrl = serverUrl.replace(/^http/, "ws") + "/ws/presence";

  const WebSocket = require("ws") as typeof import("ws");
  const socket = new WebSocket(wsUrl);

  socket.on("open", () => {
    ws = socket as any;
    sendPresence();
  });

  socket.on("message", (data: any) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.type === "update") {
        // Filter out self
        teammates = (msg.presence as Presence[]).filter((p) => p.developer !== myName);

        // Update status bar
        if (teammates.length > 0) {
          const names = teammates.map((t) => t.developer.split(" ")[0]).join(", ");
          statusBar.text = `$(people) ${names}`;
          statusBar.backgroundColor = undefined;
        } else {
          statusBar.text = "$(people) ATS";
        }

        // Refresh everything
        fileDecoEmitter.fire(undefined);
        decorateEditors();
        sidebar.refresh(teammates);
      }
    } catch {}
  });

  socket.on("close", () => {
    ws = null;
    reconnectTimer = setTimeout(connect, 5000);
  });

  socket.on("error", () => {
    socket.close();
  });
}

function sendPresence() {
  if (!ws) return;
  const files = vscode.window.visibleTextEditors
    .map((e) => vscode.workspace.asRelativePath(e.document.uri, false))
    .filter((p) => !p.startsWith("/"));

  try {
    (ws as any).send(
      JSON.stringify({ type: "presence", developer: myName, agent: myAgent, files })
    );
  } catch {}
}

// --- Editor inline banners ---

const bannerTypes = new Map<string, vscode.TextEditorDecorationType>();

function decorateEditors() {
  // Clear old
  for (const [, type] of bannerTypes) type.dispose();
  bannerTypes.clear();

  for (const editor of vscode.window.visibleTextEditors) {
    const rel = vscode.workspace.asRelativePath(editor.document.uri, false);
    if (rel === editor.document.uri.fsPath) continue;

    for (const t of teammates) {
      if (t.files.includes(rel)) {
        const c = colorFor(t.developer);
        const type = vscode.window.createTextEditorDecorationType({
          isWholeLine: true,
          after: {
            contentText: `  ${t.developer} (${t.agent})`,
            color: c.hex,
            fontStyle: "italic",
            margin: "0 0 0 2em",
          },
          overviewRulerColor: c.hex,
          overviewRulerLane: vscode.OverviewRulerLane.Full,
        });
        bannerTypes.set(`${rel}:${t.developer}`, type);
        editor.setDecorations(type, [{ range: new vscode.Range(0, 0, 0, 0) }]);
        break;
      }
    }
  }
}

// --- Sidebar ---

class SidebarProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;

  resolveWebviewView(v: vscode.WebviewView) {
    this.view = v;
    v.webview.options = { enableScripts: false };
    this.render([]);
  }

  refresh(people: Presence[]) {
    if (!this.view) return;
    this.view.badge = people.length > 0
      ? { value: people.length, tooltip: `${people.length} teammate(s)` }
      : undefined;
    this.render(people);
  }

  private render(people: Presence[]) {
    if (!this.view) return;

    if (people.length === 0) {
      this.view.webview.html = `<!DOCTYPE html><html><head><style>
        body { font-family: var(--vscode-font-family); color: var(--vscode-descriptionForeground); padding: 16px; font-size: 12px; }
      </style></head><body>
        <div style="text-align:center; margin-top:20px">No teammates online.<br><br>
        <em>When someone opens files, they appear here with colored badges in your explorer.</em></div>
      </body></html>`;
      return;
    }

    let html = "";
    for (const p of people) {
      const c = colorFor(p.developer);
      const ini = initials(p.developer);
      const files = p.files
        .map((f) => `<div class="f"><span class="b" style="background:${c.hex}">${esc(ini)}</span>${esc(f)}</div>`)
        .join("");
      html += `<div class="p" style="border-left-color:${c.hex}">
        <div class="n" style="color:${c.hex}">${esc(p.developer)}</div>
        <div class="a">${esc(p.agent)}</div>
        ${files}
      </div>`;
    }

    this.view.webview.html = `<!DOCTYPE html><html><head><style>
      body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 8px; font-size: 12px; margin: 0; }
      .p { border-left: 3px solid #444; padding: 8px 10px; margin-bottom: 8px;
           background: var(--vscode-editor-background); border-radius: 3px; }
      .n { font-weight: 600; font-size: 12px; }
      .a { color: var(--vscode-descriptionForeground); font-size: 10px; margin-bottom: 4px; }
      .f { font-family: var(--vscode-editor-font-family); font-size: 11px; padding: 2px 0; }
      .b { display: inline-block; width: 20px; height: 14px; border-radius: 2px; text-align: center;
           font-size: 9px; font-weight: 700; color: #fff; line-height: 14px; margin-right: 5px;
           font-family: var(--vscode-font-family); }
    </style></head><body>${html}</body></html>`;
  }
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// --- Server discovery ---

async function discover(): Promise<string> {
  // Check user setting first
  const configured = vscode.workspace
    .getConfiguration("aiTeamSync")
    .get<string>("serverUrl", "");
  if (configured && configured !== "http://localhost:8400") {
    if (await probe(configured)) return configured;
  }

  // Check .ai-team-sync.toml
  for (const folder of vscode.workspace.workspaceFolders || []) {
    try {
      const toml = await vscode.workspace.fs.readFile(
        vscode.Uri.joinPath(folder.uri, ".ai-team-sync.toml")
      );
      const match = Buffer.from(toml).toString().match(/url\s*=\s*"([^"]+)"/);
      if (match && (await probe(match[1]))) return match[1];
    } catch {}
  }

  // Try common addresses
  for (const url of ["http://localhost:8400", "http://192.168.50.135:8400"]) {
    if (await probe(url)) return url;
  }

  return "";
}

function probe(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const client = url.startsWith("https") ? https : http;
    const req = client.get(`${url}/health`, { timeout: 1500 }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        try {
          resolve(JSON.parse(data)?.status === "ok");
        } catch {
          resolve(false);
        }
      });
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

async function getGitUser(): Promise<string> {
  try {
    const { exec } = require("child_process");
    return new Promise((resolve) => {
      exec("git config user.name", (err: any, stdout: string) => {
        resolve(err ? "unknown" : stdout.trim());
      });
    });
  } catch {
    return "unknown";
  }
}
