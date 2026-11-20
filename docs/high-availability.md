# High availability

One ForgeSRE VM with its own Postgres is the supported default; `./forgesre backup` and a
restore on a new VM are the recovery plan for most teams. When the NOC itself must survive the loss
of a VM, run two appliances in **active / standby** on one shared, replicated Postgres.

## What makes it safe

- Everything that must be shared is in Postgres: incidents, sessions, sign-in throttle, the job
  queue (atomic claim), loop heartbeats and the **leader lock**. Only the process holding the lock
  runs escalations, reports, jobs, ForgeAI, discovery and syncs, so two appliances never send the
  same mail twice. If the leader dies, Postgres drops its lock and the other appliance takes over
  within one loop interval.
- System Health › **Background loops** (and `./forgesre doctor`) shows which process is the leader
  and how old each loop's last beat is.

## How to set it up

1. **Postgres**: a replicated Postgres 16 outside the appliances (a managed service, or Patroni /
   repmgr with a virtual IP). Create the `forgesre` database and user.
2. **Both appliances**: install normally, then in `.env` point Core at it and restart:
   ```bash
   FORGESRE_DATABASE_URL=postgresql+psycopg2://forgesre:<password>@pg-vip.dc.local:5432/forgesre
   ./forgesre update
   ```
   Use the same `config/forgesre.yml` and `secrets/secrets.env` on both (copy them once; keep the
   session secret identical so a sign-in survives a switch).
3. **One address for people and alarms**: a virtual IP in front of both, e.g. keepalived checking
   `GET /api/v1/health`, or a load balancer with that health check. Point the public address
   (Settings › General), Zabbix and LibreNMS webhooks and mail links at it.
4. **Alertmanager**: each appliance's Alertmanager sends to its own Core, so an alarm arrives while
   either is up; ForgeSRE deduplicates by fingerprint into one incident. To silence once for both,
   run Alertmanager in cluster mode (`--cluster.peer=<other VM>:9094`).
5. **Backups**: back up the shared Postgres with its own tooling; `./forgesre backup` still saves
   config, secrets and generated files of each appliance.

## Limits

Prometheus keeps its own data on each appliance (graphs may differ for the minutes one was down),
and the bundled NetBox and LibreNMS are not replicated: use external ones if they must survive too.
This is a documented pattern, not an automated cluster; test the switch-over before relying on it.
