# Configuration

This page lists each setting that Lupin reads: environment variables,
command-line options with the same function, systemd credentials, and files.

Terms:

- **Variable**: an environment variable.
- **Host config**: the configuration of a machine, for example its NixOS
  modules or systemd units. This repo does not supply units. The host config
  owns them.
- **Fleet group** and **local group**: two sets of commands. The table in
  [Redis connection](#redis-connection) lists them.
- **Loop units**: the systemd units that `lupin run`, `lupin once`, and the
  loop recovery start with `sudo -n systemd-run`.

Each variable entry has these fields:

- **Purpose**: what the variable is for.
- **Default**: the value when the variable is not set.
- **Effect**: what the value changes.
- **Read by**: the file and function that read it.
- **Set by**: who usually sets it.

A test, `tests/test_configuration_doc.py`, fails when a `LUPIN_` variable in
`src/` has no heading on this page. It also fails when this page names a
variable that `src/` does not use.

## Redis connection

Lupin uses one Redis server for the fleet. This page calls it the
coordinator.

Each path below finds the Redis server in its own way. "Option" is the
command-line option, for example `--redis-host`. "Variable" is the
matching variable, for example `LUPIN_REDIS_HOST`. "Credential" is the
systemd credential `redis-password`.

<!-- markdownlint-disable MD013 -->
| Path | Host and port | Username | Password |
| --- | --- | --- | --- |
| Fleet group (see the list below) | option, variable, `fleet.json`, then `localhost:6379` | option, variable, `fleet.json`, then Redis `default` | option, variable, credential |
| `roadmap` | option, variable, then `localhost:6379` | option, variable, `fleet.json`, then Redis `default` | option, variable, credential |
| Debriefs (`debrief._redis_connection`) | variable, `fleet.json`, then `localhost:6379` | variable, `fleet.json`, then Redis `default` | credential, variable |
| Loop worker (`loop_runtime._redis_kwargs`) | variable, then `localhost:6379` | variable, then Redis `default` | credential, variable |
| Local group (see the list below) | option, variable, then `localhost:6379` | option, variable, then Redis `default` | option, variable |

Sources: `cli.py` (`_redis_args`, `_redis_conn_args`,
`_fleet_connection_args`, `_cmd_roadmap`), `machines.py`
(`resolve_connection`, `_redis_password`), `debrief.py`
(`_redis_connection`), `loop_runtime.py` (`_redis_kwargs`).

The fleet group is these commands: `fetch-models`, `fetch-benchmarks`,
`quota`, `ledger`, `serve`, `heartbeat`, `drain`, `undrain`, `machines`,
`place`, `run`, `fleet-run`, `loops`, `stop`, `peek`, `attach`,
`schedule`, `pause`, and `resume`.

The local group is these commands: `claim`, `renew-claim`,
`release-claim`, `cmd`, `agent`, `quest`, and `reconcile`. The slot
commands `acquire`, `hold`, `release`, and `status` are also in it when
they use `--backend redis`.

The fleet group uses `LUPIN_REDIS_HOST` and `LUPIN_REDIS_PORT` when set.
Otherwise, it reads `redis_host` and `redis_port` from `fleet.json`, then
uses `localhost:6379`. Debriefs follow the same defaults. The local group,
`roadmap`, and the loop worker use `localhost:6379` when their variables or
options are unset. For usernames, the fleet group, `roadmap`, and debriefs
read `redis_username` from `fleet.json` when no option or variable is set.
Other paths send no username, so Redis uses its `default` user.

The GitHub cache (`gh_cache._resolve`) fills missing fields from the default
`~/.config/lupin/fleet.json`, then uses `localhost:6379` and Redis `default`.
It does not read `LUPIN_FLEET_CONFIG`. A caller-provided connection takes
precedence.

`lupin join HOST[:PORT]` takes the host and port as its argument. It reads
the username and password from the options or the variables only.

`lupin agent` reads its command queue through the local group path. Its
debriefs use the debrief path and read `fleet.json` when their connection
variables are unset. The queue needs `LUPIN_REDIS_PASSWORD` or
`--redis-password`, or the systemd credential `redis-password`.

A value of `LUPIN_REDIS_PORT` that is not an integer gives one line and
exit code 2 on the commands that use it. `--help` still works.

### `LUPIN_CLAIM_HOLDER`

- **Default**: unset; claim commands require `--holder` unless this variable is set.
- **Effect**: Lupin sets this in each loop agent's shell to
  `<host>:<repo>:<run branch>`. `claim`, `renew-claim`, and `release-claim`
  use it as the holder when `--holder` is omitted.
- **Read by**: `cli.py` (`_holder_arg`).
- **Set by**: the loop runtime for each agent.

### `LUPIN_REDIS_HOST`

- **Default**: none. The fleet group and debriefs then use `redis_host`
  from `fleet.json`, then `localhost`. The local group, `roadmap`, and the
  loop worker use `localhost`.
- **Effect**: Lupin connects to this host for slots, claims, the command
  queue, fleet records, caches, and the ledger.
- **Read by**: `src/lupin/cli.py` (`_redis_args`, `_redis_conn_args`,
  `_fleet_connection_args`), `src/lupin/serve.py` (`main`),
  `src/lupin/loop_runtime.py` (`_redis_kwargs`), `src/lupin/debrief.py`
  (`_redis_connection`).
- **Set by**: the user or the host config. `lupin run` copies it into loop
  units (`_service_environment`).
- **Option**: `--redis-host`.

### `LUPIN_REDIS_PORT`

- **Purpose**: the TCP port of the coordinator.
- **Default**: none. The fleet group and debriefs then use `redis_port`
  from `fleet.json`, then `6379`. The local group, `roadmap`, and the loop
  worker use `6379`.
- **Effect**: Lupin connects to this port. A value that is not a whole
  number gives one line and exit code 2 and names the variable.
- **Read by**: `src/lupin/cli.py` (`_redis_port`, `_redis_args`,
  `_redis_conn_args`, `_fleet_connection_args`), `src/lupin/serve.py`
  (`_serve_port`, `main`), `src/lupin/loop_runtime.py` (`_env_int` via
  `_redis_kwargs`), `src/lupin/debrief.py` (`_redis_connection`).
  `debrief._redis_connection` ignores a value that is not all digits.
  `loop_runtime._env_int` raises `EnvValueError`, which `cli.main` reports
  as the same one line and exit code 2.
- **Set by**: the user or the host config. `lupin run` copies it into loop
  units.
- **Option**: `--redis-port`.

### `LUPIN_REDIS_USERNAME`

- **Purpose**: the Redis ACL user name.
- **Default**: none. The fleet group, `roadmap`, and debriefs then use
  `redis_username` from `fleet.json`. The local group and loop worker send
  no username; Redis uses its `default` user.
- **Effect**: Lupin logs in with this user name.
- **Read by**: the same functions as `LUPIN_REDIS_HOST`.
- **Set by**: the user or the host config. `lupin run` copies it into loop
  units.
- **Option**: `--redis-username`.

### `LUPIN_REDIS_PASSWORD`

- **Purpose**: the Redis password.
- **Default**: none. The fleet group, `roadmap`, debriefs, and the loop
  worker then read the systemd credential `redis-password`. The local group
  then sends no password.
- **Effect**: Lupin logs in with this password. For the fleet group and
  `roadmap`, the option and the variable come before the credential. For
  the loop worker and debriefs, the credential comes first.
- **Read by**: `src/lupin/cli.py` (`_redis_args`, `_redis_conn_args`,
  `_fleet_connection_args`, `_join_args`), `src/lupin/serve.py` (`main`),
  `src/lupin/machines.py` (`_redis_password`), `src/lupin/loop_runtime.py`
  (`_redis_kwargs`).
- **Set by**: the user, through a secret manager. `lupin run` does not copy
  it into loop units. The loop worker (`loop_runtime.worker`) sets it in
  its own process from the credential `redis-password`.
- **Option**: `--redis-password`. Do not put a password in a command line.
  Other users on the machine can see command lines.
- **Errors**: when Redis refuses the login, most commands exit with code 3.
  The message names the setting to check. It does not show the password.
  Some read paths do not follow this rule yet. See "Refused login and ACL
  denial" in `docs/redis-schema.md`.

### `LUPIN_LOOP_REDIS_PASSWORD_FILE`

- **Purpose**: a file that holds the Redis password for loop units.
- **Default**: none. Loop units then get no credential `redis-password`.
- **Effect**: when the path is a file, `lupin run` adds
  `LoadCredential=redis-password:<path>` to the loop worker unit. systemd
  reads the file. The worker reads the credential.
- **Read by**: `src/lupin/loop_runtime.py` (`_password_source`), called from
  `start_loop` and from the loop recovery.
- **Set by**: the host config. `lupin run` copies it into loop units.

### Systemd credential `redis-password`

- **Purpose**: the Redis password, given by systemd `LoadCredential=`.
- **Location**: `$CREDENTIALS_DIRECTORY/redis-password`. Lupin removes
  space at the start and end. An empty file counts as no password.
- **Read by**: `src/lupin/machines.py` (`_load_credential`,
  `_redis_password`), `src/lupin/loop_runtime.py` (`_load_credential`,
  `_redis_kwargs`, `worker`).
- **Set by**: the host config for `lupin serve` and other services, and
  `lupin run` for loop units (from `LUPIN_LOOP_REDIS_PASSWORD_FILE`).

## Slots

### `LUPIN_BACKEND`

- **Purpose**: the slot-lease backend for `acquire`, `hold`, `release`, and
  `status`.
- **Default**: `local`.
- **Effect**: `redis` selects the Redis backend. Any value except `local`
  or `redis` gives exit code 2 with `invalid choice`, the same error as a
  bad `--backend` value.
- **Read by**: `src/lupin/cli.py` (`_backend_args`, `_backend_name`,
  `_backend_module`).
- **Set by**: the user or the host config. `lupin run` copies it into loop
  units.
- **Option**: `--backend local|redis`.

### `LUPIN_STATE_ROOT`

- **Purpose**: the directory of the `local` slot backend.
- **Default**: `~/.lupin/slots`. The path depends on the home directory of
  the user.
- **Effect**: each slot is one directory in it. Each holder is one file.
  `~` in the value is expanded.
- **Read by**: `src/lupin/slots.py` (`_resolve_root`).
- **Set by**: the user or the host config. `lupin run` copies it into loop
  units. The loop monitor uses this backend for the `claude` slot.
- **Option**: `--state-root` (slot commands only). The option comes first.

## State directories

### `LUPIN_LOOP_STATE_DIR`

- **Purpose**: the directory for loop state.
- **Default**: `/var/lib/delegation-loop`.
- **Effect**: Lupin keeps these items in it: `repos`, `orchestrators.json`,
  `herdr-loops/`, `notes/`, `reports/`, `handoffs/`, `worktrees/`,
  `once/`, `locks/`, `debriefs/`, and `timer-dropin.d/override.conf`.
- **Read by**: `src/lupin/loop_runtime.py` (module constant `STATE_DIR`,
  read one time when the module loads). `src/lupin/serve.py` copies it.
- **Set by**: the host config. `lupin run` copies it into loop units. The
  debrief screenshot code sets it for its own dashboard child process
  (`debrief._serve_env`).

### `LUPIN_LOOP_CODE_DIR`

- **Purpose**: the directory that holds one Git checkout for each repo.
- **Default**: `/code`.
- **Effect**: `run` finds the checkout at `<dir>/<repo>`. The Repos and
  Roadmap pages list the checkouts in it. A repo with no checkout there
  does not show.
- **Read by**: `src/lupin/loop_runtime.py` (module constant `CODE_DIR`),
  `src/lupin/cli.py` (`_cmd_roadmap`). Both use the same default.
  `src/lupin/serve.py` and `src/lupin/roadmap.py` copy `CODE_DIR`.
- **Set by**: the host config. `lupin run` copies it into loop units.

## Loops and Herdr

### `LUPIN_EXECUTABLE`

- **Purpose**: the `lupin` program that loop units run.
- **Default**: the resolved path of the running program (`sys.argv[0]`).
- **Effect**: loop units, the Herdr server unit, and one-off timers run
  this program.
- **Read by**: `src/lupin/loop_runtime.py` (`_lupin_command`).
- **Set by**: the host config. `lupin run` copies it into loop units.

### `LUPIN_HERDR_BIN`

- **Purpose**: the Herdr program.
- **Default**: `herdr`, found on `PATH`. The Nix package puts Herdr on its
  `PATH`.
- **Effect**: every Herdr call in `loop_runtime.py` uses this program,
  `attach` included. The dashboard shows an attach command with plain
  `herdr`.
- **Read by**: `src/lupin/loop_runtime.py` (module constant `HERDR`).
- **Set by**: the user or the host config. `lupin run` copies it into loop
  units.

### `LUPIN_SYSTEMD_RUN`

- **Purpose**: a wrapper that validates `systemd-run` arguments before it
  runs `systemd-run`.
- **Default**: none. Lupin runs `sudo -n systemd-run` directly.
- **Effect**: when set, Lupin runs `sudo -n <wrapper>` in place of
  `sudo -n systemd-run` for loop units, the Herdr server, and one-off
  timers. The wrapper gets the `systemd-run` arguments without the command
  name. The NixOS module sets this to a wrapper that accepts only the
  options Lupin uses, so the loop user does not need `NOPASSWD` on
  `systemd-run` itself.
- **Read by**: `src/lupin/loop_runtime.py` (`_sudo_argv`).
- **Set by**: the host config. The NixOS module sets it for `lupin agent`,
  `lupin serve`, and `lupin fleet-run`. Those services copy it into loop
  units.

### `LUPIN_LOOP_USER`

- **Purpose**: the user that loop units run as.
- **Default**: the value of `USER`. If both are unset, `run` fails with
  `no loop user: set LUPIN_LOOP_USER or USER`.
- **Effect**: Lupin passes it to `systemd-run --uid`.
- **Read by**: `src/lupin/loop_runtime.py` (`_loop_user`).
- **Set by**: the host config. `lupin run` copies it into loop units.

### `LUPIN_LOOP_GROUP`

- **Purpose**: the group that loop units run as.
- **Default**: `users`.
- **Effect**: Lupin passes it to `systemd-run --gid`.
- **Read by**: `src/lupin/loop_runtime.py` (`_systemd_run`,
  `schedule_once`).
- **Set by**: the host config. `lupin run` copies it into loop units.

### `LUPIN_LOOP_PATH`

- **Purpose**: the `PATH` for loop units.
- **Default**: none. The units then get the `PATH` that systemd gives.
- **Effect**: Lupin adds `--setenv=PATH=<value>` to each loop unit.
- **Read by**: `src/lupin/loop_runtime.py` (`_service_environment`).
- **Set by**: the host config. `lupin run` copies it into loop units.

### `LUPIN_LOOP_PROMPT_FILE`

- **Purpose**: a file with the base prompt for the loop agent.
- **Default**: none. Lupin then uses the built-in prompt
  (`loop_runtime.DEFAULT_PROMPT`).
- **Effect**: `run` reads the file each time it starts a loop. It adds the
  handoff notes and the run note to this text. If Lupin cannot read the
  file, `run` fails.
- **Read by**: `src/lupin/loop_runtime.py` (`_write_prompt`).
- **Set by**: the host config. `lupin run` copies it into loop units.

### `LUPIN_LOOP_DEFAULT_REPOS`

- **Purpose**: the first list of enabled repos.
- **Default**: empty.
- **Effect**: a comma-separated list of repo names. Lupin reads it only
  when the `repos` file in the state directory does not exist. Lupin then
  writes each name to that file with the platform `claude`. After that,
  the file is the list. Use `lupin enable` and `lupin disable` to change
  it.
- **Read by**: `src/lupin/loop_runtime.py` (`_default_repos`, called from
  `enabled_repos`).
- **Set by**: the host config. `lupin run` copies it into loop units.

### `LUPIN_LOOP_CLAUDE_MAX_CONCURRENT`

- **Purpose**: the limit of Claude loops that run at the same time on this
  machine.
- **Default**: `1`.
- **Effect**: the loop monitor holds the `local` slot `claude` with this
  limit. A slot keeps the limit from its first `acquire`. A changed value
  has no effect on a slot that already exists (see "How a slot's limit
  (`max`) works" in `AGENTS.md`).
- **Read by**: `src/lupin/loop_runtime.py` (`_monitor_loop`).
- **Set by**: the host config. `lupin run` copies it into loop units.

### `LUPIN_LOOP_CREDENTIALS_FILE`

- **Purpose**: a JSON file that maps each repo to its GitHub token file.
- **Default**: none. Loops then get no repo token from Lupin.
- **Format**: one JSON object. Each key is a repo directory name, not
  `OWNER/REPO`. Each value is an absolute path to a file that holds the
  token:

  ```json
  {"REPO": "/run/secrets/REPO-gh-token"}
  ```

- **Effect**: Lupin gives the token to the repo's Herdr workspace as
  `GH_TOKEN`. For an older per-repo Herdr session, Lupin loads the file as
  the systemd credential `gh-token`. A file that is not a JSON object, a
  value that is not an absolute path, or a token file that does not exist
  stops the loop start with an error.
- **Read by**: `src/lupin/loop_runtime.py` (`_runtime_paths`, called from
  `_workspace_env` and `_ensure_server`). `herdr_server` reads the credential
  `gh-token`, not this variable.
- **Set by**: the host config. `lupin run` copies it into loop units.

## Fleet and signing

### `LUPIN_FLEET_CONFIG`

- **Purpose**: the path of the fleet config file.
- **Default**: `~/.config/lupin/fleet.json`. The path depends on the home
  directory of the user.
- **Effect**: `lupin join` writes the file. The fleet group reads it.
- **Read by**: `src/lupin/cli.py` (`_fleet_connection_args`, `_join_args`),
  `src/lupin/serve.py` (`main`), `src/lupin/debrief.py`
  (`_redis_connection`). These functions give the path to
  `machines.load_config` and `machines.join`. Those two functions do not
  read the variable; their callers pass its value. `lupin roadmap` and the
  GitHub cache (`gh_cache._resolve`) do not read the variable. They read the
  default path.
- **Set by**: the user or the host config.
- **Option**: `--config-path`.

### `LUPIN_CMD_SIGNING_KEY`

- **Purpose**: one signing key for the command queue.
- **Default**: none.
- **Effect**:
  - `lupin agent` checks each command with this key. When the key is unset,
    the agent reads its own key from `LUPIN_CMD_SIGNING_KEYS_DIR`.
  - `cmd send` signs each command with this key.
  - Some commands sign with this key when they have a remote `--machine`:
    `run`, `stop`, `peek`, `loops`, `schedule`, `pause`, and `resume`.
  - These commands use one key. Give the key of the target machine.
  - `lupin serve` uses it only when `LUPIN_CMD_SIGNING_KEYS_DIR` is unset
    (including `--cmd-signing-key-dir`) and the worker has no systemd
    credential file.
- **Read by**: `src/lupin/cli.py` (`_signing_key_arg`), `src/lupin/serve.py`
  (`main`, `Handler._signing_key_for`).
- **Set by**: the host config, through a secret manager. Do not put it in a
  command line or in a global environment file.
- **Option**: `--signing-key`. For `serve`: `--cmd-signing-key`.

### `LUPIN_CMD_SIGNING_KEYS_DIR`

- **Purpose**: a directory with one signing key file for each worker.
- **Default**: none.
- **Format**: each file name is the machine name of a worker. The machine
  name is the host name (`socket.gethostname()`) unless the worker runs
  `lupin agent --machine NAME`. Lupin removes space at the start and end.
- **Effect**: `fleet-run`, `lupin serve`, and `lupin agent` read a key from
  this directory. `lupin agent` reads the key of its own machine. Lupin
  finds the key in this order:
  1. When the variable is set, `<dir>/<machine>`. If that file is missing,
     the machine has no key.
  2. When the variable is not set, `$CREDENTIALS_DIRECTORY/<machine>`, if
     that file exists.
  3. Else, for `lupin serve` only, `LUPIN_CMD_SIGNING_KEY`.
- **Read by**: `src/lupin/commands.py` (`signing_key_for`),
  `src/lupin/cli.py` (`_cmd_fleet_run`), `src/lupin/serve.py` (`main`).
- **Set by**: the host config of the sending machine.
- **Option**: `--cmd-signing-key-dir` (`serve` only).

`lupin agent` reads the key of its own machine from this directory. You can
also give it one key with `LUPIN_CMD_SIGNING_KEY` or `--signing-key`. That
key wins over the directory.

### `LUPIN_GH_CANONICAL_FETCHER`

- **Purpose**: the host name of the machine that runs `gh` for the shared
  GitHub cache.
- **Default**: none. No machine is named.
- **Effect**: Lupin compares the value with `socket.gethostname()`.
  - The named machine fetches on a cache miss. It also fetches when Redis
    is down.
  - Another machine reads only the cache while the named machine is
    `online` in the fleet registry. When the named machine is draining,
    offline, or not registered, another machine fetches. When Redis is
    down, another machine does not fetch.
  - With no named machine, a miss takes a lock for that cache key. The
    machine with the lock fetches. A machine without the lock waits a
    short time for the cache. If the cache is still empty, it reports
    "fetch in progress" and does not fetch.
- **Read by**: `src/lupin/gh_cache.py` (`canonical_fetcher`, read on each
  call).
- **Set by**: the host config. Set the same value on every machine.
  `lupin run` copies it into loop units.

## Dashboard

### `LUPIN_LOOP_COORDINATOR_ONLY`

- **Purpose**: marks the machine that runs `lupin serve` as a dispatch
  machine that runs no loops.
- **Default**: unset. The local machine can run loops.
- **Effect**: only the value `1` turns it on. Then the dashboard does not
  offer the local machine for "Run now". It refuses a start request for the
  local machine.
- **Read by**: `src/lupin/serve.py` (`_coordinator_only`).
- **Set by**: the host config of the dispatch machine.

### `LUPIN_ROADMAP_FRAGMENT_TTL`

- **Purpose**: how long the dashboard keeps a rendered roadmap fragment.
- **Default**: `30` (seconds).
- **Effect**: `/roadmap/board` uses a kept fragment for this many seconds.
  Lupin reads the value when it is used. `lupin serve` checks it at
  startup and exits 2 with one line that names the variable. Other
  commands do not read it.
- **Read by**: `src/lupin/serve.py` (`_fragment_ttl`, used by
  `_cached_fragment`; checked by `main`).
- **Set by**: the user or the host config.

## Models and quota

### `LUPIN_MODEL_TIERS`

- **Purpose**: a JSON file with the model tiers for routing.
- **Default**: the bundled file `src/lupin/model-tiers.json`.
- **Effect**: `route`, `review-route`, the Models page, and the benchmark
  fallback read this file. An empty value is an error:
  `LUPIN_MODEL_TIERS must name a file`. The file format is in `README.md`.
  On quota fallback, `route` needs a live price snapshot no more than seven
  days old. It uses only a model whose known input, output, and cache prices
  are no higher than the blocked model. If prices are missing or unclear,
  `route` waits for quota. Run `lupin fetch-models` to refresh the snapshot.
- **Read by**: `src/lupin/model_tiers.py` (`path`, `load`),
  `src/lupin/serve.py` (`_model_tiers_path`).
- **Set by**: the user.

### `LUPIN_WEEKLY_QUOTA_DAILY_TARGETS`

- **Purpose**: set daily usage targets for weekly quota windows.
- **Default**: Claude gets `90/7` percentage points per elapsed day. Other
  providers use the existing 95% reserve.
- **Effect**: use a JSON object to map quota provider names to daily
  percentage points. Values must be finite numbers from 0 to 100. Entries
  override defaults. Lupin multiplies each rate by elapsed days since the
  weekly reset. It caps Claude at 90% and other providers at 95%. Usage at
  the exact target is allowed. Lupin blocks a configured weekly row with no
  reset time. For example, `{"claude": 15, "openai": 12}` sets both rates.
- **Read by**: `src/lupin/pace.py`, used by route, profile selection, and
  quota account choice.
- **Set by**: the user or host config. Loop units get this variable when set.

### `OPENCODE_API_KEY`

This variable does not start with `LUPIN_`. The test does not check it.

- **Purpose**: the OpenCode Go API key.
- **Default**: none.
- **Effect**: Lupin uses it when the OpenCode auth file has no `api` key
  for `opencode-go`. The auth file is
  `$XDG_DATA_HOME/opencode/auth.json` (default
  `~/.local/share/opencode/auth.json`). The key order is:
  - `lupin quota`: the auth file, then this variable, then the output of
    `omp token opencode-go`.
  - `lupin fetch-models`: the auth file, then this variable.
- **Read by**: `src/lupin/quota.py` (`opencode_go_quota`),
  `src/lupin/model_fetch.py` (`opencode_go_models`).
- **Set by**: the user or the host config.

`lupin quota` and `lupin fetch-models` also read the login files of Claude,
Codex, OpenCode, and `omp`. Those files belong to the tools. Their paths
are in `src/lupin/quota.py` and `src/lupin/model_fetch.py`.

## Variables that loop units get

`lupin run`, `lupin once`, and the loop recovery copy these variables into
each loop unit when they are set (`loop_runtime._service_environment`):

`HOME`, `LUPIN_BACKEND`, `LUPIN_REDIS_HOST`, `LUPIN_REDIS_PORT`,
`LUPIN_REDIS_USERNAME`, `LUPIN_STATE_ROOT`, `LUPIN_EXECUTABLE`,
`LUPIN_HERDR_BIN`, `LUPIN_GH_CANONICAL_FETCHER`, `LUPIN_LOOP_STATE_DIR`,
`LUPIN_LOOP_CODE_DIR`, `LUPIN_LOOP_USER`, `LUPIN_LOOP_GROUP`,
`LUPIN_LOOP_DEFAULT_REPOS`, `LUPIN_LOOP_CLAUDE_MAX_CONCURRENT`,
`LUPIN_WEEKLY_QUOTA_DAILY_TARGETS`,
`LUPIN_LOOP_CREDENTIALS_FILE`, `LUPIN_LOOP_REDIS_PASSWORD_FILE`,
`LUPIN_LOOP_PROMPT_FILE`, `LUPIN_LOOP_PATH`, `ANTHROPIC_BASE_URL`,
`HEADROOM_TELEMETRY_DISABLED`, `RTK_TELEMETRY_DISABLED`,
`CODEGRAPH_TELEMETRY`, `DO_NOT_TRACK`.

Loop units do not get `LUPIN_REDIS_PASSWORD`, `LUPIN_CMD_SIGNING_KEY`,
`LUPIN_CMD_SIGNING_KEYS_DIR`, `LUPIN_FLEET_CONFIG`, `LUPIN_MODEL_TIERS`,
`LUPIN_ROADMAP_FRAGMENT_TTL`, or `LUPIN_LOOP_COORDINATOR_ONLY`.

## Systemd credentials

systemd gives a credential to a service as a file in
`$CREDENTIALS_DIRECTORY`. Lupin reads these credentials:

<!-- markdownlint-disable MD013 -->
| Name | Content | Read by | Given by |
| --- | --- | --- | --- |
| `redis-password` | The Redis password. | `machines._redis_password`, `loop_runtime._redis_kwargs`, `loop_runtime.worker` | The host config, or `lupin run` from `LUPIN_LOOP_REDIS_PASSWORD_FILE`. |
| `gh-token` | A GitHub token for one repo. | `loop_runtime.herdr_server` | `lupin run`, from `LUPIN_LOOP_CREDENTIALS_FILE`, for a per-repo Herdr session only. The NixOS module's `systemd-run` wrapper allows it only when the source is one of the absolute paths in that file. |
| `<machine>` | The signing key of that worker. | `commands.signing_key_for` | The host config of the machine that reads or sends commands. The NixOS module gives the agent its own key this way. |
<!-- markdownlint-enable MD013 -->

## Files

### `~/.config/lupin/fleet.json`

- **Purpose**: the coordinator location of this machine.
- **Written by**: `lupin join HOST[:PORT]` (`machines.join`). The port
  default is `6379`.
- **Content**: `redis_host`, `redis_port`, and `redis_username` when you
  give one. The file never holds the password.
- **Read by**: `machines.load_config`, through `machines.resolve_connection`.
  A missing file counts as empty.
- **Path**: `LUPIN_FLEET_CONFIG` or `--config-path` can change it.

### `~/.config/lupin/ssh-targets`

- **Purpose**: the SSH target of each worker, for `lupin attach`.
- **Format**: one `<machine> <target>` pair on each line. Lupin ignores
  blank lines and lines that start with `#`. The target is `user@host` or
  an SSH config alias.
- **Effect**: `attach --machine M` runs `herdr --remote <target>`. With no
  line for `M`, `attach` fails. It does not guess a host name. The
  dashboard shows the attach command from the same file.
- **Read by**: `src/lupin/loops.py` (`ssh_target_for`).
- **Path**: fixed. No variable changes it.
- **Set by**: the user or the host config. This file is local. Lupin does
  not share it through Redis.

### Model tiers file

- **Path**: `LUPIN_MODEL_TIERS`, or the bundled `src/lupin/model-tiers.json`.
- **Format**: see "Quick start" in `README.md`.

### Files in the loop state directory

These files are in `LUPIN_LOOP_STATE_DIR`:

- `repos`: the enabled repos. One `<repo> [platform]` pair on each line.
  Lupin ignores blank lines and lines that start with `#`. The platform
  default is `claude`. `lupin enable` and `lupin disable` change it.
- `orchestrators.json`: the orchestrator profile of each repo. `lupin
  enable --orchestrator` changes it.
- `timer-dropin.d/override.conf`: `lupin schedule first` and `lupin
  schedule cal` write it. Then Lupin restarts `delegation-loop.timer`. The
  host config must make systemd read this file for that timer.

### Files that Lupin writes as a cache

<!-- markdownlint-disable MD013 -->
| Path | Content | Written by |
| --- | --- | --- |
| `$XDG_STATE_HOME/lupin/cache.json` (default `~/.local/state/lupin/cache.json`) | GitHub data and repo identity for the dashboard. | `src/lupin/roadmap.py` |
| `$XDG_STATE_HOME/lupin/model-snapshot.json` (default `~/.local/state/lupin/model-snapshot.json`) | The `fetch-models` snapshot. | `src/lupin/model_fetch.py` |
<!-- markdownlint-enable MD013 -->
