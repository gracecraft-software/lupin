# lupin

Lupin runs coding-agent loops on a group of machines (a fleet). It has 4 jobs:

1. **Routing.** It picks a model and an effort level for a task. It uses Claude,
   OpenAI, and OpenCode Go quota.
2. **Slots.** It limits how many callers use a resource at the same time.
3. **Dashboard.** A web page shows the loops. You can start and stop them there.
4. **Fleet.** It shares issue claims, quests, machine records, quota, and events
   through Redis. It ranks open issues and starts loops on workers.

> **Status:** Early software. Many defaults and data files are specific to the
> owner's fleet. Read [Known limits](#known-limits) before you install it.

## Requirements

| Item | Necessary for | Notes |
| --- | --- | --- |
| Nix with flakes | All | The supported way to build Lupin. Nix supplies Python 3.11 or later. |
| Redis | Fleet commands, `redis` slot backend, dashboard data | Not necessary for `route` or the `local` slot backend. |
| `gh` (GitHub CLI), `git` | `gh`: claims, roadmap, dashboard. `git`: loops, roadmap. | The Nix package adds both to `PATH`. |
| systemd (Linux) | Loop workers, timers | Not available on macOS. |
| Herdr | Loop workers | The Lupin package adds it to `PATH`. Set `LUPIN_HERDR_BIN` to use another binary. |
| `omp` | OMP loops, `fetch-benchmarks`, some quota data | The default adds it to `PATH` from a pinned input. `without-omp` saves about 228 MiB. Install `omp` separately for OMP tasks. |

## Install

Run Lupin one time without installing it: `nix run github:OWNER/REPO -- --help`.
Replace `OWNER/REPO` with the owner and repository name of the Lupin flake. To
build it from a checkout, run `nix build .`, then `./result/bin/lupin --help`.

To install it on a NixOS or nix-darwin machine, add this flake as an input. Add
`packages.<system>.default` to `environment.systemPackages`.

## Quick start

These commands need no Redis. Select a model for a medium-size prose task. Then
get a slot lease, check the slot, and release the lease. `lupin hold` runs a
command while you hold a lease. It releases the lease when the command stops.

```sh
$ lupin route prose size-m
sonnet high
$ lupin acquire demo --holder me --max 1
demo:7d023867bcbed45f
$ lupin status
demo: 1/1
$ lupin release --lease demo:7d023867bcbed45f
$ lupin hold demo --holder me --wait 5 -- echo inside-lease
```

The route categories are `coding`, `general`, `frontend-ui`, `translation`,
`prose`, and `cad-spatial`. If `LUPIN_MODEL_TIERS` is not set, Lupin uses the
bundled `src/lupin/model-tiers.json` file. To use your own model tiers, set it
to a JSON file. The file must have the category that `route` selects. A category
has a `tiers` object with one or more of `tier0`, `tier1`, and `tier2`. Each
tier is a list of `{model, effort}` choices that Lupin tries in order. Effort
must be `low`, `medium`, `high`, or `xhigh`. If a route needs an omitted tier,
Lupin uses the next higher tier. If no higher tier exists, the route reports an
error. Optional fields are `source`, `last_verified`, and `note`. Lupin ignores
a top-level key that starts with `_`. The Models page shows the file path.

```json
{
  "coding": {
    "tiers": {
      "tier1": [
        {"model": "opencode-go/glm-5.3", "effort": "high"}
      ]
    }
  }
}
```

## Fleet setup

- **One coordinator.** A Redis server that all machines can reach. Do not bind
  it to `0.0.0.0`. Use a private network, for example a tailnet.
- **Workers.** Each worker runs `lupin agent` and runs loops in Herdr.
- **Optional: one dispatch machine.** It runs `lupin serve` and the `fleet-run`
  timer. It does not run loops.

Set the Redis connection for each machine:

| Variable | Use |
| --- | --- |
| `LUPIN_REDIS_HOST`, `LUPIN_REDIS_PORT` | Redis host name and port. |
| `LUPIN_REDIS_USERNAME` | Redis ACL user. |
| `LUPIN_REDIS_PASSWORD` | Redis password. Give it through a secret manager. Do not put it in a command line. |

Each worker needs its own key to sign remote loop commands. Lupin reads the keys
from `LUPIN_CMD_SIGNING_KEYS_DIR` or the systemd `CREDENTIALS_DIRECTORY`.

## NixOS module

`nixosModules.default` writes the service user, `lupin-agent.service`,
`lupin-serve.service`, the `lupin-quota` timer, and the `delegation-loop` timer.
Set `role` to `worker`, `dispatch`, or `both`.

```nix
services.lupin = {
  enable = true;
  role = "worker";
  redis.host = "COORDINATOR_HOST";
  redis.passwordFile = "/run/secrets/redis-password";
  signingKeysDir = "/run/secrets/lupin-keys";
};
```

The module needs these secret files:

- `redis.passwordFile`: the Redis password file. The module loads it as the
  systemd credential `redis-password`. Skip it if the coordinator has none.
- `signingKeysDir`: a directory with one command-queue key per worker. The file
  name is the worker name. On a worker, the directory must be root-owned with
  mode `0700`, and the key files must have mode `0400`. `lupin agent` gets only
  its own key (a systemd credential). The loop `systemd-run` wrapper adds
  `--property=InaccessiblePaths=<signingKeysDir>` to every unit it starts. So a
  loop unit cannot read a key or remove this property. `lupin serve` and
  `fleet-run` need the whole directory read-only. On a dispatch machine,
  `cfg.user` must be able to read it. With `role = "both"`, `serve` holds the
  directory open to `cfg.user`. A loop unit on that machine can reach the keys
  through `serve`. To keep the keys apart, use separate `worker` and `dispatch`
  machines.
- `credentialsFile`: a JSON file that maps a repository name to a GitHub token
  file, for `LUPIN_LOOP_CREDENTIALS_FILE`. The agent, dashboard, and `fleet-run`
  read it. The wrapper allows `LoadCredential=gh-token:PATH` only when `PATH` is
  a value in this file. Otherwise it rejects `gh-token`.

The default `serve.bind` is `127.0.0.1` and cannot be `0.0.0.0`. The default
`serve.port` is 8788. The default package is the flake's
`packages.<system>.default`. Without `fleetConfig`, the module writes
`/etc/lupin/fleet.json` from the `redis` options. To use one file for several
machines, set `fleetConfig` to that file.

The module also writes sudo rules for the loop user. The loop user can start and
stop `lupin-*` units and the `delegation-loop` timer. The `systemd-run` rule
names a wrapper, not `systemd-run` itself. The wrapper accepts only the options
and command that Lupin uses. So the loop user cannot pass `--uid=0` or
`--property=User=root`. `role = "dispatch"` sets `LUPIN_LOOP_COORDINATOR_ONLY`
for `lupin serve` and `fleet-run`, which hides the local loop start control.

## Dashboard

Run `lupin serve --bind 127.0.0.1 --port 8788`. Then open
`http://localhost:8788`. To see it from another computer, use an SSH tunnel:
`ssh -N -L 8788:127.0.0.1:8788 MACHINE`. The Repos and Roadmap pages read the
Git checkouts in `LUPIN_LOOP_CODE_DIR` (default `/code`).

## Commands

Run `lupin --help` for all commands. `lupin <command> --help` shows its options.

| Job | Commands |
| --- | --- |
| Select a model | `route`, `classify`, `review-route`, `place` |
| Model data | `fetch-models`, `fetch-benchmarks` |
| Provider quota | `quota` |
| Slots | `acquire`, `hold`, `release`, `status` |
| Rank open issues | `roadmap` |
| Claim an issue | `claim`, `renew-claim`, `release-claim` |
| Work a set of issues on one machine | `quest` |
| Release old claims and quests | `reconcile` |
| Shared events and handoffs | `ledger` |
| Machines | `join`, `heartbeat`, `drain`, `undrain`, `machines` |
| Loops | `enable`, `disable`, `run`, `once`, `fleet-run`, `loops`, `stop`, `peek`, `attach`, `schedule`, `pause`, `resume` |
| Remote commands | `agent`, `cmd` |
| Dashboard | `serve` |

Examples:

- `lupin place 123 --explain` shows which machine should run issue 123.
- `lupin quest start --issue 123 --issue 124` claims two issues as one quest.
- `lupin ledger read OWNER/REPO` shows the latest shared events.

## Development

Enter the development shell with `nix develop`. It has Python, pytest, Redis.
Run the tests from the repo root with `pytest -q`. Before you open a pull
request, run the full gate with `nix flake check`. Some tests start a local
`redis-server`. Do not set `LUPIN_REDIS_HOST` to a shared Redis in tests.

## Contributing

Open pull requests against `main`. The owner merges approved pull requests. A
loop never merges. A reviewer who did not write the change must approve it.

By opening a pull request, you agree to license your change under the terms in
`LICENSE`.

## License

Lupin uses the Functional Source License, Version 1.1, MIT Future License
(`FSL-1.1-MIT`). The full text is in `LICENSE`.

- You may use, copy, change, and share Lupin for any purpose, except a
  competing use. A competing use is a commercial product or service that
  replaces Lupin, does the same job, or substitutes for another product or
  service we offer using Lupin.
- Running Lupin for your own work is always allowed.
- Two years after a version is released, that version also becomes available
  under the MIT license.
- If this summary and `LICENSE` differ, `LICENSE` applies.

`THIRD_PARTY_NOTICES.md` lists text from other projects. Those projects keep
their own licenses.

## Known limits


- Some defaults name the owner's machines and user name.
- Only Linux is tested. The flake builds for macOS. Loop workers need systemd.

## Documentation

| File | Content |
| --- | --- |
| `AGENTS.md` | Agent and repository rules for work in this repo. |
| `docs/user-guide.md` | User-facing command and state behavior. |
| `docs/redis-schema.md` | Redis keys and their format. |
| `docs/delegation-loop.md` | The loop policy for this repo. |
| `skills/` | Shared skills for loops. `delegation-loop` is the main one. |
| `docs/ui-mockups.md` | Screenshots of the dashboard mockups. |
| `THIRD_PARTY_NOTICES.md` | Licenses of adapted third-party text. |
