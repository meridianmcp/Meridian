// k6 load smoke test — MCP tools/list endpoint (d188b408).
//
// Hammers the JSON-RPC tools/list method (the hottest MCP path — every session
// start calls it) to check the server stays responsive under load. Run:
//   BASE_URL=https://meridian-preview.fly.dev k6 run k6/mcp_tools.js
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  vus: 5,
  duration: '30s',
  thresholds: {
    http_req_duration: ['p(95)<1500'],
  },
};

const BASE = __ENV.BASE_URL || 'http://localhost:7878';

export default function () {
  const payload = JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    method: 'tools/list',
    params: {},
  });
  const res = http.post(`${BASE}/mcp`, payload, {
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
    },
  });
  check(res, {
    'status < 500': (r) => r.status < 500,
  });
  sleep(1);
}
