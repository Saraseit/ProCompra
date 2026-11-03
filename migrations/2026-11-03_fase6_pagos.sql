-- Fase 6: módulo de pagos. Un pago puede cubrir varias órdenes.
-- Idempotente.
--
-- movimientos_pago es el pago tal como sale del banco o de caja (fecha,
-- forma, referencia). Cada orden que cubre conserva su renglón en pagos con
-- el monto que le toca, ligado al movimiento por pagos.movimiento_id: el
-- saldo de cada orden y el estado de cuenta de crédito no cambian.
-- El total del movimiento es la suma de sus renglones (no se guarda aparte,
-- así nunca se desajusta si una orden se borra).

create table if not exists public.movimientos_pago (
  id             uuid primary key default gen_random_uuid(),
  fecha_pago     date not null,
  metodo         metodo_pago not null,
  referencia     text,
  entregado_a    text,
  notas          text,
  registrado_por uuid references public.usuarios(id) on delete set null,
  created_at     timestamptz not null default now()
);

create index if not exists movimientos_pago_fecha_idx
  on public.movimientos_pago (fecha_pago desc);

-- El backend usa la service_role key; RLS cerrado para cualquier otro acceso.
alter table public.movimientos_pago enable row level security;

alter table public.pagos
  add column if not exists movimiento_id uuid
  references public.movimientos_pago(id) on delete cascade;

create index if not exists pagos_movimiento_idx on public.pagos (movimiento_id);

comment on table public.movimientos_pago is
  'Un pago real (transferencia, efectivo, tarjeta) que puede cubrir varias órdenes; cada orden tiene su renglón en pagos.';
comment on column public.pagos.movimiento_id is
  'Movimiento al que pertenece este pago. NULL = pago registrado desde la orden, antes del módulo de pagos.';
