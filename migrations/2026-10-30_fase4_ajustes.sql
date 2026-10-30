-- Fase 4: surtido parcial, órdenes complementarias y avisos dentro de la app.
-- Idempotente: se puede correr más de una vez.

-- Orden complementaria: compra el faltante que el proveedor original no surtió.
alter table public.ordenes_compra
  add column if not exists orden_origen_id uuid references public.ordenes_compra(id) on delete set null;
create index if not exists ordenes_compra_orden_origen_idx on public.ordenes_compra(orden_origen_id);

-- Partida complementaria -> partida original, y cuánto se pidió antes del ajuste.
alter table public.partidas
  add column if not exists partida_origen_id uuid references public.partidas(id) on delete set null;
alter table public.partidas
  add column if not exists cantidad_original numeric;
create index if not exists partidas_partida_origen_idx on public.partidas(partida_origen_id);

comment on column public.ordenes_compra.orden_origen_id is
  'Orden cuyo faltante compra esta (surtido parcial).';
comment on column public.partidas.partida_origen_id is
  'Partida original cuyo faltante cubre esta partida.';
comment on column public.partidas.cantidad_original is
  'Cantidad pedida antes de registrar un surtido parcial.';

-- Avisos de la campana: uno por destinatario.
create table if not exists public.notificaciones (
  id          uuid primary key default gen_random_uuid(),
  usuario_id  uuid not null references public.usuarios(id) on delete cascade,
  orden_id    uuid references public.ordenes_compra(id) on delete cascade,
  tipo        text not null default 'orden',
  titulo      text not null,
  detalle     text,
  creado_por  uuid references public.usuarios(id) on delete set null,
  leida_at    timestamptz,
  created_at  timestamptz not null default now()
);
create index if not exists notificaciones_usuario_idx
  on public.notificaciones(usuario_id, created_at desc);

-- El backend usa la service_role key; sin políticas, nadie más puede leerla.
alter table public.notificaciones enable row level security;

-- PostgREST debe ver las columnas y la tabla nuevas.
notify pgrst, 'reload schema';
