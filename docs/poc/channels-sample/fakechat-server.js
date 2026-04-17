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
 *   claude --mcp-config /tmp/fakechat-mcp.json --channels server:fakechat
 *
 * (Requires Claude Code v2.1.81+)
 */

const readline = require("readline");

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function reply(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function replyError(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

const permQueue = [];
let promptActive = false;

function askTerminal(cb) {
  permQueue.push(cb);
  drainPrompt();
}

function drainPrompt() {
  if (promptActive || permQueue.length === 0) return;
  promptActive = true;
}

const rl = readline.createInterface({ input: process.stdin });

rl.on("line", (raw) => {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
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

  if (id !== undefined && method) {
    switch (method) {
      case "initialize":
        reply(id, {
          protocolVersion: "2024-11-05",
          serverInfo: { name: "fakechat", version: "0.1.0" },
          capabilities: {
            "claude/channel": { permission: true },
          },
        });
        process.stderr.write("[fakechat] ✅ initialize — claude/channel/permission declared\n");
        break;

      case "tools/list":
        reply(id, { tools: [] });
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

  if (id === undefined && method) {
    switch (method) {
      case "notifications/initialized":
        process.stderr.write("[fakechat] ✅ Session ready. Waiting for permission requests...\n");
        break;

      case "notifications/claude/channel/permission_request": {
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

        askTerminal((approved) => {
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
        break;
    }
  }
}

process.stderr.write("[fakechat] MCP server started (stdio). Declaring claude/channel/permission.\n");
