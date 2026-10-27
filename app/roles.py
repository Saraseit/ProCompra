"""
app/roles.py
------------
Quién puede hacer qué. El backend usa la service_role key (se salta RLS),
así que estas tuplas son la única barrera de permisos.

Contabilidad (rol 'pagos') tiene los mismos permisos que admin, salvo crear
y gestionar usuarios, que sigue siendo solo de admin (app/api/usuarios.py).
"""

# Equivalentes a admin para todo lo que no sea usuarios.
ROLES_ADMIN = ("admin", "pagos")

# Capturan requerimientos, proveedores y órdenes. Almacén y compras conviven.
ROLES_OPERACION = ("admin", "pagos", "compras", "almacen")

# Autorizan, pagan, devuelven, cancelan y cierran órdenes.
ROLES_CONTABILIDAD = ("admin", "pagos")

ROLES_TODOS = ("admin", "compras", "almacen", "pagos")
