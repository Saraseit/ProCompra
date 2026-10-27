-- Fase 3: estado de cuenta de proveedores a crédito.
-- Idempotente. El vencimiento se calcula en el backend:
--   fecha de la factura (o de recepción, o de la orden) + dias_credito.

alter table public.proveedores
  add column if not exists dias_credito integer;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'proveedores_dias_credito_rango'
  ) then
    alter table public.proveedores
      add constraint proveedores_dias_credito_rango
      check (dias_credito is null or dias_credito between 0 and 365);
  end if;
end $$;

comment on column public.proveedores.dias_credito is
  'Días de crédito que da el proveedor. NULL = sin plazo pactado.';
