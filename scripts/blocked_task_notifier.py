#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, subprocess, tempfile, time
from pathlib import Path
from hermes_cli import kanban_db as kb

BOARDS_DIR = Path(os.environ.get('BOARDS_DIR', '/home/frank/.hermes/kanban/boards'))
STATE_DIR = Path(os.environ.get('STATE_DIR', '/home/frank/.hermes/cron/state'))
STATE = Path(os.environ.get('STATE', str(STATE_DIR / 'blocked-task-notifier.first_seen.json')))
LEGACY_SEEN = Path(os.environ.get('LEGACY_STATE', str(STATE_DIR / 'blocked-task-notifier.seen')))
DELEGATED_AUTHORITY = Path(os.environ.get('DELEGATED_AUTHORITY_PATH', '/home/frank/uaa-rules/delegated-authority.md'))
APPROVALS_REGISTRY = Path(os.environ.get('APPROVALS_REGISTRY_PATH', '/home/frank/uaa-rules/approvals-registry.md'))
HERMES = os.environ.get('HERMES_BIN', '/home/frank/.local/bin/hermes')
ALERT_TARGET = os.environ.get('BLOCKED_TASK_ALERT_TARGET', 'discord:#critical-alerts')
ESCALATION_TIERS = ((0, 'NEW'), (24 * 3600, 'ESCALATION-24H'), (72 * 3600, 'ESCALATION-72H'))


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return ''

# Read governance files every run; this is also a deterministic existence guard.
_AUTH_TEXT = read_text(DELEGATED_AUTHORITY)
_APPROVALS_TEXT = read_text(APPROVALS_REGISTRY)

CRITICAL_RULES = [
    ('real-money/payment-flow', re.compile(r'\b(real[- ]money|live payment flow|actual charges?|charge customers?|stripe live|payment wiring)\b', re.I)),
    ('live-trading-action', re.compile(r'\b(live trading|live[-_ ]capped|live mode|trade_intents?|hyperliquid live|place live trades?|execute live trades?)\b', re.I)),
    ('credential-secret-mutation', re.compile(r'\b(create|creating|rotate|rotating|copy|copying|delete|deleting|move|moving|print|printing|exfiltrat\w*)\b.{0,80}\b(credentials?|secrets?|api[-_ ]?keys?|tokens?|auth[_-]?token|ssh keys?)\b|\b(credentials?|secrets?|api[-_ ]?keys?|tokens?|auth[_-]?token|ssh keys?)\b.{0,80}\b(create|creating|rotate|rotating|copy|copying|delete|deleting|move|moving|print|printing|exfiltrat\w*)\b', re.I | re.S)),
    ('production-deploy', re.compile(r'\b(production deploy|prod deploy|deploy to prod|user-facing going live|release to production)\b', re.I)),
    ('irreversible-data-operation', re.compile(r'\b(irreversible data|drop table|drop database|mass delete|schema[- ]destructive|destructive migration|truncate table)\b', re.I)),
    ('new-spending-commitment', re.compile(r'\b(new spend|new spending|spending commitment|paid tier|api tier|subscription|cost raise|increase concurrency|concurrency raise|buy credits?)\b', re.I)),
]
MATERIAL_RULES = [
    ('jarvis-gateway-cli-unavailable', re.compile(r'\b(jarvis gateway|hermes gateway|gateway|hermes cli|jarvis cli)\b.{0,80}\b(unavailable|down|cannot start|failed|crash|broken)\b', re.I | re.S)),
    ('repeated-frank-delivery-failure', re.compile(r'\b(repeated|persistent|consecutive)\b.{0,60}\b(delivery failure|delivery failed|telegram delivery|frank-facing channel)\b', re.I | re.S)),
    ('critical-work-crashstorm', re.compile(r'\b(crashstorm|crash loop|repeated crashes)\b.{0,100}\b(critical-list|frank gate|approval gate|critical blocker)\b', re.I | re.S)),
    ('critical-safety-guard-failure', re.compile(r'\b(safety guard|approval guard|critical-list guard)\b.{0,100}\b(fail|bypass|allow|without approval)\b', re.I | re.S)),
]
APPROVAL_COVERED = re.compile(r'\b(already approved|standing approval|approved by frank|granted by frank|approved scope|registry.*approved)\b', re.I | re.S)
APPROVAL_NOT_COVERED = re.compile(r'\b(not approved|awaiting frank approval|pending frank|requires frank approval|needs frank|must ask frank|approval remains awaiting)\b', re.I | re.S)
ROUTINE_DELEGATED = re.compile(r'\b(review-required|guardian review|delegated:|delegated blocker|routine|board hygiene|provider/model failure|pid .* not alive|worker startup crash|privileged-action-required)\b', re.I)
NEGATED_CRITICAL_SENTENCE = re.compile(r"\b(do not|don't|no|not)\b[^\n.;]{0,160}\b(credentials?|secrets?|prod deploy|production|live trading|new spend|spend|irreversible data|payments?|live payment|trading)\b|\b(credentials?|secrets?|prod deploy|production|live trading|new spend|spend|irreversible data|payments?|live payment|trading)\b[^\n.;]{0,160}\b(do not|don't|no|not)\b", re.I)
STOPWORDS = {
    'approval', 'approved', 'frank', 'granted', 'scope', 'usage', 'calls',
    'only', 'both', 'machines', 'number', 'traffic', 'with', 'from', 'this',
    'that', 'not', 'deleting', 'creating', 'rotating', 'copying', 'moving',
    'modifying', 'billing', 'plan', 'changes', 'campaigns', 'spend', 'sending',
}


def parse_approval_registry(text: str):
    entries = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) < 4 or cells[0] in {'#', '---'}:
            continue
        if not cells[0].isdigit():
            continue
        entries.append((cells[1], cells[2]))
    return entries

APPROVAL_REGISTRY_ENTRIES = parse_approval_registry(_APPROVALS_TEXT)


def meaningful_tokens(text: str):
    tokens = re.findall(r'[a-z0-9_@+-]{4,}', text.lower())
    return {token for token in tokens if token not in STOPWORDS}


def approval_registry_covers(context: str) -> bool:
    if not APPROVAL_COVERED.search(context) or APPROVAL_NOT_COVERED.search(context):
        return False
    context_tokens = meaningful_tokens(context)
    for approval, scope in APPROVAL_REGISTRY_ENTRIES:
        approval_hits = context_tokens & meaningful_tokens(approval)
        scope_hits = context_tokens & meaningful_tokens(scope)
        if len(approval_hits) >= 2 and scope_hits:
            return True
    return False


def strip_negated_critical_boilerplate(context: str) -> str:
    kept = []
    for part in re.split(r'([\n.;])', context):
        if part and NEGATED_CRITICAL_SENTENCE.search(part):
            continue
        kept.append(part)
    return ''.join(kept)


def table_exists(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def natively_notified_task_ids(con, task_ids: list[str]) -> set[str]:
    """Return blocked task ids whose native *notification* was delivered.

    The gateway watcher advances ``kanban_notify_subs.last_event_id`` only after
    adapter.send() succeeds. Requiring that cursor to cover the latest blocked
    event gives this polling safety net an independently checkable handoff
    boundary. Wake-only rows are excluded because they do not deliver a visible
    notification. Any missing table, column, event, or query failure fails open.
    """
    if not task_ids or not table_exists(con, 'kanban_notify_subs') or not table_exists(con, 'task_events'):
        return set()
    placeholders = ','.join('?' * len(task_ids))
    try:
        rows = con.execute(
            f'''SELECT s.task_id
                FROM kanban_notify_subs AS s
                JOIN (
                    SELECT task_id, MAX(id) AS event_id
                    FROM task_events
                    WHERE kind = 'blocked' AND task_id IN ({placeholders})
                    GROUP BY task_id
                ) AS e ON e.task_id = s.task_id
                WHERE s.task_id IN ({placeholders})
                  AND COALESCE(s.delivery_mode, 'notify') IN ('notify', 'notify+wake')
                  AND s.last_event_id >= e.event_id''',
            list(task_ids) + list(task_ids),
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def fetch_context(con, task_id: str, row) -> str:
    parts = [row['title'] or '', row['body'] or '', row['result'] or '', row['last_failure_error'] or '']
    if table_exists(con, 'task_comments'):
        parts += [r[0] or '' for r in con.execute('SELECT body FROM task_comments WHERE task_id=? ORDER BY created_at DESC LIMIT 8', (task_id,))]
    if table_exists(con, 'task_events'):
        for kind, payload in con.execute('SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY created_at DESC LIMIT 12', (task_id,)):
            if kind in {'blocked', 'commented', 'failed', 'spawn_failed'}:
                parts.append(payload or '')
    if table_exists(con, 'task_runs'):
        for summary, metadata, error, outcome in con.execute('SELECT summary, metadata, error, outcome FROM task_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 5', (task_id,)):
            parts += [summary or '', metadata or '', error or '', outcome or '']
    return '\n'.join(p for p in parts if p)


def classify(context: str):
    critical_context = strip_negated_critical_boilerplate(context)
    for label, pattern in CRITICAL_RULES:
        if pattern.search(critical_context):
            if approval_registry_covers(context):
                return None, 'approval-covered'
            return label, 'critical-list'
    for label, pattern in MATERIAL_RULES:
        if pattern.search(context):
            if approval_registry_covers(context):
                return None, 'approval-covered'
            return label, 'material-regression'
    if approval_registry_covers(context):
        return None, 'approval-covered'
    if ROUTINE_DELEGATED.search(context):
        return None, 'routine/delegated'
    return None, 'non-critical'


def load_state() -> dict:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        try:
            data = json.loads(STATE.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    legacy_keys = [line.strip() for line in read_text(LEGACY_SEEN).splitlines() if line.strip()]
    migrated = {key: {'first_seen_epoch': int(time.time()), 'delivered_tiers': ['NEW'], 'migrated_from_seen_set': True} for key in legacy_keys}
    return migrated


def write_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=STATE.name + '.', dir=str(STATE.parent))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, STATE)


def alert_tier(age_seconds: float, delivered_tiers: list[str]) -> str | None:
    due = None
    for threshold, name in ESCALATION_TIERS:
        if age_seconds >= threshold:
            due = name
    if due and due not in delivered_tiers:
        return due
    return None


WA_FALLBACK = os.environ.get('BLOCKED_TASK_WA_FALLBACK', 'whatsapp:Frank')
# Hard send timeout per attempt. The notifier runs every 15m; a single attempt must not
# block the whole run for longer than this, and must never raise into main().
SEND_TIMEOUT = int(os.environ.get('BLOCKED_TASK_SEND_TIMEOUT', '60'))
# Primary (discord) retry count before failing over to WhatsApp. Transient egress blips
# (e.g. DGX IPv6 frame-drop) are intermittent, so one quick retry maximizes discord delivery.
PRIMARY_RETRIES = int(os.environ.get('BLOCKED_TASK_PRIMARY_RETRIES', '2'))


def _run_hermes_send(target: str, subject: str, message: str, timeout: int) -> tuple[int | None, str]:
    """Invoke `hermes send` and return (returncode, detail).

    Never raises: a timeout or spawn error is reported as (None, detail) so the caller can
    fail over instead of crashing the notifier (root cause of t_36a16eb1 DEAD status).
    """
    env = os.environ.copy()
    env['HERMES_HOME'] = '/home/frank/.hermes'
    try:
        result = subprocess.run(
            [HERMES, 'send', '-q', '-t', target, '-s', subject, message],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        detail = (result.stdout + '\n' + result.stderr).strip().replace('\n', ' ')[:300]
        return result.returncode, detail
    except subprocess.TimeoutExpired:
        return None, f'timed out after {timeout}s'
    except Exception as exc:  # pexpect/EOFError/spawn failures must not kill the notifier
        return None, f'exception: {type(exc).__name__}: {str(exc)[:200]}'


def send_alert(message: str) -> tuple[bool, str]:
    # Primary channel: discord:#critical-alerts (named consumer — Frank critical response path).
    # Retry a transient hang/blip before failing over, so the primary channel is preferred.
    last_rc, last_detail = None, ''
    for attempt in range(1, PRIMARY_RETRIES + 1):
        rc, detail = _run_hermes_send(ALERT_TARGET, 'Frank-critical kanban blocker', message, timeout=SEND_TIMEOUT)
        last_rc, last_detail = rc, detail
        if rc == 0:
            return True, f'rc={rc} {detail}'.strip()
        last_detail += f' [attempt {attempt}/{PRIMARY_RETRIES}: rc={rc}]'
    # Primary failed (non-zero rc, timeout, or exception). Cross-channel failover to WhatsApp.
    wa_rc, wa_detail = _run_hermes_send(WA_FALLBACK, '🔁 FAILOVER: Frank-critical kanban blocker', message, timeout=SEND_TIMEOUT)
    wa_ok = wa_rc == 0
    detail = f'{last_detail} | wa-fallback={"ok" if wa_ok else "failed"} {wa_detail}'
    # The notifier's contract is "alert reaches Frank". A successful WhatsApp failover
    # satisfies that, so it counts as delivered (prevents 15m re-alert spam on discord blips).
    return wa_ok, f'rc={last_rc} {detail}'.strip()


def suppress_duplicate_tier(item: dict, tier: str | None) -> bool:
    """Suppress only a natively delivered task's duplicate NEW alert."""
    return tier == 'NEW' and bool(item.get('native_notified'))


def format_message(tier: str, pageable: list[dict], now_epoch: int) -> str:
    prefix = '🚨 Frank-critical newly blocked kanban task(s):' if tier == 'NEW' else f'🚨 {tier}: Frank-critical blocked kanban task(s) still unacked:'
    lines = [prefix]
    for item in pageable:
        age_h = (now_epoch - int(item.get('first_seen_epoch', now_epoch))) / 3600
        lines.append(f"  • [{item['board']}] {item['id']} — {item['title']}")
        lines.append(f"    classifier={item['category']}:{item['label']} first_seen_age={age_h:.1f}h")
    lines.append('Action: review the blocker; routine/delegated blockers are intentionally silent under quiet-mode policy.')
    lines.append(f'Governance: {DELEGATED_AUTHORITY} + {APPROVALS_REGISTRY}')
    return '\n'.join(lines)


def main():
    # --- Verdict-vocabulary detector (t_1d6ed4c0): read-only scan for
    #     out-of-contract REVIEW_VERDICT values that the verdict-router fails
    #     closed on (the "review black hole"). Reuses this notifier's existing
    #     discord #critical-alerts escalation path — NO new cron schedule.
    #     Dedup across runs via the detector's own state file so the same
    #     malformed card is not re-alerted every 15m. Read-only: never mutates.
    try:
        import verdict_vocabulary_detector as vvd  # type: ignore

        count, vvd_failures = vvd.run_with_alerting(
            send_alert,
            state_path=os.environ.get('VERDICT_DETECTOR_STATE'),
        )
        if vvd_failures:
            print('VERDICT_VOCAB_DETECTOR_DELIVERY_FAILED ' + ' | '.join(vvd_failures))
    except Exception as exc:  # detector must never crash the blocked-task notifier
        print(f'VERDICT_VOCAB_DETECTOR_ERROR {type(exc).__name__}: {str(exc)[:300]}')

    now = {}
    for db in sorted(BOARDS_DIR.glob('*/kanban.db')):
        board = db.parent.name
        con = None
        try:
            con = kb.connect(db_path=db)
            rows = con.execute("SELECT id, title, body, result, last_failure_error FROM tasks WHERE status='blocked'").fetchall()
            # ADOPT-7 (t_8d2b855a): classify every blocked task. The native
            # suppression below is limited to the duplicate NEW event leg, and
            # only after the gateway cursor proves adapter.send() succeeded.
            natively_notified = natively_notified_task_ids(con, [row['id'] for row in rows])
            for row in rows:
                key = f"{board}:{row['id']}"
                label, cat = classify(fetch_context(con, row['id'], row))
                now[key] = {
                    'board': board, 'id': row['id'],
                    'title': (row['title'] or '').replace('\n', ' ')[:100],
                    'label': label, 'category': cat,
                    'native_notified': row['id'] in natively_notified,
                }
        except Exception as exc:
            key = f'{board}:__notifier_db_error__'
            now[key] = {'board': board, 'id': '__notifier_db_error__', 'title': f'blocked-task-notifier could not read board DB: {exc}', 'label': 'notifier-board-db-read-failure', 'category': 'material-regression', 'native_notified': False}
        finally:
            if con is not None:
                con.close()

    now_epoch = int(time.time())
    state = load_state()
    active_state = {key: state.get(key, {}) for key in now}
    for key, entry in active_state.items():
        entry.setdefault('first_seen_epoch', now_epoch)
        entry.setdefault('delivered_tiers', [])
        entry.update({k: now[key][k] for k in ('board', 'id', 'title', 'label', 'category', 'native_notified')})

    due_by_tier: dict[str, list[dict]] = {}
    for key, item in now.items():
        if not item.get('label'):
            continue
        entry = active_state[key]
        tier = alert_tier(now_epoch - int(entry.get('first_seen_epoch', now_epoch)), list(entry.get('delivered_tiers', [])))
        if suppress_duplicate_tier(item, tier):
            # Native notify-subscribe already delivered this event. Keep the
            # classified item eligible for 24H/72H governance escalation.
            continue
        if tier:
            payload = dict(item)
            payload['key'] = key
            payload['first_seen_epoch'] = int(entry.get('first_seen_epoch', now_epoch))
            due_by_tier.setdefault(tier, []).append(payload)

    failures = []
    for tier in [name for _, name in ESCALATION_TIERS]:
        pageable = due_by_tier.get(tier, [])
        if not pageable:
            continue
        ok, detail = send_alert(format_message(tier, pageable, now_epoch))
        if ok:
            for item in pageable:
                delivered = active_state[item['key']].setdefault('delivered_tiers', [])
                if tier not in delivered:
                    delivered.append(tier)
                active_state[item['key']]['last_delivery_success_epoch'] = now_epoch
        else:
            failures.append(f'{tier}: {detail}')

    write_state(active_state)
    if failures:
        print('BLOCKED_TASK_NOTIFIER_DELIVERY_FAILED ' + ' | '.join(failures))


if __name__ == '__main__':
    main()
