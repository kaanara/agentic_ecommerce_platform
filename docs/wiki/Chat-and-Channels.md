# Chat and Channels

The owner talks to agents through:
- **Dashboard**: the Chat page (requires login).
- **Telegram Bot**: paired phone chat (one-time pairing code).
- **WhatsApp** (optional, off by default): needs Meta business account and approved message templates.
- **API**: `/conversations` endpoints for external integrations.

All chats are stored in `ops_conversations` and `ops_messages` tables, masked on write, and subject to retention policy.

## Chat in the Dashboard

**Page**: Chat (requires dashboard login).

1. **Start a conversation**: select an agent (ceo, finance, researcher, sentinel, foreman, or custom).
2. **Send messages**: type text or upload attachments (images, PDFs, CSVs up to the configured size limit).
3. **View history**: last N turns kept in context; older turns folded into a summary.
4. **Stream replies**: responses appear token-by-token as `text_delta` events; tool calls render as nested steps.

A new conversation auto-titles from the first user message (trimmed, masked, max 60 chars). Conversations are archived manually or via retention policy.

### Attachments

Attachments are:
- **Stored** under `data/chat_files/` (size-capped per `chat_max_attachment_mb`).
- **Fenced** into the task text with names and metadata so agents cannot confuse them with ordinary text.
- **Images and PDFs**: base64-decoded, stored as files, then referenced with "not shown as text" notes.
- **Text and CSVs**: stored as text, fenced with content inline.
- **Always untrusted**: agents cannot read attachment paths or break out of the fence.

Max size per attachment is configured in Settings > Chat; oversized attachments are skipped and logged.

## Streaming

Streaming replies bridge the async agent core to synchronous callers (dashboard, API, messaging channels) via:

1. **Dashboard page** (Streamlit): `_stream_turn()` runs `chat.converse()` in a background thread, reads `on_event` callbacks through a queue, renders `text_delta` as streaming text and tool/escalation events as side-effect steps.
2. **API** (`/conversations/{id}/stream`): same pattern, returns `text/event-stream` with SSE-formatted `text_delta`, `tool_start`, `tool_end`, `escalation`, `done` events.
3. **Messaging channels**: no streaming visible to the user; messages are sent whole when the turn completes.

Event types:
- `text_delta`: chunk of the reply (many per turn).
- `tool_start`: tool called (name, id).
- `tool_end`: tool finished (name, id, result).
- `escalation`: approval created (tool, tier).
- `done`: turn complete (message_id, status, cost, escalations).
- `error`: turn failed.

All chat runs use `chat=True` in `_run_core()`, which:
- Drops terminal tools (`submit_plan`, `submit_verdict`, `submit_brief`) so replies are free text.
- Offers `consult_agent` for depth-0 chats (allows one agent to ask another a question).
- Streams token-by-token if `on_event` is provided.

## History and Summaries

**Context window**: the last `chat_history_turns * 2` messages are kept in context (configurable, default: 5 turns = 10 messages).

**Auto-summary**: when the history exceeds the window, old turns are folded:
1. The oldest `overflow` messages are extracted.
2. A summary is generated using Haiku (cheap, deterministic).
3. The summary replaces the overflow messages.
4. The cost is booked to the agent's daily chat spend.

**Offline mode** (`llm_mode=scripted`): the scripted model has no understanding, so the summary falls back to a raw excerpt (no loss of context).

History and summary live in `ops_conversations`:
- `summary` (TEXT): older turns folded.
- `messages` (JSON): array of `{id, role, content, agent_id, tool_steps, status, cost_eur, agent_run_id, escalations, attachments, created_at}`.

## Tools Through the Policy Gate

All tools are gated: agents must pass the policy check before calling them.

### Tool tiers (Tier Order)
- **T0**: always allowed (e.g. think).
- **T1**: routine, low risk (e.g. web search, summary).
- **T2**: bounded risk (e.g. email, update settings) — needs context checks or approval if risky args.
- **T3**: escalation (high stakes, e.g. refund, marketing spend) — always waits for human approval.
- **T4**: human-only (e.g. `delete_customer_data`, form submission) — never callable by agents.

**Policy file** (`src/aecom/policy/policy.yaml`): defines rules per tool. Rules include:
- `tier`: the default tier.
- `agents`: if set, only these agents may use it.
- `recipient_must_be_counterparty`: for email/messages, the `to` field must match the current email sender (prevents agents from emailing themselves or strangers).

**Evaluation** (`aecom.policy.gate.evaluate`):
1. Check tier, agent allowlist, recipient check, kill switch, safe mode, agents_paused.
2. If T0–T1: allow.
3. If T2: check spend, context, decide allow or escalate.
4. If T3: escalate.
5. If T4: deny.

**Side effects**: T0 read-only tools have `side_effect=False` and skip most checks. T1–T4 have `side_effect=True` and are blocked when the kill switch is on.

## consult_agent

`consult_agent(agent: str, question: str) -> str` (chat-mode only, depth 0).

Allows one agent to ask another a question and get the answer. It is a chat-only tool: it is built inside the chat run and never appears in automated runs, `TOOLS.md` or the tool registry. It goes through the policy gate like any other side-effecting tool (rule `consult_agent`, tier T1, for the chat agents), so the kill switch, safe mode, "agents paused", audit and the tool-call log all apply. The reply is a JSON object with:
- `output`: the agent's free-text reply.
- `status`: ok | error | max_steps | etc.
- `messages`: array of tool steps (if any).

**Limits**:
- At most 2 calls per turn.
- Both must be depth 1 (no three-level nesting).
- The target agent must be in `chat_agents` settings.
- The consulted agent's own tools still go through the gate.

Example: the CEO asks the Researcher to investigate a topic, or the Finance agent asks the Foreman to check production status.

## message_owner

`message_owner(text: str, urgency: str = "normal", question: bool = False) -> dict`.

Send the owner a message outside the current chat turn (e.g. an alert or a question). Returns `{sent: bool, conversation_id: str, delivered: {dashboard, telegram, ntfy}}`.

**Urgency**:
- `info`: routine, respects quiet hours.
- `normal`: important, respects quiet hours.
- `high`: urgent, ignores quiet hours.

**Delivery**:
1. Paired Telegram chat first (if active).
2. WhatsApp second (paired and enabled; free-form messages only, so outside Meta's 24-hour window it can fail and falls through).
3. ntfy third (if configured; only tried when neither Telegram nor WhatsApp delivered).
4. Dashboard inbox always.

Starts a new conversation with `started_by="agent"`. Capped by `owner_message_daily_limit` per agent (default: 5 per day).

The `question=True` flag indicates the owner's reply should continue this conversation (stored in `ops_conversations` under the agent's id).

## Budgets

Chat runs share the agent's daily LLM budget (`daily_budget(agent_id)`).

**Chat cap share** (`chat_daily_cap_share`, default 50%): chat may use at most this fraction of the budget; the rest is reserved for automated runs. A turn is rejected before the LLM call if the chat cap is exhausted.

**Summary cost**: the Haiku model's cost is booked to the agent's daily chat spend (`run_id=f"chat-summary-..."`).

Cost is precise: every message stores `cost_eur` (not estimated) and the conversation tracks `total_cost_eur` (sum of all messages).

## Telegram Pairing and Commands

**Config**: `TELEGRAM_ENABLED` (Settings > Channels > Telegram enabled), `TELEGRAM_BOT_TOKEN` (secret).

**Pairing**:
1. Dashboard > Chat > "Pair Telegram" generates a 6-digit code (valid for 10 minutes).
2. Owner sends `/pair CODE` to the bot.
3. If valid, the chat id is stored in `ops_channel_links` (active=True).
4. Pairing requires dashboard login to be on (prevents unauthenticated access).

**Commands** (paired chats only):
- `/pair CODE`: pair the chat.
- `/agent ID`: switch to a different agent (ceo, finance, etc.).
- `/new`: start a new conversation with the current agent.
- `/status`: show agent run summary and open incidents.
- `/approvals`: show pending approvals as buttons.
- `/stop`: pause all agents (requires confirmation).
- Anything else: sent to the chat's current agent as a normal message.

**Approval tier cap** (`max_approve_tier`, default T1):
- Telegram approval buttons are limited to this tier.
- T4 is never approvable (human-only tools).
- Raise the cap in Settings > Channels if needed.

**Transport**: Telegram Bot API via `net_guard.pinned_get` (HTTP GET only, SSRF-protected). Updates are polled once per scheduler tick (not a blocking long-poll). Failures are logged and recorded via observability, never raised.

## WhatsApp Status

**Config**: `WHATSAPP_ENABLED` (Settings > Channels > WhatsApp enabled), four secrets: `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`.

**Status**: built in W4 but off by default.

**Requirements**:
- Meta business account and verified business number.
- Public webhook URL (typically through Cloudflare Tunnel).
- Approved message templates for business-initiated messages.

**Transport**: Meta Cloud API via `net_guard.pinned_post` (SSRF-protected). Inbound events arrive at `POST /webhooks/whatsapp` (signature verified). Failures are logged and recorded, never raised.

**Pairing**: same `/pair CODE` flow as Telegram.

## API Routes

All routes require `Authorization: Bearer <AECOM_API_TOKEN>` (from `secrets.get("AECOM_API_TOKEN")`). Return 503 if the token is not configured (fails closed).

### Conversations

**GET /conversations**
- List all conversations (default: non-archived, newest first).
- Query params: `include_archived=bool`, `only_unread=bool`, `search=str`, `limit=50`.
- Returns: `[{id, title, agent_id, channel, started_by, owner, summary, archived, unread, total_cost_eur, created_at, updated_at, messages: [...]}]`.

**POST /conversations**
- Create a new conversation.
- Body: `{agent_id, title?, channel?, started_by?, owner?}`.
- Returns: the conversation with empty messages.

**GET /conversations/{conversation_id}**
- Get one conversation with its full message history.
- Returns: conversation dict (see GET /conversations).

**DELETE /conversations/{conversation_id}**
- Hard delete (messages cascade). Attachment files removed by the chat layer.
- Query param: `by?` (audited).
- Returns: `{deleted: true}`.

### Messages

**POST /conversations/{conversation_id}/messages**
- Run one chat turn (no streaming, full reply at once).
- Fails 403 if chat is blocked (dashboard login off or unconfigured).
- Body: `{text, attachments: [{type, name, content|data}, ...]?}`.
- Returns: `ChatTurn` dict: `{conversation_id, message_id, agent_id, text, status, cost_eur, tool_steps, escalations, agent_run_id}`.

**GET /conversations/{conversation_id}/stream**
- Stream one chat turn as SSE events.
- Query params: `message_id` (the user message's id), `user_attachment?` (single attachment URL, e.g. data: URI).
- Returns: `text/event-stream`.
  - Events: `{type: "text_delta", text: "..."}`, `{type: "tool_start", name, id}`, `{type: "tool_end", name, result}`, `{type: "escalation", ...}`, `{type: "done", message_id, status, cost_eur, escalations}`, `{type: "error", detail}`.

## GDPR Retention for Conversations

**Scope**: chats are user data (conversations, messages, attachments) and are subject to GDPR erasure and retention.

**Retention period** (`chat_retention_days`, Settings > Chat > Delete conversations idle for, default: 90 days):
- Conversations not updated for N days are hard-deleted (messages cascade).
- Job: the scheduler's `chat_retention` job runs every night at 03:30 (`cron:30 3 * * *`) and calls `conversations.purge_older_than(chat_retention_days)`. It reschedules itself when settings change.
- Set to 0 to keep conversations forever (the job then does nothing).

**Hard delete** (manual delete in the dashboard or API, and the retention job):
- Deletes all messages in `ops_messages` (masking was applied on write, so messages are already safe).
- Deletes the conversation in `ops_conversations`.
- Deletes the conversation's attachment files under `data/chat_files/`; the delete is limited to that folder and never raises.
- No archive or soft-delete: retention is final.

**Audit trail**:
- The retention job is audited: `audit("system", "conversation_purge", "ok", {"count": N, "older_than_days": D})`.
- Manual deletion (API or dashboard) is audited: `audit("human:<user>", "conversation_delete", "ok", {"conversation_id": "..."})`.

**Export / erasure**:
- A data-subject erasure request includes all messages: see [GDPR and Retention](GDPR-and-Retention.md) section H5 for the procedure.
- The conversation table references no personal data other than `owner` (usually empty for dashboard chats) and `summary` (masked).
- Attachments are stored as files under `data/chat_files/` and are deleted together with their conversation.

## Blocked Reason

Chat is blocked (returns 403 / blocked status) when:
- Dashboard login is **disabled** (chat needs it for access control).
- Dashboard login is not **configured** (no password set).
- A **paired channel** (Telegram/WhatsApp) bypasses the login check.

The `blocked_reason()` function returns the reason or None. The dashboard shows a clear message; the API returns 403 Forbidden.

## Masked Content

All content (messages, summaries, tool results) is masked on write via `aecom.observability.mask()`. Masking:
- Redacts email addresses and phone numbers.
- Redacts API keys and tokens (detected by common patterns).
- Leaves ordinary text intact.

Display and export apply masking again (layered), so sensitive data is doubly protected.

## Integration Points

- **Handlers** (`aecom.handlers`): agents are run via `run_agent()`, which calls chat's entry point `converse()` via `run_sync()`.
- **CLI** (`aecom.cli`): `uv run aecom chat <agent_id>` for interactive chat.
- **Scheduler**: `poll_telegram` fetches waiting Telegram updates (only when `TELEGRAM_BOT_TOKEN` is set and Telegram is enabled); `chat_retention` purges old conversations nightly. WhatsApp has no poller: Meta calls `POST /webhooks/whatsapp`.
- **Event bus** (`aecom.bus.events`, `aecom.handlers`): chats can trigger events (e.g. a message_owner call publishes an event that handlers pick up).
- **Notifications** (`aecom.notify`): message_owner delivery uses the notify router.
