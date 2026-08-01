"""Operator provider policy for kanban worker dispatch (t_e6c9ccaf).

A **hard stop**: when an operator declares that an inference provider must
not be used, the kanban dispatcher must never create a worker process whose
effective provider is that provider. No subprocess, no inference connection,
no tokens.

Why this exists
---------------
The dispatcher spawns ``hermes -p <assignee> ... chat -q`` subprocesses. The
worker's provider is resolved *inside the child* from (in precedence order,
see ``cli.py`` ``requested_provider``):

1. ``--provider <name>`` on argv — the dispatcher passes this only when the
   task carries BOTH ``model_override`` and ``provider_override``
   (:func:`hermes_cli.kanban_db._default_spawn`).
2. the assignee profile's ``config.yaml`` ``model.provider``.
3. ``$HERMES_INFERENCE_PROVIDER`` inherited from the dispatcher's env.
4. ``"auto"`` — resolved lazily at first use from stored credentials /
   ``auth.json`` ``active_provider``, which can land on *any* logged-in
   provider.

By the time a worker could report which provider it picked, the process
already exists and may already have opened a billed connection. So the gate
has to run in the *parent*, before the spawn, and it has to be conservative
about step 4: an unresolved provider is treated as a possible match.

Policy surface
--------------
Two operator controls, both **off by default** (empty ⇒ this module is a
no-op and dispatch behaviour is byte-for-byte unchanged):

``HERMES_KANBAN_BLOCKED_PROVIDERS`` (env, highest precedence)
    Comma/whitespace-separated provider names. Present-and-empty
    (``HERMES_KANBAN_BLOCKED_PROVIDERS=``) is an explicit *disable* that
    overrides config — the escape hatch for a one-off CLI dispatch.

``kanban.blocked_providers`` (config.yaml, list or comma string)
    Persistent policy. Read fresh on every dispatcher tick, so flipping it
    takes effect on the next tick without a gateway restart (same contract
    as ``kanban.auto_decompose``, #49638).

Names are normalized through :func:`hermes_cli.models.normalize_provider` —
the same alias/case normalization the rest of provider resolution uses. No
new aliases are invented here.

Fail-closed rule
----------------
When a policy is active and the effective provider cannot be pinned to a
concrete, *allowed* name, the spawn is denied (``provider_unresolved``). A
profile that clearly declares an unrelated provider (``model.provider:
openai``) is never denied by an unrelated config error — resolution has to
be genuinely ambiguous for the fail-closed branch to fire.

A malformed *policy* value is a different matter: it is reported (once) and
treated as "no policy", because turning an operator typo into a fleet-wide
block is a worse failure than the guard being inert. The malformed value is
logged at WARNING so it is visible.

This module performs no I/O beyond reading config/profile YAML, never
touches credentials, and never records a balance.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

_log = logging.getLogger(__name__)

#: Environment override. Wins over ``kanban.blocked_providers``. Present but
#: empty means "policy explicitly disabled for this process".
BLOCKED_PROVIDERS_ENV = "HERMES_KANBAN_BLOCKED_PROVIDERS"

#: Config key holding the persistent policy (a list, or a comma string).
BLOCKED_PROVIDERS_CONFIG_KEY = "blocked_providers"

#: Env var the worker would inherit as provider precedence step 3.
INFERENCE_PROVIDER_ENV = "HERMES_INFERENCE_PROVIDER"

# --- decision reasons (stable, machine-readable; persisted on the card) ---
REASON_POLICY_INACTIVE = "policy_inactive"
REASON_ALLOWED = "provider_allowed"
REASON_BLOCKED = "provider_blocked"
REASON_UNRESOLVED = "provider_unresolved"

# --- where the effective provider came from -------------------------------
SOURCE_TASK_OVERRIDE = "task_override"
SOURCE_MODEL_OVERRIDE_PREFIX = "model_override_prefix"
SOURCE_PROFILE_CONFIG = "profile_config"
SOURCE_ENV_INFERENCE_PROVIDER = "env_inference_provider"
SOURCE_UNRESOLVED = "unresolved"

#: Providers that are never concrete: the child would still have to choose.
_AMBIGUOUS_PROVIDER_NAMES = frozenset({"auto"})

#: Malformed policy values already reported, so a per-tick dispatcher loop
#: does not reprint the same warning forever.
_warned_bad_policy: set = set()


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of evaluating one task against the operator provider policy.

    ``allowed`` is the only field the dispatcher branches on. The rest is
    the deterministic record persisted on the card so an operator can see
    *why* a spawn was refused without reading dispatcher logs.
    """

    allowed: bool
    reason: str
    provider: Optional[str]
    source: str
    policy: Tuple[str, ...]
    detail: str

    @property
    def active(self) -> bool:
        """True when an operator policy was configured for this evaluation."""
        return bool(self.policy)

    def as_event_payload(self) -> dict:
        """Deterministic payload for the ``provider_policy_denied`` event."""
        return {
            "reason": self.reason,
            "provider": self.provider,
            "source": self.source,
            "policy": list(self.policy),
            "detail": self.detail,
        }


def normalize_provider_name(value: Any) -> Optional[str]:
    """Normalize one provider name, or ``None`` when it carries no name.

    Delegates to :func:`hermes_cli.models.normalize_provider` so aliases and
    casing resolve exactly as they do everywhere else. That function defaults
    an empty input to ``"openrouter"``, which would be a dangerous surprise
    here, so empties are filtered out *before* the call and map to ``None``
    ("no name given") instead.

    Falls back to plain casefolding if ``hermes_cli.models`` is unimportable
    (minimal test envs). Canonical ids — ``nous`` among them — are unchanged
    by alias expansion, so the fallback still matches them correctly.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        from hermes_cli.models import normalize_provider
    except Exception:  # pragma: no cover - exercised via the fallback test
        return text.lower()
    try:
        normalized = normalize_provider(text)
    except Exception:
        return text.lower()
    normalized = (normalized or "").strip().lower()
    return normalized or text.lower()


def _coerce_policy_names(raw: Any, *, origin: str) -> Optional[frozenset]:
    """Parse a policy value into a normalized provider set.

    Accepts a comma/whitespace-separated string or any iterable of strings.
    Returns ``None`` (not an empty set) when the value is structurally wrong,
    so the caller can distinguish "operator typo" from "no policy set".
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        parts: Iterable[Any] = raw.replace(",", " ").split()
    elif isinstance(raw, (list, tuple, set, frozenset)):
        parts = raw
    else:
        _warn_bad_policy(raw, origin=origin)
        return None
    names = set()
    for part in parts:
        if not isinstance(part, str):
            _warn_bad_policy(raw, origin=origin)
            return None
        for token in part.replace(",", " ").split():
            normalized = normalize_provider_name(token)
            if normalized:
                names.add(normalized)
    return frozenset(names)


def _warn_bad_policy(raw: Any, *, origin: str) -> None:
    key = (origin, repr(raw))
    if key in _warned_bad_policy:
        return
    _warned_bad_policy.add(key)
    _log.warning(
        "kanban provider policy: ignoring malformed %s value %r — expected a "
        "list of provider names or a comma-separated string. The dispatch "
        "guard is INACTIVE until this is fixed.",
        origin, raw,
    )


def load_blocked_providers(
    *,
    config: Optional[dict] = None,
    env: Optional[dict] = None,
) -> frozenset:
    """Return the normalized set of providers the operator has blocked.

    Precedence (first match wins):

    1. ``HERMES_KANBAN_BLOCKED_PROVIDERS`` when **present** in the
       environment. An empty value disables the policy outright — it does
       not fall through to config.
    2. ``kanban.blocked_providers`` from ``config.yaml``.
    3. Empty set (no policy — dispatch is unchanged).

    ``config`` / ``env`` are injectable for tests; production callers pass
    neither and get a fresh read on every tick.
    """
    environ = os.environ if env is None else env
    if BLOCKED_PROVIDERS_ENV in environ:
        parsed = _coerce_policy_names(
            environ.get(BLOCKED_PROVIDERS_ENV), origin=BLOCKED_PROVIDERS_ENV,
        )
        # Malformed env → treat as no policy (see module docstring); an
        # explicitly empty env value is the documented off-switch.
        return parsed if parsed is not None else frozenset()

    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception as exc:
            _log.debug("kanban provider policy: config unreadable (%s)", exc)
            return frozenset()
    kanban_cfg = cfg.get("kanban") if isinstance(cfg, dict) else None
    if not isinstance(kanban_cfg, dict):
        return frozenset()
    parsed = _coerce_policy_names(
        kanban_cfg.get(BLOCKED_PROVIDERS_CONFIG_KEY),
        origin="kanban." + BLOCKED_PROVIDERS_CONFIG_KEY,
    )
    return parsed if parsed is not None else frozenset()


def _profile_configured_provider(assignee: Optional[str]) -> Optional[str]:
    """Return the assignee profile's configured ``model.provider``.

    ``None`` means "could not pin it" — missing profile dir, missing or
    malformed ``config.yaml``, or a config that simply does not name a
    provider. All of those leave the worker to resolve the provider itself
    (``auto``), which is exactly the ambiguity the fail-closed branch exists
    for. Reuses :func:`hermes_cli.profiles._read_config_model`, the existing
    reader for this key, so the gate sees what ``hermes profile show`` sees.
    """
    if not assignee:
        return None
    try:
        from hermes_cli.profiles import _read_config_model, get_profile_dir
    except Exception as exc:  # pragma: no cover - exotic env
        _log.debug("kanban provider policy: profiles module unavailable (%s)", exc)
        return None
    try:
        _model, provider = _read_config_model(get_profile_dir(assignee))
    except Exception as exc:
        _log.debug(
            "kanban provider policy: could not read profile %r config (%s)",
            assignee, exc,
        )
        return None
    return normalize_provider_name(provider)


def _model_override_provider_prefix(model_override: Optional[str]) -> Optional[str]:
    """Return the provider named by a ``provider:model`` override, if any.

    ``hermes_cli.models.parse_model_input`` is the canonical parser for that
    syntax (it only treats the colon as a delimiter when the left side is a
    recognized provider name/alias). Passing an empty ``current_provider``
    makes a non-empty return mean "the override explicitly named a provider".

    The dispatcher's own ``-m`` argv does not currently re-split this prefix,
    so this is deliberately conservative: a card whose model override *reads*
    as a blocked provider is denied rather than reasoned about.
    """
    if not model_override:
        return None
    try:
        from hermes_cli.models import parse_model_input
    except Exception:  # pragma: no cover - exotic env
        return None
    try:
        provider, _model = parse_model_input(str(model_override), "")
    except Exception:
        return None
    return normalize_provider_name(provider)


def resolve_effective_providers(
    *,
    assignee: Optional[str],
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
    env: Optional[dict] = None,
) -> Tuple[Tuple[Optional[str], str], ...]:
    """Return every provider the spawned worker could end up using.

    Each entry is ``(normalized_provider_or_None, source)``. ``None`` means
    the worker would fall through to ``auto`` and pick from credentials —
    unpinnable from here.

    The primary candidate mirrors ``cli.py``'s ``requested_provider`` chain:

    * ``provider_override`` **only when** ``model_override`` is also set —
      :func:`hermes_cli.kanban_db._default_spawn` emits ``--provider`` inside
      the ``if task.model_override:`` branch, so a bare provider override
      never reaches argv and must not be mistaken for one that does. This is
      what makes "explicitly overridden away from a blocked provider" work:
      an effective ``--provider openai`` replaces the profile default rather
      than being unioned with it.
    * otherwise the profile's ``model.provider``,
    * otherwise ``$HERMES_INFERENCE_PROVIDER`` (the worker inherits the
      dispatcher's environment),
    * otherwise unresolved.

    A ``provider:model`` prefix on the model override is added as an extra
    candidate rather than replacing the primary one, because it is a signal
    of intent that the argv builder does not currently honour.
    """
    environ = os.environ if env is None else env
    candidates: list = []

    explicit = None
    if model_override and provider_override:
        explicit = normalize_provider_name(provider_override)
    if explicit is not None:
        candidates.append((explicit, SOURCE_TASK_OVERRIDE))
    else:
        profile_provider = _profile_configured_provider(assignee)
        if profile_provider is not None:
            candidates.append((profile_provider, SOURCE_PROFILE_CONFIG))
        else:
            env_provider = normalize_provider_name(
                environ.get(INFERENCE_PROVIDER_ENV)
            )
            if env_provider is not None:
                candidates.append((env_provider, SOURCE_ENV_INFERENCE_PROVIDER))
            else:
                candidates.append((None, SOURCE_UNRESOLVED))

    prefix_provider = _model_override_provider_prefix(model_override)
    if prefix_provider is not None:
        candidates.append((prefix_provider, SOURCE_MODEL_OVERRIDE_PREFIX))

    return tuple(candidates)


def evaluate_task(
    *,
    assignee: Optional[str],
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
    blocked: Optional[frozenset] = None,
    env: Optional[dict] = None,
    config: Optional[dict] = None,
) -> PolicyDecision:
    """Decide whether a worker may be spawned for this task.

    Returns an *allowed* decision immediately when no policy is configured,
    so the default install pays one frozenset check and nothing else.
    """
    policy = load_blocked_providers(config=config, env=env) if blocked is None else blocked
    if not policy:
        return PolicyDecision(
            allowed=True,
            reason=REASON_POLICY_INACTIVE,
            provider=None,
            source=SOURCE_UNRESOLVED,
            policy=(),
            detail="no blocked-provider policy configured",
        )

    policy_tuple = tuple(sorted(policy))
    candidates = resolve_effective_providers(
        assignee=assignee,
        model_override=model_override,
        provider_override=provider_override,
        env=env,
    )

    # 1. A concrete match is the unambiguous denial. Checked first so the
    #    persisted reason names the provider that was actually blocked.
    for provider, source in candidates:
        if provider is not None and provider in policy:
            return PolicyDecision(
                allowed=False,
                reason=REASON_BLOCKED,
                provider=provider,
                source=source,
                policy=policy_tuple,
                detail=(
                    f"effective provider {provider!r} (from {source}) is "
                    f"blocked by operator policy"
                ),
            )

    # 2. Fail closed: an unpinnable provider could still resolve to a blocked
    #    one inside the child (``auto`` reads stored credentials). Refuse.
    for provider, source in candidates:
        if provider is None:
            return PolicyDecision(
                allowed=False,
                reason=REASON_UNRESOLVED,
                provider=None,
                source=source,
                policy=policy_tuple,
                detail=(
                    "effective provider could not be resolved (no profile "
                    "model.provider and no inference-provider env); the "
                    "worker would fall through to credential-based 'auto' "
                    "selection, which could reach a blocked provider"
                ),
            )
        if provider in _AMBIGUOUS_PROVIDER_NAMES:
            return PolicyDecision(
                allowed=False,
                reason=REASON_UNRESOLVED,
                provider=provider,
                source=source,
                policy=policy_tuple,
                detail=(
                    f"effective provider is {provider!r} (from {source}); it "
                    f"resolves from stored credentials at worker startup and "
                    f"could reach a blocked provider"
                ),
            )

    provider, source = candidates[0]
    return PolicyDecision(
        allowed=True,
        reason=REASON_ALLOWED,
        provider=provider,
        source=source,
        policy=policy_tuple,
        detail=f"effective provider {provider!r} is not blocked",
    )
