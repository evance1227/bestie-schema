create table if not exists users (
  id bigserial primary key,
  phone varchar(32) unique,
  created_at timestamptz default now()
);

create table if not exists conversations (
  id bigserial primary key,
  user_id bigint references users(id) on delete cascade,
  started_at timestamptz default now()
);

create table if not exists messages (
  id bigserial primary key,
  conversation_id bigint references conversations(id) on delete cascade,
  direction varchar(10) check (direction in ('in','out')) not null,
  message_id varchar(64),
  text text,
  created_at timestamptz default now(),
  unique (message_id)
);

create table if not exists links (
  id bigserial primary key,
  conversation_id bigint references conversations(id) on delete cascade,
  raw_url text not null,
  affiliate_url text,
  campaign varchar(64),
  commission_pct numeric(5,2) default 0,
  sponsor_bid_cents integer default 0,
  last_ctr numeric(5,2) default 0,
  last_conv_rate numeric(5,2) default 0,
  created_at timestamptz default now()
);

create table if not exists clicks (
  id bigserial primary key,
  link_id bigint references links(id) on delete cascade,
  user_id bigint references users(id) on delete cascade,
  clicked_at timestamptz default now()
);

create table if not exists purchases (
  id bigserial primary key,
  link_id bigint references links(id) on delete cascade,
  user_id bigint references users(id) on delete cascade,
  amount_cents integer,
  purchased_at timestamptz default now()
);

create table if not exists error_log (
  id bigserial primary key,
  source varchar(64),
  detail text,
  created_at timestamptz default now()
);

create index if not exists idx_messages_convo on messages(conversation_id, created_at);
create index if not exists idx_links_convo on links(conversation_id, created_at);
