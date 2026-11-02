-- ProCompra · Fase 5: histórico de precios de insumos.
--
-- Guarda cada renglón (concepto) de las facturas CFDI y un catálogo de
-- insumos que junta las distintas formas en que cada proveedor nombra lo
-- mismo. Con eso se calcula el precio de referencia y se avisa cuando una
-- cotización se sale de lo normal.
--
-- Idempotente y aditiva: no borra ni renombra nada existente.

-- 1. Facturas importadas solo para el histórico (XML de antes de ProCompra).
--    No se vinculan a órdenes ni cuentan como "sin vincular".
alter table public.facturas
  add column if not exists historica boolean not null default false;
create index if not exists facturas_historica_idx on public.facturas (historica);

-- 2. Catálogo de insumos.
create table if not exists public.insumos (
  id          uuid primary key default gen_random_uuid(),
  nombre      text not null,
  unidad      text,
  clave_sat   text,
  notas       text,
  creado_por  uuid references public.usuarios(id),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create unique index if not exists insumos_nombre_unq on public.insumos (lower(nombre));
comment on table public.insumos is 'Catálogo de insumos para el histórico de precios.';
alter table public.insumos enable row level security;

-- 3. Formas de nombrar un insumo. texto_norm es la descripción normalizada
--    (mayúsculas, sin acentos ni signos; ver app/utils/precios.py). Con
--    ignorar = true el texto no es insumo (fletes, servicios, maniobras).
create table if not exists public.insumo_alias (
  id          uuid primary key default gen_random_uuid(),
  texto_norm  text not null unique,
  insumo_id   uuid references public.insumos(id) on delete cascade,
  ignorar     boolean not null default false,
  creado_por  uuid references public.usuarios(id),
  created_at  timestamptz not null default now(),
  constraint insumo_alias_destino check (ignorar or insumo_id is not null)
);
create index if not exists insumo_alias_insumo_idx on public.insumo_alias (insumo_id);
alter table public.insumo_alias enable row level security;

-- 4. Renglones de cada factura. El precio unitario es neto de descuento,
--    sin IVA y en pesos (si la factura viene en otra moneda se convierte con
--    su TipoCambio). Fecha y emisor se copian de la factura para consultar
--    rápido sin unir tablas.
create table if not exists public.factura_conceptos (
  id                uuid primary key default gen_random_uuid(),
  factura_id        uuid not null references public.facturas(id) on delete cascade,
  renglon           integer not null,
  descripcion       text not null,
  texto_norm        text not null,
  clave_sat         text,
  no_identificacion text,
  cantidad          numeric(16,4) not null,
  unidad            text,
  clave_unidad      text,
  valor_unitario    numeric(16,4),
  descuento         numeric(14,2),
  importe           numeric(14,2),
  precio_unitario   numeric(16,4) not null,
  fecha             timestamptz,
  rfc_emisor        text,
  nombre_emisor     text,
  insumo_id         uuid references public.insumos(id) on delete set null,
  ignorado          boolean not null default false,
  created_at        timestamptz not null default now(),
  unique (factura_id, renglon)
);
create index if not exists factura_conceptos_insumo_idx on public.factura_conceptos (insumo_id, fecha);
create index if not exists factura_conceptos_texto_idx on public.factura_conceptos (texto_norm);
create index if not exists factura_conceptos_fecha_idx on public.factura_conceptos (fecha);
comment on table public.factura_conceptos is 'Renglones de facturas CFDI para el histórico de precios.';
alter table public.factura_conceptos enable row level security;

-- 5. Al guardar un renglón se reconoce solo si su texto ya está clasificado.
create or replace function public.factura_concepto_reconocer()
returns trigger language plpgsql set search_path = public as $$
declare
  a record;
begin
  if new.insumo_id is null and not coalesce(new.ignorado, false) then
    select insumo_id, ignorar into a from public.insumo_alias where texto_norm = new.texto_norm;
    if found then
      new.insumo_id := a.insumo_id;
      new.ignorado := a.ignorar;
    end if;
  end if;
  new.ignorado := coalesce(new.ignorado, false);
  return new;
end $$;

drop trigger if exists factura_concepto_reconocer on public.factura_conceptos;
create trigger factura_concepto_reconocer
  before insert on public.factura_conceptos
  for each row execute function public.factura_concepto_reconocer();

-- 6. Resumen por artículo: un insumo del catálogo, o un texto aún sin
--    clasificar. Es la lista de la pantalla «Precios de insumos» y de
--    «Por clasificar». Solo facturas de ingreso; sin renglones ignorados.
create or replace view public.precios_articulos with (security_invoker = true) as
select
  coalesce(c.insumo_id::text, 't:' || c.texto_norm)                   as clave,
  c.insumo_id,
  case when c.insumo_id is null then c.texto_norm end                 as texto_norm,
  max(i.nombre)                                                       as insumo_nombre,
  max(i.unidad)                                                       as insumo_unidad,
  (array_agg(c.descripcion order by c.fecha desc nulls last))[1]      as descripcion,
  mode() within group (order by c.unidad)                             as unidad,
  count(*)                                                            as compras,
  count(distinct c.rfc_emisor)                                        as proveedores,
  (array_agg(c.nombre_emisor order by c.fecha desc nulls last))[1]    as ultimo_proveedor,
  min(c.fecha)                                                        as primera,
  max(c.fecha)                                                        as ultima,
  (array_agg(c.precio_unitario order by c.fecha desc nulls last))[1]  as ultimo_precio,
  percentile_cont(0.5) within group (order by c.precio_unitario)      as mediana,
  sum(c.importe)                                                      as gastado,
  lower(coalesce(max(i.nombre), '') || ' ' || string_agg(distinct c.texto_norm, ' ')) as busqueda
from public.factura_conceptos c
left join public.insumos i on i.id = c.insumo_id
where not c.ignorado
group by 1, 2, 3;
comment on view public.precios_articulos is 'Resumen de precios por insumo o texto sin clasificar.';
