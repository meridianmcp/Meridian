// k6 load smoke test — magic-link signup path (d188b408).
//
// Drives POST /auth/magic under concurrency to surface provisioning/DB
// contention before real users do. Not run in CI (needs the k6 binary + a live
// target); run manually against a preview deploy:
//   BASE_URL=https://meridian-preview.fly.dev k6 run k6/signup_smoke.js
// NEVER point this at production — it exercises the signup/provisioning path.
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  vus: 5,
  duration: '30s',
  thresholds: {
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<1500'],
  },
};

const BASE = __ENV.BASE_URL || 'http://localhost:7878';

export default function () {
  const email = `load+${__VU}-${__ITER}@example.com`;
  const res = http.post(
    `${BASE}/auth/magic`,
    JSON.stringify({ email }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(res, {
    'status is 200': (r) => r.status === 200,
    'ok body': (r) => String(r.body).includes('ok'),
  });
  sleep(1);
}
