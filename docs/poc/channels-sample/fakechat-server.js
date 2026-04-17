#!/usr/bin/env node
/**
 * fakechat-server.js — Minimal Claude Code Channels MCP server PoC
 *
 * Declares the claude/channel/permission capability and relays
 * permission requests to the terminal for manual approve/deny.
 *
 * Usage:
 *   node fakechat-server.js
 *
 * Then connect with:
 *   claude --channels fakechat
 *
 * (Requires claude/channels support: Claude Code v2.1.81+)
 * (For development: add --dangerously-load-development-channels)
 */

const readline = require("readline");

// --- MCP JSON-RPC over stdio transport ---

const pending = new Map(); // requestId -> { resolve, reject }
let msgId = 0;

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function reply(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function replyError(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

// --- Permission relay: terminal-based approve/deny ---

const rl = readline.createInterface({ input: process.stdin });
let lineBuffer = "";
const permQueue = []; // pending permission requests waiting for terminal input
let promptActive = false;

function askTerminal(prompt, cb) {
  process.stderr.write(prompt);
  permQueue.push(cb);
  drainPrompt();
}

function drainPrompt() {
  if (promptActive || permQueue.length === 0) return;
  promptActive = true;
  // handled in rl.on('line')
}

// --- MCP message handling ---

rl.on("line", (raw) => {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    // silently ignore non-JSON lines (e.g., terminal input for perms)
    if (promptActive && permQueue.length > 0) {
      const verdict = raw.trim().toLowerCase();
      const cb = permQueue.shift();
      promptActive = false;
      cb(verdict === "y" || verdict === "yes" || verdict === "approve");
      drainPrompt();
    }
    return;
  }

  handleMessage(msg);
});

function handleMessage(msg) {
  const { id, method, params } = msg;

  // Requests (have id + method)
  if (id !== undefined && method) {
    switch (method) {
      case "initialize":
        reply(id, {
          protocolVersion: "2024-11-05",
          serverInfo: { name: "fakechat", version: "0.1.0" },
          capabilities: {
            // Declare claude/channel capability for permission relay
            "claude/channel": {
              permission: true, // we handle permission relay
            },
          },
        });
        process.stderr.write("[fakechat] ✅ initialize OK — channel capability declared\n");
        break;

      case "tools/list":
        reply(id, { tools: [] }); // no tools in this PoC
        break;

      case "resources/list":
        reply(id, { resources: [] });
        break;

      case "prompts/list":
        reply(id, { prompts: [] });
        break;

      default:
        replyError(id, -32601, `Method not found: ${method}`);
    }
    return;
  }

  // Notifications (no id, only method)
  if (id === undefined && method) {
    switch (method) {
      case "notifications/initialized":
        process.stderr.write("[fakechat] ✅ Session initialized. Waiting for permission requests...\n");
        break;

      case "notifications/claude/channel/permission_request": {
        // Permission relay: Claude Code is asking us to forward this to the user
        const { request_id, tool_name, tool_input, reason } = params ?? {};
        const display = JSON.stringify(tool_input ?? {}, null, 2).slice(0, 400);

        process.stderr.write(
          `\n┌─────────────────────────────────────────────┐\n` +
          `│  ⚡ PERMISSION REQUEST [${request_id}]\n` +
          `│  Tool: ${tool_name}\n` +
          `│  Input: ${display}\n` +
          (reason ? `│  Reason: ${reason}\n` : "") +
          `└─────────────────────────────────────────────┘\n` +
          `  Approve? (y/n): `
        );

        askTerminal("", (approved) => {
          // Send verdict back to Claude Code via notification
          send({
            jsonrpc: "2.0",
            method: "notifications/claude/channel/permission_response",
            params: {
              request_id,
              verdict: approved ? "approved" : "denied",
            },
          });
          process.stderr.write(
            `[fakechat] ${approved ? "✅ approved" : "❌ denied"} [${request_id}]\n`
          );
        });
        break;
      }

      default:
        // Unknown notifications are silently ignored per MCP spec
        break;
    }
  }
}

process.stderr.write("[fakechat] MCP server started (stdio). Declaring claude/channel/permission.\n");
