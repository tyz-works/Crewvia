import { createServer } from 'node:http';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { getUserDisplayName, calculateTotal } from './utils.ts';
import type { User } from './types.ts';

const PORT = parseInt(process.env.PORT ?? '3000', 10);

function handleRequest(req: IncomingMessage, res: ServerResponse): void {
  const url = req.url ?? '/';

  if (url === '/health' && req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', timestamp: new Date().toISOString() }));
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Not found' }));
}

export const server = createServer(handleRequest);

export function start(): void {
  server.listen(PORT, () => {
    console.log(`Server running on http://localhost:${PORT}`);
  });
}
