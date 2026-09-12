# Runtime startup readiness

## Overview

Integrated startup currently treats a named tmux session as evidence that its service or runner is
usable. A session may instead be inert, may have inherited stale environment values from the tmux
server, or may belong to a different Panopticon installation. The resulting failure is misleading:
startup appears successful while new tasks remain queued indefinitely. Running Alembic before
identifying a live service can also migrate a database underneath an older process that still has
it open.

This contract gives one foreground invocation a resolved runtime identity and passes the complete
set of non-secret launch controls explicitly into every integrated tmux child. Startup probes an
authenticated identity endpoint and the live-runner registry within a fixed deadline. A compatible
API revision, rather than byte-for-byte package-version equality, determines whether two releases
can cooperate. The package version remains diagnostic evidence.

The runtime identity is a digest of the resolved executable/install location, service address,
data/config/cache/state locations, database target, and controlled tool settings. It contains
no secret value or raw path. The service echoes this identity; equality proves that the responding
process was launched for the intended local runtime rather than merely being some HTTP server whose
health endpoint returns 200. Package upgrades within the same managed installation retain the same
executable/install location; package version is deliberately excluded from the digest and reported
separately.

## Requirements

### 1: One explicit child environment

1. Integrated startup MUST resolve one runtime environment before starting or reusing background
   sessions.
2. Every integrated tmux child MUST first remove every Panopticon runtime control, XDG path input,
   executable or temporary path input, and supported Docker client setting from the tmux server's
   inherited environment.
3. Every integrated tmux child MUST set exactly the controlled values selected by the foreground
   invocation after removing inherited values.
4. An unset controlled value MUST remain absent in the child rather than falling back to a stale
   tmux-server value.
5. The child command line MUST NOT contain bearer tokens, API keys, credential-file contents, or
   other secret values.
6. A credential-file pathname MAY be passed as a private reference.
7. The resolved environment MUST explicitly carry the service address, container service address,
   runner id, runtime identity, executable search path, temporary path inputs, Panopticon/XDG
   storage inputs, and Docker client settings used by service or runner startup.
8. Integrated startup MUST reject a command-line runtime control that embeds a password, token,
   API key, or credential-like query value without echoing that value in the failure.
9. The launched service port and the default container callback port MUST match the selected local
   service address.

### 2: Authenticated service identity

1. The task service MUST expose an authenticated identity response containing a stable service
   kind, runtime API revision, package version, and the non-secret runtime identity supplied at
   process launch.
2. The identity response MUST use the `fleet-read` authorization class defined by the credential
   route inventory.
3. Integrated startup MUST require the service kind to equal the intended value.
4. Integrated startup MUST require the runtime identity to equal the intended value.
5. A successful `/healthz` response alone MUST NOT establish service readiness.
6. Runtime API revision `1` MUST be the only revision supported by this client implementation.
7. Integrated startup MUST require the runtime API revision to be supported.
8. Integrated startup MUST NOT reject a service solely because its package version differs.
9. A service from the v0.2.11 baseline, which has no identity response, MUST be classified as an
   unverified legacy service.
10. Startup MUST leave an unverified legacy service running.
11. Startup MUST refuse mutation when an unverified legacy service responds.
12. Startup MUST direct the operator to stop or upgrade an unverified legacy fleet deliberately.
13. Authentication rejection MUST produce an actionable failure distinct from other verification
   failures.
14. A malformed identity MUST produce an actionable failure distinct from other verification
   failures.
15. An unsupported API revision MUST produce an actionable failure distinct from other verification
   failures.
16. A different runtime identity MUST produce an actionable failure distinct from other verification
    failures.

### 3: Migration guard

1. Before automatic Alembic migration, integrated startup MUST probe the intended service address.
2. When no service accepts a connection at the intended address and no integrated service tmux
   session exists, the guard MUST report that no live service was found and permit migration.
3. When no service accepts a connection at the intended address but an integrated service tmux
   session exists, the guard MUST refuse migration because that process may be starting or inert
   while holding the database.
4. When a verified compatible intended service is already live, the guard MUST report it.
5. Integrated startup MUST skip migration when the guard reports a verified compatible intended
   service.
6. When a service responds but cannot be verified, the guard MUST refuse migration. This includes
   authentication rejection, legacy/no identity response, malformed identity, unsupported runtime
   API revision, and a different runtime identity.
7. The guard MUST NOT stop any service, runner, or tmux session.
8. The guard MUST NOT restart any service, runner, or tmux session.
9. The guard MUST NOT mutate any task, repository, or database.

### 4: Bounded service and runner readiness

1. After session creation or reuse, integrated startup MUST wait no longer than its configured
   readiness deadline for the intended authenticated service identity.
2. After session creation or reuse, integrated startup MUST wait no longer than its configured
   readiness deadline for the intended runner id to appear in the service's live-runner registry.
3. Integrated startup MUST require the intended live runner registration to carry the intended
   runtime identity.
4. A tmux `service` or `runner` session MUST NOT by itself satisfy readiness.
5. Authentication rejection, wrong service kind, wrong runtime identity, and unsupported API
   revision MUST fail immediately rather than consume the whole deadline.
6. A connection delay and a missing intended runner MAY retry until the deadline.
7. Deadline expiry MUST distinguish an unreachable service from a service that is ready but lacks
   the intended runner.
8. A missing-runner error SHOULD include the ids of other live runners when any are connected.
9. A missing-runner error MUST direct the operator to the runner log.
10. A missing-runner error MUST NOT expose secrets.
11. Readiness checks MUST use bounded HTTP operations and an injected monotonic clock/sleeper in
   deterministic tests.
12. Offline connection configuration MAY occur before service readiness because it does not mutate
    registered fleet state.
13. Repository registration or reuse MUST occur only after service and runner readiness succeeds.
14. Credential changes to an already registered repository MUST occur only after service and runner
    readiness succeeds.
15. Credential changes to an already registered repository MUST retain the repository execution
    hold for the duration required by the foreground-setup contract.

### 5: Fleet preservation

1. Verified healthy service and runner sessions MUST be reused.
2. Unknown, legacy, incompatible, or inert sessions MUST remain untouched.
3. Startup MUST fail with diagnosis rather than adopting an unknown, legacy, incompatible, or inert
   session.
4. Readiness evaluation MUST NOT create tasks.
5. Readiness evaluation MUST NOT change claims.
6. Readiness evaluation MUST NOT modify repositories.
7. Readiness evaluation MUST NOT invoke an LLM or make a real model call.

## Integration contract

The terminal entrypoint calls the runtime migration guard immediately before its automatic
migration. It migrates only when the guard reports `service_absent`; it skips migration when the
guard reports `compatible_service_live`; every verification error exits before migration. After
starting missing tmux sessions, it calls the bounded readiness wait before repository registration
or reuse, registered-repository edits, work creation, or presenting the fleet as executable.
Connection configuration that has no registered repository to mutate may run offline before this
runtime sequence.

## Non-goals

- Automatically stopping or replacing an existing runtime is outside this slice.
- Database schema negotiation or migration while a service is live is outside this slice.
- Remote-runner selection, task eligibility by workflow/capability, and image-build progress are
  outside this local integrated-startup check.
- Changing authentication storage or putting credential material into the runtime identity is
  outside this slice.
