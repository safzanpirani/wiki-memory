// omp adapter for wiki-memory. Installed by `wiki install omp`, which fills in the command below.
// Adds the wiki memory for the session's project (`wiki context --cwd <cwd> --instructions`:
// how to recall and save, the scoped index, hub and core pages, pending notes) before an
// agent turn whenever the model's context does not already hold it: the first turn of a
// session (startup, /new), the first turn after a compaction or /clear, and the first turn
// after /move takes the session to another project. Subagents get nothing.
//
// The command runs in the background from session start and compaction, so the
// first turn does not wait on it.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

/** The interpreter and script, written here by `wiki install omp`. */
const WIKI: string[] = [__WIKI_COMMAND__];
const TYPE = "wiki-memory";
const TIMEOUT_MS = 8000;

interface MemoryDetails {
  /** Working directory the memory was read for. */
  cwd: string;
  /** sha256 of the injected text, so a move within one project injects nothing. */
  hash: string;
}

type HookResult = { text: string } | { error: string };

function run(bin: string, args: string[], stdin: string): Promise<{ text: string } | { error: string }> {
  // A promise settles once; later resolve calls (close after a timeout) are no-ops.
  const { promise, resolve } = Promise.withResolvers<{ text: string } | { error: string }>();
  let out = "";
  const child = spawn(bin, args, { stdio: ["pipe", "pipe", "ignore"] });
  const timer = setTimeout(() => {
    child.kill();
    resolve({ error: `timed out after ${TIMEOUT_MS / 1000}s` });
  }, TIMEOUT_MS);
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", chunk => (out += chunk));
  child.on("error", err => {
    clearTimeout(timer);
    resolve({ error: err.message });
  });
  child.on("close", code => {
    clearTimeout(timer);
    const text = out.trim();
    // The command prints nothing useful on failure, so empty output is a failure.
    resolve(code === 0 && text ? { text } : { error: `exited ${code} with ${text ? "output" : "no output"}` });
  });
  // A command that exits early (or never starts) makes this write fail with EPIPE;
  // the error/close handlers above already report it.
  child.stdin.on("error", () => {});
  child.stdin.end(stdin);
  return promise;
}

function runHook(cwd: string): Promise<HookResult> {
  return run(WIKI[0], [...WIKI.slice(1), "context", "--cwd", cwd, "--instructions"], "");
}

/**
 * The newest memory message still in the model's context, or undefined.
 * Walks the branch back from the leaf; a compaction drops everything before its
 * firstKeptEntryId and a reset boundary everything before itself, so the search stops there.
 */
function injectedMemory(entries: ReadonlyArray<unknown>): Partial<MemoryDetails> | undefined {
  let stopAt: string | undefined;
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i];
    if (!e || typeof e !== "object") continue;
    const id = "id" in e ? e.id : undefined;
    const type = "type" in e ? e.type : undefined;
    if (type === "custom_message" && "customType" in e && e.customType === TYPE) {
      const d = "details" in e ? e.details : undefined;
      if (!d || typeof d !== "object") return {};
      return {
        cwd: "cwd" in d && typeof d.cwd === "string" ? d.cwd : undefined,
        hash: "hash" in d && typeof d.hash === "string" ? d.hash : undefined,
      };
    }
    // /clear leaves earlier entries in the journal but out of the model's context.
    if (type === "reset_boundary") return undefined;
    if (stopAt !== undefined && id === stopAt) return undefined;
    if (type === "compaction") {
      const kept = "firstKeptEntryId" in e ? e.firstKeptEntryId : undefined;
      if (typeof kept !== "string" || !kept) return undefined;
      stopAt ??= kept;
    }
  }
  return undefined;
}

export default function wikiMemory(pi: ExtensionAPI) {
  const pending = new Map<string, Promise<HookResult>>();
  let warned = false;

  const prewarm = (ctx: ExtensionContext) => {
    pending.clear();
    if (ctx.agent?.kind === "sub") return;
    pending.set(ctx.cwd, runHook(ctx.cwd));
  };
  pi.on("session_start", (_event, ctx) => prewarm(ctx));
  pi.on("session_switch", (_event, ctx) => prewarm(ctx));
  // Pages and notes may have changed since the last read; reload for the reinjection.
  pi.on("session_compact", (_event, ctx) => prewarm(ctx));

  pi.on("before_agent_start", async (_event, ctx) => {
    if (ctx.agent?.kind === "sub") return;
    const cwd = ctx.cwd;
    const injected = injectedMemory(ctx.sessionManager.getBranch());
    if (injected && (injected.cwd === undefined || injected.cwd === cwd)) return;

    let job = pending.get(cwd);
    if (!job) {
      job = runHook(cwd);
      pending.set(cwd, job);
    }
    const result = await job;
    if ("error" in result) {
      pending.delete(cwd);
      if (!warned && ctx.hasUI) {
        warned = true;
        ctx.ui.notify(`Wiki memory not loaded: wiki ${result.error}`, "warning");
      }
      return;
    }
    const hash = createHash("sha256").update(result.text).digest("hex");
    if (injected?.hash === hash) return;
    const details: MemoryDetails = { cwd, hash };
    return { message: { customType: TYPE, content: result.text, display: false, details } };
  });
}
