-- รันใน Supabase → SQL Editor → New query → วางทั้งหมด → Run
-- ตารางเก็บข้อมูล Lazada สำหรับ dashboard (sync เฉพาะข้อมูลใหม่)

create table if not exists lz_orders (
  order_id      text primary key,
  date          date,
  hour          int,
  platform      text default 'lazada',
  status        text,
  region        text,
  customer      text default 'new',
  shipping_fee  numeric default 0,
  platform_fee  numeric default 0,
  created_at_lz text
);

create table if not exists lz_order_items (
  order_id  text,
  line      int,
  sku       text,
  name      text,
  category  text default '',
  qty       int default 1,
  price     numeric default 0,
  cost      numeric default 0,
  primary key (order_id, line)
);

create table if not exists lz_products (
  sku           text primary key,
  name          text,
  category      text default '',
  price         numeric default 0,
  cost          numeric default 0,
  stock_lazada  int default 0
);

-- จำสถานะการ sync (เวลาที่ sync ล่าสุด)
create table if not exists lz_sync (
  id              int primary key default 1,
  last_sync_time  timestamptz,
  updated_at      timestamptz default now()
);
insert into lz_sync (id, last_sync_time) values (1, null)
  on conflict (id) do nothing;

create index if not exists idx_items_order on lz_order_items(order_id);
create index if not exists idx_orders_date on lz_orders(date);
