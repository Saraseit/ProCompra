-- ProCompra · Fase 1 y 2: crédito, comprobación, adjuntos y facturas CFDI.
--
-- Cómo aplicarla: Supabase → proyecto ProCompra → SQL Editor → pegar todo
-- este archivo → Run. Es idempotente: si se corre dos veces no duplica nada.
-- Todo es aditivo: no se borra ni se renombra ninguna columna existente.
--
-- IMPORTANTE: correr ANTES de desplegar el backend de la rama feat/fase-1-2.

-- 1. Crédito con proveedor como forma de pago y estado "por comprobar".
alter type public.metodo_pago add value if not exists 'credito';
alter type public.estado_oc add value if not exists 'comprobacion' after 'recoleccion';

-- 2. Devolución para corrección y comprobación del gasto real.
alter table public.ordenes_compra
  add column if not exists motivo_devolucion  text,
  add column if not exists monto_comprobado   numeric(14,2) check (monto_comprobado >= 0),
  add column if not exists cambio_devuelto    numeric(14,2) check (cambio_devuelto >= 0),
  add column if not exists notas_comprobacion text,
  add column if not exists comprobada_por     uuid references public.usuarios(id),
  add column if not exists comprobada_at      timestamptz;

-- 3. Entrega de efectivo o tarjeta: a quién se le dio. La referencia deja de
--    ser obligatoria en la base porque el efectivo no tiene; el backend la
--    sigue exigiendo para transferencias.
alter table public.pagos add column if not exists entregado_a text;
alter table public.pagos alter column referencia drop not null;

-- 4. Documentos adjuntos a una orden (cotización, comprobante, ticket, factura).
do $$ begin
  create type public.tipo_adjunto as enum ('cotizacion', 'comprobante_pago', 'ticket', 'factura', 'otro');
exception when duplicate_object then null; end $$;

create table if not exists public.adjuntos (
  id             uuid primary key default gen_random_uuid(),
  orden_id       uuid not null references public.ordenes_compra(id),
  tipo           public.tipo_adjunto not null,
  nombre_archivo text not null,
  ruta           text not null unique,
  mime           text,
  tamano         bigint,
  subido_por     uuid references public.usuarios(id),
  created_at     timestamptz not null default now()
);
create index if not exists adjuntos_orden_idx on public.adjuntos (orden_id);
comment on table public.adjuntos is 'Archivos de cada orden; el binario vive en el bucket privado "adjuntos".';
-- Sin políticas: solo el backend (service_role) lee y escribe.
alter table public.adjuntos enable row level security;

-- 5. Facturas CFDI. Se cargan por XML (uno o muchos) y se vinculan a la orden
--    automáticamente por RFC del emisor + total, o a mano si hay ambigüedad.
create table if not exists public.facturas (
  id               uuid primary key default gen_random_uuid(),
  uuid_fiscal      text unique,
  rfc_emisor       text not null,
  nombre_emisor    text,
  rfc_receptor     text,
  serie            text,
  folio            text,
  fecha_emision    timestamptz,
  subtotal         numeric(14,2),
  iva              numeric(14,2),
  total            numeric(14,2) not null,
  moneda           text,
  metodo_pago      text,
  forma_pago       text,
  tipo_comprobante text,
  orden_id         uuid references public.ordenes_compra(id),
  vinculacion      text not null default 'pendiente'
                   check (vinculacion in ('pendiente', 'automatica', 'manual')),
  xml_ruta         text,
  subido_por       uuid references public.usuarios(id),
  created_at       timestamptz not null default now()
);
create index if not exists facturas_orden_idx on public.facturas (orden_id);
create index if not exists facturas_rfc_idx on public.facturas (rfc_emisor);
comment on table public.facturas is 'CFDI de proveedores. orden_id nulo = pendiente de vincular.';
alter table public.facturas enable row level security;

-- 6. Bucket privado para los archivos (máx. 10 MB, solo PDF, imágenes y XML).
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('adjuntos', 'adjuntos', false, 10485760,
        array['application/pdf', 'image/jpeg', 'image/png', 'image/webp',
              'application/xml', 'text/xml'])
on conflict (id) do nothing;

-- 7. Reparación de datos (Fase 1): los requerimientos de órdenes canceladas
--    quedaban atrapados como 'en_orden' y desaparecían de pendientes.
--    Hoy afecta a "TERGAL ARENA" (folio 846) y "tela tergal" (folio 847).
update public.requerimientos r
   set estado = 'pendiente', orden_id = null
  from public.ordenes_compra o
 where o.id = r.orden_id
   and o.estado = 'rechazada'
   and r.estado = 'en_orden';
