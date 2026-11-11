# ProPago — Especificación del módulo de pagos y registros ERP

Módulo dentro de **ProCompra** (FastAPI + Supabase + React/Vite) para registrar todos los pagos de Minimal 4.0 desde la caja que los paga, amarrarlos a su orden y su comprobante, y generar automáticamente los renglones de importación al ERP siguiendo las reglas contables vigentes.

> Versión 1.1 — 11/11/2026. Basada en las reglas aplicadas a mano en la captura de registros de jun–nov 2026.
> Las secciones marcadas **[ABIERTO]** requieren decisión antes de implementar esa parte; el resto está decidido.

---

## 0. Cambios respecto a la versión 1

1. **Tablas ajustadas a lo que ya existe en ProCompra.** El "pago" de la v1 es la tabla `movimientos_pago`; la "aplicación" es la tabla `pagos`; las órdenes OS/OG/OP viven en `ordenes_compra` con una columna `tipo`; los CFDI siguen en `facturas`. Ver §4.0.
2. **El renglón de IVA (H01) nunca lleva `[CDV] - `.** El prefijo depende de la cuenta del propio renglón (§7.4).
3. **Cuentas, reglas, plantillas, contratos y parámetros se editan desde la pantalla** (§10). Una sola migración crea las tablas; después ningún cambio de catálogo necesita migración.
4. **Retenciones:** gasto por el subtotal, IVA en H01 y cada retención en su propia cuenta por pagar. Las claves de esas cuentas quedan pendientes (§7.7.3, §14).
5. **Factura pagada en abonos:** cada abono lleva su parte proporcional del IVA y se exige el complemento de pago (§7.7.4).
6. **Comisiones y diferencias** entre comprobante y banco se suman al registro principal (§7.9).
7. **Correcciones después de exportar:** el ERP no acepta cargos negativos; se generan ajustes a aplicar en el ERP (§6.15).
8. **Folios:** OC, OS, OG y OP llevan cada uno su propia numeración (§4.3.2).
9. **Póliza TCB:** queda fija al exportarse; una compra que llega tarde toma el siguiente número (§6.6.1).
10. IVA por partida (16 %, 8 %, 0 %, exento) en lugar del 16 % fijo de toda la orden.
11. Catálogo de contratos, IEPS, descuentos, USD, saldo de cajas, deshacer lote y aprobación en ausencia del responsable (§13).
12. Casos de prueba corregidos (CP-05, CP-07, CP-10) y nuevos (CP-13 a CP-19).

---

## 1. Objetivo y alcance

**Objetivo.** Que cada salida de dinero:
1. salga de una **caja** con dueño y permisos,
2. se aplique a una o varias **órdenes** (o quede como gasto sin orden justificado),
3. tenga su **comprobante** (CFDI, ticket, remisión, nota),
4. genere sus **renglones ERP** sin escribirlos a mano,
5. se **concilie** contra el estado de cuenta de la caja.

**Se construye sobre lo que ya existe**
- Órdenes de compra con su flujo (borrador → autorización → autorizada → pagada → recolección → comprobación → cerrada).
- Módulo Pagos: un pago puede cubrir varias órdenes; "esta semana", "después" y "sin fecha".
- Compras a crédito con calendario de pagos por orden y pantalla de Crédito. **Se conservan sin cambios de comportamiento.**
- Lectura de CFDI (XML) con sus renglones, detección de UUID duplicado e histórico de precios por insumo.

**Dentro del alcance v1**
- Cajas y permisos por caja.
- Pagos totales, parciales (anticipo/abono/saldo) y a varias órdenes.
- Gastos sin orden con justificación.
- Cajas chicas administrativas (virtuales) y fondo fijo de Cancún.
- Lectura de comprobantes (XML CFDI, complementos de pago y tickets por visión).
- Motor de reglas → renglones ERP → exportación Excel.
- Edición de cuentas, reglas, plantillas, contratos y parámetros desde la pantalla.
- Conciliación contra estados de cuenta (BBVA compras, AMEX, BBVA, etc.).
- Aprobación de registros de auxiliares.

**Fuera de alcance v1**
- Estado de cuenta por proveedor a crédito con cartera y antigüedad de saldos (lavanderías, Departamento 360, etc.). Lo que ya existe en Crédito sigue funcionando.
- Escritura directa al ERP (se sigue importando por Excel).

---

## 2. Glosario

| Término | Significado |
|---|---|
| **Caja** | Medio de pago: cuenta bancaria, tarjeta, AMEX, inversión o caja chica. Tiene propietario ERP y usuarios asignados. |
| **Orden** | Documento de ProCompra que autoriza un pago: OC (compra), OS (subrenta), OG (gasto), OP (pago de servicio, permiso o similar). Todas viven en `ordenes_compra`. |
| **Pago** | Salida de dinero de una caja, por un monto y fecha. Tabla `movimientos_pago`. |
| **Aplicación** | Parte de un pago aplicada a una orden. Tabla `pagos` (nombre heredado). Un pago puede aplicarse a varias órdenes y una orden recibir varios pagos. |
| **Comprobante** | Evidencia del pago. Los CFDI viven en `facturas`; tickets, remisiones, notas y solicitudes en `comprobantes`. |
| **Complemento de pago (REP)** | CFDI tipo P que el proveedor emite por cada abono a una factura PPD. |
| **Gasto sin orden** | Pago hecho por urgencia sin orden previa (casetas, Uber/DiDi, hidratación, comisiones). Requiere justificación. |
| **Traspaso virtual** | Movimiento contable entre cajas sin mover efectivo (ej. Luis → caja chica de David). |
| **Comprobación de fondo** | Entrega de tickets de un fondo fijo (Miguel, Cancún) que justifica el efectivo y dispara la reposición. No confundir con la *comprobación de una orden* que ya existe (monto comprobado y cambio devuelto). |
| **Renglón ERP** | Una fila del archivo de importación (FECHA, CONCEPTO, CARGO, CLIENTE O GASTO EN, REF, ID_EVENTO, PROPIETARIO). |
| **Lote de exportación** | Conjunto de renglones ERP exportados juntos a un Excel. |
| **Ajuste ERP** | Corrección que hay que hacer a mano en el ERP sobre un renglón ya exportado (§6.15). |
| **Plaza** | `MID` (Mérida) o `CUN` (Cancún). Prefijo del texto del registro. |

---

## 3. Usuarios, roles y permisos

La autorización vive en el backend (Python), igual que el resto de ProCompra (service_role; RLS activado sin políticas, así que solo el backend lee y escribe).

### 3.1 Roles globales (existentes)
`admin`, `compras`, `almacen`, `pagos`. Se conservan. ProPago agrega permisos **por caja** encima de ellos.

| Acción | Quién |
|---|---|
| Crear y editar cajas, asignar usuarios a cajas | admin |
| Editar cuentas, reglas, plantillas, contratos y parámetros (§10) | admin y pagos |
| Registrar, exportar y aprobar | según su asignación en la caja (§3.2) |
| Aprobar en ausencia del responsable | admin |
| Borrar pagos no exportados | admin (como hoy) |

### 3.2 Permiso por caja
- Cada usuario ve **solo las cajas que tiene asignadas** en `caja_usuarios`.
- Todos ven su **caja chica personal**.
- Asignación inicial:
  - Isaac Rodríguez: BBVA, Revolvente, Actinver, Fondo de inversión, su caja chica.
  - David: Compras (TCB), AMEX, su caja chica.
  - Miriam: su caja chica.
  - Luis Hu (gerente administrativo): custodio del efectivo de caja chica; ve todas las cajas chicas.
  - Miguel (responsable operativo Cancún): su fondo fijo Cancún.
- Permisos por asignación: `ver`, `registrar`, `exportar`, `aprobar`.

### 3.3 Validación
- Por defecto, quien registra **puede exportar directamente**; cada quien es responsable de revisar sus registros.
- **Auxiliares:** usuario con `requiere_aprobacion = true` y un `responsable_id`. Sus renglones quedan en `por_aprobar` hasta que el responsable (o un admin) los aprueba; solo entonces pueden exportarse.

---

## 4. Modelo de datos (Supabase / Postgres)

Convenciones para tablas nuevas: `id uuid pk default gen_random_uuid()`, `created_at`, `created_by`, `updated_at`. Montos `numeric(14,2)`. Fechas `date`. RLS activado sin políticas.

### 4.0 Qué se reutiliza y qué es nuevo

| Concepto | Tabla | Estado |
|---|---|---|
| Orden (OC, OS, OG, OP) | `ordenes_compra` | Existe; se agregan columnas (§4.3) |
| Partida | `partidas` | Existe; se agregan columnas |
| Reparto entre contratos | `partida_contratos` | Nueva |
| Pago (salida de dinero) | `movimientos_pago` | Existe; se agregan columnas (§4.4) |
| Aplicación a una orden | `pagos` | Existe; se agregan columnas |
| Líneas de un gasto sin orden | `pago_lineas` | Nueva |
| CFDI de ingreso | `facturas` + `factura_conceptos` | Existen; se agregan columnas (§4.5) |
| Complemento de pago | `complementos_pago` | Nueva |
| Ticket, remisión, nota, solicitud | `comprobantes` + `comprobante_lineas` | Nuevas |
| Archivos de una orden | `adjuntos` | Existe sin cambios |
| Historial de una orden | `historial` | Existe sin cambios |
| Bitácora general | `bitacora` | Nueva |
| Configuración del usuario | `usuarios` | Existe; se agregan columnas |
| Calendario de crédito | `pagos_programados` | Existe sin cambios |
| Catálogos y parámetros | `cuentas_contables`, `reglas_cuenta`, `plantillas_registro`, `contratos`, `parametros_contables` | Nuevas, editables desde pantalla |
| Cajas | `cajas`, `caja_usuarios`, `traspasos`, `comprobaciones_fondo` | Nuevas |
| Registros y exportación | `registros_erp`, `lotes_exportacion`, `ajustes_erp`, `folios` | Nuevas |
| Conciliación | `estados_cuenta`, `estado_cuenta_movimientos` | Nuevas |

### 4.1 Catálogos editables

**`cuentas_contables`**
| columna | tipo | nota |
|---|---|---|
| clave | text unique | `A09.1`, `C06`, `H01`… Se puede corregir; todo lo demás liga por `id`. |
| nombre_erp | text | Texto exacto del ERP, ej. `100 - D03 - SUB-CONTRATACION MANTTO GENERAL INVENTARIO` |
| grupo | text | A, B, C, D… |
| tipo | text | `gasto`, `iva` (H01), `retencion_isr`, `retencion_iva` |
| cdv | bool | Sus renglones llevan `[CDV] - ` (C06 y toda la serie B). H01 siempre `false`. |
| activa | bool | Una cuenta usada nunca se borra: se desactiva. |
| verificada_erp | bool | Nombre confirmado contra el ERP |

**`reglas_cuenta`** — sugerencia de cuenta y tratamiento por concepto. Una regla aplica si **todas** sus condiciones llenas se cumplen; gana la de menor prioridad.

| columna | tipo | nota |
|---|---|---|
| nombre | text | Para reconocerla en pantalla: "Telas en compras" |
| prioridad | int | Menor = se evalúa primero |
| *Condiciones* | | Vacía = no se toma en cuenta |
| tipos_orden | text[] | OC/OS/OG/OP |
| caja_id | uuid null | |
| plaza | text null | MID/CUN |
| proveedor_id | uuid null | Ej. lavanderías → D03 |
| insumo_id | uuid null | Insumo del histórico de precios ("Rollo emplaye") |
| palabras | text[] | Basta con que aparezca una, sobre el texto normalizado (sin acentos, mayúsculas). Sin expresiones regulares. |
| palabras_excluir | text[] | Si aparece alguna, la regla no aplica |
| con_cfdi | bool null | null = da igual |
| monto_min / monto_max | numeric null | |
| *Acciones* | | |
| cuenta_id | uuid fk | |
| categoria_texto | text null | `TELA`, `PINTURA`, `LAVADO`, `#LOG`, `ESCENOGRAFIA` |
| fuerza_nd | bool | Importe completo sin H01 aunque haya IVA (caja Cancún con tickets) |
| desglosa_iva | bool | Calcula subtotal e IVA desde el total cuando no hay CFDI (AMEX, §8.3) |
| plantilla_id | uuid null | Plantilla del texto (§7.5) |
| activa | bool | |

**`plantillas_registro`** — una por tipo de registro (§7.5)
| columna | tipo |
|---|---|
| codigo | text unique (`TCB`, `AMEX`, `LAVANDERIA`, `DEPTO360`, `FLETE`, `SUBRENTA`, `REEMBOLSO`, `CAJA_CUN`, `MYD_CUN`, `GENERAL`, `FERRETERIA`) |
| plantilla | text con placeholders (§10.3) |
| agrupa_por_contrato | bool |
| descripcion | text |
| activa | bool |

**`contratos`** — catálogo opcional importado del ERP
| columna | tipo | nota |
|---|---|---|
| numero | text unique | 5 dígitos o especial (`DIRECTO`, `BODEGA`, `MONTAJE`, `MANTENIMIENTO`, `PROTOTIPO`) |
| id_evento | int | Valor de ID_EVENTO; 0 para los especiales |
| especial | bool | |
| cliente | text null | Llena el texto de fletes y MYD |
| lugar | text null | |
| fecha_evento | date null | De aquí sale la semana de fletes |
| plaza | text null | |
| activo | bool | |

Mientras el catálogo no tenga un contrato, se acepta con aviso (§11).

**`parametros_contables`** — clave/valor editable
| clave | valor inicial | uso |
|---|---|---|
| `cuenta_comisiones` | vacío | Vacío = la comisión se suma al registro principal (§7.9). Con cuenta = renglón aparte. |
| `cuenta_diferencias` | vacío | Igual, para propinas y redondeos |
| `aviso_diferencia_monto` | 50.00 | Avisa si la diferencia supera este monto… |
| `aviso_diferencia_pct` | 0.05 | …o este porcentaje del pago |
| `dias_conciliacion` | 3 | Tolerancia entre operación y liquidación |
| `confianza_minima_vision` | 0.85 | Debajo de esto se exige revisión |
| `propietario_default` | vacío | Para cajas sin propietario [ABIERTO §14.3] |

### 4.2 Cajas

**`cajas`**
| columna | tipo | nota |
|---|---|---|
| nombre | text | `Compras BBVA`, `AMEX Gold Corporate`, `Caja chica David`… |
| tipo | text | `banco`, `tarjeta_debito`, `tarjeta_credito`, `amex`, `inversion`, `caja_chica_virtual`, `fondo_fijo` |
| metodo_pago | metodo_pago | Valor del enum existente que se guarda en los pagos de esta caja (transferencia, tarjeta_credito, efectivo, tarjeta_debito) para que las pantallas actuales sigan funcionando |
| propietario_erp | text null | `COMPRAS`, `AMERICAN`, `BBVA`… [ABIERTO §14.3] para cajas chicas y fondo Cancún |
| plaza_default | text | `MID` / `CUN` |
| prefijo_poliza | text null | `TCB` para la tarjeta de compras |
| numero_cuenta_mask | text | Solo los últimos dígitos |
| responsable_id | uuid | Custodio (Luis para cajas chicas, Miguel para fondo Cancún) |
| monto_fondo | numeric null | Solo `fondo_fijo` |
| activa | bool | |

El **saldo** se calcula, no se guarda:
- Caja chica y fondo fijo: traspasos recibidos − pagos registrados.
- Bancos y tarjetas: ProPago no registra ingresos, así que su saldo es el del último estado de cuenta cargado (§6.12).

**`caja_usuarios`**: `caja_id`, `usuario_id`, `puede_ver`, `puede_registrar`, `puede_exportar`, `puede_aprobar`.

**`usuarios`** (existe) + `requiere_aprobacion bool default false`, `responsable_id uuid null`.

### 4.3 Órdenes

#### 4.3.1 `ordenes_compra` (existe) — columnas nuevas
| columna | tipo | nota |
|---|---|---|
| tipo | text default `OC` | `OC`, `OS`, `OG`, `OP` |
| plaza | text default `MID` | |
| doc_proveedor | text null | Remisión o factura del proveedor (REM2405, FAC0155) |
| tipo_viaje | text null | Fletes: `REDONDO` / `SENCILLO` |
| semana_evento | int null | Fletes; si hay catálogo de contratos se calcula de `fecha_evento` |
| origen | text default `normal` | `normal` o `regularizacion` (OG creada al comprobar un gasto sin orden) |

- `folio` deja de ser único global: único por `(tipo, folio)`.
- El **estado operativo** existente no cambia. El **estado de pago** se calcula en la vista `ordenes_saldo`: `total`, `pagado` (Σ aplicaciones), `saldo`, `estado_pago` (`sin_pago`, `parcial`, `pagada`).
- Flujo por tipo:
  - OC: el actual.
  - OS: el actual; recolección y entrega sirven para recoger y devolver lo subrentado.
  - OP: borrador → autorización → autorizada → pagada → cerrada (sin recolección).
  - OG de regularización: nace `pagada`, ligada al pago sin orden que regulariza.

#### 4.3.2 Folios por tipo
Tabla **`folios`** (`tipo` pk, `ultimo int`) y una función `siguiente_folio(tipo)` que incrementa de forma atómica.
- OC continúa la numeración actual (arranca en el folio más alto existente).
- OS, OG y OP empiezan en 1 cada uno.
- En pantalla: `OC 850`, `OS 12`, `OG 3`, `OP 7`. Donde solo se ven órdenes de compra se puede seguir mostrando `#850`.

#### 4.3.3 `partidas` (existe) — columnas nuevas
| columna | tipo | nota |
|---|---|---|
| iva_tasa | text default `16` | `16`, `8`, `0`, `exento` |
| cuenta_id | uuid null | Sugerida por reglas, editable |
| categoria_texto | text null | |

- El campo `contrato` existente se mantiene como texto capturado; el reparto real vive en `partida_contratos`.
- La función `recalcular_totales_orden` cambia: hoy aplica 16 % fijo a toda la orden; pasa a sumar el IVA de cada partida según su tasa. Las órdenes existentes no cambian (todas sus partidas quedan en `16`).

#### 4.3.4 `partida_contratos` (nueva)
| columna | tipo | nota |
|---|---|---|
| partida_id | uuid null | Uno de los dos, nunca ambos |
| pago_linea_id | uuid null | |
| contrato | text | Número o especial |
| monto | numeric null | Null = partes iguales (§7.6) |
| evento | text null | Fletes y MYD: "ANA SANTOS, PRIVADA ALLEGRA"; se autocompleta del catálogo de contratos |

### 4.4 Pagos

#### 4.4.1 `movimientos_pago` (existe) — el pago
Columnas existentes: `fecha_pago` (fecha del movimiento), `metodo`, `referencia` (AUT, folio SPEI…), `entregado_a`, `notas`, `registrado_por`.

Columnas nuevas:
| columna | tipo | nota |
|---|---|---|
| caja_id | uuid | |
| monto | numeric | Lo que salió de la caja. Debe ser Σ aplicaciones + Σ líneas sin orden + comisión + diferencia. |
| tipo | text | `con_orden`, `sin_orden`, `reposicion` |
| plaza | text | De la caja; editable |
| justificacion | text null | Obligatoria si `sin_orden` |
| comision | numeric default 0 | Comisión cobrada por el medio de pago (OXXO, SPEI) |
| diferencia | numeric default 0 | Propina o redondeo entre comprobante y banco |
| poliza | int null | Solo cajas con `prefijo_poliza` (§6.6.1) |
| poliza_fija | bool default false | `true` al exportarse |
| estado | text | `borrador`, `comprobado`, `por_aprobar`, `listo`, `exportado`, `cancelado`, `historico` |
| conciliado | bool | |

#### 4.4.2 `pagos` (existe) — la aplicación a una orden
Columnas existentes: `orden_id`, `movimiento_id`, `monto`, `fecha_pago`, `metodo`, `referencia`, `comprobante_url`, `notas`. Las que repiten datos del movimiento se siguen llenando para que las pantallas actuales funcionen.

Columnas nuevas:
| columna | tipo | nota |
|---|---|---|
| naturaleza | text | `total`, `anticipo`, `abono`, `saldo`. Va en la aplicación, no en el pago: un mismo pago puede ser saldo de una orden y anticipo de otra. |
| factura_id | uuid null | CFDI que paga este abono; base del IVA proporcional (§7.7.4). Si un abono cubre dos facturas, son dos aplicaciones. |

#### 4.4.3 `pago_lineas` (nueva) — líneas de un gasto sin orden
Mismo esquema que una partida: `movimiento_id`, `cantidad`, `concepto`, `importe`, `iva_tasa`, `cuenta_id`, `categoria_texto`. Su reparto entre contratos va en `partida_contratos.pago_linea_id`.

#### 4.4.4 `traspasos` (nueva)
`caja_origen_id`, `caja_destino_id`, `monto`, `fecha`, `motivo`, `virtual bool`, `comprobacion_id null` (reposición de fondo).

#### 4.4.5 `comprobaciones_fondo` (nueva)
`caja_id`, `periodo_desde`, `periodo_hasta`, `total_comprobado`, `estado` (`borrador`, `entregada`, `registrada`, `repuesta`), `traspaso_reposicion_id`.

### 4.5 Comprobantes

#### 4.5.1 `facturas` (existe) — CFDI de ingreso
Ya guarda UUID (único), RFC, serie, folio, fecha, subtotal, IVA, total, moneda, `metodo_pago` (PUE/PPD), forma de pago, tipo de comprobante, orden y XML. Sus renglones están en `factura_conceptos`, ya con precio neto de descuento.

Columnas nuevas:
| columna | tipo | nota |
|---|---|---|
| tipo_cambio | numeric null | Si la moneda no es MXN |
| isr_retenido | numeric default 0 | Del nodo de retenciones |
| iva_retenido | numeric default 0 | |
| ieps | numeric default 0 | Trasladado |
| iva_por_tasa | jsonb | `{"16": 160.00, "8": 0, "0": 0}`; base de la validación del IVA |

#### 4.5.2 `complementos_pago` (nueva)
| columna | tipo | nota |
|---|---|---|
| uuid_fiscal | text unique | |
| factura_id | uuid | Factura relacionada (IdDocumento) |
| aplicacion_id | uuid null | Abono que ampara |
| num_parcialidad | int | |
| imp_pagado | numeric | |
| imp_saldo_insoluto | numeric | |
| fecha_pago | date | |
| xml_ruta | text | |

Hoy la importación descarta los CFDI tipo P; se agrega su lectura (`Pagos` → `DoctoRelacionado`).

#### 4.5.3 `comprobantes` (nueva) — todo lo que no es CFDI
| columna | tipo | nota |
|---|---|---|
| movimiento_id | uuid null | |
| comprobacion_id | uuid null | |
| tipo | text | `ticket`, `remision`, `nota_venta`, `solicitud`, `estado_cuenta` |
| archivo_ruta | text | Supabase Storage |
| proveedor_texto | text | Como viene en el documento |
| fecha_doc | date | |
| folio_doc | text | |
| total | numeric | |
| moneda | text | |
| fuente_extraccion | text | `vision`, `texto_pdf`, `manual` |
| confianza | numeric | 0–1 |
| revisado | bool | El usuario confirmó los datos |

**`comprobante_lineas`**: `comprobante_id`, `cantidad`, `descripcion`, `importe`, `iva_tasa`, `contrato_detectado` (las remisiones de 100 Años traen "CONTRATO: 17132" por bloque).

### 4.6 Registros ERP y exportación

**`registros_erp`**
| columna | tipo | nota |
|---|---|---|
| movimiento_id | uuid | |
| aplicacion_id | uuid null | |
| fecha_captura | date | Columna FECHA |
| concepto_erp | text | Siempre `EFECTIVO` |
| cargo | numeric(14,2) | Siempre positivo |
| cuenta_id | uuid | |
| cuenta_nombre_erp | text | **Copia** del nombre al generar el renglón. Si después se renombra la cuenta, los renglones exportados conservan el nombre con que salieron. |
| ref | text | Texto completo (§7.4) |
| id_evento | int | |
| propietario | text null | De la caja |
| poliza | int null | |
| es_iva | bool | Renglón H01 |
| es_retencion | bool | [ABIERTO §14.1] |
| grupo_iva | uuid | Liga subtotales con su IVA |
| regla_id | uuid null | Regla que sugirió la cuenta (para "volver a aplicar reglas", §10.2) |
| estado | text | `por_aprobar`, `listo`, `exportado` |
| aprobado_por | uuid null | |
| lote_id | uuid null | |
| fecha_registro_erp | date null | Cuando se subió al ERP |
| editado_manual | bool | El usuario sobrescribió texto, cuenta o importe |

**`lotes_exportacion`**: `caja_id null`, `generado_por`, `fecha`, `num_renglones`, `total`, `archivo_ruta`, `estado` (`generado`, `registrado`, `anulado`).

**`ajustes_erp`** — correcciones a renglones ya exportados (§6.15)
| columna | tipo | nota |
|---|---|---|
| registro_id | uuid | Renglón exportado afectado |
| accion | text | `modificar` o `eliminar` |
| antes | jsonb | Valores como se exportaron |
| despues | jsonb null | Valores correctos (`modificar`) |
| motivo | text | |
| estado | text | `pendiente`, `aplicado` |
| aplicado_por / aplicado_at | | Quien lo hizo en el ERP |

### 4.7 Conciliación

**`estados_cuenta`**: `caja_id`, `periodo_desde`, `periodo_hasta`, `saldo_inicial`, `saldo_final`, `total_cargos`, `total_abonos`, `archivo_ruta`.

**`estado_cuenta_movimientos`**: `estado_cuenta_id`, `fecha_operacion`, `fecha_liquidacion`, `descripcion`, `rfc`, `cargo`, `abono`, `saldo`, `autorizacion`, `tipo` (`cargo`, `traspaso_propio`, `pago_tarjeta`, `credito`, `comision`, `interes`), `movimiento_id null`, `estado_match` (`conciliado`, `sin_pago`, `ignorado`).

### 4.8 Bitácora
**`bitacora`**: `entidad`, `entidad_id`, `accion`, `antes jsonb`, `despues jsonb`, `usuario_id`, `fecha`. Registra todo cambio a catálogos, pagos, renglones, lotes y ajustes. El `historial` de cada orden se sigue llenando como hoy.

### 4.9 Datos existentes
- Los pagos registrados antes de ProPago quedan con `estado = historico`: no generan renglones ni se exportan.
- Se les asigna caja según su método (transferencia → BBVA, tarjeta → Compras, efectivo → caja chica de quien registró), solo como referencia.
- Las órdenes existentes quedan como `tipo = OC`, `plaza = MID`, partidas al 16 %.

---

## 5. Estados

**Orden:** el flujo operativo actual no cambia. El estado de pago (`sin_pago`, `parcial`, `pagada`) se calcula del saldo.

**Pago:** `borrador` → `comprobado` (tiene comprobante revisado o justificación) → `por_aprobar` (solo auxiliares) → `listo` → `exportado`. `cancelado` antes de exportar. Un pago `exportado` no se edita: se corrige con un ajuste ERP (§6.15).

**Renglón ERP:** se regenera mientras el pago no esté `exportado`. Si `editado_manual = true`, no se sobrescribe al regenerar; se marca la diferencia contra lo que daría el motor.

---

## 6. Flujos

### 6.1 Pago con orden (OC / OS / OG / OP)
1. El usuario elige la caja (solo ve las suyas) y "Nuevo pago".
2. Selecciona una o varias órdenes con saldo; captura el monto aplicado a cada una (default = saldo pendiente) y su naturaleza.
3. Sube el comprobante (XML/PDF/foto). El sistema extrae los datos (§7.1) y los compara contra las órdenes (§7.2).
4. Captura la comisión y la diferencia si el banco cobró algo más (§7.9).
5. Confirma o corrige las diferencias.
6. El motor genera los renglones ERP (§7.3). El usuario los revisa en pantalla.
7. Estado `listo` (o `por_aprobar` si es auxiliar).

### 6.2 Anticipos, abonos y saldos
- Una orden recibe varios pagos (ej. Charly 17200: anticipo $5,000 y saldo $9,329; REM2405: $7,820.91 y después $30,000).
- El texto se arma con la misma plantilla de la orden, cambiando el verbo:
  - Anticipo: `PAGO DE ANTICIPO …`
  - Saldo: `PAGO DE SALDO …`
  - Abono: mismo texto que el pago original con la fecha del abono (acordado para Departamento 360).
- Con factura (CFDI), cada abono lleva su IVA proporcional y exige complemento de pago (§7.7.4).
- La orden muestra el saldo pendiente y el historial de pagos.

### 6.3 Gasto sin orden (urgencia)
Casetas, Uber/DiDi, hidratación, comisiones, compras de último momento.
1. "Pago sin orden" desde la caja. **Justificación obligatoria.**
2. Se capturan las líneas (concepto, contrato, monto) o se leen del ticket.
3. Las reglas sugieren la cuenta; se generan los renglones.
4. **Regularizar** (opcional, no bloquea la exportación): al entregarse los tickets se crea una OG con `origen = regularizacion` y se liga el pago.
- Reporte de gastos sin orden por usuario y mes.

### 6.4 Caja chica administrativa (virtual)
Luis custodia el efectivo; Miriam, Isaac y David solo registran.
1. Luis hace un **traspaso virtual** de su caja a la caja chica de, por ejemplo, David.
2. David registra los gastos que Luis pagó en efectivo (con o sin orden) contra su caja chica.
3. Saldo de la caja chica = traspasos recibidos − pagos registrados.
4. Luis ve todas las cajas chicas y su saldo.

### 6.5 Fondo fijo Cancún (Miguel)
Miguel sí tiene efectivo y comprueba periódicamente.
1. La caja `fondo_fijo` tiene un monto de fondo.
2. Miguel (o administración) captura una **comprobación de fondo** con todos sus tickets: cada ticket es un pago sin orden o ligado a orden.
3. Administración revisa y registra; los renglones llevan plaza `CUN`.
4. Se cierra la comprobación y se genera un **traspaso de reposición** por el total comprobado.
- Regla vigente (como regla editable con `fuerza_nd`, caja = fondo Cancún, `con_cfdi = false`): los tickets **no llevan IVA**; todos ND con importe completo aunque el ticket muestre IVA. Con CFDI, IVA normal.
- Ejemplo real: Comprobación caja Miguel CUN oct 2026, 14 tickets, $2,049.94.

### 6.6 Tarjeta de compras (TCB, BBVA)
1. Cada compra con tarjeta es un pago en la caja `Compras` con su ticket o CFDI.
2. **No se registra una compra sin comprobante**; el cargo queda pendiente hasta que llegue (aparece en conciliación como "cargo sin pago").
3. Al cierre se importa el estado de cuenta y se concilia (§6.12).
- Los traspasos entre cuentas propias y los "pago a su crédito" se identifican en la conciliación y no generan registro.

#### 6.6.1 Pólizas
- La póliza pertenece al mes de la **fecha del movimiento**.
- Mientras un pago no se exporta, su póliza es provisional: los no exportados del mes se numeran en orden cronológico, a partir del número más alto ya exportado en ese mes.
- Al exportarse, la póliza queda fija (`poliza_fija = true`) y no se vuelve a numerar.
- **Una compra que llega tarde con fecha anterior toma el siguiente número** del mes de su fecha, aunque rompa el orden cronológico (CP-15).
- Antes de exportar se puede cambiar el número a mano, sin repetir uno existente del mismo mes.

### 6.7 AMEX (mensual)
1. Se importa el estado de cuenta del corte. Cada cargo nuevo se convierte en pago de la caja AMEX.
2. FECHA de captura = **último día del mes siguiente al corte**; PROPIETARIO = `AMERICAN`; ID_EVENTO = 0 salvo que haya contrato.
3. Desglose de IVA solo para los conceptos con regla `desglosa_iva` (§8.3).
4. Cargos en USD o del extranjero: completos, sin IVA; el monto es el cargo en pesos del estado de cuenta.
5. Créditos por aclaración: el crédito y el cargo reversado no se registran. Si el cargo original ya se exportó, se genera un **ajuste ERP** para eliminarlo (§6.15).
6. Cuadre obligatorio: Σ renglones = "nuevos cargos" del estado de cuenta.

### 6.8 Lavandería (Wilbert Vera, Lavandería 100 Años)
- **Una orden por remisión o solicitud de lavado.**
- La lectura extrae los renglones; **se agrupan por contrato** en un solo registro: conceptos con la cantidad al inicio, separados por coma; los importes se suman.
- Cuenta D03, ND.
- Pagos parciales: se marca qué partidas se pagan (en las hojas de Wilbert vienen marcadas a mano con la fecha de pago); el resto queda como saldo de la orden.
- Cargos extra sin contrato (ej. "cargo por retirar cera… $450"): se asignan a un contrato o quedan con contrato 0 y se avisan.

### 6.9 Departamento 360
- Orden por remisión (REM####) o factura (FAC####).
- Remisión: ND. Factura: sin ND, con IVA en H01 y, en abonos, IVA proporcional con complemento de pago.
- Se pagan por banco: PROPIETARIO `BBVA`.
- Sin contrato: se omite `C…`, ID_EVENTO 0, cuenta G01.
- Abonos: repiten el texto con la fecha del abono.

### 6.10 Subrentas (C06)
- Orden de subrenta (OS) por proveedor (Dilgos, Don Guillermo, Pastor, Vero Lara, Lepayu…).
- Los subtotales llevan `[CDV] - `; el H01 no.
- Con IVA → sin ND y renglón H01; sin IVA → ND.
- Merma o reposición al proveedor de subrenta: [ABIERTO §14.4] (hoy se usa A02).

### 6.11 Fletes
- Orden con proveedor, tipo de viaje (REDONDO/SENCILLO), contrato(s) con su evento (cliente y lugar) y semana.
- Programados → A03. ND sin CFDI.
- Con CFDI de persona física: retenciones (§7.7.3).
- Varios contratos → un renglón por contrato, monto en partes iguales salvo indicación.

### 6.12 Conciliación de estado de cuenta
1. Se sube el PDF del estado de cuenta de la caja; se extraen los movimientos.
2. Matching automático por monto exacto, fecha (± `dias_conciliacion` entre operación y liquidación) y autorización/RFC.
3. Resultado: conciliados, **cargos sin pago** (faltan por registrar), **pagos sin cargo** (registrados que no aparecen) y traspasos propios (ignorados).
4. Cuadre: saldo inicial + abonos − cargos = saldo final; Σ pagos de la caja en el periodo = Σ cargos.
5. Cargos liquidados después de la hora de generación del reporte (caso real: $2,000 el 30/09 que no aparecía en el reporte mensual) se detectan porque el siguiente periodo no arranca con el saldo esperado.

### 6.13 Exportación al ERP
1. El usuario filtra renglones `listo` por caja y fecha.
2. Genera el lote → Excel con el formato de §9. Las pólizas del lote quedan fijas.
3. Los renglones pasan a `exportado`; el usuario marca `fecha_registro_erp` al subirlos (lote `registrado`).
4. **Deshacer lote:** si el ERP rechazó la importación, un lote no `registrado` se anula y sus renglones vuelven a `listo`. Las pólizas siguen fijas.
5. Reporte de cuadre del lote: total, IVA total, número de pólizas, diferencias contra los documentos.

### 6.14 Comisiones y diferencias
- Ejemplo: compra en Temu pagada en OXXO, $350 + $17 de comisión.
- La comisión y la diferencia (propina, redondeo) se capturan en el pago y **se suman al renglón principal** (§7.9).
- Si en `parametros_contables` se configura `cuenta_comisiones` o `cuenta_diferencias`, se generan en renglón aparte en esa cuenta.

### 6.15 Correcciones después de exportar
El ERP **no acepta cargos negativos**, así que ProPago no genera reversos.
1. Al cancelar o corregir un pago exportado, el sistema crea un **ajuste ERP** por cada renglón afectado: `eliminar` (pago cancelado, crédito AMEX) o `modificar` (importe, cuenta, texto o contrato), con los valores antes y después.
2. La pantalla "Ajustes pendientes en el ERP" los lista por caja.
3. Quien hace el cambio en el ERP lo marca como `aplicado`.
4. El renglón en ProPago se actualiza al valor corregido y conserva su historia en la bitácora.
5. Si la corrección agrega dinero (faltó registrar algo), se registra como un pago nuevo, no como ajuste.

---

## 7. Lectura de comprobantes y motor de reglas

### 7.1 Extracción
Orden de preferencia:
1. **XML CFDI** (ya existe): RFC, UUID, fecha, conceptos, subtotal, IVA por tasa, retenciones, IEPS, total y tipo de cambio exactos. Se rechaza un UUID ya registrado.
2. **XML de complemento de pago** (nuevo): factura relacionada, parcialidad, importe pagado y saldo insoluto.
3. **PDF con texto** (remisiones de 100 Años, estados de cuenta): extracción de texto.
4. **Foto** (tickets, solicitudes manuscritas de Wilbert, notas de venta): modelo con visión. Se usa la API de Claude desde el backend (modelo configurable por variable de entorno); las fotos se guardan en el Storage de ProCompra. Devuelve campos y confianza; por debajo de `confianza_minima_vision`, o en manuscritos, se exige revisión.

Validaciones de extracción:
- Σ líneas = total escrito del documento. Si no cuadra, se marca la línea dudosa. Caso real: en una hoja el Imperial Snow se leía $200 y era $300; el total escrito lo resolvió.
- Precio unitario contra el histórico del mismo insumo (ya existe en Precios de insumos; ej. Imperial Snow siempre $15/pieza).
- Contrato con formato de 5 dígitos o especial; aviso si no está en el catálogo de contratos o rompe la serie del documento (ej. 16007 entre puros 165xx).

### 7.2 Comparación con la orden
- Proveedor/RFC, monto, conceptos y contratos.
- Avisos: ticket con más cantidad que la OC, contrato distinto entre OC y ticket, la misma compra en dos órdenes (posible duplicado), artículos que no coinciden ("probable"). La comparación de factura contra lo cotizado ya existe.

### 7.3 Pipeline del motor
Para cada pago:
1. Obtener las líneas: de las partidas aplicadas o de las líneas sin orden.
2. Cuenta de cada línea: la de la partida; si no hay, la primera regla que aplique (§4.1); si nada aplica, queda vacía y bloquea `listo`.
3. Importe base: con CFDI, de la factura (proporcional si es abono, §7.7.4); sin CFDI, el aplicado.
4. IVA, IEPS y retenciones (§7.7).
5. Reparto entre contratos (§7.6).
6. Agrupación si la plantilla lo pide (lavandería, §7.8).
7. Comisión y diferencia (§7.9).
8. Texto (§7.4–7.5).
9. Renglones: un subtotal por contrato, un único renglón H01 por grupo y uno por cada retención.
10. Validación (§11).

### 7.4 Gramática del texto (columna REF)

```
[CDV] - {PLAZA} - [{POLIZA} - ][ND - ][{CATEGORIA} - ]{CUERPO}[ C{CONTRATO}] EL {DD/MM/YY}[ SEM {NN} PAGO]
```

Orden de los prefijos (fijo):
1. `[CDV] - ` **solo si la cuenta del renglón tiene `cdv = true`** (C06 y toda la serie B). **El renglón H01 nunca lo lleva**, aunque su subtotal sí.
2. Plaza: `MID` o `CUN`.
3. Póliza: `TCB04` (dos dígitos), solo en cajas con prefijo de póliza.
4. `ND - ` si el grupo no tiene renglón H01 (§7.7).
5. Categoría: `TELA`, `PINTURA`, `LAVADO`, `#LOG`, `ESCENOGRAFIA`.
6. Cuerpo según la plantilla.
7. ` C{contrato}` si el contrato no es 0. Especiales: `CDIRECTO`, `CBODEGA`, `CMONTAJE`, `CMANTENIMIENTO`, `CPROTOTIPO`. Departamento 360 sin contrato: se omite.
8. ` EL {fecha}`: fecha del documento o del movimiento, formato `dd/mm/yy`.
9. Fletes: ` SEM {semana} PAGO`.

**Texto del renglón H01:** el mismo de sus subtotales, sin `[CDV] - `. Si el grupo tiene varios conceptos o contratos, el cuerpo los une con `, ` y los contratos con `/` (CP-01).

Texto en MAYÚSCULAS. Se conservan Ñ y acentos solo donde ya se usan (PEÑAFIEL, AÑOS); en general sin acentos.

### 7.5 Plantillas por tipo (carga inicial, editables)

| Código | Plantilla del cuerpo | Ejemplo real |
|---|---|---|
| GENERAL | `{VERBO} DE {CONCEPTO}[ A {PROVEEDOR}]` | `MID - ND - PAGO DE SERVICIO DE FRENOS UNIDAD VAN EL 06/11/26` |
| TCB | `COMPRA DE {CONCEPTO}` | `MID - TCB12 - TELA - COMPRA DE TELA TAFETAN NEGRO C17095 EL 13/10/26` |
| AMEX | `{VERBO} DE {CONCEPTO}` | `MID - PAGO DE SERVICIO DE ENERGIA ELECTRICA CFE EL 04/09/26` |
| LAVANDERIA | `LAVADO - PAGO DE LAVANDERIA DE {ITEMS} A {PROVEEDOR}` | `MID - ND - LAVADO - PAGO DE LAVANDERIA DE 19 MANTELES RECTANGULAR, 8 MANTELES REDONDOS A LAVANDERIA 100 AÑOS C17132 EL 22/10/26` |
| DEPTO360 | `PAGO DE {FABRICACION\|MANTENIMIENTO} DE {CONCEPTO} A DEPARTAMENTO 360 {REM\|FAC}{FOLIO}` | `MID - ND - PAGO DE FABRICACION DE SILLA MODELO TEQUIS SIN PINTURA CON TEJIDO A DEPARTAMENTO 360 REM2404 C16614 EL 03/10/26` |
| SUBRENTA | `PAGO DE SUBRENTA DE {ITEMS} A {PROVEEDOR}` | `[CDV] - MID - ND - PAGO DE SUBRENTA DE 14 SERVILLETAS LINO ARENA A DON GUILLERMO C17144 EL 03/10/26` |
| FLETE | `#LOG - PAGO DE FLETE {REDONDO\|SENCILLO} EVENTO {EVENTO} A {PROVEEDOR}` + `SEM nn PAGO` | `MID - ND - #LOG - PAGO DE FLETE REDONDO EVENTO ANA SANTOS, PRIVADA ALLEGRA A BENITO C16643 EL 19/06/26 SEM 25 PAGO` |
| REEMBOLSO | `REEMBOLSO A {PERSONA} POR {CONCEPTO}[ EN {TIENDA}]` | `MID - REEMBOLSO A SAMUEL POR COMPRA DE MEMORIA USB EN TECNOMUNDO C17254 EL 24/10/26` |
| CAJA_CUN | `COMPRA DE {CONCEPTO} EN {TIENDA}[ EVENTO {EVENTO}]` | `CUN - ND - COMPRA DE HIELO 5 KG Y AGUA E PURA 10 LT EN MINISUPER GM EVENTO MAJESTIC C17101 EL 18/10/26` |
| MYD_CUN | `PAGO MYD {N} PAX EVENTO {EVENTO}` | `CUN - ND - PAGO MYD 5 PAX EVENTO MIRAGE, SKY TERRACE C17022 EL 19/10/26` |
| FERRETERIA (histórico) | `COMPRA DE {DESC} EN FERROTLAPALERIA EL NIÑO JESUS` + `SEM{NN}` | `MID - COMPRA DE ESTOPA EN FERROTLAPALERIA EL NIÑO JESUS CBODEGA EL 20/08/2026 SEM30` |

Verbos: `COMPRA DE` (bienes), `PAGO DE` (servicios, rentas, fabricación, permisos), `REEMBOLSO A`, `RETIRO DE UTILIDAD`. Si el concepto ya empieza con un verbo, no se duplica.

La plantilla FERRETERIA conserva el formato histórico (año de 4 dígitos, `SEM30` sin espacio) y no se usa en registros nuevos.

### 7.6 Reparto entre contratos
- Contratos separados por `/` o `-` en la captura.
- Si la partida trae monto por contrato, se usa ese monto.
- Si no, **partes iguales** sobre el subtotal: cada parte se redondea a 2 decimales y **la diferencia de redondeo se carga al último contrato**, para que Σ partes = subtotal exacto.
  - Ej.: 3,006.90 / 7 → 429.56 × 6 + 429.54.
- **El IVA va en un solo renglón H01**, con todos los contratos en el texto (`C17095/17100`) e ID_EVENTO = el primer contrato.
- Subtotal y H01 llevan el mismo texto, salvo el prefijo `[CDV] - ` del subtotal (§7.4).

### 7.7 IVA, ND, IEPS y retenciones

#### 7.7.1 Regla general
- **Con CFDI:** subtotal e IVA del XML. Renglón H01 aparte, sin ND.
- **Sin CFDI** (ticket, remisión, nota, solicitud): importe completo, **ND**, sin H01, aunque el ticket desglose IVA.
- **Desglose calculado** (regla con `desglosa_iva` y solo se conoce el total): `subtotal = round(total / 1.16, 2)`; `iva = total − subtotal`. El IVA siempre por diferencia, para que subtotal + IVA = monto del banco al centavo.
- Todos los importes a **2 decimales** (half-up). Nunca se guardan más decimales en `registros_erp`.
- **ND si y solo si el grupo no tiene renglón H01.**

#### 7.7.2 CFDI con varias tasas, IEPS o descuento
- **Descuentos:** se usa el importe neto (la lectura de CFDI ya lo calcula así).
- **IEPS** (combustible, bebidas): se suma al gasto; no va a H01.
- **IVA 0 % o exento:** esas líneas van al gasto sin IVA. Si el CFDI tiene alguna línea al 16 % u 8 %, el grupo lleva un H01 por el IVA trasladado total y no lleva ND.
- **IVA 8 %:** va a H01 igual que el 16 %. La validación del IVA compara contra `iva_por_tasa`, no contra 16 % del subtotal.

#### 7.7.3 Retenciones (fletes y servicios de persona física)
- El **gasto va por el subtotal**, el **IVA trasladado a H01**, y **cada retención a su propia cuenta por pagar**: ISR retenido (`tipo = retencion_isr`) e IVA retenido (`tipo = retencion_iva`). Así la suma da lo realmente pagado: subtotal + IVA − retenciones = monto del banco.
- Las claves y nombres ERP de esas dos cuentas **quedan pendientes**. Mientras no existan en el catálogo, un pago con retenciones se puede capturar pero no pasar a `listo`.
- Cómo viajan las retenciones en el archivo: [ABIERTO §14.1].
- En abonos, las retenciones también se prorratean como el IVA (§7.7.4).

#### 7.7.4 Factura pagada en abonos
- Cada abono lleva su **parte proporcional del IVA**, porque el IVA se acredita al pagarse:
  `iva_abono = round(abono ÷ total_factura × iva_factura, 2)`; `subtotal_abono = abono − iva_abono`.
- **El último abono** (el que deja el saldo en cero) toma el IVA restante, `iva_factura − Σ iva de los abonos anteriores`, para que la suma de los IVA sea exactamente el de la factura.
- Lo mismo para cada retención.
- **Factura PPD:** se exige el **complemento de pago** del abono. Sin él, el abono se registra pero no pasa a `listo`. El complemento se liga por la factura relacionada y el importe pagado.
- **Factura PUE pagada en abonos:** no hay complemento que pedir; se aplica el IVA proporcional y se avisa que la factura debió ser PPD.
- Así el IVA de una factura nunca se registra dos veces.
- Anticipos: si el anticipo se paga antes de que exista la factura, va ND. Al llegar la factura, los pagos siguientes llevan el IVA proporcional de lo que resta y se avisa para revisar el anticipo.

### 7.8 Agrupación (lavandería)
- Clave de agrupación: (orden/documento, contrato).
- Ítems con formato `{cantidad} {concepto}`, unidos por `, `.
- Importe = suma de las líneas.

### 7.9 Comisiones y diferencias
- **Comisión** (ej. los $17 de OXXO en una compra de Temu) y **diferencia** entre comprobante y banco (propina, redondeo): **se suman al renglón principal**. Con varios contratos, al renglón del último contrato (igual que el residuo de redondeo). Nunca al H01.
- Se guardan aparte en el pago (`comision`, `diferencia`) para poder reportarlas.
- Si se configura `cuenta_comisiones` o `cuenta_diferencias`, van en un renglón propio en esa cuenta, con el mismo texto del principal.
- Aviso si la diferencia supera `aviso_diferencia_monto` o `aviso_diferencia_pct`.

---

## 8. Catálogo inicial

Se carga desde la pantalla (§10.4), no con migración. Lo que sigue es la carga inicial.

### 8.1 Cuentas

| Clave | nombre_erp | Verificado contra ERP | [CDV] |
|---|---|---|---|
| A02 | 100 - A02 - COSTO POR PERDIDAS | no (catálogo) | no |
| A03 | 100 - A03 - FLETES TERCERIZADOS PROGRAMADOS | no | no |
| A06 | 100 - A06 - FLOTILLA-MANTENIMIENTO | sí | no |
| A07 | 100 - A07 - FLOTILLA - ARRENDAMIENTOS | no | no |
| A09.1 | 100 - A09.1 - INSUMOS Y GASTOS EN PREPARACION DE PEDIDOS | sí | no |
| A09.2 | 100 - A09.2 - OTROS INSUMOS O SERVICIOS EN PREPARACION DE PEDIDOS | sí | no |
| A10 | 100 - A10 - OTROS LOGISTICA Y DISTRIBUCION | sí | no |
| A11.2 | 100 - A11.2 - ALIMENTO EN HORAS EXTRAS A PERSONAL EN SERVICIO | no | no |
| A12 | 100 - A12 - TERCERIZACION MONTAJE/DESMONTAJE | sí (indicado por David) | no |
| B01 | 100 - B01 - SUB-CONTRATACION | no | **sí** |
| B02 | 100 - B02 - PRODUCCION FLORAL | sí | **sí** |
| B04 | 100 - B04 - MATERIA PRIMA | no | **sí** |
| B11 | 100 - B11 - ? | no (falta nombre) | **sí** |
| C03 | 100 - C03 - MARKETING | sí | no |
| C04 | 100 - C04 - VIATICOS Y REEMBOLSOS DE VENTA | no | no |
| C06 | 100 - C06 - SUB-ARRENDAMIENTO | sí | **sí** |
| D02 | 100 - D02 - INSUMOS | sí | no |
| D03 | 100 - D03 - SUB-CONTRATACION MANTTO GENERAL INVENTARIO | **sí** | no |
| E06 | 100 - E06 - GASTOS GRAL RH | sí | no |
| E09 | 100 - E09 - REPARTO DE UTILIDAD | no | no |
| F02 | 100 - F02 - SERVICIOS PUBLICOS | sí | no |
| F03 | 100 - F03 - GASTOS DE OFICINA | sí | no |
| F04 | 100 - F04 - PERMISOS Y DERECHOS | no | no |
| F05 | 100 - F05 - VIATICOS Y REEMBOLSO OFICINA | sí | no |
| F06 | 100 - F06 - MOBILIARIO Y EQUIPO DE OFICINA | no | no |
| G01 | 100 - G01 - EQUIPO NUEVO (MOBILIARIO) | sí | no |
| G02 | 100 - G02 - HERRAMIENTAS MAYORES | no | no |
| G05 | 100 - G05 - INSTALACION EN INMUEBLE | no | no |
| H01 | 100 - H01 - IVA POR ACREDITAR | sí | **no, nunca** |
| I04 | 100 - I04 - INTERESES POR CRÉDITOS Y FINANCIAMIENTOS | no | no |
| *(pendiente)* | ISR retenido por pagar | — | no |
| *(pendiente)* | IVA retenido por pagar | — | no |

> Antes de producción: importar el catálogo completo **exportado del ERP** y marcar `verificada_erp`. El catálogo de 2024 está desactualizado; manda lo que diga David o el ERP, no la descripción del catálogo.

### 8.2 Reglas iniciales (concepto → cuenta)

| Prioridad | Condición | Cuenta | Categoría | Acciones |
|---|---|---|---|---|
| 10 | Proveedor lavandería (Wilbert Vera, Lavandería 100 Años) | D03 | LAVADO | ND, plantilla LAVANDERIA |
| 10 | Orden OS, o palabras `SUBRENTA`, `RENTA DE PISTA` | C06 | — | plantilla SUBRENTA |
| 10 | Retiro de utilidad de socios | E09 | — | |
| 10 | Palabras `MYD`, `PAX`, plaza CUN | A12 | — | plantilla MYD_CUN |
| 10 | Flete programado | A03 | #LOG | plantilla FLETE |
| 15 | Caja fondo Cancún, sin CFDI | (la que dé la siguiente regla) | — | `fuerza_nd` |
| 20 | Palabras de tela (TERGAL, TAFETAN, MANTA, BOMBAY, MAGALY, LICRA…), orden OC | A09.1 | TELA | |
| 20 | Palabras de pintura (PINTURA, THINNER, TINTA, MANCHADOR, BERELINTE) | A09.1 | PINTURA | |
| 20 | Flores, espuma floral, insumos de arreglo floral | B02 | — | |
| 20 | Hidratación, hielo, bebidas, alimentos del personal en servicio | A11.2 | — | |
| 30 | Otros insumos de preparación (hilo, listón, silicón, tornillería, masking, estopa, contactos, soportes) | A09.2 | — | |
| 30 | Uber/DiDi para compras o diligencias, casetas, combustible de logística | A10 | — | |
| 30 | Fabricación o servicio tercerizado de producción (vinil, herrería, carpintería, torres, estructuras) | B01 | — | |
| 30 | Proveedor Departamento 360, sin contrato | G01 | — | plantilla DEPTO360 |
| 40 | Telefonía, internet, CFE, basura municipal | F02 | — | |
| 40 | Papelería, tóner, agua y limpieza de oficina, reparaciones de oficina | F03 | — | |
| 40 | Festejos, premios, uniformes y gastos generales del personal | E06 | — | |
| 40 | Obsequios a clientes, suscripciones de software de marketing, hosting | C03 | — | |
| 40 | Permisos de unidades (BOB) | F04 | — | |
| 40 | Mantenimiento de unidades (frenos, servicio) | A06 | — | |
| 40 | Renta de unidades | A07 | — | |
| 50 | Merma o pérdida cobrada por proveedor | A02 | — | [ABIERTO §14.4] |

Las reglas solo **sugieren**: la cuenta que indique el usuario en la orden o el pago manda. El `[CDV]` sale de la cuenta, no de la regla.

### 8.3 IVA desglosado en AMEX (reglas con `desglosa_iva`, caja AMEX)
Combustible, Telmex, CFE, capacitación con CFDI (Growth Institute), Vivaaerobus, hospedaje con factura (City Express). Interés Flex Pricing: I04 + IVA H01. Suscripciones en USD: sin desglose.

---

## 9. Formato de exportación ERP

Hoja única, encabezados exactos:

| Columna | Contenido | Formato |
|---|---|---|
| Fecha | **Día en que se descarga el archivo** (decisión del 11/11/2026; reemplaza la regla de AMEX). Volver a bajar un lote conserva su fecha original. | Celda de **fecha** con formato `dd-mmm-yy` |
| CONCEPTO | `EFECTIVO` | texto |
| CARGO | Importe del movimiento, siempre positivo | número, 2 decimales |
| CLIENTE O GASTO EN | `cuenta_nombre_erp` del renglón | texto |
| REF | Texto del registro (§7.4) | texto |
| ID_EVENTO | Contrato numérico; 0 si no lleva contrato o es especial | entero |
| PROPIETARIO | **Nombre de la caja** del pago, en mayúsculas: `COMPRAS`, `AMERICAN`, `DAVID`… | texto |

- El encabezado de la primera columna es `Fecha` (así viene el formato del ERP).
- Se eligen pagos completos para descargar, no renglones sueltos: los subtotales y su IVA viajan juntos.

- Orden de los renglones: por póliza o fecha y, dentro de cada grupo, subtotales primero, luego H01, luego retenciones.
- La FECHA va como celda de fecha y no como texto, para que el idioma del mes no dependa del archivo. **Antes de producción se prueba con una importación real** (§14.7).

---

## 10. Edición de catálogos desde la pantalla

Objetivo: que un cambio de cuenta, regla, texto o contrato **nunca requiera una migración**. Una sola migración crea las tablas; después todo se hace aquí. Editan admin y pagos.

### 10.1 Cuentas
- Alta, edición y desactivación. Una cuenta con renglones no se borra.
- Renombrar `nombre_erp` afecta solo renglones nuevos o no exportados; los exportados conservan su copia.
- Cambiar `cdv` muestra cuántos renglones no exportados cambiarían de texto y los regenera al confirmar.

### 10.2 Reglas
- Lista ordenada por prioridad, con su condición resumida ("Proveedor Wilbert Vera → D03, LAVADO, ND").
- **Probar:** se escribe un concepto (y opcionalmente proveedor, caja, monto) y se ve qué regla aplica, con qué cuenta y qué texto queda.
- **Impacto:** al guardar un cambio se muestra cuántos renglones pasados, de los últimos 12 meses, habrían tomado otra cuenta.
- Cambiar una regla **no toca los renglones ya revisados**. Un botón "Volver a aplicar reglas a los pendientes" muestra antes y después de cada renglón no exportado y sin `editado_manual`, y aplica solo los que se confirmen.
- Las palabras clave se comparan sobre el texto normalizado (sin acentos, mayúsculas, la misma normalización de Precios de insumos). No se aceptan expresiones regulares.

### 10.3 Plantillas
- Edición con lista de placeholders disponibles: `{VERBO}`, `{CONCEPTO}`, `{PROVEEDOR}`, `{ITEMS}`, `{EVENTO}`, `{PERSONA}`, `{TIENDA}`, `{FOLIO}`, `{N}`, `{REDONDO|SENCILLO}`, `{FABRICACION|MANTENIMIENTO}`, `{REM|FAC}`.
- Un placeholder desconocido no se puede guardar.
- Vista previa con un pago de ejemplo real.

### 10.4 Importar desde el ERP
- Cuentas y contratos se pueden cargar desde un Excel exportado del ERP.
- La pantalla muestra nuevas, cambiadas y faltantes antes de aplicar.
- Las cuentas importadas quedan `verificada_erp = true`.

### 10.5 Parámetros
Edición de `parametros_contables` con su descripción y valor actual.

### 10.6 Bitácora
Cada cambio queda con quién, cuándo, antes y después. Cada renglón ERP guarda la regla que le dio la cuenta.

### 10.7 Lo que sí requiere migración
Agregar un **tipo** de condición o acción nuevo a las reglas, una columna nueva o un tipo de caja nuevo. Cambiar valores nunca.

---

## 11. Validaciones (bloquean `listo` o avisan)

**Bloquean**
- Cuenta vacía, inexistente o desactivada.
- Σ renglones del pago (subtotales + H01 − retenciones + comisión + diferencia) ≠ monto del pago.
- Importe con más de 2 decimales, o cargo negativo.
- Pago `sin_orden` sin justificación.
- Compra con tarjeta sin comprobante.
- UUID CFDI duplicado.
- Renglón H01 con prefijo `[CDV]`.
- Retención sin cuenta en el catálogo.
- Abono de factura PPD sin complemento de pago.

**Avisan**
- Mismo proveedor, monto y fecha que otro pago (posible duplicado).
- Aplicación mayor al saldo pendiente de la orden.
- Monto redondo en un proveedor que normalmente cobra con centavos.
- Contrato que no está en el catálogo, fuera de la serie o con dígitos transpuestos (16586 vs 16856).
- Diferencia entre ticket y orden (cantidades, contrato, artículos).
- Diferencia entre comprobante y banco mayor al umbral.
- Precio unitario fuera del histórico.
- IVA que no corresponde a la tasa de sus líneas (± $0.01).
- Fecha del documento muy anterior al pago, o con año distinto (nota "24/10/24").
- Subtotal de cuenta [CDV] sin el prefijo, o prefijo en una cuenta que no lo lleva.
- Factura PUE pagada en abonos.

---

## 12. Casos de prueba (entradas y salidas reales)

Todos deben pasar como pruebas automáticas del motor desde la fase 2.

**CP-01 TCB con dos contratos y montos dados**
Entrada: caja Compras, póliza 12, 13/10/26, Performance Telas, $900.16, CFDI subtotal 776.00 IVA 124.16; contratos 17095 = 291.00 (tafetán negro), 17100 = 485.00 (tafetán crudo).
Salida:
```
291.00 | A09.1 | MID - TCB12 - TELA - COMPRA DE TELA TAFETAN NEGRO C17095 EL 13/10/26 | 17095 | COMPRAS
485.00 | A09.1 | MID - TCB12 - TELA - COMPRA DE TELA TAFETAN CRUDO C17100 EL 13/10/26 | 17100 | COMPRAS
124.16 | H01   | MID - TCB12 - TELA - COMPRA DE TELA TAFETAN NEGRO, TAFETAN CRUDO C17095/17100 EL 13/10/26 | 17095 | COMPRAS
```

**CP-02 TCB, partes iguales con residuo**
Entrada: póliza 11, Tda Assis, $3,488.00, subtotal 3,006.90, IVA 481.10, contratos 17163/17066/17100/17232/17168/17252/17152, tela tergal minimat.
Salida: seis renglones de 429.56, uno de 429.54 (17152) y un H01 de 481.10. Σ = 3,488.00.

**CP-03 Desglose calculado AMEX (CFE)**
Entrada: $9,846.00, CFE, 04/09/26.
Salida: `8,487.93 F02` + `1,358.07 H01`, mismo texto, sin ND.

**CP-04 Lavandería agrupada**
Entrada: solicitud Wilbert Vera 19/09/26: 17036 19 asiento marfil 133, 19 respaldo marfil 133, 6 funda Henson 90.
Salida: `356.00 | D03 | MID - ND - LAVADO - PAGO DE LAVANDERIA DE 19 ASIENTO MARFIL, 19 RESPALDO MARFIL, 6 FUNDA HENSON A WILBERT VERA C17036 EL 19/09/26 | 17036`

**CP-05 Subrenta con IVA** *(corregido: plantilla SUBRENTA y H01 sin [CDV])*
Entrada: Dilgos, 30 servilletas tela rosa, 17300, 240.00 + IVA 38.40, 30/10/26.
Salida:
```
240.00 | C06 | [CDV] - MID - PAGO DE SUBRENTA DE 30 SERVILLETAS TELA ROSA A DILGOS C17300 EL 30/10/26 | 17300
 38.40 | H01 | MID - PAGO DE SUBRENTA DE 30 SERVILLETAS TELA ROSA A DILGOS C17300 EL 30/10/26 | 17300
```

**CP-06 Subrenta sin IVA**
Entrada: Don Guillermo, 77 servilletas lino arena, 17024, 3,080.00, 09/10/26.
Salida: `3,080.00 | C06 | [CDV] - MID - ND - PAGO DE SUBRENTA DE 77 SERVILLETAS LINO ARENA A DON GUILLERMO C17024 EL 09/10/26`

**CP-07 Flete con dos contratos**
Entrada: Logix Plus, redondo, 16620-15821, $10,900, 17/10/26, sem 42; eventos Touche/Tekik de Regil y Bravo Catering/Tekit de Regil.
Salida: dos renglones de 5,450.00 en A03 con `MID - ND - #LOG - PAGO DE FLETE REDONDO EVENTO … A LOGIX PLUS C{c} EL 17/10/26 SEM 42 PAGO`. Además, **aviso esperado**: 15821 rompe la serie de 166xx.

**CP-08 Caja Cancún (ticket con IVA impreso, sin CFDI)**
Entrada: fondo fijo Miguel, Minisuper GM 18/10/26, hielo 5 kg + E Pura 10 lt, $100.50, 17101.
Salida: `100.50 | A11.2 | CUN - ND - COMPRA DE HIELO 5 KG Y AGUA E PURA 10 LT EN MINISUPER GM EVENTO MAJESTIC C17101 EL 18/10/26`

**CP-09 Abono a remisión de Departamento 360**
Entrada: orden REM2405 (total $98,935.75), pago de $30,000 el 12/10/26.
Salida: un renglón de 30,000.00 con el texto de la remisión y fecha 12/10/26; la orden queda `parcial` con el saldo actualizado.

**CP-10 Gasto sin orden** *(corregido: la entrada trae la fecha)*
Entrada: caja chica David, 30/10/26, $142, "DiDi para hacer depósitos y cambiar dinero en el banco", justificación "urgencia".
Salida: `142.00 | A10 | MID - ND - PAGO DE DIDI PARA HACER DEPOSITOS Y CAMBIAR DINERO EN EL BANCO EL 30/10/26 | 0`

**CP-11 Conciliación con cargo faltante**
Entrada: estado de cuenta de compras de agosto con un cargo Telcel de $231.00 el 31/08 sin pago registrado.
Salida: el movimiento aparece en "cargos sin pago"; el cuadre muestra una diferencia de 231.00.

**CP-12 Regla de redondeo**
Entrada: cualquier total. Verificar que subtotal + IVA = total exacto y que ningún renglón tiene más de 2 decimales.

**CP-13 Factura PPD pagada en dos abonos** *(nuevo)*
Entrada: FAC de Departamento 360, subtotal 10,000.00, IVA 1,600.00, total 11,600.00, PPD. Abono 1: 5,000.00 con su complemento. Abono 2: 6,600.00 con su complemento.
Salida:
```
Abono 1:  4,310.34 G01 | 689.66 H01     (5,000 ÷ 11,600 × 1,600 = 689.655 → 689.66)
Abono 2:  5,689.66 G01 | 910.34 H01     (IVA restante: 1,600.00 − 689.66)
```
Σ G01 = 10,000.00; Σ H01 = 1,600.00. Sin complemento, el abono queda bloqueado.

**CP-14 Comisión sumada al principal** *(nuevo)*
Entrada: compra Temu pagada en OXXO, ticket $350.00, comisión $17.00, banco $367.00, sin CFDI.
Salida: un renglón de 367.00, ND.
Variante con CFDI (subtotal 301.72, IVA 48.28): `318.72` gasto + `48.28` H01 = 367.00.

**CP-15 Póliza de una compra que llega tarde** *(nuevo)*
Entrada: caja Compras, octubre ya exportado con TCB01–TCB12. El 03/11/26 se registra una compra con fecha 05/10/26.
Salida: póliza TCB13 de octubre: `MID - TCB13 - … EL 05/10/26`. TCB01–TCB12 no cambian.

**CP-16 Diferencia por propina** *(nuevo)*
Entrada: ticket de restaurante $482.50, cargo en banco $500.00, sin CFDI.
Salida: un renglón de 500.00, ND; el pago guarda `diferencia = 17.50`. Sin aviso: 17.50 < $50 y < 5 %.

**CP-17 Crédito AMEX sobre un cargo ya exportado** *(nuevo)*
Entrada: cargo de $1,250.00 exportado en septiembre; el corte de octubre trae su crédito por aclaración.
Salida: ningún renglón negativo; un ajuste ERP `eliminar` pendiente sobre el renglón de septiembre.

**CP-18 Factura PUE pagada en abonos** *(nuevo)*
Entrada: como CP-13 pero PUE.
Salida: mismos renglones, sin exigir complemento, con aviso "factura PUE pagada en abonos".

**CP-19 Flete con retenciones** *(nuevo; se activa cuando existan las cuentas y se resuelva §14.1)*
Entrada: CFDI de persona física, subtotal 5,000.00, IVA 800.00, IVA retenido 200.00 (4 %), total 5,600.00, pagado 5,600.00.
Salida esperada: gasto A03 5,000.00, H01 800.00 y retención de IVA 200.00 en su cuenta, de modo que el pago cuadre en 5,600.00.

---

## 13. Decisiones tomadas en la 1.1

| # | Tema | Decisión | Por qué |
|---|---|---|---|
| 1 | Tablas | Extender `movimientos_pago`, `pagos`, `ordenes_compra`, `partidas` y `facturas` en lugar de crear tablas paralelas | Evita duplicar datos y no rompe el módulo Pagos ni Crédito |
| 2 | [CDV] en H01 | Nunca | Indicado por David. El prefijo depende de la cuenta del renglón. |
| 3 | Catálogos | Tablas editables desde pantalla, con prueba, impacto, bitácora y copia del nombre ERP en cada renglón | Cambiar cuentas o reglas sin migraciones y sin alterar lo exportado |
| 4 | Retenciones | Subtotal al gasto, IVA a H01, cada retención a su cuenta por pagar | Indicado por David. Claves pendientes. |
| 5 | Abonos con factura | IVA proporcional; el último toma el resto; se exige complemento en PPD | Indicado por David. El IVA nunca se registra dos veces. |
| 6 | Comisiones y diferencias | Se suman al renglón principal (último contrato); se guardan aparte; cuenta propia opcional por parámetro | Indicado por David; queda configurable sin migración |
| 7 | Correcciones exportadas | Ajustes ERP pendientes (modificar/eliminar), nunca cargos negativos | El ERP no acepta negativos |
| 8 | Folios | Cada tipo su numeración; OC continúa la actual | Indicado por David |
| 9 | Póliza TCB | Provisional y cronológica hasta exportar; fija después; la tardía toma el siguiente número | Indicado por David. La numeración la asigna el sistema (resuelve §12.2 de la v1). |
| 10 | IVA por partida | Tasa por partida (16/8/0/exento) | Hoy es 16 % fijo y hay órdenes sin IVA |
| 11 | IEPS y descuentos | IEPS al gasto; descuentos netos | No son IVA acreditable |
| 12 | Contratos | Catálogo opcional importado del ERP; contrato desconocido avisa, no bloquea | ID_EVENTO debe existir en el ERP; da cliente, lugar y semana para fletes |
| 13 | USD | El pago siempre en pesos; la factura guarda moneda y tipo de cambio | El banco cobra en pesos |
| 14 | Saldo de cajas | Calculado: cajas chicas y fondo por traspasos − pagos; bancos por estado de cuenta | ProPago no registra ingresos |
| 15 | Deshacer lote | Permitido mientras el lote no esté `registrado`; las pólizas siguen fijas | Recuperarse de una importación rechazada |
| 16 | Aprobación | El responsable o un admin | Que nada se atore por ausencia |
| 17 | Lectura de fotos | API de Claude desde el backend; fotos en el Storage de ProCompra | Ya hay infraestructura de archivos; la llave queda en el servidor |
| 18 | Pagos anteriores | Quedan `historico`, sin renglones | No duplicar lo que ya está en el ERP |

---

## 14. Decisiones abiertas

1. **Retenciones: pendiente por decisión de David (11/11/2026).** Son casos muy raros en la operación; no se implementan en v1. Si llega uno, se captura a mano.
2. ~~Póliza TCB~~ — resuelta (§6.6.1).
3. ~~PROPIETARIO de cajas chicas y del fondo Cancún~~ — resuelta: es el nombre de la caja (§9).
4. **Merma o reposición a proveedores de subrenta:** se usó A02 (cuenta virtual); el catálogo sugiere C06 "reposición al proveedor". Definir.
5. **Nombres de cuentas no verificados** (§8.1): importar el catálogo exportado del ERP (§10.4).
6. **B11:** falta su nombre.
7. **Formato de FECHA:** confirmar con una importación de prueba que el ERP acepta la celda de fecha `dd-mmm-yy`.
8. ~~Fecha de captura~~ — resuelta: la del día de la descarga (§9).
9. **Gastos sin orden:** ¿límite de monto o de frecuencia antes de exigir una OG?
10. **Estado de cuenta por proveedor a crédito:** fuera de v1.

---

## 15. Fases

1. **Catálogos y su pantalla de edición** (§10) con importación desde el ERP; cajas, permisos por caja y folios por tipo. La migración de esta fase es la única que toca catálogos.
2. **Pagos en ProPago:** ampliar el módulo Pagos con cajas, pagos sin orden, IVA por partida, comisiones y diferencias; motor de reglas; exportación Excel; pólizas; ajustes ERP. Los casos de §12 como pruebas automáticas. **Con esto ya se reemplaza el trabajo por chat.**
3. **Lectura automática:** complementos de pago y retenciones en XML; visión para tickets y remisiones.
4. **Conciliación** de estados de cuenta (BBVA compras y AMEX primero).
5. **Cajas chicas virtuales**, fondo fijo de Cancún y aprobación de auxiliares.
6. **Cartera** de proveedores a crédito.
