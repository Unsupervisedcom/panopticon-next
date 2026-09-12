# Repository Source Selection

## Overview

Quickstart selects a concrete Git source before registering a repository. The current checkout is
a convenient suggestion, while an explicit Git URL, local checkout, or Git bundle remains a valid
choice. Repository identity comes from the selected source rather than its friendly name, so two
unrelated projects named `widget` can coexist without either registration being reused or changed.

The task service continues to store the selected source in the repository's existing `git_url`
field. This specification changes source selection and registration behavior; it does not migrate
or rewrite repositories that are already registered.

## Requirements

### 1: Selection

1. When the current working directory belongs to an identifiable repository, quickstart MUST: (a)
   suggest that repository; and (b) display its friendly name and exact selected source before
   registration.
2. Quickstart MUST allow the operator to replace the suggestion with an explicit supported Git URL,
   local Git checkout path, or Git bundle path.
3. When the selected checkout has no `remote.origin.url`, its source MUST be the checkout's
   canonical absolute path.
4. When no current-checkout suggestion can be identified, quickstart MUST request an explicit
   source rather than beginning repository registration with a fallback repository.
5. The confirmed selection MUST be the source passed unchanged to repository registration.

The selector is a reusable helper with injectable working-directory and input dependencies so the
terminal entry point and deterministic tests can call the same behavior.

### 2: Validation and presentation

1. A local checkout source MUST resolve to an existing Git worktree or bare Git repository before
   registration.
2. A local bundle source MUST resolve to an existing regular file that `git bundle verify` accepts
   before registration.
3. An invalid local checkout or bundle MUST: (a) be rejected with the selected path identified; and
   (b) cause no repository registration, runtime startup, task creation, tmux mutation, container
   mutation, or image mutation. Harness setup explicitly saved before source selection may remain.
4. A Git bundle MUST be described as a fixed snapshot in selection and confirmation output.
5. A bundle's friendly name MUST be its filename stem with one `.bundle` suffix removed.
6. A local checkout's friendly name MUST be the checkout directory's name.
7. A remote's friendly name MUST be the final nonempty repository path component with one `.git`
   suffix removed.
8. A remote containing an HTTPS user-info field, password, query string, or fragment MUST be
   rejected without reproducing the credential-bearing source in output.

### 3: Source equivalence

1. Exact source matches after trimming surrounding whitespace MUST identify an existing repository.
2. Equivalent GitHub HTTPS and SSH spellings MUST identify the same source when their host,
   owner path, and repository path match case-insensitively after removing one terminal `.git`
   suffix and trailing separators.
3. GitHub source equivalence MUST NOT equate different hosts, owners, or repository paths.
4. Local source equivalence MUST compare canonical absolute paths without removing a `.git` suffix
   from a path component.
5. Bundle source equivalence MUST compare canonical absolute paths without removing the bundle's
   filename suffix.
6. Non-GitHub remote source equivalence MUST use exact trimmed source text without inferring
   equivalence between different URL or transport spellings.

### 4: Identity and collision handling

1. A new repository ID MUST combine a friendly source stem with a stable source-derived suffix so
   unrelated sources with the same basename receive distinct IDs.
2. Re-selecting an equivalent source MUST reuse its existing repository ID.
3. A repository MUST NOT be reused solely because its ID, friendly name, or source basename matches
   the selected source.
4. If a create request conflicts, quickstart MUST refresh registered repositories and reuse a
   repository only when its stored source is equivalent to the selected source.
5. If a create request conflicts and no equivalent stored source exists, quickstart MUST: (a) retry
   with a distinct deterministic candidate ID or fail explicitly; and (b) never report the
   conflicting repository as the selected source.
6. Registering or re-selecting a source MUST NOT change another repository's source, harness,
   model, credential bindings, or enabled workflows.
7. Source selection and registration MUST preserve an existing repository's explicit harness,
   model, environment-file, credential-directory, and other credential bindings. A later explicit
   replacement through foreground setup may change them.
8. Quickstart MUST NOT write a selected default harness to an already-registered repository.

### 5: Workflow capability

1. GitHub workflows MUST be enabled only for sources whose parsed host is `github.com` and whose
   transport is a supported HTTPS, SSH URL, or scp-like SSH form.
2. Hostname comparison for GitHub workflow eligibility MUST: (a) be case-insensitive; and (b)
   reject suffix, prefix, user-info, and port constructions that merely contain `github.com`.
3. Local checkouts, local bundles, `file://` sources, and non-GitHub remotes MUST receive only the
   forge-free local Git workflow from quickstart.
4. Re-selecting an existing repository MAY add missing workflows appropriate to that equivalent
   source while preserving its other enabled workflows.

### 6: Compatibility

1. Existing registered repositories MUST retain their IDs and stored source strings without
   migration.
2. Existing explicit repository bindings MUST remain authoritative after quickstart is upgraded or
   rerun.
3. The source selector and repository setup functions MUST remain free of LLM calls.
