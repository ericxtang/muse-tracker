-- Sample muse.db schema (PostgreSQL) -- the exact column definitions for the
-- tables the muse-braintrust bridge reads, taken from the muse.db catalog.
--
-- muse.db has ~200 tables across many schemas (chat, goals, memory, runtime,
-- agent, ...). This file covers the bridge subgraph: the two tables the
-- bridge reads directly, plus the tables their foreign keys point at, so the
-- sample is self-contained and joinable.
--
-- Foreign keys whose target table is outside this subgraph
-- (runtime.events, runtime.requests, agent.sessions) are documented in
-- comments instead of being declared, so this file runs standalone.
-- The SQLite twin of this schema lives in make_sample.py; column names,
-- order, and nullability are identical, with SQLite type affinities
-- (bigint -> INTEGER, boolean -> INTEGER, timestamptz -> TEXT ISO-8601,
-- arrays -> TEXT JSON).

CREATE SCHEMA IF NOT EXISTS agent;
CREATE SCHEMA IF NOT EXISTS runtime;

-- Values observed in queryable data. Reasoning kinds (thinking,
-- redacted-thinking) exist in the real enum but are withheld from the
-- inspection view, so they cannot be enumerated from outside.
CREATE TYPE agent.context_item_kind AS ENUM (
    'message_text',
    'text_segments',
    'tool_call',
    'tool_output'
);

CREATE TYPE runtime.message_role AS ENUM (
    'user',
    'assistant',
    'system'
);

-- ---------------------------------------------------------------------------
-- agent.agents -- Agent identity, kind, session and parent relationships.
-- ---------------------------------------------------------------------------
CREATE TABLE agent.agents (
    agent_id                        text PRIMARY KEY,
    id                              text UNIQUE,
    session_id                      text,  -- soft ref -> agent.sessions.session_id
    kind                            text NOT NULL DEFAULT 'root',
    parent_id                       text,  -- soft ref -> agent.agents.id
    parent_agent_id                 text,  -- soft ref -> agent.agents.agent_id
    root_session_id                 text,  -- soft ref -> agent.sessions.session_id
    request_id                      text,  -- FK -> runtime.requests.request_id (external)
    model                           text NOT NULL DEFAULT '',
    status                          text NOT NULL,
    depth                           integer NOT NULL DEFAULT 0,
    created_at                      bigint NOT NULL DEFAULT (EXTRACT(epoch FROM now()))::bigint,
    updated_at                      bigint NOT NULL DEFAULT (EXTRACT(epoch FROM now()))::bigint,
    last_assistant_message          text,
    seen_at_ms                      bigint,
    prompt_floor_seq                bigint,
    ephemeral                       boolean NOT NULL DEFAULT false,
    last_assistant_resources        text,
    agent_type                      text,
    presentation_root_session_id    text,  -- soft ref -> agent.sessions.session_id
    spawn_metadata_json             text,
    originating_location_context_json text,
    handoff_context_high_water_seq  bigint
);

-- ---------------------------------------------------------------------------
-- runtime.messages -- Stored message roles, text and event references.
-- ---------------------------------------------------------------------------
CREATE TABLE runtime.messages (
    message_id          text PRIMARY KEY,
    event_seq           bigint NOT NULL UNIQUE,  -- FK -> runtime.events.event_seq (external)
    role                runtime.message_role NOT NULL,
    prompt_rendering_id bigint,
    author_label        text,
    provider_message_id text,
    created_at          timestamp with time zone NOT NULL DEFAULT now(),
    body                text
);

-- ---------------------------------------------------------------------------
-- runtime.tool_calls -- Recorded tool calls, inputs, names, execution state.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS runtime.tool_calls_tool_call_id_seq;
CREATE TABLE runtime.tool_calls (
    tool_call_id   bigint PRIMARY KEY DEFAULT nextval('runtime.tool_calls_tool_call_id_seq'),
    event_seq      bigint NOT NULL UNIQUE,  -- FK -> runtime.events.event_seq (external)
    call_id        text NOT NULL UNIQUE,
    tool_name      text NOT NULL,
    server_name    text,
    status         text NOT NULL,
    created_at     timestamp with time zone NOT NULL DEFAULT now(),
    arguments_json text
);

-- ---------------------------------------------------------------------------
-- runtime.tool_outputs -- Recorded tool results and their call references.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS runtime.tool_outputs_tool_output_id_seq;
CREATE TABLE runtime.tool_outputs (
    tool_output_id bigint PRIMARY KEY DEFAULT nextval('runtime.tool_outputs_tool_output_id_seq'),
    event_seq      bigint NOT NULL UNIQUE,  -- FK -> runtime.events.event_seq (external)
    call_id        text NOT NULL,
    status         text NOT NULL,
    created_at     timestamp with time zone NOT NULL DEFAULT now(),
    output_text    text,
    error_text     text
);

-- ---------------------------------------------------------------------------
-- agent.context_items -- Ordered readable conversation items, including
-- message and tool references. The bridge's primary source.
-- (Queries run against the security-barrier view inspection.context_items,
-- which withholds rows holding model reasoning.)
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS agent.context_items_context_item_id_seq;
CREATE TABLE agent.context_items (
    context_item_id       bigint PRIMARY KEY DEFAULT nextval('agent.context_items_context_item_id_seq'),
    agent_id              text NOT NULL,
    seq                   bigint NOT NULL,
    item_kind             agent.context_item_kind NOT NULL,
    message_id            text REFERENCES runtime.messages (message_id),
    tool_call_id          bigint REFERENCES runtime.tool_calls (tool_call_id),
    tool_output_id        bigint REFERENCES runtime.tool_outputs (tool_output_id),
    role                  runtime.message_role,
    call_id               text,  -- soft ref -> runtime.workflow_agent_calls.call_id
    tool_name             text,
    success               boolean,
    message_source        text,
    created_at            timestamp with time zone NOT NULL DEFAULT now(),
    data_fbids            bigint[],
    data_message_ids      text[],
    data_cleanup_checked_at timestamp with time zone,
    data_summarized_at    timestamp with time zone,
    data_thread_ids       text[],
    data_expires_at       timestamp with time zone,
    text_content          text,
    item_json             text,
    UNIQUE (agent_id, seq)
);

-- ---------------------------------------------------------------------------
-- agent.agent_message_token_usage -- Token usage attributed to agent+message.
-- Note: created_at here is a bigint epoch, unlike the timestamptz columns
-- elsewhere. Token rows exist only for user-visible assistant messages.
-- ---------------------------------------------------------------------------
CREATE TABLE agent.agent_message_token_usage (
    agent_id      text NOT NULL,
    message_id    text NOT NULL,
    input_tokens  bigint NOT NULL,
    output_tokens bigint NOT NULL,
    created_at    bigint NOT NULL,
    PRIMARY KEY (agent_id, message_id)
);
