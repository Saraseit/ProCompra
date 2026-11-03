-- Fase 7: el plazo de crédito es de cada compra, no del proveedor.
-- Idempotente.
--
-- proveedores.da_credito solo dice si el proveedor nos da crédito: sus
-- órdenes nacen a crédito. Cuándo se paga cada compra va en su calendario
-- (pagos_programados): una fecha, o varias si se divide en 2 o 3 pagos. La
-- suma puede ser mayor que la orden cuando diferir trae recargo.
-- proveedores.dias_credito (fase 3) se conserva pero ya no calcula nada.

alter table public.proveedores
  add column if not exists da_credito boolean not null default false;

-- Quien ya tenía días de crédito capturados se marca como proveedor con crédito.
update public.proveedores set da_credito = true
 where coalesce(dias_credito, 0) > 0 and not da_credito;

comment on column public.proveedores.da_credito is
  'Nos da crédito: sus órdenes nacen a crédito. El plazo va en el calendario de cada orden.';
comment on column public.proveedores.dias_credito is
  'Obsoleto desde la fase 7: el plazo es de cada compra (pagos_programados).';

create table if not exists public.pagos_programados (
  id         uuid primary key default gen_random_uuid(),
  orden_id   uuid not null references public.ordenes_compra(id) on delete cascade,
  fecha      date not null,
  monto      numeric(14,2) not null check (monto > 0),
  notas      text,
  creado_por uuid references public.usuarios(id) on delete set null,
  created_at timestamptz not null default now()
);

create index if not exists pagos_programados_orden_idx on public.pagos_programados (orden_id);
create index if not exists pagos_programados_fecha_idx on public.pagos_programados (fecha);

-- El backend usa la service_role key; RLS cerrado para cualquier otro acceso.
alter table public.pagos_programados enable row level security;

comment on table public.pagos_programados is
  'Calendario de pagos de una compra a crédito: cuándo y cuánto se paga. Lo pagado se aplica a las fechas en orden.';
