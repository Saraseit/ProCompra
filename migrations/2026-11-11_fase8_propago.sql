-- Fase 8: ProPago. Cajas, catálogo contable editable y registros para el ERP.
-- Idempotente: se puede correr más de una vez.
--
-- Cada pago sale de una caja. Al registrarlo, ProCompra arma sus renglones
-- para el ERP (cuenta, texto, contrato, importe) con las reglas del catálogo;
-- se revisan, se eligen y se descargan en Excel para subirlos al ERP.
--
-- Cuentas, reglas, plantillas y parámetros son datos: se editan desde la
-- pantalla de Catálogo contable, sin otra migración.

-- ── Catálogo contable ─────────────────────────────────

create table if not exists public.cuentas_contables (
  id              uuid primary key default gen_random_uuid(),
  clave           text not null unique,
  nombre_erp      text not null,
  grupo           text,
  tipo            text not null default 'gasto'
                  check (tipo in ('gasto', 'iva', 'retencion_isr', 'retencion_iva')),
  cdv             boolean not null default false,
  activa          boolean not null default true,
  verificada_erp  boolean not null default false,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table if not exists public.plantillas_registro (
  id           uuid primary key default gen_random_uuid(),
  codigo       text not null unique,
  plantilla    text not null,
  descripcion  text,
  activa       boolean not null default true,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create table if not exists public.cajas (
  id              uuid primary key default gen_random_uuid(),
  nombre          text not null unique,
  tipo            text not null default 'banco'
                  check (tipo in ('banco', 'tarjeta_debito', 'tarjeta_credito', 'amex', 'inversion',
                                  'caja_chica_virtual', 'fondo_fijo')),
  metodo          metodo_pago not null default 'transferencia',
  plaza           text not null default 'MID' check (plaza in ('MID', 'CUN')),
  prefijo_poliza  text,
  numero_cuenta   text,
  responsable_id  uuid references public.usuarios(id) on delete set null,
  activa          boolean not null default true,
  notas           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table if not exists public.caja_usuarios (
  caja_id          uuid not null references public.cajas(id) on delete cascade,
  usuario_id       uuid not null references public.usuarios(id) on delete cascade,
  puede_registrar  boolean not null default true,
  puede_exportar   boolean not null default true,
  primary key (caja_id, usuario_id)
);

-- Una regla con cuenta decide cuenta, categoría y plantilla (gana la de menor
-- prioridad). Una regla sin cuenta solo modifica: fuerza ND o desglosa IVA.
create table if not exists public.reglas_cuenta (
  id                uuid primary key default gen_random_uuid(),
  nombre            text not null,
  prioridad         int not null default 50,
  proveedor_id      uuid references public.proveedores(id) on delete cascade,
  caja_id           uuid references public.cajas(id) on delete cascade,
  plaza             text check (plaza in ('MID', 'CUN')),
  insumo_id         uuid references public.insumos(id) on delete cascade,
  palabras          text[] not null default '{}',
  palabras_excluir  text[] not null default '{}',
  con_cfdi          boolean,
  cuenta_id         uuid references public.cuentas_contables(id) on delete restrict,
  categoria_texto   text,
  fuerza_nd         boolean not null default false,
  desglosa_iva      boolean not null default false,
  plantilla_id      uuid references public.plantillas_registro(id) on delete set null,
  activa            boolean not null default true,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists reglas_cuenta_prioridad_idx on public.reglas_cuenta (prioridad);

create table if not exists public.parametros_contables (
  clave        text primary key,
  valor        text,
  descripcion  text,
  updated_at   timestamptz not null default now()
);

-- ── Pagos ─────────────────────────────────────────────

alter table public.movimientos_pago
  add column if not exists caja_id          uuid references public.cajas(id) on delete restrict,
  add column if not exists monto            numeric(14,2),
  add column if not exists tipo             text not null default 'con_orden',
  add column if not exists plaza            text,
  add column if not exists justificacion    text,
  add column if not exists proveedor_texto  text,
  add column if not exists comision         numeric(14,2) not null default 0,
  add column if not exists diferencia       numeric(14,2) not null default 0,
  add column if not exists poliza           int,
  add column if not exists poliza_fija      boolean not null default false;

do $$ begin
  alter table public.movimientos_pago
    add constraint movimientos_pago_tipo_chk check (tipo in ('con_orden', 'sin_orden'));
exception when duplicate_object then null; end $$;

create index if not exists movimientos_pago_caja_idx on public.movimientos_pago (caja_id, fecha_pago);

alter table public.pagos
  add column if not exists naturaleza text;

do $$ begin
  alter table public.pagos
    add constraint pagos_naturaleza_chk check (naturaleza in ('total', 'anticipo', 'abono', 'saldo'));
exception when duplicate_object then null; end $$;

-- Líneas de un pago sin orden (casetas, DiDi, hidratación...).
create table if not exists public.pago_lineas (
  id             uuid primary key default gen_random_uuid(),
  movimiento_id  uuid not null references public.movimientos_pago(id) on delete cascade,
  renglon        int not null default 1,
  concepto       text not null,
  cantidad       numeric,
  contrato       text,
  importe        numeric(14,2) not null check (importe > 0),
  con_iva        boolean not null default false,
  cuenta_id      uuid references public.cuentas_contables(id) on delete restrict,
  created_at     timestamptz not null default now()
);

create index if not exists pago_lineas_movimiento_idx on public.pago_lineas (movimiento_id);

-- ── Registros para el ERP ─────────────────────────────

create table if not exists public.lotes_exportacion (
  id             uuid primary key default gen_random_uuid(),
  fecha          date not null,
  num_renglones  int not null default 0,
  total          numeric(14,2) not null default 0,
  estado         text not null default 'generado' check (estado in ('generado', 'registrado', 'anulado')),
  generado_por   uuid references public.usuarios(id) on delete set null,
  registrado_por uuid references public.usuarios(id) on delete set null,
  registrado_at  timestamptz,
  created_at     timestamptz not null default now()
);

create table if not exists public.registros_erp (
  id                 uuid primary key default gen_random_uuid(),
  movimiento_id      uuid not null references public.movimientos_pago(id) on delete cascade,
  pago_id            uuid references public.pagos(id) on delete cascade,
  orden_id           uuid references public.ordenes_compra(id) on delete set null,
  caja_id            uuid references public.cajas(id) on delete set null,
  renglon            int not null default 1,
  cargo              numeric(14,2) not null check (cargo > 0),
  cuenta_id          uuid references public.cuentas_contables(id) on delete restrict,
  cuenta_nombre_erp  text,
  ref                text not null,
  contrato           text,
  id_evento          bigint not null default 0,
  propietario        text,
  es_iva             boolean not null default false,
  regla_id           uuid references public.reglas_cuenta(id) on delete set null,
  estado             text not null default 'listo' check (estado in ('pendiente', 'listo', 'exportado')),
  avisos             text[] not null default '{}',
  editado_manual     boolean not null default false,
  lote_id            uuid references public.lotes_exportacion(id) on delete set null,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create index if not exists registros_erp_movimiento_idx on public.registros_erp (movimiento_id);
create index if not exists registros_erp_estado_idx on public.registros_erp (estado, caja_id);
create index if not exists registros_erp_lote_idx on public.registros_erp (lote_id);
create index if not exists registros_erp_orden_idx on public.registros_erp (orden_id);

-- Correcciones a renglones ya exportados: el ERP no acepta cargos negativos,
-- así que se corrigen a mano en el ERP y aquí se marcan como aplicadas.
create table if not exists public.ajustes_erp (
  id            uuid primary key default gen_random_uuid(),
  registro_id   uuid references public.registros_erp(id) on delete set null,
  lote_id       uuid references public.lotes_exportacion(id) on delete set null,
  accion        text not null check (accion in ('modificar', 'eliminar')),
  antes         jsonb not null,
  despues       jsonb,
  motivo        text,
  estado        text not null default 'pendiente' check (estado in ('pendiente', 'aplicado')),
  creado_por    uuid references public.usuarios(id) on delete set null,
  aplicado_por  uuid references public.usuarios(id) on delete set null,
  aplicado_at   timestamptz,
  created_at    timestamptz not null default now()
);

create index if not exists ajustes_erp_estado_idx on public.ajustes_erp (estado);

create table if not exists public.bitacora (
  id          uuid primary key default gen_random_uuid(),
  entidad     text not null,
  entidad_id  text,
  accion      text not null,
  antes       jsonb,
  despues     jsonb,
  usuario_id  uuid references public.usuarios(id) on delete set null,
  created_at  timestamptz not null default now()
);

create index if not exists bitacora_entidad_idx on public.bitacora (entidad, entidad_id);

-- El backend usa la service_role key; RLS cerrado para cualquier otro acceso.
alter table public.cuentas_contables   enable row level security;
alter table public.plantillas_registro enable row level security;
alter table public.cajas               enable row level security;
alter table public.caja_usuarios       enable row level security;
alter table public.reglas_cuenta       enable row level security;
alter table public.parametros_contables enable row level security;
alter table public.pago_lineas         enable row level security;
alter table public.lotes_exportacion   enable row level security;
alter table public.registros_erp       enable row level security;
alter table public.ajustes_erp         enable row level security;
alter table public.bitacora            enable row level security;

-- Los pagos anteriores a ProPago no se exportan: ya están en el ERP.
comment on column public.movimientos_pago.caja_id is
  'Caja de la que salió el pago. NULL = pago anterior a ProPago (no genera registros ERP).';
comment on table public.registros_erp is
  'Renglones del archivo de importación al ERP, generados por el motor de reglas a partir de cada pago.';
