// Pi extension for wiki-memory. Installed by `wiki install pi`, which fills in the command below;
// reinstalling overwrites this file.
// Adds the wiki memory for the session's project (`wiki hook text`) before the first agent turn of
// a session, and again after the session compacts.

import { spawnSync } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** The interpreter and script, written here by `wiki install pi`. */
const WIKI: string[] = [__WIKI_COMMAND__];

function memory(cwd: string): string {
  try {
    const r = spawnSync(WIKI[0], [...WIKI.slice(1), "hook", "text"], {
      input: JSON.stringify({ cwd }),
      encoding: "utf8",
      timeout: 8000,
    });
    return (r.stdout || "").trim();
  } catch {
    return "";
  }
}

export default function wikiMemory(pi: ExtensionAPI) {
  let pending = false;

  pi.on("session_start", async (event: { reason?: string }) => {
    // An extension reload keeps the conversation, which already holds the memory.
    if (event?.reason !== "reload") pending = true;
  });

  pi.on("session_compact", async () => {
    pending = true;
  });

  pi.on("before_agent_start", async (_event: unknown, ctx: { cwd?: string }) => {
    if (!pending) return;
    pending = false;
    const text = memory(ctx?.cwd ?? process.cwd());
    if (!text) return;
    return { message: { customType: "wiki-memory", content: text, display: false } };
  });
}
